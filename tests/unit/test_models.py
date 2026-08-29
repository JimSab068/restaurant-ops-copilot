"""
Tests for models.py — entity schema, defaults, and tenant scoping.
"""
from db import get_session
from datetime import datetime, timezone
from decimal import Decimal
import pytest  # <--- Add this line
from sqlalchemy.exc import IntegrityError

from models import (
    Tenant, 
    Supplier, 
    Ingredient, 
    MenuItem, 
    StaffShift, 
    Event, 
    EventType,
    OrderVolume,
    Decision,
    DecisionStatus,
    ActionType,
)

@pytest.fixture
def db_session():
    """Provides a transactional database session with rollback cleanup."""
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
    return tenant


@pytest.fixture
def tenant_b(db_session):
    tenant = Tenant(name="Golden Wok")
    db_session.add(tenant)
    db_session.commit()
    return tenant


def test_tenant_created_with_uuid_and_timestamp():
    session = get_session()
    try:
        tenant = Tenant(name="New Place")
        session.add(tenant)
        session.commit()

        assert tenant.id is not None
        assert len(tenant.id) == 36  # uuid4 string length
        assert tenant.created_at is not None
    finally:
        session.close()


def test_ingredient_defaults(tenant_with_data):
    session = get_session()
    try:
        ing = session.get(Ingredient, tenant_with_data["flour_id"])
        assert ing.name == "Flour"
        assert ing.unit == "kg"
        assert ing.current_price == 1.0
        assert ing.current_stock_level == 20.0
        assert ing.current_supplier_id == tenant_with_data["supplier_id"]
    finally:
        session.close()


def test_menu_item_defaults_to_active(tenant_with_data):
    session = get_session()
    try:
        item = session.get(MenuItem, tenant_with_data["pizza_id"])
        assert item.active is True
        assert item.current_price == 12.0
    finally:
        session.close()


def test_staff_shift_headcount_default():
    session = get_session()
    try:
        tenant = Tenant(name="Shift Test")
        session.add(tenant)
        session.flush()

        shift = StaffShift(tenant_id=tenant.id, role="server", day_of_week=2)
        session.add(shift)
        session.commit()

        assert shift.headcount == 1  # default
    finally:
        session.close()


def test_event_stores_arbitrary_json_payload(tenant_with_data):
    session = get_session()
    try:
        event = Event(
            tenant_id=tenant_with_data["tenant_id"],
            event_type=EventType.SUPPLIER_PRICE_CHANGE,
            payload={"ingredient_id": tenant_with_data["flour_id"], "new_price": 1.5, "nested": {"a": 1}},
        )
        session.add(event)
        session.commit()
        session.refresh(event)

        assert event.payload["new_price"] == 1.5
        assert event.payload["nested"]["a"] == 1
        assert event.source == "simulator"  # default
    finally:
        session.close()


def test_query_scoped_to_single_tenant_excludes_others(tenant_with_data, second_tenant_with_data):
    """
    The core guarantee the whole isolation model depends on: querying by
    tenant_id must never return another tenant's rows.
    """
    session = get_session()
    try:
        tenant_a_ingredients = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == tenant_with_data["tenant_id"])
            .all()
        )
        tenant_b_ingredients = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == second_tenant_with_data["tenant_id"])
            .all()
        )

        assert {i.name for i in tenant_a_ingredients} == {"Flour", "Cheese"}
        assert {i.name for i in tenant_b_ingredients} == {"Rice"}

        # explicitly assert no cross-contamination
        a_ids = {i.id for i in tenant_a_ingredients}
        b_ids = {i.id for i in tenant_b_ingredients}
        assert a_ids.isdisjoint(b_ids)
    finally:
        session.close()



# =====================================================================
# 1. PRECISION & FINANCIAL EXACTNESS TESTS
# =====================================================================

def test_decimal_numeric_precision_exactness(db_session, tenant_a):
    """
    Verifies that prices and stock levels use exact Decimal types instead
    of IEEE 754 floating-point approximations.
    """
    ing = Ingredient(
        tenant_id=tenant_a.id,
        name="Truffle Oil",
        unit="liter",
        current_price=Decimal("19.99"),
        current_stock_level=Decimal("10.0005"),
    )
    db_session.add(ing)
    db_session.commit()
    db_session.refresh(ing)

    assert isinstance(ing.current_price, Decimal)
    assert ing.current_price == Decimal("19.99")
    assert ing.current_stock_level == Decimal("10.0005")


# =====================================================================
# 2. RED-TEAM ADVERSARIAL ISOLATION TESTS (DB Engine Enforcement)
# =====================================================================

def test_redteam_cross_tenant_supplier_poisoning_fails(db_session, tenant_a, tenant_b):
    """
    Adversarial Attack: Attempt to assign Tenant B's supplier to Tenant A's ingredient.
    The Composite Foreign Key (tenant_id, current_supplier_id) must reject this at DB level.
    """
    supplier_b = Supplier(tenant_id=tenant_b.id, name="Sysco Global")
    db_session.add(supplier_b)
    db_session.commit()

    # Tenant A ingredient illegally referencing Tenant B's supplier
    poisoned_ingredient = Ingredient(
        tenant_id=tenant_a.id,
        name="Flour",
        current_price=Decimal("2.50"),
        current_supplier_id=supplier_b.id,
    )
    db_session.add(poisoned_ingredient)

    with pytest.raises(IntegrityError) as exc_info:
        db_session.commit()
    
    assert "fk_ingredient_supplier_tenant" in str(exc_info.value).lower() or "foreign key" in str(exc_info.value).lower()


def test_redteam_supplier_delete_restrict_cascade(db_session, tenant_a):
    """
    Adversarial Attack: Attempt to delete a supplier while an active ingredient depends on it.
    The RESTRICT constraint must block orphaned ingredients.
    """
    supplier = Supplier(tenant_id=tenant_a.id, name="Local Produce")
    db_session.add(supplier)
    db_session.commit()

    ing = Ingredient(
        tenant_id=tenant_a.id,
        name="Tomatoes",
        current_price=Decimal("1.20"),
        current_supplier_id=supplier.id,
    )
    db_session.add(ing)
    db_session.commit()

    # Attempt to delete the supplier directly
    db_session.delete(supplier)
    with pytest.raises(IntegrityError):
        db_session.commit()


# =====================================================================
# 3. RED-TEAM CHECK CONSTRAINT BOUNDARY TESTS
# =====================================================================

@pytest.mark.parametrize("invalid_price", [Decimal("-0.01"), Decimal("-500.00")])
def test_redteam_negative_ingredient_price_fails(db_session, tenant_a, invalid_price):
    """Adversarial Attack: Injecting negative currency amounts to manipulate accounting."""
    ing = Ingredient(tenant_id=tenant_a.id, name="Bad Price", current_price=invalid_price)
    db_session.add(ing)
    
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize("invalid_day", [-1, 7, 99])
def test_redteam_invalid_staff_shift_day_fails(db_session, tenant_a, invalid_day):
    """Adversarial Attack: Invariant breach on day_of_week enum bounds (0-6)."""
    shift = StaffShift(tenant_id=tenant_a.id, role="Chef", day_of_week=invalid_day, headcount=2)
    db_session.add(shift)

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize("invalid_confidence", [Decimal("-0.1000"), Decimal("1.0001")])
def test_redteam_out_of_bounds_confidence_score_fails(db_session, tenant_a, invalid_confidence):
    """Adversarial Attack: Simulator injecting confidence scores outside 0.0 - 1.0 range."""
    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.PRICE_CHANGE,
        action_payload={"item_id": "123", "new_price": 10.0},
        confidence=invalid_confidence,
    )
    db_session.add(decision)

    with pytest.raises(IntegrityError):
        db_session.commit()


# =====================================================================
# 4. MODEL LINEAGE, AUDIT LEDGER & PAYLOAD RESILIENCE TESTS
# =====================================================================

def test_decision_model_lineage_and_metadata_persistence(db_session, tenant_a):
    """Validates that ML lineage metadata and features are fully recorded."""
    now = datetime.now(timezone.utc)
    decision = Decision(
        tenant_id=tenant_a.id,
        action_type=ActionType.MENU_SWAP,
        action_payload={"remove_item": "Caprese"},
        status=DecisionStatus.SIMULATED,
        confidence=Decimal("0.8500"),
        model_version="2.1.0-alpha",
        training_timestamp=now,
        feature_snapshot={"historical_avg_sales": 42, "elasticity_score": -1.2},
    )
    db_session.add(decision)
    db_session.commit()
    db_session.refresh(decision)

    assert decision.model_version == "2.1.0-alpha"
    assert decision.feature_snapshot["elasticity_score"] == -1.2
    assert decision.confidence == Decimal("0.8500")


def test_event_hash_chain_and_complex_payload_resilience(db_session, tenant_a):
    """Tests cryptographic SHA-256 hash storing and resilient JSON payload handling."""
    mock_hash = "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
    mock_prev_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    event = Event(
        tenant_id=tenant_a.id,
        event_type=EventType.SUPPLIER_PRICE_CHANGE,
        payload={
            "nested_injection": "'; DROP TABLE tenants; --",
            "unicode_chars": "辣椒 / 🌶️",
            "values": [1, 2, {"key": "val"}],
        },
        hash=mock_hash,
        previous_hash=mock_prev_hash,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.hash == mock_hash
    assert event.previous_hash == mock_prev_hash
    assert event.payload["nested_injection"] == "'; DROP TABLE tenants; --"
    assert event.payload["unicode_chars"] == "辣椒 / 🌶️"