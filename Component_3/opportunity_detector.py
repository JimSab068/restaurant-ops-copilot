"""Deterministic, tenant-scoped operational opportunity detection.

The detector is deliberately read-only.  It turns current operational state
into auditable facts; Component 3 reasoning may propose actions from those
facts, but Component 2 simulation and later policy gates retain authority.
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


class OpportunityDetectorError(ValueError):
    """Raised when detector input violates its public contract."""


def _tenant_id(tenant_id: str) -> str:
    try:
        return validate_tenant_id(tenant_id)
    except (TypeError, ValueError) as exc:
        raise OpportunityDetectorError(str(exc)) from exc


def _money(value: Decimal | float) -> float:
    return round(float(value), 2)


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


def _demand_by_menu_item(session, tenant_id: str) -> dict[str, int]:
    rows = (
        session.query(OrderVolume.menu_item_id, func.coalesce(func.sum(OrderVolume.quantity), 0))
        .filter(OrderVolume.tenant_id == tenant_id)
        .group_by(OrderVolume.menu_item_id)
        .all()
    )
    return {menu_item_id: int(quantity) for menu_item_id, quantity in rows}


def _margin_opportunities(session, tenant_id: str) -> Iterable[Opportunity]:
    costs = _recipe_costs(session, tenant_id)
    demand = _demand_by_menu_item(session, tenant_id)
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
        units = max(demand.get(item.id, 0), 1)
        monthly_loss = max(Decimal("0"), TARGET_CONTRIBUTION_MARGIN * price - (price - cost)) * units
        yield Opportunity(
            id=_opportunity_id(OpportunityType.MARGIN_DETERIORATION, item.id),
            tenant_id=tenant_id,
            opportunity_type=OpportunityType.MARGIN_DETERIORATION,
            title=f"{item.name} contribution margin is below target",
            cause="Recipe-derived ingredient cost exceeds the configured contribution-margin target.",
            affected_entity_ids=(item.id,),
            estimated_monthly_impact=-_money(monthly_loss),
            confidence=0.85 if demand.get(item.id, 0) else 0.65,
            evidence={"menu_item_id": item.id, "current_price": _money(price), "recipe_cost": _money(cost), "contribution_margin_pct": round(float(margin), 4), "target_contribution_margin_pct": float(TARGET_CONTRIBUTION_MARGIN), "demand_units": units},
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
        lost_revenue = Decimal(str(item.current_price)) * Decimal(baseline - recent)
        yield Opportunity(
            id=_opportunity_id(OpportunityType.DEMAND_DECLINE, item.id), tenant_id=tenant_id,
            opportunity_type=OpportunityType.DEMAND_DECLINE,
            title=f"{item.name} demand declined materially",
            cause="Recent order volume is materially below the preceding equal-length baseline.",
            affected_entity_ids=(item.id,), estimated_monthly_impact=-_money(lost_revenue), confidence=0.75,
            evidence={"menu_item_id": item.id, "current_price": _money(Decimal(str(item.current_price))), "recent_units": recent, "baseline_units": baseline, "demand_change_pct": round(-decline, 4), "window_days": LOOKBACK_DAYS},
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
        opportunities = [*_margin_opportunities(session, tenant_id), *_stockout_opportunities(session, tenant_id), *_demand_opportunities(session, tenant_id, now)]
    return sorted(opportunities, key=lambda item: (-abs(item.estimated_monthly_impact), -item.confidence, item.id))
