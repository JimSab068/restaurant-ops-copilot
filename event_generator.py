"""
Background event generator — produces the "restaurants are dynamic" behavior
the whole system depends on. Runs unattended, periodically triggering
believable events per tenant: supplier price changes, stockouts, demand
spikes, restocks.


Produces simulated operational events for each tenant: - supplier price changes - stockouts - restocks - demand spikes
Usage:
    python event_generator.py                 # run continuously
    python event_generator.py --once           # fire a single random round (for testing/demos)
"""


from __future__ import annotations

import argparse
import logging
import random
import time
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from db import get_db_context
from event_store import append_event
from models import Ingredient, MenuItem, Tenant, EventType


logger = logging.getLogger(__name__)


# Simulation tuning.
EVENT_INTERVAL_SECONDS = 5.0
EVENT_PROBABILITY = 0.4

PRICE_CHANGE_MIN = Decimal("-0.10")
PRICE_CHANGE_MAX = Decimal("0.20")

RESTOCK_MIN = Decimal("15.0")
RESTOCK_MAX = Decimal("60.0")

DEMAND_MULTIPLIER_MIN = Decimal("1.30")
DEMAND_MULTIPLIER_MAX = Decimal("3.00")


EventChoice = Callable[
    [Any, Tenant, Sequence[Ingredient], Sequence[MenuItem]],
    str | None,
]


class EventGenerationError(RuntimeError):
    """Raised when an event cannot be safely generated."""


def _validate_probability(probability: float) -> None:
    """Validate the global event probability before using it."""
    if not 0.0 <= probability <= 1.0:
        raise ValueError(
            f"EVENT_PROBABILITY must be between 0 and 1, got {probability!r}"
        )


def _get_tenants(session: Any) -> list[Tenant]:
    """Return all tenants.

    Tenant records are returned directly from the current DB session.
    No tenant data from another data source is accepted here.
    """
    return list(session.query(Tenant).all())


def _get_tenant_ingredients(
    session: Any,
    tenant_id: int,
) -> list[Ingredient]:
    """Return ingredients strictly scoped to one tenant."""
    if tenant_id is None:
        raise ValueError("tenant_id cannot be None")

    return list(
        session.query(Ingredient)
        .filter(Ingredient.tenant_id == tenant_id)
        .all()
    )


def _get_tenant_menu_items(
    session: Any,
    tenant_id: int,
) -> list[MenuItem]:
    """Return active menu items strictly scoped to one tenant."""
    if tenant_id is None:
        raise ValueError("tenant_id cannot be None")

    return list(
        session.query(MenuItem)
        .filter(
            MenuItem.tenant_id == tenant_id,
            MenuItem.active.is_(True),
        )
        .all()
    )


def _safe_decimal(
    value: Any,
    *,
    field_name: str,
    minimum: Decimal | None = None,
) -> Decimal:
    """Convert a DB value into a validated Decimal.

    Floating-point values are deliberately converted through ``str`` so
    binary floating-point representation does not leak into event values.
    """
    if value is None:
        raise EventGenerationError(
            f"{field_name} cannot be NULL"
        )

    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EventGenerationError(
            f"{field_name} is not a valid decimal: {value!r}"
        ) from exc

    if not result.is_finite():
        raise EventGenerationError(
            f"{field_name} must be finite: {value!r}"
        )

    if minimum is not None and result < minimum:
        raise EventGenerationError(
            f"{field_name} must be >= {minimum}, got {result}"
        )

    return result


def _choose_event_type() -> str:
    """Choose an event type using the configured simulation weights."""
    return random.choices(
        [
            "supplier_price_change",
            "stockout",
            "restock",
            "demand_spike",
        ],
        weights=[0.35, 0.20, 0.20, 0.25],
        k=1,
    )[0]


def _fire_random_event(
    session: Any,
    tenant: Tenant,
) -> str | None:
    """Generate and persist one event for exactly one tenant.

    All source data is explicitly tenant-scoped. This function never accepts
    an arbitrary ingredient/menu item from a caller, which prevents accidental
    cross-tenant event generation.
    """
    if tenant is None or tenant.id is None:
        raise ValueError("A persisted tenant with a valid id is required")

    tenant_id = tenant.id

    ingredients = _get_tenant_ingredients(session, tenant_id)
    menu_items = _get_tenant_menu_items(session, tenant_id)

    if not ingredients or not menu_items:
        logger.debug(
            "Skipping event generation for tenant_id=%s: "
            "ingredients=%d menu_items=%d",
            tenant_id,
            len(ingredients),
            len(menu_items),
        )
        return None

    choice = _choose_event_type()

    if choice == "supplier_price_change":
        ing = random.choice(ingredients)

        old_price = _safe_decimal(
            ing.current_price,
            field_name=f"ingredient[{ing.id}].current_price",
            minimum=Decimal("0"),
        )

        pct = Decimal(
            str(
                random.uniform(
                    float(PRICE_CHANGE_MIN),
                    float(PRICE_CHANGE_MAX),
                )
            )
        )

        new_price = (old_price * (Decimal("1") + pct)).quantize(
            Decimal("0.01")
        )

        if new_price < Decimal("0.00"):
            raise EventGenerationError(
                f"Generated negative price for ingredient_id={ing.id}"
            )

        append_event(
            tenant_id,
            EventType.SUPPLIER_PRICE_CHANGE,
            {
                "ingredient_id": ing.id,
                "ingredient_name": ing.name,
                "old_price": str(old_price),
                "new_price": str(new_price),
                "pct_change": str(pct.quantize(Decimal("0.001"))),
            },
        )

        logger.info(
            "Supplier price event generated: tenant_id=%s ingredient_id=%s "
            "old_price=%s new_price=%s",
            tenant_id,
            ing.id,
            old_price,
            new_price,
        )

        return (
            f"[{tenant.name}] {ing.name} price "
            f"{old_price} -> {new_price} ({pct:+.0%})"
        )

    if choice == "stockout":
        available = [
            ingredient
            for ingredient in ingredients
            if _safe_decimal(
                ingredient.current_stock_level,
                field_name=f"ingredient[{ingredient.id}].current_stock_level",
                minimum=Decimal("0"),
            )
            > Decimal("0")
        ]

        if not available:
            logger.debug(
                "Skipping stockout for tenant_id=%s: "
                "no ingredients currently in stock",
                tenant_id,
            )
            return None

        ing = random.choice(available)

        append_event(
            tenant_id,
            EventType.STOCKOUT,
            {
                "ingredient_id": ing.id,
                "ingredient_name": ing.name,
            },
        )

        logger.info(
            "Stockout event generated: tenant_id=%s ingredient_id=%s",
            tenant_id,
            ing.id,
        )

        return f"[{tenant.name}] STOCKOUT: {ing.name}"

    if choice == "restock":
        ing = random.choice(ingredients)

        new_level = Decimal(
            str(
                round(
                    random.uniform(
                        float(RESTOCK_MIN),
                        float(RESTOCK_MAX),
                    ),
                    1,
                )
            )
        )

        append_event(
            tenant_id,
            EventType.RESTOCK,
            {
                "ingredient_id": ing.id,
                "ingredient_name": ing.name,
                "new_stock_level": float(new_level),
            },
        )

        logger.info(
            "Restock event generated: tenant_id=%s ingredient_id=%s "
            "new_stock_level=%s",
            tenant_id,
            ing.id,
            new_level,
        )

        return f"[{tenant.name}] Restocked {ing.name} -> {new_level}"

    if choice == "demand_spike":
        item = random.choice(menu_items)

        magnitude = Decimal(
            str(
                round(
                    random.uniform(
                        float(DEMAND_MULTIPLIER_MIN),
                        float(DEMAND_MULTIPLIER_MAX),
                    ),
                    2,
                )
            )
        )

        append_event(
            tenant_id,
            EventType.DEMAND_SPIKE,
            {
                "menu_item_id": item.id,
                "menu_item_name": item.name,
                "demand_multiplier": float(magnitude),
            },
        )

        logger.info(
            "Demand spike generated: tenant_id=%s menu_item_id=%s "
            "multiplier=%s",
            tenant_id,
            item.id,
            magnitude,
        )

        return (
            f"[{tenant.name}] DEMAND SPIKE: "
            f"{item.name} x{magnitude}"
        )

    raise EventGenerationError(
        f"Unsupported generated event type: {choice!r}"
    )


def run_once() -> list[str]:
    """Run one generation cycle across all tenants.

    A failure for one tenant is isolated and logged; it does not prevent
    other tenants from receiving their events.
    """
    _validate_probability(EVENT_PROBABILITY)

    fired: list[str] = []

    try:
        with get_db_context() as session:
            tenants = _get_tenants(session)

            for tenant in tenants:
                if random.random() >= EVENT_PROBABILITY:
                    continue

                try:
                    message = _fire_random_event(session, tenant)

                    if message is not None:
                        fired.append(message)

                except (EventGenerationError, SQLAlchemyError, ValueError):
                    logger.exception(
                        "Failed to generate event for tenant_id=%s",
                        getattr(tenant, "id", None),
                    )

                    # Continue processing the remaining tenants.
                    continue

    except SQLAlchemyError:
        logger.exception("Failed to initialize or query event-generation DB")
        raise

    return fired


def run_forever() -> None:
    """Continuously generate events until interrupted."""
    logger.info(
        "Event generator started; interval=%ss probability=%s",
        EVENT_INTERVAL_SECONDS,
        EVENT_PROBABILITY,
    )

    try:
        while True:
            try:
                for message in run_once():
                    logger.info("%s", message)
            except SQLAlchemyError:
                # A transient database outage should not permanently kill
                # the background worker.
                logger.exception(
                    "Database failure during event-generation cycle"
                )
            except Exception:
                # Last-resort worker boundary. Unexpected programming errors
                # are logged, but the unattended worker remains alive.
                logger.exception(
                    "Unexpected event-generator failure"
                )

            time.sleep(EVENT_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Event generator stopped")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate simulated restaurant operational events."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fire a single generation round and exit.",
    )

    args = parser.parse_args()

    if args.once:
        for message in run_once():
            print(message)
    else:
        run_forever()


if __name__ == "__main__":
    main()
