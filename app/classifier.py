from __future__ import annotations

import logging
from typing import Literal, get_args

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .models import AgentMessage

logger = logging.getLogger(__name__)

Label = Literal["compra", "asesoramiento", "sin_stock", "sin_etiqueta"]
# Etiquetas que maneja el clasificador (las que pisa/actualiza). El resto —ej. apagar_bot,
# que la pone un humano— NO se tocan.
CLASSIFIER_LABELS: set[str] = set(get_args(Label))

CLASSIFIER_PROMPT = """\
Sos un clasificador de conversaciones comerciales de una ferretería. Analizás la conversación
COMPLETA del cliente con el asistente y asignás UNA SOLA etiqueta. No expliques nada.

Categorías (devolvé exactamente una):

- compra: intención clara y directa de comprar / avanzar a la compra ("¿cómo lo pago?",
  "quiero comprarlo", "pasame el link", "me lo llevo").
- asesoramiento: pide precio mayorista, reventa, descuento/rebaja, cotización por volumen,
  o necesita que lo atienda un humano / el bot no pudo resolver ("quiero revender",
  "precio por cantidad", "está muy caro", "necesito un asesor", "quiero una cotización").
- sin_stock: el cliente pide un producto que no está disponible / agotado, o el asistente
  ya le dijo que está sin stock ("¿cuándo vuelve?", "ese está agotado").
- sin_etiqueta: no está decidido, solo consulta precios sin intención clara, consultas
  generales, está explorando ("¿cuánto sale?", "estoy mirando", "gracias, después veo").

Reglas de prioridad:
1. Si NO hay intención clara de compra → sin_etiqueta.
2. Si pide mayorista/reventa/descuento/cotización o pide humano → asesoramiento.
3. Si el producto no tiene stock → sin_stock.
4. compra SOLO con intención clara y directa.
"""


def classify(history: list[AgentMessage]) -> str:
    """Devuelve una de las 4 etiquetas para la conversación. Ante cualquier falla → sin_etiqueta."""
    if not settings.openai_api_key or not history:
        return "sin_etiqueta"
    model = OpenAIChatModel(settings.classifier_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    agent: Agent[None, Label] = Agent(model=model, output_type=Label, system_prompt=CLASSIFIER_PROMPT)
    convo = "\n".join(f"{m.role}: {m.content}" for m in history)
    try:
        return str(agent.run_sync(f"Conversación:\n{convo}").output)
    except Exception as exc:
        logger.warning("classify_failed", extra={"error": str(exc)})
        return "sin_etiqueta"


if __name__ == "__main__":
    from .models import AgentMessage as M

    cases = {
        "compra": [M(role="user", content="quiero comprar el taladro, ¿cómo lo pago?")],
        "asesoramiento": [M(role="user", content="necesito precio por 10 taladros, soy revendedor")],
        "sin_stock": [
            M(role="user", content="tenés el juego de 129 piezas?"),
            M(role="assistant", content="Lo tenemos pero está sin stock por ahora."),
        ],
        "sin_etiqueta": [M(role="user", content="hola, ¿cuánto sale una amoladora?")],
    }
    if not settings.openai_api_key:
        print("self-check: SALTEADO (falta OPENAI_API_KEY)")
    else:
        ok = 0
        for expected, hist in cases.items():
            got = classify(hist)
            mark = "✓" if got == expected else "✗"
            if got == expected:
                ok += 1
            print(f"  {mark} esperado={expected:14} got={got}")
        print(f"clasificador: {ok}/{len(cases)}")
