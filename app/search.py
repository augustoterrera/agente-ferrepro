from __future__ import annotations

import html
import json
import urllib.request
from typing import Any

from .config import settings
from .models import Product
from .supabase import SupabaseError, rpc


class SearchError(RuntimeError):
    pass


def embed_query(text: str, api_key: str, model: str) -> list[float]:
    """Embebe la query con OpenAI. urllib directo (sin el paquete openai) — una llamada,
    no justifica la dependencia. Mismo modelo que generó los embeddings del catálogo."""
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=json.dumps({"input": text, "model": model}).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["data"][0]["embedding"]
    except Exception as exc:
        raise SearchError(f"No se pudo embeber la query: {exc}") from exc


def buscar_productos(
    query: str,
    *,
    limite: int | None = None,
    solo_con_stock: bool = True,
    semantic: bool | None = None,
) -> list[Product]:
    """Busca en Supabase vía la RPC ferrepro_buscar_productos (híbrido RRF, ya hecho en SQL).

    El único trabajo del lado del agente es embeber la query; el ranking lo arma el RPC.
    Sin openai_api_key (o semantic=False) va léxico+trigram (query_embedding=NULL),
    que el RPC soporta y degrada solo.
    """
    limite = limite or settings.search_default_limit
    use_semantic = settings.semantic_search_enabled if semantic is None else semantic

    embedding_literal = None
    if use_semantic and settings.openai_api_key:
        emb = embed_query(query, settings.openai_api_key, settings.embedding_model)
        embedding_literal = "[" + ",".join(str(float(v)) for v in emb) + "]"

    try:
        rows = rpc(
            "ferrepro_buscar_productos",
            {"consulta": query, "query_embedding": embedding_literal, "solo_con_stock": solo_con_stock, "limite": limite},
        )
    except SupabaseError as exc:
        raise SearchError(str(exc)) from exc

    return [product_from_row(row) for row in (rows or [])]


def product_from_row(row: dict[str, Any]) -> Product:
    # name/description traen entidades HTML sin decodificar (&oacute;, &aacute;) desde el sync.
    # Las decodificamos al leer para no mostrar basura.
    # ponytail: arreglar en origen (facturas-ferrepro) cuando se migre el sync; acá es el parche barato.
    return Product(
        id=int(row["id"]),
        name=html.unescape(row.get("name") or ""),
        brand=row.get("brand"),
        description=html.unescape(row["description"]) if row.get("description") else None,
        category_names=list(row.get("category_names") or []),
        price_min=_float_or_none(row.get("price_min")),
        price_max=_float_or_none(row.get("price_max")),
        in_stock=bool(row.get("in_stock")),
        total_stock=row.get("total_stock"),
        primary_image=row.get("primary_image"),
        canonical_url=row.get("canonical_url"),
        score=float(row.get("score") or 0.0),
    )


def compact_for_llm(products: list[Product]) -> list[dict[str, Any]]:
    """Lo mínimo que el LLM necesita para presentar: nombre, marca, precio, link.
    `en_stock` es booleano (no cantidad): sirve para distinguir un producto sin stock
    cuando se busca con incluir_sin_stock. Nunca exponemos la cantidad."""
    return [
        {
            "id": p.id,
            "nombre": p.name,
            "marca": p.brand,
            "precio": format_price(p.price_min, p.price_max),
            "link": p.canonical_url,
            "categorias": p.category_names,
            "en_stock": p.in_stock,
        }
        for p in products
    ]


def format_price(price_min: float | None, price_max: float | None) -> str | None:
    """Formato argentino sin centavos: 41110.29 -> "$41.110" (regla del prompt)."""
    def fmt(v: float) -> str:
        return "$" + f"{int(round(v)):,}".replace(",", ".")

    if price_min is None and price_max is None:
        return None
    if price_min is None:
        return fmt(price_max)
    if price_max is None or price_min == price_max:
        return fmt(price_min)
    return f"{fmt(price_min)} - {fmt(price_max)}"


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


if __name__ == "__main__":
    # Self-check. Parte pura (sin red) siempre; parte viva si hay SUPABASE_* + OPENAI.
    fake = product_from_row(
        {
            "id": 1,
            "name": "Taladro Percutor 13mm &oacute;ptimo",
            "description": "Voltaje: 20v capacidad mandril 13mm con percusi&oacute;n",
            "category_names": ["HERRAMIENTAS"],
            "price_min": 41110.29,
            "price_max": 41110.29,
            "in_stock": True,
            "total_stock": 10,
            "canonical_url": "https://x/y",
            "score": 0.03,
        }
    )
    assert "óptimo" in fake.name, fake.name
    assert "percusión" in (fake.description or "")
    compact = compact_for_llm([fake])[0]
    assert compact["precio"] == "$41.110", compact["precio"]
    assert "stock" not in compact  # no exponer cantidades
    assert format_price(100.0, 200.0) == "$100 - $200"
    assert format_price(None, None) is None
    print("self-check puro: OK")

    if settings.supabase_url and settings.supabase_service_key and settings.openai_api_key:
        hits = buscar_productos("taladro a bateria 12v", limite=3)
        assert hits, "la búsqueda viva no devolvió resultados"
        assert all(h.name for h in hits)
        print(f"self-check vivo: OK ({len(hits)} hits)")
        for h in hits:
            print(f"  {h.score:.4f}  {h.name[:55]:55} {format_price(h.price_min, h.price_max)}")
    else:
        print("self-check vivo: SALTEADO (faltan SUPABASE_* u OPENAI_API_KEY)")
