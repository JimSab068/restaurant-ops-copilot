"""
Entity schema for the Restaurant Ops Copilot — Component 1.

Architecture:

    Tenant
       │
       ├── MenuItem ── Recipe ── Ingredient
       │                         │
       │                         └── SupplierSKU ── Supplier
       │                                            │
       │                                            └── SupplierPrice
       │
       ├── Invoice ── InvoiceLine ── SupplierSKU
       │
       ├── InventoryLevel ── Ingredient
       │
       ├── SalesOrder ── SalesOrderLine ── MenuItem
       │
       ├── WasteRecord ── Ingredient
       │
       ├── StaffShift
       │
       ├── Decision
       │
       └── Event

Design principles:

- Every operational table is scoped by tenant_id.
- Tenant ownership is enforced as close to the database layer as practical.
- Current-state tables are projections optimized for fast reads.
- Event is the historical source of truth for operational state changes.
- Historical state can be reconstructed by replaying events.
- Currency uses Decimal-compatible PostgreSQL NUMERIC types.
- Domain invariants are enforced with database CHECK constraints.
- Composite foreign keys prevent cross-tenant references.
- Operational history remains separate from current-state projections.
- Decisions retain model lineage for Component 2 calibration.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)

from .db import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

class Tenant(Base):
    """Root tenant isolation boundary."""

    __tablename__ = "tenants"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    name = Column(
        String,
        nullable=False,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

class Supplier(Base):
    """A supplier belonging to exactly one restaurant tenant."""

    __tablename__ = "suppliers"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name = Column(
        String,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_supplier_tenant_id",
        ),
    )


# ---------------------------------------------------------------------------
# Ingredient
# ---------------------------------------------------------------------------

class Ingredient(Base):
    """
    Logical/raw ingredient.

    Ingredient represents the restaurant's conceptual material rather than a
    particular supplier's SKU.

    Current price, supplier and stock are retained for compatibility with the
    existing operational model. Supplier-specific procurement information is
    represented by SupplierSKU and SupplierPrice.
    """

    __tablename__ = "ingredients"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name = Column(
        String,
        nullable=False,
    )

    unit = Column(
        String,
        default="unit",
        nullable=False,
    )

    current_price = Column(
        Numeric(10, 2),
        nullable=False,
        default=0.00,
    )

    current_supplier_id = Column(
        String,
        nullable=True,
    )

    current_stock_level = Column(
        Numeric(12, 4),
        nullable=False,
        default=0.0000,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "current_supplier_id"],
            ["suppliers.tenant_id", "suppliers.id"],
            name="fk_ingredient_supplier_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_ingredient_tenant_id",
        ),
        CheckConstraint(
            "current_price >= 0",
            name="ck_ingredient_price_nonnegative",
        ),
        CheckConstraint(
            "current_stock_level >= 0",
            name="ck_ingredient_stock_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Supplier SKU
# ---------------------------------------------------------------------------

class SupplierSKU(Base):
    """
    Supplier-specific purchasable SKU.

    This separates the restaurant's logical ingredient from the supplier's
    commercial representation of that ingredient.
    """

    __tablename__ = "supplier_skus"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    supplier_id = Column(
        String,
        nullable=False,
    )

    ingredient_id = Column(
        String,
        nullable=False,
    )

    sku_code = Column(
        String,
        nullable=False,
    )

    description = Column(
        String,
        nullable=True,
    )

    unit = Column(
        String,
        nullable=False,
        default="unit",
    )

    active = Column(
        Boolean,
        nullable=False,
        default=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "supplier_id"],
            ["suppliers.tenant_id", "suppliers.id"],
            name="fk_supplier_sku_supplier_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "ingredient_id"],
            ["ingredients.tenant_id", "ingredients.id"],
            name="fk_supplier_sku_ingredient_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "supplier_id",
            "sku_code",
            name="uq_supplier_sku_code",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_supplier_sku_tenant_id",
        ),
    )


# ---------------------------------------------------------------------------
# Supplier price
# ---------------------------------------------------------------------------

class SupplierPrice(Base):
    """
    Historical supplier pricing.

    Event remains the operational source of truth; this table is a structured
    representation useful for procurement and analytical queries.
    """

    __tablename__ = "supplier_prices"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    supplier_sku_id = Column(
        String,
        nullable=False,
    )

    price = Column(
        Numeric(10, 2),
        nullable=False,
    )

    effective_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "supplier_sku_id"],
            ["supplier_skus.tenant_id", "supplier_skus.id"],
            name="fk_supplier_price_sku_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "price >= 0",
            name="ck_supplier_price_nonnegative",
        ),
        Index(
            "idx_supplier_prices_tenant_effective",
            "tenant_id",
            "effective_at",
        ),
    )


# ---------------------------------------------------------------------------
# Menu item
# ---------------------------------------------------------------------------

class MenuItem(Base):
    """Current menu item state."""

    __tablename__ = "menu_items"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name = Column(
        String,
        nullable=False,
    )

    current_price = Column(
        Numeric(10, 2),
        nullable=False,
        default=0.00,
    )

    active = Column(
        Boolean,
        default=True,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_menu_item_tenant_id",
        ),
        CheckConstraint(
            "current_price >= 0",
            name="ck_menu_item_price_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------

class Recipe(Base):
    """
    Recipe linking a menu item to its ingredient requirements.

    This is the critical bridge that allows inventory and supplier events to
    affect menu-item economics.
    """

    __tablename__ = "recipes"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    menu_item_id = Column(
        String,
        nullable=False,
    )

    name = Column(
        String,
        nullable=False,
    )

    active = Column(
        Boolean,
        nullable=False,
        default=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "menu_item_id"],
            ["menu_items.tenant_id", "menu_items.id"],
            name="fk_recipe_menu_item_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_recipe_tenant_id",
        ),
    )


# ---------------------------------------------------------------------------
# Recipe ingredients
# ---------------------------------------------------------------------------

class RecipeIngredient(Base):
    """Ingredient quantity required by a recipe."""

    __tablename__ = "recipe_ingredients"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    recipe_id = Column(
        String,
        nullable=False,
    )

    ingredient_id = Column(
        String,
        nullable=False,
    )

    quantity = Column(
        Numeric(12, 4),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "recipe_id"],
            ["recipes.tenant_id", "recipes.id"],
            name="fk_recipe_ingredient_recipe_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "ingredient_id"],
            ["ingredients.tenant_id", "ingredients.id"],
            name="fk_recipe_ingredient_ingredient_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "recipe_id",
            "ingredient_id",
            name="uq_recipe_ingredient",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_recipe_ingredient_quantity_positive",
        ),
    )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class InventoryLevel(Base):
    """
    Current inventory projection.

    Event history records inventory-changing events. This table provides the
    fast operational read model.
    """

    __tablename__ = "inventory_levels"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    ingredient_id = Column(
        String,
        nullable=False,
    )

    quantity = Column(
        Numeric(12, 4),
        nullable=False,
        default=0.0000,
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ingredient_id"],
            ["ingredients.tenant_id", "ingredients.id"],
            name="fk_inventory_ingredient_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "ingredient_id",
            name="uq_inventory_tenant_ingredient",
        ),
        CheckConstraint(
            "quantity >= 0",
            name="ck_inventory_quantity_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

class Invoice(Base):
    """Supplier invoice header."""

    __tablename__ = "invoices"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    supplier_id = Column(
        String,
        nullable=False,
    )

    invoice_number = Column(
        String,
        nullable=False,
    )

    invoice_date = Column(
        DateTime(timezone=True),
        nullable=False,
    )

    total_amount = Column(
        Numeric(12, 2),
        nullable=False,
        default=0.00,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "supplier_id"],
            ["suppliers.tenant_id", "suppliers.id"],
            name="fk_invoice_supplier_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_invoice_tenant_id",
        ),
        UniqueConstraint(
        "tenant_id",
        "invoice_number",
        name="uq_invoice_number_tenant",
    ),
        CheckConstraint(
            "total_amount >= 0",
            name="ck_invoice_total_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Invoice line
# ---------------------------------------------------------------------------

class InvoiceLine(Base):
    """Individual supplier SKU purchased on an invoice."""

    __tablename__ = "invoice_lines"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    invoice_id = Column(
        String,
        nullable=False,
    )

    supplier_sku_id = Column(
        String,
        nullable=False,
    )

    quantity = Column(
        Numeric(12, 4),
        nullable=False,
    )

    unit_price = Column(
        Numeric(10, 2),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_invoice_line_invoice_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "supplier_sku_id"],
            ["supplier_skus.tenant_id", "supplier_skus.id"],
            name="fk_invoice_line_sku_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_invoice_line_quantity_positive",
        ),
        CheckConstraint(
            "unit_price >= 0",
            name="ck_invoice_line_price_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

class SalesOrder(Base):
    """Historical order header used as a demand signal."""

    __tablename__ = "sales_orders"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    ordered_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
        "tenant_id",
        "id",
        name="uq_sales_order_tenant_id",
    ),
        Index(
            "idx_sales_orders_tenant_ordered",
            "tenant_id",
            "ordered_at",
        ),
    )


class SalesOrderLine(Base):
    """Menu-item demand contained in a sales order."""

    __tablename__ = "sales_order_lines"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    order_id = Column(
        String,
        nullable=False,
    )

    menu_item_id = Column(
        String,
        nullable=False,
    )

    quantity = Column(
        Integer,
        nullable=False,
    )

    unit_price = Column(
        Numeric(10, 2),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["sales_orders.tenant_id", "sales_orders.id"],
            name="fk_sales_line_order_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "menu_item_id"],
            ["menu_items.tenant_id", "menu_items.id"],
            name="fk_sales_line_menu_item_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_sales_line_quantity_positive",
        ),
        CheckConstraint(
            "unit_price >= 0",
            name="ck_sales_line_price_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Backward-compatible demand projection
# ---------------------------------------------------------------------------

class OrderVolume(Base):
    """
    Rolling demand signal.

    Retained because Component 2 currently consumes this representation.
    Longer term this can be derived from SalesOrder/SalesOrderLine events.
    """

    __tablename__ = "order_volume"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    menu_item_id = Column(
        String,
        nullable=False,
    )

    timestamp = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        index=True,
    )

    quantity = Column(
        Integer,
        default=1,
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "menu_item_id"],
            ["menu_items.tenant_id", "menu_items.id"],
            name="fk_order_volume_menu_item_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_order_volume_quantity_positive",
        ),
        Index(
            "idx_order_volume_tenant_timestamp",
            "tenant_id",
            "timestamp",
        ),
    )


# ---------------------------------------------------------------------------
# Waste
# ---------------------------------------------------------------------------

class WasteRecord(Base):
    """Historical record of ingredient waste."""

    __tablename__ = "waste_records"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    ingredient_id = Column(
        String,
        nullable=False,
    )

    quantity = Column(
        Numeric(12, 4),
        nullable=False,
    )

    reason = Column(
        String,
        nullable=False,
    )

    recorded_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ingredient_id"],
            ["ingredients.tenant_id", "ingredients.id"],
            name="fk_waste_ingredient_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_waste_quantity_positive",
        ),
    )


# ---------------------------------------------------------------------------
# Staff shifts
# ---------------------------------------------------------------------------

class StaffShift(Base):
    """Current staffing configuration."""

    __tablename__ = "staff_shifts"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role = Column(
        String,
        nullable=False,
    )

    day_of_week = Column(
        Integer,
        nullable=False,
    )

    headcount = Column(
        Integer,
        default=1,
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="ck_staff_shift_day_valid",
        ),
        CheckConstraint(
            "headcount >= 0",
            name="ck_staff_shift_headcount_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

class ActionType(str, enum.Enum):
    PRICE_CHANGE = "price_change"
    SUPPLIER_SWITCH = "supplier_switch"
    MENU_SWAP = "menu_swap"
    STAFFING_CHANGE = "staffing_change"


class DecisionStatus(str, enum.Enum):
    PROPOSED = "proposed"
    SIMULATED = "simulated"
    HELD_LOW_CONFIDENCE = "held_low_confidence"
    EXECUTED = "executed"
    EVALUATED = "evaluated"


class Decision(Base):
    """
    Calibration ledger for Component 2.

    A Decision records:

        proposed
            ↓
        simulated
            ↓
        executed
            ↓
        evaluated

    Predicted and actual outcomes are retained so simulator calibration can
    be measured over time.
    """

    __tablename__ = "decisions"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    action_type = Column(
        Enum(ActionType),
        nullable=False,
    )

    action_payload = Column(
        JSON,
        nullable=False,
    )

    predicted_outcome = Column(
        JSON,
        nullable=True,
    )

    confidence = Column(
        Numeric(5, 4),
        nullable=True,
    )

    confidence_threshold_cleared = Column(
        Boolean,
        nullable=True,
    )

    actual_outcome = Column(
        JSON,
        nullable=True,
    )

    calibration_error = Column(
        Numeric(8, 4),
        nullable=True,
    )

    status = Column(
        Enum(DecisionStatus),
        default=DecisionStatus.PROPOSED,
        nullable=False,
    )

    model_version = Column(
        String,
        nullable=True,
        default="1.0.0",
    )

    training_timestamp = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    feature_snapshot = Column(
        JSON,
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
    )

    evaluated_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_decision_tenant_id",
        ),
        CheckConstraint(
            "confidence IS NULL OR "
            "(confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_decision_confidence_range",
        ),
        CheckConstraint(
            "calibration_error IS NULL OR calibration_error >= 0.0",
            name="ck_decision_calibration_error_nonnegative",
        ),
        Index(
            "idx_decisions_tenant_status",
            "tenant_id",
            "status",
        ),
        Index(
            "idx_decisions_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class EventType(str, enum.Enum):
    # Procurement / supplier events
    SUPPLIER_PRICE_CHANGE = "supplier_price_change"
    SUPPLIER_SWITCH = "supplier_switch"

    # Inventory events
    STOCKOUT = "stockout"
    RESTOCK = "restock"
    INVENTORY_ADJUSTMENT = "inventory_adjustment"
    WASTE_RECORDED = "waste_recorded"

    # Demand events
    DEMAND_SPIKE = "demand_spike"
    ORDER_RECORDED = "order_recorded"

    # Menu events
    MENU_ITEM_ADDED = "menu_item_added"
    MENU_ITEM_REMOVED = "menu_item_removed"
    MENU_PRICE_CHANGE = "menu_price_change"

    # Staffing events
    STAFFING_CHANGE = "staffing_change"

    # Procurement / invoice events
    INVOICE_RECORDED = "invoice_recorded"


class Event(Base):
    """
    Append-only operational event log.

    Event is the historical source of truth.

    Current-state tables such as Ingredient, MenuItem and InventoryLevel are
    projections optimized for operational reads.

    Hash and previous_hash provide a tamper-evident per-tenant chain.
    """

    __tablename__ = "events"

    id = Column(
        String,
        primary_key=True,
        default=_uuid,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type = Column(
        Enum(EventType),
        nullable=False,
    )

    timestamp = Column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        index=True,
    )

    payload = Column(
        JSON,
        nullable=False,
        default=dict,
    )

    source = Column(
        String,
        default="simulator",
        nullable=False,
    )

    hash = Column(
        String(64),
        nullable=False,
    )

    previous_hash = Column(
        String(64),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "idx_events_tenant_timestamp",
            "tenant_id",
            "timestamp",
        ),
        CheckConstraint(
            "length(hash) = 64",
            name="ck_event_hash_length",
        ),
        CheckConstraint(
            "length(previous_hash) = 64",
            name="ck_event_previous_hash_length",
        ),
    )