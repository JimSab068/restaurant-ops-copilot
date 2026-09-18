"""Repository seam for durable Component 7 telemetry."""
from __future__ import annotations

from typing import Protocol
from .models import EvaluationRecord


class EvaluationRepository(Protocol):
    def append(self, record: EvaluationRecord) -> None: ...
    def list_for_tenant(self, tenant_id: str) -> list[EvaluationRecord]: ...
