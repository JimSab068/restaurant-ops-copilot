"""
Tests for Component 4 — Tool & Action Framework.

These tests use the PostgreSQL test database configured by
tests/unit/conftest.py. SQLite is intentionally not used.

Coverage:
- action/category mapping
- confidence gate behavior
- independent critic gate
- successful execution
- audit logging
- tenant isolation
- state-change event routing
- unsupported MENU_SWAP add is audited and fails closed
- malformed payload handling
- action-log validation
- cross-tenant decision integrity
"""

from unittest import result

import pytest

from Component_1.db import get_session
from Component_1.models import (
    ActionType,
    Decision,
    Event,
    EventType,
    MenuItem,
)
from Component_2.decision_engine import DecisionStateError
from Component_4.tool_framework import (
    TOOL_CATEGORY_BY_ACTION,
    UnsupportedToolActionError,
    _apply_state_change,
    _current_value_for_critic,
    execute_tool_call,
    get_action_log,
)
from Component_4.tool_models import ToolExecutionLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_decision(
    session,
    tenant_id,
    action_type=ActionType.PRICE_CHANGE,
):
    """Create the minimum Decision needed by framework tests."""
    decision = Decision(
        tenant_id=tenant_id,
        action_type=action_type,
        action_payload={},
        predicted_outcome={},
        confidence=0.90,
        confidence_threshold_cleared=True,
    )
    session.add(decision)
    session.flush()
    return decision


def test_menu_add_is_logged_as_unsupported_before_simulation(
    tenant_with_data,
    monkeypatch,
):
    monkeypatch.setattr(
        "Component_4.tool_framework.propose_and_simulate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported menu add must not be simulated")
        ),
    )

    result = execute_tool_call(
        tenant_with_data["tenant_id"],
        ActionType.MENU_SWAP,
        {"action": "add"},
    )

    assert result["executed"] is False
    assert result["reason"] == "unsupported_action"
    assert "initial price" in result["critic_reason"]


def get_events(session, tenant_id):
    return (
        session.query(Event)
        .filter(Event.tenant_id == tenant_id)
        .order_by(Event.timestamp.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


class TestToolCategoryMapping:
    def test_supplier_switch_maps_to_procurement(self):
        assert (
            TOOL_CATEGORY_BY_ACTION[ActionType.SUPPLIER_SWITCH]
            == "procurement"
        )

    def test_price_change_maps_to_pricing(self):
        assert (
            TOOL_CATEGORY_BY_ACTION[ActionType.PRICE_CHANGE]
            == "pricing"
        )

    def test_staffing_change_maps_to_staffing(self):
        assert (
            TOOL_CATEGORY_BY_ACTION[ActionType.STAFFING_CHANGE]
            == "staffing"
        )

    def test_menu_swap_maps_to_menu(self):
        assert (
            TOOL_CATEGORY_BY_ACTION[ActionType.MENU_SWAP]
            == "menu"
        )


# ---------------------------------------------------------------------------
# Current-value lookup
# ---------------------------------------------------------------------------


class TestCurrentValueForCritic:
    def test_price_change_returns_menu_item_current_price(self, tenant_with_data):
        value = _current_value_for_critic(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {"menu_item_id": tenant_with_data["pizza_id"]},
        )

        assert value == pytest.approx(12.0)

    def test_supplier_switch_returns_ingredient_current_price(
        self,
        tenant_with_data,
    ):
        value = _current_value_for_critic(
            tenant_with_data["tenant_id"],
            ActionType.SUPPLIER_SWITCH,
            {"ingredient_id": tenant_with_data["flour_id"]},
        )

        assert value == pytest.approx(1.0)

    def test_unknown_menu_item_returns_none(self, tenant_with_data):
        value = _current_value_for_critic(
            tenant_with_data["tenant_id"],
            ActionType.PRICE_CHANGE,
            {"menu_item_id": "does-not-exist"},
        )

        assert value is None

    def test_unknown_ingredient_returns_none(self, tenant_with_data):
        value = _current_value_for_critic(
            tenant_with_data["tenant_id"],
            ActionType.SUPPLIER_SWITCH,
            {"ingredient_id": "does-not-exist"},
        )

        assert value is None

    def test_non_numeric_action_has_no_current_value(self, tenant_with_data):
        value = _current_value_for_critic(
            tenant_with_data["tenant_id"],
            ActionType.STAFFING_CHANGE,
            {"headcount_delta": 2},
        )

        assert value is None


# ---------------------------------------------------------------------------
# State-change event routing
# ---------------------------------------------------------------------------


class TestApplyStateChange:
    def test_price_change_creates_menu_price_change_event(
        self,
        tenant_with_data,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        _apply_state_change(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 15.0,
            },
        )

        session = get_session()
        try:
            events = get_events(session, tenant_id)

            assert len(events) == 1
            assert events[0].event_type == EventType.MENU_PRICE_CHANGE
            assert events[0].source == "agent_action"
            assert events[0].payload["menu_item_id"] == tenant_with_data["pizza_id"]
            assert float(events[0].payload["new_price"]) == pytest.approx(15.0)
        finally:
            session.close()

    def test_supplier_switch_creates_supplier_price_change_event(
        self,
        tenant_with_data,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        _apply_state_change(
            tenant_id,
            ActionType.SUPPLIER_SWITCH,
            {
                "ingredient_id": tenant_with_data["flour_id"],
                "new_supplier_price": 1.50,
            },
        )

        session = get_session()
        try:
            events = get_events(session, tenant_id)

            assert len(events) == 1
            assert events[0].event_type == EventType.SUPPLIER_PRICE_CHANGE
            assert events[0].source == "agent_action"
            assert events[0].payload["ingredient_id"] == tenant_with_data["flour_id"]
            assert float(events[0].payload["new_price"]) == pytest.approx(1.50)
        finally:
            session.close()

    def test_menu_swap_remove_creates_menu_item_removed_event(
        self,
        tenant_with_data,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        _apply_state_change(
            tenant_id,
            ActionType.MENU_SWAP,
            {
                "action": "remove",
                "menu_item_id": tenant_with_data["pizza_id"],
            },
        )

        session = get_session()
        try:
            events = get_events(session, tenant_id)

            assert len(events) == 1
            assert events[0].event_type == EventType.MENU_ITEM_REMOVED
            assert events[0].source == "agent_action"
            assert (
                events[0].payload["menu_item_id"]
                == tenant_with_data["pizza_id"]
            )
        finally:
            session.close()

    def test_menu_swap_add_fails_closed_until_it_has_a_state_contract(
        self,
        tenant_with_data,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        with pytest.raises(UnsupportedToolActionError):
            _apply_state_change(
                tenant_id,
                ActionType.MENU_SWAP,
                {"action": "add"},
            )

    def test_staffing_change_creates_staffing_event(
        self,
        tenant_with_data,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        _apply_state_change(
            tenant_id,
            ActionType.STAFFING_CHANGE,
            {
                "headcount_delta": 2,
            },
        )

        session = get_session()
        try:
            events = get_events(session, tenant_id)

            assert len(events) == 1
            assert events[0].event_type == EventType.STAFFING_CHANGE
            assert events[0].source == "agent_action"
            assert events[0].payload["headcount_delta"] == 2
        finally:
            session.close()


# ---------------------------------------------------------------------------
# execute_tool_call — confidence gate
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    def test_low_confidence_does_not_execute(
        self,
        tenant_with_data,
        monkeypatch,
    ):
        tenant_id = tenant_with_data["tenant_id"]


        session = get_session()

        try:
            decision = create_decision(
                session,
                tenant_id,
                ActionType.PRICE_CHANGE,
            )
            decision_id = decision.id
            session.commit()
        finally:
            session.close()

        class FakeDecision:
            id = decision_id
            confidence_threshold_cleared = False
            confidence = 0.20

        calls = {
            "execute": 0,
            "critic": 0,
        }

        def fake_propose(*args, **kwargs):
            return FakeDecision()

        def fake_execute(*args, **kwargs):
            calls["execute"] += 1
            raise AssertionError(
                "execute_and_observe must not run after confidence failure"
            )

        def fake_critic(*args, **kwargs):
            calls["critic"] += 1
            raise AssertionError(
                "critic must not run after confidence failure"
            )

        monkeypatch.setattr(
            "Component_4.tool_framework.propose_and_simulate",
            fake_propose,
        )
        monkeypatch.setattr(
            "Component_4.tool_framework.execute_and_observe",
            fake_execute,
        )
        monkeypatch.setattr(
            "Component_4.tool_framework.critique",
            fake_critic,
        )

        result = execute_tool_call(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 15.0,
            },
        )

        assert result["executed"] is False
        assert result["reason"] == "held_low_confidence"
        assert result["decision_id"] == decision_id
        assert result["confidence"] == pytest.approx(0.20)
        assert calls["critic"] == 0
        assert calls["execute"] == 0


# ---------------------------------------------------------------------------
# execute_tool_call — critic gate
# ---------------------------------------------------------------------------


class TestCriticGate:
    def test_critic_rejection_does_not_execute(
        self,
        tenant_with_data,
        monkeypatch,
    ):
        tenant_id = tenant_with_data["tenant_id"]


        session = get_session()

        try:
            decision = create_decision(
                session,
                tenant_id,
                ActionType.PRICE_CHANGE,
            )
            decision_id = decision.id
            session.commit()
        finally:
            session.close()

        class FakeDecision:
            id = decision_id
            confidence_threshold_cleared = True
            confidence = 0.95

        class FakeVerdict:
            approved = False
            reason = "Price change exceeds hard limit."

        calls = {"execute": 0}

        monkeypatch.setattr(
            "Component_4.tool_framework.propose_and_simulate",
            lambda *args, **kwargs: FakeDecision(),
        )

        monkeypatch.setattr(
            "Component_4.tool_framework.critique",
            lambda *args, **kwargs: FakeVerdict(),
        )

        def fake_execute(*args, **kwargs):
            calls["execute"] += 1
            raise AssertionError(
                "execute_and_observe must not run after critic rejection"
            )

        monkeypatch.setattr(
            "Component_4.tool_framework.execute_and_observe",
            fake_execute,
        )

        result = execute_tool_call(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 100.0,
            },
        )

        assert result["executed"] is False
        assert result["reason"] == "blocked_by_critic"
        assert result["decision_id"] == decision_id        
        assert result["critic_reason"] == "Price change exceeds hard limit."
        assert calls["execute"] == 0


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------


class TestSuccessfulExecution:
    def test_approved_action_executes_and_logs(
        self,
        tenant_with_data,
        monkeypatch,
    ):
        tenant_id = tenant_with_data["tenant_id"]


        session = get_session()

        try:
            decision = create_decision(
                session,
                tenant_id,
                ActionType.PRICE_CHANGE,
            )
            decision_id = decision.id
            session.commit()
        finally:
            session.close()

        class FakeDecision:
            id = decision_id
            confidence_threshold_cleared = True
            confidence = 0.95

        class FakeVerdict:
            approved = True
            reason = "Within price-change bounds."

        class FakeResult:
            id = decision_id
            calibration_error = 0.05
            actual_outcome = {
                "margin": 0.20,
            }

        monkeypatch.setattr(
            "Component_4.tool_framework.propose_and_simulate",
            lambda *args, **kwargs: FakeDecision(),
        )

        monkeypatch.setattr(
            "Component_4.tool_framework.critique",
            lambda *args, **kwargs: FakeVerdict(),
        )

        monkeypatch.setattr(
            "Component_4.tool_framework.execute_and_observe",
            lambda *args, **kwargs: FakeResult(),
        )

        result = execute_tool_call(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 13.0,
            },
        )

        assert result["executed"] is True
        assert result["decision_id"] == decision_id
        assert result["calibration_error"] == pytest.approx(0.05)
        assert result["actual_outcome"] == {"margin": 0.20}

        session = get_session()
        try:
            logs = (
                session.query(ToolExecutionLog)
                .filter(
                    ToolExecutionLog.tenant_id == tenant_id,
                    ToolExecutionLog.decision_id == decision_id,
                )
                .all()
            )

            assert len(logs) == 1
            assert logs[0].executed is True
            assert logs[0].confidence_gate_passed is True
            assert logs[0].critic_approved is True
            assert logs[0].critic_reason == "Within price-change bounds."
            assert logs[0].tool_category == "pricing"
            assert logs[0].action_type == ActionType.PRICE_CHANGE.value
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Action logging
# ---------------------------------------------------------------------------


class TestActionLog:
    def test_low_confidence_attempt_is_logged(
        self,
        tenant_with_data,
        monkeypatch,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        session = get_session()

        try:
            decision = create_decision(
                session,
                tenant_id,
                ActionType.PRICE_CHANGE,
            )
            decision_id = decision.id
            session.commit()
        finally:
            session.close()

        class FakeDecision:
            id = decision_id
            confidence_threshold_cleared = False
            confidence = 0.30

        monkeypatch.setattr(
            "Component_4.tool_framework.propose_and_simulate",
            lambda *args, **kwargs: FakeDecision(),
        )

        result = execute_tool_call(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 13.0,
            },
        )

        assert result["executed"] is False

        logs = get_action_log(tenant_id)

        assert len(logs) == 1
        assert logs[0]["decision_id"] == decision_id
        assert logs[0]["executed"] is False
        assert logs[0]["confidence_gate_passed"] is False
        assert logs[0]["critic_approved"] is None

    def test_critic_block_is_logged(
        self,
        tenant_with_data,
        monkeypatch,
    ):
        tenant_id = tenant_with_data["tenant_id"]

        session = get_session()
        try:
            decision = create_decision(
                session,
                tenant_id,
                ActionType.PRICE_CHANGE,
            )
            decision_id = decision.id
            session.commit()
        finally:
            session.close()

        class FakeDecision:
            id = decision_id
            confidence_threshold_cleared = True
            confidence = 0.95

        class FakeVerdict:
            approved = False
            reason = "Hard business limit exceeded."

        monkeypatch.setattr(
            "Component_4.tool_framework.propose_and_simulate",
            lambda *args, **kwargs: FakeDecision(),
        )

        monkeypatch.setattr(
            "Component_4.tool_framework.critique",
            lambda *args, **kwargs: FakeVerdict(),
        )

        result = execute_tool_call(
            tenant_id,
            ActionType.PRICE_CHANGE,
            {
                "menu_item_id": tenant_with_data["pizza_id"],
                "new_price": 100.0,
            },
        )

        assert result["executed"] is False

        logs = get_action_log(tenant_id)

        assert len(logs) == 1
        assert logs[0]["critic_approved"] is False
        assert logs[0]["critic_reason"] == "Hard business limit exceeded."
        assert logs[0]["executed"] is False

    def test_action_log_is_tenant_scoped(
        self,
        tenant_with_data,
        second_tenant_with_data,
    ):
        tenant_a = tenant_with_data["tenant_id"]
        tenant_b = second_tenant_with_data["tenant_id"]

        session = get_session()
        try:
            decision_a = create_decision(session, tenant_a)
            decision_b = create_decision(session, tenant_b)

            session.add_all(
                [
                    ToolExecutionLog(
                        tenant_id=tenant_a,
                        decision_id=decision_a.id,
                        tool_category="pricing",
                        action_type=ActionType.PRICE_CHANGE.value,
                        confidence_gate_passed=False,
                        critic_approved=None,
                        critic_reason=None,
                        executed=False,
                        payload={"test": "A"},
                    ),
                    ToolExecutionLog(
                        tenant_id=tenant_b,
                        decision_id=decision_b.id,
                        tool_category="pricing",
                        action_type=ActionType.PRICE_CHANGE.value,
                        confidence_gate_passed=False,
                        critic_approved=None,
                        critic_reason=None,
                        executed=False,
                        payload={"test": "B"},
                    ),
                ]
            )
            session.commit()
        finally:
            session.close()

        logs_a = get_action_log(tenant_a)
        logs_b = get_action_log(tenant_b)

        assert len(logs_a) == 1
        assert len(logs_b) == 1

        assert logs_a[0]["payload"]["test"] == "A"
        assert logs_b[0]["payload"]["test"] == "B"


# ---------------------------------------------------------------------------
# Payload / API contract tests
#
# These currently document expected failures in the framework.
# They should become normal passing tests after the production validation
# refactor.
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    def test_missing_menu_item_id_is_rejected(
        self,
        tenant_with_data,
    ):
        with pytest.raises((KeyError, ValueError, TypeError)):
            execute_tool_call(
                tenant_with_data["tenant_id"],
                ActionType.PRICE_CHANGE,
                {"new_price": 13.0},
            )

    def test_missing_new_price_is_rejected(
        self,
        tenant_with_data,
    ):
        with pytest.raises((KeyError, ValueError, TypeError)):
            execute_tool_call(
                tenant_with_data["tenant_id"],
                ActionType.PRICE_CHANGE,
                {
                    "menu_item_id": tenant_with_data["pizza_id"],
                },
            )

    def test_missing_ingredient_id_is_rejected(
        self,
        tenant_with_data,
    ):
        with pytest.raises((KeyError, ValueError, TypeError)):
            execute_tool_call(
                tenant_with_data["tenant_id"],
                ActionType.SUPPLIER_SWITCH,
                {"new_supplier_price": 2.0},
            )

    def test_missing_new_supplier_price_is_rejected(
        self,
        tenant_with_data,
    ):
        with pytest.raises((KeyError, ValueError, TypeError)):
            execute_tool_call(
                tenant_with_data["tenant_id"],
                ActionType.SUPPLIER_SWITCH,
                {
                    "ingredient_id": tenant_with_data["flour_id"],
                },
            )

    def test_missing_headcount_delta_is_rejected(
        self,
        tenant_with_data,
    ):
        with pytest.raises((KeyError, ValueError, TypeError)):
            execute_tool_call(
                tenant_with_data["tenant_id"],
                ActionType.STAFFING_CHANGE,
                {},
            )


# ---------------------------------------------------------------------------
# Action log limit validation
#
# These document the desired API contract and should be enabled when
# get_action_log() validates its limit argument.
# ---------------------------------------------------------------------------


class TestActionLogLimit:
    def test_zero_limit_is_rejected(self, tenant_with_data):
        with pytest.raises(ValueError):
            get_action_log(
                tenant_with_data["tenant_id"],
                limit=0,
            )

    def test_negative_limit_is_rejected(self, tenant_with_data):
        with pytest.raises(ValueError):
            get_action_log(
                tenant_with_data["tenant_id"],
                limit=-1,
            )

    def test_non_integer_limit_is_rejected(self, tenant_with_data):
        with pytest.raises((TypeError, ValueError)):
            get_action_log(
                tenant_with_data["tenant_id"],
                limit="50",
            )

    def test_bool_limit_is_rejected(self, tenant_with_data):
        with pytest.raises((TypeError, ValueError)):
            get_action_log(
                tenant_with_data["tenant_id"],
                limit=True,
            )


# ---------------------------------------------------------------------------
# Cross-tenant decision reference
# ---------------------------------------------------------------------------


class TestCrossTenantDecisionReference:
    def test_cross_tenant_decision_reference_is_rejected(
        self,
        tenant_with_data,
        second_tenant_with_data,
    ):
        tenant_a = tenant_with_data["tenant_id"]
        tenant_b = second_tenant_with_data["tenant_id"]

        session = get_session()

        try:
            decision_b = create_decision(session, tenant_b)

            entry = ToolExecutionLog(
                tenant_id=tenant_a,
                decision_id=decision_b.id,
                tool_category="pricing",
                action_type=ActionType.PRICE_CHANGE.value,
                confidence_gate_passed=False,
                critic_approved=None,
                critic_reason=None,
                executed=False,
                payload={},
            )

            session.add(entry)

            # Component 1 + Component 4 now enforce the composite
            # tenant-scoped relationship at the PostgreSQL level.
            #
            # Tenant A's log cannot reference Tenant B's Decision.
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                session.flush()

        finally:
            session.rollback()
            session.close()
