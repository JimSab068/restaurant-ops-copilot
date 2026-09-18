"""Structured contracts for future recipe update and menu-item swap tools."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecipeUpdateRequest:
    recipe_id: str
    ingredient_id: str
    quantity: float

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.recipe_id, self.ingredient_id)):
            raise ValueError("recipe_id and ingredient_id are required")
        if isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True)
class MenuSwapRequest:
    retiring_menu_item_id: str
    replacement_name: str
    initial_price: float

    def __post_init__(self) -> None:
        if not isinstance(self.retiring_menu_item_id, str) or not self.retiring_menu_item_id.strip():
            raise ValueError("retiring_menu_item_id is required")
        if not isinstance(self.replacement_name, str) or not self.replacement_name.strip():
            raise ValueError("replacement_name is required")
        if isinstance(self.initial_price, bool) or self.initial_price <= 0:
            raise ValueError("initial_price must be positive")
