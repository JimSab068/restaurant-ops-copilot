"""Safe, dependency-free metrics for the Version 2 evaluation contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import EvaluationRecord, canonical_tenant_id


def _rate(records: list[EvaluationRecord], field: str, positive: bool = True) -> float | None:
    known = [getattr(record, field) for record in records if getattr(record, field) is not None]
    if not known:
        return None
    matches = sum(value is positive for value in known)
    return round(matches / len(known), 4)


@dataclass(frozen=True)
class EvaluationMetrics:
    """Version 2 metrics, with ``None`` representing no observed denominator."""

    record_count: int
    opportunity_detection_accuracy: float | None
    candidate_action_validity: float | None
    decision_success_rate: float | None
    autonomous_action_rate: float | None
    human_override_rate: float | None
    policy_violation_rate: float | None
    isolation_breach_count: int
    execution_failure_rate: float | None
    retry_rate: float | None
    mean_decision_latency_ms: float | None
    mean_cost_per_decision_usd: float | None
    mean_absolute_prediction_error: float | None
    mean_relative_prediction_error: float | None


def evaluate(tenant_id: str, records: Iterable[EvaluationRecord]) -> EvaluationMetrics:
    """Aggregate records for exactly one tenant; mixed input is rejected.

    The caller supplies tenant-scoped observations. Rejecting a foreign record
    instead of filtering it makes accidental cross-tenant aggregation visible.
    """
    tenant_id = canonical_tenant_id(tenant_id)
    selected = list(records)
    if any(not isinstance(record, EvaluationRecord) for record in selected):
        raise TypeError("records must contain EvaluationRecord instances")
    if any(record.tenant_id != tenant_id for record in selected):
        raise ValueError("records must all belong to tenant_id")

    latency = [record.latency_ms for record in selected if record.latency_ms is not None]
    costs = [record.cost_usd for record in selected if record.cost_usd is not None]
    retries = [record.retry_count for record in selected if record.retry_count is not None]
    paired = [
        (record.predicted_economic_impact, record.actual_economic_impact)
        for record in selected
        if record.predicted_economic_impact is not None and record.actual_economic_impact is not None
    ]
    absolute_errors = [abs(predicted - actual) for predicted, actual in paired]
    # A relative error requires a non-zero actual baseline. Zero is retained
    # in absolute-error metrics but omitted here to avoid inventing infinity.
    relative_errors = [abs(predicted - actual) / abs(actual) for predicted, actual in paired if actual != 0]
    average = lambda values: round(sum(values) / len(values), 4) if values else None
    return EvaluationMetrics(
        record_count=len(selected),
        opportunity_detection_accuracy=_rate(selected, "opportunity_was_correct"),
        candidate_action_validity=_rate(selected, "candidate_was_valid"),
        decision_success_rate=_rate(selected, "decision_succeeded"),
        autonomous_action_rate=_rate(selected, "autonomous"),
        human_override_rate=_rate(selected, "human_overrode"),
        policy_violation_rate=_rate(selected, "policy_violation"),
        isolation_breach_count=sum(record.isolation_breach is True for record in selected),
        execution_failure_rate=_rate(selected, "execution_failed"),
        retry_rate=(round(sum(retry > 0 for retry in retries) / len(retries), 4) if retries else None),
        mean_decision_latency_ms=average(latency),
        mean_cost_per_decision_usd=average(costs),
        mean_absolute_prediction_error=average(absolute_errors),
        mean_relative_prediction_error=average(relative_errors),
    )


class EvaluationLedger:
    """In-memory, tenant-isolated ledger suitable for tests and adapters.

    Production persistence should be supplied by an event-store adapter rather
    than allowing observability to bypass Component 1's audit boundary.
    """
    def __init__(self) -> None:
        self._records: dict[str, dict[str, EvaluationRecord]] = {}

    def record(self, observation: EvaluationRecord) -> None:
        if not isinstance(observation, EvaluationRecord):
            raise TypeError("observation must be an EvaluationRecord")
        tenant_records = self._records.setdefault(observation.tenant_id, {})
        if observation.record_id in tenant_records:
            raise ValueError("record_id already exists for this tenant")
        tenant_records[observation.record_id] = observation

    def metrics(self, tenant_id: str) -> EvaluationMetrics:
        tenant_id = canonical_tenant_id(tenant_id)
        return evaluate(tenant_id, self._records.get(tenant_id, {}).values())
