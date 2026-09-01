"""Reparto de derivaciones entre sucursales y vigilancia de intromisiones.

Chatwoot Community no puede ocultar conversaciones (los roles custom son de pago), así que la
separación entre sucursales es por asignación + auditoría, no por permisos. Estos tests fijan
las dos mitades.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import main
from app.chat_memory import Conversation
from app.chatwoot import AgentIntrusion, detect_agent_intrusion, pick_sucursal
from app.tasks import chatwoot_tasks as tasks

SUCURSALES = [10, 30, 31]  # Avellaneda, Saenz Peña, Francisco de Aguirre
BOT = 32


class Reparto(unittest.TestCase):
    def test_reparte_parejo(self) -> None:
        """Los ids de conversación de Chatwoot son incrementales → el módulo ES round-robin."""
        asignados = [pick_sucursal(cid, SUCURSALES) for cid in range(100, 130)]
        for sucursal in SUCURSALES:
            self.assertEqual(asignados.count(sucursal), 10)

    def test_es_estable_ante_reintentos(self) -> None:
        """Si el handoff se reintenta, la conversación no puede saltar de sucursal."""
        self.assertEqual(pick_sucursal(2116, SUCURSALES), pick_sucursal(2116, SUCURSALES))

    def test_sin_sucursales_usa_el_fallback(self) -> None:
        """Un deploy a medio configurar no puede dejar la derivación sin dueño."""
        self.assertEqual(pick_sucursal(5, [], fallback=29), 29)
        self.assertIsNone(pick_sucursal(5, [], fallback=None))

    def test_id_no_numerico_no_explota(self) -> None:
        self.assertEqual(pick_sucursal("abc", SUCURSALES), SUCURSALES[0])
        self.assertEqual(pick_sucursal(None, SUCURSALES), SUCURSALES[0])

    def test_acepta_el_id_como_string(self) -> None:
        """external_conversation_id viene como texto desde chat_conversations."""
        self.assertEqual(pick_sucursal("2116", SUCURSALES), pick_sucursal(2116, SUCURSALES))


class HandoffAsigna(unittest.TestCase):
    def _handoff(self, sucursales, fallback=None):
        conv = Conversation(5, "chatwoot", "2117", "6")
        client = MagicMock()
        client.get_conversation_labels.return_value = []
        client.set_conversation_labels.return_value = ["bot_apagado"]
        with (
            patch.object(tasks.settings, "chatwoot_sucursal_agent_ids", sucursales),
            patch.object(tasks.settings, "chatwoot_assignee_id", fallback),
            patch.object(tasks, "should_handoff_to_agent", return_value=True),
            patch.object(tasks, "detect_handoff_flags", return_value=()),
            patch.object(tasks, "sync_crm_labels"),
        ):
            tasks._handoff_if_needed(client, "6", conv, "te paso con un vendedor")
        return client

    def test_deriva_a_la_sucursal_que_toca(self) -> None:
        client = self._handoff(SUCURSALES)
        # 2117 % 3 == 2 → Francisco de Aguirre
        client.assign_conversation.assert_called_once_with("6", "2117", 31)

    def test_sin_sucursales_cae_al_assignee_viejo(self) -> None:
        client = self._handoff([], fallback=29)
        client.assign_conversation.assert_called_once_with("6", "2117", 29)

    def test_sin_nada_configurado_no_asigna(self) -> None:
        client = self._handoff([], fallback=None)
        client.assign_conversation.assert_not_called()


def _saliente(sender_id: int, assignee_id: int | None, **extra) -> dict:
    meta = {"assignee": {"id": assignee_id, "name": f"agente {assignee_id}"}} if assignee_id else {}
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": "hola, te ayudo yo",
        "sender": {"id": sender_id, "name": f"agente {sender_id}", "type": "user"},
        "conversation": {"id": 2117, "meta": meta},
    }
    payload.update(extra)
    return payload


class Vigilancia(unittest.TestCase):
    def _detect(self, payload) -> AgentIntrusion | None:
        return detect_agent_intrusion(payload, SUCURSALES, BOT)

    def test_sucursal_contesta_la_de_otra(self) -> None:
        i = self._detect(_saliente(30, 31))
        self.assertEqual(i.motivo, "asignada_a_otro")
        self.assertEqual((i.sender_id, i.assignee_id), (30, 31))

    def test_sucursal_contesta_una_sin_asignar(self) -> None:
        """El caso que más molesta: contestan sin que se los haya derivado."""
        i = self._detect(_saliente(30, None))
        self.assertEqual(i.motivo, "sin_asignar")
        self.assertIsNone(i.assignee_id)

    def test_sucursal_contesta_la_suya(self) -> None:
        self.assertIsNone(self._detect(_saliente(30, 30)))

    def test_el_bot_nunca_es_intromision(self) -> None:
        """El bot escribe en TODAS las conversaciones: sin esto, alerta permanente."""
        self.assertIsNone(self._detect(_saliente(BOT, None)))
        self.assertIsNone(self._detect(_saliente(BOT, 31)))

    def test_los_admins_nunca_son_intromision(self) -> None:
        """Ale (28) y Felipe (29) supervisan: es legítimo que contesten donde sea."""
        self.assertIsNone(self._detect(_saliente(28, 31)))
        self.assertIsNone(self._detect(_saliente(29, None)))

    def test_ignora_mensajes_del_cliente(self) -> None:
        self.assertIsNone(self._detect(_saliente(30, 31, message_type="incoming")))

    def test_ignora_notas_privadas(self) -> None:
        """La nota privada no la ve el cliente: coordinar por ahí no es meterse."""
        self.assertIsNone(self._detect(_saliente(30, 31, private=True)))

    def test_ignora_otros_eventos(self) -> None:
        self.assertIsNone(self._detect(_saliente(30, 31, event="conversation_updated")))

    def test_sin_sucursales_configuradas_no_vigila(self) -> None:
        self.assertIsNone(detect_agent_intrusion(_saliente(30, 31), [], BOT))

    def test_sin_bot_configurado_no_vigila(self) -> None:
        """Fail-safe: si no sabemos quién es el bot, sus propios mensajes se leerían como
        intromisión de la sucursal cuyo usuario esté usando (pasa al migrar el token).
        Callado es mejor que acusar en falso."""
        self.assertIsNone(detect_agent_intrusion(_saliente(30, 31), SUCURSALES, None))
        self.assertIsNone(detect_agent_intrusion(_saliente(10, None), SUCURSALES, None))

    def test_payload_roto_no_explota(self) -> None:
        for roto in ({}, {"event": "message_created"}, {"event": "message_created", "message_type": "outgoing"}):
            self.assertIsNone(self._detect(roto))


class VigilanciaEnElWebhook(unittest.TestCase):
    """La vigilancia vive dentro del webhook que ya existe: los mensajes de los agentes llegan
    ahí igual (se descartan por no ser "incoming"). Lo crítico es que no pueda romper la
    atención al cliente."""

    def _post(self, payload, token="secreto"):
        from starlette.testclient import TestClient

        from app.main import app

        with (
            patch.object(main.settings, "chatwoot_webhook_secret", "secreto"),
            patch.object(main.settings, "chatwoot_sucursal_agent_ids", SUCURSALES),
            patch.object(main.settings, "chatwoot_bot_agent_id", BOT),
            patch.object(main.notifier, "notify_warning", return_value=True) as alerta,
        ):
            resp = TestClient(app).post(f"/webhooks/chatwoot?token={token}", json=payload)
        return resp, alerta

    def test_intromision_dispara_la_alerta(self) -> None:
        resp, alerta = self._post(_saliente(30, 31))
        self.assertEqual(resp.status_code, 200)
        # El webhook igual responde "no manejado": el mensaje de un agente no es del cliente.
        self.assertFalse(resp.json()["handled"])
        alerta.assert_called_once()
        contexto = alerta.call_args.args[1]
        self.assertIn("30", str(contexto["contestó"]))
        self.assertIn("31", str(contexto["asignada a"]))

    def test_mensaje_del_bot_no_alerta(self) -> None:
        resp, alerta = self._post(_saliente(BOT, 31))
        self.assertEqual(resp.status_code, 200)
        alerta.assert_not_called()

    def test_si_la_alerta_falla_el_webhook_no_se_cae(self) -> None:
        """Una alerta rota jamás puede cortar la entrada de mensajes."""
        from starlette.testclient import TestClient

        from app.main import app

        with (
            patch.object(main.settings, "chatwoot_webhook_secret", "secreto"),
            patch.object(main.settings, "chatwoot_sucursal_agent_ids", SUCURSALES),
            patch.object(main.settings, "chatwoot_bot_agent_id", BOT),
            patch.object(main, "detect_agent_intrusion", side_effect=RuntimeError("boom")),
        ):
            resp = TestClient(app).post("/webhooks/chatwoot?token=secreto", json=_saliente(30, 31))
        self.assertEqual(resp.status_code, 200)

    def test_token_invalido_rebota(self) -> None:
        resp, alerta = self._post(_saliente(30, 31), token="cualquiera")
        self.assertEqual(resp.status_code, 401)
        alerta.assert_not_called()


if __name__ == "__main__":
    unittest.main()
