"""
OFFLINE integration suite: Components 1-7, end to end, NO live LLM API call.

Every LLM-touching path here uses Component_3.llm_client.FakeLLMClient, so
this file has zero external network/API-key dependency and is safe to run
in a Docker build step or CI pipeline with no secrets configured.

Scope (in addition to what test_integration_closed_loop.py / test_pipeline.py
already cover for Components 1/3/4/6 at the seam level):

  Component 2  - decision_engine lifecycle called directly, with REAL
                 (unforced) confidence values, not force_confidence().
  Component 3  - LLMReasoner SUCCESS path via FakeLLMClient (a well-formed
                 fake response), not just the failure/degradation path.
  Component 6  - risk_classifier BOUNDARY values (exactly at the LOW/MEDIUM
                 line and the MEDIUM/HIGH hard limit), not just mid-band.
  Component 7  - adapters.py against real Decision/tool-call output, ledger
                 aggregation, cross-tenant rejection, duplicate-record
                 rejection, and a documented mismatch in
                 from_tool_result()'s execution_failed detection.

Requires: ops_copilot_test (see conftest.py). No RLS_* env vars, no LLM
API key.

Run:
    pytest test_integration_offline.py -v -m offline
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.offline

from Component_1.db import get_db_context
from Component_1.event_store import append_event
from Component_1.models import ActionType, Decision, DecisionStatus, EventType, MenuItem

from Component_2.decision_engine import (
    DecisionNotFoundError,
    DecisionStateError,
    execute_and_observe,
    propose_and_simulate,
)

from Component_3.llm_client import FakeLLMClient
from Component_3.llm_reasoner import LLMReasoner
from Component_3.models import CandidateSource
from Component_3.opportunity_detector import detect_opportunities

from Component_6.risk_classifier import (
    MAX_PRICE_CHANGE_PCT,
    MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION,
    RiskLevel,
    SMALL_PRICE_CHANGE_PCT,
    classify_risk,
)

from Component_4.tool_framework import execute_tool_call

from Component_7 import EvaluationLedger, evaluate
from Component_7.event_adapter import from_decision, from_tool_result
from Component_7.models import EvaluationRecord

import orchestrator as pipeline
from orchestrator import CycleMode, run_cycle


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _menu_price(ids) -> Decimal:
    with get_db_context(ids["tenant_id"]) as s:
        return (
            s.query(MenuItem.current_price)
            .filter(
                MenuItem.id == ids["pizza_id"],
                MenuItem.tenant_id == ids["tenant_id"],
            )
            .scalar()
        )


def _as_decimal(value) -> Decimal:
    """
    Some policy constants (e.g. Component_4.critic.MAX_PRICE_CHANGE_PCT)
    are plain floats rather than Decimal, unlike
    risk_classifier.SMALL_PRICE_CHANGE_PCT. Decimal arithmetic refuses to
    mix with float directly, so normalize via str() first.
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _price_at_pct(base: Decimal, pct) -> float:
    pct = _as_decimal(pct)
    return float((base * (Decimal("1") + pct)).quantize(Decimal("0.01")))


def _spike_cheese(ids, price=60.0):
    return append_event(
        ids["tenant_id"], EventType.SUPPLIER_PRICE_CHANGE,
        {
            "supplier_sku_id": ids["cheese_sku_id"],
            "ingredient_id": ids["cheese_id"],
            "new_price": price,
        },
        source="test",
    )


class _FixedConfidencePredicted:
    def __init__(self, confidence):
        self.confidence = confidence


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
# Component 6: risk-classifier boundaries
# ===========================================================================

class TestRiskClassifierBoundaries:

    def test_exactly_small_pct_price_change_is_low_risk(self, tenant_with_data):
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, SMALL_PRICE_CHANGE_PCT)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(0.95),
            current,
        )
        assert result.risk_level is RiskLevel.LOW, result.reason

    def test_just_above_small_pct_price_change_is_medium_risk(self, tenant_with_data):
        """
        Epsilon must survive currency rounding (.quantize to cents): on a
        $12.00 base, 1 cent is already a ~0.083% swing, so a tiny fractional
        epsilon like 0.0001 can round back down to exactly the boundary.
        0.01 (a full extra percentage point) is safely distinguishable.
        """
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, SMALL_PRICE_CHANGE_PCT + Decimal("0.01"))

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(0.95),
            current,
        )
        assert result.risk_level is RiskLevel.MEDIUM, result.reason

    def test_exactly_max_pct_price_change_is_not_blocked(self, tenant_with_data):
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, MAX_PRICE_CHANGE_PCT)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(0.95),
            current,
        )
        assert result.risk_level is not RiskLevel.HIGH, result.reason

    def test_just_over_max_pct_price_change_is_high_risk(self, tenant_with_data):
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, _as_decimal(MAX_PRICE_CHANGE_PCT) + Decimal("0.01"))

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(0.95),
            current,
        )
        assert result.risk_level is RiskLevel.HIGH, result.reason

    def test_confidence_exactly_at_autonomous_threshold_is_accepted(self, tenant_with_data):
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, SMALL_PRICE_CHANGE_PCT / 2)

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(float(MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION)),
            current,
        )
        assert result.risk_level is not RiskLevel.HIGH, result.reason

    def test_confidence_just_below_threshold_forces_high_risk(self, tenant_with_data):
        ids = tenant_with_data
        current = _menu_price(ids)
        price = _price_at_pct(current, SMALL_PRICE_CHANGE_PCT / 2)
        just_under = float(MIN_CONFIDENCE_FOR_AUTONOMOUS_ACTION) - 0.0001

        result = classify_risk(
            ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": price},
            _FixedConfidencePredicted(just_under),
            current,
        )
        assert result.risk_level is RiskLevel.HIGH


# ===========================================================================
# Component 2: decision_engine lifecycle, direct, real confidence
# ===========================================================================

class TestDecisionEngineLifecycleDirect:

    def test_propose_and_simulate_returns_a_coherent_decision(self, tenant_with_data):
        ids = tenant_with_data
        tid = ids["tenant_id"]

        decision = propose_and_simulate(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
        )

        assert decision.status in (
            DecisionStatus.SIMULATED, DecisionStatus.HELD_LOW_CONFIDENCE,
        )
        assert decision.confidence_threshold_cleared == (
            decision.status is DecisionStatus.SIMULATED
        )

    def test_execute_and_observe_full_lifecycle_reaches_evaluated(self, tenant_with_data):
        """
        NOTE: execute_and_observe() (Component 2) only transitions the
        Decision row (EXECUTED -> observe -> EVALUATED) and records
        actual_outcome/calibration_error. It does NOT itself mutate
        MenuItem.current_price -- that state change is Component 4's
        responsibility (tool_framework._apply_state_change, reached via
        execute_tool_call / execute_approved_tool_call), which the
        existing closed-loop integration tests already cover directly.
        Calling propose_and_simulate/execute_and_observe on their own
        (bypassing Component 4) is intentional here: it isolates Component
        2's own transaction lifecycle from Component 4's state-mutation
        responsibility.
        """
        ids = tenant_with_data
        tid = ids["tenant_id"]
        price_before = _menu_price(ids)

        decision = propose_and_simulate(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
        )
        if decision.status is not DecisionStatus.SIMULATED:
            pytest.skip("simulator did not clear confidence threshold for this input")

        evaluated = execute_and_observe(tid, decision.id)

        assert evaluated.status is DecisionStatus.EVALUATED
        assert evaluated.calibration_error is not None
        assert evaluated.calibration_error >= 0
        assert evaluated.actual_outcome is not None
        assert evaluated.evaluated_at is not None
        # Confirms this call path, by itself, does NOT apply the state
        # change -- that's Component 4's job, tested elsewhere.
        assert _menu_price(ids) == price_before

    def test_execute_and_observe_rejects_uncleared_decision(self, tenant_with_data):
        ids = tenant_with_data
        tid = ids["tenant_id"]

        with get_db_context(tid) as session:
            decision = Decision(
                tenant_id=tid,
                action_type=ActionType.PRICE_CHANGE,
                action_payload={"menu_item_id": ids["pizza_id"], "new_price": 12.5},
                predicted_outcome={"confidence": 0.1},
                confidence=Decimal("0.10"),
                confidence_threshold_cleared=False,
                status=DecisionStatus.HELD_LOW_CONFIDENCE,
            )
            session.add(decision)
            session.flush()
            decision_id = decision.id

        with pytest.raises(DecisionStateError):
            execute_and_observe(tid, decision_id)

    def test_execute_and_observe_rejects_already_executed_decision(self, tenant_with_data):
        ids = tenant_with_data
        tid = ids["tenant_id"]

        decision = propose_and_simulate(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
        )
        if decision.status is not DecisionStatus.SIMULATED:
            pytest.skip("simulator did not clear confidence threshold")

        execute_and_observe(tid, decision.id)
        with pytest.raises(DecisionStateError):
            execute_and_observe(tid, decision.id)

    def test_execute_and_observe_unknown_decision_raises_not_found(self, tenant_with_data):
        with pytest.raises(DecisionNotFoundError):
            execute_and_observe(tenant_with_data["tenant_id"], "does-not-exist")


# ===========================================================================
# Component 3: LLM reasoning SUCCESS path via FakeLLMClient (no live API)
# ===========================================================================

class TestLLMReasoningSuccessPathFake:

    def test_wellformed_fake_llm_response_yields_a_validated_candidate(
        self, tenant_with_historical_demand
    ):
        ids = tenant_with_historical_demand
        _spike_cheese(ids)

        margin_opps = [
            o for o in detect_opportunities(ids["tenant_id"])
            if o.affected_entity_ids == (ids["pizza_id"],)
        ]
        assert margin_opps
        opp = margin_opps[0]

        proposed_price = round(float(_menu_price(ids)) * 1.05, 2)
        response = json.dumps({
            "candidates": [{
                "action_type": "price_change",
                "payload": {"menu_item_id": ids["pizza_id"], "new_price": proposed_price},
                "rationale": "raise price to offset higher cheese cost",
                "requires_human_review": False,
            }]
        })

        reasoner = LLMReasoner(FakeLLMClient(response_text=response))
        candidates = reasoner.reason(opp, allowed_action_types=("price_change",))

        assert len(candidates) == 1
        assert candidates[0].source is CandidateSource.LLM
        assert candidates[0].action_type is ActionType.PRICE_CHANGE

    def test_wellformed_fake_llm_response_runs_through_the_full_pipeline(
        self, tenant_with_historical_demand
    ):
        ids = tenant_with_historical_demand
        tid = ids["tenant_id"]
        _spike_cheese(ids)

        proposed_price = round(float(_menu_price(ids)) * 1.05, 2)
        response = json.dumps({
            "candidates": [{
                "action_type": "price_change",
                "payload": {"menu_item_id": ids["pizza_id"], "new_price": proposed_price},
                "rationale": "raise price to offset higher cheese cost",
                "requires_human_review": False,
            }]
        })

        report = run_cycle(
            tid,
            llm_reasoner=LLMReasoner(FakeLLMClient(response_text=response)),
            use_llm=True,
            mode=CycleMode.PROPOSE,
        )

        assert report.status == "completed"
        if report.llm is not None:
            assert report.llm["failures"] == 0
        sources = {c["source"] for o in report.outcomes for c in o.candidates}
        assert "llm" in sources


# ===========================================================================
# Component 7: evaluation/observability against real pipeline output
# ===========================================================================

class TestComponent7Adapters:

    def test_evaluated_decision_translates_to_a_successful_record(
        self, tenant_with_data, force_confidence
    ):
        force_confidence(0.95)
        ids = tenant_with_data
        tid = ids["tenant_id"]

        evaluated = execute_and_observe(
            tid,
            propose_and_simulate(
                tid, ActionType.PRICE_CHANGE,
                {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
            ).id,
        )

        record = from_decision(tid, evaluated, record_id=str(uuid.uuid4()), autonomous=True)

        assert record.tenant_id == tid
        assert record.decision_succeeded is True
        assert record.action_type == "price_change"

    def test_ledger_aggregates_real_mixed_outcomes_correctly(
        self, tenant_with_data, force_confidence
    ):
        ids = tenant_with_data
        tid = ids["tenant_id"]
        ledger = EvaluationLedger()

        force_confidence(0.95)
        good = execute_and_observe(
            tid,
            propose_and_simulate(
                tid, ActionType.PRICE_CHANGE,
                {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
            ).id,
        )
        ledger.record(from_decision(tid, good, record_id=str(uuid.uuid4())))

        force_confidence(0.10)
        bad = propose_and_simulate(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 13.00},
        )
        ledger.record(from_decision(tid, bad, record_id=str(uuid.uuid4())))

        metrics = ledger.metrics(tid)
        assert metrics.record_count == 2
        assert metrics.decision_success_rate == 0.5

    def test_evaluate_rejects_records_from_two_real_tenants(
        self, tenant_with_data, second_tenant_with_data, force_confidence
    ):
        force_confidence(0.95)
        ids_a = tenant_with_data
        tid_a = ids_a["tenant_id"]
        tid_b = second_tenant_with_data["tenant_id"]

        decision_a = execute_and_observe(
            tid_a,
            propose_and_simulate(
                tid_a, ActionType.PRICE_CHANGE,
                {"menu_item_id": ids_a["pizza_id"], "new_price": 12.50},
            ).id,
        )
        record_a = from_decision(tid_a, decision_a, record_id=str(uuid.uuid4()))
        record_b = EvaluationRecord(
            tenant_id=tid_b, record_id=str(uuid.uuid4()), decision_succeeded=True,
        )

        with pytest.raises(ValueError):
            evaluate(tid_a, [record_a, record_b])

    def test_ledger_keeps_two_real_tenants_metrics_separate(
        self, tenant_with_data, second_tenant_with_data, force_confidence
    ):
        ids_a = tenant_with_data
        tid_a = ids_a["tenant_id"]
        tid_b = second_tenant_with_data["tenant_id"]
        ledger = EvaluationLedger()

        force_confidence(0.95)
        decision_a = execute_and_observe(
            tid_a,
            propose_and_simulate(
                tid_a, ActionType.PRICE_CHANGE,
                {"menu_item_id": ids_a["pizza_id"], "new_price": 12.50},
            ).id,
        )
        ledger.record(from_decision(tid_a, decision_a, record_id=str(uuid.uuid4())))
        ledger.record(EvaluationRecord(
            tenant_id=tid_b, record_id=str(uuid.uuid4()), decision_succeeded=False,
        ))

        assert ledger.metrics(tid_a).decision_success_rate == 1.0
        assert ledger.metrics(tid_b).decision_success_rate == 0.0

    def test_executed_tool_result_translates_to_successful_record(
        self, tenant_with_historical_demand, force_confidence
    ):
        force_confidence(0.95)
        ids = tenant_with_historical_demand
        tid = ids["tenant_id"]

        result = execute_tool_call(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
        )
        assert result["executed"] is True

        record = from_tool_result(tid, result, record_id=str(uuid.uuid4()))
        assert record.decision_succeeded is True
        assert record.execution_failed is False

    def test_held_reason_never_matches_execution_failed_literal(
        self, tenant_with_historical_demand, force_confidence
    ):
        """
        Documents current behavior: from_tool_result() only sets
        execution_failed=True for the literal reason string
        "execution_failed", which the real tool_framework never returns
        (it returns held_low_confidence / blocked_by_critic /
        pending_human_approval / unsupported_action). If this assertion
        starts failing after a fix to adapters.py, update it to `is True`.
        """
        force_confidence(0.10)
        ids = tenant_with_historical_demand
        tid = ids["tenant_id"]

        result = execute_tool_call(
            tid, ActionType.PRICE_CHANGE,
            {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
        )
        assert result["executed"] is False
        assert result["reason"] == "held_low_confidence"

        record = from_tool_result(tid, result, record_id=str(uuid.uuid4()))
        assert record.decision_succeeded is False
        assert record.execution_failed is False

    def test_duplicate_record_id_for_same_tenant_is_rejected(
        self, tenant_with_data, force_confidence
    ):
        force_confidence(0.95)
        ids = tenant_with_data
        tid = ids["tenant_id"]
        ledger = EvaluationLedger()

        decision = execute_and_observe(
            tid,
            propose_and_simulate(
                tid, ActionType.PRICE_CHANGE,
                {"menu_item_id": ids["pizza_id"], "new_price": 12.50},
            ).id,
        )
        rid = str(uuid.uuid4())
        ledger.record(from_decision(tid, decision, record_id=rid))
        with pytest.raises(ValueError):
            ledger.record(from_decision(tid, decision, record_id=rid))