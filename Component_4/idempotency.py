"""Postgres-backed, tenant-scoped idempotency keys (safe across workers)."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy.dialects.postgresql import insert as pg_insert

from Component_1.db import get_db_context, validate_tenant_id
from Component_1.models import _now, _uuid
from .idempotency_models import IdempotencyKey

logger = logging.getLogger(__name__)

IN_PROGRESS, COMPLETED, FAILED = "in_progress", "completed", "failed"

# A claim that never completed (worker crashed) is treated as abandoned after this.
STALE_AFTER = timedelta(minutes=5)


class IdempotencyKeyReuseError(ValueError):
    """Same key, different request."""


@dataclass(frozen=True)
class Claim:
    owned: bool                       # True -> caller must execute and then complete()/fail()
    result: dict[str, Any] | None = None  # when not owned: what to return instead


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    # Results contain Decimals (e.g. decision.confidence) that JSON columns reject.
    return json.loads(json.dumps(value, default=_json_default))


def request_fingerprint(action_type: str, payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"action_type": action_type, "payload": dict(payload)},
        sort_keys=True, separators=(",", ":"), default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_key(key: Any) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("idempotency key must be a non-empty string")
    if len(key) > 255:
        raise ValueError("idempotency key must be at most 255 characters")
    return key


class IdempotencyRegistry:
    """Stateless; all state lives in the idempotency_keys table."""

    def claim(self, tenant_id: str, key: str, fingerprint: str) -> Claim:
        tenant_id = validate_tenant_id(tenant_id)
        key = _validate_key(key)

        with get_db_context(tenant_id) as session:
            # If another worker holds an uncommitted claim, this blocks until it
            # commits, then reports a conflict instead of inserting.
            inserted = session.execute(
                pg_insert(IdempotencyKey)
                .values(
                    id=_uuid(),
                    tenant_id=tenant_id,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    status=IN_PROGRESS,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(IdempotencyKey.id)
            ).scalar_one_or_none()

            if inserted is not None:
                return Claim(owned=True)

            row = (
                session.query(IdempotencyKey)
                .filter(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.idempotency_key == key,
                )
                .one()
            )
            stored_fp, status, result = row.request_fingerprint, row.status, row.result

            if status == IN_PROGRESS and _now() - row.created_at > STALE_AFTER:
                row.status = FAILED
                row.result = {"error": "abandoned"}
                status = FAILED

        if stored_fp != fingerprint:
            raise IdempotencyKeyReuseError(
                "idempotency key was already used with a different request"
            )
        if status == COMPLETED:
            return Claim(False, dict(result))
        if status == IN_PROGRESS:
            return Claim(False, {"executed": False, "reason": "request_in_progress"})
        return Claim(False, {"executed": False, "reason": "previous_attempt_incomplete"})

    def complete(self, tenant_id: str, key: str, result: Mapping[str, Any]) -> dict[str, Any]:
        tenant_id = validate_tenant_id(tenant_id)
        key = _validate_key(key)
        safe = _json_safe(dict(result))
        self._finish(tenant_id, key, COMPLETED, safe)
        return safe

    def fail(self, tenant_id: str, key: str, error: BaseException) -> None:
        try:
            self._finish(
                validate_tenant_id(tenant_id), _validate_key(key),
                FAILED, {"error": type(error).__name__},
            )
        except Exception:  # never mask the original exception
            logger.exception("Could not mark idempotency key as failed")

    @staticmethod
    def _finish(tenant_id: str, key: str, status: str, result: dict[str, Any]) -> None:
        with get_db_context(tenant_id) as session:
            (
                session.query(IdempotencyKey)
                .filter(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.idempotency_key == key,
                    IdempotencyKey.status == IN_PROGRESS,
                )
                .update(
                    {"status": status, "result": result, "completed_at": _now()},
                    synchronize_session=False,
                )
            )
