"""
Postgres-backed storage for Component 7's EvaluationRecord.

Mirrors the shape of Component_4/tool_models.py and
Component_4/idempotency_models.py: tenant-scoped, RLS-enabled via
Component_5/enable_rls.py's generic `tenant_id = current_setting(...)`
policy (this table just needs adding to TENANT_SCOPED_TABLES there).

record_id is unique PER TENANT (not globally), matching the semantics the
in-memory EvaluationLedger previously enforced in Python
(`if observation.record_id in tenant_records: raise ValueError`) -- now
enforced by the database via UniqueConstraint("tenant_id", "record_id").
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class EvaluationRecordRow(Base):
    """One durable Component 7 observation."""

    __tablename__ = "evaluation_records"

    id = Column(String, primary_key=True, default=_uuid)

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Caller-supplied identifier; unique per tenant, not globally.
    record_id = Column(String, nullable=False)

    decision_id = Column(String, nullable=True)
    action_type = Column(String, nullable=True)

    opportunity_was_correct = Column(Boolean, nullable=True)
    candidate_was_valid = Column(Boolean, nullable=True)
    decision_succeeded = Column(Boolean, nullable=True)
    human_overrode = Column(Boolean, nullable=True)
    policy_violation = Column(Boolean, nullable=True)
    isolation_breach = Column(Boolean, nullable=True)
    execution_failed = Column(Boolean, nullable=True)
    autonomous = Column(Boolean, nullable=True)

    retry_count = Column(Integer, nullable=True)

    latency_ms = Column(Float, nullable=True)
    cost_usd = Column(Float, nullable=True)
    predicted_economic_impact = Column(Float, nullable=True)
    actual_economic_impact = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "record_id",
            name="uq_evaluation_record_tenant_record_id",
        ),
        CheckConstraint(
            "retry_count IS NULL OR retry_count >= 0",
            name="ck_evaluation_record_retry_count_nonnegative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_evaluation_record_latency_nonnegative",
        ),
        CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0",
            name="ck_evaluation_record_cost_nonnegative",
        ),
    )

    