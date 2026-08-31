"""
Tests for decision_engine.py — the propose -> simulate -> gate -> execute
-> observe -> calibrate loop.

Coverage:
    - normal proposal/simulation
    - confidence gating
    - execution
    - observation
    - calibration
    - missing decisions
    - cross-tenant access
    - replayed execution
    - malformed payloads
    - invalid confidence
    - NaN / infinity
    - malformed prediction data
    - observation failures
    - database failures
"""
import pytest
from Component_1.db import get_session
from Component_1.models import Decision, DecisionStatus, ActionType

from unittest.mock import MagicMock
from Component_2.decision_engine import (
    CONFIDENCE_THRESHOLD,
    DecisionNotFoundError,
    DecisionStateError,
    InvalidPredictionError,
    _calibration_error,
    execute_and_observe,
    propose_and_simulate,
)


def test_propose_and_simulate_creates_decision_row(tenant_with_data):
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 13.0},
    )
    assert decision.id is not None
    assert decision.predicted_outcome is not None
    assert decision.confidence is not None


def test_high_confidence_decision_marked_simulated(tenant_with_data):
    # small price change -> high confidence, per simulator.py's confidence formula
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 12.2},
    )
    assert decision.confidence_threshold_cleared is True
    assert decision.status == DecisionStatus.SIMULATED


def test_low_confidence_decision_held_for_review(tenant_with_data):
    # staffing changes are capped at 0.45 confidence in simulator.py, always below 0.5 threshold
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.STAFFING_CHANGE, {"headcount_delta": 1},
    )
    assert decision.confidence < CONFIDENCE_THRESHOLD
    assert decision.confidence_threshold_cleared is False
    assert decision.status == DecisionStatus.HELD_LOW_CONFIDENCE


def test_execute_and_observe_fills_in_actual_outcome(tenant_with_data):
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 12.1},
    )
    result = execute_and_observe(
        tenant_with_data["tenant_id"],
        decision.id,
    )
    assert result.status == DecisionStatus.EVALUATED
    assert result.actual_outcome is not None
    assert result.calibration_error is not None
    assert result.calibration_error >= 0
    assert result.evaluated_at is not None


def test_execute_and_observe_refuses_low_confidence_decision(tenant_with_data):
    """
    This is the safety-relevant test: a decision that didn't clear the
    confidence bar must NOT be silently auto-executed. It should raise,
    requiring an explicit human override path (Component 6) instead.
    """
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.STAFFING_CHANGE, {"headcount_delta": 1},
    )
    assert decision.confidence_threshold_cleared is False

    with pytest.raises(DecisionStateError, match="did not clear the confidence threshold"):
        execute_and_observe(
            tenant_with_data["tenant_id"],
            decision.id,
)

def test_execute_and_observe_raises_on_missing_decision(tenant_with_data):
    with pytest.raises(DecisionNotFoundError, match="decision not found"):
        execute_and_observe(
            tenant_with_data["tenant_id"],
            "nonexistent-id",
        )


def test_calibration_error_is_symmetric_and_nonnegative(tenant_with_data):
    """Sanity check on the calibration math itself, not just plumbing."""
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"], ActionType.PRICE_CHANGE,
        {"menu_item_id": tenant_with_data["pizza_id"], "new_price": 12.05},
    )
    result = execute_and_observe(
        tenant_with_data["tenant_id"],
        decision.id,
    )

    predicted = result.predicted_outcome
    actual = result.actual_outcome

    expected_error = round(
        (abs(predicted["margin_change_pct"] - actual["margin_change_pct"])
         + abs(predicted["demand_change_pct"] - actual["demand_change_pct"])) / 2,
        4,
    )
    assert float(result.calibration_error) == pytest.approx(
    expected_error )





def test_propose_and_simulate_creates_decision_row(
    tenant_with_data,
):
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"],
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 13.0,
        },
    )

    assert decision.id is not None
    assert decision.tenant_id == tenant_with_data["tenant_id"]
    assert decision.predicted_outcome is not None
    assert decision.confidence is not None


def test_high_confidence_decision_is_simulated(
    tenant_with_data,
):
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"],
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 12.2,
        },
    )

    assert decision.confidence >= CONFIDENCE_THRESHOLD
    assert decision.confidence_threshold_cleared is True
    assert decision.status == DecisionStatus.SIMULATED


def test_low_confidence_decision_is_held(
    tenant_with_data,
):
    decision = propose_and_simulate(
        tenant_with_data["tenant_id"],
        ActionType.STAFFING_CHANGE,
        {"headcount_delta": 1},
    )

    assert decision.confidence < CONFIDENCE_THRESHOLD
    assert decision.confidence_threshold_cleared is False
    assert decision.status == DecisionStatus.HELD_LOW_CONFIDENCE


def test_execute_and_observe_evaluates_decision(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_id,
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 12.1,
        },
    )

    result = execute_and_observe(
        tenant_id,
        decision.id,
    )

    assert result.status == DecisionStatus.EVALUATED
    assert result.actual_outcome is not None
    assert result.calibration_error is not None
    assert result.calibration_error >= 0
    assert result.evaluated_at is not None


def test_low_confidence_decision_cannot_execute(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_id,
        ActionType.STAFFING_CHANGE,
        {"headcount_delta": 1},
    )

    with pytest.raises(
        DecisionStateError,
        match="did not clear the confidence threshold",
    ):
        execute_and_observe(
            tenant_id,
            decision.id,
        )


def test_missing_decision_is_rejected():
    with pytest.raises(DecisionNotFoundError):
        execute_and_observe(
            "tenant-a",
            "does-not-exist",
        )


def test_empty_tenant_id_is_rejected():
    with pytest.raises(ValueError, match="tenant_id"):
        propose_and_simulate(
            "",
            ActionType.PRICE_CHANGE,
            {},
        )


def test_whitespace_tenant_id_is_rejected():
    with pytest.raises(ValueError, match="tenant_id"):
        propose_and_simulate(
            "   ",
            ActionType.PRICE_CHANGE,
            {},
        )


def test_non_string_tenant_id_is_rejected():
    with pytest.raises(TypeError, match="tenant_id"):
        propose_and_simulate(
            123,
            ActionType.PRICE_CHANGE,
            {},
        )


def test_empty_decision_id_is_rejected():
    with pytest.raises(ValueError, match="decision_id"):
        execute_and_observe(
            "tenant-a",
            "",
        )


def test_non_string_decision_id_is_rejected():
    with pytest.raises(TypeError, match="decision_id"):
        execute_and_observe(
            "tenant-a",
            123,
        )


def test_non_mapping_payload_is_rejected(
    tenant_with_data,
):
    with pytest.raises(TypeError, match="payload"):
        propose_and_simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            None,
        )


def test_payload_is_defensively_copied(
    tenant_with_data,
):
    payload = {
        "menu_item_id": tenant_with_data["pizza_id"],
        "new_price": 13.0,
    }

    decision = propose_and_simulate(
        tenant_with_data["tenant_id"],
        ActionType.PRICE_CHANGE,
        payload,
    )

    payload["new_price"] = 999999.0

    assert decision.action_payload["new_price"] == 13.0


def test_invalid_action_type_is_rejected(
    tenant_with_data,
):
    with pytest.raises(TypeError, match="action_type"):
        propose_and_simulate(
            tenant_with_data["tenant_id"],
            "PRICE_CHANGE",
            {},
        )


def test_cross_tenant_decision_access_is_rejected(
    tenant_with_data,
):
    """
    Security regression test.

    A valid decision ID must not be sufficient to operate on another
    tenant's decision.
    """
    tenant_a = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_a,
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 12.2,
        },
    )

    attacker_tenant = "attacker-tenant"

    with pytest.raises(DecisionNotFoundError):
        execute_and_observe(
            attacker_tenant,
            decision.id,
        )


def test_already_evaluated_decision_cannot_be_replayed(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_id,
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 12.1,
        },
    )

    execute_and_observe(
        tenant_id,
        decision.id,
    )

    with pytest.raises(DecisionStateError):
        execute_and_observe(
            tenant_id,
            decision.id,
        )


def test_held_decision_cannot_be_executed(
    tenant_with_data,
):
    tenant_id = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_id,
        ActionType.STAFFING_CHANGE,
        {"headcount_delta": 1},
    )

    assert decision.status == DecisionStatus.HELD_LOW_CONFIDENCE

    with pytest.raises(DecisionStateError):
        execute_and_observe(
            tenant_id,
            decision.id,
        )


def test_calibration_error_is_correct():
    predicted = MagicMock()
    predicted.margin_change_pct = 10.0
    predicted.demand_change_pct = 20.0

    actual = MagicMock()
    actual.margin_change_pct = 8.0
    actual.demand_change_pct = 14.0

    assert _calibration_error(
        predicted,
        actual,
    ) == 4.0


def test_calibration_error_is_symmetric():
    predicted = MagicMock()
    predicted.margin_change_pct = 10.0
    predicted.demand_change_pct = 20.0

    actual = MagicMock()
    actual.margin_change_pct = 8.0
    actual.demand_change_pct = 14.0

    error_a = _calibration_error(
        predicted,
        actual,
    )

    error_b = _calibration_error(
        actual,
        predicted,
    )

    assert error_a == error_b


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_calibration_rejects_non_finite_prediction(
    value,
):
    predicted = MagicMock()
    predicted.margin_change_pct = value
    predicted.demand_change_pct = 1.0

    actual = MagicMock()
    actual.margin_change_pct = 1.0
    actual.demand_change_pct = 1.0

    with pytest.raises(
        InvalidPredictionError,
        match="finite",
    ):
        _calibration_error(
            predicted,
            actual,
        )


def test_calibration_rejects_none_prediction():
    predicted = MagicMock()
    predicted.margin_change_pct = None
    predicted.demand_change_pct = 1.0

    actual = MagicMock()
    actual.margin_change_pct = 1.0
    actual.demand_change_pct = 1.0

    with pytest.raises(InvalidPredictionError):
        _calibration_error(
            predicted,
            actual,
        )


def test_calibration_rejects_none_actual():
    predicted = MagicMock()
    predicted.margin_change_pct = 1.0
    predicted.demand_change_pct = 1.0

    actual = MagicMock()
    actual.margin_change_pct = None
    actual.demand_change_pct = 1.0

    with pytest.raises(InvalidPredictionError):
        _calibration_error(
            predicted,
            actual,
        )


def test_confidence_must_be_finite(
    tenant_with_data,
    monkeypatch,
):
    prediction = MagicMock()
    prediction.confidence = float("nan")
    prediction.margin_change_pct = 1.0
    prediction.demand_change_pct = 1.0

    monkeypatch.setattr(
        "Component_2.decision_engine.simulate",
        lambda *args, **kwargs: prediction,
    )

    with pytest.raises(
        InvalidPredictionError,
        match="finite",
    ):
        propose_and_simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {},
        )


@pytest.mark.parametrize(
    "confidence",
    [-0.1, 1.1, float("inf"), float("-inf")],
)
def test_invalid_confidence_is_rejected(
    tenant_with_data,
    monkeypatch,
    confidence,
):
    prediction = MagicMock()
    prediction.confidence = confidence
    prediction.margin_change_pct = 1.0
    prediction.demand_change_pct = 1.0
    prediction.to_dict.return_value = {
        "confidence": confidence,
        "margin_change_pct": 1.0,
        "demand_change_pct": 1.0,
    }

    monkeypatch.setattr(
        "Component_2.decision_engine.simulate",
        lambda *args, **kwargs: prediction,
    )

    with pytest.raises(InvalidPredictionError):
        propose_and_simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {},
        )


def test_observation_failure_does_not_return_evaluated_decision(
    tenant_with_data,
    monkeypatch,
):
    tenant_id = tenant_with_data["tenant_id"]

    decision = propose_and_simulate(
        tenant_id,
        ActionType.PRICE_CHANGE,
        {
            "menu_item_id": tenant_with_data["pizza_id"],
            "new_price": 12.2,
        },
    )

    def failing_observation(*args, **kwargs):
        raise RuntimeError("ground truth unavailable")

    monkeypatch.setattr(
        "Component_2.decision_engine.observe_actual_outcome",
        failing_observation,
    )

    with pytest.raises(
        RuntimeError,
        match="ground truth unavailable",
    ):
        execute_and_observe(
            tenant_id,
            decision.id,
        )


def test_database_failure_during_proposal_is_propagated(
    tenant_with_data,
    monkeypatch,
):
    class BrokenSession:
        def add(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

        def flush(self):
            raise RuntimeError("database unavailable")

    class BrokenContext:
        def __enter__(self):
            return BrokenSession()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        "Component_2.decision_engine.get_db_context",
        lambda tenant_id: BrokenContext(),
    )

    with pytest.raises(
        RuntimeError,
        match="database unavailable",
    ):
        propose_and_simulate(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 12.0,
            },
        )


def test_sql_injection_in_tenant_id_does_not_escape_scope(
    tenant_with_data,
):
    """
    Security regression test.

    Tenant IDs are supplied as bound ORM parameters rather than interpolated
    into SQL. A malicious tenant identifier must not expose another tenant.
    """
    malicious_tenant = (
        "' OR '1'='1' --"
    )

    with pytest.raises(DecisionNotFoundError):
        execute_and_observe(
            malicious_tenant,
            "some-decision-id",
        )