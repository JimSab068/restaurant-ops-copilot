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
    ReasoningContext   <- this module
       |
       v
    LLM
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import Opportunity


class ReasoningContextError(ValueError):
    """Raised when a reasoning context cannot be safely constructed."""


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
            "opportunity": self.opportunity.to_dict(),
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