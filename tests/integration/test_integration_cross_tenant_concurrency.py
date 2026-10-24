"""
Cross-tenant CONCURRENCY integration tests.

Distinct from what already exists:
  - test_rls_isolation.py: raw-SQL RLS enforcement, not app-layer locking.
  - test_integration_closed_loop.py::TestExecutionBranches /
    TestMultiWorkerState: race conditions, but SINGLE-tenant only.

Neither proves that two DIFFERENT tenants racing the same application-layer
code path (tool_framework's advisory lock, the idempotency-key claim table,
orchestrator's per-tenant cycle lock) don't serialize on each other or leak
state across tenants. That's the gap this file closes.

Design note (not directly testable without forcing a hash collision):
  tool_framework._serialization_key() locks via
  pg_advisory_xact_lock(hashtextextended(key, 0)) -- a 64-bit space, and the
  key string embeds tenant_id. orchestrator._tenant_cycle_lock() locks via
  pg_try_advisory_lock(hashtext(key)) -- hashtext() is only 32-bit. Both key
  strings are still tenant-scoped, so within-tenant locking is exact; the
  32-bit space is just smaller, making a cross-tenant collision there
  theoretically possible (astronomically unlikely) but not something a test
  can force deterministically. Flagged for awareness, not covered below.

Run:
    pytest test_integration_cross_tenant_concurrency.py -v
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from Component_1.db import get_db_context
from Component_1.models import ActionType, MenuItem

import Component_4.tool_framework as tool_framework
from Component_4.tool_framework import (
    execute_tool_call,
    execute_tool_call_idempotent,
    get_action_log,
)

from orchestrator import CycleMode, run_cycle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _menu_price(ids, item_id=None) -> Decimal:
    item_id = item_id or ids["pizza_id"]
    with get_db_context(ids["tenant_id"]) as s:
        return (
            s.query(MenuItem.current_price)
            .filter(MenuItem.id == item_id, MenuItem.tenant_id == ids["tenant_id"])
            .scalar()
        )


def _add_menu_item_for_tenant_b(ids_b, price=20.0) -> str:
    """
    second_tenant_with_data only creates Tenant/Supplier/Ingredient -- no
    MenuItem. PRICE_CHANGE is the only ActionType that can autonomously
    execute (SUPPLIER_SWITCH and STAFFING_CHANGE are always MEDIUM/
    approval-required per risk_classifier.py), so give tenant B one.
    """
    with get_db_context(ids_b["tenant_id"]) as session:
        item = MenuItem(tenant_id=ids_b["tenant_id"], name="Burger", current_price=price)
        session.add(item)
        session.flush()
        return item.id


@pytest.fixture
def force_confidence(monkeypatch):
    import dataclasses
    import Component_2.decision_engine as decision_engine

    real_simulate = decision_engine.simulate

    def _install(confidence: float) -> None:
        def patched(tenant_id, action_type, payload):
            predicted = real_simulate(tenant_id, action_type, payload)
            try:
                return dataclasses.replace(predicted, confidence=confidence)
            except TypeError:
                object.__setattr__(predicted, "confidence", confidence)
                return predicted

        monkeypatch.setattr(decision_engine, "simulate", patched)

    return _install


# ===========================================================================
# tool_framework advisory lock: cross-tenant non-serialization
# ===========================================================================

class TestCrossTenantAdvisoryLockIsolation:

    def test_two_tenants_price_changes_do_not_serialize_on_each_others_lock(
        self, tenant_with_data, second_tenant_with_data, force_confidence, monkeypatch
    ):
        """
        _serialization_key() embeds tenant_id, so tenant A and tenant B
        changing their OWN menu item's price must not block each other.

        A barrier inside _current_value_for_critic (called only AFTER the
        advisory lock is acquired) forces both threads to reach that point
        near-simultaneously. If tenant scoping in the lock key were broken,
        one thread would still be blocked acquiring the OTHER tenant's
        advisory lock and would never reach the barrier in time --
        surfacing as BrokenBarrierError via future.result().
        """
        force_confidence(0.95)
        ids_a = tenant_with_data
        ids_b = second_tenant_with_data
        menu_item_b = _add_menu_item_for_tenant_b(ids_b, price=20.0)

        barrier = threading.Barrier(2, timeout=2)
        real_read = tool_framework._current_value_for_critic

        def synced_read(*args, **kwargs):
            value = real_read(*args, **kwargs)
            barrier.wait()  # raises BrokenBarrierError if the other side never arrives
            return value

        monkeypatch.setattr(tool_framework, "_current_value_for_critic", synced_read)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                execute_tool_call, ids_a["tenant_id"], ActionType.PRICE_CHANGE,
                {"menu_item_id": ids_a["pizza_id"], "new_price": 12.30},
            )
            future_b = pool.submit(
                execute_tool_call, ids_b["tenant_id"], ActionType.PRICE_CHANGE,
                {"menu_item_id": menu_item_b, "new_price": 20.30},
            )
            result_a = future_a.result(timeout=10)
            result_b = future_b.result(timeout=10)

        assert result_a["executed"] is True, result_a
        assert result_b["executed"] is True, result_b
        assert _menu_price(ids_a) == Decimal("12.30")
        assert _menu_price(ids_b, menu_item_b) == Decimal("20.30")


# ===========================================================================
# Idempotency keys: composite (tenant_id, idempotency_key) isolation
# ===========================================================================

class TestCrossTenantIdempotencyKeyIsolation:

    def test_same_literal_key_two_tenants_both_succeed_independently(
        self, tenant_with_data, second_tenant_with_data, force_confidence
    ):
        """
        IdempotencyKey.__table_args__ has UniqueConstraint("tenant_id",
        "idempotency_key") -- the SAME literal key string used by two
        different tenants must be treated as two independent claims, not
        one blocking/short-circuiting the other.
        """
        force_confidence(0.95)
        ids_a = tenant_with_data
        ids_b = second_tenant_with_data
        menu_item_b = _add_menu_item_for_tenant_b(ids_b, price=20.0)
        shared_key = "concurrency-test-key"

        result_a = execute_tool_call_idempotent(
            ids_a["tenant_id"], shared_key, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids_a["pizza_id"], "new_price": 12.40},
        )
        result_b = execute_tool_call_idempotent(
            ids_b["tenant_id"], shared_key, ActionType.PRICE_CHANGE,
            {"menu_item_id": menu_item_b, "new_price": 20.40},
        )

        assert result_a["executed"] is True, result_a
        assert result_b["executed"] is True, result_b
        assert result_a["decision_id"] != result_b["decision_id"]
        assert _menu_price(ids_a) == Decimal("12.40")
        assert _menu_price(ids_b, menu_item_b) == Decimal("20.40")

    def test_same_literal_key_concurrent_claims_from_two_tenants_both_succeed(
        self, tenant_with_data, second_tenant_with_data, force_confidence
    ):
        force_confidence(0.95)
        ids_a = tenant_with_data
        ids_b = second_tenant_with_data
        menu_item_b = _add_menu_item_for_tenant_b(ids_b, price=20.0)
        shared_key = "concurrent-shared-key"

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                execute_tool_call_idempotent, ids_a["tenant_id"], shared_key,
                ActionType.PRICE_CHANGE,
                {"menu_item_id": ids_a["pizza_id"], "new_price": 12.50},
            )
            future_b = pool.submit(
                execute_tool_call_idempotent, ids_b["tenant_id"], shared_key,
                ActionType.PRICE_CHANGE,
                {"menu_item_id": menu_item_b, "new_price": 20.50},
            )
            result_a = future_a.result(timeout=10)
            result_b = future_b.result(timeout=10)

        assert result_a["executed"] is True, result_a
        assert result_b["executed"] is True, result_b


# ===========================================================================
# Action log: no cross-tenant leakage after concurrent execution
# ===========================================================================

class TestCrossTenantActionLogIsolation:

    def test_action_log_after_concurrent_cross_tenant_actions_shows_only_own_entries(
        self, tenant_with_data, second_tenant_with_data, force_confidence
    ):
        force_confidence(0.95)
        ids_a = tenant_with_data
        ids_b = second_tenant_with_data
        menu_item_b = _add_menu_item_for_tenant_b(ids_b, price=20.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                execute_tool_call, ids_a["tenant_id"], ActionType.PRICE_CHANGE,
                {"menu_item_id": ids_a["pizza_id"], "new_price": 12.60},
            )
            future_b = pool.submit(
                execute_tool_call, ids_b["tenant_id"], ActionType.PRICE_CHANGE,
                {"menu_item_id": menu_item_b, "new_price": 20.60},
            )
            future_a.result(timeout=10)
            future_b.result(timeout=10)

        log_a = get_action_log(ids_a["tenant_id"], limit=50)
        log_b = get_action_log(ids_b["tenant_id"], limit=50)

        assert log_a, "expected at least one log entry for tenant A"
        assert log_b, "expected at least one log entry for tenant B"
        assert all(
            entry["payload"].get("menu_item_id") == ids_a["pizza_id"] for entry in log_a
        )
        assert all(
            entry["payload"].get("menu_item_id") == menu_item_b for entry in log_b
        )


# ===========================================================================
# orchestrator per-tenant cycle lock: cross-tenant non-serialization
# ===========================================================================

class TestCrossTenantCycleLockIsolation:

    def test_two_tenants_cycles_run_concurrently_without_blocking_each_other(
        self, tenant_with_historical_demand, second_tenant_with_data
    ):
        """
        _tenant_cycle_lock() keys on f"ops-cycle:{tenant_id}", so two
        tenants' cycles must both complete rather than one reporting
        "cycle_already_running" because of the other.
        """
        ids_a = tenant_with_historical_demand
        ids_b = second_tenant_with_data

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(run_cycle, ids_a["tenant_id"], mode=CycleMode.PROPOSE)
            future_b = pool.submit(run_cycle, ids_b["tenant_id"], mode=CycleMode.PROPOSE)
            report_a = future_a.result(timeout=10)
            report_b = future_b.result(timeout=10)

        assert report_a.status == "completed", report_a.reason
        assert report_b.status == "completed", report_b.reason
        assert report_a.reason != "cycle_already_running"
        assert report_b.reason != "cycle_already_running"