from __future__ import annotations

import unittest

from app.models import AgentMessage
from app.scope import ScopeDecision, contextual_scope, decide_scope


class ScopeContextual(unittest.TestCase):
    def test_precio_contextual_no_pide_confirmar_rubro(self) -> None:
        history = [
            AgentMessage(
                role="assistant",
                content=(
                    "FP MAQUINARIAS · APILADOR HIDRAULICO 2TN X 2MTS\n"
                    "Precio: $1.616.455\n"
                    "🔗 https://www.ferreproindustrial.com/productos/apilador-hidraulico/"
                ),
            )
        ]

        self.assertEqual(contextual_scope("la de 1.600.000", history), ScopeDecision("general"))

    def test_fuera_de_rubro_gana_aunque_haya_historial_de_producto(self) -> None:
        history = [AgentMessage(role="assistant", content="Mirá estas apiladoras.")]

        decision = decide_scope("Motorola e14", history)

        self.assertEqual(decision.status, "out_of_scope")
        self.assertEqual(decision.product, "celulares")


if __name__ == "__main__":
    unittest.main()
