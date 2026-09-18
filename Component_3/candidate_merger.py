"""Merge deterministic and LLM candidate actions for Component 3.

Deterministic candidates are always included; LLM candidates are added
on top, deduplicated by action signature. Neither path replaces the
other — Component 2's simulator decides which candidate looks best in
practice, this module only avoids handing it exact duplicates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import CandidateAction


def _freeze(value: Any) -> Any:
    """Return a hashable, deterministic representation of JSON-like data."""
    if isinstance(value, Mapping):
        entries = [(_freeze(key), _freeze(item)) for key, item in value.items()]
        return tuple(sorted(entries, key=repr))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    try:
        hash(value)
    except TypeError:
        # Payloads should be JSON-like, but deduplication must never turn an
        # untrusted LLM payload into a pipeline crash.
        return (type(value).__qualname__, repr(value))
    return value


def _candidate_key(candidate: CandidateAction) -> tuple:
    return (
        candidate.opportunity_id,
        candidate.action_type.value,
        _freeze(candidate.payload),
    )


def merge_candidates(
    deterministic: list[CandidateAction],
    llm: list[CandidateAction],
) -> list[CandidateAction]:
    """
    Merge candidates, deterministic first, deduplicating by action
    signature (opportunity_id, action_type, sorted payload items).

    When both paths propose the identical action, the deterministic
    version is kept — it carries source=DETERMINISTIC and reproduces
    without any LLM availability, so it is the more useful of the two
    identical entries to retain in the audit trail.
    """
    result: list[CandidateAction] = []
    seen: set[tuple] = set()

    for candidate in (*deterministic, *llm):
        key = _candidate_key(candidate)

        if key in seen:
            continue

        seen.add(key)
        result.append(candidate)

    return result
