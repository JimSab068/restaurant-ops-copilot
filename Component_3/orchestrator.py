"""Component 3 opportunity-to-candidate orchestration.

This is the intended entry point for the rest of the system (Component
2's simulator, and eventually a top-level pipeline orchestrating the
full detect -> reason -> simulate -> policy -> execute -> calibrate
loop). Everything upstream of here — detector, deterministic reasoner,
LLM reasoner, validator, merger — is wired together in exactly one
place so that boundary has a single, well-tested seam to call across.
"""

from __future__ import annotations

import logging

from .action_policy import allowed_llm_actions_for
from .candidate_merger import merge_candidates
from .llm_reasoner import LLMReasoner, LLMReasonerError
from .models import CandidateAction, Opportunity
from .reasoning import generate_candidate_actions

logger = logging.getLogger(__name__)


def reason_about_opportunity(
    opportunity: Opportunity,
    *,
    llm_reasoner: LLMReasoner | None = None,
    use_llm: bool = True,
) -> list[CandidateAction]:
    """
    Produce candidates from deterministic reasoning, optionally merged
    with LLM reasoning.

    Deterministic reasoning always runs first and is always part of the
    result — this is the non-negotiable fallback described in the
    functional doc ("the LLM should never be a single point of
    failure"). LLM reasoning is attempted only if use_llm is True, an
    llm_reasoner is supplied, and the opportunity type has at least one
    LLM-eligible action type (action_policy.allowed_llm_actions_for).
    Any LLM failure — client error, malformed JSON, every candidate
    rejected by validation — degrades to the deterministic candidates
    alone; it never raises out of this function.
    """
    deterministic = generate_candidate_actions(opportunity)

    if not use_llm or llm_reasoner is None:
        return deterministic

    allowed_action_types = allowed_llm_actions_for(
        opportunity.opportunity_type
    )

    if not allowed_action_types:
        return deterministic

    try:
        llm_candidates = llm_reasoner.reason(
            opportunity,
            allowed_action_types=allowed_action_types,
        )
    except LLMReasonerError:
        logger.exception(
            "LLM reasoning failed; using deterministic fallback for "
            "opportunity=%s",
            opportunity.id,
        )
        return deterministic

    return merge_candidates(deterministic, llm_candidates)


def reason_about_opportunities(
    opportunities: list[Opportunity],
    *,
    llm_reasoner: LLMReasoner | None = None,
    use_llm: bool = True,
) -> dict[str, list[CandidateAction]]:
    """Batch form of reason_about_opportunity, grouped by opportunity ID."""
    result: dict[str, list[CandidateAction]] = {}
    batch_tenant_id: str | None = None

    for opportunity in opportunities:
        if not isinstance(opportunity, Opportunity):
            raise TypeError(
                "all opportunities must be Opportunity instances"
            )

        # Opportunity IDs are stable per tenant, not globally namespaced.
        # Rejecting a mixed-tenant batch prevents a same-ID opportunity from
        # silently overwriting another tenant's candidates in ``result``.
        if batch_tenant_id is None:
            batch_tenant_id = opportunity.tenant_id
        elif opportunity.tenant_id != batch_tenant_id:
            raise ValueError("all opportunities in a batch must share a tenant_id")

        result[opportunity.id] = reason_about_opportunity(
            opportunity,
            llm_reasoner=llm_reasoner,
            use_llm=use_llm,
        )

    return result
