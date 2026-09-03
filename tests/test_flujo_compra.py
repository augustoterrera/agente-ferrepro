"""Flujo de compra: link como llamado a la acción, y derivación ordenada.

Las plantillas del prompt están acopladas al código por frases literales. Cambiar una palabra
rompe el flujo en silencio: el cliente acepta y no pasa nada, o se deriva a todo el mundo.
Estos tests fijan ese contrato.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import MagicMock, patch

from app import agent as agent_mod
from app.agent import guard_links
from app.chatwoot import extract_message_event, is_handoff_acceptance, should_handoff_to_agent
from app.scope import ScopeDecision
from app.models import AgentMessage
from app.tasks import chatwoot_tasks as tasks

PROMPT = open("app/prompts/ferrepro.md", encoding="utf-8").read()
PLANTILLAS = re.findall(r"```txt\n(.*?)```", PROMPT, re.S)


def _compra() -> list[str]:
    return [b for b in PLANTILLAS if "directo desde ese link" in b or "recibí tu selección" in b]


class PlantillasDeCompra(unittest.TestCase):
    def test_existen(self) -> None:
        self.assertGreaterEqual(len(_compra()), 2)

    def test_no_derivan_solas(self) -> None:
        """Si la plantilla de compra dispara el handoff, se deriva a todo el que pregunte un
        precio: sucursales inundadas y la venta online muerta."""
        for b in _compra():
            self.assertFalse(should_handoff_to_agent(b), b[:60])

    def test_un_si_del_cliente_deriva(self) -> None:
        """La oferta debe usar la fórmula que el sistema reconoce. Con otra redacción el
        cliente acepta y no pasa nada."""
        for b in _compra():
            self.assertTrue(
                is_handoff_acceptance("si", [AgentMessage(role="assistant", content=b)]), b[:60]
            )

    def test_ofrecen_el_10_por_ciento(self) -> None:
        for b in _compra():
            self.assertIn("10% de descuento", b)


class LinkComoLlamadoALaAccion(unittest.TestCase):
    def test_el_guard_deja_pasar_un_link_ya_mostrado(self) -> None:
        """El link de compra se repite de memoria, sin volver a buscar. Si el guard lo borra,
        el mensaje sale diciendo 'comprá desde el link' sin ningún link."""
        link = "https://www.ferreproindustrial.com/productos/taladro-abc"
        respuesta = f"Taladro ABC\n🔗 {link}\n\nPodés comprarlo directo desde ese link."
        self.assertIn(link, guard_links(respuesta, {link}))

    def test_el_guard_sigue_borrando_links_inventados(self) -> None:
        inventado = "https://www.ferreproindustrial.com/productos/no-existe"
        respuesta = f"Mirá esto\n\n🔗 {inventado}"
        self.assertNotIn(inventado, guard_links(respuesta, set()))


class HuecosDetectadosPorMutacion(unittest.TestCase):
    """Tres comportamientos que la suite NO atrapaba: se rompió el código a propósito y los
    tests seguían en verde. Cada uno de estos falla si se revierte el fix correspondiente."""

    def test_una_oferta_condicional_no_deriva_sola(self) -> None:
        """El bot ofrece derivar muchas veces por conversación. Si "si querés te derivo" contara
        como derivación, se derivaría a todo el mundo apenas se menciona un vendedor."""
        self.assertFalse(should_handoff_to_agent("Si querés te derivo con un vendedor de FerrePro."))
        self.assertTrue(should_handoff_to_agent("Perfecto, te derivo con un vendedor de FerrePro."))

    def test_una_nota_privada_no_es_un_mensaje_del_cliente(self) -> None:
        """Las notas privadas son coordinación interna entre sucursales. Si el intake las tomara
        como mensajes entrantes, el bot le contestaría a los propios empleados."""
        nota = {
            "event": "message_created",
            "message_type": "incoming",
            "private": True,
            "content": "ojo que este cliente ya vino",
            "conversation": {"id": 2117},
        }
        evento, motivo = extract_message_event(nota)
        self.assertIsNone(evento)
        self.assertEqual(motivo, "ignored_private_message")

    def test_el_link_del_cta_sobrevive_al_guard(self) -> None:
        """Prueba el cableado real de run_agent_reply, no guard_links por separado: el bot escribe
        el link de memoria (sin buscar de nuevo) y el guard tiene que dejarlo pasar."""
        link = "https://www.ferreproindustrial.com/productos/taladro-abc"
        historia = [AgentMessage(role="assistant", content=f"Taladro ABC\n🔗 {link}/")]

        class _Corrida:
            output = f"Taladro ABC\n🔗 {link}\n\nPodés comprarlo directo desde ese link."

        agente = MagicMock()
        agente.run_sync.return_value = _Corrida()
        with (
            patch.object(agent_mod, "decide_scope", return_value=ScopeDecision("in_scope")),
            patch.object(agent_mod, "build_agent", return_value=agente),
        ):
            # "si" no pide repetir el link: es justo el caso donde antes se borraba.
            respuesta = agent_mod.run_agent_reply("si", historia)
        self.assertIn(link, respuesta.text)


class ClasificacionDespuesDelHandoff(unittest.TestCase):
    """El clasificador reescribe TODAS las etiquetas. Si corre a la par del handoff, pisa el
    `bot_apagado` y el bot sigue contestando una conversación ya derivada."""

    def test_el_envio_dispara_la_clasificacion_al_final(self) -> None:
        with patch.object(tasks, "classify_and_label_conversation") as clasif:
            tasks._dispatch_classify("77")
        clasif.apply_async.assert_called_once()
        self.assertEqual(clasif.apply_async.call_args.args[0], ("77",))

    def test_sin_conversacion_no_clasifica(self) -> None:
        with patch.object(tasks, "classify_and_label_conversation") as clasif:
            tasks._dispatch_classify(None)
        clasif.apply_async.assert_not_called()

    def test_el_turno_ya_no_clasifica_en_paralelo(self) -> None:
        """La regresión que hay que evitar: volver a despachar la clasificación por su cuenta
        desde el turno. Ahí es donde nacía la carrera con el handoff."""
        conv = tasks.chat_memory.Conversation(5, "chatwoot", "13", "6")
        with (
            patch.object(tasks, "_conversation_lock"),
            patch.object(tasks, "_debounce_active", return_value=False),
            patch.object(tasks.chat_memory, "get_conversation", return_value=conv),
            patch.object(tasks.chat_memory, "acquire_lock", return_value=True),
            patch.object(tasks.chat_memory, "release_lock"),
            patch.object(tasks.chat_memory, "update_jobs"),
            patch.object(tasks, "process_pending_conversation_messages", return_value=(99, True)),
            patch.object(tasks, "send_chatwoot_outbound_message") as envio,
            patch.object(tasks, "classify_and_label_conversation") as clasif,
        ):
            tasks.process_chatwoot_conversation.run("13")

        clasif.apply_async.assert_not_called()
        # La clasificación viaja como argumento del envío, que la dispara recién al terminar.
        self.assertEqual(envio.apply_async.call_args.args[0], ("99", "13"))

    def test_sin_clasificar_el_envio_no_arrastra_conversacion(self) -> None:
        conv = tasks.chat_memory.Conversation(5, "chatwoot", "13", "6")
        with (
            patch.object(tasks, "_conversation_lock"),
            patch.object(tasks, "_debounce_active", return_value=False),
            patch.object(tasks.chat_memory, "get_conversation", return_value=conv),
            patch.object(tasks.chat_memory, "acquire_lock", return_value=True),
            patch.object(tasks.chat_memory, "release_lock"),
            patch.object(tasks.chat_memory, "update_jobs"),
            patch.object(tasks, "process_pending_conversation_messages", return_value=(99, False)),
            patch.object(tasks, "send_chatwoot_outbound_message") as envio,
        ):
            tasks.process_chatwoot_conversation.run("13")
        self.assertEqual(envio.apply_async.call_args.args[0], ("99", None))

    def test_una_falla_de_envio_no_pierde_la_clasificacion(self) -> None:
        """Regresión detectada en auditoría: al acoplar clasificación y envío, los finales que
        NO son exitosos dejaban la conversación sin etapa ni flags para siempre."""
        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=None),
            patch.object(tasks, "classify_and_label_conversation") as clasif,
        ):
            r = tasks.send_chatwoot_outbound_message.run("99", "13")
        self.assertEqual(r["status"], "not_found")
        clasif.apply_async.assert_called_once_with(("13",), queue="chatwoot_outbound")

    def test_el_sweeper_de_outbox_tambien_arrastra_clasificacion(self) -> None:
        """Si Celery cae entre crear el outbox y encolarlo, el beat lo rescata. Ese camino también
        tiene que clasificar; si no, el mensaje sale pero el BI/retargeting pierden etapa y flags."""
        with (
            patch.object(
                tasks.chat_memory,
                "pending_outbox",
                return_value=[{"id": 99, "conversation_id": 13}],
            ),
            patch.object(tasks, "send_chatwoot_outbound_message") as envio,
        ):
            r = tasks.dispatch_pending_outbox_messages()

        self.assertEqual(r, {"ok": True, "dispatched": 1})
        envio.apply_async.assert_called_once_with(("99", "13"), queue="chatwoot_outbound")


if __name__ == "__main__":
    unittest.main()
