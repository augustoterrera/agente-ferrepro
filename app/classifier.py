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
# [FERREPRO] Flag = Literal["sin_stock", "mayorista", "envio", "negociacion", "reclamo", "fuera_rubro"]
# Asesor solar: hogar/empresa/campo es el segmento del PASO 1, para que el equipo llegue a la
# reunión sabiendo qué simular. El resto conserva los nombres del agente de productos: el retargeting veta por
# reclamo y fuera_rubro, y las etiquetas ya existen en Chatwoot.
Flag = Literal["hogar", "empresa", "campo", "negociacion", "reclamo", "fuera_rubro"]

STAGE_LABELS: set[str] = set(get_args(Stage))
FLAG_LABELS: set[str] = set(get_args(Flag))

DEFAULT_STAGE: Stage = "curioso"


class Classification(BaseModel):
    stage: Stage
    flags: list[Flag] = Field(default_factory=list)


CLASSIFIER_PROMPT_SOLAR = """\
Sos un clasificador de conversaciones comerciales de FerrePro, empresa de energía solar. El asistente
sigue un proceso de venta consultiva: pregunta si es para una casa, una empresa o el campo, hace hasta
tres preguntas de diagnóstico y lleva al cliente a agendar una reunión por videollamada con un link de
calendario. Analizás la conversación COMPLETA y devolvés DOS cosas: la etapa del cliente en el embudo
(una sola) y las flags presentes (cero o varias). No expliques nada.

ETAPA (elegí exactamente una):
- curioso: solo saludó o hizo una consulta vaga ("info", "precio"); todavía no dijo para qué es ni
  respondió preguntas del diagnóstico.
- interesado: dijo para qué es (casa, empresa o campo) o respondió preguntas del diagnóstico, pero
  TODAVÍA no avanzó con la reunión.
- compra: avanzó con la reunión: dice que agendó el turno, eligió día u horario, o pidió que le
  reserven la reunión.

FLAGS (marcá TODAS las que apliquen; puede no haber ninguna):
- hogar: la consulta es para su casa o vivienda.
- empresa: la consulta es para una empresa, comercio, industria u oficina.
- campo: la consulta es para el campo (riego, hacienda, agua, zona rural o sin red eléctrica).
- negociacion: pide descuento, rebaja o insiste en que le den un precio por chat.
- reclamo: ya es cliente y tiene un problema, una falla o un reclamo con una instalación.
- fuera_rubro: pide un producto o servicio claramente ajeno a la energía solar.

Reglas:
- La etapa es SIEMPRE una; las flags son independientes de la etapa.
- Si la consulta es para más de un segmento (casa y campo), marcá los dos.
- No marques una flag sin evidencia clara en la conversación.
- Ante la duda de etapa, la más baja del embudo (curioso < interesado < compra).
"""

# [FERREPRO] Clasificador del agente de productos (desactivado). Va con el Flag comentado arriba.
CLASSIFIER_PROMPT_FERREPRO = """\
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

# [FERREPRO] CLASSIFIER_PROMPT = CLASSIFIER_PROMPT_FERREPRO
CLASSIFIER_PROMPT = CLASSIFIER_PROMPT_SOLAR


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
        ("curioso", (), [M(role="user", content="hola, quería info de los paneles")]),
        ("interesado", ("hogar",), [
            M(role="assistant", content="¿Es para tu casa, para una empresa o para el campo?"),
            M(role="user", content="para mi casa, se corta mucho la luz"),
        ]),
        ("compra", (), [M(role="user", content="listo, agendé la reunión para el jueves a las 17")]),
        (None, ("empresa",), [M(role="user", content="es para la fábrica, cuando se corta se para la producción")]),
        (None, ("campo",), [M(role="user", content="es para riego, tengo un pozo de 40 metros")]),
        (None, ("negociacion",), [M(role="user", content="no quiero reunión, pasame el precio y si hay descuento")]),
        (None, ("reclamo",), [M(role="user", content="ya me instalaron el equipo y el inversor no anda")]),
        (None, ("fuera_rubro",), [M(role="user", content="venden celulares?")]),
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
