"""
critic.py — Independent hard-policy validation for proposed actions.

The critic is intentionally rule-based and has NO database access.

Architecture:
    simulator -> prediction/confidence
                    |
                    v
                 critic -> hard business-policy verdict
                    |
                    v
                 execution

The simulator answers:
    "How likely is this action to produce a good outcome?"

The critic answers:
    "Does this action violate a hard business rule?"

Keeping these questions separate prevents the critic from becoming a
second copy of the simulator's reasoning process and reduces correlated
blind spots.

Security / reliability principle:
    The critic is a policy gate, so malformed or unverifiable inputs fail
    closed rather than being silently approved.

Current limitations:
    MENU_SWAP remains unbounded by design because the current tool
    framework does not create a new menu item's initial price through
    this critic path.

TODO:
    Once tool_framework supports creating menu items with an explicit
    initial price, add a hard numeric bound for MENU_SWAP "add" actions.
    For example, require the initial price to be within an approved
    absolute range or within a configurable percentage of an appropriate
    reference value.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from Component_1.models import ActionType


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticVerdict:
    """Immutable result of the independent policy check."""

    approved: bool
    reason: str


# Hard bounds — deliberately conservative, and independent of the
# simulator's confidence. A high-confidence prediction that still crosses
# one of these lines gets blocked; confidence measures "how sure the model
# is," not "how much this could hurt the business if it's wrong."
MAX_PRICE_CHANGE_PCT = 0.30
MAX_SUPPLIER_PRICE_INCREASE_PCT = 0.50
MAX_HEADCOUNT_DELTA = 5


# Numeric values accepted by the critic.
# Decimal is important because SQLAlchemy Numeric columns commonly return
# Decimal instances at runtime.
NumericValue = int | float | Decimal


def _is_valid_numeric(value: Any) -> bool:
    """
    Return True only for finite numeric values.

    bool is deliberately rejected even though bool subclasses int in Python.
    Strings such as "12.50" are also rejected rather than implicitly parsed.
    """
    if isinstance(value, bool):
        return False

    if isinstance(value, Decimal):
        return value.is_finite()

    if isinstance(value, (int, float)):
        return math.isfinite(value)

    return False


def _log_verdict(
    action_type: Any,
    verdict: CriticVerdict,
) -> CriticVerdict:
    """
    Log every critic decision at the point it is made.

    Do not log the complete payload: action payloads may eventually contain
    sensitive or tenant-specific operational data. The verdict, action type,
    and reason are sufficient for debugging the policy layer.
    """
    action_name = (
        action_type.value
        if isinstance(action_type, ActionType)
        else str(action_type)
    )

    log_method = logger.info if verdict.approved else logger.warning

    log_method(
        "critic_verdict action_type=%s approved=%s reason=%s",
        action_name,
        verdict.approved,
        verdict.reason,
        extra={
            "critic_action_type": action_name,
            "critic_approved": verdict.approved,
            "critic_reason": verdict.reason,
        },
    )

    return verdict


def _blocked(action_type: Any, reason: str) -> CriticVerdict:
    """Create and log a blocked verdict."""
    return _log_verdict(
        action_type,
        CriticVerdict(approved=False, reason=reason),
    )


def _approved(action_type: Any, reason: str) -> CriticVerdict:
    """Create and log an approved verdict."""
    return _log_verdict(
        action_type,
        CriticVerdict(approved=True, reason=reason),
    )


def _require_payload_dict(payload: Any, action_type: ActionType) -> bool:
    """Validate that the action payload has the expected container type."""
    if not isinstance(payload, dict):
        return False

    return True


def _validate_positive_baseline(
    current_value: Any,
    field_name: str,
    action_type: ActionType,
) -> CriticVerdict | None:
    """
    Validate the baseline used for percentage-based policy checks.

    A missing, zero, negative, or non-numeric baseline cannot establish a
    meaningful percentage change. The critic therefore fails closed.

    This is intentionally different from MENU_SWAP: creation of a genuinely
    new item belongs to MENU_SWAP and is currently outside this numeric
    policy check.
    """
    if current_value is None:
        return _blocked(
            action_type,
            f"Cannot evaluate {field_name}: current value is missing.",
        )

    if not _is_valid_numeric(current_value):
        return _blocked(
            action_type,
            f"Cannot evaluate {field_name}: current value is not a finite number.",
        )

    if current_value <= 0:
        return _blocked(
            action_type,
            f"Cannot evaluate {field_name}: current value must be greater than zero.",
        )

    return None


def _validate_payload_number(
    payload: dict[str, Any],
    field_name: str,
    action_type: ActionType,
) -> tuple[NumericValue | None, CriticVerdict | None]:
    """
    Retrieve and validate a required numeric payload field.

    Missing and malformed values are blocked rather than treated as absent
    or converted implicitly.
    """
    if field_name not in payload:
        return None, _blocked(
            action_type,
            f"Missing required payload field: {field_name}.",
        )

    value = payload[field_name]

    if not _is_valid_numeric(value):
        return None, _blocked(
            action_type,
            f"Payload field {field_name} must be a finite number.",
        )

    return value, None


def critique(
    action_type: ActionType,
    payload: dict[str, Any],
    current_value: NumericValue | None = None,
) -> CriticVerdict:
    """
    Independently validate a proposed action against hard business rules.

    Parameters
    ----------
    action_type:
        The proposed action category.

    payload:
        Action-specific payload. Required numeric fields are validated
        strictly; missing or malformed fields fail closed.

    current_value:
        The pre-change price/cost when required for percentage-based checks.
        This value is supplied by the caller. The critic deliberately does
        not query the database.

    Returns
    -------
    CriticVerdict
        approved=False means the action must not proceed.

    Important policy behavior
    -------------------------
    PRICE_CHANGE and SUPPLIER_SWITCH require a strictly positive,
    finite current_value. None, zero, negative, NaN, and infinity are
    rejected because the critic cannot safely establish the percentage
    bound.

    MENU_SWAP is intentionally approved without a numeric bound. This is
    a known limitation, not an accidental omission.
    """
    if not isinstance(action_type, ActionType):
        return _blocked(
            action_type,
            f"Unrecognized action type: {action_type}",
        )

    if not isinstance(payload, dict):
        return _blocked(
            action_type,
            "Action payload must be a dictionary.",
        )

    if action_type == ActionType.PRICE_CHANGE:
        baseline_error = _validate_positive_baseline(
            current_value,
            "price change",
            action_type,
        )
        if baseline_error is not None:
            return baseline_error

        new_price, payload_error = _validate_payload_number(
            payload,
            "new_price",
            action_type,
        )
        if payload_error is not None:
            return payload_error

        # current_value has been validated as a finite positive number.
        assert current_value is not None
        assert new_price is not None

        pct_change = abs(new_price - current_value) / current_value

        if pct_change > MAX_PRICE_CHANGE_PCT:
            return _blocked(
                action_type,
                f"Price change of {pct_change:.0%} exceeds the "
                f"{MAX_PRICE_CHANGE_PCT:.0%} hard limit, regardless "
                f"of simulator confidence.",
            )

        return _approved(
            action_type,
            "Within price-change bounds.",
        )

    if action_type == ActionType.SUPPLIER_SWITCH:
        baseline_error = _validate_positive_baseline(
            current_value,
            "supplier cost change",
            action_type,
        )
        if baseline_error is not None:
            return baseline_error

        new_price, payload_error = _validate_payload_number(
            payload,
            "new_supplier_price",
            action_type,
        )
        if payload_error is not None:
            return payload_error

        assert current_value is not None
        assert new_price is not None

        pct_change = (new_price - current_value) / current_value

        if pct_change > MAX_SUPPLIER_PRICE_INCREASE_PCT:
            return _blocked(
                action_type,
                f"New supplier cost is {pct_change:+.0%} vs. current "
                f"— exceeds the {MAX_SUPPLIER_PRICE_INCREASE_PCT:.0%} "
                f"increase limit.",
            )

        return _approved(
            action_type,
            "Within supplier cost-change bounds.",
        )

    if action_type == ActionType.STAFFING_CHANGE:
        if "headcount_delta" not in payload:
            return _blocked(
                action_type,
                "Missing required payload field: headcount_delta.",
            )

        delta = payload["headcount_delta"]

        if not _is_valid_numeric(delta):
            return _blocked(
                action_type,
                "Payload field headcount_delta must be a finite number.",
            )

        # Headcount is discrete. Accept integer-valued numeric types but
        # reject values such as 2.5 because they cannot represent a real
        # staffing delta.
        if isinstance(delta, float) and not delta.is_integer():
            return _blocked(
                action_type,
                f"Headcount change of {delta} must be an integer.",
            )

        if isinstance(delta, Decimal) and delta != delta.to_integral_value():
            return _blocked(
                action_type,
                f"Headcount change of {delta} must be an integer.",
            )

        integer_delta = int(delta)

        if abs(integer_delta) > MAX_HEADCOUNT_DELTA:
            return _blocked(
                action_type,
                f"Headcount change of {integer_delta:+d} exceeds the "
                f"+/-{MAX_HEADCOUNT_DELTA} hard limit.",
            )

        return _approved(
            action_type,
            "Within staffing-change bounds.",
        )

    if action_type == ActionType.MENU_SWAP:
        # Intentionally preserved current behavior.
        #
        # TODO(Component 4):
        # Once tool_framework supports creating menu items with an explicit
        # initial price, add a hard bound for MENU_SWAP "add" actions.
        # Suggested policy: require the new item's initial price to satisfy
        # an explicit absolute/relative bound before execution.
        return _approved(
            action_type,
            "Menu changes are not currently bounded by the critic — "
            "see limitations.",
        )

    return _blocked(
        action_type,
        f"Unrecognized action type: {action_type}",
    )
