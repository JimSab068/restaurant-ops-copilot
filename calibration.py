"""
Calibration tracking — the metric that actually matters for Component 2.

A simulator that runs without crashing proves nothing. This module answers
the real question: "how far off are the predictions, on average, and is
that getting better or worse over time / by action type?"
Calibration tracking for the Restaurant Ops Copilot.

This module measures how accurately the decision simulator predicts
observed outcomes. It is an observability/evaluation boundary and must
never leak calibration data across tenants.

Public API:
    overall_calibration()
    calibration_by_action_type()
    worst_predictions()
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

from db import get_db_context
from models import Decision, DecisionStatus

logger = logging.getLogger(__name__)

_DEFAULT_WORST_PREDICTIONS_LIMIT = 5
_MAX_WORST_PREDICTIONS_LIMIT = 100


def _validate_tenant_id(tenant_id: str) -> str:
    """Validate and normalize a tenant identifier.

    Tenant scope is mandatory because calibration data is tenant-owned.
    """
    if not isinstance(tenant_id, str):
        raise TypeError("tenant_id must be a string")

    normalized = tenant_id.strip()

    if not normalized:
        raise ValueError("tenant_id must not be empty")

    return normalized


def _validate_limit(limit: int) -> int:
    """Validate and bound result-set limits."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")

    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    if limit > _MAX_WORST_PREDICTIONS_LIMIT:
        raise ValueError(
            f"limit must not exceed {_MAX_WORST_PREDICTIONS_LIMIT}"
        )

    return limit


def _valid_error(value: Any) -> bool:
    """Return True only for finite numeric calibration errors."""
    if value is None:
        return False

    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return False

    return math.isfinite(numeric_value)


def _safe_round(value: Any) -> float | None:
    """Convert a calibration error to a finite rounded float."""
    if not _valid_error(value):
        return None

    return round(float(value), 4)


def _action_type_name(action_type: Any) -> str | None:
    """Safely extract an action type name from an enum-like value."""
    if action_type is None:
        return None

    value = getattr(action_type, "value", None)

    if value is None:
        return None

    if not isinstance(value, str):
        return str(value)

    return value


def _safe_rationale(predicted_outcome: Any) -> Any:
    """Extract rationale without assuming JSON structure."""
    if not isinstance(predicted_outcome, dict):
        return None

    return predicted_outcome.get("rationale")


def overall_calibration(tenant_id: str) -> dict[str, int | float | None]:
    """
    Calculate aggregate calibration error for one tenant.

    Only EVALUATED decisions belonging to the supplied tenant are included.

    Returns:
        {
            "count": int,
            "mean_calibration_error": float | None,
            "min_error": float | None,
            "max_error": float | None,
        }

    A tenant with no valid calibration data returns None metrics rather
    than incorrectly reporting perfect calibration as 0.0.
    """
    tenant_id = _validate_tenant_id(tenant_id)

    logger.debug(
        "Calculating overall calibration",
        extra={"tenant_id": tenant_id},
    )

    try:
        with get_db_context() as session:
            # Keep tenant_id explicitly in the predicate. Do not rely on
            # callers or ORM relationships to establish tenant isolation.
            decisions = (
                session.query(Decision.calibration_error)
                .filter(
                    Decision.tenant_id == tenant_id,
                    Decision.status == DecisionStatus.EVALUATED,
                    Decision.calibration_error.isnot(None),
                )
                .all()
            )

            errors = [
                float(row[0])
                for row in decisions
                if _valid_error(row[0])
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
                "mean_calibration_error": round(sum(errors) / len(errors), 4),
                "min_error": round(min(errors), 4),
                "max_error": round(max(errors), 4),
            }

    except Exception:
        logger.exception(
            "Failed to calculate overall calibration",
            extra={"tenant_id": tenant_id},
        )
        raise


def calibration_by_action_type(
    tenant_id: str,
) -> dict[str, dict[str, int | float]]:
    """
    Calculate calibration error grouped by action type for one tenant.

    Decisions with:
        - no calibration error
        - invalid/non-finite calibration error
        - no action type

    are excluded from the grouping.

    Tenant scope is mandatory.
    """
    tenant_id = _validate_tenant_id(tenant_id)

    logger.debug(
        "Calculating calibration by action type",
        extra={"tenant_id": tenant_id},
    )

    try:
        with get_db_context() as session:
            decisions = (
                session.query(
                    Decision.action_type,
                    Decision.calibration_error,
                )
                .filter(
                    Decision.tenant_id == tenant_id,
                    Decision.status == DecisionStatus.EVALUATED,
                    Decision.calibration_error.isnot(None),
                )
                .all()
            )

            errors_by_type: dict[str, list[float]] = defaultdict(list)

            for action_type, calibration_error in decisions:
                action_name = _action_type_name(action_type)

                if action_name is None:
                    continue

                if not _valid_error(calibration_error):
                    logger.warning(
                        "Ignoring invalid calibration error",
                        extra={
                            "tenant_id": tenant_id,
                            "action_type": action_name,
                        },
                    )
                    continue

                errors_by_type[action_name].append(
                    float(calibration_error)
                )

            return {
                action_type: {
                    "count": len(errors),
                    "mean_calibration_error": round(
                        sum(errors) / len(errors),
                        4,
                    ),
                }
                for action_type, errors in errors_by_type.items()
                if errors
            }

    except Exception:
        logger.exception(
            "Failed to calculate calibration by action type",
            extra={"tenant_id": tenant_id},
        )
        raise


def worst_predictions(
    tenant_id: str,
    limit: int = _DEFAULT_WORST_PREDICTIONS_LIMIT,
) -> list[dict[str, Any]]:
    """
    Return the highest-error evaluated predictions for one tenant.

    Results are ordered from highest to lowest calibration error.

    The tenant_id predicate is mandatory and applied directly to the
    database query to prevent cross-tenant data exposure.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    limit = _validate_limit(limit)

    logger.debug(
        "Fetching worst predictions",
        extra={
            "tenant_id": tenant_id,
            "limit": limit,
        },
    )

    try:
        with get_db_context() as session:
            decisions = (
                session.query(Decision)
                .filter(
                    Decision.tenant_id == tenant_id,
                    Decision.status == DecisionStatus.EVALUATED,
                    Decision.calibration_error.isnot(None),
                )
                .order_by(
                    Decision.calibration_error.desc(),
                    Decision.id.asc(),
                )
                .limit(limit)
                .all()
            )

            results: list[dict[str, Any]] = []

            for decision in decisions:
                error = _safe_round(decision.calibration_error)

                # Database data may have been corrupted or may have been
                # written by an older application version. Do not allow
                # NaN/Infinity into API responses.
                if error is None:
                    continue

                action_type = _action_type_name(decision.action_type)

                if action_type is None:
                    logger.warning(
                        "Skipping decision with invalid action type",
                        extra={
                            "tenant_id": tenant_id,
                            "decision_id": decision.id,
                        },
                    )
                    continue

                results.append(
                    {
                        "id": decision.id,
                        "action_type": action_type,
                        "predicted": decision.predicted_outcome,
                        "actual": decision.actual_outcome,
                        "calibration_error": error,
                        "rationale": _safe_rationale(
                            decision.predicted_outcome
                        ),
                    }
                )

            return results

    except Exception:
        logger.exception(
            "Failed to retrieve worst predictions",
            extra={
                "tenant_id": tenant_id,
                "limit": limit,
            },
        )
        raise