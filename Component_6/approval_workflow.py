"""Tenant-scoped human approval state machine.

The in-memory store is an adapter seam; a production caller should persist the
same fields in an RLS-protected table before invoking execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


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


@dataclass
class ApprovalWorkflow:
    _requests: dict[tuple[str, str], ApprovalRequest] = field(default_factory=dict)

    def request(self, tenant_id: str, decision_id: str, reason: str) -> ApprovalRequest:
        request = ApprovalRequest(str(uuid4()), tenant_id, decision_id, reason)
        self._requests[(tenant_id, request.id)] = request
        return request

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        request = self._requests.get((tenant_id, request_id))
        if request is None:
            raise KeyError("approval request not found for tenant")
        return request

    def resolve(self, tenant_id: str, request_id: str, *, approved: bool, reviewer_id: str, override_reason: str | None = None) -> ApprovalRequest:
        request = self._requests.get((tenant_id, request_id))
        if request is None:
            raise KeyError("approval request not found for tenant")
        if request.status is not ApprovalStatus.PENDING:
            raise ValueError("approval request has already been resolved")
        if not reviewer_id.strip():
            raise ValueError("reviewer_id is required")
        resolved = ApprovalRequest(request.id, tenant_id, request.decision_id, request.reason, ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED, reviewer_id, override_reason)
        self._requests[(tenant_id, request_id)] = resolved
        return resolved
