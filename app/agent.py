from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .models import AgentMessage
from .search import buscar_productos as _buscar, compact_for_llm
from .supabase import select as sb_select

PROMPT_FILE = Path(__file__).parent / "prompts" / "ferrepro.md"


def load_system_prompt() -> str:
    # Se relee por turno: editás ferrepro.md y el bot cambia sin reiniciar. El costo de
    # leer un archivo chico por request es nulo (lo cachea el SO) y habilita iterar en vivo.
    return PROMPT_FILE.read_text(encoding="utf-8")

# Links que el bot puede emitir aunque no vengan de una búsqueda.
FIXED_LINKS = {
    "https://maps.app.goo.gl/ryspgRto3yHArQYp7",
    "https://www.ferreproindustrial.com/productos/",
}


class AgentError(RuntimeError):
    pass


@dataclass
class Deps:
    # Links de productos realmente devueltos por las tools este turno → set permitido del guard.
    shown_links: set[str] = field(default_factory=set)


def build_agent() -> Agent[Deps, str]:
    if not settings.openai_api_key:
        raise AgentError("Falta OPENAI_API_KEY para el agente.")
    model = OpenAIChatModel(settings.agent_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    # reasoning_effort solo aplica a familias de razonamiento (gpt-5*, o*); en gpt-4.1 el
    # parámetro no va, así que se omite y el cambio de modelo por env sigue funcionando.
    model_settings = None
    effort = settings.agent_reasoning_effort
    if effort and (settings.agent_model.startswith("gpt-5") or settings.agent_model.startswith("o")):
        model_settings = OpenAIChatModelSettings(openai_reasoning_effort=effort)
    agent = Agent(model=model, deps_type=Deps, system_prompt=load_system_prompt(), model_settings=model_settings)

    @agent.tool
    def buscar_productos(
        ctx: RunContext[Deps], consulta: str, limite: int = 8, incluir_sin_stock: bool = False
    ) -> list[dict[str, Any]]:
        """Busca productos. Por defecto solo con stock. Pasá incluir_sin_stock=true para verificar
        si un producto existe aunque esté sin stock (devuelve en_stock por ítem)."""
        productos = _buscar(consulta, limite=limite, solo_con_stock=not incluir_sin_stock)
        for p in productos:
            if p.canonical_url:
                ctx.deps.shown_links.add(p.canonical_url)
        return compact_for_llm(productos)

    @agent.tool
    def detalle_producto(ctx: RunContext[Deps], id: int) -> dict[str, Any]:
        """Detalle fino de un producto ya mostrado: peso, medidas, SKU, variantes."""
        return _detalle(id, ctx.deps)

    return agent


def _detalle(product_id: int, deps: Deps) -> dict[str, Any]:
    prods = sb_select("ferrepro_productos", f"id=eq.{product_id}&select=id,name,description,canonical_url")
    if not prods:
        return {"error": "producto no encontrado"}
    p = prods[0]
    if p.get("canonical_url"):
        deps.shown_links.add(p["canonical_url"])
    variants = sb_select(
        "ferrepro_variantes",
        f"product_id=eq.{product_id}&select=sku,values,price,promotional_price,weight,position&order=position",
    )
    return {
        "nombre": html.unescape(p.get("name") or ""),
        "descripcion": html.unescape(p["description"]) if p.get("description") else None,
        # Sin stock: el prompt prohíbe informar cantidades.
        "variantes": [
            {
                "sku": v.get("sku"),
                "peso": v.get("weight"),
                "valores": v.get("values"),
                "precio": v.get("promotional_price") or v.get("price"),
            }
            for v in variants
        ],
    }


def run_agent(
    message: str,
    history: list[AgentMessage] | None = None,
    images: list[tuple[bytes, str]] | None = None,
) -> str:
    """`images`: lista de (bytes, media_type) para que el modelo multimodal las vea."""
    agent = build_agent()
    deps = Deps()
    text = _build_input(message, history or [])
    prompt: object = text
    if images:
        prompt = [text, *[BinaryContent(data=data, media_type=mt) for data, mt in images]]
    try:
        result = agent.run_sync(prompt, deps=deps)
    except Exception as exc:
        raise AgentError(f"Falló la corrida del agente: {exc}") from exc
    return guard_links(result.output, deps.shown_links | FIXED_LINKS)


def _build_input(message: str, history: list[AgentMessage]) -> str:
    if not history:
        return message
    lines = ["Historial reciente:"]
    lines += [f"{m.role}: {m.content}" for m in history[-8:]]
    lines += ["", f"Mensaje actual del cliente: {message}"]
    return "\n".join(lines)


_URL_RE = re.compile(r"https?://\S+")


def guard_links(answer: str, allowed: set[str]) -> str:
    """Saca líneas con links que el agente no obtuvo de las tools (anti-alucinación de links).
    Guard mínimo: el prompt ya prohíbe inventar; esto es la red de seguridad para el link.
    ponytail: no replica el renumerado/reparación de odranid; si hace falta, se suma."""
    allowed_norm = {u.rstrip("/") for u in allowed}
    kept: list[str] = []
    for line in answer.splitlines():
        urls = [u.rstrip(".,)").rstrip("/") for u in _URL_RE.findall(line)]
        if urls and any(u not in allowed_norm for u in urls):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


if __name__ == "__main__":
    # Guard puro (sin red) siempre.
    g = guard_links("Mirá esto\n🔗 https://ok/p1\n🔗 https://malo/x", {"https://ok/p1"})
    assert "https://ok/p1" in g and "malo" not in g, g
    print("guard puro: OK")

    if settings.openai_api_key and settings.supabase_url and settings.supabase_service_key:
        print("\n--- 'hola, tenés taladros a batería?' ---")
        print(run_agent("hola, tenés taladros a batería?"))
        print("\n--- 'necesito precio por 10 taladros' (mayorista → derivar) ---")
        out = run_agent("necesito precio por 10 taladros")
        print(out)
        assert "vendedor" in out.lower(), "esperaba derivación a vendedor"
        print("\nself-check vivo: OK")
    else:
        print("self-check vivo: SALTEADO (faltan OPENAI/SUPABASE)")
