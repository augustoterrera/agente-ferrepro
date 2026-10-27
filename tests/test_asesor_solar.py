"""Asesor solar de FerrePro: proceso de venta consultiva y derivación automática.

Igual que en FerrePro, las plantillas del prompt están acopladas al código por frases literales:
"Te derivo con un asesor" es lo que dispara el pase a una persona. Cambiar una palabra rompe la
derivación en silencio, o deriva a todo el mundo. Estos tests fijan ese contrato.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import MagicMock, patch

from app import agent as agent_mod
from app import chatwoot_service
from app.chat_memory import Conversation
from app.chatwoot import is_handoff_acceptance, should_handoff_to_agent
from app.classifier import FLAG_LABELS
from app.models import AgentMessage
from app.retargeting import DERIVA_A_VENDEDOR

PROMPT = open("app/prompts/ferrepro_solar.md", encoding="utf-8").read()
PLANTILLAS = re.findall(r"```txt\n(.*?)```", PROMPT, re.S)
LINK = "https://calendar.app.google/abc123"


def _plantillas(fragmento: str) -> list[str]:
    return [b for b in PLANTILLAS if fragmento.lower() in b.lower()]


def _correr(salida: str, mensaje: str = "hola", history=None, **kwargs):
    class _Corrida:
        output = salida

    agente = MagicMock()
    agente.run_sync.return_value = _Corrida()
    with (
        patch.object(agent_mod.settings, "calendar_booking_url", LINK),
        patch.object(agent_mod, "build_agent", return_value=agente),
        patch.object(agent_mod, "decide_scope") as scope,
    ):
        reply = agent_mod.run_agent_reply(mensaje, history or [], **kwargs)
    return reply, agente, scope


class PlantillasDelProceso(unittest.TestCase):
    def test_la_derivacion_por_no_saber_deriva(self) -> None:
        """El pedido central: si no sabe algo, deriva solo, sin esperar un "sí" del cliente."""
        derivaciones = _plantillas("Te derivo con un asesor")
        # La de "no sé", la de reservar el turno y la del ejemplo.
        self.assertGreaterEqual(len(derivaciones), 3)
        for b in derivaciones:
            self.assertTrue(should_handoff_to_agent(b), b[:60])

    def test_los_pasos_del_proceso_no_derivan(self) -> None:
        """Si el cierre o la segmentación derivaran, el bot apagaría cada conversación apenas
        arranca y el cliente nunca llegaría a la agenda."""
        for fragmento in (
            "¿Es para tu casa",
            "Arrancamos por el que más te urge",
            "Elegí el día y la hora",
            "¿Te queda mejor a la mañana o a la tarde?",
            "Diseñamos tu sistema en software",
            "quedó tu turno",
            "El número sale de la simulación",
        ):
            bloques = _plantillas(fragmento)
            self.assertTrue(bloques, fragmento)
            for b in bloques:
                self.assertFalse(should_handoff_to_agent(b), b[:60])

    def test_el_cierre_lleva_el_link_de_agenda(self) -> None:
        for b in _plantillas("Elegí el día y la hora"):
            self.assertIn(agent_mod.CALENDAR_PLACEHOLDER, b)

    def test_ninguna_plantilla_pregunta_cuando_puede(self) -> None:
        """Regla de oro: siempre el link, nunca "¿cuándo podés?"."""
        for b in PLANTILLAS:
            self.assertNotIn("cuándo podés", b.lower())

    def test_la_aceptacion_de_una_oferta_tambien_deriva(self) -> None:
        self.assertTrue(should_handoff_to_agent(chatwoot_service.HANDOFF_ACCEPTED_REPLY))
        self.assertTrue(
            is_handoff_acceptance("si", [AgentMessage(role="assistant", content="¿Querés que te derive con un asesor?")])
        )

    def test_una_derivacion_condicional_no_deriva(self) -> None:
        self.assertFalse(should_handoff_to_agent("Si querés, te derivo con un asesor de FerrePro."))


class PreguntarNoEsDerivar(unittest.TestCase):
    """El bot a veces pregunta en vez de derivar. Una pregunta es una oferta: la conversación
    sigue con el bot hasta que el cliente dice que sí. Antes, "¿Querés que te derivo con un
    vendedor?" contenía la frase de derivación y apagaba el bot sin que el cliente aceptara."""

    OFERTAS = (
        "¿Querés que te pase con un vendedor?",
        "¿Querés que te derivo con un vendedor?",
        "¿Te derivo con un asesor de FerrePro?",
        "No tengo ese dato. ¿Querés que te derivo con un asesor para que te ayude?",
        "Te derivo con un vendedor, ¿te parece?",
        "Querés que te derivo con un asesor?",
    )

    def test_una_pregunta_no_deriva(self) -> None:
        for oferta in self.OFERTAS:
            self.assertFalse(should_handoff_to_agent(oferta), oferta)

    def test_el_si_del_cliente_si_deriva(self) -> None:
        for oferta in self.OFERTAS:
            historia = [AgentMessage(role="assistant", content=oferta)]
            self.assertTrue(is_handoff_acceptance("si", historia), oferta)
            self.assertTrue(is_handoff_acceptance("dale", historia), oferta)

    def test_una_afirmacion_sigue_derivando(self) -> None:
        for afirmacion in (
            "Te derivo con un asesor de FerrePro para que lo vea con vos.",
            "Eso te lo responde mejor un asesor del equipo.\nTe derivo con un asesor de FerrePro",
            "No tengo ese dato. Te derivo con un vendedor de FerrePro para que te ayude.",
        ):
            self.assertTrue(should_handoff_to_agent(afirmacion), afirmacion)

    def test_una_pregunta_sin_oferta_no_cuenta_como_oferta(self) -> None:
        historia = [AgentMessage(role="assistant", content="¿Es para tu casa, para una empresa o para el campo?")]
        self.assertFalse(is_handoff_acceptance("si", historia))

    def test_el_retargeting_reconoce_la_derivacion_a_asesor(self) -> None:
        self.assertTrue(DERIVA_A_VENDEDOR.search("Te derivo con un asesor de FerrePro."))


class LinkDeAgenda(unittest.TestCase):
    def test_se_inyecta_en_el_prompt(self) -> None:
        with patch.object(agent_mod.settings, "calendar_booking_url", LINK):
            prompt = agent_mod.load_system_prompt()
        self.assertIn(LINK, prompt)
        self.assertNotIn(agent_mod.CALENDAR_PLACEHOLDER, prompt)
        self.assertNotIn("NO HAY LINK DE AGENDA", prompt)

    def test_sin_link_el_cierre_deriva(self) -> None:
        with patch.object(agent_mod.settings, "calendar_booking_url", None):
            prompt = agent_mod.load_system_prompt()
        self.assertNotIn(agent_mod.CALENDAR_PLACEHOLDER, prompt)
        self.assertIn("NO HAY LINK DE AGENDA", prompt)
        self.assertTrue(should_handoff_to_agent(agent_mod.SIN_LINK_AVISO.split("cerrá con")[1]))

    def test_el_link_de_agenda_sobrevive_al_guard(self) -> None:
        cierre = f"Perfecto, ya tengo una idea. Elegí el día y la hora que te quede mejor acá 👇\n{LINK}"
        reply, _, _ = _correr(cierre, "sí, es mía, losa")
        self.assertIn(LINK, reply.text)

    def test_un_link_inventado_se_borra(self) -> None:
        reply, _, _ = _correr("Mirá nuestros equipos.\n\nhttps://ferrepro.example/precios\n\n¿Es para tu casa?")
        self.assertNotIn("ferrepro.example", reply.text)
        self.assertIn("¿Es para tu casa?", reply.text)

    def test_si_el_guard_borra_todo_deriva(self) -> None:
        """Una respuesta vacía no se puede mandar: el cliente quedaría sin contestar."""
        reply, _, _ = _correr("Agendá acá 👇\nhttps://calendar.example/inventado")
        self.assertEqual(reply.text, agent_mod.DERIVACION_DE_RESPALDO)
        self.assertTrue(should_handoff_to_agent(reply.text))


class TurnoDelAsesor(unittest.TestCase):
    def test_no_pasa_por_la_compuerta_de_rubro_de_ferrepro(self) -> None:
        """"la heladera y el aire" es una respuesta normal del diagnóstico; la compuerta de
        FerrePro la cortaba como electrodomésticos fuera de rubro."""
        reply, agente, scope = _correr("¿La casa es tuya? ¿Techo de losa o chapa?", "la heladera y el aire")
        scope.assert_not_called()
        agente.run_sync.assert_called_once()
        self.assertNotIn("no vendemos", reply.text)

    def test_no_manda_productos_al_catalogo(self) -> None:
        reply, _, _ = _correr("Perfecto. ¿Qué te hizo buscar esto ahora, los cortes o la factura?")
        self.assertEqual(reply.product_ids, [])

    def test_el_nombre_del_cliente_llega_al_modelo(self) -> None:
        _, agente, _ = _correr("Hola Juan", contact_name="Juan")
        entrada = agente.run_sync.call_args.args[0]
        self.assertIn("Nombre del cliente (perfil de WhatsApp): Juan", entrada)

    def test_el_historial_alcanza_para_todo_el_proceso(self) -> None:
        """El cierre usa el segmento del primer mensaje: con 8 mensajes de historial se perdía."""
        history = [AgentMessage(role="user", content="es para el campo")] + [
            AgentMessage(role="assistant" if i % 2 else "user", content=f"m{i}") for i in range(11)
        ]
        _, agente, _ = _correr("no hay red", history=history)
        self.assertIn("es para el campo", agente.run_sync.call_args.args[0])


class EtiquetasDelSegmento(unittest.TestCase):
    def test_el_clasificador_marca_el_segmento(self) -> None:
        self.assertTrue({"hogar", "empresa", "campo"} <= FLAG_LABELS)


class NombreEnElTurno(unittest.TestCase):
    def test_el_worker_pasa_el_nombre_de_whatsapp(self) -> None:
        conversation = Conversation(5, "chatwoot", "8", "6")
        pending = [{"id": 10, "content": "hola", "raw_payload": {"sender": {"name": "Juan Pérez"}}}]
        with (
            patch.object(chatwoot_service.chat_memory, "get_conversation", return_value=conversation),
            patch.object(chatwoot_service.chat_memory, "pending_messages", side_effect=[pending, []]),
            patch.object(chatwoot_service.chat_memory, "recent_history", return_value=[]),
            patch.object(chatwoot_service.chat_memory, "add_message"),
            patch.object(chatwoot_service.chat_memory, "mark_messages_processed"),
            patch.object(chatwoot_service.chat_memory, "create_outbox", return_value={"id": 99}),
            patch.object(chatwoot_service.chat_memory, "update_jobs"),
            patch.object(chatwoot_service.chat_memory, "update_events"),
            patch.object(chatwoot_service, "sync_crm_contact"),
            patch.object(chatwoot_service, "link_ad_referral"),
            patch.object(
                chatwoot_service, "run_agent_reply", return_value=chatwoot_service.AgentReply("hola!", [])
            ) as agente,
        ):
            chatwoot_service.process_pending_conversation_messages(5)
        self.assertEqual(agente.call_args.kwargs["contact_name"], "Juan")


if __name__ == "__main__":
    unittest.main()
