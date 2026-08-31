"""
Tests for simulator.py — each action type's prediction logic.

These tests check the *mechanics* (does a price increase predict lower
demand, does an unproven supplier get capped confidence) rather than
asserting exact numeric outputs, since the underlying model is an
explicitly-stated heuristic, not a fitted model with a single correct
answer. See simulator.py's module docstring for why.
These tests validate:

    - directional prediction mechanics
    - confidence behavior
    - input validation
    - numeric safety
    - tenant isolation
    - action dispatch
    - database failure propagation
"""
import pytest
from Component_1.db import get_session
from decimal import Decimal
from unittest.mock import patch
from Component_1.models import Ingredient, MenuItem, ActionType
from Component_2.simulator import (
    SimulatorInputError,
    PredictedOutcome,
    simulate,
    simulate_menu_swap,
    simulate_price_change,
    simulate_staffing_change,
    simulate_supplier_switch,
)


def test_price_increase_predicts_demand_decrease(tenant_with_data):
    result = simulate_price_change(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], 15.0)  # up from 12.0
    assert result.demand_change_pct < 0
    assert result.confidence <= 1.0 and result.confidence >= 0.0


def test_price_decrease_predicts_demand_increase(tenant_with_data):
    result = simulate_price_change(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], 9.0)  # down from 12.0
    assert result.demand_change_pct > 0


def test_larger_price_swing_has_lower_confidence(tenant_with_data):
    small_change = simulate_price_change(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], 12.5)
    big_change = simulate_price_change(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], 20.0)
    assert big_change.confidence < small_change.confidence


def test_price_change_raises_on_wrong_tenant(tenant_with_data, second_tenant_with_data):
    with pytest.raises(ValueError):
        simulate_price_change(second_tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], 15.0)


def test_supplier_switch_cheaper_improves_margin(tenant_with_data):
    result = simulate_supplier_switch(tenant_with_data["tenant_id"], tenant_with_data["flour_id"], 0.8)  # down from 1.0
    assert result.margin_change_pct > 0


def test_supplier_switch_more_expensive_hurts_margin(tenant_with_data):
    result = simulate_supplier_switch(tenant_with_data["tenant_id"], tenant_with_data["flour_id"], 1.5)  # up from 1.0
    assert result.margin_change_pct < 0


def test_supplier_switch_confidence_is_capped_low(tenant_with_data):
    """An unproven supplier should never get high confidence — that's the point."""
    result = simulate_supplier_switch(tenant_with_data["tenant_id"], tenant_with_data["flour_id"], 0.9)
    assert result.confidence <= 0.6


def test_supplier_switch_raises_stockout_risk_above_baseline(tenant_with_data):
    result = simulate_supplier_switch(tenant_with_data["tenant_id"], tenant_with_data["flour_id"], 1.0)
    assert result.stockout_risk > 0.10  # baseline plus the unproven-supplier delta


def test_menu_removal_predicts_net_demand_loss(tenant_with_data):
    result = simulate_menu_swap(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], "remove")
    assert result.demand_change_pct < 0


def test_menu_removal_confidence_low_since_margin_unmodeled(tenant_with_data):
    result = simulate_menu_swap(tenant_with_data["tenant_id"], tenant_with_data["pizza_id"], "remove")
    assert result.margin_change_pct == 0.0  # explicitly unmodeled, not guessed
    assert result.confidence < 0.5


def test_staffing_increase_raises_labor_cost_prediction():
    result = simulate_staffing_change("dummy-tenant", 2)
    assert result.margin_change_pct < 0  # more staff assumed to cost more


def test_staffing_decrease_raises_stockout_risk_proxy():
    """A headcount cut should raise the risk proxy (capacity strain), a headcount
    increase should lower it — verifies the direction of the effect, not exact value."""
    cut = simulate_staffing_change("dummy-tenant", -2)
    add = simulate_staffing_change("dummy-tenant", 2)
    assert cut.stockout_risk > add.stockout_risk


def test_dispatch_routes_to_correct_simulator(tenant_with_data):
    result = simulate(
        tenant_with_data["tenant_id"],
        ActionType.PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 14.0},
    )
    assert result.demand_change_pct < 0  # price went up from 12.0 -> 14.0


def test_dispatch_rejects_non_action_type(tenant_with_data):
    with pytest.raises(TypeError, match="action_type must be an ActionType"):
        simulate(
            tenant_with_data["tenant_id"],
            "not_a_real_action_type",
            {},
        )



# ---------------------------------------------------------------------------
# Price changes
# ---------------------------------------------------------------------------

def test_price_increase_predicts_demand_decrease(tenant_with_data):
    result = simulate_price_change(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        15.0,
    )

    assert result.demand_change_pct < 0
    assert 0.0 <= result.confidence <= 1.0
    assert 0.0 <= result.stockout_risk <= 1.0


def test_price_decrease_predicts_demand_increase(tenant_with_data):
    result = simulate_price_change(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        9.0,
    )

    assert result.demand_change_pct > 0


def test_larger_price_swing_has_lower_confidence(
    tenant_with_data,
):
    small_change = simulate_price_change(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        12.5,
    )

    big_change = simulate_price_change(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        20.0,
    )

    assert big_change.confidence < small_change.confidence


def test_price_change_rejects_cross_tenant_access(
    tenant_with_data,
    second_tenant_with_data,
):
    with pytest.raises(
        SimulatorInputError,
        match="not found for this tenant",
    ):
        simulate_price_change(
            second_tenant_with_data["tenant_id"],
            tenant_with_data["pizza_id"],
            15.0,
        )


@pytest.mark.parametrize(
    "bad_price",
    [
        None,
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "not-a-number",
    ],
)
def test_price_change_rejects_invalid_price(
    tenant_with_data,
    bad_price,
):
    with pytest.raises(SimulatorInputError):
        simulate_price_change(
            tenant_with_data["tenant_id"],
            tenant_with_data["pizza_id"],
            bad_price,
        )

from Component_1.models import MenuItem

def test_price_change_rejects_zero_current_price(
    tenant_with_data,
):
    session = get_session()

    try:
        item = session.get(
            MenuItem,
            tenant_with_data["pizza_id"],
        )

        item.current_price = Decimal("0")
        session.commit()
    finally:
        session.close()

    with pytest.raises(
        SimulatorInputError,
        match="current price",
    ):
        simulate_price_change(
            tenant_with_data["tenant_id"],
            tenant_with_data["pizza_id"],
            15.0,
        )


# ---------------------------------------------------------------------------
# Supplier changes
# ---------------------------------------------------------------------------

def test_supplier_switch_cheaper_improves_margin(
    tenant_with_data,
):
    result = simulate_supplier_switch(
        tenant_with_data["tenant_id"],
        tenant_with_data["flour_id"],
        0.8,
    )

    assert result.margin_change_pct > 0


def test_supplier_switch_more_expensive_hurts_margin(
    tenant_with_data,
):
    result = simulate_supplier_switch(
        tenant_with_data["tenant_id"],
        tenant_with_data["flour_id"],
        1.5,
    )

    assert result.margin_change_pct < 0


def test_supplier_switch_confidence_is_capped_low(
    tenant_with_data,
):
    result = simulate_supplier_switch(
        tenant_with_data["tenant_id"],
        tenant_with_data["flour_id"],
        0.9,
    )

    assert result.confidence <= 0.6


def test_supplier_switch_raises_stockout_risk_above_baseline(
    tenant_with_data,
):
    result = simulate_supplier_switch(
        tenant_with_data["tenant_id"],
        tenant_with_data["flour_id"],
        1.0,
    )

    assert result.stockout_risk > 0.10


def test_supplier_switch_rejects_cross_tenant_access(
    tenant_with_data,
    second_tenant_with_data,
):
    with pytest.raises(
        SimulatorInputError,
        match="not found for this tenant",
    ):
        simulate_supplier_switch(
            second_tenant_with_data["tenant_id"],
            tenant_with_data["flour_id"],
            0.8,
        )


@pytest.mark.parametrize(
    "bad_price",
    [
        None,
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "invalid",
    ],
)
def test_supplier_switch_rejects_invalid_price(
    tenant_with_data,
    bad_price,
):
    with pytest.raises(SimulatorInputError):
        simulate_supplier_switch(
            tenant_with_data["tenant_id"],
            tenant_with_data["flour_id"],
            bad_price,
        )


def test_supplier_switch_handles_decimal_input(
    tenant_with_data,
):
    result = simulate_supplier_switch(
        tenant_with_data["tenant_id"],
        tenant_with_data["flour_id"],
        Decimal("0.80"),
    )

    assert result.margin_change_pct > 0


# ---------------------------------------------------------------------------
# Menu changes
# ---------------------------------------------------------------------------

def test_menu_removal_predicts_net_demand_loss(
    tenant_with_data,
):
    result = simulate_menu_swap(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        "remove",
    )

    assert result.demand_change_pct < 0


def test_menu_removal_confidence_is_low(
    tenant_with_data,
):
    result = simulate_menu_swap(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        "remove",
    )

    assert result.margin_change_pct == 0.0
    assert result.confidence < 0.5


def test_menu_add_has_low_confidence(
    tenant_with_data,
):
    result = simulate_menu_swap(
        tenant_with_data["tenant_id"],
        tenant_with_data["pizza_id"],
        "add",
    )

    assert result.confidence < 0.5


@pytest.mark.parametrize(
    "bad_action",
    [
        "",
        "delete",
        "REMOVE_ITEM",
        "garbage",
        None,
    ],
)
def test_menu_swap_rejects_unknown_action(
    tenant_with_data,
    bad_action,
):
    with pytest.raises(
        (SimulatorInputError, TypeError),
    ):
        simulate_menu_swap(
            tenant_with_data["tenant_id"],
            tenant_with_data["pizza_id"],
            bad_action,
        )


def test_menu_swap_rejects_cross_tenant_access(
    tenant_with_data,
    second_tenant_with_data,
):
    with pytest.raises(
        SimulatorInputError,
        match="not found for this tenant",
    ):
        simulate_menu_swap(
            second_tenant_with_data["tenant_id"],
            tenant_with_data["pizza_id"],
            "remove",
        )


# ---------------------------------------------------------------------------
# Staffing
# ---------------------------------------------------------------------------

def test_staffing_increase_raises_labor_cost_prediction():
    result = simulate_staffing_change(
        "tenant-a",
        2,
    )

    assert result.margin_change_pct < 0


def test_staffing_decrease_raises_stockout_risk_proxy():
    cut = simulate_staffing_change(
        "tenant-a",
        -2,
    )

    add = simulate_staffing_change(
        "tenant-a",
        2,
    )

    assert cut.stockout_risk > add.stockout_risk


def test_staffing_change_accepts_negative_headcount():
    result = simulate_staffing_change(
        "tenant-a",
        -1,
    )

    assert result.stockout_risk > 0.05


@pytest.mark.parametrize(
    "bad_delta",
    [
        None,
        1.5,
        "2",
        True,
        False,
    ],
)
def test_staffing_change_rejects_invalid_headcount(
    bad_delta,
):
    with pytest.raises(
        (TypeError, SimulatorInputError),
    ):
        simulate_staffing_change(
            "tenant-a",
            bad_delta,
        )


# ---------------------------------------------------------------------------
# PredictedOutcome validation
# ---------------------------------------------------------------------------

def test_predicted_outcome_rejects_nan():
    with pytest.raises(SimulatorInputError):
        PredictedOutcome(
            margin_change_pct=float("nan"),
            demand_change_pct=0.0,
            stockout_risk=0.05,
            confidence=0.5,
            rationale="test",
        )


def test_predicted_outcome_rejects_infinite_confidence():
    with pytest.raises(SimulatorInputError):
        PredictedOutcome(
            margin_change_pct=0.0,
            demand_change_pct=0.0,
            stockout_risk=0.05,
            confidence=float("inf"),
            rationale="test",
        )


def test_predicted_outcome_rejects_invalid_confidence():
    with pytest.raises(SimulatorInputError):
        PredictedOutcome(
            margin_change_pct=0.0,
            demand_change_pct=0.0,
            stockout_risk=0.05,
            confidence=1.5,
            rationale="test",
        )


def test_predicted_outcome_rejects_invalid_stockout_risk():
    with pytest.raises(SimulatorInputError):
        PredictedOutcome(
            margin_change_pct=0.0,
            demand_change_pct=0.0,
            stockout_risk=2.0,
            confidence=0.5,
            rationale="test",
        )


# ---------------------------------------------------------------------------
# Tenant / identifier validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_tenant",
    [
        "",
        "   ",
        None,
        123,
        "\x00tenant",
    ],
)
def test_price_simulation_rejects_invalid_tenant(
    tenant_with_data,
    bad_tenant,
):
    with pytest.raises(
        (TypeError, SimulatorInputError),
    ):
        simulate_price_change(
            bad_tenant,
            tenant_with_data["pizza_id"],
            13.0,
        )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def test_dispatch_routes_price_change(
    tenant_with_data,
):
    result = simulate(
        tenant_with_data["tenant_id"],
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 14.0,
        },
    )

    assert result.demand_change_pct < 0


def test_dispatch_routes_supplier_switch(
    tenant_with_data,
):
    result = simulate(
        tenant_with_data["tenant_id"],
        ActionType.SUPPLIER_SWITCH,
        {
            "ingredient_id": tenant_with_data["flour_id"],
            "new_supplier_price": 0.8,
        },
    )

    assert result.margin_change_pct > 0


def test_dispatch_routes_menu_swap(
    tenant_with_data,
):
    result = simulate(
        tenant_with_data["tenant_id"],
        ActionType.MENU_SWAP,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "action": "remove",
        },
    )

    assert result.demand_change_pct < 0


def test_dispatch_routes_staffing_change(
    tenant_with_data,
):
    result = simulate(
        tenant_with_data["tenant_id"],
        ActionType.STAFFING_CHANGE,
        {
            "headcount_delta": -2,
        },
    )

    assert result.stockout_risk > 0.05


def test_dispatch_rejects_unknown_action_type(
    tenant_with_data,
):
    with pytest.raises(
        (TypeError, SimulatorInputError),
    ):
        simulate(
            tenant_with_data["tenant_id"],
            "not-a-real-action",
            {},
        )


def test_dispatch_rejects_missing_required_payload(
    tenant_with_data,
):
    with pytest.raises(
        SimulatorInputError,
        match="missing required field",
    ):
        simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {},
        )


def test_dispatch_rejects_non_mapping_payload(
    tenant_with_data,
):
    with pytest.raises(TypeError):
        simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            None,
        )


# ---------------------------------------------------------------------------
# Database failure behavior
# ---------------------------------------------------------------------------

def test_database_failure_is_propagated(
    tenant_with_data,
):
    with patch(
        "Component_2.simulator.get_db_context",
        side_effect=RuntimeError("database unavailable"),
    ):
        with pytest.raises(
            RuntimeError,
            match="database unavailable",
        ):
            simulate_price_change(
                tenant_with_data["tenant_id"],
                tenant_with_data["pizza_id"],
                13.0,
            )