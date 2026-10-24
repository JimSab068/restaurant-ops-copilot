"""
tests/integration/test_api_acceptance_catalog.py

Full-pipeline acceptance test catalog: exercises every path claimed by the
README (autonomous execution, human-approval hold, critic block,
cross-tenant denial, safe degradation under malformed/adversarial input)
through the real HTTP surface (POST /v1/actions, GET /v1/actions/{id})
PLUS the real worker (worker.handler.process_message) -- not by calling
execute_tool_call() directly.

===========================================================================
CONFIDENCE MATH, CONFIRMED FROM Component_2/simulator.py
===========================================================================
There are TWO confidence gates in the pipeline:

  1. Component_2.decision_engine.CONFIDENCE_THRESHOLD = 0.5
     Below this, propose_and_simulate() marks the decision
     HELD_LOW_CONFIDENCE and tool_framework returns immediately with
     reason="held_low_confidence" -- the critic and risk classifier are
     never reached.

  2. Component_6.risk_classifier.MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION = 0.80
     Reached only once (1) clears. Below 0.80, classify_risk() returns a
     HIGH-risk verdict for ANY action type, forcing
     reason="pending_human_approval" even for a small, reversible price
     change.

Per-action-type confidence, as simulator.py actually computes it:

  PRICE_CHANGE:  confidence = max(0.30, 1.0 - 1.5 * |pct_change|)
      Clears gate (2) [0.80] only for |pct_change| <= ~13.3%.
      Clears gate (1) [0.50] only for |pct_change| <= ~33.3%.
      This is why the "autonomous" tests below use <=10% bumps (clears
      both gates, confidence ~0.85-0.985) and the "over-limit" critic
      test uses 32%, not 40% (40% would itself get caught by gate (1)
      as held_low_confidence, before the critic is ever consulted --
      32% clears gate (1) at confidence ~0.52 while still exceeding the
      critic's 30% hard limit).

  SUPPLIER_SWITCH: confidence is HARDCODED at 0.50 (0.40 if the
      ingredient's inventory is depleted). This barely clears gate (1)
      for our seeded, non-depleted Flour, but always fails gate (2), so
      it always ends up "pending_human_approval" (HIGH risk, via the
      confidence-based branch in classify_risk -- never even reaching
      _classify_supplier_switch's own MEDIUM-risk logic).

  STAFFING_CHANGE: confidence is HARDCODED at 0.45, regardless of
      headcount_delta. This is BELOW gate (1) [0.50], so staffing
      changes ALWAYS return "held_low_confidence" today and NEVER reach
      the critic or classify_risk at all -- contradicting the README's
      "staffing change -> approval" claim. Worth flagging to whoever
      owns Component_2/simulator.py: either this confidence should be
      >= 0.5 so staffing actually reaches the approval workflow, or the
      README's autonomy table needs updating to reflect that staffing
      is currently always held for low confidence.

  MENU_SWAP "remove": confidence is HARDCODED at 0.40. Also below gate
      (1), so menu removal ALSO always returns "held_low_confidence"
      today, never reaching classify_risk's HIGH-risk menu-removal
      branch -- contradicting the README's "menu removal -> approval,
      HIGH risk, never autonomous" claim in the same way as staffing.

  MENU_SWAP "add": short-circuited in tool_framework.execute_tool_call
      BEFORE propose_and_simulate ever runs; always
      reason="unsupported_action", confidence is irrelevant.

Reason-string constants (confirmed directly from tool_framework.py):
    "held_low_confidence"           decision.confidence_threshold_cleared=False
    "blocked_by_critic"             critic.critique() rejected
    "pending_human_approval"        risk_classifier.classify_risk() requires approval
    "unsupported_action"            MENU_SWAP action="add" (not "remove")
    "concurrent_change_in_progress" ToolBusyError (advisory lock timeout)
    executed=True carries no "reason" key at all on success.

Invalid simulator inputs are converted to ToolFrameworkError at the
Component 4 boundary. The worker can then mark malformed or unresolvable
requests as terminal failures instead of retrying them indefinitely.

WorkRequest.status only ever takes "pending" / "processing" / "completed"
/ "failed" (worker/handler.py: `"completed" if result.get("executed")
else "failed"`) -- so EVERY non-executed outcome above, including
pending_human_approval, lands as WorkRequest.status == "failed" with the
real reason inside WorkRequest.error["reason"]. "failed" here means
"did not execute this run", not "errored".
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

import api.main as api_main_module
from api.auth import hash_api_key
from api.credentials_models import ApiCredential
from api.generate_api_key import issue_key
from api.work_request_models import WorkRequest

from Component_1.db import get_session
from Component_1.event_store import append_event
from Component_1.models import (
    Tenant, Supplier, Ingredient, MenuItem, SupplierSKU, Recipe,
    RecipeIngredient, Decision, DecisionStatus, ActionType, EventType,
)
from Component_7.models import EvaluationRecordRow
from Component_3.llm_client import FakeLLMClient
from Component_3.llm_reasoner import LLMReasoner
from orchestrator import CycleMode, run_cycle

from shared.queue import QueueMessage
from worker.handler import process_message


# ===========================================================================
# Multi-tenant seeding
#
# Mirrors conftest.py's tenant_with_data fixture exactly (same shape, same
# field names, same seeded values) so this file's tenants are drop-in
# interchangeable with the existing single-tenant fixture, just multiplied
# by N. All percentage-change math in this file assumes:
#   Pizza (MenuItem) current_price = 12.0
#   Flour (Ingredient) current_price = 1.0
# ===========================================================================

# The deployment plan's acceptance target is 10 seeded tenants. Kept small
# here for a fast local/CI run; bump to 10 for the real AWS acceptance pass.
N_CATALOG_TENANTS = 3


def _seed_one_tenant(session, name: str) -> dict:
    tenant = Tenant(name=name)
    session.add(tenant)
    session.flush()

    supplier = Supplier(tenant_id=tenant.id, name=f"{name} Supplier")
    new_supplier = Supplier(tenant_id=tenant.id, name=f"{name} Replacement Supplier")
    session.add_all([supplier, new_supplier])
    session.flush()

    flour = Ingredient(
        tenant_id=tenant.id, name="Flour", unit="kg",
        current_price=1.0, current_stock_level=20.0,
        current_supplier_id=supplier.id,
    )
    cheese = Ingredient(
        tenant_id=tenant.id, name="Cheese", unit="kg",
        current_price=5.0, current_stock_level=10.0,
        current_supplier_id=supplier.id,
    )
    session.add_all([flour, cheese])
    session.flush()

    flour_sku = SupplierSKU(
        tenant_id=tenant.id, supplier_id=supplier.id, ingredient_id=flour.id,
        sku_code="FLOUR-001", description="All-purpose flour", unit="kg",
    )
    cheese_sku = SupplierSKU(
        tenant_id=tenant.id, supplier_id=supplier.id, ingredient_id=cheese.id,
        sku_code="CHEESE-001", description="Mozzarella cheese", unit="kg",
    )
    session.add_all([flour_sku, cheese_sku])
    session.flush()

    pizza = MenuItem(tenant_id=tenant.id, name="Pizza", current_price=12.0)
    session.add(pizza)
    session.flush()

    recipe = Recipe(tenant_id=tenant.id, menu_item_id=pizza.id, name="Pizza Recipe", active=True)
    session.add(recipe)
    session.flush()

    session.add_all([
        RecipeIngredient(tenant_id=tenant.id, recipe_id=recipe.id, ingredient_id=flour.id, quantity=0.25),
        RecipeIngredient(tenant_id=tenant.id, recipe_id=recipe.id, ingredient_id=cheese.id, quantity=0.15),
    ])
    session.flush()

    return {
        "tenant_id": tenant.id,
        "supplier_id": supplier.id,
        "new_supplier_id": new_supplier.id,
        "flour_id": flour.id,
        "cheese_id": cheese.id,
        "pizza_id": pizza.id,
        "flour_sku_id": flour_sku.id,
        "cheese_sku_id": cheese_sku.id,
        "recipe_id": recipe.id,
    }


@pytest.fixture
def catalog_tenants():
    """N_CATALOG_TENANTS synthetic tenants, each with one issued API key."""
    session = get_session()
    try:
        tenants = [_seed_one_tenant(session, f"Catalog Tenant {i}") for i in range(N_CATALOG_TENANTS)]
        session.commit()
    finally:
        session.close()

    for t in tenants:
        t["api_key"] = issue_key(t["tenant_id"])

    return tenants


@pytest.fixture(autouse=True)
def no_real_queue(monkeypatch):
    """Every test in this file drives the worker directly (see
    _submit_and_process below), so SQS publishing is recorded, never sent."""
    published = []

    def fake_publish(tenant_id, work_request_id):
        published.append((tenant_id, work_request_id))

    monkeypatch.setattr(api_main_module, "publish_work_request", fake_publish)
    return published


@pytest.fixture
def client():
    return TestClient(api_main_module.app)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ===========================================================================
# Shared helpers
# ===========================================================================

def _submit(client, api_key, action_type, payload, idempotency_key=None):
    return client.post(
        "/v1/actions",
        headers=_auth(api_key),
        json={
            "idempotency_key": idempotency_key or f"{action_type}-{uuid.uuid4()}",
            "action_type": action_type,
            "payload": payload,
        },
    )


def _process(tenant_id: str, work_request_id: str) -> bool:
    """Drive the request through the real worker, bypassing SQS transport.
    process_message() only needs tenant_id/work_request_id -- see
    shared/queue.py's QueueMessage / poll_messages contract; the receipt
    handle is unused by process_message itself (only by the poll loop's
    delete_message call in worker/main.py, which we never reach here)."""
    return process_message(QueueMessage(
        tenant_id=tenant_id,
        work_request_id=work_request_id,
        receipt_handle="test-receipt",
    ))


def _work_request_row(tenant_id: str, work_request_id: str) -> WorkRequest:
    session = get_session()
    try:
        return (
            session.query(WorkRequest)
            .filter(WorkRequest.tenant_id == tenant_id, WorkRequest.id == work_request_id)
            .one()
        )
    finally:
        session.close()


def _decision_for(tenant_id: str, decision_id: str) -> Decision:
    session = get_session()
    try:
        return (
            session.query(Decision)
            .filter(Decision.tenant_id == tenant_id, Decision.id == decision_id)
            .one()
        )
    finally:
        session.close()


def _submit_and_process(client, api_key, tenant_id, action_type, payload, idempotency_key=None):
    """POST /v1/actions -> drive worker -> return (submit_response,
    poll_result, work_request_row)."""
    submit_resp = _submit(client, api_key, action_type, payload, idempotency_key)
    assert submit_resp.status_code == 202
    request_id = submit_resp.json()["request_id"]

    poll_result = _process(tenant_id, request_id)
    row = _work_request_row(tenant_id, request_id)
    return submit_resp, poll_result, row


# ===========================================================================
# Per-tenant catalog: 20 requests x N_CATALOG_TENANTS
# ===========================================================================

@pytest.mark.parametrize("tenant_idx", range(N_CATALOG_TENANTS))
class TestPerTenantCatalog:
    """pytest reruns this class's methods once per tenant_idx, so a
    failure names exactly which tenant and which category broke."""

    # --- 5x autonomous price change (README S9: small price adjustment) ---
    @pytest.mark.parametrize("bump_pct", [0.01, 0.03, 0.05, 0.07, 0.10])
    def test_autonomous_price_change_executes(self, client, catalog_tenants, tenant_idx, bump_pct):
        t = catalog_tenants[tenant_idx]
        new_price = round(12.0 * (1 + bump_pct), 2)  # seeded Pizza price = 12.0

        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "price_change",
            {"menu_item_id": t["pizza_id"], "new_price": new_price},
        )
        assert poll_ok is True
        assert row.status == "completed", (
            f"expected autonomous execution at {bump_pct:.0%}; got "
            f"status={row.status} error={row.error} -- if the reason is "
            f"pending_human_approval or held_low_confidence, the "
            f"simulator's confidence for seeded data may be below the "
            f"0.80/0.50 thresholds this test assumes (see module docstring)"
        )
        assert row.result is not None
        assert "calibration_error" in row.result

        decision = _decision_for(t["tenant_id"], row.result["decision_id"])
        assert decision.status == DecisionStatus.EVALUATED

    # --- 3x approval-gated price change (README S9: large price adjustment) ---
    @pytest.mark.parametrize("bump_pct", [0.15, 0.20, 0.25])
    def test_approval_gated_price_change_holds(self, client, catalog_tenants, tenant_idx, bump_pct):
        t = catalog_tenants[tenant_idx]
        new_price = round(12.0 * (1 + bump_pct), 2)

        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "price_change",
            {"menu_item_id": t["pizza_id"], "new_price": new_price},
        )
        assert poll_ok is True
        assert row.status == "failed"  # "failed" == "did not execute" here, not an error
        assert row.error["reason"] == "pending_human_approval"
        assert row.error["risk_level"] in ("MEDIUM", "HIGH")
        assert "approval_request_id" in row.error

        decision = _decision_for(t["tenant_id"], row.error["decision_id"])
        assert decision.status == DecisionStatus.SIMULATED  # never claimed for execution

    # --- 2x supplier switch (always requires approval per risk_classifier) ---
    @pytest.mark.parametrize("new_price_ratio", [1.05, 1.20])
    def test_supplier_switch_always_requires_approval(self, client, catalog_tenants, tenant_idx, new_price_ratio):
        t = catalog_tenants[tenant_idx]
        new_supplier_price = round(1.0 * new_price_ratio, 2)  # seeded Flour price = 1.0

        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "supplier_switch",
            {"ingredient_id": t["flour_id"], "new_supplier_price": new_supplier_price},
        )
        assert poll_ok is True
        assert row.status == "failed"
        assert row.error["reason"] == "pending_human_approval"

    # --- 2x staffing change ---
    # README/deployment-plan expectation is "approval". ACTUAL current
    # behavior (simulator.py hardcodes confidence=0.45, below
    # decision_engine's 0.5 gate) is that staffing NEVER reaches the
    # critic/risk-classifier layer at all -- see module docstring.
    @pytest.mark.parametrize("headcount_delta", [1, -1])
    def test_staffing_change_is_held_for_low_confidence(self, client, catalog_tenants, tenant_idx, headcount_delta):
        t = catalog_tenants[tenant_idx]
        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "staffing_change",
            {"headcount_delta": headcount_delta},
        )
        assert poll_ok is True
        assert row.status == "failed"
        assert row.error["reason"] == "held_low_confidence", (
            "if this now reads pending_human_approval, "
            "Component_2/simulator.py's hardcoded staffing confidence "
            "(0.45) has changed -- update this test AND note that "
            "staffing changes now actually reach the approval workflow "
            "the README describes"
        )

    # --- 1x menu removal ---
    # README/deployment-plan expectation is "approval, HIGH risk, never
    # autonomous". ACTUAL current behavior (simulator.py hardcodes
    # confidence=0.40 for MENU_SWAP "remove", below decision_engine's 0.5
    # gate) is that menu removal ALSO never reaches classify_risk -- see
    # module docstring. It's still "never autonomous" (true), just for a
    # different reason than the README states (confidence gate, not the
    # risk classifier's HIGH-risk menu-removal rule).
    def test_menu_removal_is_held_for_low_confidence(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "menu_swap",
            {"menu_item_id": t["pizza_id"], "action": "remove"},
        )
        assert poll_ok is True
        assert row.status == "failed"
        assert row.error["reason"] == "held_low_confidence", (
            "if this now reads pending_human_approval with "
            "risk_level=HIGH, simulator.py's hardcoded menu-removal "
            "confidence (0.40) has changed -- update this test to check "
            "risk_level == 'HIGH' instead, matching the README's "
            "original claim"
        )

    # --- 1x over-limit price change (blocked_by_critic, >30% hard limit) ---
    def test_over_limit_price_change_blocked_by_critic(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        # 32%, not the catalog's original 40%: at 40% simulator.py's
        # confidence formula (max(0.30, 1 - 1.5*|pct_change|)) drops to
        # 0.40, which fails decision_engine's OWN 0.5 confidence gate --
        # the request would be held_low_confidence before the critic is
        # ever consulted. 32% keeps confidence at ~0.52 (clears gate 1)
        # while still exceeding critic.MAX_PRICE_CHANGE_PCT (0.30).
        new_price = round(12.0 * 1.32, 2)

        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "price_change",
            {"menu_item_id": t["pizza_id"], "new_price": new_price},
        )
        assert poll_ok is True
        assert row.status == "failed"
        assert row.error["reason"] == "blocked_by_critic"
        assert "critic_reason" in row.error
        assert "30%" in row.error["critic_reason"]

    # --- malformed payloads fail permanently instead of retrying forever ---
    def test_malformed_payload_missing_new_price_marks_request_failed(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        # api/main.py only validates action_type against the ActionType enum
        # (see submit_action) -- payload shape isn't checked there, so this
        # reaches the worker rather than bouncing as a 422.
        submit_resp = _submit(
            client, t["api_key"], "price_change",
            {"menu_item_id": t["pizza_id"]},  # new_price omitted
        )
        assert submit_resp.status_code == 202
        request_id = submit_resp.json()["request_id"]

        poll_ok = _process(t["tenant_id"], request_id)
        row = _work_request_row(t["tenant_id"], request_id)

        assert poll_ok is True
        assert row.status == "failed"
        assert row.result is None
        assert row.error["error"] == "tool_framework_error"

    def test_malformed_payload_nonexistent_menu_item_marks_request_failed(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        fake_menu_item_id = str(uuid.uuid4())
        submit_resp = _submit(
            client, t["api_key"], "price_change",
            {"menu_item_id": fake_menu_item_id, "new_price": 13.0},
        )
        assert submit_resp.status_code == 202
        request_id = submit_resp.json()["request_id"]

        poll_ok = _process(t["tenant_id"], request_id)
        row = _work_request_row(t["tenant_id"], request_id)

        assert poll_ok is True
        assert row.status == "failed"
        assert row.result is None
        assert row.error["error"] == "tool_framework_error"

    # --- 2x idempotent resubmission ---
    @pytest.mark.parametrize("action_type,payload_fn", [
        ("price_change", lambda t: {"menu_item_id": t["pizza_id"], "new_price": 12.3}),
        ("staffing_change", lambda t: {"headcount_delta": 1}),
    ])
    def test_idempotent_resubmission_returns_same_request(
        self, client, catalog_tenants, tenant_idx, action_type, payload_fn,
    ):
        t = catalog_tenants[tenant_idx]
        key = f"idem-{action_type}-{uuid.uuid4()}"
        payload = payload_fn(t)

        first = _submit(client, t["api_key"], action_type, payload, key)
        second = _submit(client, t["api_key"], action_type, payload, key)

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["request_id"] == second.json()["request_id"]

        session = get_session()
        try:
            count = (
                session.query(WorkRequest)
                .filter(WorkRequest.tenant_id == t["tenant_id"], WorkRequest.idempotency_key == key)
                .count()
            )
        finally:
            session.close()
        assert count == 1

    # --- 1x concurrent duplicate race ---
    def test_concurrent_duplicate_submissions_collapse_to_one_request(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        key = f"race-{uuid.uuid4()}"
        payload = {"menu_item_id": t["pizza_id"], "new_price": 12.4}

        def submit():
            return _submit(client, t["api_key"], "price_change", payload, key)

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = [f.result() for f in [pool.submit(submit) for _ in range(20)]]

        assert all(r.status_code == 202 for r in results)
        assert len({r.json()["request_id"] for r in results}) == 1

    # --- 1x cross-tenant reference attempt ---
    def test_cross_tenant_menu_item_reference_is_denied(self, client, catalog_tenants, tenant_idx):
        t = catalog_tenants[tenant_idx]
        other = catalog_tenants[(tenant_idx + 1) % N_CATALOG_TENANTS]

        _, poll_ok, row = _submit_and_process(
            client, t["api_key"], t["tenant_id"], "price_change",
            {"menu_item_id": other["pizza_id"], "new_price": 13.5},  # tenant B's real menu item
        )
        # The tenant-scoped lookup rejects tenant B's id, and the invalid
        # reference is terminally failed rather than endlessly retried.
        assert poll_ok is True
        assert row.status == "failed"
        assert row.error["error"] == "tool_framework_error"

        # tool_framework._current_value_for_critic() filters by
        # (MenuItem.id == ..., MenuItem.tenant_id == tenant_id), so tenant
        # B's real id resolves to nothing under tenant A's RLS-scoped
        # session. Confirm tenant B's price is actually untouched:
        session = get_session()
        try:
            other_item = (
                session.query(MenuItem)
                .filter(MenuItem.tenant_id == other["tenant_id"], MenuItem.id == other["pizza_id"])
                .one()
            )
            assert float(other_item.current_price) == 12.0  # unchanged
        finally:
            session.close()


# ===========================================================================
# Adversarial requests -- fired once per full run, not per-tenant
# ===========================================================================

class TestAdversarialOnce:

    def test_revoked_key_never_reaches_queue(self, client, catalog_tenants, no_real_queue):
        t = catalog_tenants[0]
        raw_key = issue_key(t["tenant_id"])
        session = get_session()
        try:
            session.query(ApiCredential).filter(
                ApiCredential.key_hash == hash_api_key(raw_key)
            ).update({"active": False})
            session.commit()
        finally:
            session.close()

        resp = _submit(client, raw_key, "price_change", {"menu_item_id": t["pizza_id"], "new_price": 12.5})
        assert resp.status_code == 401
        assert len(no_real_queue) == 0

    def test_unknown_key_is_rejected(self, client, no_real_queue):
        resp = _submit(client, "not-a-real-key", "price_change", {})
        assert resp.status_code == 401
        assert len(no_real_queue) == 0

    def test_tenant_id_in_body_is_silently_ignored(self, client, catalog_tenants, no_real_queue):
        t, other = catalog_tenants[0], catalog_tenants[1]
        resp = client.post(
            "/v1/actions",
            headers=_auth(t["api_key"]),
            json={
                "idempotency_key": f"smuggle-{uuid.uuid4()}",
                "action_type": "price_change",
                "payload": {"menu_item_id": t["pizza_id"], "new_price": 12.6},
                "tenant_id": other["tenant_id"],  # ActionRequest has no such field; pydantic drops it
            },
        )
        assert resp.status_code == 202
        assert no_real_queue[0][0] == t["tenant_id"]
        assert no_real_queue[0][0] != other["tenant_id"]

        row = _work_request_row(t["tenant_id"], resp.json()["request_id"])
        assert row.tenant_id == t["tenant_id"]

    def test_get_for_another_tenants_request_id_returns_404_not_403(self, client, catalog_tenants, no_real_queue):
        t, other = catalog_tenants[0], catalog_tenants[1]
        submit = _submit(client, t["api_key"], "price_change", {"menu_item_id": t["pizza_id"], "new_price": 12.7})
        request_id = submit.json()["request_id"]

        resp = client.get(f"/v1/actions/{request_id}", headers=_auth(other["api_key"]))
        assert resp.status_code == 404  # not 403 -- existence isn't confirmed either


class TestCycleApiSurface:
    """The cloud-facing entry point for the Component 1 -> 7 loop."""

    def test_cycle_endpoint_uses_authenticated_tenant_and_llm(
        self, client, catalog_tenants, monkeypatch
    ):
        t = catalog_tenants[0]
        captured = {}
        sentinel_reasoner = object()

        monkeypatch.setattr(api_main_module, "BedrockLLMClient", lambda: object())
        monkeypatch.setattr(
            api_main_module, "LLMReasoner", lambda provider: sentinel_reasoner,
        )

        report = {
            "cycle_id": "cycle-1", "tenant_id": t["tenant_id"],
            "mode": "simulate", "status": "completed", "reason": None,
            "actions_attempted": 0, "llm": {"calls": 1, "failures": 0},
            "outcomes": [],
        }

        def fake_run_cycle(tenant_id, **kwargs):
            captured["tenant_id"] = tenant_id
            captured.update(kwargs)
            return SimpleNamespace(to_dict=lambda: report)

        monkeypatch.setattr(api_main_module, "run_cycle", fake_run_cycle)
        response = client.post(
            "/v1/cycles",
            headers=_auth(t["api_key"]),
            json={
                "mode": "simulate", "use_llm": True,
                "max_opportunities": 2, "max_actions": 1,
                "tenant_id": "attacker-controlled-value",
            },
        )

        assert response.status_code == 200
        assert response.json() == report
        assert captured["tenant_id"] == t["tenant_id"]
        assert captured["llm_reasoner"] is sentinel_reasoner
        assert captured["use_llm"] is True
        assert captured["require_rls"] is True


class TestFullComponentLoop:
    """Component 1 -> 3 (detector plus validated LLM) -> 2 -> 4 -> 6 -> 7.

    The HTTP action catalog above intentionally starts at Component 4. This
    complementary test proves the actual autonomous-loop entry point,
    ``run_cycle()``, crosses every component before an action can be
    attempted. The fake provider makes its result deterministic; the live
    Bedrock smoke test remains responsible for the external model seam.
    """

    def test_event_to_llm_to_policy_to_observability_loop(self, catalog_tenants):
        t = catalog_tenants[0]
        observed = []
        append_event(
            t["tenant_id"], EventType.SUPPLIER_PRICE_CHANGE,
            {
                "supplier_sku_id": t["cheese_sku_id"],
                "ingredient_id": t["cheese_id"],
                "new_price": 60.0,
            },
            source="acceptance-test",
        )
        llm = FakeLLMClient(response_text=(
            "{\"candidates\":[{\"action_type\":\"price_change\","
            "\"payload\":{\"menu_item_id\":\"%s\",\"new_price\":12.6},"
            "\"rationale\":\"Recover margin with a bounded price change.\","
            "\"requires_human_review\":true,\"assumptions\":[]}] }"
        ) % t["pizza_id"])

        report = run_cycle(
            t["tenant_id"], mode=CycleMode.EXECUTE,
            llm_reasoner=LLMReasoner(llm), use_llm=True,
            scorer=lambda _prediction: 1.0, max_actions=1,
            observer=lambda event, payload: observed.append((event, payload)),
        )

        assert report.status == "completed"
        assert report.llm is not None
        assert report.llm["calls"] >= 1
        assert report.llm["failures"] == 0
        assert llm.calls, "Component 3 must have called the configured LLM"
        assert any(event == "opportunities_detected" for event, _ in observed)
        assert any(event == "candidates_generated" for event, _ in observed)
        assert any(event == "cycle_finished" for event, _ in observed)
        assert report.outcomes
        assert all(outcome.status.value != "error" for outcome in report.outcomes)
        assert any(
            candidate["source"] == "llm"
            for outcome in report.outcomes
            for candidate in outcome.candidates
        )


# ===========================================================================
# Release-gate checks (README S17 / deployment plan acceptance criteria)
#
# Run these AFTER the full catalog above (and, for the real AWS pass, after
# the 20-concurrent-requests/15-30-minute load run described in the source
# document) against the same database.
# ===========================================================================

class TestReleaseGates:

    def test_no_duplicate_work_requests_per_tenant_idempotency_key(self):
        session = get_session()
        try:
            dupes = (
                session.query(WorkRequest.tenant_id, WorkRequest.idempotency_key)
                .group_by(WorkRequest.tenant_id, WorkRequest.idempotency_key)
                .having(func.count(WorkRequest.id) > 1)
                .all()
            )
        finally:
            session.close()
        assert dupes == []

    def test_no_medium_or_high_risk_action_ever_auto_executed(self):
        session = get_session()
        try:
            bad = (
                session.query(Decision)
                .filter(
                    Decision.status == DecisionStatus.EVALUATED,
                    Decision.action_type.in_([ActionType.SUPPLIER_SWITCH, ActionType.MENU_SWAP]),
                )
                .all()
            )
        finally:
            session.close()
        assert bad == [], (
            f"{len(bad)} supplier_switch/menu_swap decisions reached EVALUATED "
            f"(i.e. were auto-executed) -- these action types must always "
            f"require human approval"
        )

    def test_no_work_request_stuck_in_processing(self):
        """A row stuck in 'processing' past the run window indicates a
        swallowed transient failure."""
        session = get_session()
        try:
            stuck = session.query(WorkRequest).filter(WorkRequest.status == "processing").all()
        finally:
            session.close()
        assert stuck == []

    def test_every_failed_work_request_has_a_reason(self):
        """Basic sanity check, independent of the evaluation-ledger gates
        below: every non-executed WorkRequest carries SOME machine-
        readable reason in .error, even though the richer per-outcome
        Component_7 check lives in TestEvaluationLedger now that
        event_adapter.py is available."""
        session = get_session()
        try:
            failed_without_reason = (
                session.query(WorkRequest)
                .filter(WorkRequest.status == "failed", WorkRequest.error.is_(None))
                .all()
            )
        finally:
            session.close()
        assert failed_without_reason == []


class TestEvaluationLedger:
    """Component_7/evaluation_records checks, now buildable against the
    real event_adapter.py. Two corrections to the README's original "every
    blocked_by_critic/pending_human_approval/held_low_confidence outcome
    has a matching evaluation_records row with isolation_breach=false"
    gate, found by tracing event_adapter.from_tool_result():

    1. isolation_breach is NEVER set to False by from_tool_result() -- it
       takes no isolation_breach argument at all, so every row
       worker/handler.py creates gets EvaluationRecord's dataclass default
       (None), not False. A literal `isolation_breach = false` SQL check
       against this codebase would match zero rows regardless of whether
       anything was actually breached -- it isn't verifying the invariant
       the README wants it to. test_isolation_breach_is_never_explicitly_
       true below is the check that's actually meaningful today: nothing
       should ever be EXPLICITLY flagged True, since nothing sets it at
       all currently. If real isolation-breach detection is wired up
       later, this test starts actually proving something; right now it
       mostly documents that the field is unused.

    2. execution_failed is dead code as currently wired: from_tool_result()
       only sets it True when result.get("reason") == "execution_failed",
       but no path in tool_framework.py ever returns that string (see the
       reason-string constants in the module docstring). Not asserted on
       here since there's nothing meaningful to check yet.

    One more thing I could NOT verify: I have EvaluationRecord (the
    dataclass) and EvaluationRecordRow (the table) and the two adapter
    functions, but not Component_7's EvaluationLedger class itself (likely
    in Component_7/__init__.py or a ledger.py), so I'm assuming
    .record(record) persists an EvaluationRecordRow with matching field
    values via session.add()/commit(), not verified directly.
    """

    def test_every_processed_work_request_has_an_evaluation_record(self):
        """For every WorkRequest the worker actually finished handling
        (status completed or failed), there should be a matching
        evaluation_records row, joined on decision_id."""
        session = get_session()
        try:
            processed = (
                session.query(WorkRequest)
                .filter(WorkRequest.status.in_(["completed", "failed"]))
                .all()
            )
            missing = []
            for wr in processed:
                decision_id = (wr.result or wr.error or {}).get("decision_id")
                if decision_id is None:
                    # unsupported_action (MENU_SWAP "add") is the one
                    # outcome whose result dict carries no decision_id at
                    # all -- see tool_framework.execute_tool_call's early
                    # return for that branch.
                    continue
                match = (
                    session.query(EvaluationRecordRow)
                    .filter(
                        EvaluationRecordRow.tenant_id == wr.tenant_id,
                        EvaluationRecordRow.decision_id == decision_id,
                    )
                    .one_or_none()
                )
                if match is None:
                    missing.append((wr.id, wr.status, decision_id))
        finally:
            session.close()
        assert missing == [], (
            f"{len(missing)} WorkRequest(s) have no matching "
            f"evaluation_records row: {missing}"
        )

    def test_isolation_breach_is_never_explicitly_true(self):
        """See class docstring point 1 -- this is the meaningful version
        of the README's isolation_breach gate given that nothing in the
        traced path currently sets it to False, only ever leaves it NULL."""
        session = get_session()
        try:
            breaches = (
                session.query(EvaluationRecordRow)
                .filter(EvaluationRecordRow.isolation_breach.is_(True))
                .all()
            )
        finally:
            session.close()
        assert breaches == [], (
            f"{len(breaches)} evaluation_records row(s) explicitly "
            f"flagged isolation_breach=True"
        )
