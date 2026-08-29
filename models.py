
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
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, ForeignKey, Enum, JSON, Boolean
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
    created_at = Column(DateTime, default=_now)


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    unit = Column(String, default="unit")  # e.g. "kg", "unit", "liter"

    # current known state (derived cache — see module docstring)
    current_price = Column(Float, default=0.0)          # cost per unit, from supplier
    current_supplier_id = Column(String, ForeignKey("suppliers.id"), nullable=True)
    current_stock_level = Column(Float, default=0.0)


class MenuItem(Base):
    __tablename__ = "menu_items"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    current_price = Column(Float, default=0.0)   # price charged to customer
    active = Column(Boolean, default=True)         # False if 86'd / removed


class StaffShift(Base):
    __tablename__ = "staff_shifts"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    role = Column(String, nullable=False)          # e.g. "line cook", "server"
    day_of_week = Column(Integer, nullable=False)   # 0=Mon .. 6=Sun
    headcount = Column(Integer, default=1)


class OrderVolume(Base):
    """Rolling record of order counts, used as a demand signal."""
    __tablename__ = "order_volume"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    menu_item_id = Column(String, ForeignKey("menu_items.id"), nullable=False)
    timestamp = Column(DateTime, default=_now, index=True)
    quantity = Column(Integer, default=1)


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
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    action_type = Column(Enum(ActionType), nullable=False)
    action_payload = Column(JSON, nullable=False)          # the proposed action's parameters

    predicted_outcome = Column(JSON, nullable=True)        # {margin_change_pct, demand_change_pct, stockout_risk, ...}
    confidence = Column(Float, nullable=True)               # simulator's own confidence, 0-1
    confidence_threshold_cleared = Column(Boolean, nullable=True)

    actual_outcome = Column(JSON, nullable=True)            # filled in after execution + observation
    calibration_error = Column(Float, nullable=True)        # mean abs error, predicted vs actual

    status = Column(Enum(DecisionStatus), default=DecisionStatus.PROPOSED)
    created_at = Column(DateTime, default=_now)
    evaluated_at = Column(DateTime, nullable=True)


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
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    event_type = Column(Enum(EventType), nullable=False)
    timestamp = Column(DateTime, default=_now, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    # Was this event generated by the background simulator, or by an
    # agent-executed action (Component 4)? Lets us separate "world events"
    # from "actions Patty took" in the same log.
    source = Column(String, default="simulator")  # "simulator" | "agent_action"