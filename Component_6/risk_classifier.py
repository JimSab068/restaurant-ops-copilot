from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from Component_1.models import ActionType
from Component_2.decision_engine import Decision, PredictedOutcome

from Component_4.critic import (
    MAX_PRICE_CHANGE_PCT,
    _is_valid_numeric,
)


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


MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION = Decimal("0.80")

# Exactly 10% remains LOW.
SMALL_PRICE_CHANGE_PCT = Decimal("0.10")


def _high_risk(
    reason: str,
    *,
    reversible: bool = False,
) -> RiskClassification:
    return RiskClassification(
        risk_level=RiskLevel.HIGH,
        reversible=reversible,
        requires_approval=True,
        reason=reason,
    )


def _medium_risk(
    reason: str,
    *,
    reversible: bool = True,
) -> RiskClassification:
    return RiskClassification(
        risk_level=RiskLevel.MEDIUM,
        reversible=reversible,
        requires_approval=True,
        reason=reason,
    )


def _low_risk(reason: str) -> RiskClassification:
    return RiskClassification(
        risk_level=RiskLevel.LOW,
        reversible=True,
        requires_approval=False,
        reason=reason,
    )


def _to_exact_decimal(value: Any) -> Decimal:
    """
    Convert an already-validated numeric value to Decimal.

    bool is deliberately rejected because bool is a subclass of int.
    """
    if isinstance(value, bool):
        raise InvalidOperation("boolean is not a numeric policy value")

    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not value == value or value in (float("inf"), float("-inf")):
            raise InvalidOperation("non-finite float")
        result = Decimal(str(value))
    elif isinstance(value, str):
        result = Decimal(value.strip())
    else:
        result = Decimal(str(value))

    if not result.is_finite():
        raise InvalidOperation("non-finite decimal")

    return result


def _pct_change(
    new_price: Any,
    current_value: Any,
) -> Decimal:
    new_decimal = _to_exact_decimal(new_price)
    current_decimal = _to_exact_decimal(current_value)

    if current_decimal <= 0:
        raise InvalidOperation(
            "current value must be greater than zero"
        )

    return abs(new_decimal - current_decimal) / current_decimal


def _prediction_confidence(predicted: Any) -> Any:
    return getattr(predicted, "confidence", None)


def _confidence_is_valid(predicted: Any) -> bool:
    confidence = _prediction_confidence(predicted)

    if not _is_valid_numeric(confidence):
        return False

    if isinstance(confidence, bool):
        return False

    try:
        confidence_decimal = _to_exact_decimal(confidence)
    except (InvalidOperation, TypeError, ValueError):
        return False

    return Decimal("0") <= confidence_decimal <= Decimal("1")


def _confidence_is_high_enough(predicted: Any) -> bool:
    confidence = _prediction_confidence(predicted)

    if not _confidence_is_valid(predicted):
        return False

    try:
        confidence_decimal = _to_exact_decimal(confidence)
    except (InvalidOperation, TypeError, ValueError):
        return False

    return confidence_decimal >= MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION


def _classify_price_change(
    payload: dict[str, Any],
    current_value: Any,
) -> RiskClassification:

    if not _is_valid_numeric(current_value) or isinstance(
        current_value,
        bool,
    ):
        return _high_risk(
            "Cannot classify price-change risk: current value "
            "must be a finite number greater than zero."
        )

    try:
        current_decimal = _to_exact_decimal(current_value)
    except (InvalidOperation, TypeError, ValueError):
        return _high_risk(
            "Cannot classify price-change risk: current value "
            "must be a finite number."
        )

    if current_decimal <= 0:
        return _high_risk(
            "Cannot classify price-change risk: current value "
            "must be greater than zero."
        )

    if "new_price" not in payload:
        return _high_risk(
            "Cannot classify price-change risk: new_price is missing."
        )

    new_price = payload["new_price"]

    if not _is_valid_numeric(new_price) or isinstance(
        new_price,
        bool,
    ):
        return _high_risk(
            "Cannot classify price-change risk: new_price "
            "must be a finite number."
        )

    try:
        new_decimal = _to_exact_decimal(new_price)
        pct_change = _pct_change(
            new_decimal,
            current_decimal,
        )
        max_price_change_pct = _to_exact_decimal(
            MAX_PRICE_CHANGE_PCT
        )
    except (InvalidOperation, TypeError, ValueError):
        return _high_risk(
            "Cannot classify price-change risk: numeric values "
            "could not be represented exactly."
        )

    if new_decimal < 0:
        return _high_risk(
            "Cannot classify price-change risk: new_price "
            "cannot be negative."
        )

    # HARD LIMIT: strictly greater than 30%.
    if pct_change > max_price_change_pct:
        return _high_risk(
            f"Price change of {pct_change:.2%} exceeds the critic's "
            f"{max_price_change_pct:.2%} hard limit."
        )

    # IMPORTANT:
    # <= means exactly 10% is LOW.
    if pct_change <= SMALL_PRICE_CHANGE_PCT:
        return _low_risk(
            f"Small price change of {pct_change:.2%}; reversible and "
            "eligible for autonomous execution."
        )

    return _medium_risk(
        f"Price change of {pct_change:.2%} is larger than the "
        f"{SMALL_PRICE_CHANGE_PCT:.2%} autonomous threshold and "
        "requires human approval."
    )


def _classify_supplier_switch(
    payload: dict[str, Any],
    current_value: Any,
) -> RiskClassification:

    if (
        not _is_valid_numeric(current_value)
        or isinstance(current_value, bool)
    ):
        return _high_risk(
            "Cannot classify supplier-switch risk: current value "
            "must be a finite number greater than zero."
        )

    try:
        current_decimal = _to_exact_decimal(current_value)
    except (InvalidOperation, TypeError, ValueError):
        return _high_risk(
            "Cannot classify supplier-switch risk: current value "
            "must be numeric."
        )

    if current_decimal <= 0:
        return _high_risk(
            "Cannot classify supplier-switch risk: current value "
            "must be greater than zero."
        )

    if "new_supplier_price" not in payload:
        return _high_risk(
            "Cannot classify supplier-switch risk: "
            "new_supplier_price is missing."
        )

    new_price = payload["new_supplier_price"]

    if (
        not _is_valid_numeric(new_price)
        or isinstance(new_price, bool)
    ):
        return _high_risk(
            "Cannot classify supplier-switch risk: "
            "new_supplier_price must be a finite number."
        )

    try:
        if _to_exact_decimal(new_price) < 0:
            return _high_risk(
                "Cannot classify supplier-switch risk: "
                "new_supplier_price cannot be negative."
            )
    except (InvalidOperation, TypeError, ValueError):
        return _high_risk(
            "Cannot classify supplier-switch risk: "
            "new_supplier_price must be numeric."
        )

    return _medium_risk(
        "Supplier switches affect procurement relationships "
        "and require human approval.",
        reversible=True,
    )


def _classify_menu_swap(
    payload: dict[str, Any],
) -> RiskClassification:

    action = payload.get("action")

    if action == "remove":
        return _high_risk(
            "Menu-item removal is a high-impact action and requires "
            "human review before execution.",
            reversible=False,
        )

    return _high_risk(
        f"Unsupported MENU_SWAP action: {action!r}. "
        "Manual review is required."
    )


def _classify_staffing_change(
    payload: dict[str, Any],
) -> RiskClassification:

    if "headcount_delta" not in payload:
        return _high_risk(
            "Cannot classify staffing-change risk: "
            "headcount_delta is missing."
        )

    delta = payload["headcount_delta"]

    if isinstance(delta, bool):
        return _high_risk(
            "Cannot classify staffing-change risk: "
            "headcount_delta cannot be boolean."
        )

    if not _is_valid_numeric(delta):
        return _high_risk(
            "Cannot classify staffing-change risk: "
            "headcount_delta must be a finite number."
        )

    try:
        delta_decimal = _to_exact_decimal(delta)
    except (InvalidOperation, TypeError, ValueError):
        return _high_risk(
            "Cannot classify staffing-change risk: "
            "headcount_delta must be numeric."
        )

    # Staffing headcount is discrete.
    if delta_decimal != delta_decimal.to_integral_value():
        return _high_risk(
            f"Headcount change of {delta_decimal} must be an integer."
        )

    return _medium_risk(
        "Staffing changes affect scheduled labor and require "
        "human approval."
    )


def classify_risk(
    action_type: ActionType,
    payload: dict[str, Any],
    predicted: Decision | PredictedOutcome,
    current_value: Any,
) -> RiskClassification:

    if not isinstance(action_type, ActionType):
        return _high_risk(
            f"Unrecognized action type: {action_type!r}"
        )

    if not isinstance(payload, dict):
        return _high_risk(
            "Action payload must be a dictionary."
        )

    if not _confidence_is_valid(predicted):
        return _high_risk(
            "Prediction confidence is missing, invalid, or outside "
            "the valid range [0, 1]."
        )

    if not _confidence_is_high_enough(predicted):
        confidence = _prediction_confidence(predicted)

        try:
            confidence_decimal = _to_exact_decimal(confidence)
            confidence_text = f"{confidence_decimal:.3f}"
        except (InvalidOperation, TypeError, ValueError):
            confidence_text = repr(confidence)

        return _high_risk(
            f"Prediction confidence {confidence_text} is below the "
            f"{MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION:.2f} risk threshold; "
            "human review is required.",
            reversible=True,
        )

    if action_type == ActionType.PRICE_CHANGE:
        return _classify_price_change(
            payload,
            current_value,
        )

    if action_type == ActionType.SUPPLIER_SWITCH:
        return _classify_supplier_switch(
            payload,
            current_value,
        )

    if action_type == ActionType.MENU_SWAP:
        return _classify_menu_swap(payload)

    if action_type == ActionType.STAFFING_CHANGE:
        return _classify_staffing_change(payload)

    return _high_risk(
        f"No risk policy is defined for action type: "
        f"{action_type.value!r}."
    )