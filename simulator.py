"""
Simulator — Component 2's core: "what happens if I do X" -> a structured,
confidence-scored prediction.

HONEST DESIGN NOTE (read this before treating the numbers below as real):
This is a heuristic/statistical simulator, not a trained ML model. There's
no historical real-world restaurant data to fit a model on, so pretending
to have one would be dishonest. Instead, every prediction is derived from
explicit, stated assumptions (elasticity coefficients, cost ratios, etc.)
that are clearly documented here so they can be inspected, challenged, and
replaced with fitted values once real data exists (see event_store's
reconstruct_state_at, which is exactly what a future fitting step would
read from).

The point of this component isn't "the numbers are right" — it's the
*structure*: propose -> predict -> compare to actual -> track calibration
over time. That loop is what's being demonstrated, and calibration.py is
what proves whether the heuristics are actually any good.

Architecture:

    Live State
        -> Agent proposal
        -> Simulator
        -> Confidence gate
        -> Policy/Critic
        -> Executor
        -> Actual outcome
        -> Calibration

Security invariants:

    - Database-backed simulations use get_db_context(tenant_id).
    - Database queries explicitly constrain tenant_id.
    - Invalid tenant identifiers are rejected.
    - Invalid, NaN, and infinite numeric inputs are rejected.
    - Predictions are validated before being returned.
    - ORM queries are parameterized; no SQL is constructed from user input.
    - Action dispatch rejects unknown action types.

"""


from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from db import get_db_context
from models import ActionType, Ingredient, MenuItem


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Explicit heuristic assumptions
# ---------------------------------------------------------------------------

PRICE_ELASTICITY = -1.2
ASSUMED_FOOD_COST_RATIO = 0.30
SUPPLIER_SWITCH_RELIABILITY_DELTA = 0.15
MENU_REMOVAL_DEMAND_TRANSFER = 0.60
STAFFING_CHANGE_CAPACITY_FACTOR = 0.08

BASELINE_STOCKOUT_RISK = 0.05
SUPPLIER_SWITCH_BASE_RISK = 0.10
STAFFING_MIN_RISK = 0.02
STAFFING_MAX_RISK = 0.90

VALID_MENU_ACTIONS = frozenset({"add", "remove"})


class SimulatorInputError(ValueError):
    """Raised when simulator input is invalid."""


@dataclass(frozen=True)
class PredictedOutcome:
    """
    Structured prediction returned by the simulator.

    All percentage values are represented as decimal fractions.

    Example:
        0.05 == +5%
        -0.10 == -10%
    """

    margin_change_pct: float
    demand_change_pct: float
    stockout_risk: float
    confidence: float
    rationale: str

    def __post_init__(self) -> None:
        numeric_fields = (
            "margin_change_pct",
            "demand_change_pct",
            "stockout_risk",
            "confidence",
        )

        for field_name in numeric_fields:
            value = getattr(self, field_name)

            if isinstance(value, bool):
                raise SimulatorInputError(
                    f"{field_name} must be numeric"
                )

            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                raise SimulatorInputError(
                    f"{field_name} must be numeric"
                ) from exc

            if not math.isfinite(numeric_value):
                raise SimulatorInputError(
                    f"{field_name} must be finite"
                )

        if not 0.0 <= float(self.stockout_risk) <= 1.0:
            raise SimulatorInputError(
                "stockout_risk must be between 0 and 1"
            )

        if not 0.0 <= float(self.confidence) <= 1.0:
            raise SimulatorInputError(
                "confidence must be between 0 and 1"
            )

        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise SimulatorInputError(
                "rationale must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON/persistence-friendly representation."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_tenant_id(tenant_id: str) -> str:
    """Validate tenant identity without silently changing its value."""
    if not isinstance(tenant_id, str):
        raise TypeError("tenant_id must be a string")

    tenant_id = tenant_id.strip()

    if not tenant_id:
        raise SimulatorInputError(
            "tenant_id must not be empty"
        )

    if "\x00" in tenant_id:
        raise SimulatorInputError(
            "tenant_id contains invalid null bytes"
        )

    return tenant_id


def _validate_identifier(
    value: str,
    field_name: str,
) -> str:
    """Validate an entity identifier before database access."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    value = value.strip()

    if not value:
        raise SimulatorInputError(
            f"{field_name} must not be empty"
        )

    if "\x00" in value:
        raise SimulatorInputError(
            f"{field_name} contains invalid null bytes"
        )

    return value


def _to_positive_decimal(
    value: Any,
    field_name: str,
) -> Decimal:
    """
    Convert an incoming numeric value to Decimal and require a
    finite positive value.
    """
    if isinstance(value, bool):
        raise SimulatorInputError(
            f"{field_name} must be numeric"
        )

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimulatorInputError(
            f"{field_name} must be a valid numeric value"
        ) from exc

    if not decimal_value.is_finite():
        raise SimulatorInputError(
            f"{field_name} must be finite"
        )

    if decimal_value <= 0:
        raise SimulatorInputError(
            f"{field_name} must be greater than zero"
        )

    return decimal_value


def _require_payload_value(
    payload: Mapping[str, Any],
    field_name: str,
) -> Any:
    """Retrieve a required action-payload field."""
    if field_name not in payload:
        raise SimulatorInputError(
            f"payload is missing required field: {field_name}"
        )

    return payload[field_name]


# ---------------------------------------------------------------------------
# Price simulation
# ---------------------------------------------------------------------------

def simulate_price_change(
    tenant_id: str,
    menu_item_id: str,
    new_price: Any,
) -> PredictedOutcome:
    """Predict the effect of changing a menu item's price."""
    tenant_id = _validate_tenant_id(tenant_id)
    menu_item_id = _validate_identifier(
        menu_item_id,
        "menu_item_id",
    )
    new_price_decimal = _to_positive_decimal(
        new_price,
        "new_price",
    )

    logger.info(
        "Simulating price change",
        extra={
            "tenant_id": tenant_id,
            "menu_item_id": menu_item_id,
        },
    )

    with get_db_context(tenant_id) as session:
        item = (
            session.query(MenuItem)
            .filter(
                MenuItem.id == menu_item_id,
                MenuItem.tenant_id == tenant_id,
            )
            .one_or_none()
        )

        if item is None:
            raise SimulatorInputError(
                "menu item not found for this tenant"
            )

        old_price = item.current_price

        if old_price is None:
            raise SimulatorInputError(
                "menu item current price must be greater than zero"
            )

        try:
            old_price_decimal = Decimal(str(old_price))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SimulatorInputError(
                "menu item current price is invalid"
            ) from exc

        if (
            not old_price_decimal.is_finite()
            or old_price_decimal <= 0
        ):
            raise SimulatorInputError(
                "menu item current price must be greater than zero"
            )

        pct_change = float(
            (new_price_decimal - old_price_decimal)
            / old_price_decimal
        )

        demand_change_pct = (
            PRICE_ELASTICITY * pct_change
        )

        margin_change_pct = (
            pct_change
            + demand_change_pct * ASSUMED_FOOD_COST_RATIO
        )

        confidence = max(
            0.3,
            1.0 - abs(pct_change) * 1.5,
        )

        return PredictedOutcome(
            margin_change_pct=round(
                margin_change_pct,
                4,
            ),
            demand_change_pct=round(
                demand_change_pct,
                4,
            ),
            stockout_risk=BASELINE_STOCKOUT_RISK,
            confidence=round(confidence, 3),
            rationale=(
                f"Price {old_price_decimal} -> "
                f"{new_price_decimal} ({pct_change:+.1%}). "
                f"Assumed elasticity {PRICE_ELASTICITY} "
                f"implies demand {demand_change_pct:+.1%}."
            ),
        )


# ---------------------------------------------------------------------------
# Supplier simulation
# ---------------------------------------------------------------------------

def simulate_supplier_switch(
    tenant_id: str,
    ingredient_id: str,
    new_supplier_price: Any,
) -> PredictedOutcome:
    """Predict the effect of switching an ingredient supplier."""
    tenant_id = _validate_tenant_id(tenant_id)
    ingredient_id = _validate_identifier(
        ingredient_id,
        "ingredient_id",
    )
    new_price_decimal = _to_positive_decimal(
        new_supplier_price,
        "new_supplier_price",
    )

    logger.info(
        "Simulating supplier switch",
        extra={
            "tenant_id": tenant_id,
            "ingredient_id": ingredient_id,
        },
    )

    with get_db_context(tenant_id) as session:
        ingredient = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == tenant_id,
            )
            .one_or_none()
        )

        if ingredient is None:
            raise SimulatorInputError(
                "ingredient not found for this tenant"
            )

        old_price = ingredient.current_price

        if old_price is None:
            raise SimulatorInputError(
                "ingredient current price must be greater than zero"
            )

        try:
            old_price_decimal = Decimal(str(old_price))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SimulatorInputError(
                "ingredient current price is invalid"
            ) from exc

        if (
            not old_price_decimal.is_finite()
            or old_price_decimal <= 0
        ):
            raise SimulatorInputError(
                "ingredient current price must be greater than zero"
            )

        # Keep the calculation entirely in Decimal until conversion
        # to float for the final prediction.
        cost_pct_change = float(
            (new_price_decimal - old_price_decimal)
            / old_price_decimal
        )

        margin_change_pct = (
            -cost_pct_change
            * ASSUMED_FOOD_COST_RATIO
        )

        stockout_risk = min(
            1.0,
            SUPPLIER_SWITCH_BASE_RISK
            + SUPPLIER_SWITCH_RELIABILITY_DELTA,
        )

        return PredictedOutcome(
            margin_change_pct=round(
                margin_change_pct,
                4,
            ),
            demand_change_pct=0.0,
            stockout_risk=round(
                stockout_risk,
                3,
            ),
            confidence=0.5,
            rationale=(
                f"{ingredient.name} cost "
                f"{old_price_decimal} -> {new_price_decimal} "
                f"({cost_pct_change:+.1%}). "
                f"New/unproven supplier assumed to raise "
                f"stockout risk by "
                f"{SUPPLIER_SWITCH_RELIABILITY_DELTA:.0%}."
            ),
        )


# ---------------------------------------------------------------------------
# Menu simulation
# ---------------------------------------------------------------------------

def simulate_menu_swap(
    tenant_id: str,
    menu_item_id: str,
    action: str,
) -> PredictedOutcome:
    """Predict the effect of adding or removing a menu item."""
    tenant_id = _validate_tenant_id(tenant_id)
    menu_item_id = _validate_identifier(
        menu_item_id,
        "menu_item_id",
    )

    if not isinstance(action, str):
        raise TypeError("action must be a string")

    action = action.strip().lower()

    if action not in VALID_MENU_ACTIONS:
        raise SimulatorInputError(
            "action must be either 'add' or 'remove'"
        )

    logger.info(
        "Simulating menu change",
        extra={
            "tenant_id": tenant_id,
            "menu_item_id": menu_item_id,
            "action": action,
        },
    )

    with get_db_context(tenant_id) as session:
        item = (
            session.query(MenuItem)
            .filter(
                MenuItem.id == menu_item_id,
                MenuItem.tenant_id == tenant_id,
            )
            .one_or_none()
        )

        if item is None:
            raise SimulatorInputError(
                "menu item not found for this tenant"
            )

        if action == "remove":
            retained_demand = MENU_REMOVAL_DEMAND_TRANSFER
            demand_change_pct = -(
                1.0 - retained_demand
            )

            return PredictedOutcome(
                margin_change_pct=0.0,
                demand_change_pct=round(
                    demand_change_pct,
                    4,
                ),
                stockout_risk=BASELINE_STOCKOUT_RISK,
                confidence=0.4,
                rationale=(
                    f"Removing '{item.name}'. Assumed "
                    f"{retained_demand:.0%} of its demand transfers "
                    f"to other menu items; margin effect is not "
                    f"modeled because substitute mix is unknown."
                ),
            )

        return PredictedOutcome(
            margin_change_pct=0.0,
            demand_change_pct=0.05,
            stockout_risk=BASELINE_STOCKOUT_RISK,
            confidence=0.25,
            rationale=(
                "Adding a new item. Effect on demand and margin "
                "is largely unknown before launch."
            ),
        )


# ---------------------------------------------------------------------------
# Staffing simulation
# ---------------------------------------------------------------------------

def simulate_staffing_change(
    tenant_id: str,
    headcount_delta: int,
) -> PredictedOutcome:
    """Predict the operational effect of a staffing change."""
    tenant_id = _validate_tenant_id(tenant_id)

    if isinstance(headcount_delta, bool):
        raise TypeError(
            "headcount_delta must be an integer"
        )

    if not isinstance(headcount_delta, int):
        raise TypeError(
            "headcount_delta must be an integer"
        )

    capacity_change_pct = (
        STAFFING_CHANGE_CAPACITY_FACTOR
        * headcount_delta
    )

    if headcount_delta >= 0:
        stockout_risk = max(
            STAFFING_MIN_RISK,
            BASELINE_STOCKOUT_RISK
            - capacity_change_pct * 0.3,
        )
    else:
        stockout_risk = min(
            STAFFING_MAX_RISK,
            BASELINE_STOCKOUT_RISK
            + abs(capacity_change_pct) * 0.5,
        )

    margin_change_pct = (
        -headcount_delta * 0.01
    )

    logger.info(
        "Simulating staffing change",
        extra={
            "tenant_id": tenant_id,
            "headcount_delta": headcount_delta,
        },
    )

    return PredictedOutcome(
        margin_change_pct=round(
            margin_change_pct,
            4,
        ),
        demand_change_pct=0.0,
        stockout_risk=round(
            stockout_risk,
            3,
        ),
        confidence=0.45,
        rationale=(
            f"Headcount change of {headcount_delta:+d}. "
            "Rough labor-cost and capacity proxy only — "
            "weakest-modeled action type, kept at capped confidence."
        ),
    )


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------

def simulate(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> PredictedOutcome:
    """
    Dispatch an action proposal to the appropriate simulator.

    The dispatcher validates the action type and payload shape before
    invoking the action-specific implementation.
    """
    tenant_id = _validate_tenant_id(tenant_id)

    if not isinstance(action_type, ActionType):
        raise TypeError(
            "action_type must be an ActionType"
        )

    if not isinstance(payload, Mapping):
        raise TypeError(
            "payload must be a mapping"
        )

    if action_type == ActionType.PRICE_CHANGE:
        return simulate_price_change(
            tenant_id,
            _require_payload_value(
                payload,
                "menu_item_id",
            ),
            _require_payload_value(
                payload,
                "new_price",
            ),
        )

    if action_type == ActionType.SUPPLIER_SWITCH:
        return simulate_supplier_switch(
            tenant_id,
            _require_payload_value(
                payload,
                "ingredient_id",
            ),
            _require_payload_value(
                payload,
                "new_supplier_price",
            ),
        )

    if action_type == ActionType.MENU_SWAP:
        return simulate_menu_swap(
            tenant_id,
            _require_payload_value(
                payload,
                "menu_item_id",
            ),
            payload.get("action", "remove"),
        )

    if action_type == ActionType.STAFFING_CHANGE:
        return simulate_staffing_change(
            tenant_id,
            _require_payload_value(
                payload,
                "headcount_delta",
            ),
        )

    raise SimulatorInputError(
        f"unsupported action type: {action_type}"
    )


