"""Tenant-scoped idempotency keys for production action adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IdempotencyRegistry:
    """In-memory adapter; replace with a unique DB constraint in deployment."""
    _results: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get(self, tenant_id: str, key: str) -> dict[str, Any] | None:
        result = self._results.get((tenant_id, key))
        return dict(result) if result is not None else None

    def store(self, tenant_id: str, key: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("idempotency key must be a non-empty string")
        token = (tenant_id, key)
        if token in self._results:
            return dict(self._results[token])
        self._results[token] = dict(result)
        return dict(result)
