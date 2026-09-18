import os

# Must happen before importing db.py / models.py.
os.environ["DATABASE_URL"] = (
    "postgresql://postgres:postgres@localhost:5432/ops_copilot_test"
)

import pytest
from Component_1.db import Base, engine


"""
Shared pytest fixtures.

Points at a separate `ops_copilot_test` database (not the dev DB) so tests
never touch or depend on real seeded data. Tables are dropped and recreated
before every test function for full isolation between tests.

Requires: createdb ops_copilot_test
"""


import pytest
from decimal import Decimal
from Component_1.db import Base, engine, get_session
from Component_1.models import (
    Tenant,
    Supplier,
    Ingredient,
    MenuItem,
    SupplierSKU,
    Recipe,
    RecipeIngredient,
    OrderVolume,
)


@pytest.fixture
def second_tenant_with_data():
    """A second, separate tenant — used for cross-tenant isolation tests."""
    session = get_session()
    try:
        tenant = Tenant(name="Other Restaurant")
        session.add(tenant)
        session.flush()

        supplier = Supplier(tenant_id=tenant.id, name="Other Supplier")
        session.add(supplier)
        session.flush()

        rice = Ingredient(
            tenant_id=tenant.id,
            name="Rice",
            unit="kg",
            current_price=2.0,
            current_stock_level=15.0,
            current_supplier_id=supplier.id,
        )
        session.add(rice)
        session.flush()

        ids = {
            "tenant_id": tenant.id,
            "supplier_id": supplier.id,
            "rice_id": rice.id,
        }
        session.commit()
        return ids
    finally:
        session.close()

@pytest.fixture(autouse=True)
def clean_db():
    """Fresh schema before every test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

@pytest.fixture
def tenant_with_historical_demand(tenant_with_data):
    session = get_session()
    try:
        session.add_all([
            OrderVolume(
                tenant_id=tenant_with_data["tenant_id"],
                menu_item_id=tenant_with_data["pizza_id"],
                quantity=100,
            ),
            OrderVolume(
                tenant_id=tenant_with_data["tenant_id"],
                menu_item_id=tenant_with_data["pizza_id"],
                quantity=110,
            ),
            OrderVolume(
                tenant_id=tenant_with_data["tenant_id"],
                menu_item_id=tenant_with_data["pizza_id"],
                quantity=90,
            ),
        ])

        session.commit()
        return tenant_with_data

    finally:
        session.close()

@pytest.fixture
def tenant_with_data():
    """
    Creates one tenant with a supplier, two ingredients, and one menu item.
    Returns a dict of ids so tests don't need to re-query for them.
    """
    session = get_session()
    try:
        tenant = Tenant(name="Test Tavern")
        session.add(tenant)
        session.flush()

        supplier = Supplier(tenant_id=tenant.id, name="Test Supplier")
        new_supplier = Supplier(tenant_id=tenant.id, name="Replacement Supplier")

        session.add_all([supplier, new_supplier])
        session.flush()

        flour = Ingredient(
            tenant_id=tenant.id,
            name="Flour",
            unit="kg",
            current_price=1.0,
            current_stock_level=20.0,
            current_supplier_id=supplier.id,
        )

        cheese = Ingredient(
            tenant_id=tenant.id,
            name="Cheese",
            unit="kg",
            current_price=5.0,
            current_stock_level=10.0,
            current_supplier_id=supplier.id,
        )

        session.add_all([flour, cheese])
        session.flush()



        flour_sku = SupplierSKU(
            tenant_id=tenant.id,
            supplier_id=supplier.id,
            ingredient_id=flour.id,
            sku_code="FLOUR-001",
            description="All-purpose flour",
            unit="kg",
        )

        cheese_sku = SupplierSKU(
            tenant_id=tenant.id,
            supplier_id=supplier.id,
            ingredient_id=cheese.id,
            sku_code="CHEESE-001",
            description="Mozzarella cheese",
            unit="kg",
        )

        session.add_all([flour_sku, cheese_sku])
        session.flush()

        pizza = MenuItem(
            tenant_id=tenant.id,
            name="Pizza",
            current_price=12.0,
        )
        session.add(pizza)
        session.flush()

        recipe = Recipe(
            tenant_id=tenant.id,
            menu_item_id=pizza.id,
            name="Pizza Recipe",
            active=True,
        )
        session.add(recipe)
        session.flush()

        session.add_all([
            RecipeIngredient(
                tenant_id=tenant.id,
                recipe_id=recipe.id,
                ingredient_id=flour.id,
                quantity=0.25,
            ),
            RecipeIngredient(
                tenant_id=tenant.id,
                recipe_id=recipe.id,
                ingredient_id=cheese.id,
                quantity=0.15,
            ),
        ])
        session.flush()

        ids = {
            "tenant_id": tenant.id,
            "supplier_id": supplier.id,
            "new_supplier_id": new_supplier.id,
            "flour_id": flour.id,
            "cheese_id": cheese.id,
            "pizza_id": pizza.id,
            "flour_sku_id": flour_sku.id,
            "cheese_sku_id": cheese_sku.id,
            "recipe_id": recipe.id,
        }
        session.commit()
        return ids
    finally:
        session.close()


