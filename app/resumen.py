"""Resumen de la charla para el asesor, como nota privada en Chatwoot.

Se arma cuando el bot manda el link de agenda, cuando deriva a un asesor y cuando el cliente
reserva. Lo lee una persona antes de la reunión o al tomar el chat: tiene que ser corto, con las
palabras del cliente y sin nada inventado. Por eso el modelo llena campos fijos y el estado (link,
derivación, turno) lo pone el código, no el modelo.

Cuándo se dispara, el dedupe y la nota viven en app/tasks/chatwoot_tasks.py (resumir_para_asesor).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .models import AgentMessage

logger = logging.getLogger(__name__)

# Una charla de punta a punta (saludo, factura, diagnóstico y cierre) son unos 12 mensajes.
HISTORIAL = 40
CAMPO_MAX_CHARS = 200
NO_LO_DIJO = "no lo dijo"

ESTADOS = {
    "link": "recibió el link de agenda",
    "derivacion": "el bot lo derivó a un asesor",
    "reserva": "reservó la reunión",
}


class ResumenError(RuntimeError):
    """Falla técnica (timeout, rate limit, API caída): la task reintenta."""


class Resumen(BaseModel):
    para: str = Field(description="Casa, empresa o campo (o más de uno)")
    por_que: str = Field(description="Por qué busca energía solar: los cortes, la factura, no tiene red...")
    no_puede_cortarse: str = Field(description="Qué no puede quedarse sin luz o qué se para con un corte")
    lugar: str = Field(
        description="Datos del lugar que dio: si es propio, techo, pozo, red eléctrica, horario de trabajo, "
        "cuánto paga de luz"
    )
    factura: str = Field(
        description="Si mandó la foto de la factura, si dijo que no la tiene o la manda después, o que no se habló"
    )
    dudas: str = Field(description="Preguntas u objeciones del cliente: precio, financiación, garantías...")


TAREA = f"""\
Resumís una charla de WhatsApp entre Matías (el asistente virtual de FerrePro, energía solar) y un
cliente, para que el asesor que lo va a atender tenga el contexto en diez segundos.

Reglas:
- Solo lo que el cliente dijo o mandó. No supongas, no completes y no opines.
- Si un dato no aparece en la charla, poné exactamente "{NO_LO_DIJO}".
- Usá las palabras del cliente, en frases cortas (menos de 15 palabras por campo).
- Los mensajes de Matías son contexto para entender a qué respondió el cliente, no son datos del
  cliente.
- "El cliente envió una o más imágenes" marca que mandó una foto. Si Matías le había pedido la
  factura o le agradeció por la factura, contá que la mandó. No describas montos ni consumos.
"""


def build_resumidor() -> Agent[None, Resumen]:
    if not settings.openai_api_key:
        raise RuntimeError("Falta OPENAI_API_KEY para resumir la conversación.")
    # Mismo modelo liviano que el redactor del retargeting: es una tarea de extracción corta.
    model = OpenAIChatModel(settings.retargeting_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    model_settings = None
    effort = settings.retargeting_reasoning_effort
    if effort and (settings.retargeting_model.startswith("gpt-5") or settings.retargeting_model.startswith("o")):
        model_settings = OpenAIChatModelSettings(openai_reasoning_effort=effort)
    return Agent(model=model, output_type=Resumen, system_prompt=TAREA, model_settings=model_settings, retries=2)


def resumir(history: list[AgentMessage], *, nombre: str | None = None, flags: list[str] | None = None) -> Resumen:
    lines = ["Conversación:"]
    lines += [f"{'cliente' if m.role == 'user' else 'Matías'}: {m.content}" for m in history]
    lines.append("")
    lines.append(f"Nombre del cliente: {nombre or 'no lo sabemos'}")
    lines.append(f"Etiquetas del clasificador: {', '.join(flags) if flags else 'ninguna'}")
    try:
        return build_resumidor().run_sync("\n".join(lines)).output
    except Exception as exc:
        logger.warning("resumen_failed", extra={"error": str(exc)})
        raise ResumenError(str(exc)) from exc


def nota(resumen: Resumen, *, motivo: str, turno: str | None = None) -> str:
    """Texto de la nota privada. `turno`: el turno vigente, si ya reservó."""
    estado = ESTADOS.get(motivo, motivo)
    if turno:
        estado = f"reservó para el {turno}" if motivo == "reserva" else f"{estado} · tiene turno el {turno}"
    campos = (
        ("Para", resumen.para),
        ("Por qué busca", resumen.por_que),
        ("Qué no puede cortarse", resumen.no_puede_cortarse),
        ("Lugar", resumen.lugar),
        ("Factura", resumen.factura),
        ("Dudas u objeciones", resumen.dudas),
    )
    lineas = ["📝 Resumen para el asesor (lo armó el bot)"]
    lineas += [f"• {titulo}: {_corto(valor)}" for titulo, valor in campos]
    lineas.append(f"• Estado: {estado}")
    return "\n".join(lineas)


def _corto(valor: str) -> str:
    valor = " ".join(str(valor or "").split()) or NO_LO_DIJO
    return valor if len(valor) <= CAMPO_MAX_CHARS else valor[: CAMPO_MAX_CHARS - 1].rstrip() + "…"
