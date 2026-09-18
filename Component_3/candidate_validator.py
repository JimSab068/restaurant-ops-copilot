"""Validation boundary for LLM-generated CandidateAction objects.

Never trust LLM output merely because it parsed as JSON. Two layers are
enforced here, both mandatory:

1. Structural validation (validate_llm_candidate): is this shaped like a
   CandidateAction at all — right types, non-empty required fields, a
   real ActionType?

2. Domain / anti-hallucination validation
   (validate_candidate_against_opportunity): does this candidate
   actually reference entities and values the opportunity itself
   supplied, rather than IDs or figures the LLM invented?

Passing layer 1 only proves the LLM produced well-formed JSON — it says
nothing about whether the JSON describes a real, safe action. Layer 2 is
what prevents entity hallucination (e.g. the LLM inventing a
menu_item_id that belongs to a different opportunity, or a different
tenant entirely).

Coverage note: layer 2 currently has an explicit, verified check only
for PRICE_CHANGE, because that is the only action type
action_policy.py currently allows the LLM to propose. Any candidate
whose action_type has no registered check in _SUPPORTED_DOMAIN_CHECKS
is REJECTED outright, not passed through unverified — see
validate_candidate_against_opportunity's docstring. Extending
action_policy.py to allow a new action type for the LLM requires adding
its domain check here FIRST, or every candidate of that type will be
silently rejected (safe, but pointless) until you do.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from Component_1.models import ActionType

from .models import CandidateAction, CandidateSource, Opportunity


class CandidateValidationError(ValueError):
    """Raised when an LLM candidate violates the Component 3 contract."""


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def _parse_action_type(value: Any) -> ActionType:
    if not isinstance(value, str):
        raise CandidateValidationError("action_type must be a string")

    try:
        return ActionType(value)
    except ValueError as exc:
        raise CandidateValidationError(
            f"unsupported action_type: {value!r}"
        ) from exc


def _validate_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CandidateValidationError("payload must be an object")

    if not payload:
        raise CandidateValidationError("payload cannot be empty")

    return dict(payload)


def _validate_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateValidationError(f"{field_name} must be non-empty")

    return value.strip()


def validate_llm_candidate(
    raw: Mapping[str, Any],
    opportunity: Opportunity,
) -> CandidateAction:
    """
    Convert one raw LLM-proposed candidate dict into a validated
    CandidateAction, or raise CandidateValidationError.

    This function only checks shape/types. Callers MUST also call
    validate_candidate_against_opportunity() before trusting the result
    — see module docstring.
    """
    if not isinstance(raw, Mapping):
        raise CandidateValidationError("candidate must be an object")

    action_type = _parse_action_type(raw.get("action_type"))
    payload = _validate_payload(raw.get("payload"))
    rationale = _validate_text(raw.get("rationale"), "rationale")

    requires_review = raw.get("requires_human_review", True)
    if not isinstance(requires_review, bool):
        raise CandidateValidationError(
            "requires_human_review must be boolean"
        )

    raw_assumptions = raw.get("assumptions", [])
    if not isinstance(raw_assumptions, list):
        raise CandidateValidationError("assumptions must be a list")

    assumptions: list[str] = []
    for assumption in raw_assumptions:
        if not isinstance(assumption, str):
            raise CandidateValidationError(
                "every assumption must be a string"
            )
        cleaned = assumption.strip()
        if cleaned:
            assumptions.append(cleaned)

    # LLM candidates can never self-authorize autonomous execution.
    # Component 6's risk classifier makes the real autonomy decision;
    # whatever the LLM set for requires_human_review above is
    # informational only and is intentionally discarded here in favor
    # of a hard True.
    return CandidateAction(
        opportunity_id=opportunity.id,
        action_type=action_type,
        payload=payload,
        rationale=rationale,
        requires_human_review=True,
        assumptions=tuple(assumptions),
        source=CandidateSource.LLM,
    )


# ---------------------------------------------------------------------------
# Domain / anti-hallucination validation
# ---------------------------------------------------------------------------

def _validate_price_change_against_opportunity(
    candidate: CandidateAction,
    opportunity: Opportunity,
) -> bool:
    payload = candidate.payload

    menu_item_id = payload.get("menu_item_id")
    new_price = payload.get("new_price")

    if not isinstance(menu_item_id, str) or not menu_item_id:
        return False

    # The anti-hallucination check: the LLM must reference an entity ID
    # the opportunity itself actually named, never one it invented.
    if menu_item_id not in opportunity.affected_entity_ids:
        return False

    if isinstance(new_price, bool):
        return False

    try:
        price = float(new_price)
    except (TypeError, ValueError):
        return False

    if not math.isfinite(price) or price <= 0:
        return False

    return True


# Every action type action_policy.py allows the LLM to propose MUST have
# an entry here. An action type present in
# ALLOWED_LLM_ACTIONS_BY_OPPORTUNITY without a corresponding entry here
# is a configuration bug — validate_candidate_against_opportunity()
# fails closed on the mismatch (rejects the candidate) rather than
# silently treating an unverified action type as safe.
_SUPPORTED_DOMAIN_CHECKS: dict[
    ActionType,
    Callable[[CandidateAction, Opportunity], bool],
] = {
    ActionType.PRICE_CHANGE: _validate_price_change_against_opportunity,
}


def validate_candidate_against_opportunity(
    candidate: CandidateAction,
    opportunity: Opportunity,
) -> bool:
    """
    Verify a structurally-valid candidate against the opportunity that
    produced it: right opportunity, real entity IDs, plausible numeric
    values.

    Fails closed: an action_type with no registered domain check above
    is rejected (returns False), never passed through unverified. This
    is what keeps action_policy.py and this validator from silently
    drifting apart — if SUPPLIER_SWITCH were added to the allowlist
    without a matching check added here, every SUPPLIER_SWITCH
    candidate would be rejected until the check exists, rather than
    being waved through on the strength of structural validity alone.
    """
    if candidate.opportunity_id != opportunity.id:
        return False

    check = _SUPPORTED_DOMAIN_CHECKS.get(candidate.action_type)

    if check is None:
        return False

    return check(candidate, opportunity)