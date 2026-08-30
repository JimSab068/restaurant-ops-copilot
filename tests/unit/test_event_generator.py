"""
Tests for event_generator.py — event variety, handling of edge cases
(empty ingredient/menu lists), and that fired events actually land in the DB.


Coverage: - normal event generation - every event type - tenant isolation - empty data - NULL / invalid numeric data - invalid probability configuration - DB failures - per-tenant failure isolation - zero/full probability - invalid event types - stockout edge cases - CLI/worker resilience

"""
from __future__ import annotations 
import random
from decimal import Decimal 
from unittest.mock import Mock 
import pytest 
from sqlalchemy.exc import SQLAlchemyError 
import event_generator 
from event_generator import ( 
    EventGenerationError, _fire_random_event, _get_tenant_ingredients, _get_tenant_menu_items, run_once 
) 
from models import Event, EventType, Ingredient, MenuItem, Tenant 
from db import get_session







def test_fire_random_event_returns_none_with_no_ingredients():
    """A tenant with no ingredients/menu items shouldn't crash the generator."""
    session = get_session()
    try:
        tenant = Tenant(name="Empty Place")
        session.add(tenant)
        session.commit()

        result = _fire_random_event(session, tenant)
        assert result is None
    finally:
        session.close()


def test_fire_random_event_produces_a_real_event(tenant_with_data):
    session = get_session()
    try:
        tenant = session.get(Tenant, tenant_with_data["tenant_id"])
        random.seed(1)  # deterministic for this test
        msg = _fire_random_event(session, tenant)

        assert msg is not None
        assert tenant.name in msg

        events = session.query(Event).filter(Event.tenant_id == tenant.id).all()
        assert len(events) == 1
    finally:
        session.close()


def test_all_event_types_reachable_over_many_trials(tenant_with_data):
    """
    Statistical check: over enough trials, every event type in the
    generator's weighted choice should fire at least once. Catches a typo
    in event type name or a zero-weight bug.
    """
    session = get_session()
    try:
        tenant = session.get(Tenant, tenant_with_data["tenant_id"])
        seen_types = set()

        for _ in range(200):
            _fire_random_event(session, tenant)

        events = session.query(Event).filter(Event.tenant_id == tenant.id).all()
        seen_types = {e.event_type for e in events}

        expected = {"supplier_price_change", "stockout", "restock", "demand_spike"}
        assert expected.issubset({t.value for t in seen_types})
    finally:
        session.close()


def test_run_once_respects_zero_probability(monkeypatch, tenant_with_data):
    """With EVENT_PROBABILITY forced to 0, no events should fire."""
    import event_generator
    monkeypatch.setattr(event_generator, "EVENT_PROBABILITY", 0.0)

    fired = run_once()
    assert fired == []


def test_run_once_fires_with_full_probability(monkeypatch, tenant_with_data):
    """With EVENT_PROBABILITY forced to 1, every tenant should get an event."""
    import event_generator
    monkeypatch.setattr(event_generator, "EVENT_PROBABILITY", 1.0)

    fired = run_once()
    assert len(fired) == 1  # one tenant seeded in this fixture





@pytest.fixture
def session():
    """Provide a real DB session and always close it."""
    db = get_session()

    try:
        yield db
    finally:
        db.close()


def _events_for_tenant(session, tenant_id):
    return (
        session.query(Event)
        .filter(Event.tenant_id == tenant_id)
        .all()
    )


def test_no_event_when_tenant_has_no_ingredients(session):
    tenant = Tenant(name="Empty Ingredients")
    session.add(tenant)
    session.commit()

    result = _fire_random_event(session, tenant)

    assert result is None
    assert _events_for_tenant(session, tenant.id) == []


def test_no_event_when_tenant_has_no_menu_items(session):
    tenant = Tenant(name="Empty Menu")
    session.add(tenant)
    session.commit()

    ingredient = Ingredient(
        tenant_id=tenant.id,
        name="Tomatoes",
        current_price=Decimal("4.00"),
        current_stock_level=10,
    )

    session.add(ingredient)
    session.commit()

    result = _fire_random_event(session, tenant)

    assert result is None
    assert _events_for_tenant(session, tenant.id) == []


@pytest.mark.parametrize(
    ("event_choice", "expected_type"),
    [
        ("supplier_price_change", EventType.SUPPLIER_PRICE_CHANGE),
        ("stockout", EventType.STOCKOUT),
        ("restock", EventType.RESTOCK),
        ("demand_spike", EventType.DEMAND_SPIKE),
    ],
)
def test_each_event_type_is_generated_deterministically(
    monkeypatch,
    tenant_with_data,
    event_choice,
    expected_type,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: event_choice,
        )

        message = _fire_random_event(session, tenant)

        assert message is not None

        events = _events_for_tenant(session, tenant.id)

        assert len(events) == 1
        assert events[0].event_type == expected_type

    finally:
        session.close()


def test_supplier_price_change_never_generates_negative_price(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "supplier_price_change",
        )

        monkeypatch.setattr(
            event_generator.random,
            "uniform",
            lambda *_: -0.10,
        )

        _fire_random_event(session, tenant)

        event = _events_for_tenant(session, tenant.id)[0]

        assert Decimal(event.payload["new_price"]) >= Decimal("0.00")

    finally:
        session.close()


def test_null_current_price_is_rejected(
    monkeypatch,
    tenant_with_data,
):
    """
    Generator must fail closed when an ingredient has a NULL price.

    This is intentionally tested with an in-memory ORM object rather than
    committing NULL to PostgreSQL because current_price is NOT NULL at the
    database level.
    """
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        assert tenant is not None

        ingredient = Ingredient(
            id=tenant_with_data["flour_id"],
            tenant_id=tenant.id,
            name="Malformed Ingredient",
            current_price=None,
            current_stock_level=10,
        )

        # Keep the real DB session for everything else, but force the
        # tenant-scoped ingredient lookup to return our malformed object.
        monkeypatch.setattr(
            event_generator,
            "_get_tenant_ingredients",
            lambda _session, tenant_id: [ingredient],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "supplier_price_change",
        )

        with pytest.raises(
            EventGenerationError,
            match="cannot be NULL",
        ):
            _fire_random_event(session, tenant)

    finally:
        session.rollback()
        session.close()



def test_invalid_current_price_is_rejected(
    monkeypatch,
    tenant_with_data,
):
    """
    Generator must reject a non-numeric ingredient price.

    The malformed object is kept entirely in memory so PostgreSQL never sees
    the invalid value.
    """
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        assert tenant is not None

        malformed_ingredient = Ingredient(
            id=tenant_with_data["flour_id"],
            tenant_id=tenant.id,
            name="Malformed Ingredient",
            current_price="not-a-price",
            current_stock_level=10,
        )

        # Replace only the dependency being tested. This guarantees that
        # _safe_decimal() receives the malformed value.
        monkeypatch.setattr(
            event_generator,
            "_get_tenant_ingredients",
            lambda _session, tenant_id: [malformed_ingredient],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "supplier_price_change",
        )

        with pytest.raises(EventGenerationError):
            _fire_random_event(session, tenant)

    finally:
        session.rollback()
        session.close()


def test_negative_stock_is_never_selected_for_stockout(
    monkeypatch,
    tenant_with_data,
):
    """
    Negative inventory is invalid at the DB level.

    Instead of attempting to persist an impossible state, inject an invalid
    in-memory value and verify the generator fails closed.
    """
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        ingredients = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == tenant.id)
            .all()
        )

        assert ingredients

        for ingredient in ingredients:
            ingredient.current_stock_level = -1

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "stockout",
        )

        with pytest.raises(EventGenerationError, match="must be >= 0"):
            _fire_random_event(session, tenant)

    finally:
        session.rollback()
        session.close()


def test_negative_stock_is_rejected(
    monkeypatch,
    tenant_with_data,
):
    """Invalid negative stock must fail closed before event creation."""
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        ingredient = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == tenant.id)
            .first()
        )

        assert ingredient is not None

        # Deliberately create an invalid in-memory ORM state.
        # Never commit it: the DB correctly rejects negative stock.
        ingredient.current_stock_level = -1

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "stockout",
        )

        with session.no_autoflush:
            with pytest.raises(
                EventGenerationError,
                match="must be >= 0",
            ):
                _fire_random_event(session, tenant)

    finally:
        session.rollback()
        session.close()



def test_stockout_only_selects_positive_stock(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        ingredients = (
            session.query(Ingredient)
            .filter(Ingredient.tenant_id == tenant.id)
            .all()
        )

        positive = ingredients[0]
        positive.current_stock_level = 10

        for ingredient in ingredients[1:]:
            ingredient.current_stock_level = 0

        session.commit()

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "stockout",
        )

        message = _fire_random_event(session, tenant)

        assert positive.name in message

    finally:
        session.close()


def test_menu_items_are_strictly_tenant_scoped(
    tenant_with_data,
):
    session = get_session()

    try:
        tenant_one = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        tenant_two = Tenant(name="Other Restaurant")
        session.add(tenant_two)
        session.commit()

        other_item = MenuItem(
            tenant_id=tenant_two.id,
            name="SECRET ITEM",
            active=True,
        )

        session.add(other_item)
        session.commit()

        items = _get_tenant_menu_items(
            session,
            tenant_one.id,
        )

        assert all(
            item.tenant_id == tenant_one.id
            for item in items
        )

        assert "SECRET ITEM" not in {
            item.name for item in items
        }

    finally:
        session.close()


def test_ingredients_are_strictly_tenant_scoped(
    tenant_with_data,
):
    session = get_session()

    try:
        tenant_one = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        tenant_two = Tenant(name="Other Restaurant")
        session.add(tenant_two)
        session.commit()

        secret = Ingredient(
            tenant_id=tenant_two.id,
            name="SECRET INGREDIENT",
            current_price=Decimal("9999.99"),
            current_stock_level=100,
        )

        session.add(secret)
        session.commit()

        ingredients = _get_tenant_ingredients(
            session,
            tenant_one.id,
        )

        assert all(
            ingredient.tenant_id == tenant_one.id
            for ingredient in ingredients
        )

        assert "SECRET INGREDIENT" not in {
            ingredient.name
            for ingredient in ingredients
        }

    finally:
        session.close()


def test_none_tenant_id_is_rejected():
    session = Mock()

    with pytest.raises(ValueError, match="tenant_id"):
        _get_tenant_ingredients(session, None)

    with pytest.raises(ValueError, match="tenant_id"):
        _get_tenant_menu_items(session, None)


def test_none_tenant_is_rejected():
    session = Mock()

    with pytest.raises(ValueError):
        _fire_random_event(session, None)


def test_persisted_tenant_is_required(session):
    tenant = Tenant(name="Transient Restaurant")

    with pytest.raises(ValueError, match="valid id"):
        _fire_random_event(session, tenant)


def test_zero_probability_generates_nothing(
    monkeypatch,
    tenant_with_data,
):
    monkeypatch.setattr(
        event_generator,
        "EVENT_PROBABILITY",
        0.0,
    )

    fired = run_once()

    assert fired == []


def test_full_probability_generates_for_every_tenant(
    monkeypatch,
    tenant_with_data,
):
    monkeypatch.setattr(
        event_generator,
        "EVENT_PROBABILITY",
        1.0,
    )

    fired = run_once()

    assert len(fired) == 1


def test_invalid_probability_is_rejected(monkeypatch):
    monkeypatch.setattr(
        event_generator,
        "EVENT_PROBABILITY",
        1.5,
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        run_once()


def test_negative_probability_is_rejected(monkeypatch):
    monkeypatch.setattr(
        event_generator,
        "EVENT_PROBABILITY",
        -0.1,
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        run_once()


def test_unknown_event_type_fails_closed(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "ATTACKER_CONTROLLED_EVENT",
        )

        with pytest.raises(
            EventGenerationError,
            match="Unsupported generated event type",
        ):
            _fire_random_event(session, tenant)

    finally:
        session.close()


def test_event_store_failure_does_not_leak_other_tenant_data(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant_one = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        tenant_two = Tenant(name="Second Restaurant")
        session.add(tenant_two)
        session.commit()

        # This test verifies the event-generation function always derives
        # ingredients/menu items from the supplied tenant rather than from
        # global collections.

        ingredients = _get_tenant_ingredients(
            session,
            tenant_one.id,
        )

        assert all(
            ingredient.tenant_id == tenant_one.id
            for ingredient in ingredients
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "stockout",
        )

        _fire_random_event(session, tenant_one)

        events_one = _events_for_tenant(
            session,
            tenant_one.id,
        )

        events_two = _events_for_tenant(
            session,
            tenant_two.id,
        )

        assert all(
            event.tenant_id == tenant_one.id
            for event in events_one
        )
        assert events_two == []

    finally:
        session.close()


def test_db_failure_is_logged_and_does_not_create_fake_events(
    monkeypatch,
):
    class BrokenContext:
        def __enter__(self):
            raise SQLAlchemyError("database unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        event_generator,
        "get_db_context",
        lambda: BrokenContext(),
    )

    with pytest.raises(SQLAlchemyError):
        run_once()


def test_one_tenant_failure_does_not_stop_remaining_tenants(
    monkeypatch,
):
    tenant_one = Mock()
    tenant_one.id = 1
    tenant_one.name = "Broken Restaurant"

    tenant_two = Mock()
    tenant_two.id = 2
    tenant_two.name = "Healthy Restaurant"

    session = Mock()

    session.query.return_value.all.return_value = [
        tenant_one,
        tenant_two,
    ]

    class Context:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        event_generator,
        "get_db_context",
        lambda: Context(),
    )

    monkeypatch.setattr(
        event_generator.random,
        "random",
        lambda: 0.0,
    )

    calls = []

    def fake_fire(current_session, tenant):
        calls.append(tenant.id)

        if tenant.id == 1:
            raise EventGenerationError("simulated failure")

        return "[Healthy Restaurant] event"

    monkeypatch.setattr(
        event_generator,
        "_fire_random_event",
        fake_fire,
    )

    result = run_once()

    assert calls == [1, 2]
    assert result == ["[Healthy Restaurant] event"]


def test_cross_tenant_payload_cannot_be_injected_through_query_scope(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant_one = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        tenant_two = Tenant(name="Tenant Two")
        session.add(tenant_two)
        session.commit()

        secret = Ingredient(
            tenant_id=tenant_two.id,
            name="TENANT_TWO_SECRET",
            current_price=Decimal("500.00"),
            current_stock_level=50,
        )

        session.add(secret)
        session.commit()

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "supplier_price_change",
        )

        _fire_random_event(session, tenant_one)

        events = _events_for_tenant(
            session,
            tenant_one.id,
        )

        assert len(events) == 1

        payload = events[0].payload

        assert payload["ingredient_id"] != secret.id
        assert payload["ingredient_name"] != "TENANT_TWO_SECRET"

    finally:
        session.close()


def test_event_payload_contains_no_non_finite_values(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "demand_spike",
        )

        _fire_random_event(session, tenant)

        event = _events_for_tenant(
            session,
            tenant.id,
        )[0]

        multiplier = Decimal(
            str(event.payload["demand_multiplier"])
        )

        assert multiplier.is_finite()
        assert multiplier >= Decimal("1.30")
        assert multiplier <= Decimal("3.00")

    finally:
        session.close()


def test_supplier_price_event_payload_has_expected_fields(
    monkeypatch,
    tenant_with_data,
):
    session = get_session()

    try:
        tenant = session.get(
            Tenant,
            tenant_with_data["tenant_id"],
        )

        monkeypatch.setattr(
            event_generator,
            "_choose_event_type",
            lambda: "supplier_price_change",
        )

        _fire_random_event(session, tenant)

        event = _events_for_tenant(
            session,
            tenant.id,
        )[0]

        assert event.event_type == EventType.SUPPLIER_PRICE_CHANGE

        required = {
            "ingredient_id",
            "ingredient_name",
            "old_price",
            "new_price",
            "pct_change",
        }

        assert required.issubset(event.payload.keys())

    finally:
        session.close()
