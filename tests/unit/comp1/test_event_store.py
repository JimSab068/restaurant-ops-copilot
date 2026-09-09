"""
Tests for Component_1.event_store.

The event store is a high-stakes Component 1 boundary because it provides:

    event -> append-only history
         -> current-state projection
         -> point-in-time reconstruction
         -> tenant isolation
         -> tamper-evident hash chain

These tests intentionally exercise both the happy path and failure paths.
"""

from datetime import datetime, timedelta, timezone

import pytest

from Component_1.db import get_db_context, get_session
from Component_1.event_store import (
    EventStoreError,
    HashChainCorruptedError,
    TenantIsolationError,
    append_event,
    get_tenant_events,
    reconstruct_state_at,
    verify_chain_integrity,
)
from Component_1.models import (
    Event,
    EventType,
    Ingredient,
    InventoryLevel,
    MenuItem,
    OrderVolume,
    SalesOrder,
    SalesOrderLine,
    StaffShift,
    SupplierPrice,
    Tenant,
    WasteRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_one(session, model, object_id):
    """Load a tenant-owned object by primary key."""
    return session.get(model, object_id)


# ---------------------------------------------------------------------------
# Basic append / retrieval
# ---------------------------------------------------------------------------


def append_event_creates_event_row(tenant_with_data):
    event = append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "old_price": 1.0,
            "new_price": 1.3,
        },
    )

    assert event.id is not None
    assert event.tenant_id == tenant_with_data["tenant_id"]
    assert event.event_type == EventType.SUPPLIER_PRICE_CHANGE
    assert event.payload["new_price"] == 1.3
    assert event.source == "simulator"
    assert len(event.hash) == 64
    assert len(event.previous_hash) == 64


def test_get_tenant_events_returns_events_in_order(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    first = append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "new_price": 1.25,
        },
    )

    second = append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "new_price": 1.50,
        },
    )

    events = get_tenant_events(tenant_id)

    assert [event.id for event in events] == [first.id, second.id]


def test_get_tenant_events_supports_event_type_filter(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "new_price": 1.50,
        },
    )

    append_event(
        tenant_id,
        EventType.STOCKOUT,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
        },
    )

    price_events = get_tenant_events(
        tenant_id,
        event_type=EventType.SUPPLIER_PRICE_CHANGE,
    )

    assert len(price_events) == 1
    assert price_events[0].event_type == EventType.SUPPLIER_PRICE_CHANGE


def test_get_tenant_events_respects_limit(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    for price in (1.10, 1.20, 1.30):
        append_event(
            tenant_id,
            EventType.SUPPLIER_PRICE_CHANGE,
            {
                "ingredient_id": tenant_with_data["flour_id"],
                "supplier_sku_id": tenant_with_data["flour_sku_id"],
                "new_price": price,
            },
        )

    events = get_tenant_events(tenant_id, limit=2)

    assert len(events) == 2


# ---------------------------------------------------------------------------
# Supplier price projection
# ---------------------------------------------------------------------------


def test_supplier_price_change_updates_ingredient_current_price(
    tenant_with_data,
):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "old_price": 1.0,
            "new_price": 1.75,
        },
    )

    session = get_session()
    try:
        ingredient = _get_one(
            session,
            Ingredient,
            tenant_with_data["flour_id"],
        )

        assert float(ingredient.current_price) == 1.75
    finally:
        session.close()

from decimal import Decimal

def test_supplier_price_change_creates_supplier_price_history(
    tenant_with_data,
):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "old_price": 1.0,
            "new_price": 1.75,
        },
    )

    session = get_session()
    try:
        prices = (
            session.query(SupplierPrice)
            .filter(
                SupplierPrice.tenant_id == tenant_with_data["tenant_id"],
                SupplierPrice.supplier_sku_id
                == tenant_with_data["flour_sku_id"],
            )
            .all()
        )

        assert len(prices) == 1
        assert prices[0].price == Decimal("1.75")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Inventory projection
# ---------------------------------------------------------------------------


def test_stockout_zeroes_ingredient_stock(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": tenant_with_data["cheese_id"],
        },
    )

    session = get_session()
    try:
        ingredient = _get_one(
            session,
            Ingredient,
            tenant_with_data["cheese_id"],
        )

        assert float(ingredient.current_stock_level) == 0.0
    finally:
        session.close()


def test_stockout_creates_inventory_projection(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": tenant_with_data["cheese_id"],
        },
    )

    session = get_session()
    try:
        inventory = (
            session.query(InventoryLevel)
            .filter(
                InventoryLevel.tenant_id == tenant_with_data["tenant_id"],
                InventoryLevel.ingredient_id
                == tenant_with_data["cheese_id"],
            )
            .one()
        )

        assert float(inventory.quantity) == 0.0
    finally:
        session.close()


def test_restock_sets_new_stock_level(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["flour_id"]

    append_event(
        tenant_id,
        EventType.STOCKOUT,
        {"ingredient_id": ingredient_id},
    )

    append_event(
        tenant_id,
        EventType.RESTOCK,
        {
            "ingredient_id": ingredient_id,
            "new_stock_level": 40.0,
        },
    )

    session = get_session()
    try:
        ingredient = _get_one(session, Ingredient, ingredient_id)

        inventory = (
            session.query(InventoryLevel)
            .filter(
                InventoryLevel.tenant_id == tenant_id,
                InventoryLevel.ingredient_id == ingredient_id,
            )
            .one()
        )

        assert float(ingredient.current_stock_level) == 40.0
        assert float(inventory.quantity) == 40.0
    finally:
        session.close()


def test_inventory_adjustment_applies_delta(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["flour_id"]

    append_event(
        tenant_id,
        EventType.INVENTORY_ADJUSTMENT,
        {
            "ingredient_id": ingredient_id,
            "stock_delta": -5.0,
        },
    )

    session = get_session()
    try:
        ingredient = _get_one(session, Ingredient, ingredient_id)

        inventory = (
            session.query(InventoryLevel)
            .filter(
                InventoryLevel.tenant_id == tenant_id,
                InventoryLevel.ingredient_id == ingredient_id,
            )
            .one()
        )

        assert float(ingredient.current_stock_level) == 15.0
        assert float(inventory.quantity) == 15.0
    finally:
        session.close()


def test_inventory_adjustment_rejects_negative_result(tenant_with_data):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.INVENTORY_ADJUSTMENT,
            {
                "ingredient_id": tenant_with_data["flour_id"],
                "stock_delta": -100.0,
            },
        )

    # The failed event must not survive.
    events = get_tenant_events(tenant_with_data["tenant_id"])

    assert events == []


# ---------------------------------------------------------------------------
# Waste projection
# ---------------------------------------------------------------------------


def test_waste_recorded_creates_waste_record_and_reduces_stock(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["cheese_id"]

    append_event(
        tenant_id,
        EventType.WASTE_RECORDED,
        {
            "ingredient_id": ingredient_id,
            "quantity": 2.5,
            "reason": "Spoilage",
        },
    )

    session = get_session()
    try:
        waste = (
            session.query(WasteRecord)
            .filter(
                WasteRecord.tenant_id == tenant_id,
                WasteRecord.ingredient_id == ingredient_id,
            )
            .one()
        )

        ingredient = _get_one(session, Ingredient, ingredient_id)

        inventory = (
            session.query(InventoryLevel)
            .filter(
                InventoryLevel.tenant_id == tenant_id,
                InventoryLevel.ingredient_id == ingredient_id,
            )
            .one()
        )

        assert float(waste.quantity) == 2.5
        assert waste.reason == "Spoilage"
        assert float(ingredient.current_stock_level) == 7.5
        assert float(inventory.quantity) == 7.5
    finally:
        session.close()


def test_waste_cannot_make_stock_negative(tenant_with_data):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.WASTE_RECORDED,
            {
                "ingredient_id": tenant_with_data["cheese_id"],
                "quantity": 100.0,
                "reason": "Bad batch",
            },
        )

    session = get_session()
    try:
        ingredient = _get_one(
            session,
            Ingredient,
            tenant_with_data["cheese_id"],
        )

        assert float(ingredient.current_stock_level) == 10.0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Menu projections
# ---------------------------------------------------------------------------


def test_menu_price_change_updates_menu_item(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.MENU_PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 15.0,
        },
    )

    session = get_session()
    try:
        item = _get_one(
            session,
            MenuItem,
            tenant_with_data["pizza_id"],
        )

        assert float(item.current_price) == 15.0
    finally:
        session.close()


def test_menu_item_removed_sets_inactive(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.MENU_ITEM_REMOVED,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
        },
    )

    session = get_session()
    try:
        item = _get_one(
            session,
            MenuItem,
            tenant_with_data["pizza_id"],
        )

        assert item.active is False
    finally:
        session.close()


def test_menu_item_added_creates_menu_item(tenant_with_data):
    # The fixture intentionally does not contain this menu item.
    new_menu_item_id = "00000000-0000-0000-0000-000000000001"

    append_event(

        tenant_with_data["tenant_id"],
        EventType.MENU_ITEM_ADDED,
        {
            "menu_item_id": new_menu_item_id,
            "name": "Burger",
            "current_price": 14.0,
            "active": True,
        },
    )

    session = get_session()
    try:
        item = _get_one(session, MenuItem, new_menu_item_id)

        assert item is not None
        assert item.name == "Burger"
        assert float(item.current_price) == 14.0
        assert item.active is True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Supplier switching
# ---------------------------------------------------------------------------


def test_supplier_switch_updates_ingredient_supplier(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_SWITCH,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "new_supplier_id": tenant_with_data["new_supplier_id"],
        },
    )

    session = get_session()
    try:
        ingredient = _get_one(
            session,
            Ingredient,
            tenant_with_data["flour_id"],
        )

        assert ingredient.current_supplier_id == (
            tenant_with_data["new_supplier_id"]
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Informational events
# ---------------------------------------------------------------------------


def test_demand_spike_does_not_mutate_menu_state(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.DEMAND_SPIKE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "demand_multiplier": 2.0,
        },
    )

    session = get_session()
    try:
        item = _get_one(
            session,
            MenuItem,
            tenant_with_data["pizza_id"],
        )

        assert float(item.current_price) == 12.0
        assert item.active is True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Orders / demand projection
# ---------------------------------------------------------------------------


def test_order_recorded_creates_order_and_order_volume(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    order_id = "00000000-0000-0000-0000-000000000010"

    append_event(
        tenant_id,
        EventType.ORDER_RECORDED,
        {
            "order_id": order_id,
            "lines": [
                {
                    "menu_item_id": tenant_with_data["pizza_id"],
                    "quantity": 3,
                    "unit_price": 12.0,
                }
            ],
        },
    )

    session = get_session()
    try:
        order = _get_one(session, SalesOrder, order_id)

        assert order is not None

        line = (
            session.query(SalesOrderLine)
            .filter(
                SalesOrderLine.tenant_id == tenant_id,
                SalesOrderLine.order_id == order_id,
            )
            .one()
        )

        volume = (
            session.query(OrderVolume)
            .filter(
                OrderVolume.tenant_id == tenant_id,
                OrderVolume.menu_item_id
                == tenant_with_data["pizza_id"],
            )
            .one()
        )

        assert line.quantity == 3
        assert float(line.unit_price) == 12.0
        assert volume.quantity == 3
    finally:
        session.close()
        

# ---------------------------------------------------------------------------
# Staffing
# ---------------------------------------------------------------------------


def test_staffing_change_creates_staff_shift(tenant_with_data):
    shift_id = "00000000-0000-0000-0000-000000000020"

    append_event(
        tenant_with_data["tenant_id"],
        EventType.STAFFING_CHANGE,
        {
            "staff_shift_id": shift_id,
            "role": "server",
            "day_of_week": 5,
            "headcount": 4,
        },
    )

    session = get_session()
    try:
        shift = _get_one(session, StaffShift, shift_id)

        assert shift is not None
        assert shift.role == "server"
        assert shift.day_of_week == 5
        assert shift.headcount == 4
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Point-in-time reconstruction
# ---------------------------------------------------------------------------


def test_reconstruct_state_replays_events_in_order(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["flour_id"]

    append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "old_price": 1.0,
            "new_price": 1.2,
        },
    )

    append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "supplier_sku_id": tenant_with_data["flour_sku_id"],
            "old_price": 1.2,
            "new_price": 1.4,
        },
    )

    state = reconstruct_state_at(
        tenant_id,
        datetime.now(timezone.utc),
    )

    assert state["ingredients"][ingredient_id]["current_price"] == 1.4
    assert state["event_count_replayed"] == 2


def test_reconstruct_state_at_past_timestamp_excludes_later_events(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["flour_id"]

    cutoff = datetime.now(timezone.utc)

    append_event(
        tenant_id,
        EventType.STOCKOUT,
        {
            "ingredient_id": ingredient_id,
        },
    )

    state_before = reconstruct_state_at(
        tenant_id,
        cutoff,
    )

    assert state_before["event_count_replayed"] == 0
    assert ingredient_id not in state_before["ingredients"]

    state_after = reconstruct_state_at(
        tenant_id,
        datetime.now(timezone.utc),
    )

    assert state_after["event_count_replayed"] == 1
    assert (
        state_after["ingredients"][ingredient_id]["stock_level"]
        == 0.0
    )


def test_reconstruct_state_excludes_other_tenants_events(
    tenant_with_data,
    second_tenant_with_data,
):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": tenant_with_data["flour_id"],
        },
    )

    append_event(
        second_tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": second_tenant_with_data["rice_id"],
        },
    )

    state = reconstruct_state_at(
        tenant_with_data["tenant_id"],
        datetime.now(timezone.utc),
    )

    assert state["event_count_replayed"] == 1
    assert tenant_with_data["flour_id"] in state["ingredients"]
    assert second_tenant_with_data["rice_id"] not in state["ingredients"]


def test_current_state_projection_matches_reconstruction(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["cheese_id"]

    append_event(
        tenant_id,
        EventType.SUPPLIER_PRICE_CHANGE,
        {
            "supplier_sku_id": tenant_with_data["cheese_sku_id"],
            "old_price": 5.0,
            "new_price": 6.25,
        },
    )

    session = get_session()
    try:
        projected_price = _get_one(
            session,
            Ingredient,
            ingredient_id,
        ).current_price
    finally:
        session.close()

    reconstructed = reconstruct_state_at(
        tenant_id,
        datetime.now(timezone.utc),
    )

    replayed_price = reconstructed["ingredients"][ingredient_id]["current_price"]

    assert float(projected_price) == replayed_price == 6.25


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_redteam_cross_tenant_event_isolation(
    tenant_with_data,
    second_tenant_with_data,
):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "secret": "alpha-secret",
        },
    )

    append_event(
        second_tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {
            "ingredient_id": second_tenant_with_data["rice_id"],
            "secret": "beta-secret",
        },
    )

    alpha_events = get_tenant_events(
        tenant_with_data["tenant_id"],
    )

    beta_events = get_tenant_events(
        second_tenant_with_data["tenant_id"],
    )

    assert len(alpha_events) == 1
    assert len(beta_events) == 1

    assert "alpha-secret" in alpha_events[0].payload["secret"] == "alpha-secret"
    assert "beta-secret" not in alpha_events[0].payload.values()



@pytest.mark.parametrize(
    "invalid_tenant",
    ["", None, 12345, "not-a-uuid"],
)
def test_invalid_tenant_raises_isolation_error(invalid_tenant):
    with pytest.raises(TenantIsolationError):
        append_event(
            tenant_id=invalid_tenant,
            event_type=EventType.DEMAND_SPIKE,
            payload={},
        )

    with pytest.raises(TenantIsolationError):
        get_tenant_events(tenant_id=invalid_tenant)


def test_cross_tenant_ingredient_id_is_rejected(
    tenant_with_data,
    second_tenant_with_data,
):
    """
    A caller must not be able to append an event for tenant A that mutates
    an ingredient owned by tenant B.
    """
    with pytest.raises((TenantIsolationError, EventStoreError)):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.STOCKOUT,
            {
                "ingredient_id": second_tenant_with_data["rice_id"],
            },
        )

    events = get_tenant_events(tenant_with_data["tenant_id"])

    assert events == []


# ---------------------------------------------------------------------------
# Validation / fail-closed behavior
# ---------------------------------------------------------------------------


def test_unknown_event_type_is_rejected(tenant_with_data):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            "NOT_A_REAL_EVENT",
            {},
        )


@pytest.mark.parametrize(
    "payload",
    [None, [], "string", 123, True],
)
def test_payload_must_be_dict(tenant_with_data, payload):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.DEMAND_SPIKE,
            payload,
        )


def test_supplier_price_change_requires_ingredient_id(
    tenant_with_data,
):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.SUPPLIER_PRICE_CHANGE,
            {
                "new_price": 10.0,
            },
        )

    assert get_tenant_events(
        tenant_with_data["tenant_id"],
    ) == []


def test_supplier_price_change_rejects_negative_price(
    tenant_with_data,
):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.SUPPLIER_PRICE_CHANGE,
            {
                "ingredient_id": tenant_with_data["flour_id"],
                "new_price": -1.0,
            },
        )

    assert get_tenant_events(
        tenant_with_data["tenant_id"],
    ) == []


def test_restock_rejects_negative_stock(
    tenant_with_data,
):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.RESTOCK,
            {
                "ingredient_id": tenant_with_data["flour_id"],
                "new_stock_level": -5.0,
            },
        )

    assert get_tenant_events(
        tenant_with_data["tenant_id"],
    ) == []


def test_source_must_be_non_empty(tenant_with_data):
    with pytest.raises(EventStoreError):
        append_event(
            tenant_with_data["tenant_id"],
            EventType.DEMAND_SPIKE,
            {},
            source="   ",
        )

    assert get_tenant_events(
        tenant_with_data["tenant_id"],
    ) == []


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


def test_first_event_uses_genesis_hash(tenant_with_data):
    event = append_event(
        tenant_with_data["tenant_id"],
        EventType.DEMAND_SPIKE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "demand_multiplier": 2.0,
        },
    )

    assert event.previous_hash == "0" * 64
    assert len(event.hash) == 64


def test_hash_chain_links_sequential_events(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    e1 = append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 1},
    )

    e2 = append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 2},
    )

    assert e1.previous_hash == "0" * 64
    assert e2.previous_hash == e1.hash
    assert verify_chain_integrity(tenant_id) is True


def test_each_tenant_has_independent_hash_chain(
    tenant_with_data,
    second_tenant_with_data,
):
    alpha = append_event(
        tenant_with_data["tenant_id"],
        EventType.DEMAND_SPIKE,
        {"value": "alpha"},
    )

    beta = append_event(
        second_tenant_with_data["tenant_id"],
        EventType.DEMAND_SPIKE,
        {"value": "beta"},
    )

    assert alpha.previous_hash == "0" * 64
    assert beta.previous_hash == "0" * 64

    assert verify_chain_integrity(
        tenant_with_data["tenant_id"],
    )

    assert verify_chain_integrity(
        second_tenant_with_data["tenant_id"],
    )


def test_tampered_payload_is_detected(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 10},
    )

    append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 20},
    )

    with get_db_context(tenant_id=tenant_id) as session:
        event = (
            session.query(Event)
            .filter(Event.tenant_id == tenant_id)
            .order_by(Event.timestamp.asc(), Event.id.asc())
            .first()
        )

        event.payload = {"value": 999}

    with pytest.raises(HashChainCorruptedError):
        verify_chain_integrity(tenant_id)


def test_tampered_source_is_detected(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 10},
    )

    with get_db_context(tenant_id=tenant_id) as session:
        event = (
            session.query(Event)
            .filter(Event.tenant_id == tenant_id)
            .first()
        )

        event.source = "tampered"

    with pytest.raises(HashChainCorruptedError):
        verify_chain_integrity(tenant_id)


def test_tampered_previous_hash_is_detected(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 10},
    )

    append_event(
        tenant_id,
        EventType.DEMAND_SPIKE,
        {"value": 20},
    )

    with get_db_context(tenant_id=tenant_id) as session:
        event = (
            session.query(Event)
            .filter(Event.tenant_id == tenant_id)
            .order_by(Event.timestamp.asc(), Event.id.asc())
            .offset(1)
            .first()
        )

        event.previous_hash = "f" * 64

    with pytest.raises(HashChainCorruptedError):
        verify_chain_integrity(tenant_id)


# ---------------------------------------------------------------------------
# Transactional integrity
# ---------------------------------------------------------------------------


def test_failed_projection_does_not_persist_event(tenant_with_data):
    tenant_id = tenant_with_data["tenant_id"]

    with pytest.raises(EventStoreError):
        append_event(
            tenant_id,
            EventType.STOCKOUT,
            {
                "ingredient_id": "00000000-0000-0000-0000-000000000999",
            },
        )

    events = get_tenant_events(tenant_id)

    assert events == []


def test_successful_append_and_projection_are_atomic(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]
    ingredient_id = tenant_with_data["flour_id"]

    append_event(
        tenant_id,
        EventType.RESTOCK,
        {
            "ingredient_id": ingredient_id,
            "new_stock_level": 50.0,
        },
    )

    events = get_tenant_events(tenant_id)

    assert len(events) == 1

    session = get_session()
    try:
        ingredient = _get_one(
            session,
            Ingredient,
            ingredient_id,
        )

        assert float(ingredient.current_stock_level) == 50.0
    finally:
        session.close()