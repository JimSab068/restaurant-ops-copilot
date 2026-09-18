"""Vendor-neutral ingestion contracts and deterministic in-memory adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class OperationsConnector(Protocol):
    """Read-only boundary for POS, invoice, and scheduler integrations."""
    def fetch(self, tenant_id: str) -> list[dict[str, Any]]: ...


@dataclass
class InMemoryConnector:
    """Test/development connector; never shares records across tenants."""
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def fetch(self, tenant_id: str) -> list[dict[str, Any]]:
        return [dict(record) for record in self.records.get(tenant_id, ())]


POSConnector = OperationsConnector
InvoiceConnector = OperationsConnector
SchedulerConnector = OperationsConnector
