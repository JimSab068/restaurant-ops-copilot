"""
Decision engine — orchestration loop for Component 2:

    propose -> simulate -> confidence check -> execute (or hold)
    -> observe actual -> record calibration

This is the decision boundary used by Component 4 (tool/action framework).
Given a proposed action, the engine returns a decision that is either
cleared for execution or held for human review.

Lifecycle:

    propose -> simulate -> confidence gate
        -> SIMULATED / HELD_LOW_CONFIDENCE
        -> execute -> observe -> calibrate -> EVALUATED

Security invariants:

    - All database operations are performed inside get_db_context().
    - Every database lookup is explicitly tenant-scoped.
    - PostgreSQL RLS is established through get_db_context(tenant_id).
    - Decision execution uses row-level locking to prevent concurrent
      execution of the same decision.
    - Only SIMULATED, confidence-cleared decisions may be auto-executed.
    - Prediction and calibration values must be finite numeric values.
    - Database failures are rolled back by the DB context manager.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import ActionType, Decision, DecisionStatus
from .ground_truth import ActualOutcome, observe_actual_outcome
from .simulator import PredictedOutcome, simulate


logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.5


class DecisionEngineError(RuntimeError):
    """Base exception for decision-engine failures."""


class DecisionNotFoundError(DecisionEngineError):
    """Raised when a tenant-scoped decision cannot be found."""


class DecisionStateError(DecisionEngineError):
    """Raised when a decision is not in a valid lifecycle state."""


class InvalidPredictionError(DecisionEngineError):
    """Raised when prediction or observation data is invalid."""


def _validate_tenant_id(tenant_id: str) -> str:
    """Validate and canonicalize a tenant identifier."""
    try:
        return validate_tenant_id(tenant_id)
    except TypeError as exc:
        raise TypeError("tenant_id must be a string") from exc
    except ValueError:
        raise


def _validate_decision_id(decision_id: str) -> str:
    """Validate a decision identifier before database access."""
    if not isinstance(decision_id, str):
        raise TypeError("decision_id must be a string")

    decision_id = decision_id.strip()

    if not decision_id:
        raise ValueError("decision_id must not be empty")

    if "\x00" in decision_id:
        raise ValueError("decision_id contains invalid null bytes")

    return decision_id


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Validate and defensively copy an action payload.

    The copy prevents the caller from mutating the dictionary after the
    decision has been created.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    return dict(payload)


def _validate_finite_number(
    value: Any,
    field_name: str,
) -> float:
    """
    Validate that a value is numeric and finite.

    Rejects None, NaN, positive infinity, negative infinity, and
    non-numeric values.
    """
    if isinstance(value, bool):
        raise InvalidPredictionError(
            f"{field_name} must be numeric"
        )

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPredictionError(
            f"{field_name} must be numeric"
        ) from exc

    if not math.isfinite(numeric_value):
        raise InvalidPredictionError(
            f"{field_name} must be finite"
        )

    return numeric_value


def _validate_prediction(
    predicted: PredictedOutcome,
) -> None:
    """Validate simulator output before persistence or execution."""
    confidence = _validate_finite_number(
        predicted.confidence,
        "confidence",
    )

    _validate_finite_number(
        predicted.margin_change_pct,
        "margin_change_pct",
    )

    _validate_finite_number(
        predicted.demand_change_pct,
        "demand_change_pct",
    )

    if not 0.0 <= confidence <= 1.0:
        raise InvalidPredictionError(
            "confidence must be between 0 and 1"
        )


def _get_decision(
    session,
    tenant_id: str,
    decision_id: str,
    *,
    lock: bool = False,
    status: DecisionStatus | None = None,
) -> Decision | None:
    """
    Retrieve a decision within an explicit tenant scope.

    PostgreSQL RLS is established by get_db_context(), while the explicit
    tenant predicate provides application-level defense in depth.

    When lock=True, the row is locked for the duration of the transaction.
    """
    query = (
        session.query(Decision)
        .filter(
            Decision.id == decision_id,
            Decision.tenant_id == tenant_id,
        )
    )

    if status is not None:
        query = query.filter(Decision.status == status)

    if lock:
        query = query.with_for_update()

    return query.one_or_none()


def _prediction_from_decision(
    decision: Decision,
) -> PredictedOutcome:
    """
    Reconstruct and validate a stored prediction.

    Stored JSON is historical data and must therefore be treated as
    untrusted input.
    """
    predicted_data = decision.predicted_outcome

    if not isinstance(predicted_data, Mapping):
        raise InvalidPredictionError(
            "stored predicted_outcome is invalid"
        )

    try:
        predicted = PredictedOutcome(
            **dict(predicted_data)
        )
    except (TypeError, ValueError) as exc:
        raise InvalidPredictionError(
            "stored predicted_outcome cannot be reconstructed"
        ) from exc

    _validate_prediction(predicted)

    return predicted


def propose_and_simulate(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> Decision:
    """
    Simulate an action, apply the confidence gate, and persist the decision.

    Returns:
        DecisionStatus.SIMULATED when confidence clears the threshold.
        DecisionStatus.HELD_LOW_CONFIDENCE otherwise.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    payload_copy = _validate_payload(payload)

    if not isinstance(action_type, ActionType):
        raise TypeError("action_type must be an ActionType")

    logger.info(
        "Simulating decision",
        extra={
            "tenant_id": tenant_id,
            "action_type": action_type.value,
        },
    )

    predicted = simulate(
        tenant_id,
        action_type,
        payload_copy,
    )

    _validate_prediction(predicted)

    confidence = float(predicted.confidence)
    cleared = confidence >= CONFIDENCE_THRESHOLD

    status = (
        DecisionStatus.SIMULATED
        if cleared
        else DecisionStatus.HELD_LOW_CONFIDENCE
    )

    try:
        with get_db_context(tenant_id) as session:
            decision = Decision(
                tenant_id=tenant_id,
                action_type=action_type,
                action_payload=payload_copy,
                predicted_outcome=predicted.to_dict(),
                confidence=confidence,
                confidence_threshold_cleared=cleared,
                status=status,
            )

            session.add(decision)
            session.flush()
            session.refresh(decision)

            logger.info(
                "Decision created",
                extra={
                    "tenant_id": tenant_id,
                    "decision_id": decision.id,
                    "status": status.value,
                    "confidence": confidence,
                },
            )

            # Return a detached object because the session is about to close.
            session.expunge(decision)

            return decision

    except (TypeError, ValueError, InvalidPredictionError):
        raise
    except Exception:
        logger.exception(
            "Failed to persist decision",
            extra={
                "tenant_id": tenant_id,
                "action_type": action_type.value,
            },
        )
        raise


def execute_and_observe(
    tenant_id: str,
    decision_id: str,
) -> Decision:
    """
    Execute a tenant-scoped confidence-cleared decision and evaluate it.

    Lifecycle:

        SIMULATED
            ↓
        row lock
            ↓
        confidence/state validation
            ↓
        EXECUTED
            ↓
        observe actual outcome
            ↓
        calculate calibration error
            ↓
        EVALUATED

    The execution claim and evaluation persistence are separate database
    transactions so external observation never occurs while holding a
    database transaction open.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    decision_id = _validate_decision_id(decision_id)

    logger.info(
        "Attempting decision execution",
        extra={
            "tenant_id": tenant_id,
            "decision_id": decision_id,
        },
    )

    # ------------------------------------------------------------------
    # Phase 1: Atomically claim the decision.
    # ------------------------------------------------------------------
    with get_db_context(tenant_id) as session:
        decision = _get_decision(
            session,
            tenant_id,
            decision_id,
            lock=True,
        )

        if decision is None:
            logger.warning(
                "Decision not found for tenant",
                extra={
                    "tenant_id": tenant_id,
                    "decision_id": decision_id,
                },
            )
            raise DecisionNotFoundError("decision not found")

        if not decision.confidence_threshold_cleared:
            raise DecisionStateError(
                "cannot auto-execute a decision that did not clear "
                "the confidence threshold; requires human override "
                "(Component 6)"
            )

        if decision.status != DecisionStatus.SIMULATED:
            raise DecisionStateError(
                f"decision cannot be executed from state "
                f"{decision.status.value}"
            )

        predicted = _prediction_from_decision(decision)

        action_type = decision.action_type
        action_payload = dict(decision.action_payload)

        # The row remains locked until this transaction commits.
        decision.status = DecisionStatus.EXECUTED
        session.flush()

        logger.info(
            "Decision claimed for execution",
            extra={
                "tenant_id": tenant_id,
                "decision_id": decision_id,
            },
        )

    # ------------------------------------------------------------------
    # Phase 2: Observe the actual outcome.
    #
    # This intentionally occurs outside the database transaction.
    # ------------------------------------------------------------------
    try:
        actual = observe_actual_outcome(
            action_type,
            action_payload,
            predicted,
        )
    except Exception:
        logger.exception(
            "Failed to observe decision outcome",
            extra={
                "tenant_id": tenant_id,
                "decision_id": decision_id,
            },
        )
        raise

    calibration_error = _calibration_error(
        predicted,
        actual,
    )

    # ------------------------------------------------------------------
    # Phase 3: Persist observation and calibration.
    # ------------------------------------------------------------------
    with get_db_context(tenant_id) as session:
        decision = _get_decision(
            session,
            tenant_id,
            decision_id,
            status=DecisionStatus.EXECUTED,
        )

        if decision is None:
            raise DecisionStateError(
                "decision is no longer available for evaluation"
            )

        decision.actual_outcome = actual.to_dict()
        decision.calibration_error = calibration_error
        decision.status = DecisionStatus.EVALUATED
        decision.evaluated_at = datetime.now(timezone.utc)

        session.flush()
        session.refresh(decision)

        logger.info(
            "Decision evaluated",
            extra={
                "tenant_id": tenant_id,
                "decision_id": decision_id,
                "calibration_error": calibration_error,
            },
        )

        session.expunge(decision)

        return decision


def _calibration_error(
    predicted: PredictedOutcome,
    actual: ActualOutcome,
) -> float:
    """
    Calculate mean absolute error across numeric prediction dimensions.

    Lower is better:
        0.0 = perfect prediction.

    The calculation fails closed when either prediction or observation
    contains missing, non-numeric, NaN, or infinite values.
    """
    predicted_margin = _validate_finite_number(
        predicted.margin_change_pct,
        "predicted.margin_change_pct",
    )

    predicted_demand = _validate_finite_number(
        predicted.demand_change_pct,
        "predicted.demand_change_pct",
    )

    actual_margin = _validate_finite_number(
        actual.margin_change_pct,
        "actual.margin_change_pct",
    )

    actual_demand = _validate_finite_number(
        actual.demand_change_pct,
        "actual.demand_change_pct",
    )

    error = (
        abs(predicted_margin - actual_margin)
        + abs(predicted_demand - actual_demand)
    ) / 2.0

    if not math.isfinite(error):
        raise InvalidPredictionError(
            "calibration error is not finite"
        )

    return round(error, 4)
