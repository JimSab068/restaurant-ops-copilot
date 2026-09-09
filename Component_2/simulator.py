"""
Simulator — Component 2.

Answers the counterfactual question:

    "What happens if we do X?"

The simulator is intentionally a heuristic/statistical model, not a trained
ML model. It uses explicit assumptions and currently available operational
data from Component 1:

    MenuItem
        └── Recipe
              └── RecipeIngredient
                    └── Ingredient

    Ingredient
        └── Supplier / SupplierSKU / SupplierPrice

    MenuItem
        └── historical OrderVolume

The goal is not to pretend that synthetic data produces precise predictions.
The goal is to provide an inspectable prediction contract that can later be
calibrated against real observed outcomes.

Important architectural boundary:

    Simulator confidence != authorization to execute.

The simulator reports prediction reliability. Policy/autonomy decisions belong
to later components.

Security invariants:

    - Tenant IDs are validated using Component 1's canonical validator.
    - Database-backed simulations use get_db_context(tenant_id).
    - Database queries explicitly constrain tenant_id.
    - Cross-tenant dependency traversal is rejected by tenant-scoped queries.
    - Invalid, NaN, and infinite numeric inputs are rejected.
    - Predictions are validated before being returned.
    - Unknown action types are rejected.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import (
    ActionType,
    Ingredient,
    InventoryLevel,
    MenuItem,
    OrderVolume,
    Recipe,
    RecipeIngredient,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Explicit heuristic assumptions
# ---------------------------------------------------------------------------

# Demand elasticity is an explicit model assumption. A future calibration
# process can replace this with a fitted value.
PRICE_ELASTICITY = -1.2

# Used only when no recipe data exists. It is intentionally conservative and
# accompanied by reduced confidence. It is not the primary food-cost model.
FALLBACK_FOOD_COST_RATIO = 0.30

# Supplier reliability is currently heuristic because there is no historical
# supplier-performance dataset in the synthetic environment.
SUPPLIER_SWITCH_RELIABILITY_DELTA = 0.15
SUPPLIER_SWITCH_BASE_RISK = 0.10

MENU_REMOVAL_DEMAND_TRANSFER = 0.60

STAFFING_CHANGE_CAPACITY_FACTOR = 0.08
STAFFING_MIN_RISK = 0.02
STAFFING_MAX_RISK = 0.90

BASELINE_STOCKOUT_RISK = 0.05

VALID_MENU_ACTIONS = frozenset({"add", "remove"})


class SimulatorInputError(ValueError):
    """Raised when simulator input is invalid."""


@dataclass(frozen=True)
class PredictedOutcome:
    """
    Stable prediction contract consumed by later Component 2 layers.

    Percentage values are represented as decimal fractions.

    Examples:
        0.05  == +5%
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
    """
    Reuse Component 1's canonical tenant validator.

    Tenant identity is a security boundary, so Component 2 must not maintain
    a weaker independent validation implementation.
    """
    try:
        return validate_tenant_id(tenant_id)
    except (TypeError, ValueError) as exc:
        raise SimulatorInputError(str(exc)) from exc


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
    """Convert a numeric value to a finite positive Decimal."""
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


def _to_nonnegative_decimal(
    value: Any,
    field_name: str,
) -> Decimal:
    """Convert a numeric value to a finite non-negative Decimal."""
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

    if decimal_value < 0:
        raise SimulatorInputError(
            f"{field_name} must not be negative"
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


def _decimal(value: Any, field_name: str) -> Decimal:
    """Safely convert a database numeric value to finite Decimal."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimulatorInputError(
            f"{field_name} is invalid"
        ) from exc

    if not result.is_finite():
        raise SimulatorInputError(
            f"{field_name} must be finite"
        )

    return result


# ---------------------------------------------------------------------------
# Operational-state helpers
# ---------------------------------------------------------------------------


def _load_recipe_cost(
    session,
    tenant_id: str,
    menu_item_id: str,
) -> Decimal | None:
    """
    Calculate current recipe food cost for a menu item.

    Cost is derived from:

        RecipeIngredient.quantity × Ingredient.current_price

    Only tenant-matching rows are considered. If no active recipe exists,
    None is returned so the caller can explicitly lower confidence rather
    than pretending the cost is known.
    """
    rows = (
        session.query(
            Recipe.id,
            RecipeIngredient.quantity,
            Ingredient.current_price,
        )
        .join(
            RecipeIngredient,
            (
                RecipeIngredient.recipe_id == Recipe.id
                )
                & (
                    RecipeIngredient.tenant_id == Recipe.tenant_id
                ),
        )
        .join(
            Ingredient,
            (
                Ingredient.id == RecipeIngredient.ingredient_id
                )
                & (
                    Ingredient.tenant_id == RecipeIngredient.tenant_id
                ),
        )
        .filter(
            Recipe.tenant_id == tenant_id,
            Recipe.menu_item_id == menu_item_id,
            Recipe.active.is_(True),
            RecipeIngredient.tenant_id == tenant_id,
            Ingredient.tenant_id == tenant_id,
        )
        .all()
    )

    if not rows:
        return None

    total_cost = Decimal("0")

    for _, quantity, current_price in rows:
        quantity_decimal = _decimal(
            quantity,
            "recipe ingredient quantity",
        )
        price_decimal = _decimal(
            current_price,
            "ingredient current price",
        )

        if quantity_decimal <= 0:
            raise SimulatorInputError(
                "recipe ingredient quantity must be greater than zero"
            )

        if price_decimal < 0:
            raise SimulatorInputError(
                "ingredient current price must not be negative"
            )

        total_cost += quantity_decimal * price_decimal

    return total_cost


def _load_demand_baseline(
    session,
    tenant_id: str,
    menu_item_id: str,
) -> Decimal | None:
    """
    Calculate average historical demand from OrderVolume.

    OrderVolume is currently the Component 2-compatible demand projection.
    Returning None means there is insufficient historical demand data.
    """
    rows = (
        session.query(OrderVolume.quantity)
        .filter(
            OrderVolume.tenant_id == tenant_id,
            OrderVolume.menu_item_id == menu_item_id,
        )
        .all()
    )

    if not rows:
        return None

    quantities: list[Decimal] = []

    for (quantity,) in rows:
        quantity_decimal = _decimal(
            quantity,
            "historical demand quantity",
        )

        if quantity_decimal <= 0:
            continue

        quantities.append(quantity_decimal)

    if not quantities:
        return None

    return sum(quantities, Decimal("0")) / Decimal(len(quantities))


def _load_inventory_level(
    session,
    tenant_id: str,
    ingredient_id: str,
) -> Decimal | None:
    """Load tenant-scoped current inventory for an ingredient."""
    row = (
        session.query(InventoryLevel.quantity)
        .filter(
            InventoryLevel.tenant_id == tenant_id,
            InventoryLevel.ingredient_id == ingredient_id,
        )
        .one_or_none()
    )

    if row is None:
        return None

    return _decimal(
        row[0],
        "inventory quantity",
    )


def _validate_menu_item(
    session,
    tenant_id: str,
    menu_item_id: str,
) -> MenuItem:
    """Load a menu item using explicit tenant scoping."""
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

    return item


def _validate_ingredient(
    session,
    tenant_id: str,
    ingredient_id: str,
) -> Ingredient:
    """Load an ingredient using explicit tenant scoping."""
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

    return ingredient


# ---------------------------------------------------------------------------
# Price simulation
# ---------------------------------------------------------------------------


def simulate_price_change(
    tenant_id: str,
    menu_item_id: str,
    new_price: Any,
) -> PredictedOutcome:
    """
    Predict the effect of changing a menu item's price.

    Demand uses the explicit elasticity assumption.

    Margin uses recipe-derived food cost when available. If recipe data is
    unavailable, a clearly marked fallback cost ratio is used and confidence
    is reduced.
    """
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
        item = _validate_menu_item(
            session,
            tenant_id,
            menu_item_id,
        )

        old_price_decimal = _decimal(
            item.current_price,
            "menu item current price",
        )

        if old_price_decimal <= 0:
            raise SimulatorInputError(
                "menu item current price must be greater than zero"
            )

        pct_change = (
            (new_price_decimal - old_price_decimal)
            / old_price_decimal
        )

        demand_change_pct = (
            Decimal(str(PRICE_ELASTICITY))
            * pct_change
        )

        recipe_cost = _load_recipe_cost(
            session,
            tenant_id,
            menu_item_id,
        )

        historical_demand = _load_demand_baseline(
            session,
            tenant_id,
            menu_item_id,
        )

        # ---------------------------------------------------------------
        # Margin model
        # ---------------------------------------------------------------

        confidence = max(
            Decimal("0.30"),
            Decimal("1.0")
            - abs(pct_change) * Decimal("1.5"),
        )

        if recipe_cost is not None:
            old_contribution = (
                old_price_decimal - recipe_cost
            )
            new_contribution = (
                new_price_decimal - recipe_cost
            )

            if old_contribution > 0:
                baseline_demand = (
                    historical_demand
                    if historical_demand is not None
                    else Decimal("1")
                )

                predicted_demand = (
                    baseline_demand
                    * (
                        Decimal("1")
                        + demand_change_pct
                    )
                )

                baseline_value = (
                    old_contribution
                    * baseline_demand
                )

                predicted_value = (
                    new_contribution
                    * max(
                        Decimal("0"),
                        predicted_demand,
                    )
                )

                if baseline_value > 0:
                    margin_change_pct = (
                        (
                            predicted_value
                            - baseline_value
                        )
                        / baseline_value
                    )
                else:
                    margin_change_pct = pct_change
            else:
                # A menu item already below food-cost contribution is a
                # poorly modeled case. Do not manufacture precision.
                margin_change_pct = pct_change
                confidence = min(
                    confidence,
                    Decimal("0.45"),
                )

            rationale_cost = (
                f"Recipe-derived food cost {recipe_cost:.2f}"
            )

        else:
            # Compatibility path for incomplete synthetic data.
            estimated_cost = (
                old_price_decimal
                * Decimal(str(FALLBACK_FOOD_COST_RATIO))
            )

            old_contribution = (
                old_price_decimal - estimated_cost
            )
            new_contribution = (
                new_price_decimal - estimated_cost
            )

            if old_contribution > 0:
                baseline_demand = (
                    historical_demand
                    if historical_demand is not None
                    else Decimal("1")
                )

                predicted_demand = (
                    baseline_demand
                    * (
                        Decimal("1")
                        + demand_change_pct
                    )
                )

                baseline_value = (
                    old_contribution
                    * baseline_demand
                )

                predicted_value = (
                    new_contribution
                    * max(
                        Decimal("0"),
                        predicted_demand,
                    )
                )

                margin_change_pct = (
                    (
                        predicted_value
                        - baseline_value
                    )
                    / baseline_value
                )
            else:
                margin_change_pct = pct_change

            confidence = min(
                confidence,
                Decimal("0.55"),
            )

            rationale_cost = (
                "Recipe data unavailable; "
                f"fallback food-cost ratio "
                f"{FALLBACK_FOOD_COST_RATIO:.0%} used"
            )

        demand_source = (
            "historical OrderVolume baseline"
            if historical_demand is not None
            else "unit-demand fallback because no historical demand exists"
        )

        return PredictedOutcome(
            margin_change_pct=round(
                float(margin_change_pct),
                4,
            ),
            demand_change_pct=round(
                float(demand_change_pct),
                4,
            ),
            stockout_risk=BASELINE_STOCKOUT_RISK,
            confidence=round(
                float(confidence),
                3,
            ),
            rationale=(
                f"Price {old_price_decimal} -> "
                f"{new_price_decimal} ({float(pct_change):+.1%}). "
                f"Assumed elasticity {PRICE_ELASTICITY} implies "
                f"demand {float(demand_change_pct):+.1%}; "
                f"{rationale_cost}. "
                f"Demand basis: {demand_source}."
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
    """
    Predict the effect of changing an ingredient's supplier cost.

    The public contract remains compatible with the previous simulator:
    callers provide the proposed unit price.

    Current economics are propagated through the ingredient's active recipes
    rather than assuming every ingredient represents the same percentage of
    menu price.
    """
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
        ingredient = _validate_ingredient(
            session,
            tenant_id,
            ingredient_id,
        )

        old_price_decimal = _decimal(
            ingredient.current_price,
            "ingredient current price",
        )

        if old_price_decimal <= 0:
            raise SimulatorInputError(
                "ingredient current price must be greater than zero"
            )

        cost_pct_change = (
            (
                new_price_decimal
                - old_price_decimal
            )
            / old_price_decimal
        )

        # Find active recipe usages for this ingredient.
        usages = (
            session.query(
                Recipe.menu_item_id,
                RecipeIngredient.quantity,
                MenuItem.current_price,
            )
            .join(
                RecipeIngredient,
                (
                    RecipeIngredient.recipe_id == Recipe.id
                )
                & (
                    RecipeIngredient.tenant_id
                    == Recipe.tenant_id
                ),
            )
            .join(
                MenuItem,
                (
                    MenuItem.id == Recipe.menu_item_id
                )
                & (
                    MenuItem.tenant_id == Recipe.tenant_id
                ),
            )
            .filter(
                Recipe.tenant_id == tenant_id,
                RecipeIngredient.tenant_id == tenant_id,
                RecipeIngredient.ingredient_id == ingredient_id,
                Recipe.active.is_(True),
                MenuItem.tenant_id == tenant_id,
                MenuItem.active.is_(True),
            )
            .all()
        )

        total_margin_impact = Decimal("0")
        total_baseline_value = Decimal("0")

        for menu_item_id, quantity, menu_price in usages:
            quantity_decimal = _decimal(
                quantity,
                "recipe ingredient quantity",
            )
            menu_price_decimal = _decimal(
                menu_price,
                "menu item current price",
            )

            demand = _load_demand_baseline(
                session,
                tenant_id,
                menu_item_id,
            )

            if demand is None:
                demand = Decimal("1")

            # Cost delta per menu item.
            cost_delta_per_unit = (
                quantity_decimal
                * (
                    new_price_decimal
                    - old_price_decimal
                )
            )

            contribution_delta = -cost_delta_per_unit

            total_margin_impact += (
                contribution_delta * demand
            )
            total_baseline_value += (
                menu_price_decimal * demand
            )

        if total_baseline_value > 0:
            margin_change_pct = (
                total_margin_impact
                / total_baseline_value
            )
        else:
            # Ingredient isn't currently represented in an active menu
            # recipe. The switch has no modeled menu-margin effect.
            margin_change_pct = Decimal("0")

        # Supplier reliability remains intentionally heuristic.
        stockout_risk = min(
            Decimal("1.0"),
            Decimal(str(SUPPLIER_SWITCH_BASE_RISK))
            + Decimal(
                str(SUPPLIER_SWITCH_RELIABILITY_DELTA)
            ),
        )

        confidence = Decimal("0.50")

        inventory_level = _load_inventory_level(
            session,
            tenant_id,
            ingredient_id,
        )

        inventory_note = ""

        if inventory_level is not None and inventory_level <= 0:
            stockout_risk = min(
                Decimal("1.0"),
                stockout_risk + Decimal("0.10"),
            )
            confidence = min(
                confidence,
                Decimal("0.40"),
            )
            inventory_note = (
                " Current inventory is depleted, increasing uncertainty."
            )

        return PredictedOutcome(
            margin_change_pct=round(
                float(margin_change_pct),
                4,
            ),
            demand_change_pct=0.0,
            stockout_risk=round(
                float(stockout_risk),
                3,
            ),
            confidence=float(confidence),
            rationale=(
                f"{ingredient.name} cost "
                f"{old_price_decimal} -> "
                f"{new_price_decimal} "
                f"({float(cost_pct_change):+.1%}). "
                "Margin impact is propagated through active recipes. "
                f"Unproven supplier adds "
                f"{SUPPLIER_SWITCH_RELIABILITY_DELTA:.0%} "
                f"stockout-risk proxy.{inventory_note}"
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
        item = _validate_menu_item(
            session,
            tenant_id,
            menu_item_id,
        )

        if action == "remove":
            demand_change_pct = -(
                1.0 - MENU_REMOVAL_DEMAND_TRANSFER
            )

            return PredictedOutcome(
                margin_change_pct=0.0,
                demand_change_pct=round(
                    demand_change_pct,
                    4,
                ),
                stockout_risk=BASELINE_STOCKOUT_RISK,
                confidence=0.40,
                rationale=(
                    f"Removing '{item.name}'. Assumed "
                    f"{MENU_REMOVAL_DEMAND_TRANSFER:.0%} of its "
                    "demand transfers to other menu items. "
                    "Substitute mix and resulting margin effects "
                    "remain unmodeled."
                ),
            )

        return PredictedOutcome(
            margin_change_pct=0.0,
            demand_change_pct=0.05,
            stockout_risk=BASELINE_STOCKOUT_RISK,
            confidence=0.25,
            rationale=(
                f"Adding '{item.name}'. Launch demand, "
                "substitution, and margin effects are largely "
                "unknown before observing real sales."
            ),
        )


# ---------------------------------------------------------------------------
# Staffing simulation
# ---------------------------------------------------------------------------


def simulate_staffing_change(
    tenant_id: str,
    headcount_delta: int,
) -> PredictedOutcome:
    """
    Predict the operational effect of a staffing change.

    Staffing remains deliberately weakly modeled. It is a transparent
    capacity/labor-cost proxy rather than a learned workforce model.
    """
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
            "Uses a rough labor-cost and capacity proxy only. "
            "Staffing remains the weakest-modeled action type."
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