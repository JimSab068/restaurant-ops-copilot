"""LLM-backed reasoning for Component 3.

LLMReasoner.reason() is the only place Component 3 calls out to an LLM.
It has no execution capability and no database access, and it cannot
return anything but validated CandidateAction objects (or an empty
list). Every failure mode — client/network error, malformed JSON,
hallucinated entity, disallowed action type, too many candidates —
degrades to fewer/zero candidates; it never raises anything but
LLMReasonerError, and orchestrator.py is responsible for catching that
and falling back to the deterministic path in reasoning.py.
"""

from __future__ import annotations

import json
import logging

from .candidate_validator import (
    CandidateValidationError,
    validate_candidate_against_opportunity,
    validate_llm_candidate,
)
from .llm_client import LLMClient, LLMClientError
from .llm_prompts import SYSTEM_PROMPT, build_user_prompt
from .models import CandidateAction, Opportunity
from .reasoning_context import build_reasoning_context

logger = logging.getLogger(__name__)


class LLMReasonerError(RuntimeError):
    """
    Raised when LLM reasoning cannot safely complete.

    Callers (orchestrator.py) catch this and fall back to deterministic
    reasoning. It is never meant to propagate out of Component 3's
    public orchestration functions.
    """


# Hard ceiling on candidates accepted from a single LLM response,
# independent of what the prompt asked for. The system prompt asking
# for at most 5 is a hint to the model, not the enforcement mechanism —
# this constant is the enforcement mechanism, applied after validation
# so a malformed candidate near the front of the response can't push a
# valid one at the back out of the budget.
MAX_LLM_CANDIDATES = 5


class LLMReasoner:
    """Converts a bounded Opportunity into validated CandidateAction objects."""

    def __init__(
        self,
        client: LLMClient,
        *,
        constraints: dict[str, object] | None = None,
    ) -> None:
        self.client = client
        self.constraints = constraints or {}

    def reason(
        self,
        opportunity: Opportunity,
        *,
        allowed_action_types: tuple[str, ...],
    ) -> list[CandidateAction]:
        """
        Produce validated LLM candidates for one opportunity.

        allowed_action_types is supplied per call, not fixed at
        construction, because it varies by opportunity type — see
        action_policy.py. An empty allowed_action_types short-circuits
        to [] without calling the LLM at all, since there is nothing it
        could validly propose.
        """
        if not allowed_action_types:
            return []

        context = build_reasoning_context(
            opportunity,
            allowed_action_types=allowed_action_types,
            constraints=self.constraints,
        )

        try:
            response = self.client.generate(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_user_prompt(context),
            )
        except LLMClientError as exc:
            raise LLMReasonerError("LLM reasoning unavailable") from exc
        except Exception as exc:
            # Provider adapters are third-party boundaries.  Preserve the
            # deterministic fallback even if an adapter violates its protocol
            # and leaks a provider-specific exception.
            raise LLMReasonerError("LLM reasoning unavailable") from exc

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise LLMReasonerError("LLM returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise LLMReasonerError("LLM response must be a JSON object")

        raw_candidates = data.get("candidates", [])

        if not isinstance(raw_candidates, list):
            raise LLMReasonerError("candidates must be a JSON array")

        candidates: list[CandidateAction] = []

        for raw in raw_candidates:
            if len(candidates) >= MAX_LLM_CANDIDATES:
                logger.warning(
                    "LLM returned more than %d candidates for "
                    "opportunity=%s; ignoring the remainder.",
                    MAX_LLM_CANDIDATES,
                    opportunity.id,
                )
                break

            try:
                candidate = validate_llm_candidate(raw, opportunity)

                if candidate.action_type.value not in allowed_action_types:
                    raise CandidateValidationError(
                        f"action type {candidate.action_type.value!r} "
                        "is not allowed for this opportunity"
                    )

                if not validate_candidate_against_opportunity(
                    candidate,
                    opportunity,
                ):
                    raise CandidateValidationError(
                        "candidate payload does not match verified "
                        "opportunity evidence"
                    )

            except CandidateValidationError as exc:
                logger.warning(
                    "Rejected LLM candidate for opportunity=%s: %s",
                    opportunity.id,
                    exc,
                )
                continue

            candidates.append(candidate)

        return candidates
