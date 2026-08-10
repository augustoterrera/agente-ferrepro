"""Atribución de conversaciones a la pauta (migración 003).

El `referral` del anuncio viaja UNA sola vez, en el primer mensaje del cliente, dentro de un
webhook que solo estamos reenviando. Si no se captura ahí, no hay forma de reconstruirlo.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import chat_memory, chatwoot_service, meta
from app.chat_memory import Conversation
from app.supabase import SupabaseError

REFERRAL = {
    "source_url": "https://fb.me/2x9aBcD",
    "source_id": "120210000000000000",
    "source_type": "ad",
    "headline": "Herramientas Emtop en Tucumán",
    "body": "Envíos a todo el país",
    "media_type": "image",
    "ctwa_clid": "ARAaZuKq1x",
}


def _payload(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"profile": {"name": "Juan"}, "wa_id": "5493816506312"}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def _mensaje_de_aviso() -> dict:
    return {
        "from": "5493816506312",
        "id": "wamid.HBgNNTQ5MzgxNjUwNjMxMhUCABIYFjNFQjA",
        "timestamp": "1786377600",
        "type": "text",
        "text": {"body": "hola, vi el aviso de los taladros"},
        "referral": dict(REFERRAL),
    }


class ExtraerReferral(unittest.TestCase):
    def test_saca_los_campos_de_atribucion(self) -> None:
        [ref] = meta.extract_ad_referrals(_payload(_mensaje_de_aviso()))
        self.assertEqual(ref["phone"], "5493816506312")
        self.assertEqual(ref["source_id"], "120210000000000000")
        self.assertEqual(ref["ctwa_clid"], "ARAaZuKq1x")
        self.assertEqual(ref["wa_message_id"], "wamid.HBgNNTQ5MzgxNjUwNjMxMhUCABIYFjNFQjA")
        # El crudo va entero: Meta suma campos al referral sin avisar.
        self.assertEqual(ref["raw"], REFERRAL)

    def test_mensaje_organico_no_genera_nada(self) -> None:
        organico = {"from": "5493816506312", "id": "wamid.X", "type": "text", "text": {"body": "hola"}}
        self.assertEqual(meta.extract_ad_referrals(_payload(organico)), [])

    def test_payload_roto_no_explota(self) -> None:
        for roto in ({}, {"entry": None}, {"entry": [{"changes": [{"value": {"messages": [None]}}]}]}):
            self.assertEqual(meta.extract_ad_referrals(roto), [])

    def test_sin_telefono_se_descarta(self) -> None:
        """Sin teléfono el referral es inatable: no sirve de nada guardarlo."""
        sin_from = {"id": "wamid.X", "type": "text", "referral": dict(REFERRAL)}
        self.assertEqual(meta.extract_ad_referrals(_payload(sin_from)), [])


class GuardarReferral(unittest.TestCase):
    def test_reintento_de_meta_no_ensucia_los_logs(self) -> None:
        """Meta reintenta webhooks; el índice único devuelve 409 y eso es lo esperado."""
        with (
            patch.object(meta, "insert", side_effect=SupabaseError("409 Conflict")),
            patch.object(meta.logger, "warning") as warned,
        ):
            self.assertEqual(meta.save_ad_referrals(_payload(_mensaje_de_aviso())), 0)
        warned.assert_not_called()

    def test_un_supabase_caido_no_corta_la_entrega(self) -> None:
        with (
            patch.object(meta, "insert", side_effect=SupabaseError("503 Service Unavailable")),
            patch.object(meta.logger, "warning") as warned,
        ):
            meta.save_ad_referrals(_payload(_mensaje_de_aviso()))  # no levanta
        warned.assert_called_once()


class OrdenDelRelay(unittest.TestCase):
    def test_guarda_antes_de_reenviar_a_chatwoot(self) -> None:
        """Chatwoot nos rebota su webhook apenas recibe el mensaje y el worker sale a buscar el
        referral. Guardarlo después de reenviar pierde la carrera y la charla queda sin atribuir."""
        orden: list[str] = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with (
            patch.object(meta.settings, "meta_app_secret", "secreto"),
            patch.object(meta.settings, "meta_webhook_forward_url", "https://chatwoot.test/hook"),
            patch.object(meta, "insert", side_effect=lambda *a, **k: orden.append("guardar")),
            patch.object(meta.urllib.request, "urlopen", side_effect=lambda *a, **k: orden.append("reenviar") or _Resp()),
        ):
            meta.relay_webhook(json.dumps(_payload(_mensaje_de_aviso())).encode())

        self.assertEqual(orden, ["guardar", "reenviar"])


class VincularConversacion(unittest.TestCase):
    def test_ata_el_referral_por_telefono(self) -> None:
        payload = {"sender": {"phone_number": "+54 9 381 650-6312"}}
        with patch.object(chat_memory.supabase, "rpc", return_value={"source_id": "120210000000000000"}) as rpc:
            chatwoot_service.link_ad_referral(5, payload)
        self.assertEqual(rpc.call_args.args[0], "chat_link_ad_referral")
        # El teléfono se normaliza a dígitos: Chatwoot lo manda con +, espacios y guiones;
        # Meta lo manda pelado. Sin esto no cruzan nunca.
        self.assertEqual(rpc.call_args.args[1]["p_phone"], "5493816506312")
        self.assertEqual(rpc.call_args.args[1]["p_conversation_id"], 5)

    def test_sin_telefono_no_llama_a_supabase(self) -> None:
        with patch.object(chat_memory.supabase, "rpc") as rpc:
            chatwoot_service.link_ad_referral(5, {})
        rpc.assert_not_called()

    def test_charla_organica_no_rompe_el_turno(self) -> None:
        with patch.object(chat_memory.supabase, "rpc", return_value=None):
            chatwoot_service.link_ad_referral(5, {"sender": {"phone_number": "+5493816506312"}})

    def test_supabase_caido_no_rompe_el_turno(self) -> None:
        with patch.object(chat_memory.supabase, "rpc", side_effect=SupabaseError("boom")):
            chatwoot_service.link_ad_referral(5, {"sender": {"phone_number": "+5493816506312"}})


class VinculoEnElTurno(unittest.TestCase):
    def test_el_worker_intenta_atar_en_cada_turno(self) -> None:
        conversation = Conversation(5, "chatwoot", "8", "6")
        pending = [{"id": 10, "content": "hola", "raw_payload": {"sender": {"phone_number": "+5493816506312"}}}]
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
            patch.object(chatwoot_service, "run_agent_reply", return_value=chatwoot_service.AgentReply("hola!", [])),
            patch.object(chatwoot_service, "link_ad_referral") as link,
        ):
            chatwoot_service.process_pending_conversation_messages(5)
        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], 5)


if __name__ == "__main__":
    unittest.main()
