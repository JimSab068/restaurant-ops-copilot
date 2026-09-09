"""
Calibration and evaluation utilities for Component 2.

The calibration layer compares predicted outcomes against actual outcomes
recorded in the Decision ledger. It is an observability/evaluation boundary,
not a decision-making or execution authority.

Responsibilities:
- Aggregate calibration error across evaluated decisions.
- Break calibration error down by action type.
- Identify the worst predictions for inspection.
- Enforce tenant isolation at the database boundary.
- Defensively handle malformed historical prediction/error data.

Design principles:
- Decision is the source of truth for calibration records.
- Calibration is read-only.
- Tenant isolation is enforced through transaction-local PostgreSQL RLS
  context and explicit tenant predicates.
- Calibration metrics do not grant execution authority.
- Existing calibration_error semantics are preserved as a non-negative
  error magnitude.
""" 

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import Decision, DecisionStatus

logger = logging.getLogger(__name__)


_DEFAULT_WORST_PREDICTIONS_LIMIT = 5
_MAX_WORST_PREDICTIONS_LIMIT = 100

def _require_tenant_id(tenant_id: str) -> str:
    if tenant_id is None:
        raise ValueError("tenant_id must not be None.")
    return validate_tenant_id(tenant_id)

def _validate_limit(limit: int) -> int:
    """
    Validate and return a worst-predictions result limit.

    bool is explicitly rejected because bool is a subclass of int in Python.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer.")

    if limit <= 0:
        raise ValueError("limit must be greater than zero.")

    if limit > _MAX_WORST_PREDICTIONS_LIMIT:
        raise ValueError(
            f"limit must be <= {_MAX_WORST_PREDICTIONS_LIMIT}."
        )

    return limit


def _valid_error(value: Any) -> bool:
    """
    Return True when a calibration error is a finite numeric value.

    Historical data is treated defensively: malformed or non-finite values
    are ignored rather than allowed to corrupt aggregate metrics.
    """
    try:
        error = float(value)
    except (TypeError, ValueError):
        return False

    return math.isfinite(error)


def _safe_round(value: Optional[float]) -> Optional[float]:
    """
    Round a numeric metric to four decimal places.

    None is preserved for empty datasets.
    """
    if value is None:
        return None

    return round(float(value), 4)


def _action_type_name(action_type: Any) -> Optional[str]:
    """
    Return the stable string representation of an ActionType enum.

    Returns None when historical/corrupt data does not contain a usable
    action type.
    """
    if action_type is None:
        return None

    value = getattr(action_type, "value", None)

    if value is None:
        return None

    return str(value)


def _safe_rationale(predicted_outcome: Any) -> Optional[str]:
    """
    Extract a prediction rationale defensively.

    Prediction payloads are historical JSON and therefore should not be
    assumed to have the expected structure.
    """
    if not isinstance(predicted_outcome, dict):
        return None

    rationale = predicted_outcome.get("rationale")

    if rationale is None:
        return None

    return str(rationale)


def _calibration_query(session, tenant_id: str):
    """
    Return the canonical population of decisions used for calibration.

    Calibration metrics are defined over:
        - the requested tenant,
        - evaluated decisions only,
        - decisions with a recorded calibration error.

    The explicit tenant predicate is intentional even though
    get_db_context(tenant_id) establishes PostgreSQL RLS context. This gives
    defense-in-depth at both the database-session and application-query
    layers.
    """
    return (
        session.query(Decision)
        .filter(
            Decision.tenant_id == tenant_id,
            Decision.status == DecisionStatus.EVALUATED,
            Decision.calibration_error.isnot(None),
        )
    )


def overall_calibration(tenant_id: str) -> dict:
    tenant_id = _require_tenant_id(tenant_id)

    with get_db_context(tenant_id) as session:
        decisions = _calibration_query(session, tenant_id)

        errors = [
            float(decision.calibration_error)
            for decision in decisions
            if _valid_error(decision.calibration_error)
        ]

        if not errors:
            return {
                "count": 0,
                "mean_calibration_error": None,
                "min_error": None,
                "max_error": None,
            }

        return {
            "count": len(errors),
            "mean_calibration_error": _safe_round(
                sum(errors) / len(errors)
            ),
            "min_error": _safe_round(min(errors)),
            "max_error": _safe_round(max(errors)),
        }
    
def calibration_by_action_type(
    tenant_id: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Calculate calibration metrics grouped by action type.

    Invalid/non-finite calibration errors and unusable action types are
    ignored defensively.

    Returns:
        {
            "price_change": {
                "count": int,
                "mean_calibration_error": float,
            },
            ...
        }
    """
    tenant_id = _require_tenant_id(tenant_id)

    with get_db_context(tenant_id) as session:
        decisions = _calibration_query(session, tenant_id).all()

        grouped_errors: Dict[str, List[float]] = {}

        for decision in decisions:
            action_type = _action_type_name(decision.action_type)

            if action_type is None:
                continue

            if not _valid_error(decision.calibration_error):
                continue

            error = float(decision.calibration_error)

            grouped_errors.setdefault(action_type, []).append(error)

        result: Dict[str, Dict[str, Any]] = {}

        for action_type in sorted(grouped_errors):
            errors = grouped_errors[action_type]

            result[action_type] = {
                "count": len(errors),
                "mean_calibration_error": _safe_round(
                    sum(errors) / len(errors)
                ),
            }

        return result

def worst_predictions(tenant_id: str, limit: int = 5) -> list:
    tenant_id = _require_tenant_id(tenant_id)
    limit = _validate_limit(limit)

    with get_db_context(tenant_id) as session:
        decisions = _calibration_query(session, tenant_id)

        results = []

        for decision in decisions:
            if not _valid_error(decision.calibration_error):
                continue

            results.append(
                {
                    "decision_id": decision.id,
                    "action_type": _action_type_name(decision.action_type),
                    "predicted_outcome": decision.predicted_outcome,
                    "actual_outcome": decision.actual_outcome,
                    "calibration_error": _safe_round(
                        decision.calibration_error
                    ),
                    "rationale": _safe_rationale(
                        decision.predicted_outcome
                    ),
                }
            )

        results.sort(
            key=lambda item: (
                -item["calibration_error"],
                item["decision_id"],
            )
        )

        return results[:limit]