"""Translate existing component outputs into Component 7 observations."""
from __future__ import annotations

from typing import Any

from .models import EvaluationRecord


def from_decision(tenant_id: str, decision: Any, *, record_id: str, autonomous: bool | None = None, human_overrode: bool | None = None) -> EvaluationRecord:
    """Create an outcome record from Component 2's detached Decision object."""
    predicted = getattr(decision, "predicted_outcome", None) or {}
    actual = getattr(decision, "actual_outcome", None) or {}
    predicted_impact = predicted.get("estimated_monthly_impact") if isinstance(predicted, dict) else None
    actual_impact = actual.get("estimated_monthly_impact") if isinstance(actual, dict) else None
    return EvaluationRecord(tenant_id=tenant_id, record_id=record_id, decision_id=getattr(decision, "id", None), action_type=getattr(getattr(decision, "action_type", None), "value", None), decision_succeeded=getattr(getattr(decision, "status", None), "value", None) == "evaluated", autonomous=autonomous, human_overrode=human_overrode, predicted_economic_impact=predicted_impact, actual_economic_impact=actual_impact)


def from_tool_result(tenant_id: str, result: dict[str, Any], *, record_id: str, latency_ms: float | None = None, cost_usd: float | None = None) -> EvaluationRecord:
    return EvaluationRecord(tenant_id=tenant_id, record_id=record_id, decision_id=result.get("decision_id"), decision_succeeded=result.get("executed"), execution_failed=(result.get("executed") is False and result.get("reason") == "execution_failed"), latency_ms=latency_ms, cost_usd=cost_usd)
