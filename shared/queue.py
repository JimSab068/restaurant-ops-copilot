"""
SQS FIFO queue wrapper, shared by api/ (publish) and worker/ (consume).

Design choice worth calling out: the message body carries ONLY tenant_id
and work_request_id -- never the action_type/payload themselves. The
worker re-fetches the full WorkRequest row from Postgres, tenant-scoped
via get_db_context(tenant_id), before doing anything else. This means:

  - a forged/corrupted queue message can, at worst, name a tenant_id that
    doesn't own that work_request_id -- in which case the tenant-scoped
    RLS-filtered lookup simply finds nothing, and the message is treated
    as invalid (see worker/handler.py). It can never cause one tenant's
    action to run under another tenant's identity.
  - Postgres remains the single source of truth for what's being
    requested; the queue is purely a wakeup/ordering signal, matching the
    plan's "RDS is the source of truth ... SQS is transport only".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import boto3

from .settings import AWS_REGION, SQS_ENDPOINT_URL, SQS_QUEUE_URL

logger = logging.getLogger(__name__)

_sqs_client = None


def _client():
    global _sqs_client
    if _sqs_client is None:
        client_options = {"region_name": AWS_REGION}
        if SQS_ENDPOINT_URL:
            client_options["endpoint_url"] = SQS_ENDPOINT_URL
        _sqs_client = boto3.client("sqs", **client_options)
    return _sqs_client


def publish_work_request(tenant_id: str, work_request_id: str) -> None:
    """Enqueue a work request. MessageGroupId=tenant_id serializes each
    tenant's own work while letting different tenants run fully in
    parallel -- the transport-layer mirror of tool_framework's per-tenant
    advisory locks and orchestrator's per-tenant cycle lock."""
    body = json.dumps({
        "tenant_id": tenant_id,
        "work_request_id": work_request_id,
    })

    _client().send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=body,
        MessageGroupId=tenant_id,
        # SQS-level dedup (a message delivered twice by the transport) is
        # a different concern from Component 4's idempotency_key (the
        # same LOGICAL action requested twice). This guards the former.
        MessageDeduplicationId=work_request_id,
    )


@dataclass(frozen=True)
class QueueMessage:
    tenant_id: str
    work_request_id: str
    receipt_handle: str


def poll_messages(max_messages: int = 10, wait_seconds: int = 10) -> list[QueueMessage]:
    response = _client().receive_message(
        QueueUrl=SQS_QUEUE_URL,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=wait_seconds,
    )

    messages: list[QueueMessage] = []
    for raw in response.get("Messages", []):
        try:
            body: dict[str, Any] = json.loads(raw["Body"])
            messages.append(QueueMessage(
                tenant_id=body["tenant_id"],
                work_request_id=body["work_request_id"],
                receipt_handle=raw["ReceiptHandle"],
            ))
        except (KeyError, json.JSONDecodeError) as exc:
            logger.error("Malformed queue message, deleting: %s", exc)
            # A malformed message can never become valid on retry --
            # delete it immediately rather than let it exhaust its
            # receive count and clutter the DLQ with noise.
            delete_message(raw["ReceiptHandle"])

    return messages


def delete_message(receipt_handle: str) -> None:
    _client().delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)