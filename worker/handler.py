"""
Worker message handler.

process_message() is the ONLY place that bridges the queue to your
existing, already-tested pipeline (Component_4.tool_framework). Nothing
in Components 1-6 changes to support deployment -- this function just
calls execute_tool_call_idempotent() the same way your integration tests
already do, then records the outcome for Component 7 and the work_request
table.

Return value contract for worker/main.py's poll loop:
    True  -> delete the message (handled: succeeded, or failed in a way
             that will never succeed on retry, e.g. bad action_type).
    False -> do NOT delete; let SQS redeliver after the visibility
             timeout, eventually landing in the DLQ if it keeps failing.
             Used for transient-looking failures (DB connectivity, etc.)
             where a retry might actually succeed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from Component_1.db import get_db_context
from Component_1.models import ActionType

from Component_4.tool_framework import ToolFrameworkError, execute_tool_call_idempotent

from Component_7 import EvaluationLedger
from Component_7.event_adapter import from_tool_result

from shared.queue import QueueMessage
from shared.settings import SYNTHETIC_EXECUTION_ONLY

from api.work_request_models import WorkRequest

logger = logging.getLogger("worker")

_evaluation_ledger = EvaluationLedger()


class SyntheticGuardError(RuntimeError):
    """Raised when SYNTHETIC_EXECUTION_ONLY would be violated."""


def _assert_synthetic_safe(tenant_id: str, payload: dict[str, Any]) -> None:
    """
    Hard kill-switch check, run before every execution attempt.

    This deployment's whole safety story rests on never touching anything
    outside seeded synthetic tenants/data. Concretely: refuse to proceed
    if the flag is off in an environment that isn't explicitly configured
    for it, and refuse any payload that names an external connector or
    credential field (none of Component 4's real action payloads should
    ever contain one -- if one shows up, something upstream is already
    misbehaving).
    """
    if not SYNTHETIC_EXECUTION_ONLY:
        return  # explicitly configured off; not this function's call to judge

    forbidden_keys = {"external_connector", "vendor_credential_id", "webhook_url"}
    present = forbidden_keys & payload.keys()
    if present:
        raise SyntheticGuardError(
            f"payload contains fields forbidden under SYNTHETIC_EXECUTION_ONLY: "
            f"{sorted(present)}"
        )


def _mark_work_request(
    tenant_id: str,
    work_request_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    with get_db_context(tenant_id) as session:
        (
            session.query(WorkRequest)
            .filter(
                WorkRequest.tenant_id == tenant_id,
                WorkRequest.id == work_request_id,
            )
            .update(
                {
                    "status": status,
                    "result": result,
                    "error": error,
                    "updated_at": datetime.now(timezone.utc),
                    "completed_at": (
                        datetime.now(timezone.utc)
                        if status in ("completed", "failed")
                        else None
                    ),
                },
                synchronize_session=False,
            )
        )


def process_message(message: QueueMessage) -> bool:
    tenant_id = message.tenant_id
    work_request_id = message.work_request_id

    log_extra = {"tenant_id": tenant_id, "work_request_id": work_request_id}

    # Step 1: re-derive the request from the DB, tenant-scoped via RLS.
    # A forged/stale tenant_id in the message simply finds nothing here --
    # it can never cause cross-tenant execution.
    with get_db_context(tenant_id) as session:
        work_request = (
            session.query(WorkRequest)
            .filter(
                WorkRequest.tenant_id == tenant_id,
                WorkRequest.id == work_request_id,
            )
            .one_or_none()
        )

        if work_request is None:
            logger.error(
                "Work request not found for tenant; discarding message",
                extra=log_extra,
            )
            return True  # poison message; retrying will never find it

        if work_request.status != "pending":
            # At-least-once delivery: this message was already handled
            # (or is currently being handled by another worker). Not an
            # error -- just nothing left to do.
            logger.info(
                "Work request already %s; skipping",
                work_request.status,
                extra=log_extra,
            )
            return True

        action_type_str = work_request.action_type
        idempotency_key = work_request.idempotency_key
        payload = dict(work_request.payload)

        session.query(WorkRequest).filter(
            WorkRequest.tenant_id == tenant_id,
            WorkRequest.id == work_request_id,
        ).update({"status": "processing"}, synchronize_session=False)

    # Step 2: validate the action type against Component 1's real enum.
    try:
        action_type = ActionType(action_type_str)
    except ValueError:
        error = {"error": "invalid_action_type", "action_type": action_type_str}
        _mark_work_request(tenant_id, work_request_id, status="failed", error=error)
        logger.error("Invalid action_type", extra=log_extra)
        return True  # will never succeed on retry

    # Step 3: hard synthetic-only guard.
    try:
        _assert_synthetic_safe(tenant_id, payload)
    except SyntheticGuardError as exc:
        error = {"error": "synthetic_guard_violation", "detail": str(exc)}
        _mark_work_request(tenant_id, work_request_id, status="failed", error=error)
        logger.error("Synthetic guard violation: %s", exc, extra=log_extra)
        return True

    # Step 4: run the actual pipeline. This is your existing, already-
    # tested Component 4 code, unchanged.
    try:
        result = execute_tool_call_idempotent(
            tenant_id, idempotency_key, action_type, payload,
        )
    except ToolFrameworkError as exc:
        # A malformed request (bad payload shape, unknown ids, etc.) --
        # will never succeed on retry.
        error = {"error": "tool_framework_error", "detail": str(exc)}
        _mark_work_request(tenant_id, work_request_id, status="failed", error=error)
        logger.error("Tool framework rejected request: %s", exc, extra=log_extra)
        return True
    except Exception as exc:
        # Anything else (DB connectivity, etc.) is treated as transient --
        # leave status=processing, do NOT delete the message, let SQS
        # redeliver. A stuck "processing" row past a reasonable window is
        # a monitoring/alarm concern (elevated worker failure rate), not
        # something this function should paper over.
        logger.exception("Transient failure processing work request", extra=log_extra)
        return False

    # Step 5: record the outcome.
    status_value = "completed" if result.get("executed") else "failed"
    _mark_work_request(
        tenant_id, work_request_id,
        status=status_value,
        result=result if result.get("executed") else None,
        error=None if result.get("executed") else {"reason": result.get("reason"), **result},
    )

    try:
        record = from_tool_result(
            tenant_id, result, record_id=str(uuid.uuid4()),
        )
        _evaluation_ledger.record(record)
    except Exception:
        # Evaluation/telemetry must never fail the actual work outcome.
        logger.exception("Failed to record Component 7 evaluation", extra=log_extra)

    logger.info(
        "Work request %s", status_value,
        extra={**log_extra, "decision_id": result.get("decision_id"),
               "policy_outcome": result.get("reason", "executed")},
    )
    return True