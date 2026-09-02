"""
Event store for Restaurant Ops Copilot — Component 1.

Responsibilities
----------------
1. Append immutable operational events.
2. Maintain a per-tenant cryptographic hash chain.
3. Update current-state projections atomically with event insertion.
4. Persist structured historical records for procurement, inventory,
   sales, waste, and staffing events.
5. Reconstruct event-backed operational state at a point in time.
6. Verify event-chain integrity.

Architecture
------------

                    ┌──────────────────────┐
                    │      Event Log       │
                    │  historical truth    │
                    └──────────┬───────────┘
                               │
                ┌──────────────┼──────────────┐
                │              │              │
                ▼              ▼              ▼
        Current projections  History       Replay
        InventoryLevel       SupplierPrice  PIT state
        Ingredient           Invoice
        MenuItem             SalesOrder
        StaffShift           WasteRecord
        OrderVolume

Event insertion and projection updates occur in one database transaction.
A failure rolls back both the event and all projection changes.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db import get_db_context, validate_tenant_id
from .models import (
    Event,
    EventType,
    Ingredient,
    InventoryLevel,
    Invoice,
    InvoiceLine,
    MenuItem,
    OrderVolume,
    Recipe,
    RecipeIngredient,
    SalesOrder,
    SalesOrderLine,
    StaffShift,
    Supplier,
    SupplierPrice,
    SupplierSKU,
    Tenant,
    WasteRecord,
)


logger = logging.getLogger(__name__)


GENESIS_HASH = "0" * 64


class EventStoreError(Exception):
    """Base exception for event-store failures."""


class TenantIsolationError(EventStoreError):
    """Raised when tenant scoping requirements are violated."""


class HashChainCorruptedError(EventStoreError):
    """Raised when an event hash chain fails integrity verification."""


class InvalidEventError(EventStoreError):
    """Raised when an event payload violates event-store requirements."""


# ---------------------------------------------------------------------------
# Validation / normalization helpers
# ---------------------------------------------------------------------------

def _normalize_tenant_id(tenant_id: str) -> str:
    """Validate and canonicalize a tenant identifier."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise TenantIsolationError(
            "A valid tenant_id is required."
        )

    try:
        return validate_tenant_id(tenant_id)
    except ValueError as exc:
        raise TenantIsolationError(str(exc)) from exc


def _normalize_event_type(
    event_type: Union[EventType, str],
) -> str:
    """Return the canonical EventType value."""
    if isinstance(event_type, EventType):
        return event_type.value

    if not isinstance(event_type, str) or not event_type.strip():
        raise InvalidEventError(
            "event_type must be a valid EventType."
        )

    value = event_type.strip().lower()

    valid_values = {member.value for member in EventType}

    # Preserve backwards compatibility with the old event generator.
    aliases = {
        "price_change": EventType.SUPPLIER_PRICE_CHANGE.value,
    }

    value = aliases.get(value, value)

    if value not in valid_values:
        raise InvalidEventError(
            f"Unsupported event type: {event_type!r}"
        )

    return value


def _canonical_timestamp(timestamp: datetime) -> str:
    """Return a deterministic UTC representation of a timestamp."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    return timestamp.isoformat()


def _compute_sha256_hash(
    previous_hash: str,
    event_type: str,
    payload: Dict[str, Any],
    timestamp_iso: str,
    source: str,
) -> str:
    """
    Compute a deterministic SHA-256 hash for an event.

    JSON keys are sorted and separators are canonicalized so equivalent
    payload dictionaries produce the same representation.
    """
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )

    canonical_data = (
        f"{previous_hash}|"
        f"{event_type}|"
        f"{canonical_json}|"
        f"{timestamp_iso}|"
        f"{source}"
    )

    return hashlib.sha256(
        canonical_data.encode("utf-8")
    ).hexdigest()

def _decimal(
    value: Any,
    field_name: str,
) -> Decimal:
    """Convert an event payload value into Decimal safely."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidEventError(
            f"{field_name} must be a valid numeric value."
        ) from exc


def _require_payload_field(
    payload: Dict[str, Any],
    field_name: str,
) -> Any:
    """Require a non-null payload field."""
    value = payload.get(field_name)

    if value is None:
        raise InvalidEventError(
            f"Event payload requires '{field_name}'."
        )

    return value


# ---------------------------------------------------------------------------
# Tenant-scoped lookup helpers
# ---------------------------------------------------------------------------

def _get_ingredient(
    session: Any,
    tenant_id: str,
    ingredient_id: str,
) -> Ingredient:
    """Load an ingredient while enforcing tenant ownership."""
    ingredient = (
        session.query(Ingredient)
        .filter(
            Ingredient.id == ingredient_id,
            Ingredient.tenant_id == tenant_id,
        )
        .one_or_none()
    )

    if ingredient is None:
        raise InvalidEventError(
            f"Ingredient {ingredient_id} does not belong to tenant "
            f"{tenant_id}."
        )

    return ingredient


def _get_menu_item(
    session: Any,
    tenant_id: str,
    menu_item_id: str,
) -> MenuItem:
    """Load a menu item while enforcing tenant ownership."""
    menu_item = (
        session.query(MenuItem)
        .filter(
            MenuItem.id == menu_item_id,
            MenuItem.tenant_id == tenant_id,
        )
        .one_or_none()
    )

    if menu_item is None:
        raise InvalidEventError(
            f"Menu item {menu_item_id} does not belong to tenant "
            f"{tenant_id}."
        )

    return menu_item


def _get_supplier(
    session: Any,
    tenant_id: str,
    supplier_id: str,
) -> Supplier:
    """Load a supplier while enforcing tenant ownership."""
    supplier = (
        session.query(Supplier)
        .filter(
            Supplier.id == supplier_id,
            Supplier.tenant_id == tenant_id,
        )
        .one_or_none()
    )

    if supplier is None:
        raise InvalidEventError(
            f"Supplier {supplier_id} does not belong to tenant "
            f"{tenant_id}."
        )

    return supplier


def _get_supplier_sku(
    session: Any,
    tenant_id: str,
    supplier_sku_id: str,
) -> SupplierSKU:
    """Load a supplier SKU while enforcing tenant ownership."""
    sku = (
        session.query(SupplierSKU)
        .filter(
            SupplierSKU.id == supplier_sku_id,
            SupplierSKU.tenant_id == tenant_id,
        )
        .one_or_none()
    )

    if sku is None:
        raise InvalidEventError(
            f"Supplier SKU {supplier_sku_id} does not belong to tenant "
            f"{tenant_id}."
        )

    return sku


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def _update_inventory_projection(
    session: Any,
    tenant_id: str,
    ingredient_id: str,
    new_quantity: Decimal,
    event_timestamp: datetime,
) -> None:
    """
    Update InventoryLevel for an ingredient.

    InventoryLevel is the current-state projection. The Event remains the
    historical source of truth.
    """
    _get_ingredient(
        session,
        tenant_id,
        ingredient_id,
    )

    inventory = (
        session.query(InventoryLevel)
        .filter(
            InventoryLevel.tenant_id == tenant_id,
            InventoryLevel.ingredient_id == ingredient_id,
        )
        .one_or_none()
    )

    if inventory is None:
        inventory = InventoryLevel(
            tenant_id=tenant_id,
            ingredient_id=ingredient_id,
            quantity=new_quantity,
            updated_at=event_timestamp,
        )
        session.add(inventory)
    else:
        inventory.quantity = new_quantity
        inventory.updated_at = event_timestamp


def _apply_supplier_price_change(
    session: Any,
    event: Event,
) -> None:
    """Persist supplier price history and update the compatibility price."""
    payload = event.payload

    supplier_sku_id = _require_payload_field(
        payload,
        "supplier_sku_id",
    )
    new_price = _decimal(
        _require_payload_field(payload, "new_price"),
        "new_price",
    )

    if new_price < 0:
        raise InvalidEventError(
            "new_price cannot be negative."
        )

    sku = _get_supplier_sku(
        session,
        event.tenant_id,
        supplier_sku_id,
    )

    price_record = SupplierPrice(
        tenant_id=event.tenant_id,
        supplier_sku_id=sku.id,
        price=new_price,
        effective_at=event.timestamp,
    )

    session.add(price_record)

    # Maintain the legacy/current Ingredient projection as well.
    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        sku.ingredient_id,
    )

    ingredient.current_price = new_price


def _apply_supplier_switch(
    session: Any,
    event: Event,
) -> None:
    """Update the current supplier projection for an ingredient."""
    payload = event.payload

    ingredient_id = _require_payload_field(
        payload,
        "ingredient_id",
    )
    new_supplier_id = _require_payload_field(
        payload,
        "new_supplier_id",
    )

    _get_supplier(
        session,
        event.tenant_id,
        new_supplier_id,
    )

    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        ingredient_id,
    )

    ingredient.current_supplier_id = new_supplier_id


def _apply_stockout(
    session: Any,
    event: Event,
) -> None:
    """Set ingredient inventory projection to zero."""
    ingredient_id = _require_payload_field(
        event.payload,
        "ingredient_id",
    )

    _update_inventory_projection(
        session=session,
        tenant_id=event.tenant_id,
        ingredient_id=ingredient_id,
        new_quantity=Decimal("0"),
        event_timestamp=event.timestamp,
    )

    # Maintain backwards-compatible current-state field.
    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        ingredient_id,
    )
    ingredient.current_stock_level = Decimal("0")


def _apply_restock(
    session: Any,
    event: Event,
) -> None:
    """Set inventory to the restocked quantity."""
    ingredient_id = _require_payload_field(
        event.payload,
        "ingredient_id",
    )

    new_quantity = _decimal(
        _require_payload_field(
            event.payload,
            "new_stock_level",
        ),
        "new_stock_level",
    )

    if new_quantity < 0:
        raise InvalidEventError(
            "new_stock_level cannot be negative."
        )

    _update_inventory_projection(
        session=session,
        tenant_id=event.tenant_id,
        ingredient_id=ingredient_id,
        new_quantity=new_quantity,
        event_timestamp=event.timestamp,
    )

    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        ingredient_id,
    )

    ingredient.current_stock_level = new_quantity


def _apply_inventory_adjustment(
    session: Any,
    event: Event,
) -> None:
    """Apply either an absolute or delta inventory adjustment."""
    payload = event.payload

    ingredient_id = _require_payload_field(
        payload,
        "ingredient_id",
    )

    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        ingredient_id,
    )

    inventory = (
        session.query(InventoryLevel)
        .filter(
            InventoryLevel.tenant_id == event.tenant_id,
            InventoryLevel.ingredient_id == ingredient_id,
        )
        .one_or_none()
    )

    current_quantity = (
        inventory.quantity
        if inventory is not None
        else ingredient.current_stock_level
    )

    if "new_stock_level" in payload:
        new_quantity = _decimal(
            payload["new_stock_level"],
            "new_stock_level",
        )
    elif "stock_delta" in payload:
        delta = _decimal(
            payload["stock_delta"],
            "stock_delta",
        )
        new_quantity = current_quantity + delta
    else:
        raise InvalidEventError(
            "inventory_adjustment requires either "
            "'new_stock_level' or 'stock_delta'."
        )

    if new_quantity < 0:
        raise InvalidEventError(
            "Inventory quantity cannot become negative."
        )

    _update_inventory_projection(
        session=session,
        tenant_id=event.tenant_id,
        ingredient_id=ingredient_id,
        new_quantity=new_quantity,
        event_timestamp=event.timestamp,
    )

    ingredient.current_stock_level = new_quantity


def _apply_waste(
    session: Any,
    event: Event,
) -> None:
    """Persist a historical waste record and reduce current inventory."""
    payload = event.payload

    ingredient_id = _require_payload_field(
        payload,
        "ingredient_id",
    )

    quantity = _decimal(
        _require_payload_field(payload, "quantity"),
        "quantity",
    )

    reason = _require_payload_field(
        payload,
        "reason",
    )

    if quantity <= 0:
        raise InvalidEventError(
            "Waste quantity must be greater than zero."
        )

    ingredient = _get_ingredient(
        session,
        event.tenant_id,
        ingredient_id,
    )

    inventory = (
        session.query(InventoryLevel)
        .filter(
            InventoryLevel.tenant_id == event.tenant_id,
            InventoryLevel.ingredient_id == ingredient_id,
        )
        .one_or_none()
    )

    current_quantity = (
        inventory.quantity
        if inventory is not None
        else ingredient.current_stock_level
    )

    new_quantity = current_quantity - quantity

    if new_quantity < 0:
        raise InvalidEventError(
            "Waste quantity exceeds current inventory."
        )

    waste = WasteRecord(
        tenant_id=event.tenant_id,
        ingredient_id=ingredient_id,
        quantity=quantity,
        reason=str(reason),
        recorded_at=event.timestamp,
    )

    session.add(waste)

    _update_inventory_projection(
        session=session,
        tenant_id=event.tenant_id,
        ingredient_id=ingredient_id,
        new_quantity=new_quantity,
        event_timestamp=event.timestamp,
    )

    ingredient.current_stock_level = new_quantity


def _apply_menu_price_change(
    session: Any,
    event: Event,
) -> None:
    """Update the current menu-item price."""
    menu_item_id = _require_payload_field(
        event.payload,
        "menu_item_id",
    )

    new_price = _decimal(
        _require_payload_field(
            event.payload,
            "new_price",
        ),
        "new_price",
    )

    if new_price < 0:
        raise InvalidEventError(
            "Menu price cannot be negative."
        )

    menu_item = _get_menu_item(
        session,
        event.tenant_id,
        menu_item_id,
    )

    menu_item.current_price = new_price


def _apply_menu_item_added(
    session: Any,
    event: Event,
) -> None:
    """Create the current-state projection for a newly added menu item."""
    payload = event.payload

    menu_item_id = _require_payload_field(
        payload,
        "menu_item_id",
    )

    name = _require_payload_field(
        payload,
        "name",
    )

    current_price = _decimal(
        _require_payload_field(
            payload,
            "current_price",
        ),
        "current_price",
    )

    if current_price < 0:
        raise InvalidEventError(
            "Menu item current_price cannot be negative."
        )

    active = payload.get("active", True)

    if not isinstance(active, bool):
        raise InvalidEventError(
            "Menu item active must be a boolean."
        )

    # Tenant-scoped lookup prevents an event from modifying another
    # tenant's menu item.
    existing = (
        session.query(MenuItem)
        .filter(
            MenuItem.id == menu_item_id,
            MenuItem.tenant_id == event.tenant_id,
        )
        .one_or_none()
    )

    if existing is None:
        session.add(
            MenuItem(
                id=menu_item_id,
                tenant_id=event.tenant_id,
                name=str(name),
                current_price=current_price,
                active=active,
            )
        )
    else:
        # Idempotent projection behavior: if the item already exists
        # for this tenant, update its current state.
        existing.name = str(name)
        existing.current_price = current_price
        existing.active = active


def _apply_menu_item_removed(
    session: Any,
    event: Event,
) -> None:
    """Deactivate a menu item."""
    menu_item_id = _require_payload_field(
        event.payload,
        "menu_item_id",
    )

    menu_item = _get_menu_item(
        session,
        event.tenant_id,
        menu_item_id,
    )

    menu_item.active = False

def _apply_order_recorded(
    session: Any,
    event: Event,
) -> None:
    """
    Persist an order and its line items.

    Expected payload:

        {
            "order_id": "...",              # optional
            "ordered_at": "...",            # optional
            "lines": [
                {
                    "menu_item_id": "...",
                    "quantity": 2,
                    "unit_price": 14.50
                }
            ]
        }

    The event itself remains the immutable historical record.
    """
    payload = event.payload

    lines = payload.get("lines")

    if not isinstance(lines, list) or not lines:
        raise InvalidEventError(
            "order_recorded requires a non-empty 'lines' list."
        )

    order_id = payload.get("order_id")

    order = SalesOrder(
        id=order_id if order_id else None,
        tenant_id=event.tenant_id,
        ordered_at=event.timestamp,
    )

    session.add(order)
    session.flush()

    for line in lines:
        menu_item_id = _require_payload_field(
            line,
            "menu_item_id",
        )

        quantity = int(
            _require_payload_field(
                line,
                "quantity",
            )
        )

        unit_price = _decimal(
            _require_payload_field(
                line,
                "unit_price",
            ),
            "unit_price",
        )

        if quantity <= 0:
            raise InvalidEventError(
                "Order quantity must be greater than zero."
            )

        if unit_price < 0:
            raise InvalidEventError(
                "Order unit price cannot be negative."
            )

        _get_menu_item(
            session,
            event.tenant_id,
            menu_item_id,
        )

        session.add(
            SalesOrderLine(
                tenant_id=event.tenant_id,
                order_id=order.id,
                menu_item_id=menu_item_id,
                quantity=quantity,
                unit_price=unit_price,
            )
        )

        # Maintain the Component 2 demand projection.
        session.add(
            OrderVolume(
                tenant_id=event.tenant_id,
                menu_item_id=menu_item_id,
                timestamp=event.timestamp,
                quantity=quantity,
            )
        )


def _apply_invoice_recorded(
    session: Any,
    event: Event,
) -> None:
    """Persist supplier invoice header and line history."""
    payload = event.payload

    supplier_id = _require_payload_field(
        payload,
        "supplier_id",
    )
    invoice_number = _require_payload_field(
        payload,
        "invoice_number",
    )

    _get_supplier(
        session,
        event.tenant_id,
        supplier_id,
    )

    total_amount = _decimal(
        payload.get("total_amount", "0"),
        "total_amount",
    )

    if total_amount < 0:
        raise InvalidEventError(
            "Invoice total cannot be negative."
        )

    invoice = Invoice(
        id=payload.get("invoice_id"),
        tenant_id=event.tenant_id,
        supplier_id=supplier_id,
        invoice_number=str(invoice_number),
        invoice_date=event.timestamp,
        total_amount=total_amount,
    )

    session.add(invoice)
    session.flush()

    lines = payload.get("lines", [])

    if not isinstance(lines, list):
        raise InvalidEventError(
            "Invoice 'lines' must be a list."
        )

    for line in lines:
        supplier_sku_id = _require_payload_field(
            line,
            "supplier_sku_id",
        )

        quantity = _decimal(
            _require_payload_field(
                line,
                "quantity",
            ),
            "quantity",
        )

        unit_price = _decimal(
            _require_payload_field(
                line,
                "unit_price",
            ),
            "unit_price",
        )

        if quantity <= 0:
            raise InvalidEventError(
                "Invoice quantity must be greater than zero."
            )

        if unit_price < 0:
            raise InvalidEventError(
                "Invoice unit price cannot be negative."
            )

        sku = _get_supplier_sku(
            session,
            event.tenant_id,
            supplier_sku_id,
        )

        if sku.supplier_id != supplier_id:
            raise InvalidEventError(
                "Invoice SKU belongs to a different supplier."
            )

        session.add(
            InvoiceLine(
                tenant_id=event.tenant_id,
                invoice_id=invoice.id,
                supplier_sku_id=supplier_sku_id,
                quantity=quantity,
                unit_price=unit_price,
            )
        )

def _apply_staffing_change(
    session: Any,
    event: Event,
) -> None:
    """Create or update a staff shift projection."""
    payload = event.payload

    staff_shift_id = payload.get("staff_shift_id")
    role = _require_payload_field(payload, "role")
    day_of_week = payload.get("day_of_week")
    headcount = payload.get("headcount")

    if day_of_week is None:
        raise InvalidEventError(
            "staffing_change requires 'day_of_week'."
        )

    if headcount is None:
        raise InvalidEventError(
            "staffing_change requires 'headcount'."
        )

    try:
        day_of_week = int(day_of_week)
        headcount = int(headcount)
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(
            "day_of_week and headcount must be integers."
        ) from exc

    if not 0 <= day_of_week <= 6:
        raise InvalidEventError(
            "day_of_week must be between 0 and 6."
        )

    if headcount < 0:
        raise InvalidEventError(
            "headcount cannot be negative."
        )

    query = session.query(StaffShift).filter(
        StaffShift.tenant_id == event.tenant_id,
    )

    if staff_shift_id:
        shift = query.filter(
            StaffShift.id == staff_shift_id,
        ).one_or_none()
    else:
        shift = query.filter(
            StaffShift.role == str(role),
            StaffShift.day_of_week == day_of_week,
        ).one_or_none()

    if shift is None:
        shift = StaffShift(
            id=staff_shift_id,
            tenant_id=event.tenant_id,
            role=str(role),
            day_of_week=day_of_week,
            headcount=headcount,
        )
        session.add(shift)
    else:
        shift.role = str(role)
        shift.day_of_week = day_of_week
        shift.headcount = headcount

        
def _apply_to_current_state(
    session: Any,
    event: Event,
) -> None:
    """Apply an event to the corresponding current-state projection."""

    handlers = {
        EventType.SUPPLIER_PRICE_CHANGE.value:
            _apply_supplier_price_change,

        EventType.SUPPLIER_SWITCH.value:
            _apply_supplier_switch,

        EventType.STOCKOUT.value:
            _apply_stockout,

        EventType.RESTOCK.value:
            _apply_restock,

        EventType.INVENTORY_ADJUSTMENT.value:
            _apply_inventory_adjustment,

        EventType.WASTE_RECORDED.value:
            _apply_waste,

        EventType.MENU_ITEM_ADDED.value:
            _apply_menu_item_added,

        EventType.MENU_ITEM_REMOVED.value:
            _apply_menu_item_removed,

        EventType.MENU_PRICE_CHANGE.value:
            _apply_menu_price_change,

        EventType.ORDER_RECORDED.value:
            _apply_order_recorded,

        EventType.INVOICE_RECORDED.value:
            _apply_invoice_recorded,

        EventType.STAFFING_CHANGE.value:
            _apply_staffing_change,
    }

    handler = handlers.get(event.event_type.value)

    if handler is not None:
        handler(
            session=session,
            event=event,
        )

# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------

def append_event(
    tenant_id: str,
    event_type: Union[EventType, str],
    payload: Dict[str, Any],
    source: str = "simulator",
) -> Event:
    """
    Append one event atomically.

    Transaction:

        lock tenant
            ↓
        read latest event
            ↓
        calculate hash
            ↓
        insert Event
            ↓
        update projections/history
            ↓
        flush
            ↓
        commit

    The tenant-row lock serializes event writers for a tenant, including the
    case where that tenant has no previous events.
    """
    clean_tenant_id = _normalize_tenant_id(tenant_id)
    clean_event_type = _normalize_event_type(event_type)

    if not isinstance(payload, dict):
        raise InvalidEventError(
            "Event payload must be a dictionary."
        )

    if not isinstance(source, str) or not source.strip():
        raise InvalidEventError(
            "Event source must be a non-empty string."
        )

    try:
        with get_db_context(
            tenant_id=clean_tenant_id
        ) as session:

            # ----------------------------------------------------------
            # Serialize event writers for this tenant.
            # ----------------------------------------------------------

            tenant_stmt = (
                select(Tenant)
                .where(Tenant.id == clean_tenant_id)
                .with_for_update()
            )

            tenant = (
                session.execute(tenant_stmt)
                .scalar_one_or_none()
            )

            if tenant is None:
                raise TenantIsolationError(
                    f"Tenant {clean_tenant_id} does not exist."
                )

            # ----------------------------------------------------------
            # Find the latest event in this tenant's chain.
            # ----------------------------------------------------------

            latest_stmt = (
                select(Event)
                .where(
                    Event.tenant_id == clean_tenant_id
                )
                .order_by(
                    Event.timestamp.desc(),
                    Event.id.desc(),
                )
                .limit(1)
            )

            latest_event = (
                session.execute(latest_stmt)
                .scalar_one_or_none()
            )

            previous_hash = (
                latest_event.hash
                if latest_event is not None
                else GENESIS_HASH
            )

            # ----------------------------------------------------------
            # Generate canonical timestamp and hash.
            # ----------------------------------------------------------

            now = datetime.now(timezone.utc)

            timestamp_iso = _canonical_timestamp(now)

            event_hash = _compute_sha256_hash(
                previous_hash=previous_hash,
                event_type=clean_event_type,
                payload=payload,
                timestamp_iso=timestamp_iso,
                source=source,
            )

            # ----------------------------------------------------------
            # Append immutable event.
            # ----------------------------------------------------------

            event = Event(
                tenant_id=clean_tenant_id,
                event_type=EventType(clean_event_type),
                payload=payload,
                source=source.strip(),
                hash=event_hash,
                previous_hash=previous_hash,
                timestamp=now,
            )

            session.add(event)

            # ----------------------------------------------------------
            # Update structured projections in the SAME transaction.
            # ----------------------------------------------------------

            _apply_to_current_state(
                session=session,
                event=event,
            )

            session.flush()
            session.refresh(event)

            # Detach before closing the session.
            session.expunge(event)

            logger.info(
                "Recorded event type=%s tenant=%s hash_prefix=%s",
                clean_event_type,
                clean_tenant_id,
                event_hash[:8],
            )

            return event

    except (
        TenantIsolationError,
        InvalidEventError,
        HashChainCorruptedError,
    ):
        raise

    except IntegrityError as exc:
        logger.exception(
            "Integrity failure while appending event for tenant %s.",
            clean_tenant_id,
        )

        raise EventStoreError(
            "Event could not be persisted because a database "
            "integrity constraint was violated."
        ) from exc

    except SQLAlchemyError as exc:
        logger.exception(
            "Database failure while appending event for tenant %s.",
            clean_tenant_id,
        )

        raise EventStoreError(
            f"Failed to append event: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def get_tenant_events(
    tenant_id: str,
    limit: int = 100,
    event_type: Optional[Union[EventType, str]] = None,
) -> List[Event]:
    """Retrieve an isolated event stream for one tenant."""
    clean_tenant_id = _normalize_tenant_id(tenant_id)

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(
            "limit must be a positive integer."
        )

    if limit <= 0:
        raise ValueError(
            "limit must be a positive integer."
        )

    clean_event_type = (
        _normalize_event_type(event_type)
        if event_type is not None
        else None
    )

    try:
        with get_db_context(
            tenant_id=clean_tenant_id
        ) as session:

            stmt = (
                select(Event)
                .where(
                    Event.tenant_id == clean_tenant_id
                )
            )

            if clean_event_type is not None:
                stmt = stmt.where(
                    Event.event_type
                    == EventType(clean_event_type)
                )

            stmt = (
                stmt
                .order_by(
                    Event.timestamp.asc(),
                    Event.id.asc(),
                )
                .limit(limit)
            )

            events = list(
                session.execute(stmt)
                .scalars()
                .all()
            )

            for event in events:
                session.expunge(event)

            return events

    except SQLAlchemyError as exc:
        logger.exception(
            "Failed to fetch events for tenant %s.",
            clean_tenant_id,
        )

        raise EventStoreError(
            f"Failed to query events: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Point-in-time reconstruction
# ---------------------------------------------------------------------------

def reconstruct_state_at(
    tenant_id: str,
    target_timestamp: datetime,
) -> Dict[str, Any]:
    """
    Reconstruct event-backed operational state at a historical timestamp.

    This function deliberately reconstructs only domains for which the event
    contract currently contains enough information.

    Returned structure:

        {
            "ingredients": {...},
            "menu_items": {...},
            "inventory": {...},
            "supplier_prices": {...},
            "orders": [...],
            "waste": [...],
            "invoices": [...],
            "staffing": {...},
            "informational_events": {...},
            "event_count_replayed": N,
        }
    """
    clean_tenant_id = _normalize_tenant_id(tenant_id)

    if not isinstance(target_timestamp, datetime):
        raise TypeError(
            "target_timestamp must be a datetime."
        )

    if target_timestamp.tzinfo is None:
        target_timestamp = target_timestamp.replace(
            tzinfo=timezone.utc
        )
    else:
        target_timestamp = target_timestamp.astimezone(
            timezone.utc
        )

    try:
        with get_db_context(
            tenant_id=clean_tenant_id
        ) as session:


            sku_to_ingredient = {
                sku.id: sku.ingredient_id
                for sku in (
                    session.query(SupplierSKU)
                    .filter(
                        SupplierSKU.tenant_id == clean_tenant_id
                    )
                    .all()
                )
            }

            stmt = (
                select(Event)
                .where(
                    Event.tenant_id == clean_tenant_id,
                    Event.timestamp <= target_timestamp,
                )
                .order_by(
                    Event.timestamp.asc(),
                    Event.id.asc(),
                )
            )

            events = list(
                session.execute(stmt)
                .scalars()
                .all()
            )

            state: Dict[str, Any] = {
                "ingredients": {},
                "menu_items": {},
                "inventory": {},
                "supplier_prices": {},
                "orders": [],
                "waste": [],
                "invoices": [],
                "staffing": {},
                "informational_events": {},
            }

            for event in events:
                payload = event.payload or {}
                event_type = _normalize_event_type(
                    event.event_type
                )

                ingredient_id = payload.get(
                    "ingredient_id"
                )
                menu_item_id = payload.get(
                    "menu_item_id"
                )

                # ------------------------------------------------------
                # Supplier price
                # ------------------------------------------------------

                if (
                    event_type
                    == EventType.SUPPLIER_PRICE_CHANGE.value
                ):
                    supplier_sku_id = payload.get(
                        "supplier_sku_id"
                    )

                    if supplier_sku_id is not None:
                        price = payload.get("new_price")

                        state["supplier_prices"].setdefault(
                            supplier_sku_id,
                            {},
                        )["price"] = price


                        state["supplier_prices"][
                            supplier_sku_id
                        ]["effective_at"] = event.timestamp

                        ingredient_id = sku_to_ingredient.get(
                            supplier_sku_id
                        )

                        if ingredient_id is not None and "new_price" in payload:
                            ingredient = (
                                state["ingredients"]
                                .setdefault(
                                    ingredient_id,
                                    {},
                                )
                            )

                            ingredient["current_price"] = (
                                payload["new_price"]
                            )

                # ------------------------------------------------------
                # Supplier switch
                # ------------------------------------------------------

                elif (
                    event_type
                    == EventType.SUPPLIER_SWITCH.value
                ):
                    if ingredient_id is not None:
                        ingredient = (
                            state["ingredients"]
                            .setdefault(
                                ingredient_id,
                                {},
                            )
                        )

                        if "new_supplier_id" in payload:
                            ingredient["supplier_id"] = (
                                payload["new_supplier_id"]
                            )

                # ------------------------------------------------------
                # Inventory
                # ------------------------------------------------------

                elif event_type in {
                    EventType.STOCKOUT.value,
                    EventType.RESTOCK.value,
                    EventType.INVENTORY_ADJUSTMENT.value,
                    EventType.WASTE_RECORDED.value,
                }:
                    if ingredient_id is not None:

                        inventory = (
                            state["inventory"]
                            .setdefault(
                                ingredient_id,
                                Decimal("0"),
                            )
                        )

                        if (
                            event_type
                            == EventType.STOCKOUT.value
                        ):
                            inventory = Decimal("0")

                        elif (
                            event_type
                            == EventType.RESTOCK.value
                        ):
                            if "new_stock_level" in payload:
                                inventory = _decimal(
                                    payload["new_stock_level"],
                                    "new_stock_level",
                                )

                        elif (
                            event_type
                            == EventType.INVENTORY_ADJUSTMENT.value
                        ):
                            if "new_stock_level" in payload:
                                inventory = _decimal(
                                    payload["new_stock_level"],
                                    "new_stock_level",
                                )
                            elif "stock_delta" in payload:
                                inventory += _decimal(
                                    payload["stock_delta"],
                                    "stock_delta",
                                )

                        elif (
                            event_type
                            == EventType.WASTE_RECORDED.value
                        ):
                            quantity = _decimal(
                                payload.get(
                                    "quantity",
                                    0,
                                ),
                                "quantity",
                            )

                            inventory -= quantity

                            state["waste"].append({
                                "ingredient_id": ingredient_id,
                                "quantity": payload.get(
                                    "quantity"
                                ),
                                "reason": payload.get(
                                    "reason"
                                ),
                                "timestamp": event.timestamp,
                            })

                    state["inventory"][
                        ingredient_id
                    ] = inventory

                    ingredient = state["ingredients"].setdefault(
                        ingredient_id,
                        {}
                    )

                    ingredient["stock_level"] = float(inventory)
                # ------------------------------------------------------
                # Menu
                # ------------------------------------------------------

                elif (
                    event_type
                    == EventType.MENU_PRICE_CHANGE.value
                ):
                    if menu_item_id is not None:
                        menu_item = (
                            state["menu_items"]
                            .setdefault(
                                menu_item_id,
                                {},
                            )
                        )

                        if "new_price" in payload:
                            menu_item["current_price"] = (
                                payload["new_price"]
                            )

                elif (
                    event_type
                    == EventType.MENU_ITEM_REMOVED.value
                ):
                    if menu_item_id is not None:
                        menu_item = (
                            state["menu_items"]
                            .setdefault(
                                menu_item_id,
                                {},
                            )
                        )

                        menu_item["active"] = False

                elif (
                    event_type
                    == EventType.MENU_ITEM_ADDED.value
                ):
                    if menu_item_id is not None:
                        menu_item = (
                            state["menu_items"]
                            .setdefault(
                                menu_item_id,
                                {},
                            )
                        )

                        menu_item["active"] = True

                        if "name" in payload:
                            menu_item["name"] = payload["name"]

                        if "current_price" in payload:
                            menu_item["current_price"] = (
                                payload["current_price"]
                            )
                        elif "price" in payload:
                            menu_item["current_price"] = (
                                payload["price"]
                            )

                # ------------------------------------------------------
                # Orders
                # ------------------------------------------------------

                elif (
                    event_type
                    == EventType.ORDER_RECORDED.value
                ):
                    state["orders"].append({
                        "order_id": payload.get("order_id"),
                        "ordered_at": event.timestamp,
                        "lines": payload.get("lines", []),
                    })

                # ------------------------------------------------------
                # Invoices
                # ------------------------------------------------------

                elif (
                    event_type
                    == EventType.INVOICE_RECORDED.value
                ):
                    state["invoices"].append({
                        "invoice_id": payload.get(
                            "invoice_id"
                        ),
                        "supplier_id": payload.get(
                            "supplier_id"
                        ),
                        "invoice_number": payload.get(
                            "invoice_number"
                        ),
                        "total_amount": payload.get(
                            "total_amount"
                        ),
                        "lines": payload.get(
                            "lines",
                            [],
                        ),
                        "invoice_date": event.timestamp,
                    })

                # ------------------------------------------------------
                # Staffing
                # ------------------------------------------------------

                elif (
                    event_type
                    == EventType.STAFFING_CHANGE.value
                ):
                    role = payload.get("role")
                    day = payload.get("day_of_week")

                    if role is not None and day is not None:
                        state["staffing"][
                            f"{role}:{day}"
                        ] = {
                            "role": role,
                            "day_of_week": day,
                            "headcount": payload.get(
                                "headcount"
                            ),
                        }

                # ------------------------------------------------------
                # Informational events
                # ------------------------------------------------------

                elif event_type in {
                    EventType.DEMAND_SPIKE.value,
                }:
                    state[
                        "informational_events"
                    ].setdefault(
                        event_type,
                        [],
                    ).append({
                        "timestamp": event.timestamp,
                        "payload": payload,
                    })

            state["event_count_replayed"] = len(events)

            return state

    except SQLAlchemyError as exc:
        logger.exception(
            "Failed to reconstruct state for tenant %s.",
            clean_tenant_id,
        )

        raise EventStoreError(
            f"Failed to reconstruct state: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Hash-chain verification
# ---------------------------------------------------------------------------

def verify_chain_integrity(
    tenant_id: str,
) -> bool:
    """
    Verify the complete cryptographic event chain for a tenant.

    Returns True when the chain is valid.

    Raises HashChainCorruptedError when:
        - the first event does not point to GENESIS_HASH
        - previous_hash does not match the preceding event
        - an event's stored hash does not match its contents
    """
    clean_tenant_id = _normalize_tenant_id(tenant_id)

    try:
        with get_db_context(
            tenant_id=clean_tenant_id
        ) as session:

            stmt = (
                select(Event)
                .where(
                    Event.tenant_id == clean_tenant_id
                )
                .order_by(
                    Event.timestamp.asc(),
                    Event.id.asc(),
                )
            )

            events = list(
                session.execute(stmt)
                .scalars()
                .all()
            )

            if not events:
                return True

            expected_previous_hash = GENESIS_HASH

            for event in events:

                # ------------------------------------------------------
                # Verify chain linkage.
                # ------------------------------------------------------

                if (
                    event.previous_hash
                    != expected_previous_hash
                ):
                    raise HashChainCorruptedError(
                        f"Broken chain link at Event ID "
                        f"{event.id}. "
                        f"Expected previous hash "
                        f"{expected_previous_hash}, "
                        f"got {event.previous_hash}."
                    )

                # ------------------------------------------------------
                # Recompute event hash.
                # ------------------------------------------------------

                timestamp_iso = _canonical_timestamp(
                    event.timestamp
                )

                event_type = _normalize_event_type(
                    event.event_type
                )

                computed_hash = _compute_sha256_hash(
                    previous_hash=event.previous_hash,
                    event_type=event_type,
                    payload=event.payload or {},
                    timestamp_iso=timestamp_iso,
                    source=event.source,
                )

                if event.hash != computed_hash:
                    raise HashChainCorruptedError(
                        f"Payload/hash mismatch at Event ID "
                        f"{event.id}. "
                        f"Computed {computed_hash}, "
                        f"stored {event.hash}."
                    )

                expected_previous_hash = event.hash

            return True

    except HashChainCorruptedError:
        raise

    except SQLAlchemyError as exc:
        logger.exception(
            "Failed to verify event chain for tenant %s.",
            clean_tenant_id,
        )

        raise EventStoreError(
            f"Failed to verify chain integrity: {exc}"
        ) from exc