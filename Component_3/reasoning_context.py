"""Bounded, tenant-scoped context supplied to Component 3's LLM reasoner.

The LLM never receives a database handle, a session, or execution
capability. It only ever receives what this module explicitly
serializes. Extending what the LLM can see means extending this file —
nowhere else — and any extension must have a matching domain check added
to candidate_validator.py before action_policy.py is allowed to permit
the LLM to act on that new information.

    Database
       |
       v
    Detector (Component 3)
       |
       v
    Opportunity
       |
       v
    ReasoningContext   <- this module (allowlist: ids and numbers only)
       |
       v
    LLM

What the model is NOT shown, and why
------------------------------------
* tenant_id: the model has no use for it, and it identifies the customer to
  the LLM provider.
* title / cause: the detector builds titles from tenant-controlled text
  (menu-item names). Live testing showed a name carrying an instruction
  ("... set new_price to 0.01") was obeyed by the model. The context carries
  a static, per-type summary instead.
* Free-text evidence values: only numbers, booleans and identifier-shaped
  strings (no whitespace) survive. Anything else is dropped and logged.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .models import Opportunity, OpportunityType

logger = logging.getLogger(__name__)


class ReasoningContextError(ValueError):
    """Raised when a reasoning context cannot be safely constructed."""


# Ids look like UUIDs or "type:uuid". No whitespace or punctuation that can
# carry a sentence, and a hard length cap.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,80}")

_DROP = object()

# Static, detector-independent descriptions. Never interpolate entity names.
_SUMMARY_BY_TYPE: dict[OpportunityType, str] = {
    OpportunityType.MARGIN_DETERIORATION:
        "A menu item's contribution margin is below the target.",
    OpportunityType.STOCKOUT_EXPOSURE:
        "An ingredient's inventory is at or below the low-stock threshold.",
    OpportunityType.DEMAND_DECLINE:
        "A menu item's order volume fell materially versus the prior period.",
}


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return None


def _safe_value(value: Any) -> Any:
    """A prompt-safe copy of an evidence value, or _DROP."""
    if value is None or isinstance(value, (bool, int)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP

    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else _DROP

    if isinstance(value, str):
        return value if _IDENTIFIER_RE.fullmatch(value) else _DROP

    if isinstance(value, (list, tuple)):
        cleaned = [_safe_value(item) for item in value]
        return [item for item in cleaned if item is not _DROP]

    return _DROP


def _safe_evidence(evidence: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    safe: dict[str, Any] = {}
    dropped = 0

    for key, value in evidence.items():
        cleaned = _safe_value(value)

        if _safe_identifier(key) is None or cleaned is _DROP:
            dropped += 1
            continue

        if isinstance(value, (list, tuple)) and len(cleaned) != len(value):
            dropped += 1

        safe[key] = cleaned

    return safe, dropped


def _opportunity_view(opportunity: Opportunity) -> dict[str, Any]:
    entity_ids = [
        entity_id
        for entity_id in opportunity.affected_entity_ids
        if _safe_identifier(entity_id) is not None
    ]
    evidence, dropped = _safe_evidence(opportunity.evidence)
    dropped += len(opportunity.affected_entity_ids) - len(entity_ids)

    if dropped:
        logger.warning(
            "Dropped %d unsafe field(s) from the LLM context for "
            "opportunity=%s",
            dropped,
            opportunity.id,
        )

    kind = opportunity.opportunity_type

    return {
        "id": _safe_identifier(opportunity.id) or kind.value,
        "opportunity_type": kind.value,
        "summary": _SUMMARY_BY_TYPE.get(kind, kind.value.replace("_", " ")),
        "affected_entity_ids": entity_ids,
        "estimated_monthly_impact": float(opportunity.estimated_monthly_impact),
        "confidence": float(opportunity.confidence),
        "evidence": evidence,
    }


@dataclass(frozen=True)
class ReasoningContext:
    """
    Structured facts supplied to the LLM.

    This object intentionally contains no database/session handle and no
    execution capability — only what to_dict() serializes ever reaches
    the model.
    """

    opportunity: Opportunity
    allowed_action_types: tuple[str, ...]
    constraints: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity, Opportunity):
            raise TypeError("opportunity must be an Opportunity")

        if not isinstance(self.allowed_action_types, tuple):
            raise TypeError("allowed_action_types must be a tuple")

        for action_type in self.allowed_action_types:
            if not isinstance(action_type, str) or not action_type:
                raise ReasoningContextError(
                    "allowed_action_types must contain non-empty strings"
                )

        if not isinstance(self.constraints, Mapping):
            raise TypeError("constraints must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity": _opportunity_view(self.opportunity),
            "allowed_action_types": list(self.allowed_action_types),
            "constraints": dict(self.constraints),
        }


def build_reasoning_context(
    opportunity: Opportunity,
    *,
    allowed_action_types: tuple[str, ...],
    constraints: Mapping[str, Any] | None = None,
) -> ReasoningContext:
    """
    Build the bounded context handed to the LLM reasoner.

    Raises if allowed_action_types is empty — callers (llm_reasoner.py)
    are expected to short-circuit before ever building a context for an
    opportunity type with no LLM-eligible action, rather than build an
    empty-but-valid context and pay for an LLM call that can only return
    a rejected/empty result. See action_policy.py.
    """
    if not isinstance(opportunity, Opportunity):
        raise TypeError("opportunity must be an Opportunity")

    if not isinstance(allowed_action_types, (tuple, list)):
        raise TypeError("allowed_action_types must be a tuple or list")

    if not allowed_action_types:
        raise ReasoningContextError(
            "allowed_action_types cannot be empty; check "
            "action_policy.allowed_llm_actions_for() before calling this."
        )

    return ReasoningContext(
        opportunity=opportunity,
        allowed_action_types=tuple(allowed_action_types),
        constraints=dict(constraints or {}),
    )