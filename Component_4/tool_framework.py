"""Structured, tenant-safe execution boundary for Component 4.

Maps the JD's four categories (procurement, pricing, staffing, menu) onto
the existing ActionType enum, and wraps Component 2's decision engine with
an independent critic check before anything executes.

    Category      -> ActionType         -> what it does
    procurement   -> SUPPLIER_SWITCH    -> change supplier cost for an ingredient
    pricing       -> PRICE_CHANGE       -> change a menu item's price
    staffing      -> STAFFING_CHANGE    -> adjust headcount for a shift
    menu          -> MENU_SWAP          -> remove a menu item

KNOWN GAP (documented, not hidden): the JD specifically says "reorder" for
procurement — replenishing stock at the current supplier/price, distinct
from SWITCHING supplier. That's not modeled as its own action type yet;
SUPPLIER_SWITCH is the closest existing fit and is what's wired up here.

A true "reorder" tool (bump current_stock_level, no price/margin
implications) would be a small, low-risk addition — RESTOCK already
exists as an Event type in Component 1, it just isn't exposed as a
Decision-gated tool action yet.

Every call to execute_tool_call() flows:

    propose_and_simulate (Component 2)
        -> confidence gate
        -> critic check (independent of simulator confidence)
        -> execute_and_observe (Component 2), if both gates clear
        -> event-sourced state update
        -> ToolExecutionLog entry either way
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import (
    ActionType,
    EventType,
    Ingredient,
    MenuItem,
    StaffShift,
    SupplierSKU,
    Decision,
)
from Component_2.decision_engine import (
    execute_and_observe,
    propose_and_simulate,
)
from .critic import critique
from .tool_models import ToolExecutionLog
from Component_1.event_store import append_event
from Component_6.approval_workflow import ApprovalWorkflow
from Component_6.risk_classifier import classify_risk
from .idempotency import IdempotencyRegistry


# Adapter seam for an API/database-backed workflow. Keeping the state machine
# here ensures no caller can bypass the policy check by calling a tool directly.
approval_workflow = ApprovalWorkflow()
idempotency_registry = IdempotencyRegistry()


TOOL_CATEGORY_BY_ACTION = {
    ActionType.SUPPLIER_SWITCH: "procurement",
    ActionType.PRICE_CHANGE: "pricing",
    ActionType.STAFFING_CHANGE: "staffing",
    ActionType.MENU_SWAP: "menu",
}


class ToolFrameworkError(ValueError):
    """Base error for invalid tool-framework requests."""


class UnsupportedToolActionError(ToolFrameworkError):
    """Raised when an action has no safe execution implementation yet."""


def _validate_tool_request(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Validate at the external execution boundary and copy mutable input."""
    try:
        tenant_id = validate_tenant_id(tenant_id)
    except (TypeError, ValueError) as exc:
        raise ToolFrameworkError(str(exc)) from exc

    if not isinstance(action_type, ActionType):
        raise TypeError("action_type must be an ActionType")

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    return tenant_id, dict(payload)


def _current_value_for_critic(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> float | None:
    """
    Fetch the 'before' value the critic needs to compute a percentage
    change.

    Kept in the tool framework, not the critic itself, so the critic
    stays a pure function over numbers it's handed.
    """
    with get_db_context(tenant_id) as session:
        if action_type == ActionType.PRICE_CHANGE:
            item = (
                session.query(MenuItem)
                .filter(
                    MenuItem.id == payload.get("menu_item_id"),
                    MenuItem.tenant_id == tenant_id,
                )
                .one_or_none()
            )

            return float(item.current_price) if item else None

        if action_type == ActionType.SUPPLIER_SWITCH:
            ingredient = (
                session.query(Ingredient)
                .filter(
                    Ingredient.id == payload.get("ingredient_id"),
                    Ingredient.tenant_id == tenant_id,
                )
                .one_or_none()
            )

            return float(ingredient.current_price) if ingredient else None

        return None


def _resolve_supplier_sku_id(
    tenant_id: str,
    payload: Mapping[str, Any],
) -> str:
    """
    Resolve the SupplierSKU needed by Component 1's
    SUPPLIER_PRICE_CHANGE event contract.

    The public Component 4 payload historically only contains:

        ingredient_id
        new_supplier_price

    Component 1's event contract, however, requires supplier_sku_id.

    Resolution order:

    1. Explicit supplier_sku_id supplied by the caller.
    2. SupplierSKU belonging to the ingredient's current supplier.
    3. Any tenant-owned SupplierSKU belonging to the ingredient.

    The final fallback keeps the existing Component 4 payload contract
    usable when a fixture or tenant has an ingredient/SKU relationship
    but the legacy payload does not identify the SKU explicitly.
    """
    explicit_sku_id = payload.get("supplier_sku_id")

    with get_db_context(tenant_id) as session:
        if explicit_sku_id is not None:
            sku = (
                session.query(SupplierSKU)
                .filter(
                    SupplierSKU.id == explicit_sku_id,
                    SupplierSKU.tenant_id == tenant_id,
                )
                .one_or_none()
            )

            if sku is None:
                raise ToolFrameworkError(
                    f"Supplier SKU {explicit_sku_id} does not belong to "
                    f"tenant {tenant_id}."
                )

            ingredient_id = payload.get("ingredient_id")

            if (
                ingredient_id is not None
                and sku.ingredient_id != ingredient_id
            ):
                raise ToolFrameworkError(
                    "supplier_sku_id does not belong to the requested "
                    "ingredient."
                )

            return sku.id

        ingredient_id = payload.get("ingredient_id")

        if ingredient_id is None:
            raise ToolFrameworkError(
                "SUPPLIER_SWITCH requires ingredient_id."
            )

        ingredient = (
            session.query(Ingredient)
            .filter(
                Ingredient.id == ingredient_id,
                Ingredient.tenant_id == tenant_id,
            )
            .one_or_none()
        )

        if ingredient is None:
            raise ToolFrameworkError(
                f"Ingredient {ingredient_id} does not belong to "
                f"tenant {tenant_id}."
            )

        # Prefer the SKU associated with the ingredient's current supplier.
        current_supplier_id = getattr(
            ingredient,
            "current_supplier_id",
            None,
        )

        if current_supplier_id is None:
            current_supplier_id = getattr(
                ingredient,
                "supplier_id",
                None,
            )

        if current_supplier_id is not None:
            sku = (
                session.query(SupplierSKU)
                .filter(
                    SupplierSKU.tenant_id == tenant_id,
                    SupplierSKU.ingredient_id == ingredient_id,
                    SupplierSKU.supplier_id == current_supplier_id,
                )
                .first()
            )

            if sku is not None:
                return sku.id

        # Compatibility fallback: use any SKU for this ingredient.
        sku = (
            session.query(SupplierSKU)
            .filter(
                SupplierSKU.tenant_id == tenant_id,
                SupplierSKU.ingredient_id == ingredient_id,
            )
            .first()
        )

        if sku is None:
            raise ToolFrameworkError(
                f"No SupplierSKU exists for ingredient {ingredient_id} "
                f"within tenant {tenant_id}."
            )

        return sku.id


def _resolve_staffing_event_payload(
    tenant_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Convert Component 4's compact staffing payload into the complete
    Component 1 STAFFING_CHANGE event contract.

    Component 4 accepts:

        {"headcount_delta": 2}

    Component 1 requires:

        role
        day_of_week
        headcount

    If an existing StaffShift is available, its role/day/headcount are
    used as the target and the delta is applied.

    If no StaffShift exists, create the first shift using the compact
    action as the requested starting headcount. Explicit role,
    day_of_week, or headcount values take precedence when supplied.
    """
    if "headcount_delta" not in payload:
        raise ToolFrameworkError(
            "STAFFING_CHANGE requires headcount_delta."
        )

    delta = payload["headcount_delta"]

    # Reject booleans because bool is an int subclass.
    if isinstance(delta, bool):
        raise ToolFrameworkError(
            "headcount_delta must be an integer."
        )

    try:
        numeric_delta = int(delta)
    except (TypeError, ValueError) as exc:
        raise ToolFrameworkError(
            "headcount_delta must be an integer."
        ) from exc

    # Preserve explicit event fields when the caller supplies them.
    explicit_role = payload.get("role")
    explicit_day = payload.get("day_of_week")
    explicit_headcount = payload.get("headcount")

    if explicit_role is not None:
        role = str(explicit_role)
    else:
        role = None

    if explicit_day is not None:
        try:
            day_of_week = int(explicit_day)
        except (TypeError, ValueError) as exc:
            raise ToolFrameworkError(
                "day_of_week must be an integer."
            ) from exc
    else:
        day_of_week = None

    if explicit_headcount is not None:
        try:
            headcount = int(explicit_headcount)
        except (TypeError, ValueError) as exc:
            raise ToolFrameworkError(
                "headcount must be an integer."
            ) from exc
    else:
        headcount = None

    with get_db_context(tenant_id) as session:
        staff_shift_id = payload.get("staff_shift_id")

        query = session.query(StaffShift).filter(
            StaffShift.tenant_id == tenant_id,
        )

        # --------------------------------------------------------------
        # Explicit staff shift
        # --------------------------------------------------------------
        if staff_shift_id is not None:
            shift = (
                query.filter(
                    StaffShift.id == staff_shift_id,
                )
                .one_or_none()
            )

            if shift is None:
                raise ToolFrameworkError(
                    f"Staff shift {staff_shift_id} does not belong to "
                    f"tenant {tenant_id}."
                )

            resolved_role = role if role is not None else str(shift.role)
            resolved_day = (
                day_of_week
                if day_of_week is not None
                else int(shift.day_of_week)
            )

            if headcount is not None:
                resolved_headcount = headcount
            else:
                resolved_headcount = int(shift.headcount or 0) + numeric_delta

            if resolved_headcount < 0:
                raise ToolFrameworkError(
                    "STAFFING_CHANGE would result in a negative headcount."
                )

            return {
                "staff_shift_id": staff_shift_id,
                "role": resolved_role,
                "day_of_week": resolved_day,
                "headcount": resolved_headcount,
            }

        # --------------------------------------------------------------
        # Explicit role/day identifies an existing shift
        # --------------------------------------------------------------
        if role is not None and day_of_week is not None:
            shift = (
                query.filter(
                    StaffShift.role == role,
                    StaffShift.day_of_week == day_of_week,
                )
                .one_or_none()
            )

            if shift is not None:
                resolved_headcount = (
                    headcount
                    if headcount is not None
                    else int(shift.headcount or 0) + numeric_delta
                )

                if resolved_headcount < 0:
                    raise ToolFrameworkError(
                        "STAFFING_CHANGE would result in a negative "
                        "headcount."
                    )

                return {
                    "staff_shift_id": shift.id,
                    "role": role,
                    "day_of_week": day_of_week,
                    "headcount": resolved_headcount,
                }

            # No matching shift: create it from the supplied values.
            if headcount is None:
                headcount = numeric_delta

            if headcount < 0:
                raise ToolFrameworkError(
                    "STAFFING_CHANGE would result in a negative "
                    "headcount."
                )

            return {
                "role": role,
                "day_of_week": day_of_week,
                "headcount": headcount,
            }

        # --------------------------------------------------------------
        # Compact payload: {"headcount_delta": 2}
        #
        # The test fixture may have no StaffShift at all. In that case
        # create a valid initial staffing projection.
        # --------------------------------------------------------------
        shift = query.first()

        if shift is not None:
            resolved_role = (
                role if role is not None else str(shift.role)
            )
            resolved_day = (
                day_of_week
                if day_of_week is not None
                else int(shift.day_of_week)
            )

            resolved_headcount = (
                headcount
                if headcount is not None
                else int(shift.headcount or 0) + numeric_delta
            )

            if resolved_headcount < 0:
                raise ToolFrameworkError(
                    "STAFFING_CHANGE would result in a negative "
                    "headcount."
                )

            return {
                "staff_shift_id": shift.id,
                "role": resolved_role,
                "day_of_week": resolved_day,
                "headcount": resolved_headcount,
            }

        # No existing shift. The compact action creates the first
        # staffing projection. Use Monday (0) and a generic role unless
        # the caller explicitly supplied them.
        resolved_role = role if role is not None else "general"
        resolved_day = day_of_week if day_of_week is not None else 0

        resolved_headcount = (
            headcount
            if headcount is not None
            else numeric_delta
        )

        if resolved_headcount < 0:
            raise ToolFrameworkError(
                "STAFFING_CHANGE would result in a negative "
                "headcount."
            )

        return {
            "role": resolved_role,
            "day_of_week": resolved_day,
            "headcount": resolved_headcount,
        }

def _apply_state_change(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> None:
    """
    Write the executed action back into Component 1's live state via
    the event log.

    Component 1's event store is the source of truth for executed
    operational changes. The event and its current-state projection
    are updated atomically by append_event().

    Event mappings:

        PRICE_CHANGE
            -> MENU_PRICE_CHANGE

        SUPPLIER_SWITCH
            -> SUPPLIER_PRICE_CHANGE

        MENU_SWAP(remove)
            -> MENU_ITEM_REMOVED

        STAFFING_CHANGE
            -> STAFFING_CHANGE

    For SUPPLIER_SWITCH, Component 4's public payload uses
    new_supplier_price. Component 1's SUPPLIER_PRICE_CHANGE contract
    requires supplier_sku_id + new_price, so the SKU is resolved
    from the tenant-scoped ingredient/SKU relationship.

    For STAFFING_CHANGE, Component 4's public payload uses
    headcount_delta. Component 1's event contract requires the
    concrete target shift and resulting headcount, so the target
    shift is resolved from the tenant's existing StaffShift records.
    """
    if action_type == ActionType.PRICE_CHANGE:
        append_event(
            tenant_id,
            EventType.MENU_PRICE_CHANGE,
            {
                "menu_item_id": payload["menu_item_id"],
                "new_price": payload["new_price"],
            },
            source="agent_action",
        )

    elif action_type == ActionType.SUPPLIER_SWITCH:
        supplier_sku_id = _resolve_supplier_sku_id(
            tenant_id,
            payload,
        )

        append_event(
            tenant_id,
            EventType.SUPPLIER_PRICE_CHANGE,
            {
                "supplier_sku_id": supplier_sku_id,
                "ingredient_id": payload["ingredient_id"],
                "new_price": payload["new_supplier_price"],
            },
            source="agent_action",
        )

    elif (
        action_type == ActionType.MENU_SWAP
        and payload.get("action") == "remove"
    ):
        append_event(
            tenant_id,
            EventType.MENU_ITEM_REMOVED,
            {
                "menu_item_id": payload["menu_item_id"],
            },
            source="agent_action",
        )

    elif action_type == ActionType.STAFFING_CHANGE:
        staffing_payload = _resolve_staffing_event_payload(
            tenant_id,
            payload,
        )

        staffing_payload["headcount_delta"] = payload["headcount_delta"]


        append_event(
            tenant_id,
            EventType.STAFFING_CHANGE,
            staffing_payload,
            source="agent_action",
        )

    else:
        raise UnsupportedToolActionError(
            f"No event-sourced state change is implemented for "
            f"{action_type.value}"
        )


def _log(
    tenant_id: str,
    decision_id: str | None,
    category: str,
    action_type: ActionType,
    confidence_gate_passed: bool,
    critic_approved: bool | None,
    critic_reason: str | None,
    executed: bool,
    payload: Mapping[str, Any],
) -> None:
    """Persist a tenant-scoped ToolExecutionLog entry."""
    with get_db_context(tenant_id) as session:
        entry = ToolExecutionLog(
            tenant_id=tenant_id,
            decision_id=decision_id,
            tool_category=category,
            action_type=action_type.value,
            confidence_gate_passed=confidence_gate_passed,
            critic_approved=critic_approved,
            critic_reason=critic_reason,
            executed=executed,
            payload=dict(payload),
        )

        session.add(entry)


def execute_tool_call(
    tenant_id: str,
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """
    The single entry point Patty (or any caller) uses to attempt an
    action.

    Returns a result dict describing what happened.

    Blocked or low-confidence actions return normally with executed=False.
    Genuine programming/input errors may raise.
    """
    tenant_id, payload = _validate_tool_request(
        tenant_id,
        action_type,
        payload,
    )

    category = TOOL_CATEGORY_BY_ACTION[action_type]

    # MENU_SWAP add has no event-sourced execution contract yet.
    # It is a valid request worth auditing, but must never create a
    # simulated or executed decision that falsely suggests a menu item
    # was added.
    if (
        action_type == ActionType.MENU_SWAP
        and payload.get("action") == "add"
    ):
        reason = (
            "MENU_SWAP add is unsupported: provide a dedicated "
            "menu-create contract with name and initial price before "
            "enabling execution"
        )

        _log(
            tenant_id,
            None,
            category,
            action_type,
            confidence_gate_passed=False,
            critic_approved=None,
            critic_reason=reason,
            executed=False,
            payload=payload,
        )

        return {
            "executed": False,
            "reason": "unsupported_action",
            "critic_reason": reason,
        }

    # ------------------------------------------------------------------
    # Component 2: proposal + simulation
    # ------------------------------------------------------------------
    decision = propose_and_simulate(
        tenant_id,
        action_type,
        payload,
    )

    confidence_gate_passed = decision.confidence_threshold_cleared

    # ------------------------------------------------------------------
    # Confidence gate
    # ------------------------------------------------------------------
    if not confidence_gate_passed:
        _log(
            tenant_id,
            decision.id,
            category,
            action_type,
            confidence_gate_passed,
            critic_approved=None,
            critic_reason=None,
            executed=False,
            payload=payload,
        )

        return {
            "executed": False,
            "reason": "held_low_confidence",
            "decision_id": decision.id,
            "confidence": decision.confidence,
        }

    # ------------------------------------------------------------------
    # Independent critic
    # ------------------------------------------------------------------
    current_value = _current_value_for_critic(
        tenant_id,
        action_type,
        payload,
    )

    verdict = critique(
        action_type,
        payload,
        current_value,
    )

    if not verdict.approved:
        _log(
            tenant_id,
            decision.id,
            category,
            action_type,
            confidence_gate_passed,
            critic_approved=False,
            critic_reason=verdict.reason,
            executed=False,
            payload=payload,
        )

        return {
            "executed": False,
            "reason": "blocked_by_critic",
            "decision_id": decision.id,
            "critic_reason": verdict.reason,
        }

    # Component 6 is intentionally independent from the critic: a request may
    # satisfy hard business bounds while still being too consequential to run
    # autonomously.  Hold such work for a reviewer instead of treating the
    # simulator confidence gate as execution authority.
    risk = classify_risk(action_type, dict(payload), decision, current_value)
    if risk.requires_approval:
        approval = approval_workflow.request(
            tenant_id,
            decision.id,
            risk.reason,
        )
        _log(
            tenant_id, decision.id, category, action_type,
            confidence_gate_passed, True, risk.reason, False, payload,
        )
        return {
            "executed": False,
            "reason": "pending_human_approval",
            "decision_id": decision.id,
            "approval_request_id": approval.id,
            "risk_level": risk.risk_level.value,
        }

    # ------------------------------------------------------------------
    # Component 2: execute decision
    # ------------------------------------------------------------------
    result = execute_and_observe(
        tenant_id,
        decision.id,
    )

    # ------------------------------------------------------------------
    # Component 1: update live state through event sourcing
    #
    # execute_and_observe() records execution/calibration but does not
    # itself update operational current-state projections.
    #
    # append_event() writes the immutable event and applies its
    # projection in the same transaction.
    # ------------------------------------------------------------------
    _apply_state_change(
        tenant_id,
        action_type,
        payload,
    )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    _log(
        tenant_id,
        decision.id,
        category,
        action_type,
        confidence_gate_passed,
        critic_approved=True,
        critic_reason=verdict.reason,
        executed=True,
        payload=payload,
    )

    return {
        "executed": True,
        "decision_id": result.id,
        "calibration_error": (
            float(result.calibration_error)
            if result.calibration_error is not None
            else None
        ),
        "actual_outcome": result.actual_outcome,
    }


def execute_approved_tool_call(tenant_id: str, approval_request_id: str) -> dict[str, Any]:
    """Execute a previously approved, tenant-owned request exactly once.

    The underlying decision engine's state transition is the idempotency
    guard: an already executed/evaluated decision cannot be claimed again.
    """
    tenant_id = validate_tenant_id(tenant_id)
    approval = approval_workflow.get(tenant_id, approval_request_id)
    if approval.status.value != "approved":
        raise ToolFrameworkError("approval request has not been approved")
    with get_db_context(tenant_id) as session:
        decision = session.query(Decision).filter(
            Decision.tenant_id == tenant_id,
            Decision.id == approval.decision_id,
        ).one_or_none()
        if decision is None:
            raise ToolFrameworkError("approved decision not found for tenant")
        action_type, payload, decision_id = decision.action_type, dict(decision.action_payload), decision.id
    result = execute_and_observe(tenant_id, decision_id)
    _apply_state_change(tenant_id, action_type, payload)
    _log(tenant_id, decision_id, TOOL_CATEGORY_BY_ACTION[action_type], action_type, True, True, "human approval recorded", True, payload)
    return {"executed": True, "decision_id": result.id, "actual_outcome": result.actual_outcome, "calibration_error": float(result.calibration_error) if result.calibration_error is not None else None}


def execute_tool_call_idempotent(tenant_id: str, idempotency_key: str, action_type: ActionType, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Idempotent wrapper for callers that can provide a stable request key."""
    tenant_id = validate_tenant_id(tenant_id)
    cached = idempotency_registry.get(tenant_id, idempotency_key)
    if cached is not None:
        return cached
    return idempotency_registry.store(
        tenant_id, idempotency_key, execute_tool_call(tenant_id, action_type, payload)
    )


def get_action_log(
    tenant_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent Component 4 execution-log entries for one tenant."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")

    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    MAX_ACTION_LOG_LIMIT = 500

    if limit > MAX_ACTION_LOG_LIMIT:
        raise ValueError(
            f"limit must be between 1 and {MAX_ACTION_LOG_LIMIT}"
        )

    try:
        tenant_id = validate_tenant_id(tenant_id)
    except (TypeError, ValueError) as exc:
        raise ToolFrameworkError(str(exc)) from exc

    with get_db_context(tenant_id) as session:
        entries = (
            session.query(ToolExecutionLog)
            .filter(
                ToolExecutionLog.tenant_id == tenant_id,
            )
            .order_by(
                ToolExecutionLog.created_at.desc(),
            )
            .limit(limit)
            .all()
        )

        # Materialize to dicts inside the block. The session closes on
        # exit, so ORM objects would otherwise be detached.
        return [
            {
                "id": entry.id,
                "decision_id": entry.decision_id,
                "tool_category": entry.tool_category,
                "action_type": entry.action_type,
                "confidence_gate_passed": (
                    entry.confidence_gate_passed
                ),
                "critic_approved": entry.critic_approved,
                "critic_reason": entry.critic_reason,
                "executed": entry.executed,
                "payload": entry.payload,
                "created_at": entry.created_at,
            }
            for entry in entries
        ]
