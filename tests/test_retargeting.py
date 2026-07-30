from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from app import chat_memory, retargeting
from app.chat_memory import Conversation
from app.chatwoot import ChatwootClient
from app.tasks import chatwoot_tasks as tasks


class _Run:
    def __init__(self, output: retargeting.Followup):
        self.output = output


class _Redactor:
    def __init__(self, output: retargeting.Followup):
        self.output = output

    def run_sync(self, _prompt: str) -> _Run:
        return _Run(self.output)


class RetargetingChecks(unittest.TestCase):
    def test_perdida_exactamente_en_el_proximo_tick(self) -> None:
        cierre = datetime(2026, 8, 4, 10, 0, tzinfo=retargeting.TZ)
        now = datetime(2026, 8, 4, 9, 40, tzinfo=retargeting.TZ)
        self.assertTrue(retargeting.es_perdida_definitiva(cierre, now))

    def _componer(self, mensaje: str) -> retargeting.Followup:
        output = retargeting.Followup(vale_la_pena=True, motivo="sí", mensaje=mensaje)
        with patch("app.retargeting.build_redactor", return_value=_Redactor(output)):
            return retargeting.compose_followup([retargeting.AgentMessage(role="user", content="taladro")])

    def test_redactor_tolera_pasarse_del_objetivo(self) -> None:
        # 300 es el objetivo del prompt, no un corte: pasarse es cosmético y descartar acá
        # quemaría el lead (one-shot) por nada.
        self.assertTrue(self._componer("x" * 301).vale_la_pena)

    def test_redactor_rechaza_un_mensaje_descontrolado(self) -> None:
        with self.assertRaises(retargeting.RedactorError):
            self._componer("x" * (retargeting.MENSAJE_MAX_CHARS + 1))

    def test_dedup_protege_tambien_las_respuestas_normales(self) -> None:
        # El dedup no es exclusivo del retargeting: recibir dos veces la misma respuesta del bot
        # es igual de molesto, y pasa por lo mismo (POST entregado, confirmación perdida).
        outbox = {
            "id": 1, "conversation_id": 5, "external_conversation_id": "77", "status": "pending",
            "content": "Tengo la Bosch GWS 850.", "attempts": 1,
            "idempotency_key": "chatwoot:5:abc", "created_at": "2026-08-04T09:00:00+00:00",
        }
        posts: list[str] = []

        class _Client:
            def has_outgoing_message(self, *a, **k) -> bool:
                return True  # el POST anterior sí había llegado

            def create_outgoing_message(self, _a, _c, content):
                posts.append(content)
                return {}

        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=outbox),
            patch.object(tasks.chat_memory, "mark_outbox_processing", return_value=True),
            patch.object(tasks.chat_memory, "get_conversation",
                         return_value=Conversation(5, "chatwoot", "77", "6")),
            patch.object(tasks.chat_memory, "mark_outbox_sent"),
            patch.object(tasks, "build_chatwoot_client", return_value=_Client()),
            patch.object(tasks, "_handoff_if_needed", return_value=False),
        ):
            tasks.send_chatwoot_outbound_message("1")
        self.assertEqual(posts, [], "no puede volver a postear un mensaje que ya se entregó")

    def test_dedup_solo_mira_mensajes_posteriores_al_outbox(self) -> None:
        client = ChatwootClient("https://chatwoot.test", "token")
        payload = {
            "payload": [
                {
                    "message_type": "outgoing",
                    "content": "¿Seguís interesado en el taladro?",
                    "created_at": "2026-08-04T08:59:00Z",
                },
                {
                    "message_type": "outgoing",
                    "content": "Otro texto",
                    "created_at": "2026-08-04T09:01:00Z",
                },
            ]
        }
        with patch.object(ChatwootClient, "_request", return_value=payload):
            self.assertFalse(
                client.has_outgoing_message(
                    1,
                    2,
                    "¿Seguís interesado en el taladro?",
                    created_after="2026-08-04T09:00:00Z",
                )
            )
            payload["payload"][1]["content"] = "¿Seguís interesado en el taladro?"
            self.assertTrue(
                client.has_outgoing_message(
                    1,
                    2,
                    "¿Seguís interesado en el taladro?",
                    created_after="2026-08-04T09:00:00Z",
                )
            )

    def test_intake_atomico_devuelve_conversacion_y_job(self) -> None:
        response = {
            "is_new": True,
            "conversation": {
                "id": 7,
                "channel": "chatwoot",
                "external_conversation_id": "42",
                "account_id": "1",
                "state": {},
            },
            "job_id": 9,
        }
        with patch("app.chat_memory.supabase.rpc", return_value=response) as rpc:
            is_new, conversation, job_id = chat_memory.persist_incoming_event(
                event_key="evt",
                channel="chatwoot",
                external_conversation_id="42",
                external_contact_id="3",
                account_id="1",
                external_message_id="5",
                content="hola",
                raw_payload={"id": 5},
                max_attempts=5,
            )
        self.assertTrue(is_new)
        self.assertEqual(conversation.id, 7)
        self.assertEqual(job_id, 9)
        self.assertEqual(rpc.call_args.args[0], "chat_persist_incoming_event")


if __name__ == "__main__":
    unittest.main()
