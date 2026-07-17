from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ScopeStatus = Literal["in_scope", "out_of_scope", "ambiguous", "general"]

OUT_OF_SCOPE_REPLY = (
    "En FerrePro trabajamos productos de ferretería e industriales, no vendemos {producto}.\n"
    "¿Te ayudo con herramientas, electricidad, plomería o pinturas?"
)
AMBIGUOUS_SCOPE_REPLY = "¿Me confirmás qué producto de ferretería o industrial estás buscando?"

IN_SCOPE_RE = re.compile(
    r"\b("
    r"taladros?|amoladoras?|herramientas?|maquinas?|pinturas?|plomeria|canos?|adhesivos?|"
    r"pegamentos?|electricidad|iluminacion|cables?|enchufes?|llaves?|tornillos?|bulones?|"
    r"zorras?|transpaletas?|apiladoras?|escaleras?|seguridad|guantes?|cascos?"
    r")\b"
)
OBVIOUS_OUT_OF_SCOPE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(celulares?|celus?|smartphones?|iphone|motorola|xiaomi|galaxy|samsung\s+(?:galaxy|[asje]\d{1,3}))\b"),
        "celulares",
    ),
    (re.compile(r"\b(notebooks?|laptops?|computadoras?|pc gamer|tablets?|ipads?)\b"), "computación"),
    (re.compile(r"\b(heladeras?|lavarropas|microondas|televisores?|smart\s*tv)\b"), "electrodomésticos"),
    (re.compile(r"\b(pizzas?|hamburguesas?|empanadas?|gaseosas?|cervezas?|comida)\b"), "comida"),
)

SCOPE_PROMPT = """\
Sos una compuerta de alcance comercial para FerrePro, una ferretería industrial de Tucumán.
Tu tarea es decidir si el MENSAJE ACTUAL del cliente puede ser atendido por el agente.

Rubro permitido:
- herramientas manuales, eléctricas y a batería
- máquinas, zorras/transpaletas, apiladoras y equipamiento industrial liviano
- electricidad, iluminación, cables, pilas y materiales relacionados
- pinturas, adhesivos, plomería, seguridad/EPP y ferretería general
- consultas institucionales de FerrePro: horarios, sucursales, pagos, envíos, factura, marcas
- servicios que FerrePro puede negar desde el prompt, como copias de llave o reparación de herramientas

Fuera de rubro:
- celulares, smartphones, computación, ropa de moda, comida, motos/autos, repuestos automotor,
  electrodomésticos y cualquier producto claramente ajeno a ferretería/industrial.

Estados:
- in_scope: producto, servicio o consulta claramente del rubro permitido.
- out_of_scope: producto claramente fuera del rubro. Informá el producto/categoría en "product".
- ambiguous: parece pedir un producto, pero no alcanza para saber si es ferretería o no.
- general: saludo, seguimiento, aceptación, consulta vaga o mensaje que depende del historial.

Reglas:
- Usá el historial solo para resolver referencias del mensaje actual.
- Si el mensaje mezcla algo fuera de rubro con algo de ferretería, priorizá in_scope.
- Si el cliente pregunta por un servicio de FerrePro que no se ofrece, no es out_of_scope: es in_scope.
- Ante duda real, devolvé ambiguous. No inventes.
"""


@dataclass(frozen=True)
class ScopeDecision:
    status: ScopeStatus
    product: str | None = None


def decide_scope(message: str, history: list[Any] | None = None) -> ScopeDecision:
    fast = deterministic_scope(message)
    if fast is not None:
        return fast
    return llm_scope(message, history or [])


def deterministic_scope(message: str) -> ScopeDecision | None:
    text = _plain(message)
    if IN_SCOPE_RE.search(text):
        return ScopeDecision("in_scope")
    for pattern, product in OBVIOUS_OUT_OF_SCOPE:
        if pattern.search(text):
            return ScopeDecision("out_of_scope", product)
    return None


def llm_scope(message: str, history: list[Any]) -> ScopeDecision:
    try:
        from pydantic import BaseModel, Field
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        from .config import settings
    except Exception as exc:
        logger.warning("scope_import_failed", extra={"error": str(exc)})
        return ScopeDecision("in_scope")

    if not settings.openai_api_key:
        return ScopeDecision("in_scope")

    class ScopeOutput(BaseModel):
        status: ScopeStatus
        product: str | None = Field(default=None)

    model = OpenAIChatModel(settings.classifier_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    agent: Agent[None, ScopeOutput] = Agent(model=model, output_type=ScopeOutput, system_prompt=SCOPE_PROMPT)
    convo = _scope_input(message, history)
    try:
        out = agent.run_sync(convo).output
    except Exception as exc:
        logger.warning("scope_classify_failed", extra={"error": str(exc)})
        return ScopeDecision("in_scope")
    return ScopeDecision(out.status, _clean_product(out.product))


def scope_reply(decision: ScopeDecision) -> str | None:
    if decision.status == "out_of_scope":
        return OUT_OF_SCOPE_REPLY.format(producto=decision.product or "ese producto")
    if decision.status == "ambiguous":
        return AMBIGUOUS_SCOPE_REPLY
    return None


def _scope_input(message: str, history: list[Any]) -> str:
    if not history:
        return f"Mensaje actual del cliente: {message}"
    lines = ["Historial reciente:"]
    for item in history[-6:]:
        lines.append(f"{getattr(item, 'role', 'user')}: {getattr(item, 'content', '')}")
    lines += ["", f"Mensaje actual del cliente: {message}"]
    return "\n".join(lines)


def _plain(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _clean_product(product: str | None) -> str | None:
    product = " ".join((product or "").strip().split())
    return product[:80] or None


if __name__ == "__main__":
    assert decide_scope("Y los celulares cuánto está").status == "out_of_scope"
    assert decide_scope("Motorola e14").status == "out_of_scope"
    assert decide_scope("Samsung a15").status == "out_of_scope"
    assert decide_scope("tenés taladros?").status == "in_scope"
    assert "no vendemos celulares" in (scope_reply(ScopeDecision("out_of_scope", "celulares")) or "")
    assert scope_reply(ScopeDecision("ambiguous")) == AMBIGUOUS_SCOPE_REPLY
    print("scope puro: OK")
