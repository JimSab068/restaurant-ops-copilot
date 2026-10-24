"""Shared environment configuration for api/ and worker/."""

from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SQS_QUEUE_URL = _require("SQS_QUEUE_URL")
SQS_ENDPOINT_URL = os.environ.get("SQS_ENDPOINT_URL")

# Hard kill-switch. When true, the worker refuses any action whose result
# would touch anything outside the seeded synthetic tenants/data -- see
# worker/handler.py's enforcement of this flag before every execution.
SYNTHETIC_EXECUTION_ONLY = _bool_env("SYNTHETIC_EXECUTION_ONLY", default=True)

WORKER_POLL_WAIT_SECONDS = int(os.environ.get("WORKER_POLL_WAIT_SECONDS", "10"))
WORKER_MAX_MESSAGES_PER_POLL = int(os.environ.get("WORKER_MAX_MESSAGES_PER_POLL", "10"))
