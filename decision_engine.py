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

from db import get_db_context
from ground_truth import observe_actual_outcome
from models import ActionType, Decision, DecisionStatus
from simulator import PredictedOutcome, simulate


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
    """Validate a tenant identifier without silently changing its value."""
    if not isinstance(tenant_id, str):
        raise TypeError("tenant_id must be a string")

    tenant_id = tenant_id.strip()

    if not tenant_id:
        raise ValueError("tenant_id must not be empty")

    if "\x00" in tenant_id:
        raise ValueError("tenant_id contains invalid null bytes")

    return tenant_id


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

    Raises:
        TypeError:
            If tenant_id, action_type, or payload has the wrong type.
        ValueError:
            If tenant_id is empty or malformed.
        InvalidPredictionError:
            If the simulator returns invalid numeric data.
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
        # Passing tenant_id establishes the transaction-local PostgreSQL
        # RLS context through the existing get_db_context() implementation.
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

            # Flush makes database-generated fields such as the ID available.
            # get_db_context() remains responsible for commit/rollback.
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

            # get_db_context() commits and closes the session after this
            # block. Detach the fully-loaded object so callers can safely
            # inspect it after the session has been closed.
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

    A decision must:

        1. Exist for the supplied tenant.
        2. Have cleared the confidence threshold.
        3. Currently be in SIMULATED state.

    The database row is locked during the state transition to prevent
    concurrent workers from executing the same decision.

    Args:
        tenant_id: Authenticated tenant identifier.
        decision_id: Decision to execute.

    Raises:
        DecisionNotFoundError:
            If the decision does not belong to the tenant or does not exist.
        DecisionStateError:
            If the decision cannot be automatically executed.
        InvalidPredictionError:
            If stored prediction data is invalid.
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

    # Phase 1:
    # Atomically claim the decision for execution.
    #
    # The tenant-scoped DB context establishes PostgreSQL RLS.
    # The explicit tenant predicate provides defense in depth.
    with get_db_context(tenant_id) as session:
        decision = (
            session.query(Decision)
            .filter(
                Decision.id == decision_id,
                Decision.tenant_id == tenant_id,
            )
            .with_for_update()
            .one_or_none()
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

        # Copy everything needed by the observation phase while the
        # ORM object is still attached to the active session.
        action_type = decision.action_type
        action_payload = dict(decision.action_payload)

        # Atomically transition SIMULATED -> EXECUTED while the row
        # remains locked by this transaction.
        decision.status = DecisionStatus.EXECUTED
        session.flush()

        logger.info(
            "Decision claimed for execution",
            extra={
                "tenant_id": tenant_id,
                "decision_id": decision_id,
            },
        )

    # Phase 2:
    # Observe the actual result outside the database transaction.
    #
    # This deliberately avoids holding a database transaction open while
    # observation/external work takes place.
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

    # Phase 3:
    # Persist the actual result and calibration data.
    with get_db_context(tenant_id) as session:
        decision = (
            session.query(Decision)
            .filter(
                Decision.id == decision_id,
                Decision.tenant_id == tenant_id,
                Decision.status == DecisionStatus.EXECUTED,
            )
            .one_or_none()
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

        # The DB context commits and closes the session after leaving
        # this block. Detach the fully-loaded result first.
        session.expunge(decision)

        return decision

def _calibration_error(
    predicted: PredictedOutcome,
    actual: Any,
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