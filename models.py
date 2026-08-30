
"""
Entity schema for the Restaurant Ops Copilot — Component 1 (Live Data Model).

Design notes:
- Every table is scoped by tenant_id — this is the foundation for the
  multi-tenant isolation work in Component 5. No query should ever run
  without a tenant_id filter.
- Current-state tables (MenuItem, Supplier, InventoryLevel, StaffShift)
  hold the *latest* known values for fast reads.
- The Event table is the source of truth for history — every state change
  is appended here first. Current-state tables are a derived cache that
  gets updated when an event is applied (see event_store.py).
  This lets us reconstruct "what did tenant X's state look like at time T"
  by replaying events up to T, which is what the simulation layer (Component 2)
  will need to backtest predictions against real history.

Version 2:
Production-grade upgrades applied:
- Strict Numeric/Decimal types replacing Float for currency and inventory precision.
- Composite foreign key constraints to enforce tenant isolation at DB engine level.
- Check constraints to maintain domain invariants (e.g., non-negative prices).
- Composite time-series indexes for efficient point-in-time state reconstruction.
- Model lineage fields (model_version, training_timestamp, feature_snapshot) on Decisions.
- Cryptographic hash chaining columns (hash, previous_hash) on Events for tamper auditability.


"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Index, CheckConstraint,
    CheckConstraint, ForeignKeyConstraint, Numeric,
    UniqueConstraint, Column, String, Float, Integer, 
    DateTime, ForeignKey, Enum, JSON, Boolean
)
from sqlalchemy.orm import relationship

from db import Base


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class Tenant(Base):
    """A single restaurant. The isolation boundary starts here."""
    __tablename__ = "tenants"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_supplier_tenant_id"),
    )


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    unit = Column(String, default="unit", nullable=False)

        # Current known state (derived cache)
    current_price = Column(Numeric(10, 2), nullable=False, default=0.00)
    current_supplier_id = Column(String, nullable=True)
    current_stock_level = Column(Numeric(12, 4), nullable=False, default=0.0000)

    __table_args__ = (
    ForeignKeyConstraint(
    ["tenant_id", "current_supplier_id"],
    ["suppliers.tenant_id", "suppliers.id"],
    name="fk_ingredient_supplier_tenant",
    ondelete="RESTRICT",
    ),
    CheckConstraint("current_price >= 0", name="ck_ingredient_price_positive"),
    CheckConstraint("current_stock_level >= 0", name="ck_ingredient_stock_positive"),
        )


class MenuItem(Base):
    __tablename__ = "menu_items"


    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    current_price = Column(Numeric(10, 2), nullable=False, default=0.00)
    active = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
            UniqueConstraint("tenant_id", "id", name="uq_menu_item_tenant_id"),
            CheckConstraint("current_price >= 0", name="ck_menu_item_price_positive"),
        )

class StaffShift(Base):
    __tablename__ = "staff_shifts"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)
    day_of_week = Column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    headcount = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_staff_shift_day_valid"),
        CheckConstraint("headcount >= 0", name="ck_staff_shift_headcount_positive"),
    )

class OrderVolume(Base):
    """Rolling record of order counts, used as a demand signal."""
    __tablename__ = "order_volume"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, nullable=False, index=True)
    menu_item_id = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    quantity = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "menu_item_id"],
            ["menu_items.tenant_id", "menu_items.id"],
            name="fk_order_volume_menu_item_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint("quantity > 0", name="ck_order_volume_quantity_positive"),
        Index("idx_order_volume_tenant_timestamp", "tenant_id", "timestamp"),
    )

class ActionType(str, enum.Enum):
    PRICE_CHANGE = "price_change"
    SUPPLIER_SWITCH = "supplier_switch"
    MENU_SWAP = "menu_swap"          # add or remove a menu item
    STAFFING_CHANGE = "staffing_change"


class DecisionStatus(str, enum.Enum):
    PROPOSED = "proposed"                  # action proposed, not yet simulated
    SIMULATED = "simulated"                # prediction made, cleared confidence bar
    HELD_LOW_CONFIDENCE = "held_low_confidence"  # prediction made, did NOT clear bar
    EXECUTED = "executed"                  # action was carried out
    EVALUATED = "evaluated"                # actual outcome observed, calibration computed


class Decision(Base):
    """
    One proposed action, end to end: the action itself, what the simulator
    predicted, what actually happened, and how far off the prediction was.

    This table is the calibration ledger — Component 2's whole value
    proposition (a simulator you can trust) is measured by querying this
    table over time, not by any single prediction looking reasonable.
    """
    __tablename__ = "decisions"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    action_type = Column(Enum(ActionType), nullable=False)
    action_payload = Column(JSON, nullable=False)

    predicted_outcome = Column(JSON, nullable=True)
    confidence = Column(Numeric(5, 4), nullable=True)
    confidence_threshold_cleared = Column(Boolean, nullable=True)

    actual_outcome = Column(JSON, nullable=True)
    calibration_error = Column(Numeric(8, 4), nullable=True)

    status = Column(Enum(DecisionStatus), default=DecisionStatus.PROPOSED, nullable=False)

    # Model lineage and backtesting metadata
    model_version = Column(String, nullable=True, default="1.0.0")
    training_timestamp = Column(DateTime, nullable=True)
    feature_snapshot = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=_now, nullable=False)
    evaluated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_decision_confidence_range"),
        CheckConstraint("calibration_error >= 0.0", name="ck_decision_calibration_error_positive"),
        Index("idx_decisions_tenant_status", "tenant_id", "status"),
        Index("idx_decisions_tenant_created", "tenant_id", "created_at"),
    )

class EventType(str, enum.Enum):
    SUPPLIER_PRICE_CHANGE = "supplier_price_change"
    STOCKOUT = "stockout"
    RESTOCK = "restock"
    DEMAND_SPIKE = "demand_spike"
    MENU_ITEM_ADDED = "menu_item_added"
    MENU_ITEM_REMOVED = "menu_item_removed"
    MENU_PRICE_CHANGE = "menu_price_change"
    SUPPLIER_SWITCH = "supplier_switch"
    STAFFING_CHANGE = "staffing_change"


class Event(Base):
    """
    Append-only event log — the source of truth. Never updated or deleted.

    `payload` holds event-specific fields as JSON (kept flexible on purpose;
    a fixed column per event type would require a schema migration every
    time we add a new event type, which defeats the point of an event log).
    """
    __tablename__ = "events"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(Enum(EventType), nullable=False)
    timestamp = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    source = Column(String, default="simulator", nullable=False)

    # Cryptographic ledger audit verification
    hash = Column(String(64), nullable=True)
    previous_hash = Column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_events_tenant_timestamp", "tenant_id", "timestamp"),
    )