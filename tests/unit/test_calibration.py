"""
Production tests for calibration.py.

Covers:
    - normal calibration behavior
    - empty datasets
    - tenant isolation
    - action-type aggregation
    - result ordering and limits
    - nullable fields
    - malformed/non-finite values at the application boundary
    - SQL-injection-style tenant identifiers
    - database failures
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import calibration

from calibration import (
    calibration_by_action_type,
    overall_calibration,
    worst_predictions,
)

from Component_1.models import ActionType, Decision, DecisionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_decision(
    tenant_id: str,
    *,
    calibration_error=None,
    action_type: ActionType = ActionType.PRICE_CHANGE,
    status: DecisionStatus = DecisionStatus.EVALUATED,
    predicted_outcome=None,
    actual_outcome=None,
    action_payload=None,
) -> Decision:
    """Create a schema-valid Decision for calibration tests."""

    return Decision(
        tenant_id=tenant_id,
        action_type=action_type,
        action_payload=action_payload or {
            "menu_item_id": "test-item",
            "new_price": 12.00,
        },
        predicted_outcome=(
            predicted_outcome
            if predicted_outcome is not None
            else {"rationale": "test prediction"}
        ),
        confidence=0.90,
        confidence_threshold_cleared=True,
        actual_outcome=(
            actual_outcome
            if actual_outcome is not None
            else {"result": "observed"}
        ),
        calibration_error=calibration_error,
        status=status,
        model_version="test",
    )


def add_decision(tenant_id: str, **kwargs) -> str:
    """Persist a valid Decision and return its stable ID."""
    decision = create_decision(tenant_id, **kwargs)

    with calibration.get_db_context() as session:
        session.add(decision)
        session.flush()
        decision_id = decision.id

    return decision_id

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant_id",
    ["", " ", "\t", "\n"],
)
def test_overall_calibration_rejects_empty_tenant_id(tenant_id):
    with pytest.raises(ValueError):
        overall_calibration(tenant_id)


@pytest.mark.parametrize(
    "tenant_id",
    [None, 123, object(), [], {}],
)
def test_overall_calibration_rejects_invalid_tenant_id(tenant_id):
    with pytest.raises(TypeError):
        overall_calibration(tenant_id)


@pytest.mark.parametrize(
    "tenant_id",
    ["", " ", "\t"],
)
def test_calibration_by_action_type_rejects_empty_tenant_id(tenant_id):
    with pytest.raises(ValueError):
        calibration_by_action_type(tenant_id)


@pytest.mark.parametrize(
    "tenant_id",
    [None, 123, object(), []],
)
def test_calibration_by_action_type_rejects_invalid_tenant_id(tenant_id):
    with pytest.raises(TypeError):
        calibration_by_action_type(tenant_id)


@pytest.mark.parametrize(
    "tenant_id",
    ["", " ", "\t"],
)
def test_worst_predictions_rejects_empty_tenant_id(
    tenant_id,
):
    with pytest.raises(ValueError):
        worst_predictions(tenant_id)


@pytest.mark.parametrize(
    "limit",
    [0, -1, -100],
)
def test_worst_predictions_rejects_non_positive_limit(
    tenant_with_data,
    limit,
):
    with pytest.raises(ValueError):
        worst_predictions(
            tenant_with_data["tenant_id"],
            limit=limit,
        )


@pytest.mark.parametrize(
    "limit",
    [None, "5", 1.5, True, False],
)
def test_worst_predictions_rejects_invalid_limit(
    tenant_with_data,
    limit,
):
    with pytest.raises(TypeError):
        worst_predictions(
            tenant_with_data["tenant_id"],
            limit=limit,
        )


def test_worst_predictions_rejects_excessive_limit(
    tenant_with_data,
):
    with pytest.raises(ValueError):
        worst_predictions(
            tenant_with_data["tenant_id"],
            limit=101,
        )


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_overall_calibration_empty_returns_none_not_zero(
    tenant_with_data,
):
    result = overall_calibration(
        tenant_with_data["tenant_id"],
    )

    assert result["count"] == 0
    assert result["mean_calibration_error"] is None
    assert result["min_error"] is None
    assert result["max_error"] is None


def test_calibration_by_action_type_empty_returns_empty_dict(
    tenant_with_data,
):
    result = calibration_by_action_type(
        tenant_with_data["tenant_id"],
    )

    assert result == {}


def test_worst_predictions_empty_returns_empty_list(
    tenant_with_data,
):
    result = worst_predictions(
        tenant_with_data["tenant_id"],
    )

    assert result == []


# ---------------------------------------------------------------------------
# Overall calibration
# ---------------------------------------------------------------------------


def test_overall_calibration_reflects_evaluated_decisions(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    for error in [0.10, 0.20, 0.30]:
        add_decision(
            tenant_id,
            calibration_error=error,
        )

    result = overall_calibration(tenant_id)

    assert result["count"] == 3
    assert result["mean_calibration_error"] == 0.2
    assert result["min_error"] == 0.1
    assert result["max_error"] == 0.3


def test_overall_calibration_excludes_non_evaluated_decisions(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.25,
        status=DecisionStatus.EVALUATED,
    )

    add_decision(
        tenant_id,
        calibration_error=0.99,
        status=DecisionStatus.PROPOSED,
    )

    result = overall_calibration(tenant_id)

    assert result["count"] == 1
    assert result["mean_calibration_error"] == 0.25


def test_overall_calibration_excludes_null_errors(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.25,
    )

    add_decision(
        tenant_id,
        calibration_error=None,
    )

    result = overall_calibration(tenant_id)

    assert result["count"] == 1
    assert result["mean_calibration_error"] == 0.25


def test_overall_calibration_rounds_to_four_decimal_places(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.1234,
    )

    add_decision(
        tenant_id,
        calibration_error=0.5678,
    )

    result = overall_calibration(tenant_id)

    assert result["mean_calibration_error"] == 0.3456


# ---------------------------------------------------------------------------
# Calibration by action type
# ---------------------------------------------------------------------------


def test_calibration_by_action_type_groups_correctly(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.10,
        action_type=ActionType.PRICE_CHANGE,
    )

    add_decision(
        tenant_id,
        calibration_error=0.30,
        action_type=ActionType.PRICE_CHANGE,
    )

    result = calibration_by_action_type(tenant_id)

    assert "price_change" in result
    assert result["price_change"]["count"] == 2
    assert result["price_change"]["mean_calibration_error"] == 0.2


def test_calibration_by_action_type_separates_action_types(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.10,
        action_type=ActionType.PRICE_CHANGE,
    )

    add_decision(
        tenant_id,
        calibration_error=0.40,
        action_type=ActionType.SUPPLIER_SWITCH,
    )

    result = calibration_by_action_type(tenant_id)

    assert result["price_change"]["count"] == 1
    assert result["price_change"]["mean_calibration_error"] == 0.1

    assert result["supplier_switch"]["count"] == 1
    assert result["supplier_switch"]["mean_calibration_error"] == 0.4


def test_calibration_by_action_type_excludes_null_errors(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=None,
        action_type=ActionType.PRICE_CHANGE,
    )

    result = calibration_by_action_type(tenant_id)

    assert result == {}


# ---------------------------------------------------------------------------
# Worst predictions
# ---------------------------------------------------------------------------


def test_worst_predictions_sorted_descending_by_error(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    for error in [0.10, 0.80, 0.30, 0.60]:
        add_decision(
            tenant_id,
            calibration_error=error,
        )

    worst = worst_predictions(
        tenant_id,
        limit=10,
    )

    errors = [
        item["calibration_error"]
        for item in worst
    ]

    assert errors == [0.8, 0.6, 0.3, 0.1]


def test_worst_predictions_respects_limit(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    for error in [0.1, 0.2, 0.3, 0.4, 0.5]:
        add_decision(
            tenant_id,
            calibration_error=error,
        )

    result = worst_predictions(
        tenant_id,
        limit=2,
    )

    assert len(result) == 2
    assert [
        item["calibration_error"]
        for item in result
    ] == [0.5, 0.4]


def test_worst_predictions_includes_rationale_for_review(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.75,
        predicted_outcome={
            "rationale": "Demand was underestimated",
        },
    )

    worst = worst_predictions(
        tenant_id,
        limit=1,
    )

    assert len(worst) == 1
    assert worst[0]["rationale"] == "Demand was underestimated"


def test_worst_predictions_handles_non_dict_prediction(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    add_decision(
        tenant_id,
        calibration_error=0.75,
        predicted_outcome=["malformed", "prediction"],
    )

    worst = worst_predictions(
        tenant_id,
        limit=1,
    )

    assert len(worst) == 1
    assert worst[0]["rationale"] is None


# ---------------------------------------------------------------------------
# RED TEAM — tenant isolation
# ---------------------------------------------------------------------------


def test_overall_calibration_cannot_leak_other_tenant_data(
    tenant_with_data,
    second_tenant_with_data,
):
    tenant_a = tenant_with_data["tenant_id"]
    tenant_b = second_tenant_with_data["tenant_id"]

    add_decision(
        tenant_a,
        calibration_error=0.10,
    )

    add_decision(
        tenant_b,
        calibration_error=99.99,
    )

    result = overall_calibration(tenant_a)

    assert result["count"] == 1
    assert result["mean_calibration_error"] == 0.10
    assert result["max_error"] == 0.10


def test_action_breakdown_cannot_leak_other_tenant_data(
    tenant_with_data,
    second_tenant_with_data,
):
    tenant_a = tenant_with_data["tenant_id"]
    tenant_b = second_tenant_with_data["tenant_id"]

    add_decision(
        tenant_a,
        calibration_error=0.10,
        action_type=ActionType.PRICE_CHANGE,
    )

    add_decision(
        tenant_b,
        calibration_error=99.99,
        action_type=ActionType.PRICE_CHANGE,
    )

    result = calibration_by_action_type(tenant_a)

    assert result["price_change"]["count"] == 1
    assert result["price_change"]["mean_calibration_error"] == 0.1

def test_worst_predictions_cannot_return_other_tenant_data(
    tenant_with_data,
    second_tenant_with_data,
):
    tenant_a = tenant_with_data["tenant_id"]
    tenant_b = second_tenant_with_data["tenant_id"]

    add_decision(
        tenant_a,
        calibration_error=0.10,
        predicted_outcome={
            "rationale": "Tenant A prediction",
        },
    )

    add_decision(
        tenant_b,
        calibration_error=99.99,
        predicted_outcome={
            "rationale": "SECRET TENANT B DATA",
        },
    )

    results = worst_predictions(
        tenant_a,
        limit=100,
    )

    assert len(results) == 1
    assert results[0]["calibration_error"] == 0.10
    assert results[0]["rationale"] == "Tenant A prediction"

    assert all(
        result["rationale"] != "SECRET TENANT B DATA"
        for result in results
    )

# ---------------------------------------------------------------------------
# RED TEAM — SQL injection
# ---------------------------------------------------------------------------


def test_sql_injection_payload_does_not_expand_tenant_scope(
    tenant_with_data,
):
    legitimate_tenant = tenant_with_data["tenant_id"]

    add_decision(
        legitimate_tenant,
        calibration_error=0.42,
    )

    malicious_tenant = (
        "' OR '1'='1' --"
    )

    result = overall_calibration(malicious_tenant)

    assert result["count"] == 0
    assert result["mean_calibration_error"] is None


def test_sql_injection_cannot_turn_tenant_filter_into_global_query(
    tenant_with_data,
):
    tenant_a = tenant_with_data["tenant_id"]

    add_decision(
        tenant_a,
        calibration_error=0.42,
    )

    malicious_tenant = (
        f"{tenant_a}' OR 1=1 --"
    )

    result = overall_calibration(malicious_tenant)

    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Non-finite application-boundary values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_valid_error_rejects_non_finite_values(bad_value):
    assert calibration._valid_error(bad_value) is False


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        "not-a-number",
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_valid_error_rejects_invalid_values(bad_value):
    assert calibration._valid_error(bad_value) is False


@pytest.mark.parametrize(
    "value",
    [0, 0.123456, 1.0, 99.9999],
)
def test_valid_error_accepts_finite_numeric_values(value):
    assert calibration._valid_error(value) is True


# ---------------------------------------------------------------------------
# Database failures
# ---------------------------------------------------------------------------


def test_overall_calibration_propagates_database_failure(
    monkeypatch,
):
    context = MagicMock()
    context.__enter__.side_effect = RuntimeError(
        "database unavailable"
    )

    monkeypatch.setattr(
        calibration,
        "get_db_context",
        lambda: context,
    )

    with pytest.raises(
        RuntimeError,
        match="database unavailable",
    ):
        overall_calibration("tenant-a")


def test_action_breakdown_propagates_database_failure(
    monkeypatch,
):
    context = MagicMock()
    context.__enter__.side_effect = RuntimeError(
        "database unavailable"
    )

    monkeypatch.setattr(
        calibration,
        "get_db_context",
        lambda: context,
    )

    with pytest.raises(
        RuntimeError,
        match="database unavailable",
    ):
        calibration_by_action_type("tenant-a")


def test_worst_predictions_propagates_database_failure(
    monkeypatch,
):
    context = MagicMock()
    context.__enter__.side_effect = RuntimeError(
        "database unavailable"
    )

    monkeypatch.setattr(
        calibration,
        "get_db_context",
        lambda: context,
    )

    with pytest.raises(
        RuntimeError,
        match="database unavailable",
    ):
        worst_predictions("tenant-a")


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


def test_worst_predictions_has_deterministic_tie_breaking(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    first_id = add_decision(
        tenant_id,
        calibration_error=0.5,
    )

    second_id = add_decision(
        tenant_id,
        calibration_error=0.5,
    )

    third_id = add_decision(
        tenant_id,
        calibration_error=0.5,
    )

    results = worst_predictions(
        tenant_id,
        limit=3,
    )

    ids = [
        result["id"]
        for result in results
    ]

    assert ids == sorted(
        [first_id, second_id, third_id]
    )