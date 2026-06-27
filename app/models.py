from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Product(BaseModel):
    """Un producto tal como lo devuelve la RPC ferrepro_buscar_productos.

    Es la fila del ranking híbrido (léxico+trigram+semántico). Los detalles finos
    (variantes: sku, peso, medidas) NO vienen acá; se traen aparte por id cuando hacen falta.
    """

    id: int
    name: str
    brand: str | None = None
    description: str | None = None
    category_names: list[str] = Field(default_factory=list)
    price_min: float | None = None
    price_max: float | None = None
    in_stock: bool = False
    total_stock: int | None = None
    primary_image: str | None = None
    canonical_url: str | None = None
    score: float = 0.0
