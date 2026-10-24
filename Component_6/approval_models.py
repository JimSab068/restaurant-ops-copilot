from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint,
    String, UniqueConstraint,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class ApprovalRequestRecord(Base):
    __tablename__ = "approval_requests"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision_id = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    reviewer_id = Column(String, nullable=True)
    override_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decisions.tenant_id", "decisions.id"],
            name="fk_approval_request_decision_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "decision_id", name="uq_approval_request_decision"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_approval_request_status",
        ),
        CheckConstraint(
            "status = 'pending' OR (reviewer_id IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_approval_request_resolved_has_reviewer",
        ),
    )