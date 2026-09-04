from __future__ import annotations

import hashlib
import hmac
import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from app import chat_memory, chatwoot_service, meta, retargeting
from app.chat_memory import Conversation
from app.agent import (
    _answer_product_ids,
    _filter_requested_product_type,
    _requested_product_limit,
    _search_requested_product_types,
)
from app.chatwoot import ChatwootClient, chatwoot_contact_phone
from app.models import Product
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
            payload["payload"][1].update(
                {
                    "content": (
                        "¿Seguís interesado en el taladro?\n\n---\n"
                        "Muestra de lo enviado al cliente mediante el catálogo de WhatsApp."
                    ),
                    "private": True,
                }
            )
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

    def test_reset_autorizado_borra_historial_sin_llamar_al_agente(self) -> None:
        conversation = Conversation(5, "chatwoot", "8", "6")
        pending = [
            {
                "id": 10,
                "content": "/reset",
                "raw_payload": {"sender": {"phone_number": "+54 9 381 650-6312"}},
            }
        ]
        with (
            patch.object(chatwoot_service.settings, "chat_reset_phone", "5493816506312"),
            patch.object(chatwoot_service.settings, "chat_reset_conversation_id", "8"),
            patch.object(chatwoot_service.chat_memory, "get_conversation", return_value=conversation),
            patch.object(chatwoot_service.chat_memory, "pending_messages", return_value=pending),
            patch.object(chatwoot_service.chat_memory, "recent_history", return_value=[]),
            patch.object(chatwoot_service.chat_memory, "clear_conversation_history") as cleared,
            patch.object(chatwoot_service.chat_memory, "create_outbox", return_value={"id": 99}) as created,
            patch.object(chatwoot_service.chat_memory, "hide_messages_from_history") as hidden,
            patch.object(chatwoot_service.chat_memory, "update_jobs"),
            patch.object(chatwoot_service.chat_memory, "update_events"),
            patch.object(chatwoot_service, "sync_crm_contact"),
            patch.object(chatwoot_service, "run_agent_reply") as agent,
        ):
            result = chatwoot_service.process_pending_conversation_messages(5)

        self.assertEqual(result, (99, False))
        cleared.assert_called_once_with(5)
        hidden.assert_called_once_with([10])
        self.assertEqual(created.call_args.kwargs["raw_payload"], {"memory_reset": True})
        agent.assert_not_called()

    def test_reset_requiere_telefono_y_conversacion_correctos(self) -> None:
        with (
            patch.object(chatwoot_service.settings, "chat_reset_phone", "5493816506312"),
            patch.object(chatwoot_service.settings, "chat_reset_conversation_id", "8"),
        ):
            self.assertFalse(
                chatwoot_service._reset_authorized(
                    Conversation(5, "chatwoot", "9", "6"),
                    {"sender": {"phone_number": "+54 9 381 650-6312"}},
                )
            )
            self.assertFalse(
                chatwoot_service._reset_authorized(
                    Conversation(5, "chatwoot", "8", "6"),
                    {"sender": {"phone_number": "+54 9 381 000-0000"}},
                )
            )

    def test_extrae_ids_de_productos_mostrados(self) -> None:
        self.assertEqual(
            _answer_product_ids(
                "Mirá\n🔗 https://www.ferreproindustrial.com/productos/taladro/",
                {"https://www.ferreproindustrial.com/productos/taladro": 350971067},
            ),
            [350971067],
        )

    def test_zorra_no_devuelve_apiladores(self) -> None:
        products = [
            Product(id=1, name="ZORRA HIDRAULICA 3TN"),
            Product(id=2, name="APILADOR HIDRAULICO 3TN X 1.6MTS"),
        ]
        self.assertEqual(
            [p.id for p in _filter_requested_product_type(products, "quiero la zorra hidraulica de 500.000")],
            [1],
        )

    def test_apiladora_encuentra_productos_nombrados_apilador(self) -> None:
        products = [
            Product(id=1, name="ZORRA HIDRAULICA 3TN"),
            Product(id=2, name="APILADOR HIDRAULICO 3TN X 1.6MTS"),
        ]
        self.assertEqual(
            [p.id for p in _filter_requested_product_type(products, "perdón, me refiero a las apiladoras")],
            [2],
        )

    def test_todas_busca_cada_tipo_y_permite_hasta_diez(self) -> None:
        products = [Product(id=1, name="ZORRA HIDRAULICA 3TN")]
        products += [Product(id=index, name=f"APILADOR {index}") for index in range(2, 7)]

        def search(query: str, **_kwargs):
            return products[:1] if "zorra" in query else products[1:]

        request = "mostrame la zorra y todas las apiladoras"
        with patch("app.agent._buscar", side_effect=search):
            result = _search_requested_product_types(
                request,
                request,
                limite=_requested_product_limit(request),
                solo_con_stock=True,
            )

        self.assertEqual(_requested_product_limit(request), 10)
        self.assertEqual([product.id for product in result], [1, 2, 3, 4, 5, 6])

    def test_telefono_chatwoot_sirve_para_meta(self) -> None:
        self.assertEqual(
            chatwoot_contact_phone({"sender": {"phone_number": "+54 9 381 555-1234"}}),
            "5493815551234",
        )

    def test_envio_catalogo_usa_content_id_de_variante(self) -> None:
        outbox = {
            "id": 1,
            "content": (
                "Taladro Percutor 700W 13Mm Braber\n"
                "Precio: $55.152\n"
                "🔗 https://www.ferreproindustrial.com/productos/taladro-percutor-700w-13mm-braber-tp1370-1tkdd\n\n"
                "Podés comprarlo desde ahí. Si preferís pasar por sucursal y pagar en efectivo, tenés 10% de descuento.\n\n"
                "¿Querés que te derive con un vendedor para ayudarte con la compra o el envío?"
            ),
            "raw_payload": {"customer_phone": "5493815551234", "meta_product_product_ids": [350971067]},
        }
        sent: list[dict] = []

        def _send(payload):
            sent.append(payload)
            return {"messages": [{"id": "wamid.test"}]}

        with (
            patch.object(tasks.settings, "meta_access_token", "token"),
            patch.object(tasks.settings, "meta_phone_number_id", "phone-id"),
            patch.object(tasks.settings, "meta_catalog_id", "catalog-id"),
            patch.object(tasks, "product_retailer_ids_by_product", return_value={350971067: "1544613986"}),
            patch.object(tasks, "available_catalog_retailer_ids", return_value={"1544613986"}),
            patch.object(tasks, "send_whatsapp", side_effect=_send),
        ):
            plan = tasks._meta_product_plan(outbox)
            response = tasks._send_meta_product_plan(plan or {})

        self.assertEqual(response, {"messages": [{"id": "wamid.test"}]})
        self.assertEqual(sent[0]["interactive"]["action"]["product_retailer_id"], "1544613986")
        self.assertEqual(sent[0]["interactive"]["body"]["text"], "Te dejo el producto para verlo en WhatsApp")
        self.assertIn("10% de descuento", plan["remaining_text"])
        self.assertIn("¿Querés que te derive", plan["remaining_text"])

    def test_followup_de_catalogo_saca_bloque_repetido_y_conserva_cierre(self) -> None:
        content = (
            "Taladro Percutor 700W 13Mm Braber\n"
            "Precio: $55.152\n"
            "🔗 https://www.ferreproindustrial.com/productos/taladro-percutor-700w-13mm-braber-tp1370-1tkdd\n\n"
            "Podés comprarlo desde ahí. Si preferís pasar por sucursal y pagar en efectivo, tenés 10% de descuento.\n\n"
            "¿Querés que te derive con un vendedor para ayudarte con la compra o el envío?"
        )

        body = tasks._catalog_followup_text(content, 1)

        self.assertNotIn("Taladro Percutor", body)
        self.assertNotIn("https://", body)
        self.assertIn("10% de descuento", body)
        self.assertIn("ayudarte con la compra o el envío", body)

    def test_followup_de_catalogo_no_duplica_cierre_si_hay_productos_por_texto(self) -> None:
        remaining = (
            "También tenemos estas opciones 👇\n\n"
            "Producto sin catálogo\n"
            "Precio: $10\n"
            "🔗 https://www.ferreproindustrial.com/productos/producto-sin-catalogo\n\n"
            "¿Querés que te derive con un vendedor para ayudarte con la compra o el envío?"
        )
        followup = "¿Querés que te derive con un vendedor para ayudarte con la compra o el envío?"

        body = tasks._join_public_catalog_text(remaining, followup)

        self.assertEqual(body, remaining)

    def test_catalogo_exitoso_manda_card_y_despues_mensaje_vendedor(self) -> None:
        outbox = {
            "id": 1, "conversation_id": 5, "external_conversation_id": "8", "status": "pending",
            "content": "Producto con precio y link", "attempts": 0, "idempotency_key": "chatwoot:8:x",
            "created_at": "2026-08-04T09:00:00+00:00",
        }
        posts: list[tuple[str, bool]] = []

        class _Client:
            def create_outgoing_message(self, _a, _c, content, *, private=False):
                posts.append((content, private))
                return {"id": len(posts), "private": private}

        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=outbox),
            patch.object(tasks.chat_memory, "mark_outbox_processing", return_value=True),
            patch.object(tasks.chat_memory, "get_conversation", return_value=Conversation(5, "chatwoot", "8", "6")),
            patch.object(tasks.chat_memory, "mark_outbox_sent"),
            patch.object(tasks, "build_chatwoot_client", return_value=_Client()),
            patch.object(
                tasks,
                "_meta_product_plan",
                return_value={
                    "payload": {},
                    "catalog_product_ids": [352305267],
                    "catalog_text": "Producto con precio y link",
                    "remaining_text": "¿Querés que te derive con un vendedor de FerrePro?",
                },
            ),
            patch.object(tasks, "_send_meta_product_plan", return_value={"messages": [{"id": "wamid.test"}]}),
            patch.object(tasks, "_handoff_if_needed", return_value=False),
        ):
            tasks.send_chatwoot_outbound_message("1")

        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0][1])
        self.assertIn("Muestra de lo enviado", posts[0][0])
        self.assertEqual(posts[1], ("¿Querés que te derive con un vendedor de FerrePro?", False))

    def test_catalogo_solo_usa_productos_aprobados_para_whatsapp(self) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "data": [
                        {
                            "retailer_id": "visible",
                            "visibility": "published",
                            "availability": "in stock",
                            "capability_to_review_status": [{"key": "WHATSAPP", "value": "APPROVED"}],
                        },
                        {
                            "retailer_id": "oculto",
                            "visibility": "published",
                            "availability": "in stock",
                            "capability_to_review_status": [{"key": "WHATSAPP", "value": "NO_REVIEW"}],
                        },
                    ]
                }
            ).encode()
        )
        with (
            patch.object(meta.settings, "meta_access_token", "token"),
            patch.object(meta.settings, "meta_catalog_id", "catalog-id"),
            patch.object(meta.urllib.request, "urlopen", return_value=response),
        ):
            self.assertEqual(meta.available_catalog_retailer_ids(["visible", "oculto"]), {"visible"})

    def test_pedido_del_catalogo_se_convierte_en_texto_para_chatwoot(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.order",
                                        "type": "order",
                                        "order": {
                                            "product_items": [
                                                {"product_retailer_id": "101", "quantity": 1},
                                                {"product_retailer_id": "202", "quantity": 2},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        with patch(
            "app.meta.select",
            side_effect=[
                [{"id": 101, "product_id": 1}, {"id": 202, "product_id": 2}],
                [
                    {
                        "id": 1,
                        "name": "Zorra hidráulica",
                        "canonical_url": "https://www.ferreproindustrial.com/productos/zorra/",
                    },
                    {
                        "id": 2,
                        "name": "Apilador",
                        "canonical_url": "https://www.ferreproindustrial.com/productos/apilador/",
                    },
                ],
            ],
        ):
            transformed, count = meta.transform_order_messages(payload)

        message = transformed["entry"][0]["changes"][0]["value"]["messages"][0]
        self.assertEqual(count, 1)
        self.assertEqual(message["type"], "text")
        self.assertNotIn("order", message)
        self.assertIn("1 x Zorra hidráulica", message["text"]["body"])
        self.assertIn("2 x Apilador", message["text"]["body"])
        self.assertIn("https://www.ferreproindustrial.com/productos/zorra/", message["text"]["body"])
        self.assertIn("https://www.ferreproindustrial.com/productos/apilador/", message["text"]["body"])

        raw = b'{"test":true}'
        with patch.object(meta.settings, "meta_app_secret", "secret"):
            signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
            self.assertTrue(meta.verify_webhook_signature(raw, signature))
            self.assertFalse(meta.verify_webhook_signature(raw, "sha256=incorrecta"))

    def test_catalogo_exitoso_solo_deja_nota_privada_en_chatwoot(self) -> None:
        outbox = {
            "id": 1, "conversation_id": 5, "external_conversation_id": "8", "status": "pending",
            "content": "Zorra con precio y link", "attempts": 0, "idempotency_key": "chatwoot:8:x",
            "created_at": "2026-08-04T09:00:00+00:00",
        }
        posts: list[tuple[str, bool]] = []

        class _Client:
            def create_outgoing_message(self, _a, _c, content, *, private=False):
                posts.append((content, private))
                return {"id": 10, "private": private}

        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=outbox),
            patch.object(tasks.chat_memory, "mark_outbox_processing", return_value=True),
            patch.object(tasks.chat_memory, "get_conversation", return_value=Conversation(5, "chatwoot", "8", "6")),
            patch.object(tasks.chat_memory, "mark_outbox_sent") as marked,
            patch.object(tasks, "build_chatwoot_client", return_value=_Client()),
            patch.object(
                tasks,
                "_meta_product_plan",
                return_value={
                    "payload": {},
                    "catalog_product_ids": [352305267],
                    "catalog_text": "Zorra con precio y link",
                    "remaining_text": None,
                },
            ),
            patch.object(tasks, "_send_meta_product_plan", return_value={"messages": [{"id": "wamid.test"}]}),
            patch.object(tasks, "_handoff_if_needed", return_value=False),
        ):
            tasks.send_chatwoot_outbound_message("1")

        self.assertEqual(
            posts,
            [
                (
                    "Zorra con precio y link\n\n---\n"
                    "Muestra de lo enviado al cliente mediante el catálogo de WhatsApp.",
                    True,
                )
            ],
        )
        self.assertEqual(
            marked.call_args.args[1],
            {
                "meta": {
                    "response": {"messages": [{"id": "wamid.test"}]},
                    "product_ids": [352305267],
                },
                "chatwoot_private": {"id": 10, "private": True},
                "chatwoot_remaining": None,
            },
        )

    def test_catalogo_rechazado_conserva_texto_chatwoot(self) -> None:
        outbox = {
            "id": 1, "conversation_id": 5, "external_conversation_id": "8", "status": "pending",
            "content": "Producto con precio y link", "attempts": 0, "idempotency_key": "chatwoot:8:x",
            "created_at": "2026-08-04T09:00:00+00:00",
        }
        posts: list[str] = []

        class _Client:
            def create_outgoing_message(self, _a, _c, content):
                posts.append(content)
                return {"id": 10}

        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=outbox),
            patch.object(tasks.chat_memory, "mark_outbox_processing", return_value=True),
            patch.object(tasks.chat_memory, "get_conversation", return_value=Conversation(5, "chatwoot", "8", "6")),
            patch.object(tasks.chat_memory, "mark_outbox_sent"),
            patch.object(tasks, "build_chatwoot_client", return_value=_Client()),
            patch.object(tasks, "_meta_product_plan", return_value={"error": "no aprobado"}),
            patch.object(tasks, "_handoff_if_needed", return_value=False),
        ):
            tasks.send_chatwoot_outbound_message("1")

        self.assertEqual(posts, ["Producto con precio y link"])

    def test_envio_mixto_no_repite_productos(self) -> None:
        content = (
            "Mirá estas opciones 👇\n\n"
            "Marca · Producto A\nPrecio: $10\n🔗 https://tienda.test/a/\n\n"
            "Marca · Producto B\nPrecio: $20\n🔗 https://tienda.test/b/\n\n"
            "Marca · Producto C\nPrecio: $30\n🔗 https://tienda.test/c/\n\n"
            "¿Querés avanzar con alguno?"
        )
        catalog_text, remaining_text = tasks._split_catalog_content(
            content,
            [1, 2, 3],
            {1: "https://tienda.test/a", 2: "https://tienda.test/b", 3: "https://tienda.test/c"},
            {1, 2},
        ) or ("", "")
        outbox = {
            "id": 1, "conversation_id": 5, "external_conversation_id": "8", "status": "pending",
            "content": content, "attempts": 0, "idempotency_key": "chatwoot:8:x",
            "created_at": "2026-08-04T09:00:00+00:00",
        }
        posts: list[tuple[str, bool]] = []

        class _Client:
            def create_outgoing_message(self, _a, _c, message, *, private=False):
                posts.append((message, private))
                return {"id": len(posts), "private": private}

        with (
            patch.object(tasks.chat_memory, "get_outbox", return_value=outbox),
            patch.object(tasks.chat_memory, "mark_outbox_processing", return_value=True),
            patch.object(tasks.chat_memory, "get_conversation", return_value=Conversation(5, "chatwoot", "8", "6")),
            patch.object(tasks.chat_memory, "mark_outbox_sent"),
            patch.object(tasks, "build_chatwoot_client", return_value=_Client()),
            patch.object(
                tasks,
                "_meta_product_plan",
                return_value={
                    "payload": {}, "catalog_product_ids": [1, 2],
                    "catalog_text": catalog_text, "remaining_text": remaining_text,
                },
            ),
            patch.object(tasks, "_send_meta_product_plan", return_value={"messages": [{"id": "wamid.test"}]}),
            patch.object(tasks, "_handoff_if_needed", return_value=False),
        ):
            tasks.send_chatwoot_outbound_message("1")

        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0][1])
        self.assertFalse(posts[1][1])
        self.assertIn("/a/", posts[0][0])
        self.assertIn("/b/", posts[0][0])
        self.assertNotIn("/c/", posts[0][0])
        self.assertIn("También tenemos estas opciones", posts[1][0])
        self.assertIn("/c/", posts[1][0])
        self.assertNotIn("/a/", posts[1][0])
        self.assertNotIn("/b/", posts[1][0])


if __name__ == "__main__":
    unittest.main()
