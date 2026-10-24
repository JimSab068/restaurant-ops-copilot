"""Immutable, tenant-scoped observations used by Component 7.

This is deliberately an application contract rather than another execution
path. Adapters in Components 1--6 can emit these records after an outcome is
known; Component 7 only measures them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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

def canonical_tenant_id(tenant_id: str) -> str:
    """Validate and normalize the UUID tenant identity used by Component 1."""
    if not isinstance(tenant_id, str) or not tenant_id.strip() or "\x00" in tenant_id:
        raise ValueError("tenant_id must be a non-empty UUID string")
    try:
        return str(UUID(tenant_id.strip()))
    except ValueError as exc:
        raise ValueError("tenant_id must be a valid UUID string") from exc


def _optional_finite(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class EvaluationRecord:
    """One auditable observation for a tenant decision or safety event.

    ``None`` means the signal is not yet known; it is not silently counted as
    success or failure. This permits a record at proposal time to be enriched
    by a later execution/outcome record without corrupting aggregate rates.
    """

    tenant_id: str
    record_id: str
    decision_id: str | None = None
    action_type: str | None = None
    opportunity_was_correct: bool | None = None
    candidate_was_valid: bool | None = None
    decision_succeeded: bool | None = None
    human_overrode: bool | None = None
    policy_violation: bool | None = None
    isolation_breach: bool | None = None
    execution_failed: bool | None = None
    retry_count: int | None = None
    autonomous: bool | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    predicted_economic_impact: float | None = None
    actual_economic_impact: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", canonical_tenant_id(self.tenant_id))
        for field_name in ("record_id",):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.decision_id is not None and (not isinstance(self.decision_id, str) or not self.decision_id.strip()):
            raise ValueError("decision_id must be a non-empty string when supplied")
        if self.action_type is not None and (not isinstance(self.action_type, str) or not self.action_type.strip()):
            raise ValueError("action_type must be a non-empty string when supplied")
        for field_name in (
            "opportunity_was_correct", "candidate_was_valid", "decision_succeeded",
            "human_overrode", "policy_violation", "isolation_breach",
            "execution_failed", "autonomous",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{field_name} must be bool or None")
        if self.retry_count is not None and (isinstance(self.retry_count, bool) or not isinstance(self.retry_count, int) or self.retry_count < 0):
            raise ValueError("retry_count must be a non-negative integer or None")
        for field_name in ("latency_ms", "cost_usd", "predicted_economic_impact", "actual_economic_impact"):
            number = _optional_finite(getattr(self, field_name), field_name)
            if field_name in {"latency_ms", "cost_usd"} and number is not None and number < 0:
                raise ValueError(f"{field_name} must be non-negative")


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

    