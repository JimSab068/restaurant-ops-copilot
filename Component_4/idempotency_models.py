from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, JSON, String, UniqueConstraint,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(String, nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    status = Column(String, nullable=False, default="in_progress")
    result = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_idempotency_status",
        ),
    )