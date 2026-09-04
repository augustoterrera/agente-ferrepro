"""La traza de tools que alimenta el BI: qué buscó el cliente y si el catálogo respondió.

Se persiste en `chat_messages.tool_calls`; si deja de escribirse, la pérdida de datos es
silenciosa (la columna default es `[]`) y no se puede reconstruir después.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from app import agent as agent_mod
from app import chatwoot_service
from app.agent import AgentReply, Deps, build_agent
from app.chat_memory import Conversation
from app.models import AgentMessage, Product
from app.scope import ScopeDecision


@dataclass
class _Ctx:
    """Stand-in de RunContext: las tools solo tocan `ctx.deps`."""

    deps: Deps


def _tool(nombre: str):
    with patch.object(agent_mod.settings, "openai_api_key", "sk-test"):
        return build_agent()._function_toolset.tools[nombre].function


def _producto(product_id: int, nombre: str) -> Product:
    return Product(
        id=product_id,
        name=nombre,
        in_stock=True,
        canonical_url=f"https://www.ferreproindustrial.com/productos/{product_id}/",
    )


class BuscarProductosTrace(unittest.TestCase):
    def test_registra_consulta_y_resultados(self) -> None:
        deps = Deps(current_message="tenés taladros?")
        productos = [_producto(1, "Taladro A"), _producto(2, "Taladro B")]
        with patch.object(agent_mod, "_search_requested_product_types", return_value=productos):
            _tool("buscar_productos")(_Ctx(deps), consulta="taladro")

        self.assertEqual(
            deps.tool_calls,
            [
                {
                    "tool": "buscar_productos",
                    "consulta": "taladro",
                    "incluir_sin_stock": False,
                    "encontrados": 2,
                    "devueltos": 2,
                    "product_ids": [1, 2],
                }
            ],
        )

    def test_catalogo_vacio_queda_en_cero(self) -> None:
        """`encontrados == 0` es la señal accionable: demanda sin producto que la cubra."""
        deps = Deps(current_message="tenés un torno CNC?")
        with patch.object(agent_mod, "_search_requested_product_types", return_value=[]):
            _tool("buscar_productos")(_Ctx(deps), consulta="torno cnc")

        self.assertEqual(deps.tool_calls[0]["encontrados"], 0)
        self.assertEqual(deps.tool_calls[0]["product_ids"], [])

    def test_distingue_sin_catalogo_de_ya_mostrado(self) -> None:
        """Si el producto existe pero se filtró por repetido, `encontrados` NO puede dar 0:
        sería un falso agujero de catálogo."""
        productos = [_producto(1, "Taladro A"), _producto(2, "Taladro B")]
        deps = Deps(
            current_message="algo más?",
            # seen_links guarda links normalizados (sin barra final), como los deja _norm_url.
            seen_links={"https://www.ferreproindustrial.com/productos/1"},
        )
        with patch.object(agent_mod, "_search_requested_product_types", return_value=productos):
            _tool("buscar_productos")(_Ctx(deps), consulta="taladro")

        traza = deps.tool_calls[0]
        self.assertEqual(traza["encontrados"], 2)
        self.assertEqual(traza["devueltos"], 1)
        self.assertEqual(traza["product_ids"], [2])


class DetalleProductoTrace(unittest.TestCase):
    def test_registra_encontrado(self) -> None:
        deps = Deps()
        with patch.object(agent_mod, "_detalle", return_value={"nombre": "Taladro"}):
            _tool("detalle_producto")(_Ctx(deps), id=7)
        self.assertEqual(deps.tool_calls, [{"tool": "detalle_producto", "product_id": 7, "encontrado": True}])

    def test_registra_no_encontrado(self) -> None:
        deps = Deps()
        with patch.object(agent_mod, "_detalle", return_value={"error": "producto no encontrado"}):
            _tool("detalle_producto")(_Ctx(deps), id=7)
        self.assertFalse(deps.tool_calls[0]["encontrado"])


class ScopeTrace(unittest.TestCase):
    def test_fuera_de_rubro_se_registra_aunque_no_haya_tools(self) -> None:
        with (
            patch.object(agent_mod, "decide_scope", return_value=ScopeDecision("out_of_scope", "celulares")),
            patch.object(agent_mod, "build_agent") as build,
        ):
            reply = agent_mod.run_agent_reply("tenés celulares?")

        build.assert_not_called()
        self.assertEqual(
            reply.tool_calls, [{"tool": "scope", "status": "out_of_scope", "producto": "celulares"}]
        )


class ProductoReferidoPorPauta(unittest.TestCase):
    def _producto(self) -> dict:
        return {
            "id": 350860225,
            "name": "Taladro Ator 12V 20Nm 1Bat Usb Emtop Ecdl12456",
            "brand": "EMTOP",
            "canonical_url": "https://www.ferreproindustrial.com/productos/taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456/",
            "price_min": 50754.3,
            "price_max": 50754.3,
            "in_stock": True,
            "category_names": ["HERRAMIENTAS A BATERIA"],
        }

    def test_ref_fp_id_inyecta_producto_exacto_en_el_prompt(self) -> None:
        link = "https://www.ferreproindustrial.com/productos/taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456"

        class _Corrida:
            output = f"EMTOP · Taladro Ator 12V 20Nm\nPrecio: $50.754\n🔗 {link}"

        agente = MagicMock()
        agente.run_sync.return_value = _Corrida()
        with (
            patch.object(agent_mod, "decide_scope", return_value=ScopeDecision("in_scope")),
            patch.object(agent_mod, "build_agent", return_value=agente),
            patch.object(agent_mod, "sb_select", return_value=[self._producto()]) as select,
        ):
            reply = agent_mod.run_agent_reply("Hola, quiero info. Ref: FP-350860225", [])

        prompt = agente.run_sync.call_args.args[0]
        select.assert_called_once()
        self.assertIn("id=eq.350860225", select.call_args.args[1])
        self.assertIn("Producto referido por pauta/publicidad", prompt)
        self.assertIn("Ref: FP-350860225", prompt)
        self.assertIn("Precio vigente: $50.754", prompt)
        self.assertEqual(reply.product_ids, [350860225])
        self.assertEqual(reply.tool_calls[0]["tool"], "product_ref")
        self.assertTrue(reply.tool_calls[0]["encontrado"])

    def test_codigo_valido_no_cae_en_scope_ambiguo(self) -> None:
        link = "https://www.ferreproindustrial.com/productos/taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456"

        class _Corrida:
            output = f"EMTOP · Taladro Ator 12V 20Nm\nPrecio: $50.754\n🔗 {link}"

        agente = MagicMock()
        agente.run_sync.return_value = _Corrida()
        with (
            patch.object(agent_mod, "decide_scope") as scope,
            patch.object(agent_mod, "build_agent", return_value=agente),
            patch.object(
                agent_mod,
                "sb_select",
                return_value=[self._producto() | {"handle": "taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456-1otsu"}],
            ),
        ):
            reply = agent_mod.run_agent_reply("Hola! Quiero info del producto del anuncio. Código: 1otsu", [])

        scope.assert_not_called()
        self.assertNotIn("confirmás qué producto", reply.text)
        self.assertEqual(reply.tool_calls[0]["tipo"], "handle_suffix")

    def test_codigo_en_historial_sirve_para_respuestas_cortas(self) -> None:
        link = "https://www.ferreproindustrial.com/productos/taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456"

        class _Corrida:
            output = f"EMTOP · Taladro Ator 12V 20Nm\nPrecio: $50.754\n🔗 {link}"

        agente = MagicMock()
        agente.run_sync.return_value = _Corrida()
        history = [AgentMessage(role="user", content="Hola! Quiero info. Código: 1otsu")]
        with (
            patch.object(agent_mod, "build_agent", return_value=agente),
            patch.object(
                agent_mod,
                "sb_select",
                return_value=[self._producto() | {"handle": "taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456-1otsu"}],
            ),
        ):
            reply = agent_mod.run_agent_reply("precio?", history)

        self.assertIn("Precio", reply.text)
        self.assertEqual(reply.tool_calls[0]["tipo"], "handle_suffix")

    def test_fuera_de_rubro_no_queda_tapado_por_codigo_en_historial(self) -> None:
        history = [AgentMessage(role="user", content="Hola! Quiero info. Código: 1otsu")]
        with (
            patch.object(agent_mod, "build_agent") as build,
            patch.object(
                agent_mod,
                "sb_select",
                return_value=[self._producto() | {"handle": "taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456-1otsu"}],
            ),
        ):
            reply = agent_mod.run_agent_reply("Motorola e14", history)

        build.assert_not_called()
        self.assertIn("no vendemos celulares", reply.text)
        self.assertEqual(reply.tool_calls[-1]["tool"], "scope")

    def test_link_de_producto_tambien_resuelve_contexto_de_pauta(self) -> None:
        deps = Deps()
        url = "https://www.ferreproindustrial.com/productos/taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456/"
        with patch.object(agent_mod, "sb_select", return_value=[self._producto()]) as select:
            context = agent_mod._product_ref_context(f"Hola, quiero info de {url}", [], deps)

        self.assertIsNotNone(context)
        self.assertIn("Ref: FP-350860225", context or "")
        self.assertIn("canonical_url=eq.", select.call_args.args[1])
        self.assertEqual(deps.product_ids_by_link[url.rstrip("/")], 350860225)

    def test_codigo_corto_del_final_del_link_resuelve_producto(self) -> None:
        producto = self._producto() | {"handle": "taladro-ator-12v-20nm-1bat-usb-emtop-ecdl12456-1otsu"}
        deps = Deps()
        with patch.object(agent_mod, "sb_select", return_value=[producto]) as select:
            context = agent_mod._product_ref_context("Hola, quiero info. Código: 1otsu", [], deps)

        self.assertIsNotNone(context)
        self.assertIn("Ref: FP-350860225", context or "")
        self.assertIn("handle=ilike.*-1otsu", select.call_args.args[1])
        self.assertEqual(deps.tool_calls[0]["tipo"], "handle_suffix")
        self.assertTrue(deps.tool_calls[0]["encontrado"])

    def test_codigo_corto_ambiguo_no_se_toma_como_referencia_exacta(self) -> None:
        deps = Deps()
        with patch.object(
            agent_mod,
            "sb_select",
            return_value=[
                self._producto() | {"handle": "producto-a-1otsu"},
                self._producto() | {"id": 99, "handle": "producto-b-1otsu"},
            ],
        ):
            context = agent_mod._product_ref_context("Código: 1otsu", [], deps)

        self.assertIsNone(context)
        self.assertFalse(deps.tool_calls[0]["encontrado"])


class PersistenciaTrace(unittest.TestCase):
    def test_el_turno_guarda_la_traza_en_el_mensaje_del_asistente(self) -> None:
        conversation = Conversation(5, "chatwoot", "8", "6")
        pending = [{"id": 10, "content": "tenés taladros?", "raw_payload": {}}]
        traza = [{"tool": "buscar_productos", "consulta": "taladro", "encontrados": 3, "devueltos": 3}]
        with (
            patch.object(chatwoot_service.chat_memory, "get_conversation", return_value=conversation),
            patch.object(chatwoot_service.chat_memory, "pending_messages", side_effect=[pending, []]),
            patch.object(chatwoot_service.chat_memory, "recent_history", return_value=[]),
            patch.object(chatwoot_service.chat_memory, "add_message") as added,
            patch.object(chatwoot_service.chat_memory, "mark_messages_processed"),
            patch.object(chatwoot_service.chat_memory, "create_outbox", return_value={"id": 99}),
            patch.object(chatwoot_service.chat_memory, "update_jobs"),
            patch.object(chatwoot_service.chat_memory, "update_events"),
            patch.object(chatwoot_service, "sync_crm_contact"),
            patch.object(
                chatwoot_service,
                "run_agent_reply",
                return_value=AgentReply("Mirá estos", [1], {}, traza),
            ),
        ):
            chatwoot_service.process_pending_conversation_messages(5)

        self.assertEqual(added.call_args.kwargs["tool_calls"], traza)


if __name__ == "__main__":
    unittest.main()
