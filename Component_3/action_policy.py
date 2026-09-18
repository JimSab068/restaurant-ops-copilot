"""
Action-type allowlist per opportunity type for Component 3's LLM
reasoner.

Deliberately conservative. An opportunity type is only added here once:

    1. reasoning_context.py supplies the LLM enough real entity data
       (menu items, suppliers, ingredients) to reason about that action
       type at all, and
    2. candidate_validator.py has an explicit, verified domain check
       registered for that action type in _SUPPORTED_DOMAIN_CHECKS.

Current state: only PRICE_CHANGE is allowed, and only for opportunity
types where the detector already supplies current_price / menu_item_id
in its evidence (see Component_3/opportunity_detector.py). SUPPLIER_
SWITCH, MENU_SWAP, and STAFFING_CHANGE are deliberately withheld from
the LLM for now — the reasoning context does not carry supplier/SKU/
staffing data yet, so an LLM-proposed action of that shape could not be
verified against real entities and would have to be trusted on faith.
That is exactly the failure mode this architecture exists to prevent.

Expanding this file without also expanding reasoning_context.py and
candidate_validator.py first is a fail-open configuration bug:
candidate_validator.py's _SUPPORTED_DOMAIN_CHECKS rejects any action
type it has no registered check for, so an unmatched addition here
would silently produce zero LLM candidates rather than unverified ones
— but it is still the wrong order to do the work in.
"""

from __future__ import annotations

from Component_1.models import ActionType

from .models import OpportunityType


ALLOWED_LLM_ACTIONS_BY_OPPORTUNITY: dict[OpportunityType, tuple[str, ...]] = {
    OpportunityType.MARGIN_DETERIORATION: (
        ActionType.PRICE_CHANGE.value,
    ),
    OpportunityType.DEMAND_DECLINE: (
        ActionType.PRICE_CHANGE.value,
    ),
    # No LLM-eligible action yet: reasoning_context.py does not supply
    # supplier/SKU data, so an LLM-proposed SUPPLIER_SWITCH or MENU_SWAP
    # here could not be checked against real entities. The deterministic
    # reasoner's existing behavior (no candidate; surface for human
    # review) remains the only path for stockout opportunities.
    OpportunityType.STOCKOUT_EXPOSURE: (),
}


def allowed_llm_actions_for(
    opportunity_type: OpportunityType,
) -> tuple[str, ...]:
    """
    Return the LLM-eligible action types for an opportunity type.

    Unknown/invalid opportunity types fail closed to an empty tuple
    rather than raising, so a future OpportunityType member added
    without an explicit policy here simply gets no LLM reasoning
    (deterministic fallback still applies) instead of crashing the
    reasoning pipeline.
    """
    if not isinstance(opportunity_type, OpportunityType):
        return ()

    return ALLOWED_LLM_ACTIONS_BY_OPPORTUNITY.get(opportunity_type, ())