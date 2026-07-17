from __future__ import annotations

import logging
from typing import Literal, get_args

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .models import AgentMessage

logger = logging.getLogger(__name__)

# Dos dimensiones independientes:
# - Etapa del embudo: UNA sola por conversación (excluyente). La reemplaza cada corrida.
# - Flags comerciales: CERO o varias. Son aditivas/sticky (se acumulan turno a turno).
Stage = Literal["curioso", "interesado", "compra"]
Flag = Literal["sin_stock", "mayorista", "envio", "negociacion", "reclamo", "fuera_rubro"]

STAGE_LABELS: set[str] = set(get_args(Stage))
FLAG_LABELS: set[str] = set(get_args(Flag))

DEFAULT_STAGE: Stage = "curioso"


class Classification(BaseModel):
    stage: Stage
    flags: list[Flag] = Field(default_factory=list)


CLASSIFIER_PROMPT = """\
Sos un clasificador de conversaciones comerciales de una ferretería. Analizás la conversación
COMPLETA del cliente con el asistente y devolvés DOS cosas: la etapa del cliente en el embudo
(una sola) y las flags comerciales presentes (cero o varias). No expliques nada.

ETAPA (elegí exactamente una):
- curioso: consulta general o exploración, sin pedir un producto puntual. Saluda, pregunta
  precios al voleo, "estoy mirando", "¿qué tenés?".
- interesado: pidió un producto o categoría concreta, comparó opciones o preguntó detalles de
  algo mostrado, pero TODAVÍA no confirmó la compra.
- compra: intención clara de avanzar a la compra: "¿cómo lo pago?", "me lo llevo", "pasame el
  link para comprar", "quiero coordinar la compra".

FLAGS (marcá TODAS las que apliquen; puede no haber ninguna):
- sin_stock: pidió un producto agotado, no disponible o que no está en el catálogo.
- mayorista: pide precio por cantidad, reventa, presupuesto formal o compra por volumen.
- envio: pide, pregunta o necesita envío a domicilio.
- negociacion: pide descuento, rebaja o regatea el precio.
- reclamo: producto fallado, cambio o devolución.
- fuera_rubro: pide un producto o servicio claramente ajeno a FerrePro/ferretería industrial,
  por ejemplo celulares, computadoras, ropa de moda, comida, motos/autos o electrodomésticos.

Reglas:
- La etapa es SIEMPRE una; las flags son independientes de la etapa (alguien en "compra" puede
  tener "envio").
- No marques una flag sin evidencia clara en la conversación.
- Ante la duda de etapa, la más baja del embudo (curioso < interesado < compra).
"""


def classify(history: list[AgentMessage]) -> Classification:
    """Devuelve etapa + flags de la conversación. Ante cualquier falla → etapa por defecto, sin flags."""
    if not settings.openai_api_key or not history:
        return Classification(stage=DEFAULT_STAGE)
    model = OpenAIChatModel(settings.classifier_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    agent: Agent[None, Classification] = Agent(model=model, output_type=Classification, system_prompt=CLASSIFIER_PROMPT)
    convo = "\n".join(f"{m.role}: {m.content}" for m in history)
    try:
        return agent.run_sync(f"Conversación:\n{convo}").output
    except Exception as exc:
        logger.warning("classify_failed", extra={"error": str(exc)})
        return Classification(stage=DEFAULT_STAGE)


if __name__ == "__main__":
    from .models import AgentMessage as M

    # (etapa esperada o None para no chequearla, flags que deben estar, historia)
    cases: list[tuple[str | None, tuple[str, ...], list[M]]] = [
        ("curioso", (), [M(role="user", content="hola, estoy mirando precios")]),
        ("interesado", (), [M(role="user", content="busco un taladro percutor, ¿qué opciones tenés?")]),
        ("compra", (), [M(role="user", content="me lo llevo, ¿cómo lo pago?")]),
        (None, ("mayorista",), [M(role="user", content="necesito precio por 10 taladros, soy revendedor")]),
        (None, ("sin_stock",), [
            M(role="user", content="tenés el juego de 129 piezas?"),
            M(role="assistant", content="Lo tenemos pero está sin stock por ahora."),
        ]),
        (None, ("envio",), [M(role="user", content="quiero una amoladora, ¿hacen envíos a Salta?")]),
        (None, ("reclamo",), [M(role="user", content="el taladro que compré vino fallado, quiero cambiarlo")]),
        (None, ("fuera_rubro",), [M(role="user", content="Y los celulares cuánto está")]),
    ]
    if not settings.openai_api_key:
        print("self-check: SALTEADO (falta OPENAI_API_KEY)")
    else:
        ok = 0
        for expected_stage, expected_flags, hist in cases:
            got = classify(hist)
            stage_ok = expected_stage is None or got.stage == expected_stage
            flags_ok = set(expected_flags) <= set(got.flags)
            if stage_ok and flags_ok:
                ok += 1
            mark = "✓" if stage_ok and flags_ok else "✗"
            print(f"  {mark} esperado=({expected_stage},{expected_flags}) got=({got.stage},{got.flags})")
        print(f"clasificador: {ok}/{len(cases)}")
