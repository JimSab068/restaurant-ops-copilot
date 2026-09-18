"""Offline tests for Component 7's Version 2 evaluation metrics."""

import pytest

from Component_7 import EvaluationLedger, EvaluationRecord, evaluate

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def clean_db():
    """Component 7 metrics are deliberately dependency-free."""
    yield


def record(record_id="r1", **changes):
    data = dict(tenant_id=TENANT_A, record_id=record_id, opportunity_was_correct=True,
                candidate_was_valid=True, decision_succeeded=True, human_overrode=False,
                policy_violation=False, isolation_breach=False, execution_failed=False,
                retry_count=0, autonomous=True, latency_ms=12, cost_usd=.04,
                predicted_economic_impact=100, actual_economic_impact=80)
    data.update(changes)
    return EvaluationRecord(**data)


def test_all_version2_metrics_are_calculated_from_known_observations():
    result = evaluate(TENANT_A, [record(), record("r2", opportunity_was_correct=False, candidate_was_valid=False, decision_succeeded=False, human_overrode=True, policy_violation=True, isolation_breach=True, execution_failed=True, retry_count=2, autonomous=False, latency_ms=28, cost_usd=.06, predicted_economic_impact=50, actual_economic_impact=0)])
    assert result.record_count == 2
    assert result.opportunity_detection_accuracy == .5
    assert result.candidate_action_validity == .5
    assert result.decision_success_rate == .5
    assert result.autonomous_action_rate == .5
    assert result.human_override_rate == .5
    assert result.policy_violation_rate == .5
    assert result.isolation_breach_count == 1
    assert result.execution_failure_rate == .5 and result.retry_rate == .5
    assert result.mean_decision_latency_ms == 20 and result.mean_cost_per_decision_usd == .05
    assert result.mean_absolute_prediction_error == 35 and result.mean_relative_prediction_error == .25


def test_unknown_signals_are_not_misreported_as_success_or_failure():
    result = evaluate(TENANT_A, [EvaluationRecord(TENANT_A, "r")])
    assert result.decision_success_rate is None
    assert result.mean_absolute_prediction_error is None
    assert result.isolation_breach_count == 0


def test_tenant_isolation_duplicate_protection_and_input_validation():
    ledger = EvaluationLedger()
    ledger.record(record())
    assert ledger.metrics(TENANT_A).record_count == 1
    assert ledger.metrics(TENANT_B).record_count == 0
    with pytest.raises(ValueError): ledger.record(record())
    with pytest.raises(ValueError): evaluate(TENANT_A, [record(tenant_id=TENANT_B)])
    with pytest.raises(ValueError): EvaluationRecord(TENANT_A, "r", latency_ms=-1)
    with pytest.raises(ValueError): EvaluationRecord(TENANT_A, "r", retry_count=-1)
