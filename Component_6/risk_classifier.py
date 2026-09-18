"""
risk_classifier.py — Pure risk classification policy for Component 6.

Component 6 sits after Component 2's prediction layer and before execution.
It classifies the operational risk of a proposed action without performing
any database access or execution.

Architecture:

    Component 2 Decision / PredictedOutcome
                    |
                    v
             risk_classifier
                    |
                    v
             RiskClassification
                    |
                    v
          approval / execution gate

The classifier deliberately does NOT:
    - query the database
    - mutate application state
    - execute tools
    - call an LLM
    - silently coerce malformed numeric values

Malformed or unverifiable inputs fail closed as HIGH risk with approval
required.

Policy:
    LOW
        Small, reversible price/inventory changes.
        Autonomous execution is allowed.

    MEDIUM
        Larger price changes and supplier switches.
        Human approval is required.

    HIGH
        Menu removal, high-uncertainty predictions, unsupported actions,
        or invalid/unverifiable inputs.
        Human review/rejection is required.

Numeric validation is intentionally reused from critic.py rather than
duplicated here.
"""

from __future__ import annotations
from decimal import Decimal

from dataclasses import dataclass
from enum import Enum
from typing import Any

from Component_1.models import ActionType
from Component_2.decision_engine import Decision, PredictedOutcome

from Component_4.critic import (
    MAX_PRICE_CHANGE_PCT,
    _is_valid_numeric,
)


def _to_exact_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))

def _pct_change(new_price: Any, current_value: Any) -> Decimal:
    new_decimal = _to_exact_decimal(new_price)
    current_decimal = _to_exact_decimal(current_value)
    return abs(new_decimal - current_decimal) / current_decimal

class RiskLevel(str, Enum):
    """Operational risk level assigned to a proposed action."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class RiskClassification:
    """Immutable risk-policy result."""

    risk_level: RiskLevel
    reversible: bool
    requires_approval: bool
    reason: str


# A prediction below this confidence is considered too uncertain for
# autonomous execution. Keep this independent from the critic's hard
# business-rule limits.
MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION = 0.80

# "Small" price changes are low-risk and may be autonomous.
# Changes above this threshold become medium-risk, subject to the critic's
# separate hard limit of MAX_PRICE_CHANGE_PCT.
SMALL_PRICE_CHANGE_PCT = 0.10


def _high_risk(reason: str, *, reversible: bool = False) -> RiskClassification:
    """Create the fail-closed HIGH classification."""
    return RiskClassification(
        risk_level=RiskLevel.HIGH,
        reversible=reversible,
        requires_approval=True,
        reason=reason,
    )


def _medium_risk(reason: str, *, reversible: bool = True) -> RiskClassification:
    """Create a MEDIUM classification requiring human approval."""
    return RiskClassification(
        risk_level=RiskLevel.MEDIUM,
        reversible=reversible,
        requires_approval=True,
        reason=reason,
    )


def _low_risk(reason: str) -> RiskClassification:
    """Create a LOW classification eligible for autonomous execution."""
    return RiskClassification(
        risk_level=RiskLevel.LOW,
        reversible=True,
        requires_approval=False,
        reason=reason,
    )


def _prediction_confidence(predicted: Any) -> Any:
    """
    Extract prediction confidence without coupling the policy to a concrete
    implementation of Component 2's prediction object.

    Both Decision and PredictedOutcome are expected to expose ``confidence``.
    Missing confidence is deliberately returned as None and therefore fails
    closed.
    """
    # if not isinstance(predicted, (Decision, PredictedOutcome)):
    #     return None

    return getattr(predicted, "confidence", None)


def _confidence_is_valid(predicted: Any) -> bool:
    """Return True only when prediction confidence is finite and bounded."""
    confidence = _prediction_confidence(predicted)

    if not _is_valid_numeric(confidence):
        return False

    return 0 <= confidence <= 1


def _confidence_is_high_enough(predicted: Any) -> bool:
    """Return whether the prediction clears the autonomous-risk threshold."""
    confidence = _prediction_confidence(predicted)

    return (
        _confidence_is_valid(predicted)
        and confidence >= MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION
    )


def _classify_price_change(
    payload: dict[str, Any],
    current_value: Any,
) -> RiskClassification:
    """Classify a proposed menu-price change."""

    if not _is_valid_numeric(current_value) or current_value <= 0:
        return _high_risk(
            "Cannot classify price-change risk: current value must be a "
            "finite number greater than zero."
        )

    if "new_price" not in payload:
        return _high_risk(
            "Cannot classify price-change risk: new_price is missing."
        )

    new_price = payload["new_price"]

    if not _is_valid_numeric(new_price):
        return _high_risk(
            "Cannot classify price-change risk: new_price must be a "
            "finite number."
        )

    pct_change = _pct_change(new_price, current_value) 
    small_price_change_pct = Decimal(str(SMALL_PRICE_CHANGE_PCT))
    max_price_change_pct = Decimal(str(MAX_PRICE_CHANGE_PCT))

    # The critic independently enforces the hard 30% price-change bound.
    # Component 6 only classifies the operational risk.
    if pct_change > max_price_change_pct:
        return _high_risk(
            f"Price change of {pct_change:.0%} exceeds the critic's "
            f"{MAX_PRICE_CHANGE_PCT:.0%} hard limit."
        )

    if pct_change <= small_price_change_pct:
        return _low_risk(
            f"Small price change of {pct_change:.0%}; reversible and "
            "eligible for autonomous execution."
        )

    return _medium_risk(
        f"Price change of {pct_change:.0%} is larger than the "
        f"{small_price_change_pct:.0%} autonomous threshold and requires "
        "human approval."
    )


def _classify_supplier_switch(
    payload: dict[str, Any],
    current_value: Any,
) -> RiskClassification:
    """
    Supplier switches are inherently MEDIUM risk.

    The critic separately validates the supplier price and its hard
    percentage bound. This classifier does not duplicate that business rule.
    """
    if not _is_valid_numeric(current_value) or current_value <= 0:
        return _high_risk(
            "Cannot classify supplier-switch risk: current value must be "
            "a finite number greater than zero."
        )

    if "new_supplier_price" not in payload:
        return _high_risk(
            "Cannot classify supplier-switch risk: "
            "new_supplier_price is missing."
        )

    if not _is_valid_numeric(payload["new_supplier_price"]):
        return _high_risk(
            "Cannot classify supplier-switch risk: new_supplier_price "
            "must be a finite number."
        )

    return _medium_risk(
        "Supplier switches affect procurement relationships and require "
        "human approval.",
        reversible=True,
    )


def _classify_menu_swap(
    payload: dict[str, Any],
) -> RiskClassification:
    """Classify menu changes, with removal treated as HIGH risk."""

    action = payload.get("action")

    if action == "remove":
        return _high_risk(
            "Menu-item removal is a high-impact action and requires human "
            "review before execution.",
            reversible=False,
        )

    # MENU_SWAP add is currently unsupported by the execution framework.
    # Treating an unknown menu operation as low risk would violate the
    # fail-closed policy.
    return _high_risk(
        f"Unsupported MENU_SWAP action: {action!r}. "
        "Manual review is required."
    )


def _classify_staffing_change(payload: dict[str, Any]) -> RiskClassification:
    if "headcount_delta" not in payload:
        return _high_risk("Cannot classify staffing-change risk: headcount_delta is missing.")

    delta = payload["headcount_delta"]

    if not _is_valid_numeric(delta):
        return _high_risk("Cannot classify staffing-change risk: headcount_delta must be a finite number.")

    if isinstance(delta, float) and not delta.is_integer():
        return _high_risk(f"Headcount change of {delta} must be an integer.")

    if isinstance(delta, Decimal) and delta != delta.to_integral_value():
        return _high_risk(f"Headcount change of {delta} must be an integer.")

    return _medium_risk("Staffing changes affect scheduled labor and require human approval.")


def classify_risk(
    action_type: ActionType,
    payload: dict[str, Any],
    predicted: Decision | PredictedOutcome,
    current_value: Any,
) -> RiskClassification:
    """
    Classify the operational risk of a proposed action.

    Parameters
    ----------
    action_type:
        Component 1 ActionType identifying the proposed operation.

    payload:
        Action-specific values. Numeric values are validated using the
        existing critic.py numeric validator.

    predicted:
        Component 2 Decision or PredictedOutcome containing the simulator's
        confidence.

    current_value:
        Caller-supplied pre-change value where the action requires one.
        No database access occurs here.

    Returns
    -------
    RiskClassification
        Immutable LOW/MEDIUM/HIGH risk classification.

    Fail-closed behavior
    --------------------
    Unknown action types, malformed payloads, invalid numeric inputs,
    invalid prediction objects, and missing/invalid confidence are HIGH risk
    and require approval.
    """

    if not isinstance(action_type, ActionType):
        return _high_risk(
            f"Unrecognized action type: {action_type!r}"
        )

    if not isinstance(payload, dict):
        return _high_risk(
            "Action payload must be a dictionary."
        )

    # Confidence is a universal safety gate. A risk classifier must never
    # downgrade an uncertain prediction merely because the requested action
    # itself appears operationally small.
    if not _confidence_is_valid(predicted):
        return _high_risk(
            "Prediction confidence is missing, invalid, or outside the "
            "valid range [0, 1]."
        )

    if not _confidence_is_high_enough(predicted):
        confidence = _prediction_confidence(predicted)
        return _high_risk(
            f"Prediction confidence {confidence:.3f} is below the "
            f"{MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION:.2f} risk threshold; "
            "human review is required.",
            reversible=True,
        )

    if action_type == ActionType.PRICE_CHANGE:
        return _classify_price_change(payload, current_value)

    if action_type == ActionType.SUPPLIER_SWITCH:
        return _classify_supplier_switch(payload, current_value)

    if action_type == ActionType.MENU_SWAP:
        return _classify_menu_swap(payload)

    if action_type == ActionType.STAFFING_CHANGE:
        return _classify_staffing_change(payload)

    # Keep this explicit even though ActionType validation above makes it
    # difficult to reach. It protects the classifier if new enum members are
    # added without a corresponding Component 6 policy.
    return _high_risk(
        f"No risk policy is defined for action type: {action_type.value!r}."
    )
