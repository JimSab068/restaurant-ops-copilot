"""Explicit procurement action payloads absent from the initial tool set."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateOrderRequest:
    ingredient_id: str
    quantity: float
    supplier_sku_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.ingredient_id, self.supplier_sku_id)):
            raise ValueError("ingredient_id and supplier_sku_id are required")
        if isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True)
class UpdateOrderRequest:
    order_id: str
    quantity: float

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip() or self.quantity <= 0:
            raise ValueError("order_id and positive quantity are required")
