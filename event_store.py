"""
Event store — append-only log + current-state projection + point-in-time replay.

Core idea: `Event` rows are the source of truth. The current-state tables
(Ingredient.current_price, MenuItem.current_price, etc.) are a projection
that gets updated every time an event is appended, so normal reads stay fast.

`reconstruct_state_at(tenant_id, timestamp)` rebuilds what the tenant's
state looked like at any past moment by replaying events from scratch —
this is what proves the log is real history, not just an activity feed.

Production-grade immutable Event Store with SHA-256 hash chaining,
concurrency locking, and multi-tenant isolation safeguards.

"""
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db_context
from models import Event, EventType

logger = logging.getLogger(__name__)


class EventStoreError(Exception):
    """Base exception for Event Store operations."""


class TenantIsolationError(EventStoreError):
    """Raised when tenant scoping rules are violated."""


class HashChainCorruptedError(EventStoreError):
    """Raised when an event stream's cryptographic integrity check fails."""


def _compute_sha256_hash(previous_hash: str, event_type: str, payload: Dict[str, Any], timestamp_iso: str) -> str:
    """Deterministically hashes an event using canonicalized JSON formatting."""
    canonical_json = json.dumps(payload, sort_keys=True, default=str).replace("\x00", "")
    data_bytes = f"{previous_hash}|{event_type}|{canonical_json}|{timestamp_iso}".encode("utf-8")
    return hashlib.sha256(data_bytes).hexdigest()


def _canonical_timestamp(timestamp: datetime) -> str:
    """Return a deterministic UTC timestamp representation for hashing."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    return timestamp.isoformat()


def _apply_to_current_state(session: Any, event: Event) -> None:
    """
    Apply an event to the current-state projection.

    The Event table remains the source of truth; these tables are only
    materialized projections for fast reads.
    """
    from models import Ingredient, MenuItem

    payload = event.payload or {}
    event_type = (
        event.event_type.value
        if hasattr(event.event_type, "value")
        else str(event.event_type)
    )

    # Normalize enum/string representation.
    event_type = event_type.lower()

    # ---------------------------------------------------------
    # Ingredient price
    # ---------------------------------------------------------
    if event_type in ("supplier_price_change", "price_change"):
        ingredient_id = payload.get("ingredient_id")
        new_price = payload.get("new_price")

        if ingredient_id is None or new_price is None:
            return

        ing = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == event.tenant_id,
            )
            .first()
        )


        if ing is not None:
            if hasattr(ing, "current_price"):
                ing.current_price = new_price
            elif hasattr(ing, "cost_per_unit"):
                ing.cost_per_unit = new_price

    # ---------------------------------------------------------
    # Stockout
    # ---------------------------------------------------------
    elif event_type == "stockout":
        ingredient_id = payload.get("ingredient_id")

        if ingredient_id is None:
            return

        ing = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == event.tenant_id,
            )
            .first()
        )

        if ing is not None:
            ing.current_stock_level = 0.0

    # ---------------------------------------------------------
    # Restock
    # ---------------------------------------------------------
    elif event_type == "restock":
        ingredient_id = payload.get("ingredient_id")
        new_stock_level = payload.get("new_stock_level")

        if ingredient_id is None or new_stock_level is None:
            return

        ing = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == event.tenant_id,
            )
            .first()
        )

        if ing is not None:
            ing.current_stock_level = new_stock_level

    # ---------------------------------------------------------
    # Stock delta
    # ---------------------------------------------------------
    elif "stock" in event_type or "inventory" in event_type:
        ingredient_id = payload.get("ingredient_id")

        if ingredient_id is None:
            return

        ing = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == event.tenant_id,
            )
            .first()
        )

        if ing is not None and "stock_delta" in payload:
            ing.current_stock_level = (
                ing.current_stock_level or 0.0
            ) + payload["stock_delta"]

    # ---------------------------------------------------------
    # Supplier switch
    # ---------------------------------------------------------
    elif event_type == "supplier_switch":
        ingredient_id = payload.get("ingredient_id")
        new_supplier_id = payload.get("new_supplier_id")

        if ingredient_id is None or new_supplier_id is None:
            return

        ing = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == event.tenant_id,
            )
            .first()
        )

        if ing is not None:
            ing.current_supplier_id = new_supplier_id

    # ---------------------------------------------------------
    # Menu price change
    # ---------------------------------------------------------
    elif event_type == "menu_price_change":
        menu_item_id = payload.get("menu_item_id")
        new_price = payload.get("new_price")

        if menu_item_id is None or new_price is None:
            return

        item = (
            session.query(MenuItem)
            .filter(
                MenuItem.id == menu_item_id,
                MenuItem.tenant_id == event.tenant_id,
            )
            .first()
        )

        if item is not None:
            item.current_price = new_price

    # ---------------------------------------------------------
    # Menu item removed
    # ---------------------------------------------------------
    elif event_type == "menu_item_removed":
        menu_item_id = payload.get("menu_item_id")

        if menu_item_id is None:
            return

        item = (
            session.query(MenuItem)
            .filter(
                MenuItem.id == menu_item_id,
                MenuItem.tenant_id == event.tenant_id,
            )
            .first()
        )

        if item is not None:
            item.active = False

    # DEMAND_SPIKE and unknown events intentionally do nothing.y, event: Event) -> None:
    """
    Synchronizes read-model tables (projections) based on incoming events.
    Informational events (e.g., DEMAND_SPIKE) explicitly perform no-ops.
    """
    payload = event.payload or {}
    str_event_type = str(event.event_type.value if hasattr(event.event_type, "value") else event.event_type)

    # Handle price changes on ingredients
    if str_event_type in ("supplier_price_change", "price_change"):
        ingredient_id = payload.get("ingredient_id")
        new_price = payload.get("new_price")
        if ingredient_id and new_price is not None:
            try:
                from models import Ingredient
                ing = session.query(Ingredient).filter(
                    Ingredient.id == ingredient_id,
                    Ingredient.tenant_id == event.tenant_id,
                ).first()
                if ing:
                    if hasattr(ing, "current_price"):
                        ing.current_price = new_price
                    elif hasattr(ing, "cost_per_unit"):
                        ing.cost_per_unit = new_price
            except Exception as exc:
                logger.debug("Skipping projection for ingredient price update: %s", exc)

    # Handle inventory/stock level updates
    elif "stock" in str_event_type or "inventory" in str_event_type:
        ingredient_id = payload.get("ingredient_id")
        if ingredient_id:
            try:
                from models import Ingredient
                ing = session.query(Ingredient).filter(
                    Ingredient.id == ingredient_id,
                    Ingredient.tenant_id == event.tenant_id,
                ).first()
                if ing and hasattr(ing, "current_stock_level"):
                    if "stock_delta" in payload:
                        ing.current_stock_level = (ing.current_stock_level or 0.0) + payload["stock_delta"]
                    elif "new_stock_level" in payload:
                        ing.current_stock_level = payload["new_stock_level"]
            except Exception as exc:
                logger.debug("Skipping projection for stock update: %s", exc)

    # DEMAND_SPIKE and unknown events pass through without altering current state tables


def append_event(
    tenant_id: str,
    event_type: Union[EventType, str],
    payload: Dict[str, Any],
    source: str = "simulator",
) -> Event:
    """
    Appends an immutable event to the tenant's ledger using pessimistic locking 
    to guarantee atomic cryptographic hash chaining, and projects state changes.
    """
    if not tenant_id or not isinstance(tenant_id, str):
        raise TenantIsolationError("A valid string tenant_id is required to record events.")

    str_event_type = event_type.value if hasattr(event_type, "value") else str(event_type)
    clean_tenant_id = tenant_id.replace("\x00", "").strip()

    try:
        with get_db_context(tenant_id=clean_tenant_id) as session:
            stmt = (
                select(Event)
                .where(Event.tenant_id == clean_tenant_id)
                .order_by(Event.timestamp.desc(), Event.id.desc())
                .with_for_update()
                .limit(1)
            )
            latest_event = session.execute(stmt).scalar_one_or_none()

            previous_hash = (
                latest_event.hash
                if latest_event and latest_event.hash
                else "0" * 64
            )

            now = datetime.now(timezone.utc)
            timestamp_iso = _canonical_timestamp(now)  
                      
            event_hash = _compute_sha256_hash(
                previous_hash=previous_hash,
                event_type=str_event_type,
                payload=payload,
                timestamp_iso=timestamp_iso,
            )

            event = Event(
                tenant_id=clean_tenant_id,
                event_type=str_event_type,
                payload=payload,
                source=source,
                hash=event_hash,
                previous_hash=previous_hash,
                timestamp=now,
            )
            
            session.add(event)
            
            # Synchronize state projection (read-model tables) within the same transaction
            _apply_to_current_state(session, event)

            session.commit()
            session.refresh(event)
         
            session.expunge(event)
            
            logger.info("Recorded event %s for tenant %s [hash: %s]", str_event_type, clean_tenant_id, event_hash[:8])
            return event

    except SQLAlchemyError as exc:
        logger.error("Database failure while appending event for tenant %s: %s", clean_tenant_id, exc, exc_info=True)
        raise EventStoreError(f"Failed to append event: {exc}") from exc

def get_tenant_events(
    tenant_id: str,
    limit: int = 100,
    event_type: Optional[Union[EventType, str]] = None,
) -> List[Event]:
    """Retrieves an isolated log of events for a specific tenant."""
    if not tenant_id or not isinstance(tenant_id, str):
        raise TenantIsolationError("A valid tenant_id is required for querying events.")

    clean_tenant_id = tenant_id.replace("\x00", "").strip()
    
    try:
        with get_db_context(tenant_id=clean_tenant_id) as session:
            stmt = select(Event).where(Event.tenant_id == clean_tenant_id)

            if event_type:
                str_event_type = event_type.value if hasattr(event_type, "value") else str(event_type)
                stmt = stmt.where(Event.event_type == str_event_type)

            stmt = stmt.order_by(Event.timestamp.asc(), Event.id.asc()).limit(limit)
            events = list(session.execute(stmt).scalars().all())
            for ev in events:
                session.expunge(ev)
            return events

    except SQLAlchemyError as exc:
        logger.error("Error fetching event log for tenant %s: %s", clean_tenant_id, exc, exc_info=True)
        raise EventStoreError(f"Failed to query events: {exc}") from exc


def reconstruct_state_at(
    tenant_id: str,
    target_timestamp: datetime,
) -> Dict[str, Any]:
    if not tenant_id or not isinstance(tenant_id, str):
        raise TenantIsolationError(
            "A valid string tenant_id is required for state reconstruction."
        )

    clean_tenant_id = tenant_id.replace("\x00", "").strip()

    if target_timestamp.tzinfo is None:
        target_timestamp = target_timestamp.replace(tzinfo=timezone.utc)

    try:
        with get_db_context(tenant_id=clean_tenant_id) as session:
            stmt = (
                select(Event)
                .where(
                    Event.tenant_id == clean_tenant_id,
                    Event.timestamp <= target_timestamp,
                )
                .order_by(Event.timestamp.asc(), Event.id.asc())
            )

            events = list(session.execute(stmt).scalars().all())

            state: Dict[str, Any] = {
                "ingredients": {},
                "menu_items": {},
            }

            for event in events:
                payload = event.payload or {}

                event_type = (
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                )
                event_type = event_type.lower()

                # -------------------------------------------------
                # Ingredient events
                # -------------------------------------------------
                ingredient_id = payload.get("ingredient_id")

                if ingredient_id is not None:
                    ingredient = state["ingredients"].setdefault(
                        ingredient_id,
                        {}
                    )

                    if event_type in (
                        "supplier_price_change",
                        "price_change",
                    ):
                        if "new_price" in payload:
                            ingredient["price"] = payload["new_price"]
                            ingredient["current_price"] = payload["new_price"]

                    elif event_type == "stockout":
                        ingredient["stock_level"] = 0.0

                    elif event_type == "restock":
                        if "new_stock_level" in payload:
                            ingredient["stock_level"] = (
                                payload["new_stock_level"]
                            )

                    elif "stock" in event_type or "inventory" in event_type:
                        if "stock_delta" in payload:
                            ingredient["stock_level"] = (
                                ingredient.get("stock_level", 0.0)
                                + payload["stock_delta"]
                            )

                    elif event_type == "supplier_switch":
                        if "new_supplier_id" in payload:
                            ingredient["supplier_id"] = (
                                payload["new_supplier_id"]
                            )

                    # Preserve the old flat representation too.
                    state[ingredient_id] = ingredient

                # -------------------------------------------------
                # Menu events
                # -------------------------------------------------
                menu_item_id = payload.get("menu_item_id")

                if menu_item_id is not None:
                    menu_item = state["menu_items"].setdefault(
                        menu_item_id,
                        {}
                    )

                    if event_type == "menu_price_change":
                        if "new_price" in payload:
                            menu_item["price"] = payload["new_price"]

                    elif event_type == "menu_item_removed":
                        menu_item["active"] = False

                # Informational/unknown events
                if ingredient_id is None and menu_item_id is None:
                    state.setdefault(event_type, []).append(payload)

            state["event_count_replayed"] = len(events)

            return state

    except SQLAlchemyError as exc:
        logger.error(
            "Error reconstructing state for tenant %s at %s: %s",
            clean_tenant_id,
            target_timestamp,
            exc,
            exc_info=True,
        )
        raise EventStoreError(
            f"Failed to reconstruct state: {exc}"
        ) from exc
    

def verify_chain_integrity(tenant_id: str) -> bool:
    """Audits the hash chain for a tenant to detect tampering or corruption."""

    if not tenant_id or not isinstance(tenant_id, str):
        raise TenantIsolationError(
            "A valid tenant_id is required for verifying chain integrity."
        )

    clean_tenant_id = tenant_id.replace("\x00", "").strip()

    try:
        with get_db_context(tenant_id=clean_tenant_id) as session:
            stmt = (
                select(Event)
                .where(Event.tenant_id == clean_tenant_id)
                .order_by(Event.timestamp.asc(), Event.id.asc())
            )

            events = list(
                session.execute(stmt).scalars().all()
            )

            if not events:
                return True

            expected_previous_hash = "0" * 64

            for event in events:
                if event.previous_hash != expected_previous_hash:
                    raise HashChainCorruptedError(
                        f"Broken chain link at Event ID {event.id}. "
                        f"Expected prev_hash {expected_previous_hash}, "
                        f"got {event.previous_hash}"
                    )
                
                timestamp_iso = _canonical_timestamp(event.timestamp)

                str_event_type = (
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                )

                computed_hash = _compute_sha256_hash(
                    previous_hash=event.previous_hash,
                    event_type=str_event_type,
                    payload=event.payload or {},
                    timestamp_iso=timestamp_iso,
                )

                if event.hash != computed_hash:
                    raise HashChainCorruptedError(
                        f"Payload/Hash mismatch at Event ID {event.id}. "
                        f"Computed {computed_hash}, stored {event.hash}"
                    )

                expected_previous_hash = event.hash

            return True

    except HashChainCorruptedError:
        raise

    except SQLAlchemyError as exc:
        logger.error(
            "Error verifying chain integrity for tenant %s: %s",
            clean_tenant_id,
            exc,
            exc_info=True,
        )
        raise EventStoreError(
            f"Failed to verify chain integrity: {exc}"
        ) from exc