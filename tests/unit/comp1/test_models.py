"""
Tests for Component 1 entity models.

Coverage:
- tenant ownership and isolation
- exact Decimal financial/inventory values
- composite tenant foreign keys
- domain CHECK constraints
- recipe -> ingredient relationships
- ingredient -> supplier SKU relationships
- supplier pricing
- inventory projections
- invoice procurement history
- sales/demand history
- waste records
- staffing
- decisions and model lineage
- event schema and payload persistence
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from Component_1.db import get_session
from Component_1.models import (
    ActionType,
    Decision,
    DecisionStatus,
    Event,
    EventType,
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


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def db_session():
    """Provide a database session with rollback cleanup."""
    session = get_session()

    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def tenant_a(db_session):
    tenant = Tenant(name="Rosa's Trattoria")
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


@pytest.fixture
def tenant_b(db_session):
    tenant = Tenant(name="Golden Wok")
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


@pytest.fixture
def supplier_a(db_session, tenant_a):
    supplier = Supplier(
        tenant_id=tenant_a.id,
        name="Local Produce",
    )
    db_session.add(supplier)
    db_session.commit()
    db_session.refresh(supplier)
    return supplier


@pytest.fixture
def supplier_b(db_session, tenant_b):
    supplier = Supplier(
        tenant_id=tenant_b.id,
        name="Sysco Global",
    )
    db_session.add(supplier)
    db_session.commit()
    db_session.refresh(supplier)
    return supplier


@pytest.fixture
def ingredient_a(db_session, tenant_a, supplier_a):
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Flour",
        unit="kg",
        current_price=Decimal("1.00"),
        current_supplier_id=supplier_a.id,
        current_stock_level=Decimal("20.0000"),
    )
    db_session.add(ingredient)
    db_session.commit()
    db_session.refresh(ingredient)
    return ingredient


@pytest.fixture
def menu_item_a(db_session, tenant_a):
    item = MenuItem(
        tenant_id=tenant_a.id,
        name="Margherita Pizza",
        current_price=Decimal("12.00"),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


@pytest.fixture
def recipe_a(db_session, tenant_a, menu_item_a):
    recipe = Recipe(
        tenant_id=tenant_a.id,
        menu_item_id=menu_item_a.id,
        name="Margherita Pizza Recipe",
    )
    db_session.add(recipe)
    db_session.commit()
    db_session.refresh(recipe)
    return recipe


@pytest.fixture
def supplier_sku_a(
    db_session,
    tenant_a,
    supplier_a,
    ingredient_a,
):
    sku = SupplierSKU(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        ingredient_id=ingredient_a.id,
        sku_code="FLOUR-001",
        description="All-purpose flour",
        unit="kg",
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


# ============================================================================
# 1. TENANT MODEL
# ============================================================================


def test_tenant_created_with_uuid_and_timestamp(db_session):
    tenant = Tenant(name="New Place")

    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)

    assert tenant.id is not None
    assert len(tenant.id) == 36
    assert tenant.created_at is not None
    assert tenant.created_at.tzinfo is not None


def test_tenants_have_independent_ids(db_session):
    tenant_a = Tenant(name="Restaurant A")
    tenant_b = Tenant(name="Restaurant B")

    db_session.add_all([tenant_a, tenant_b])
    db_session.commit()

    assert tenant_a.id != tenant_b.id


# ============================================================================
# 2. INGREDIENT / SUPPLIER MODEL
# ============================================================================


def test_ingredient_defaults(
    db_session,
    tenant_a,
    supplier_a,
):
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Flour",
        unit="kg",
        current_supplier_id=supplier_a.id,
    )

    db_session.add(ingredient)
    db_session.commit()
    db_session.refresh(ingredient)

    assert ingredient.name == "Flour"
    assert ingredient.unit == "kg"
    assert ingredient.current_price == Decimal("0.00")
    assert ingredient.current_stock_level == Decimal("0.0000")
    assert ingredient.current_supplier_id == supplier_a.id


def test_ingredient_decimal_precision_exactness(
    db_session,
    tenant_a,
):
    """
    Prices and stock must use exact Decimal-compatible NUMERIC values.
    """
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Truffle Oil",
        unit="liter",
        current_price=Decimal("19.99"),
        current_stock_level=Decimal("10.0005"),
    )

    db_session.add(ingredient)
    db_session.commit()
    db_session.refresh(ingredient)

    assert isinstance(ingredient.current_price, Decimal)
    assert isinstance(ingredient.current_stock_level, Decimal)

    assert ingredient.current_price == Decimal("19.99")
    assert ingredient.current_stock_level == Decimal("10.0005")


def test_negative_ingredient_price_fails(
    db_session,
    tenant_a,
):
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Invalid Ingredient",
        current_price=Decimal("-0.01"),
    )

    db_session.add(ingredient)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_negative_ingredient_stock_fails(
    db_session,
    tenant_a,
):
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Invalid Stock",
        current_stock_level=Decimal("-1.0000"),
    )

    db_session.add(ingredient)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_cross_tenant_supplier_reference_fails(
    db_session,
    tenant_a,
    supplier_b,
):
    """
    Tenant A cannot reference Tenant B's supplier.

    This is a database-level isolation guarantee.
    """
    ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Poisoned Flour",
        current_supplier_id=supplier_b.id,
    )

    db_session.add(ingredient)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_supplier_delete_is_restricted_when_ingredient_depends_on_it(
    db_session,
    tenant_a,
    supplier_a,
    ingredient_a,
):
    db_session.delete(supplier_a)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 3. MENU ITEM
# ============================================================================


def test_menu_item_defaults_to_active(
    db_session,
    tenant_a,
):
    item = MenuItem(
        tenant_id=tenant_a.id,
        name="Margherita Pizza",
        current_price=Decimal("12.00"),
    )

    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.active is True
    assert item.current_price == Decimal("12.00")


def test_negative_menu_price_fails(
    db_session,
    tenant_a,
):
    item = MenuItem(
        tenant_id=tenant_a.id,
        name="Invalid Pizza",
        current_price=Decimal("-1.00"),
    )

    db_session.add(item)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 4. SUPPLIER SKU
# ============================================================================


def test_supplier_sku_links_supplier_and_ingredient(
    supplier_sku_a,
    supplier_a,
    ingredient_a,
):
    assert supplier_sku_a.supplier_id == supplier_a.id
    assert supplier_sku_a.ingredient_id == ingredient_a.id
    assert supplier_sku_a.sku_code == "FLOUR-001"
    assert supplier_sku_a.active is True


def test_supplier_sku_cannot_cross_tenant_supplier(
    db_session,
    tenant_a,
    supplier_b,
    ingredient_a,
):
    sku = SupplierSKU(
        tenant_id=tenant_a.id,
        supplier_id=supplier_b.id,
        ingredient_id=ingredient_a.id,
        sku_code="CROSS-TENANT",
    )

    db_session.add(sku)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_supplier_sku_cannot_cross_tenant_ingredient(
    db_session,
    tenant_a,
    supplier_a,
    ingredient_a,
    tenant_b,
):
    ingredient_b = Ingredient(
        tenant_id=tenant_b.id,
        name="Rice",
    )
    db_session.add(ingredient_b)
    db_session.commit()

    sku = SupplierSKU(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        ingredient_id=ingredient_b.id,
        sku_code="CROSS-TENANT-INGREDIENT",
    )

    db_session.add(sku)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_supplier_sku_code_unique_per_supplier(
    db_session,
    tenant_a,
    supplier_a,
    ingredient_a,
):
    first = SupplierSKU(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        ingredient_id=ingredient_a.id,
        sku_code="DUPLICATE",
    )

    second = SupplierSKU(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        ingredient_id=ingredient_a.id,
        sku_code="DUPLICATE",
    )

    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 5. SUPPLIER PRICE
# ============================================================================


def test_supplier_price_persists_exact_decimal(
    db_session,
    tenant_a,
    supplier_sku_a,
):
    effective_at = datetime.now(timezone.utc)

    price = SupplierPrice(
        tenant_id=tenant_a.id,
        supplier_sku_id=supplier_sku_a.id,
        price=Decimal("4.375"),
        effective_at=effective_at,
    )

    db_session.add(price)
    db_session.commit()
    db_session.refresh(price)

    assert isinstance(price.price, Decimal)
    assert price.price == Decimal("4.38")
    assert price.effective_at is not None


def test_negative_supplier_price_fails(
    db_session,
    tenant_a,
    supplier_sku_a,
):
    price = SupplierPrice(
        tenant_id=tenant_a.id,
        supplier_sku_id=supplier_sku_a.id,
        price=Decimal("-0.01"),
    )

    db_session.add(price)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_supplier_price_cannot_reference_other_tenant_sku(
    db_session,
    tenant_a,
    supplier_b,
    tenant_b,
):
    ingredient_b = Ingredient(
        tenant_id=tenant_b.id,
        name="Rice",
    )
    db_session.add(ingredient_b)
    db_session.commit()

    sku_b = SupplierSKU(
        tenant_id=tenant_b.id,
        supplier_id=supplier_b.id,
        ingredient_id=ingredient_b.id,
        sku_code="RICE-001",
    )
    db_session.add(sku_b)
    db_session.commit()

    price = SupplierPrice(
        tenant_id=tenant_a.id,
        supplier_sku_id=sku_b.id,
        price=Decimal("5.00"),
    )

    db_session.add(price)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 6. RECIPE MODEL
# ============================================================================


def test_recipe_links_to_menu_item(
    recipe_a,
    menu_item_a,
):
    assert recipe_a.menu_item_id == menu_item_a.id
    assert recipe_a.active is True


def test_recipe_cannot_reference_other_tenant_menu_item(
    db_session,
    tenant_a,
    tenant_b,
):
    item_b = MenuItem(
        tenant_id=tenant_b.id,
        name="Other Tenant Pizza",
        current_price=Decimal("10.00"),
    )
    db_session.add(item_b)
    db_session.commit()

    recipe = Recipe(
        tenant_id=tenant_a.id,
        menu_item_id=item_b.id,
        name="Poisoned Recipe",
    )

    db_session.add(recipe)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_recipe_ingredient_persists_quantity(
    db_session,
    tenant_a,
    recipe_a,
    ingredient_a,
):
    recipe_ingredient = RecipeIngredient(
        tenant_id=tenant_a.id,
        recipe_id=recipe_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("0.2500"),
    )

    db_session.add(recipe_ingredient)
    db_session.commit()
    db_session.refresh(recipe_ingredient)

    assert recipe_ingredient.quantity == Decimal("0.2500")


def test_recipe_ingredient_requires_positive_quantity(
    db_session,
    tenant_a,
    recipe_a,
    ingredient_a,
):
    recipe_ingredient = RecipeIngredient(
        tenant_id=tenant_a.id,
        recipe_id=recipe_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("0"),
    )

    db_session.add(recipe_ingredient)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_recipe_ingredient_cannot_cross_tenant_recipe(
    db_session,
    tenant_a,
    tenant_b,
    ingredient_a,
):
    item_b = MenuItem(
        tenant_id=tenant_b.id,
        name="Tenant B Pizza",
        current_price=Decimal("10.00"),
    )
    db_session.add(item_b)
    db_session.commit()

    recipe_b = Recipe(
        tenant_id=tenant_b.id,
        menu_item_id=item_b.id,
        name="Tenant B Recipe",
    )
    db_session.add(recipe_b)
    db_session.commit()

    recipe_ingredient = RecipeIngredient(
        tenant_id=tenant_a.id,
        recipe_id=recipe_b.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("1.0000"),
    )

    db_session.add(recipe_ingredient)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 7. INVENTORY
# ============================================================================


def test_inventory_level_persists_current_quantity(
    db_session,
    tenant_a,
    ingredient_a,
):
    inventory = InventoryLevel(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("15.7500"),
    )

    db_session.add(inventory)
    db_session.commit()
    db_session.refresh(inventory)

    assert inventory.quantity == Decimal("15.7500")
    assert inventory.updated_at is not None


def test_inventory_cannot_be_negative(
    db_session,
    tenant_a,
    ingredient_a,
):
    inventory = InventoryLevel(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("-1.0000"),
    )

    db_session.add(inventory)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_inventory_is_unique_per_tenant_and_ingredient(
    db_session,
    tenant_a,
    ingredient_a,
):
    first = InventoryLevel(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("5.0000"),
    )

    second = InventoryLevel(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("10.0000"),
    )

    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 8. INVOICE / PROCUREMENT
# ============================================================================


def test_invoice_persists_supplier_and_total(
    db_session,
    tenant_a,
    supplier_a,
):
    invoice = Invoice(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        invoice_number="INV-1001",
        invoice_date=datetime.now(timezone.utc),
        total_amount=Decimal("125.50"),
    )

    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.invoice_number == "INV-1001"
    assert invoice.total_amount == Decimal("125.50")


def test_invoice_cannot_reference_other_tenant_supplier(
    db_session,
    tenant_a,
    supplier_b,
):
    invoice = Invoice(
        tenant_id=tenant_a.id,
        supplier_id=supplier_b.id,
        invoice_number="CROSS-TENANT",
        invoice_date=datetime.now(timezone.utc),
        total_amount=Decimal("100.00"),
    )

    db_session.add(invoice)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_negative_invoice_total_fails(
    db_session,
    tenant_a,
    supplier_a,
):
    invoice = Invoice(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        invoice_number="BAD-INVOICE",
        invoice_date=datetime.now(timezone.utc),
        total_amount=Decimal("-1.00"),
    )

    db_session.add(invoice)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_invoice_line_links_invoice_and_sku(
    db_session,
    tenant_a,
    supplier_a,
    supplier_sku_a,
):
    invoice = Invoice(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        invoice_number="INV-2001",
        invoice_date=datetime.now(timezone.utc),
        total_amount=Decimal("50.00"),
    )

    db_session.add(invoice)
    db_session.commit()

    line = InvoiceLine(
        tenant_id=tenant_a.id,
        invoice_id=invoice.id,
        supplier_sku_id=supplier_sku_a.id,
        quantity=Decimal("10.0000"),
        unit_price=Decimal("5.00"),
    )

    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)

    assert line.quantity == Decimal("10.0000")
    assert line.unit_price == Decimal("5.00")


def test_invoice_line_rejects_non_positive_quantity(
    db_session,
    tenant_a,
    supplier_a,
    supplier_sku_a,
):
    invoice = Invoice(
        tenant_id=tenant_a.id,
        supplier_id=supplier_a.id,
        invoice_number="INV-BAD-QTY",
        invoice_date=datetime.now(timezone.utc),
        total_amount=Decimal("50.00"),
    )

    db_session.add(invoice)
    db_session.commit()

    line = InvoiceLine(
        tenant_id=tenant_a.id,
        invoice_id=invoice.id,
        supplier_sku_id=supplier_sku_a.id,
        quantity=Decimal("0"),
        unit_price=Decimal("5.00"),
    )

    db_session.add(line)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 9. SALES / DEMAND
# ============================================================================


def test_sales_order_and_line_persist(
    db_session,
    tenant_a,
    menu_item_a,
):
    ordered_at = datetime.now(timezone.utc)

    order = SalesOrder(
        tenant_id=tenant_a.id,
        ordered_at=ordered_at,
    )

    db_session.add(order)
    db_session.commit()

    line = SalesOrderLine(
        tenant_id=tenant_a.id,
        order_id=order.id,
        menu_item_id=menu_item_a.id,
        quantity=3,
        unit_price=Decimal("12.00"),
    )

    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)

    assert line.quantity == 3
    assert line.unit_price == Decimal("12.00")


def test_sales_line_requires_positive_quantity(
    db_session,
    tenant_a,
    menu_item_a,
):
    order = SalesOrder(tenant_id=tenant_a.id)

    db_session.add(order)
    db_session.commit()

    line = SalesOrderLine(
        tenant_id=tenant_a.id,
        order_id=order.id,
        menu_item_id=menu_item_a.id,
        quantity=0,
        unit_price=Decimal("12.00"),
    )

    db_session.add(line)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_order_volume_requires_positive_quantity(
    db_session,
    tenant_a,
    menu_item_a,
):
    volume = OrderVolume(
        tenant_id=tenant_a.id,
        menu_item_id=menu_item_a.id,
        quantity=0,
    )

    db_session.add(volume)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 10. WASTE
# ============================================================================


def test_waste_record_persists(
    db_session,
    tenant_a,
    ingredient_a,
):
    waste = WasteRecord(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("2.5000"),
        reason="Spoilage",
    )

    db_session.add(waste)
    db_session.commit()
    db_session.refresh(waste)

    assert waste.quantity == Decimal("2.5000")
    assert waste.reason == "Spoilage"
    assert waste.recorded_at is not None


def test_waste_requires_positive_quantity(
    db_session,
    tenant_a,
    ingredient_a,
):
    waste = WasteRecord(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_a.id,
        quantity=Decimal("0"),
        reason="Invalid",
    )

    db_session.add(waste)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_waste_cannot_reference_other_tenant_ingredient(
    db_session,
    tenant_a,
    tenant_b,
    supplier_b,
):
    ingredient_b = Ingredient(
        tenant_id=tenant_b.id,
        name="Rice",
        current_supplier_id=supplier_b.id,
    )

    db_session.add(ingredient_b)
    db_session.commit()

    waste = WasteRecord(
        tenant_id=tenant_a.id,
        ingredient_id=ingredient_b.id,
        quantity=Decimal("1.0000"),
        reason="Cross tenant attack",
    )

    db_session.add(waste)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 11. STAFFING
# ============================================================================


def test_staff_shift_headcount_default(
    db_session,
    tenant_a,
):
    shift = StaffShift(
        tenant_id=tenant_a.id,
        role="server",
        day_of_week=2,
    )

    db_session.add(shift)
    db_session.commit()
    db_session.refresh(shift)

    assert shift.headcount == 1


@pytest.mark.parametrize(
    "invalid_day",
    [-1, 7, 99],
)
def test_invalid_staff_shift_day_fails(
    db_session,
    tenant_a,
    invalid_day,
):
    shift = StaffShift(
        tenant_id=tenant_a.id,
        role="Chef",
        day_of_week=invalid_day,
        headcount=2,
    )

    db_session.add(shift)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_negative_staff_headcount_fails(
    db_session,
    tenant_a,
):
    shift = StaffShift(
        tenant_id=tenant_a.id,
        role="Chef",
        day_of_week=2,
        headcount=-1,
    )

    db_session.add(shift)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 12. DECISION / CALIBRATION LEDGER
# ============================================================================


def test_decision_model_lineage_and_metadata_persistence(
    db_session,
    tenant_a,
):
    now = datetime.now(timezone.utc)

    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.MENU_SWAP,
        action_payload={"remove_item": "Caprese"},
        status=DecisionStatus.SIMULATED,
        confidence=Decimal("0.8500"),
        model_version="2.1.0-alpha",
        training_timestamp=now,
        feature_snapshot={
            "historical_avg_sales": 42,
            "elasticity_score": -1.2,
        },
    )

    db_session.add(decision)
    db_session.commit()
    db_session.refresh(decision)

    assert decision.model_version == "2.1.0-alpha"
    assert decision.feature_snapshot["elasticity_score"] == -1.2
    assert decision.confidence == Decimal("0.8500")


@pytest.mark.parametrize(
    "invalid_confidence",
    [
        Decimal("-0.0001"),
        Decimal("-0.1000"),
        Decimal("1.0001"),
        Decimal("2.0000"),
    ],
)
def test_out_of_bounds_confidence_fails(
    db_session,
    tenant_a,
    invalid_confidence,
):
    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.PRICE_CHANGE,
        action_payload={"item_id": "123", "new_price": 10.0},
        confidence=invalid_confidence,
    )

    db_session.add(decision)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_null_confidence_is_allowed(
    db_session,
    tenant_a,
):
    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.PRICE_CHANGE,
        action_payload={"item_id": "123"},
        confidence=None,
    )

    db_session.add(decision)
    db_session.commit()

    assert decision.confidence is None


def test_negative_calibration_error_fails(
    db_session,
    tenant_a,
):
    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.PRICE_CHANGE,
        action_payload={"item_id": "123"},
        calibration_error=Decimal("-0.01"),
    )

    db_session.add(decision)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 13. EVENT MODEL
# ============================================================================


def test_event_stores_arbitrary_json_payload(
    db_session,
    tenant_a,
):
    event = Event(
        tenant_id=tenant_a.id,
        event_type=EventType.SUPPLIER_PRICE_CHANGE,
        payload={
            "ingredient_id": "ingredient-123",
            "supplier_id": "supplier-123",
            "sku_id": "sku-123",
            "old_price": 1.25,
            "new_price": 1.50,
            "nested": {
                "a": 1,
                "unicode": "辣椒 / 🌶️",
            },
            "values": [1, 2, {"key": "val"}],
        },
        hash="a" * 64,
        previous_hash="0" * 64,
    )

    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.payload["new_price"] == 1.50
    assert event.payload["nested"]["a"] == 1
    assert event.payload["nested"]["unicode"] == "辣椒 / 🌶️"
    assert event.source == "simulator"


def test_event_supports_all_component_one_event_types(
    db_session,
    tenant_a,
):
    event_types = list(EventType)

    assert EventType.SUPPLIER_PRICE_CHANGE in event_types
    assert EventType.SUPPLIER_SWITCH in event_types
    assert EventType.STOCKOUT in event_types
    assert EventType.RESTOCK in event_types
    assert EventType.INVENTORY_ADJUSTMENT in event_types
    assert EventType.WASTE_RECORDED in event_types
    assert EventType.DEMAND_SPIKE in event_types
    assert EventType.ORDER_RECORDED in event_types
    assert EventType.MENU_ITEM_ADDED in event_types
    assert EventType.MENU_ITEM_REMOVED in event_types
    assert EventType.MENU_PRICE_CHANGE in event_types
    assert EventType.STAFFING_CHANGE in event_types
    assert EventType.INVOICE_RECORDED in event_types


def test_event_hash_fields_require_64_characters(
    db_session,
    tenant_a,
):
    event = Event(
        tenant_id=tenant_a.id,
        event_type=EventType.RESTOCK,
        payload={
            "ingredient_id": "ingredient-123",
            "new_stock_level": 25,
        },
        hash="short",
        previous_hash="0" * 64,
    )

    db_session.add(event)

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_event_previous_hash_requires_64_characters(
    db_session,
    tenant_a,
):
    event = Event(
        tenant_id=tenant_a.id,
        event_type=EventType.RESTOCK,
        payload={
            "ingredient_id": "ingredient-123",
            "new_stock_level": 25,
        },
        hash="a" * 64,
        previous_hash="short",
    )

    db_session.add(event)

    with pytest.raises(IntegrityError):
        db_session.commit()


# ============================================================================
# 14. TENANT ISOLATION
# ============================================================================


def test_query_scoped_to_single_tenant_excludes_others(
    db_session,
    tenant_a,
    tenant_b,
):
    supplier_a = Supplier(
        tenant_id=tenant_a.id,
        name="Supplier A",
    )

    supplier_b = Supplier(
        tenant_id=tenant_b.id,
        name="Supplier B",
    )

    db_session.add_all([supplier_a, supplier_b])
    db_session.commit()

    ingredient_a = Ingredient(
        tenant_id=tenant_a.id,
        name="Flour",
        current_supplier_id=supplier_a.id,
    )

    ingredient_b = Ingredient(
        tenant_id=tenant_b.id,
        name="Rice",
        current_supplier_id=supplier_b.id,
    )

    db_session.add_all([ingredient_a, ingredient_b])
    db_session.commit()

    tenant_a_ingredients = (
        db_session.query(Ingredient)
        .filter(Ingredient.tenant_id == tenant_a.id)
        .all()
    )

    tenant_b_ingredients = (
        db_session.query(Ingredient)
        .filter(Ingredient.tenant_id == tenant_b.id)
        .all()
    )

    assert {i.name for i in tenant_a_ingredients} == {"Flour"}
    assert {i.name for i in tenant_b_ingredients} == {"Rice"}

    a_ids = {i.id for i in tenant_a_ingredients}
    b_ids = {i.id for i in tenant_b_ingredients}

    assert a_ids.isdisjoint(b_ids)