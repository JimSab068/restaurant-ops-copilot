"""
Seed a handful of synthetic tenants with realistic starting state.
Run this once before starting the event generator.
seed.py — Bootstrap synthetic restaurant tenants and starting state.

This module is intended to be run once before the event generator.

The seed operation is transactional: either the complete synthetic dataset
is created, or the transaction is rolled back.

CHANGED from the original version:
    - seed() is now idempotent. A tenant whose name already exists is
      SKIPPED, not duplicated -- safe to run this file as many times as
      you want (e.g. after adding a new TenantConfig) without doubling
      existing data. Use check_seeded_tenants.py first if you just want
      to know what's already there without writing anything.
    - main() now also ensures every tenant has an active API key
      (reusing one if an active credential already exists -- the raw key
      itself is never recoverable once issued, so a tenant with no
      *active* credential gets a fresh one even if a revoked one exists)
      and writes tenant_fixtures.json in the exact shape
      test_api_acceptance_catalog.py / llm_cloud_acceptance_report.py
      expect, so seeding and the post-deployment test scripts share one
      source of truth instead of you hand-assembling that file.
"""


from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

from Component_1.db import get_session, init_db
from Component_1.models import (
    Ingredient,
    InventoryLevel,
    Invoice,
    InvoiceLine,
    MenuItem,
    OrderVolume,
    Recipe,
    RecipeIngredient,
    SalesOrder,
    SalesOrderLine,
    StaffShift,
    Supplier,
    SupplierPrice,
    SupplierSKU,
    Tenant,
    WasteRecord,
)

# api.generate_api_key is deliberately NOT imported at module level: it
# imports api.auth, which raises at import time if API_KEY_PEPPER isn't
# set in the environment (see api/auth.py). That's the right behavior for
# the actual API/worker services, but it means anything that merely wants
# TENANT_CONFIGS from this module -- like check_seeded_tenants.py -- would
# be forced to have API_KEY_PEPPER set just to list tenants, which has
# nothing to do with credentials. issue_key() is imported lazily inside
# _ensure_api_key() instead, so only a call path that actually issues a
# key requires the pepper to be configured.
#
# api.credentials_models has no such dependency (it's a plain SQLAlchemy
# model with no import of api.auth), so it stays a normal top-level import.
from api.credentials_models import ApiCredential


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
    TenantConfig(
        name="Taco Fiesta",
        suppliers=("US Foods", "Sysco"),
        ingredients=(
            IngredientConfig("Corn Tortillas", "kg", 1.80, 35.0),
            IngredientConfig("Ground Pork", "kg", 6.40, 20.0),
            IngredientConfig("Black Beans", "kg", 1.90, 25.0),
            IngredientConfig("Cilantro", "kg", 4.50, 5.0),
        ),
        menu_items=(
            MenuItemConfig("Carnitas Tacos", 11.0),
            MenuItemConfig("Bean Burrito", 9.0),
            MenuItemConfig("Taco Salad", 10.0),
        ),
    ),
    TenantConfig(
        name="Le Petit Bistro",
        suppliers=("Sysco",),
        ingredients=(
            IngredientConfig("Butter", "kg", 9.20, 12.0),
            IngredientConfig("Duck Breast", "kg", 14.50, 10.0),
            IngredientConfig("Shallots", "kg", 3.10, 8.0),
            IngredientConfig("Red Wine", "liter", 7.50, 18.0),
        ),
        menu_items=(
            MenuItemConfig("Duck Confit", 24.0),
            MenuItemConfig("French Onion Soup", 10.0),
            MenuItemConfig("Steak Frites", 22.0),
        ),
    ),
    TenantConfig(
        name="Curry House",
        suppliers=("US Foods", "Local Produce Co"),
        ingredients=(
            IngredientConfig("Basmati Rice", "kg", 2.20, 45.0),
            IngredientConfig("Chicken Thigh", "kg", 5.10, 28.0),
            IngredientConfig("Garam Masala", "kg", 12.00, 3.0),
            IngredientConfig("Yogurt", "kg", 3.40, 15.0),
        ),
        menu_items=(
            MenuItemConfig("Chicken Tikka Masala", 15.0),
            MenuItemConfig("Vegetable Biryani", 12.0),
            MenuItemConfig("Garlic Naan", 4.0),
        ),
    ),
    TenantConfig(
        name="Sunrise Bakery & Cafe",
        suppliers=("Sysco",),
        ingredients=(
            IngredientConfig("Bread Flour", "kg", 1.10, 60.0),
            IngredientConfig("Eggs", "unit", 0.25, 240.0),
            IngredientConfig("Butter", "kg", 9.20, 14.0),
            IngredientConfig("Coffee Beans", "kg", 16.00, 8.0),
        ),
        menu_items=(
            MenuItemConfig("Croissant", 4.5),
            MenuItemConfig("Avocado Toast", 9.0),
            MenuItemConfig("Latte", 5.0),
        ),
    ),
    TenantConfig(
        name="Blue Ocean Sushi",
        suppliers=("Sysco", "US Foods"),
        ingredients=(
            IngredientConfig("Sushi Rice", "kg", 2.60, 40.0),
            IngredientConfig("Salmon", "kg", 18.50, 15.0),
            IngredientConfig("Nori", "unit", 0.40, 300.0),
            IngredientConfig("Rice Vinegar", "liter", 4.80, 10.0),
        ),
        menu_items=(
            MenuItemConfig("Salmon Nigiri (8pc)", 16.0),
            MenuItemConfig("California Roll", 9.0),
            MenuItemConfig("Miso Soup", 4.0),
        ),
    ),
    TenantConfig(
        name="Mediterraneo Grill",
        suppliers=("Local Produce Co", "US Foods"),
        ingredients=(
            IngredientConfig("Pita Bread", "unit", 0.60, 100.0),
            IngredientConfig("Lamb Shoulder", "kg", 11.20, 18.0),
            IngredientConfig("Chickpeas", "kg", 1.70, 30.0),
            IngredientConfig("Feta Cheese", "kg", 8.10, 12.0),
        ),
        menu_items=(
            MenuItemConfig("Lamb Gyro", 13.0),
            MenuItemConfig("Falafel Plate", 11.0),
            MenuItemConfig("Greek Salad", 9.0),
        ),
    ),
    TenantConfig(
        name="Prairie BBQ Smokehouse",
        suppliers=("US Foods",),
        ingredients=(
            IngredientConfig("Beef Brisket", "kg", 9.80, 30.0),
            IngredientConfig("Pork Ribs", "kg", 7.90, 25.0),
            IngredientConfig("BBQ Sauce", "liter", 5.20, 12.0),
            IngredientConfig("Coleslaw Mix", "kg", 1.60, 15.0),
        ),
        menu_items=(
            MenuItemConfig("Brisket Plate", 18.0),
            MenuItemConfig("Rib Rack", 21.0),
            MenuItemConfig("Pulled Pork Sandwich", 12.0),
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

        supplier_names: set[str] = set()

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

    ingredient_objects: list[Ingredient] = []
    supplier_skus: list[SupplierSKU] = []
    for index, ingredient_config in enumerate(config.ingredients, start=1):
        ingredient = Ingredient(
                tenant_id=tenant.id,
                name=ingredient_config.name,
                unit=ingredient_config.unit,
                current_price=ingredient_config.price,
                current_stock_level=ingredient_config.stock,
                current_supplier_id=default_supplier.id,
            )
        
        session.add(ingredient)
        session.flush()
        ingredient_objects.append(ingredient)

        session.add(
            InventoryLevel(
                tenant_id=tenant.id,
                ingredient_id=ingredient.id,
                quantity=Decimal(str(ingredient_config.stock)),
            )
        )

        sku = SupplierSKU(
            tenant_id=tenant.id, supplier_id=default_supplier.id,
            ingredient_id=ingredient.id, sku_code=f"SEED-{index:03d}",
            description=f"Synthetic {ingredient.name}", unit=ingredient.unit,
        )
        session.add(sku)
        session.flush()
        supplier_skus.append(sku)
        session.add(SupplierPrice(
            tenant_id=tenant.id, supplier_sku_id=sku.id,
            price=Decimal(str(ingredient_config.price)),
        ))

    menu_items: list[MenuItem] = []
    for item_index, item in enumerate(config.menu_items):
        menu_item = MenuItem(
            tenant_id=tenant.id, name=item.name, current_price=item.price,
            # Explicit, not relying on the model's column default: the
            # opportunity detector filters every query on
            # MenuItem.active.is_(True) (margin/demand opportunities) and
            # Recipe.active.is_(True) below -- a seeded item that ends up
            # inactive is invisible to Component 3 and to
            # test_api_acceptance_catalog.py's price-change tests alike.
            active=True,
        )
        session.add(menu_item)
        session.flush()
        menu_items.append(menu_item)
        recipe = Recipe(
            tenant_id=tenant.id, menu_item_id=menu_item.id,
            name=f"{item.name} recipe", active=True,
        )
        session.add(recipe)
        session.flush()
        # Every menu item has a recipe-driven cost basis. Rotating inputs
        # creates distinct supplier/recipe exposure for each synthetic menu.
        for offset in range(min(2, len(ingredient_objects))):
            ingredient = ingredient_objects[(item_index + offset) % len(ingredient_objects)]
            session.add(RecipeIngredient(
                tenant_id=tenant.id, recipe_id=recipe.id,
                ingredient_id=ingredient.id,
                quantity=Decimal("0.20") if offset == 0 else Decimal("0.10"),
            ))

    now = datetime.now(timezone.utc)
    invoice = Invoice(
        tenant_id=tenant.id, supplier_id=default_supplier.id,
        invoice_number=f"SEED-{tenant.id[:8]}", invoice_date=now - timedelta(days=7),
        total_amount=sum(Decimal(str(i.current_price)) * Decimal("10") for i in ingredient_objects),
    )
    session.add(invoice)
    session.flush()
    for sku, ingredient in zip(supplier_skus, ingredient_objects):
        session.add(InvoiceLine(
            tenant_id=tenant.id, invoice_id=invoice.id, supplier_sku_id=sku.id,
            quantity=Decimal("10"), unit_price=ingredient.current_price,
        ))

    # A 28-day history supports demand-decline detection and simulator tests.
    for days_ago in range(1, 29):
        order = SalesOrder(tenant_id=tenant.id, ordered_at=now - timedelta(days=days_ago))
        session.add(order)
        session.flush()
        for item_index, menu_item in enumerate(menu_items):
            quantity = 8 + ((days_ago + item_index) % 5)
            session.add(SalesOrderLine(
                tenant_id=tenant.id, order_id=order.id, menu_item_id=menu_item.id,
                quantity=quantity, unit_price=menu_item.current_price,
            ))
            session.add(OrderVolume(
                tenant_id=tenant.id, menu_item_id=menu_item.id,
                timestamp=now - timedelta(days=days_ago), quantity=quantity,
            ))

    session.add(WasteRecord(
        tenant_id=tenant.id, ingredient_id=ingredient_objects[0].id,
        quantity=Decimal("0.25"), reason="synthetic prep waste",
        recorded_at=now - timedelta(days=2),
    ))

    session.add_all(
        (
            *(
                StaffShift(tenant_id=tenant.id, role=role, day_of_week=day, headcount=headcount)
                for day in range(7)
                for role, headcount in (("line cook", 2), ("server", 3))
            ),
        )
    )

    return str(tenant.id), tenant.name


def seed() -> list[tuple[str, str]]:
    """
    Initialize the schema and create any configured synthetic tenants that
    don't already exist, by name. Idempotent: a tenant already present is
    skipped, not duplicated.

    Returns:
        A list of (tenant_id, tenant_name) tuples for EVERY configured
        tenant -- both ones just created and ones that already existed --
        so callers (main(), below) don't need a second query to know the
        full current set.

    Raises:
        SeedError: If configuration validation fails.
        Exception: Database failures are propagated after rollback.
    """
    _validate_config()
    init_db()

    session = get_session()

    try:
        existing = {
            t.name: str(t.id)
            for t in session.query(Tenant).all()
        }

        result: list[tuple[str, str]] = []
        created_count = 0

        for config in TENANT_CONFIGS:
            if config.name in existing:
                result.append((existing[config.name], config.name))
                logger.info(
                    "Tenant already seeded; skipping",
                    extra={"tenant_name": config.name, "tenant_id": existing[config.name]},
                )
                continue

            tenant_id, tenant_name = _seed_tenant(session, config)
            result.append((tenant_id, tenant_name))
            created_count += 1

        session.commit()

        logger.info(
            "Synthetic restaurant tenants seeded",
            extra={"tenant_count": len(result), "newly_created": created_count},
        )

        return result

    except Exception as exc:
        session.rollback()

        logger.exception(
            "Synthetic tenant seeding failed; transaction rolled back"
        )

        raise SeedError("failed to seed synthetic restaurant state") from exc

    finally:
        session.close()


def _ensure_api_key(session, tenant_id: str) -> str | None:
    """
    Return an active API key's RAW value for this tenant, issuing a new
    one only if no active credential already exists.

    Returns None if an active credential exists but was issued by an
    EARLIER run -- the raw key is never recoverable after issuance (see
    api/credentials_models.py's docstring), so there is nothing to put in
    tenant_fixtures.json for that tenant. In that case the tenant is
    still fully usable; you just need its existing key from wherever you
    saved it the first time this ran, or issue a fresh one deliberately
    via `python -m api.generate_api_key <tenant_id>`.
    """
    has_active = (
        session.query(ApiCredential)
        .filter(ApiCredential.tenant_id == tenant_id, ApiCredential.active.is_(True))
        .first()
        is not None
    )
    if has_active:
        return None

    from api.generate_api_key import issue_key  # see the module-level comment above

    return issue_key(tenant_id)


def _write_fixtures_file(session, tenants: list[tuple[str, str]], path: str = "tenant_fixtures.json") -> None:
    """
    Write tenant_fixtures.json in the shape
    test_api_acceptance_catalog.py / llm_cloud_acceptance_report.py expect:
    one menu item and one ingredient per tenant, plus an API key when one
    could be issued or was already on file this run.
    """
    fixtures: dict[str, dict] = {}
    skipped_keys: list[str] = []

    for tenant_id, tenant_name in tenants:
        menu_item = (
            session.query(MenuItem)
            .filter(MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True))
            .order_by(MenuItem.id)
            .first()
        )
        ingredient = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == tenant_id)
            .order_by(Ingredient.id)
            .first()
        )

        if menu_item is None or ingredient is None:
            logger.warning(
                "Tenant has no menu item/ingredient to fixture; skipping",
                extra={"tenant_id": tenant_id, "tenant_name": tenant_name},
            )
            continue

        raw_key = _ensure_api_key(session, tenant_id)
        if raw_key is None:
            skipped_keys.append(tenant_name)
            continue  # no fixture entry without a key the test scripts can use

        fixtures[tenant_id] = {
            "api_key": raw_key,
            "menu_item_id": str(menu_item.id),
            "menu_item_price": float(menu_item.current_price),
            "ingredient_id": str(ingredient.id),
            "ingredient_price": float(ingredient.current_price),
        }

    with open(path, "w") as f:
        json.dump(fixtures, f, indent=2)

    print(f"\nWrote {len(fixtures)} tenant(s) to {path}")
    if skipped_keys:
        print(
            f"{len(skipped_keys)} tenant(s) already had an active key from an earlier "
            f"run and were NOT included (raw key unrecoverable): {', '.join(skipped_keys)}"
        )
        print(
            "Run `python -m api.generate_api_key <tenant_id>` for those if you need "
            "a fresh key for the test scripts, then add them to the file by hand."
        )


def main() -> None:
    """CLI entry point: seed (idempotently), then ensure keys + fixtures file."""
    created_tenants = seed()

    print("Tenants on file after this run:")
    for tenant_id, tenant_name in created_tenants:
        print(f"  {tenant_id}  {tenant_name}")

    session = get_session()
    try:
        _write_fixtures_file(session, created_tenants)
        session.commit()  # commits any newly issued API credentials
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()