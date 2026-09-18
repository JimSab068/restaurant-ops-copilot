"""Deterministic failure scenarios for safe-degradation evaluations."""
from __future__ import annotations

from collections.abc import Callable


class InjectedFailure(RuntimeError):
    pass


def timeout() -> None:
    raise InjectedFailure("simulated integration timeout")


def invalid_data() -> None:
    raise InjectedFailure("simulated invalid operational data")


def duplicate_execution(callback: Callable[[], object]) -> tuple[object, object]:
    """Invoke twice so an idempotency adapter can prove duplicate safety."""
    return callback(), callback()
