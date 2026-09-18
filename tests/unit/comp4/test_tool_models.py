"""
PostgreSQL tests for Component 4's ToolExecutionLog model.

These tests intentionally use the project's real PostgreSQL test database
configured by tests/unit/conftest.py.

SQLite is deliberately not used because the production application runs
PostgreSQL and these tests verify PostgreSQL foreign-key and CHECK
constraint behavior.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from Component_1.db import get_session
from Component_1.models import ActionType, Decision, Tenant
from Component_4.tool_models import ToolExecutionLog


def create_tenant(session, name="Test Tavern"):
    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=name,
    )
    session.add(tenant)
    session.flush()
    return tenant


def create_decision(session, tenant_id):
    decision = Decision(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        action_type=ActionType.PRICE_CHANGE,
        action_payload={
            "menu_item_id": "item-1",
            "new_price": 12.50,
        },
    )
    session.add(decision)
    session.flush()
    return decision


def create_log(
    tenant_id,
    decision_id=None,
    *,
    confidence_gate_passed=True,
    critic_approved=True,
    executed=False,
):
    return ToolExecutionLog(
        tenant_id=tenant_id,
        decision_id=decision_id,
        tool_category="pricing",
        action_type=ActionType.PRICE_CHANGE.value,
        confidence_gate_passed=confidence_gate_passed,
        critic_approved=critic_approved,
        critic_reason="test",
        executed=executed,
        payload={
            "menu_item_id": "item-1",
            "new_price": 12.50,
        },
    )


class TestToolExecutionLogConstraints:

    def test_executed_requires_confidence_gate_and_critic(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(
                tenant.id,
                confidence_gate_passed=False,
                critic_approved=True,
                executed=True,
            )

            session.add(entry)

            with pytest.raises(IntegrityError):
                session.flush()

        finally:
            session.rollback()
            session.close()

    def test_executed_requires_critic_approval(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(
                tenant.id,
                confidence_gate_passed=True,
                critic_approved=False,
                executed=True,
            )

            session.add(entry)

            with pytest.raises(IntegrityError):
                session.flush()

        finally:
            session.rollback()
            session.close()

    def test_executed_with_both_gates_passed_is_valid(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(
                tenant.id,
                confidence_gate_passed=True,
                critic_approved=True,
                executed=True,
            )

            session.add(entry)
            session.flush()

            assert entry.executed is True

        finally:
            session.rollback()
            session.close()

    def test_confidence_block_can_have_null_critic(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(
                tenant.id,
                confidence_gate_passed=False,
                critic_approved=None,
                executed=False,
            )

            session.add(entry)
            session.flush()

            assert entry.critic_approved is None
            assert entry.executed is False

        finally:
            session.rollback()
            session.close()


class TestTenantForeignKey:

    def test_tenant_fk_accepts_existing_tenant(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(tenant.id)

            session.add(entry)
            session.flush()

            assert entry.tenant_id == tenant.id

        finally:
            session.rollback()
            session.close()

    def test_tenant_delete_cascades_tool_execution_logs(self):
        session = get_session()

        try:
            tenant = create_tenant(session)

            entry = create_log(tenant.id)

            session.add(entry)
            session.flush()

            entry_id = entry.id

            session.delete(tenant)
            session.flush()

            remaining = (
                session.query(ToolExecutionLog)
                .filter(ToolExecutionLog.id == entry_id)
                .one_or_none()
            )

            assert remaining is None

        finally:
            session.rollback()
            session.close()


class TestDecisionReferenceGap:


    def test_decision_reference_must_belong_to_same_tenant(self):
        session = get_session()

        try:
            tenant_a = create_tenant(session, "Tenant A")
            tenant_b = create_tenant(session, "Tenant B")

            decision_b = create_decision(session, tenant_b.id)

            cross_tenant_log = create_log(
                tenant_a.id,
                decision_id=decision_b.id,
            )

            session.add(cross_tenant_log)

            # This currently DOES NOT raise because decision_id is only
            # checked against decisions.id, not decisions.tenant_id.
            #
            # Once Component 1 adds UNIQUE(tenant_id, id) and Component 4
            # changes to a composite FK, this becomes the expected
            # IntegrityError.
            with pytest.raises(IntegrityError):
                session.flush()

        finally:
            session.rollback()
            session.close()
