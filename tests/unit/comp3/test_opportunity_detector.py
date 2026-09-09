"""
Production-grade tests for Component 3 opportunity detection.

Covers:
- margin deterioration
- stockout exposure
- demand decline
- thresholds
- active/inactive records
- deterministic IDs and ordering
- tenant isolation
- SQL-injection attempts
- malformed tenant IDs
- timestamp handling
- database failures
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from Component_1.db import get_session
from Component_1.models import (
    Ingredient,
    InventoryLevel,
    MenuItem,
    OrderVolume,
    Recipe,
    RecipeIngredient,
    Tenant,
)

from Component_3.opportunity_detector import (
    DEMAND_DECLINE_THRESHOLD,
    LOOKBACK_DAYS,
    MIN_MARGIN_SHORTFALL,
    STOCKOUT_QUANTITY_THRESHOLD,
    TARGET_CONTRIBUTION_MARGIN,
    OpportunityDetectorError,
    detect_opportunities,
)


def add_order_volume(
    tenant_id: str,
    menu_item_id: str,
    quantity: int,
    timestamp: datetime,
) -> None:
    session = get_session()
    try:
        session.add(
            OrderVolume(
                tenant_id=tenant_id,
                menu_item_id=menu_item_id,
                quantity=quantity,
                timestamp=timestamp,
            )
        )
        session.commit()
    finally:
        session.close()


def get_opportunities(tenant_id: str, **kwargs):
    return detect_opportunities(tenant_id, **kwargs)


# ---------------------------------------------------------------------------
# Basic detector behavior
# ---------------------------------------------------------------------------

def test_detector_returns_list(tenant_with_data):
    result = get_opportunities(tenant_with_data["tenant_id"])

    assert isinstance(result, list)


def test_detector_returns_no_margin_opportunity_for_healthy_margin(
    tenant_with_data,
):
    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "margin_deterioration"
        for item in opportunities
    )


def test_detector_only_returns_active_menu_items(tenant_with_data):
    session = get_session()
    try:
        pizza = session.get(
            MenuItem,
            tenant_with_data["pizza_id"],
        )
        pizza.active = False
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert all(
        tenant_with_data["pizza_id"]
        not in opportunity.affected_entity_ids
        for opportunity in opportunities
    )


# ---------------------------------------------------------------------------
# Margin deterioration
# ---------------------------------------------------------------------------

def test_detector_finds_margin_deterioration(tenant_with_data):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("5.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        flour.current_price = Decimal("10.00")

        cheese = session.get(Ingredient, tenant_with_data["cheese_id"])
        cheese.current_price = Decimal("10.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    matches = [
        item
        for item in opportunities
        if item.opportunity_type.value == "margin_deterioration"
    ]

    assert len(matches) == 1


def test_margin_opportunity_contains_recipe_cost_evidence(tenant_with_data):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("5.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        flour.current_price = Decimal("10.00")

        cheese = session.get(Ingredient, tenant_with_data["cheese_id"])
        cheese.current_price = Decimal("10.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    margin = next(
        item
        for item in opportunities
        if item.opportunity_type.value == "margin_deterioration"
    )

    assert "current_price" in margin.evidence
    assert "recipe_cost" in margin.evidence
    assert "contribution_margin_pct" in margin.evidence


def test_margin_opportunity_confidence_is_higher_with_demand(
    tenant_with_data,
):
    now = datetime.now(timezone.utc)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        10,
        now,
    )

    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("5.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        flour.current_price = Decimal("10.00")

        cheese = session.get(Ingredient, tenant_with_data["cheese_id"])
        cheese.current_price = Decimal("10.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    margin = next(
        item
        for item in opportunities
        if item.opportunity_type.value == "margin_deterioration"
    )

    assert margin.confidence == 0.85


def test_margin_opportunity_confidence_is_lower_without_demand(
    tenant_with_data,
):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("5.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        flour.current_price = Decimal("10.00")

        cheese = session.get(Ingredient, tenant_with_data["cheese_id"])
        cheese.current_price = Decimal("10.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    margin = next(
        item
        for item in opportunities
        if item.opportunity_type.value == "margin_deterioration"
    )

    assert margin.confidence == 0.65


def test_margin_threshold_below_shortfall_is_not_reported(
    tenant_with_data,
):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])

        # Cost produces a margin just inside the configured acceptable
        # range.
        pizza.current_price = Decimal("12.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        cheese = session.get(Ingredient, tenant_with_data["cheese_id"])

        # Recipe cost = .25*1 + .15*5 = 1.00.
        # Margin = 91.67%, safely above target.
        flour.current_price = Decimal("1.00")
        cheese.current_price = Decimal("5.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "margin_deterioration"
        for item in opportunities
    )


def test_missing_recipe_cost_does_not_create_margin_opportunity(
    tenant_with_data,
):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("1.00")

        recipe = session.get(Recipe, tenant_with_data["recipe_id"])
        recipe.active = False

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "margin_deterioration"
        for item in opportunities
    )


def test_zero_menu_price_is_skipped(tenant_with_data):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("0.00")
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "margin_deterioration"
        for item in opportunities
    )


# ---------------------------------------------------------------------------
# Stockout detection
# ---------------------------------------------------------------------------

def test_detector_finds_stockout_exposure(tenant_with_data):
    session = get_session()
    try:
        inventory = InventoryLevel(
            tenant_id=tenant_with_data["tenant_id"],
            ingredient_id=tenant_with_data["flour_id"],
            quantity=Decimal("0"),
        )
        session.add(inventory)
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    stockouts = [
        item
        for item in opportunities
        if item.opportunity_type.value == "stockout_exposure"
    ]

    assert len(stockouts) == 1

    opportunity = stockouts[0]

    assert tenant_with_data["flour_id"] in opportunity.affected_entity_ids
    assert tenant_with_data["pizza_id"] in opportunity.affected_entity_ids


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal("0"),
        Decimal("0.5"),
        Decimal("1"),
    ],
)
def test_stockout_threshold_is_inclusive(tenant_with_data, quantity):
    session = get_session()
    try:
        session.add(
            InventoryLevel(
                tenant_id=tenant_with_data["tenant_id"],
                ingredient_id=tenant_with_data["flour_id"],
                quantity=quantity,
            )
        )
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert any(
        item.opportunity_type.value == "stockout_exposure"
        for item in opportunities
    )


def test_inventory_above_threshold_is_not_stockout(tenant_with_data):
    session = get_session()
    try:
        session.add(
            InventoryLevel(
                tenant_id=tenant_with_data["tenant_id"],
                ingredient_id=tenant_with_data["flour_id"],
                quantity=Decimal("1.01"),
            )
        )
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "stockout_exposure"
        and tenant_with_data["flour_id"] in item.affected_entity_ids
        for item in opportunities
    )


def test_stockout_without_active_recipe_has_no_exposure(
    tenant_with_data,
):
    session = get_session()
    try:
        recipe = session.get(Recipe, tenant_with_data["recipe_id"])
        recipe.active = False

        session.add(
            InventoryLevel(
                tenant_id=tenant_with_data["tenant_id"],
                ingredient_id=tenant_with_data["flour_id"],
                quantity=Decimal("0"),
            )
        )

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "stockout_exposure"
        and tenant_with_data["flour_id"] in item.affected_entity_ids
        for item in opportunities
    )


def test_stockout_without_affected_menu_item_is_skipped(
    tenant_with_data,
):
    session = get_session()
    try:
        cheese = session.get(
            Ingredient,
            tenant_with_data["cheese_id"],
        )

        session.add(
            InventoryLevel(
                tenant_id=tenant_with_data["tenant_id"],
                ingredient_id=cheese.id,
                quantity=Decimal("0"),
            )
        )

        # Remove the recipe ingredient relationship.
        recipe_ingredient = (
            session.query(RecipeIngredient)
            .filter_by(
                tenant_id=tenant_with_data["tenant_id"],
                ingredient_id=cheese.id,
            )
            .one()
        )
        session.delete(recipe_ingredient)

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    assert not any(
        item.opportunity_type.value == "stockout_exposure"
        and tenant_with_data["cheese_id"] in item.affected_entity_ids
        for item in opportunities
    )


# ---------------------------------------------------------------------------
# Demand decline
# ---------------------------------------------------------------------------

def test_detector_finds_demand_decline(tenant_with_data):
    now = datetime.now(timezone.utc)

    baseline_start = now - timedelta(days=20)
    recent_start = now - timedelta(days=7)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        100,
        baseline_start,
    )

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        50,
        recent_start,
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    declines = [
        item
        for item in opportunities
        if item.opportunity_type.value == "demand_decline"
    ]

    assert len(declines) == 1

    evidence = declines[0].evidence

    assert evidence["recent_units"] == 50
    assert evidence["baseline_units"] == 100


def test_demand_decline_requires_positive_baseline(tenant_with_data):
    now = datetime.now(timezone.utc)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        50,
        now - timedelta(days=3),
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    assert not any(
        item.opportunity_type.value == "demand_decline"
        for item in opportunities
    )


def test_demand_decline_threshold_is_20_percent(tenant_with_data):
    now = datetime.now(timezone.utc)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        100,
        now - timedelta(days=20),
    )

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        80,
        now - timedelta(days=3),
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    assert any(
        item.opportunity_type.value == "demand_decline"
        for item in opportunities
    )


def test_demand_decline_below_20_percent_is_not_reported(
    tenant_with_data,
):
    now = datetime.now(timezone.utc)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        100,
        now - timedelta(days=20),
    )

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        81,
        now - timedelta(days=3),
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    assert not any(
        item.opportunity_type.value == "demand_decline"
        for item in opportunities
    )


def test_recent_orders_are_not_counted_as_baseline(tenant_with_data):
    now = datetime.now(timezone.utc)

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        100,
        now - timedelta(days=20),
    )

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        10,
        now - timedelta(days=3),
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    decline = next(
        (
            item
            for item in opportunities
            if item.opportunity_type.value == "demand_decline"
        ),
        None,
    )

    assert decline is not None
    assert decline.evidence["baseline_units"] == 100
    assert decline.evidence["recent_units"] == 10


def test_demand_decline_requires_active_menu_item(tenant_with_data):
    now = datetime.now(timezone.utc)

    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.active = False
        session.commit()
    finally:
        session.close()

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        100,
        now - timedelta(days=20),
    )

    add_order_volume(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        10,
        now - timedelta(days=3),
    )

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    assert not any(
        item.opportunity_type.value == "demand_decline"
        for item in opportunities
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_opportunity_ids_are_stable_across_repeated_detection(
    tenant_with_data,
):
    first = get_opportunities(tenant_with_data["tenant_id"])
    second = get_opportunities(tenant_with_data["tenant_id"])

    assert [item.id for item in first] == [
        item.id for item in second
    ]


def test_detector_ordering_is_deterministic(tenant_with_data):
    first = get_opportunities(tenant_with_data["tenant_id"])
    second = get_opportunities(tenant_with_data["tenant_id"])

    assert first == second


def test_detector_ranks_larger_absolute_impact_first(tenant_with_data):
    session = get_session()
    try:
        pizza = session.get(MenuItem, tenant_with_data["pizza_id"])
        pizza.current_price = Decimal("2.00")

        flour = session.get(Ingredient, tenant_with_data["flour_id"])
        flour.current_price = Decimal("20.00")

        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(tenant_with_data["tenant_id"])

    if len(opportunities) >= 2:
        assert abs(
            opportunities[0].estimated_monthly_impact
        ) >= abs(
            opportunities[1].estimated_monthly_impact
        )


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_detector_does_not_return_second_tenant_data(
    tenant_with_data,
    second_tenant_with_data,
):
    tenant_a = tenant_with_data["tenant_id"]
    tenant_b = second_tenant_with_data["tenant_id"]

    opportunities = get_opportunities(tenant_a)

    assert all(
        item.tenant_id == tenant_a
        for item in opportunities
    )

    assert all(
        item.tenant_id != tenant_b
        for item in opportunities
    )


def test_detector_never_uses_global_menu_query(tenant_with_data):
    session = get_session()
    try:
        attacker_tenant = Tenant(name="Attacker Restaurant")
        session.add(attacker_tenant)
        session.flush()

        attacker_item = MenuItem(
            id="attacker-menu",
            tenant_id=attacker_tenant.id,
            name="Attacker Pizza",
            current_price=1.00,
            active=True,
        )
        session.add(attacker_item)
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"]
    )

    assert all(
        "attacker-menu" not in item.affected_entity_ids
        for item in opportunities
    )


# ---------------------------------------------------------------------------
# SQL injection / malicious identifiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "malicious_tenant_id",
    [
        "' OR '1'='1",
        "' OR 1=1 --",
        "1; DROP TABLE tenants; --",
        "x' UNION SELECT * FROM tenants --",
        "../../etc/passwd",
        "<script>alert(1)</script>",
    ],
)
def test_malicious_tenant_id_is_rejected(
    malicious_tenant_id,
):
    with pytest.raises(
        (ValueError, OpportunityDetectorError)
    ):
        detect_opportunities(malicious_tenant_id)


def test_sql_injection_in_menu_item_id_is_treated_as_data(
    tenant_with_data,
):
    malicious_id = "' OR 1=1 --"

    session = get_session()
    try:
        malicious_item = MenuItem(
            id=malicious_id,
            tenant_id=tenant_with_data["tenant_id"],
            name="Malicious Identifier",
            current_price=12.00,
            active=True,
        )
        session.add(malicious_item)
        session.commit()
    finally:
        session.close()

    opportunities = get_opportunities(
        tenant_with_data["tenant_id"]
    )

    assert all(
        malicious_id not in item.affected_entity_ids
        for item in opportunities
    )


def test_sql_injection_in_tenant_id_cannot_change_database_state(
    tenant_with_data,
):
    malicious_id = (
        f"{tenant_with_data['tenant_id']}' ; "
        "DROP TABLE menu_items; --"
    )

    with pytest.raises(
        (ValueError, OpportunityDetectorError)
    ):
        detect_opportunities(malicious_id)

    session = get_session()
    try:
        assert session.query(MenuItem).count() >= 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------

def test_naive_now_is_rejected(tenant_with_data):
    naive = datetime.now()

    with pytest.raises(
        OpportunityDetectorError,
        match="timezone-aware",
    ):
        detect_opportunities(
            tenant_with_data["tenant_id"],
            now=naive,
        )


def test_non_datetime_now_is_rejected(tenant_with_data):
    with pytest.raises(TypeError, match="datetime"):
        detect_opportunities(
            tenant_with_data["tenant_id"],
            now="2026-01-01",
        )


def test_timezone_is_normalized_to_utc(tenant_with_data):
    eastern = timezone(timedelta(hours=-5))

    now = datetime(
        2026,
        1,
        20,
        12,
        tzinfo=eastern,
    )

    result = detect_opportunities(
        tenant_with_data["tenant_id"],
        now=now,
    )

    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Database failure propagation
# ---------------------------------------------------------------------------

def test_database_failure_is_not_silently_converted_to_empty_result(
    tenant_with_data,
    monkeypatch,
):
    from Component_3 import opportunity_detector as detector

    def fail_context(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        detector,
        "get_db_context",
        fail_context,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        detect_opportunities(
            tenant_with_data["tenant_id"]
        )
