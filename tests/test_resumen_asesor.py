"""Resumen de la charla para el asesor, como nota privada en Chatwoot.

Lo que se fija acá: que salga en los tres momentos (link de agenda, derivación y reserva), que no
se duplique cuando el envío o el sweep se reintentan, que no repita si no hubo charla nueva, y que
una falla del resumen nunca tumbe el envío al cliente.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import resumen
from app.chat_memory import Conversation
from app.chatwoot import ChatwootError
from app.models import AgentMessage
from app.tasks import chatwoot_tasks as tasks

LINK = "https://calendar.app.google/8dhSCmb5b81yTk9g7"
AHORA = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)

SALIDA = resumen.Resumen(
    para="casa",
    por_que="cortes de luz en verano",
    no_puede_cortarse="heladera y wifi, trabaja desde casa",
    lugar="casa propia, techo de losa",
    factura="la mandó",
    dudas="preguntó el precio",
)


class NotaParaElAsesor(unittest.TestCase):
    def test_campos_y_estado(self) -> None:
        texto = resumen.nota(SALIDA, motivo="link")
        self.assertTrue(texto.startswith("📝 Resumen para el asesor"))
        for linea in (
            "• Para: casa",
            "• Por qué busca: cortes de luz en verano",
            "• Qué no puede cortarse: heladera y wifi, trabaja desde casa",
            "• Lugar: casa propia, techo de losa",
            "• Factura: la mandó",
            "• Dudas u objeciones: preguntó el precio",
            "• Estado: recibió el link de agenda",
        ):
            self.assertIn(linea, texto)

    def test_el_estado_lo_pone_el_codigo(self) -> None:
        """El turno sale del calendario, no del modelo: así no puede inventar un día."""
        self.assertIn("• Estado: reservó para el jueves 5/11 a las 17 hs", resumen.nota(SALIDA, motivo="reserva", turno="jueves 5/11 a las 17 hs"))
        self.assertIn(
            "• Estado: el bot lo derivó a un asesor · tiene turno el jueves 5/11 a las 17 hs",
            resumen.nota(SALIDA, motivo="derivacion", turno="jueves 5/11 a las 17 hs"),
        )

    def test_campo_vacio_o_largo(self) -> None:
        largo = SALIDA.model_copy(update={"lugar": "x" * 500, "dudas": "  "})
        texto = resumen.nota(largo, motivo="link")
        self.assertIn("• Dudas u objeciones: no lo dijo", texto)
        linea = next(l for l in texto.splitlines() if l.startswith("• Lugar:"))
        self.assertLessEqual(len(linea), len("• Lugar: ") + resumen.CAMPO_MAX_CHARS)

    def test_falla_del_modelo_es_reintentable(self) -> None:
        with patch.object(resumen, "build_resumidor", side_effect=TimeoutError("openai")):
            with self.assertRaises(resumen.ResumenError):
                resumen.resumir([AgentMessage(role="user", content="hola")])


class CuandoSeDispara(unittest.TestCase):
    def _despues_de_enviar(self, content: str):
        with (
            patch.object(tasks.settings, "calendar_booking_url", LINK),
            patch.object(tasks, "_dispatch_resumen") as dispatch,
        ):
            tasks._resumir_si_corresponde({"id": 41, "conversation_id": 5, "content": content})
        return dispatch

    def test_link_de_agenda(self) -> None:
        dispatch = self._despues_de_enviar(f"Elegí el día y la hora acá 👇\n{LINK}")
        dispatch.assert_called_once_with(5, "link", "outbox:41")

    def test_derivacion(self) -> None:
        dispatch = self._despues_de_enviar("Perfecto, te derivo con un asesor de FerrePro para reservarte el turno.")
        dispatch.assert_called_once_with(5, "derivacion", "outbox:41")

    def test_mensaje_comun_no(self) -> None:
        self._despues_de_enviar("¿Qué te hizo buscar esto ahora, los cortes o la factura?").assert_not_called()

    def test_ofrecer_derivar_no_es_derivar(self) -> None:
        self._despues_de_enviar("¿Querés que te derive con un asesor?").assert_not_called()

    def test_apagado_por_env(self) -> None:
        with (
            patch.object(tasks.settings, "resumen_asesor_enabled", False),
            patch.object(tasks.resumir_para_asesor, "apply_async") as encolar,
        ):
            tasks._dispatch_resumen(5, "link", "outbox:41")
        encolar.assert_not_called()

    def test_broker_caido_no_tumba_el_envio(self) -> None:
        with patch.object(tasks.resumir_para_asesor, "apply_async", side_effect=ConnectionError("redis")):
            tasks._dispatch_resumen(5, "link", "outbox:41")


class LaTask(unittest.TestCase):
    def correr(self, *, state=None, ultimo_mensaje=AHORA, redis_existe=False, nota_falla=False):
        conv = Conversation(5, "chatwoot", "805", "6", state=state or {})
        chatwoot = MagicMock()
        if nota_falla:
            chatwoot.create_outgoing_message.side_effect = ChatwootError("chatwoot caído")
        redis_ = MagicMock()
        redis_.exists.return_value = redis_existe
        redis_.set.return_value = True
        self.chatwoot, self.redis = chatwoot, redis_
        with (
            patch.object(tasks.chat_memory, "get_conversation", return_value=conv),
            patch.object(tasks, "_chatwoot_client_for", return_value=(chatwoot, 6)),
            patch.object(tasks.chat_memory, "last_user_message_at", return_value=ultimo_mensaje),
            patch.object(tasks.chat_memory, "recent_history", return_value=[AgentMessage(role="user", content="para mi casa")]),
            patch.object(tasks.chat_memory, "last_user_payload", return_value={"sender": {"name": "Juan Pérez"}}),
            patch.object(tasks.chat_memory, "merge_state") as self.merge,
            patch.object(tasks.resumen, "resumir", return_value=SALIDA) as self.resumir,
            patch.object(tasks, "_redis", return_value=redis_),
        ):
            return tasks.resumir_para_asesor.run("5", "link", "outbox:41")

    def test_deja_la_nota_y_lo_anota(self) -> None:
        r = self.correr()
        self.assertEqual(r["status"], "enviado")
        args = self.chatwoot.create_outgoing_message.call_args
        self.assertTrue(args.kwargs["private"])
        self.assertIn("• Para: casa", args.args[2])
        self.assertEqual(self.resumir.call_args.kwargs["nombre"], "Juan")
        self.assertEqual(self.merge.call_args.args[1]["resumen_asesor"]["motivo"], "link")
        self.redis.set.assert_called_once()
        self.assertEqual(self.redis.set.call_args.args[0], "chatwoot:resumen:outbox:41")

    def test_reintento_no_duplica(self) -> None:
        self.assertEqual(self.correr(redis_existe=True)["status"], "ya_resumido")
        self.resumir.assert_not_called()
        self.chatwoot.create_outgoing_message.assert_not_called()

    def test_sin_charla_nueva_no_repite(self) -> None:
        """Reservó enseguida después del link: el resumen del link ya dice todo."""
        previo = {"resumen_asesor": {"at": AHORA.isoformat(), "motivo": "link"}}
        self.assertEqual(self.correr(state=previo, ultimo_mensaje=AHORA - timedelta(minutes=5))["status"], "sin_novedades")
        self.resumir.assert_not_called()

    def test_con_charla_nueva_resume_de_nuevo(self) -> None:
        previo = {"resumen_asesor": {"at": AHORA.isoformat(), "motivo": "link"}}
        self.assertEqual(self.correr(state=previo, ultimo_mensaje=AHORA + timedelta(minutes=5))["status"], "enviado")

    def test_si_chatwoot_falla_libera_la_clave_para_reintentar(self) -> None:
        with self.assertRaises(ChatwootError):
            self.correr(nota_falla=True)
        self.redis.delete.assert_called_once_with("chatwoot:resumen:outbox:41")
        self.merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
