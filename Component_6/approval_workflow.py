"""Tenant-scoped human approval state machine, persisted in Postgres."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import _now
from .approval_models import ApprovalRequestRecord as Record


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tenant_id: str
    decision_id: str
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_id: str | None = None
    override_reason: str | None = None


def _to_request(row: Record) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        tenant_id=row.tenant_id,
        decision_id=row.decision_id,
        reason=row.reason,
        status=ApprovalStatus(row.status),
        reviewer_id=row.reviewer_id,
        override_reason=row.override_reason,
    )


class ApprovalWorkflow:
    """Stateless; safe to instantiate per worker."""

    def request(self, tenant_id: str, decision_id: str, reason: str) -> ApprovalRequest:
        """Create an approval request. One per decision: re-requesting returns the existing one."""
        tenant_id = validate_tenant_id(tenant_id)
        with get_db_context(tenant_id) as session:
            session.execute(
                pg_insert(Record)
                .values(
                    id=str(uuid4()), tenant_id=tenant_id, decision_id=decision_id,
                    reason=reason, status=ApprovalStatus.PENDING.value,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "decision_id"])
            )
            row = (
                session.query(Record)
                .filter(Record.tenant_id == tenant_id, Record.decision_id == decision_id)
                .one()
            )
            return _to_request(row)

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        tenant_id = validate_tenant_id(tenant_id)
        with get_db_context(tenant_id) as session:
            row = (
                session.query(Record)
                .filter(Record.tenant_id == tenant_id, Record.id == request_id)
                .one_or_none()
            )
            if row is None:
                raise KeyError("approval request not found for tenant")
            return _to_request(row)

    def resolve(
        self, tenant_id: str, request_id: str, *,
        approved: bool, reviewer_id: str, override_reason: str | None = None,
    ) -> ApprovalRequest:
        tenant_id = validate_tenant_id(tenant_id)
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ValueError("reviewer_id is required")

        new_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

        with get_db_context(tenant_id) as session:
            # Atomic compare-and-set: only one concurrent resolver can win.
            won = session.execute(
                update(Record)
                .where(
                    Record.tenant_id == tenant_id,
                    Record.id == request_id,
                    Record.status == ApprovalStatus.PENDING.value,
                )
                .values(
                    status=new_status.value, reviewer_id=reviewer_id,
                    override_reason=override_reason, resolved_at=_now(),
                )
                .returning(Record.id)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()

            row = (
                session.query(Record)
                .filter(Record.tenant_id == tenant_id, Record.id == request_id)
                .one_or_none()
            )
            resolved = _to_request(row) if row is not None else None

        if resolved is None:
            raise KeyError("approval request not found for tenant")
        if won is None:
            raise ValueError("approval request has already been resolved")
        return resolved
    