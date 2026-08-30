"""
Tests for event_store.py — the highest-stakes component, since the
simulation layer (Component 2) and audit trail both depend on this being
correct.
"""
from datetime import datetime, timedelta, timezone
import pytest
from db import get_session
from models import Ingredient, MenuItem, EventType, Event, Tenant
from event_store import get_db_context  # Adjust module import path as needed
from event_store import (
    EventStoreError,
    HashChainCorruptedError,
    TenantIsolationError,
    append_event,
    get_tenant_events,
    reconstruct_state_at,
    verify_chain_integrity,
)

@pytest.fixture
def db_session():
    session = get_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def tenant_alpha(db_session):
    tenant = Tenant(name="Alpha Bistro")
    db_session.add(tenant)
    db_session.commit()
    return tenant


@pytest.fixture
def tenant_beta(db_session):
    tenant = Tenant(name="Beta Diner")
    db_session.add(tenant)
    db_session.commit()
    return tenant


def test_append_event_creates_event_row(tenant_with_data):
    event = append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_PRICE_CHANGE,
        {"ingredient_id": tenant_with_data["flour_id"], "old_price": 1.0, "new_price": 1.3},
    )
    assert event.id is not None
    assert event.event_type == EventType.SUPPLIER_PRICE_CHANGE
    assert event.payload["new_price"] == 1.3


def test_supplier_price_change_updates_current_state(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_PRICE_CHANGE,
        {"ingredient_id": tenant_with_data["flour_id"], "old_price": 1.0, "new_price": 1.75},
    )
    session = get_session()
    try:
        ing = session.get(Ingredient, tenant_with_data["flour_id"])
        assert ing.current_price == 1.75
    finally:
        session.close()


def test_stockout_zeroes_stock_level(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"],
        EventType.STOCKOUT,
        {"ingredient_id": tenant_with_data["cheese_id"]},
    )
    session = get_session()
    try:
        ing = session.get(Ingredient, tenant_with_data["cheese_id"])
        assert ing.current_stock_level == 0.0
    finally:
        session.close()


def test_restock_sets_new_stock_level(tenant_with_data):
    # first stock out, then restock — verifies restock overrides, not adds
    append_event(
        tenant_with_data["tenant_id"], EventType.STOCKOUT,
        {"ingredient_id": tenant_with_data["flour_id"]},
    )
    append_event(
        tenant_with_data["tenant_id"], EventType.RESTOCK,
        {"ingredient_id": tenant_with_data["flour_id"], "new_stock_level": 40.0},
    )
    session = get_session()
    try:
        ing = session.get(Ingredient, tenant_with_data["flour_id"])
        assert ing.current_stock_level == 40.0
    finally:
        session.close()


def test_menu_price_change_updates_menu_item(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"], EventType.MENU_PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 15.0},
    )
    session = get_session()
    try:
        item = session.get(MenuItem, tenant_with_data["pizza_id"])
        assert item.current_price == 15.0
    finally:
        session.close()


def test_menu_item_removed_sets_inactive(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"], EventType.MENU_ITEM_REMOVED,
        {"menu_item_id": tenant_with_data["pizza_id"]},
    )
    session = get_session()
    try:
        item = session.get(MenuItem, tenant_with_data["pizza_id"])
        assert item.active is False
    finally:
        session.close()


def test_supplier_switch_updates_ingredient_supplier(tenant_with_data):
    new_supplier_id = tenant_with_data["new_supplier_id"]

    append_event(
        tenant_with_data["tenant_id"],
        EventType.SUPPLIER_SWITCH,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "new_supplier_id": new_supplier_id,
        },
    )

    session = get_session()
    try:
        ing = session.get(Ingredient, tenant_with_data["flour_id"])
        assert ing.current_supplier_id == new_supplier_id
    finally:
        session.close()

def test_demand_spike_does_not_mutate_current_state_tables(tenant_with_data):
    """
    DEMAND_SPIKE is informational-only for now (see event_store.py docstring) —
    confirm it doesn't accidentally touch price/stock fields.
    """
    append_event(
        tenant_with_data["tenant_id"], EventType.DEMAND_SPIKE,
        {"menu_item_id": tenant_with_data["pizza_id"], "demand_multiplier": 2.0},
    )
    session = get_session()
    try:
        item = session.get(MenuItem, tenant_with_data["pizza_id"])
        assert item.current_price == 12.0  # unchanged
        assert item.active is True
    finally:
        session.close()


# --- reconstruct_state_at ---

def test_reconstruct_state_replays_events_in_order(tenant_with_data):
    append_event(
        tenant_with_data["tenant_id"], EventType.SUPPLIER_PRICE_CHANGE,
        {"ingredient_id": tenant_with_data["flour_id"], "old_price": 1.0, "new_price": 1.2},
    )
    append_event(
        tenant_with_data["tenant_id"], EventType.SUPPLIER_PRICE_CHANGE,
        {"ingredient_id": tenant_with_data["flour_id"], "old_price": 1.2, "new_price": 1.4},
    )

    state = reconstruct_state_at(tenant_with_data["tenant_id"], datetime.now(timezone.utc))
    # later event should win — price should be the LAST one applied, not the first
    assert state["ingredients"][tenant_with_data["flour_id"]]["price"] == 1.4
    assert state["event_count_replayed"] == 2


def test_reconstruct_state_at_past_timestamp_excludes_later_events(tenant_with_data):
    """
    This is the test that actually proves the log is real history, not just
    an activity feed: querying a timestamp BEFORE an event should not
    reflect that event.
    """
    cutoff = datetime.now(timezone.utc)

    # event fired AFTER the cutoff
    append_event(
        tenant_with_data["tenant_id"], EventType.STOCKOUT,
        {"ingredient_id": tenant_with_data["flour_id"]},
    )

    state_before = reconstruct_state_at(tenant_with_data["tenant_id"], cutoff)
    assert state_before["event_count_replayed"] == 0
    assert tenant_with_data["flour_id"] not in state_before["ingredients"]

    state_after = reconstruct_state_at(tenant_with_data["tenant_id"], datetime.now(timezone.utc))
    assert state_after["event_count_replayed"] == 1
    assert state_after["ingredients"][tenant_with_data["flour_id"]]["stock_level"] == 0.0


def test_reconstruct_state_excludes_other_tenants_events(tenant_with_data, second_tenant_with_data):
    """Tenant isolation, at the event-log level specifically."""
    append_event(
        tenant_with_data["tenant_id"], EventType.STOCKOUT,
        {"ingredient_id": tenant_with_data["flour_id"]},
    )
    append_event(
        second_tenant_with_data["tenant_id"], EventType.STOCKOUT,
        {"ingredient_id": second_tenant_with_data["rice_id"]},
    )

    state_a = reconstruct_state_at(tenant_with_data["tenant_id"], datetime.now(timezone.utc))
    assert state_a["event_count_replayed"] == 1
    assert tenant_with_data["flour_id"] in state_a["ingredients"]
    assert second_tenant_with_data["rice_id"] not in state_a["ingredients"]


def test_current_state_projection_matches_reconstruction(tenant_with_data):
    """
    Cross-check: the fast-read projection table and the slow-replay
    reconstruction should always agree. If they diverge, the projection
    logic in _apply_to_current_state has a bug.
    """
    append_event(
        tenant_with_data["tenant_id"], EventType.SUPPLIER_PRICE_CHANGE,
        {"ingredient_id": tenant_with_data["cheese_id"], "old_price": 5.0, "new_price": 6.25},
    )

    session = get_session()
    try:
        projected_price = session.get(Ingredient, tenant_with_data["cheese_id"]).current_price
    finally:
        session.close()

    reconstructed = reconstruct_state_at(tenant_with_data["tenant_id"], datetime.now(timezone.utc))
    replayed_price = reconstructed["ingredients"][tenant_with_data["cheese_id"]]["price"]

    assert projected_price == replayed_price == 6.25



def test_append_event_success_and_retrieval(tenant_alpha):
    payload = {"ingredient": "Cheese", "delta": -2.5}
    event = append_event(
        tenant_id=tenant_alpha.id,
        event_type=EventType.SUPPLIER_PRICE_CHANGE,
        payload=payload,
    )

    assert event.id is not None
    assert event.tenant_id == tenant_alpha.id
    assert event.previous_hash == "0" * 64
    assert len(event.hash) == 64

    events = get_tenant_events(tenant_id=tenant_alpha.id)
    assert len(events) == 1
    assert events[0].hash == event.hash


def test_hash_chain_linking_sequential_events(tenant_alpha):
    e1 = append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"price": 1.5})
    e2 = append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"price": 1.75})

    assert e1.previous_hash == "0" * 64
    assert e2.previous_hash == e1.hash
    assert verify_chain_integrity(tenant_alpha.id) is True


def test_reconstruct_state_at_point_in_time(tenant_alpha):
    now = datetime.now(timezone.utc)
    
    append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"ingredient_id": "flour_1", "new_price": 2.0})
    
    # Target timestamp between event 1 and event 2
    cutoff = now + timedelta(seconds=1)
    
    state_before = reconstruct_state_at(tenant_alpha.id, cutoff)
    assert state_before["flour_1"]["current_price"] == 2.0


def test_redteam_cross_tenant_event_leak_prevention(tenant_alpha, tenant_beta):
    append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"secret_a": 100})
    append_event(tenant_beta.id, EventType.SUPPLIER_PRICE_CHANGE, {"secret_b": 200})

    alpha_events = get_tenant_events(tenant_alpha.id)
    beta_events = get_tenant_events(tenant_beta.id)

    assert len(alpha_events) == 1
    assert len(beta_events) == 1
    assert "secret_a" in alpha_events[0].payload
    assert "secret_b" not in alpha_events[0].payload


@pytest.mark.parametrize("invalid_tenant", ["", None, 12345])
def test_redteam_invalid_tenant_raises_isolation_error(invalid_tenant):
    with pytest.raises(TenantIsolationError):
        append_event(tenant_id=invalid_tenant, event_type="TEST", payload={})

    with pytest.raises(TenantIsolationError):
        get_tenant_events(tenant_id=invalid_tenant)


def test_redteam_detect_tampered_payload_in_chain(tenant_alpha):
    append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"price": 10.0})
    append_event(tenant_alpha.id, EventType.SUPPLIER_PRICE_CHANGE, {"price": 12.0})

    with get_db_context(tenant_id=tenant_alpha.id) as session:
        evt = session.query(Event).filter(Event.tenant_id == tenant_alpha.id).first()
        evt.payload = {"price": 0.0}
        session.commit()

    with pytest.raises(HashChainCorruptedError, match="Payload/Hash mismatch"):
        verify_chain_integrity(tenant_alpha.id)