"""
Structured JSON logging shared by api/ and worker/.

Every log line that touches a request carries request_id/tenant_id/
decision_id/action_type for CloudWatch Logs Insights correlation. Never
log API keys, raw secrets, or full payloads -- payload contents can
contain business data that shouldn't sit in log aggregation indefinitely;
log identifiers and outcomes, not the data itself.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in (
            "request_id", "tenant_id", "decision_id", "action_type",
            "queue_message_id", "policy_outcome", "latency_ms",
            "work_request_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def log_context(**fields: Any) -> dict[str, Any]:
    """Pass as `extra=log_context(...)` to attach correlation fields."""
    return fields