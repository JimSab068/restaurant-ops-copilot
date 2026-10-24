"""
Worker entrypoint: the long-running poll loop for the ECS worker service.

Deletion contract lives in handler.process_message()'s docstring: True
means delete (handled, whether success or permanent failure); False means
leave it for SQS to redeliver / eventually DLQ.
"""

from __future__ import annotations

import logging
import signal
import sys

from shared.logging import configure_logging
from shared.queue import delete_message, poll_messages
from shared.settings import WORKER_MAX_MESSAGES_PER_POLL, WORKER_POLL_WAIT_SECONDS

from .handler import process_message

configure_logging()
logger = logging.getLogger("worker.main")

_shutdown = False


def _handle_signal(signum, frame) -> None:
    global _shutdown
    logger.info("Received signal %s; shutting down after current batch", signum)
    _shutdown = True


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Worker starting")

    while not _shutdown:
        try:
            messages = poll_messages(
                max_messages=WORKER_MAX_MESSAGES_PER_POLL,
                wait_seconds=WORKER_POLL_WAIT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to poll queue; backing off")
            continue

        for message in messages:
            try:
                handled = process_message(message)
            except Exception:
                # process_message() already catches everything it can
                # meaningfully classify; anything escaping here is an
                # unhandled worker crash worth its own alarm.
                logger.exception(
                    "Unhandled exception processing message",
                    extra={
                        "tenant_id": message.tenant_id,
                        "work_request_id": message.work_request_id,
                    },
                )
                handled = False

            if handled:
                delete_message(message.receipt_handle)

    logger.info("Worker stopped")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logger.exception("Worker crashed")
        sys.exit(1)