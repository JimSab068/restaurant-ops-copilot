"""
Seed a handful of synthetic tenants with realistic starting state.
Run this once before starting the event generator.
seed.py — Bootstrap synthetic restaurant tenants and starting state.

This module is intended to be run once before the event generator.

The seed operation is transactional: either the complete synthetic dataset
is created, or the transaction is rolled back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from db import get_session, init_db
from models import Ingredient, MenuItem, StaffShift, Supplier, Tenant


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngredientConfig:
    name: str
    unit: str
    price: float
    stock: float


@dataclass(frozen=True)
class MenuItemConfig:
    name: str
    price: float


@dataclass(frozen=True)
class TenantConfig:
    name: str
    suppliers: tuple[str, ...]
    ingredients: tuple[IngredientConfig, ...]
    menu_items: tuple[MenuItemConfig, ...]


TENANT_CONFIGS: Final[tuple[TenantConfig, ...]] = (
    TenantConfig(
        name="Rosa's Trattoria",
        suppliers=("US Foods", "Sysco"),
        ingredients=(
            IngredientConfig("Flour", "kg", 1.20, 50.0),
            IngredientConfig("Tomatoes", "kg", 2.10, 30.0),
            IngredientConfig("Mozzarella", "kg", 6.50, 20.0),
            IngredientConfig("Olive Oil", "liter", 8.00, 15.0),
        ),
        menu_items=(
            MenuItemConfig("Margherita Pizza", 14.0),
            MenuItemConfig("Spaghetti Pomodoro", 13.0),
            MenuItemConfig("Caprese Salad", 9.0),
        ),
    ),
    TenantConfig(
        name="Golden Wok",
        suppliers=("Sysco", "Local Produce Co"),
        ingredients=(
            IngredientConfig("Rice", "kg", 1.50, 60.0),
            IngredientConfig("Soy Sauce", "liter", 3.20, 10.0),
            IngredientConfig("Chicken Breast", "kg", 5.80, 25.0),
            IngredientConfig("Broccoli", "kg", 2.40, 18.0),
        ),
        menu_items=(
            MenuItemConfig("Kung Pao Chicken", 15.0),
            MenuItemConfig("Vegetable Fried Rice", 11.0),
            MenuItemConfig("Broccoli Beef", 16.0),
        ),
    ),
    TenantConfig(
        name="Corner Diner",
        suppliers=("US Foods",),
        ingredients=(
            IngredientConfig("Eggs", "unit", 0.25, 200.0),
            IngredientConfig("Ground Beef", "kg", 7.10, 22.0),
            IngredientConfig("Potatoes", "kg", 1.10, 40.0),
        ),
        menu_items=(
            MenuItemConfig("Classic Burger", 12.0),
            MenuItemConfig("Breakfast Special", 8.0),
            MenuItemConfig("Fries", 5.0),
        ),
    ),
)


class SeedError(RuntimeError):
    """Raised when synthetic database seeding fails."""


def _validate_config() -> None:
    """Validate static seed configuration before touching the database."""
    if not TENANT_CONFIGS:
        raise SeedError("tenant configuration must not be empty")

    tenant_names: set[str] = set()

    for tenant in TENANT_CONFIGS:
        if not tenant.name.strip():
            raise SeedError("tenant name must not be empty")

        if tenant.name in tenant_names:
            raise SeedError(f"duplicate tenant name: {tenant.name}")

        tenant_names.add(tenant.name)

        if not tenant.suppliers:
            raise SeedError(
                f"tenant {tenant.name!r} must have at least one supplier"
            )

        supplier_names = set()

        for supplier in tenant.suppliers:
            if not supplier.strip():
                raise SeedError(
                    f"tenant {tenant.name!r} contains an empty supplier"
                )

            if supplier in supplier_names:
                raise SeedError(
                    f"duplicate supplier {supplier!r} "
                    f"for tenant {tenant.name!r}"
                )

            supplier_names.add(supplier)

        ingredient_names: set[str] = set()

        for ingredient in tenant.ingredients:
            if not ingredient.name.strip():
                raise SeedError("ingredient name must not be empty")

            if ingredient.name in ingredient_names:
                raise SeedError(
                    f"duplicate ingredient {ingredient.name!r} "
                    f"for tenant {tenant.name!r}"
                )

            if ingredient.price <= 0:
                raise SeedError(
                    f"ingredient price must be greater than zero: "
                    f"{ingredient.name!r}"
                )

            if ingredient.stock < 0:
                raise SeedError(
                    f"ingredient stock cannot be negative: "
                    f"{ingredient.name!r}"
                )

            ingredient_names.add(ingredient.name)

        menu_names: set[str] = set()

        for item in tenant.menu_items:
            if not item.name.strip():
                raise SeedError("menu item name must not be empty")

            if item.name in menu_names:
                raise SeedError(
                    f"duplicate menu item {item.name!r} "
                    f"for tenant {tenant.name!r}"
                )

            if item.price <= 0:
                raise SeedError(
                    f"menu item price must be greater than zero: "
                    f"{item.name!r}"
                )

            menu_names.add(item.name)


def _seed_tenant(session, config: TenantConfig) -> tuple[str, str]:
    """Create one complete tenant graph inside the caller's transaction."""
    tenant = Tenant(name=config.name)
    session.add(tenant)
    session.flush()

    supplier_objects: dict[str, Supplier] = {}

    for supplier_name in config.suppliers:
        supplier = Supplier(
            tenant_id=tenant.id,
            name=supplier_name,
        )
        session.add(supplier)
        session.flush()

        supplier_objects[supplier_name] = supplier

    default_supplier = supplier_objects[config.suppliers[0]]

    for ingredient in config.ingredients:
        session.add(
            Ingredient(
                tenant_id=tenant.id,
                name=ingredient.name,
                unit=ingredient.unit,
                current_price=ingredient.price,
                current_stock_level=ingredient.stock,
                current_supplier_id=default_supplier.id,
            )
        )

    for item in config.menu_items:
        session.add(
            MenuItem(
                tenant_id=tenant.id,
                name=item.name,
                current_price=item.price,
            )
        )

    session.add_all(
        (
            StaffShift(
                tenant_id=tenant.id,
                role="line cook",
                day_of_week=0,
                headcount=2,
            ),
            StaffShift(
                tenant_id=tenant.id,
                role="server",
                day_of_week=0,
                headcount=3,
            ),
        )
    )

    return tenant.id, tenant.name


def seed() -> list[tuple[str, str]]:
    """
    Initialize the schema and create the configured synthetic tenants.

    Returns:
        A list of (tenant_id, tenant_name) tuples.

    Raises:
        SeedError: If configuration validation fails.
        Exception: Database failures are propagated after rollback.
    """
    _validate_config()
    init_db()

    session = get_session()

    try:
        created_tenants: list[tuple[str, str]] = []

        for config in TENANT_CONFIGS:
            created_tenants.append(
                _seed_tenant(session, config)
            )

        session.commit()

        logger.info(
            "Synthetic restaurant tenants seeded",
            extra={"tenant_count": len(created_tenants)},
        )

        return created_tenants

    except Exception as exc:
        session.rollback()

        logger.exception(
            "Synthetic tenant seeding failed; transaction rolled back"
        )

        raise SeedError("failed to seed synthetic restaurant state") from exc

    finally:
        session.close()


def main() -> None:
    """CLI entry point."""
    created_tenants = seed()

    print("Seeded tenants:")
    for tenant_id, tenant_name in created_tenants:
        print(f"  {tenant_id}  {tenant_name}")


if __name__ == "__main__":
    main()