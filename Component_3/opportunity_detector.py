
"""Deterministic, tenant-scoped operational opportunity detection.

The detector is deliberately read-only.  It turns current operational state
into auditable facts; Component 3 reasoning may propose actions from those
facts, but Component 2 simulation and later policy gates retain authority.

Economic-impact convention
--------------------------
`Opportunity.estimated_monthly_impact` is a MONTHLY figure, normalized to
`MONTH_DAYS`. Every observation feeding it is read from an explicit
trailing window (`LOOKBACK_DAYS`) and scaled to that month length via
`_to_monthly()`.

This matters because the figure is consumed downstream as if it were
authoritative: it ranks opportunities against each other, and it is
serialized into the bounded context handed to the LLM reasoner
(reasoning_context.py). An unnormalized or unbounded figure would be a
number the LLM reasons over confidently while being silently wrong.

If you add a new opportunity type, bound its observation window
explicitly and normalize through `_to_monthly()` — never sum an
unbounded table and label the result monthly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import Ingredient, InventoryLevel, MenuItem, OrderVolume, Recipe, RecipeIngredient

from .models import Opportunity, OpportunityType


MIN_MARGIN_SHORTFALL = Decimal("0.05")
TARGET_CONTRIBUTION_MARGIN = Decimal("0.35")
STOCKOUT_QUANTITY_THRESHOLD = Decimal("1")
DEMAND_DECLINE_THRESHOLD = Decimal("0.20")
LOOKBACK_DAYS = 14

# Nominal month length used to normalize every windowed observation into
# the monthly figure reported by estimated_monthly_impact.
MONTH_DAYS = 30


class OpportunityDetectorError(ValueError):
    """Raised when detector input violates its public contract."""


def _tenant_id(tenant_id: str) -> str:
    try:
        return validate_tenant_id(tenant_id)
    except (TypeError, ValueError) as exc:
        raise OpportunityDetectorError(str(exc)) from exc


def _money(value: Decimal | float) -> float:
    return round(float(value), 2)


def _to_monthly(
    value: Decimal,
    window_days: int = LOOKBACK_DAYS,
) -> Decimal:
    """
    Scale a quantity observed over `window_days` to a MONTH_DAYS month.

    Keeping this as one helper means the normalization convention is
    stated in exactly one place; every impact figure in this module
    passes through here, so a change to MONTH_DAYS or the window cannot
    leave one opportunity type on a different basis than the others.
    """
    if window_days <= 0:
        raise OpportunityDetectorError("window_days must be positive")

    return value * Decimal(MONTH_DAYS) / Decimal(window_days)


def _opportunity_id(kind: OpportunityType, entity_id: str) -> str:
    """Stable IDs prevent duplicate recommendations in repeated polling."""
    return f"{kind.value}:{entity_id}"


def _recipe_costs(session, tenant_id: str) -> dict[str, Decimal]:
    rows = (
        session.query(
            Recipe.menu_item_id,
            RecipeIngredient.quantity,
            Ingredient.current_price,
        )
        .join(RecipeIngredient, (RecipeIngredient.recipe_id == Recipe.id) & (RecipeIngredient.tenant_id == Recipe.tenant_id))
        .join(Ingredient, (Ingredient.id == RecipeIngredient.ingredient_id) & (Ingredient.tenant_id == RecipeIngredient.tenant_id))
        .filter(
            Recipe.tenant_id == tenant_id,
            Recipe.active.is_(True),
            RecipeIngredient.tenant_id == tenant_id,
            Ingredient.tenant_id == tenant_id,
        )
        .all()
    )
    costs: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for menu_item_id, quantity, unit_price in rows:
        costs[menu_item_id] += Decimal(str(quantity)) * Decimal(str(unit_price))
    return dict(costs)


def _demand_by_menu_item(
    session,
    tenant_id: str,
    now: datetime,
) -> dict[str, int]:
    """
    Return units sold per menu item over the trailing LOOKBACK_DAYS window.

    The time bound is load-bearing, not incidental. Without it this sums
    every OrderVolume row the tenant has ever recorded, so an older
    restaurant would show a larger "monthly" demand than a newer one
    purely as a function of how long it has been on the platform — and
    that inflated unit count would multiply straight through into
    estimated_monthly_impact.

    Callers are responsible for normalizing the returned window counts to
    a month via _to_monthly().
    """
    window_start = now - timedelta(days=LOOKBACK_DAYS)

    rows = (
        session.query(OrderVolume.menu_item_id, func.coalesce(func.sum(OrderVolume.quantity), 0))
        .filter(
            OrderVolume.tenant_id == tenant_id,
            OrderVolume.timestamp >= window_start,
        )
        .group_by(OrderVolume.menu_item_id)
        .all()
    )
    return {menu_item_id: int(quantity) for menu_item_id, quantity in rows}


def _margin_opportunities(
    session,
    tenant_id: str,
    now: datetime,
) -> Iterable[Opportunity]:
    costs = _recipe_costs(session, tenant_id)
    demand = _demand_by_menu_item(session, tenant_id, now)
    items = session.query(MenuItem).filter(MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True)).all()
    for item in items:
        cost = costs.get(item.id)
        price = Decimal(str(item.current_price))
        if cost is None or price <= 0:
            continue
        margin = (price - cost) / price
        shortfall = TARGET_CONTRIBUTION_MARGIN - margin
        if shortfall < MIN_MARGIN_SHORTFALL:
            continue

        window_units = demand.get(item.id, 0)

        # Floor at one unit per window so a real margin problem on an
        # item with no recorded sales is still surfaced, rather than
        # multiplied to zero impact and ranked last.
        monthly_units = _to_monthly(Decimal(max(window_units, 1)))

        per_unit_shortfall = max(
            Decimal("0"),
            TARGET_CONTRIBUTION_MARGIN * price - (price - cost),
        )
        monthly_loss = per_unit_shortfall * monthly_units

        yield Opportunity(
            id=_opportunity_id(OpportunityType.MARGIN_DETERIORATION, item.id),
            tenant_id=tenant_id,
            opportunity_type=OpportunityType.MARGIN_DETERIORATION,
            title=f"{item.name} contribution margin is below target",
            cause="Recipe-derived ingredient cost exceeds the configured contribution-margin target.",
            affected_entity_ids=(item.id,),
            estimated_monthly_impact=-_money(monthly_loss),
            confidence=0.85 if window_units else 0.65,
            evidence={
                "menu_item_id": item.id,
                "current_price": _money(price),
                "recipe_cost": _money(cost),
                "contribution_margin_pct": round(float(margin), 4),
                "target_contribution_margin_pct": float(TARGET_CONTRIBUTION_MARGIN),
                "observed_units": window_units,
                "observed_window_days": LOOKBACK_DAYS,
                "estimated_monthly_units": _money(monthly_units),
            },
        )


def _stockout_opportunities(session, tenant_id: str) -> Iterable[Opportunity]:
    rows = (
        session.query(Ingredient, InventoryLevel.quantity)
        .join(InventoryLevel, (InventoryLevel.ingredient_id == Ingredient.id) & (InventoryLevel.tenant_id == Ingredient.tenant_id))
        .filter(Ingredient.tenant_id == tenant_id, InventoryLevel.tenant_id == tenant_id, InventoryLevel.quantity <= STOCKOUT_QUANTITY_THRESHOLD)
        .all()
    )
    for ingredient, quantity in rows:
        affected = (
            session.query(Recipe.menu_item_id)
            .join(RecipeIngredient, (RecipeIngredient.recipe_id == Recipe.id) & (RecipeIngredient.tenant_id == Recipe.tenant_id))
            .join(MenuItem, (MenuItem.id == Recipe.menu_item_id) & (MenuItem.tenant_id == Recipe.tenant_id))
            .filter(Recipe.tenant_id == tenant_id, RecipeIngredient.tenant_id == tenant_id, RecipeIngredient.ingredient_id == ingredient.id, Recipe.active.is_(True), MenuItem.active.is_(True))
            .all()
        )
        menu_item_ids = tuple(row[0] for row in affected)
        if not menu_item_ids:
            continue
        yield Opportunity(
            id=_opportunity_id(OpportunityType.STOCKOUT_EXPOSURE, ingredient.id),
            tenant_id=tenant_id,
            opportunity_type=OpportunityType.STOCKOUT_EXPOSURE,
            title=f"{ingredient.name} is at stockout exposure",
            cause="Current inventory is at or below the configured low-stock threshold.",
            affected_entity_ids=(ingredient.id, *menu_item_ids),
            # Stockout exposure is a risk condition, not a measured loss.
            # Reporting 0.0 keeps it honest rather than fabricating an
            # impact figure the LLM would then reason over as fact.
            estimated_monthly_impact=0.0,
            confidence=0.9,
            evidence={"ingredient_id": ingredient.id, "inventory_quantity": _money(Decimal(str(quantity))), "threshold": float(STOCKOUT_QUANTITY_THRESHOLD), "affected_menu_item_ids": list(menu_item_ids)},
        )


def _demand_opportunities(session, tenant_id: str, now: datetime) -> Iterable[Opportunity]:
    recent_start = now - timedelta(days=LOOKBACK_DAYS)
    baseline_start = recent_start - timedelta(days=LOOKBACK_DAYS)
    items = session.query(MenuItem).filter(MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True)).all()
    for item in items:
        recent = session.query(func.coalesce(func.sum(OrderVolume.quantity), 0)).filter(OrderVolume.tenant_id == tenant_id, OrderVolume.menu_item_id == item.id, OrderVolume.timestamp >= recent_start).scalar()
        baseline = session.query(func.coalesce(func.sum(OrderVolume.quantity), 0)).filter(OrderVolume.tenant_id == tenant_id, OrderVolume.menu_item_id == item.id, OrderVolume.timestamp >= baseline_start, OrderVolume.timestamp < recent_start).scalar()
        recent, baseline = int(recent), int(baseline)
        if baseline <= 0:
            continue
        decline = (baseline - recent) / baseline
        if decline < float(DEMAND_DECLINE_THRESHOLD):
            continue

        # Units lost across one LOOKBACK_DAYS window, scaled to a month
        # so this is comparable with the margin figures above.
        window_lost_revenue = Decimal(str(item.current_price)) * Decimal(baseline - recent)
        monthly_lost_revenue = _to_monthly(window_lost_revenue)

        yield Opportunity(
            id=_opportunity_id(OpportunityType.DEMAND_DECLINE, item.id), tenant_id=tenant_id,
            opportunity_type=OpportunityType.DEMAND_DECLINE,
            title=f"{item.name} demand declined materially",
            cause="Recent order volume is materially below the preceding equal-length baseline.",
            affected_entity_ids=(item.id,),
            estimated_monthly_impact=-_money(monthly_lost_revenue),
            confidence=0.75,
            evidence={
                "menu_item_id": item.id,
                "current_price": _money(Decimal(str(item.current_price))),
                "recent_units": recent,
                "baseline_units": baseline,
                "demand_change_pct": round(-decline, 4),
                "window_days": LOOKBACK_DAYS,
                "window_lost_revenue": _money(window_lost_revenue),
            },
        )


def detect_opportunities(tenant_id: str, *, now: datetime | None = None) -> list[Opportunity]:
    """Return economically ranked opportunities visible to one tenant only."""
    tenant_id = _tenant_id(tenant_id)
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    elif now.tzinfo is None:
        raise OpportunityDetectorError("now must be timezone-aware")
    else:
        now = now.astimezone(timezone.utc)
    with get_db_context(tenant_id) as session:
        opportunities = [*_margin_opportunities(session, tenant_id, now), *_stockout_opportunities(session, tenant_id), *_demand_opportunities(session, tenant_id, now)]
    return sorted(opportunities, key=lambda item: (-abs(item.estimated_monthly_impact), -item.confidence, item.id))