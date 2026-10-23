"""
Durable, tenant-scoped work-request record.

Distinct from Component_4/idempotency_models.py's IdempotencyKey table:
that one guards Component 4's tool-call EXECUTION (propose/simulate/
critic/act). This one is the client-facing REQUEST lifecycle -- did the
API ever receive this, is it queued, is a worker processing it, what
should GET /v1/actions/{id} return. A client's idempotency_key is the same
string passed through to execute_tool_call_idempotent(), so Component 4's
own dedup still applies underneath this; this table exists so a client can
poll status before/independent of that deeper execution layer completing.

Tenant-scoped -- add to Component_5/enable_rls.py's TENANT_SCOPED_TABLES.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    JSON,
    String,
    UniqueConstraint,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class WorkRequest(Base):
    __tablename__ = "work_requests"

    id = Column(String, primary_key=True, default=_uuid)

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    idempotency_key = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)

    status = Column(String, nullable=False, default="pending")
    result = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_work_request_tenant_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_work_request_status",
        ),
    )