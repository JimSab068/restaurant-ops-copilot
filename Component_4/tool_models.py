"""
Additive model for Component 4 — the tool/action framework's execution log.

Kept in a separate file rather than editing Component_1/models.py directly.
Component 1's models.py is the established, hardened schema and should not
be modified implicitly by Component 4.

Every attempted tool action is recorded here, including actions that are
held by the confidence gate or blocked by the independent critic.

Execution invariant enforced at the database level:

    executed=True
        -> confidence_gate_passed=True
        -> critic_approved=True

Cross-tenant decision integrity
--------------------------------
Component 1 provides ``UNIQUE(tenant_id, id)`` for decisions. This model
uses a composite foreign key, so a log entry cannot reference a decision
owned by a different tenant.

RLS
---
This table is tenant-scoped and intentionally retains the exact column name
`tenant_id`.

enable_rls.py can therefore apply the existing policy:

    tenant_id = current_setting('app.current_tenant')

No alternate tenant identifier or relationship-based tenant field is used.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
    String,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class ToolExecutionLog(Base):
    """
    Every tool-call attempt, whether it was allowed or blocked, and why.

    This is the audit trail the JD's 'tool and action framework' calls for:
    procurement/pricing/staffing/menu actions all flow through
    execute_tool_call(), and every attempt lands here regardless of outcome.

    Tenant isolation:
    - tenant_id is mandatory because this table is RLS-scoped.
    - decision_id uses a composite foreign key so a log can only reference
      a Decision belonging to the same tenant.

    Cross-tenant integrity:
    Component 1's Decision model provides UNIQUE(tenant_id, id), allowing
    this composite foreign key to be enforced by PostgreSQL.
    """
    __tablename__ = "tool_execution_log"

    id = Column(String, primary_key=True, default=_uuid)

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    decision_id = Column(
        String,
        nullable=True,
    )

    tool_category = Column(String, nullable=False)
    action_type = Column(String, nullable=False)

    confidence_gate_passed = Column(Boolean, nullable=False)

    critic_approved = Column(Boolean, nullable=True)

    critic_reason = Column(String, nullable=True)

    executed = Column(Boolean, nullable=False, default=False)

    payload = Column(JSON, nullable=False)

    created_at = Column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decisions.tenant_id", "decisions.id"],
            name="fk_tool_execution_log_decision_tenant",
            ondelete="RESTRICT",
        ),

        CheckConstraint(
            "NOT executed OR "
            "(confidence_gate_passed AND critic_approved)",
            name="ck_tool_execution_executed_requires_gates",
        ),

        Index(
            "idx_tool_execution_log_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )
