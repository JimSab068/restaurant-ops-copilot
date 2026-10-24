"""Component 3: tenant-scoped opportunity detection and reasoning.

CHANGED: this package now exports orchestrator.py's reason_about_opportunity
/ reason_about_opportunities (deterministic + optional LLM merge, with
automatic fallback on any LLM failure) as the public API, instead of
reasoning.py's deterministic-only versions.

Why this matters: reasoning.py's reason_about_opportunities(opportunities)
and orchestrator.py's reason_about_opportunities(opportunities,
llm_reasoner=None, use_llm=True) are behaviorally identical when called
with no llm_reasoner -- orchestrator short-circuits to the deterministic
path whenever llm_reasoner is None (see orchestrator.reason_about_opportunity's
`if not use_llm or llm_reasoner is None: return deterministic`). So this
swap is safe for every existing caller that doesn't pass an llm_reasoner,
and it's what makes LLM-assisted reasoning actually reachable through
`from Component_3 import reason_about_opportunities` for callers that do.

reasoning.generate_candidate_actions is still exported directly -- that's
the non-negotiable deterministic fallback itself, used standalone by
orchestrator.py internally and worth keeping importable on its own.
"""

from .opportunity_detector import detect_opportunities
from .orchestrator import reason_about_opportunity, reason_about_opportunities
from .reasoning import generate_candidate_actions
from .models import CandidateAction, Opportunity, OpportunityType

__all__ = [
    "CandidateAction",
    "Opportunity",
    "OpportunityType",
    "detect_opportunities",
    "generate_candidate_actions",
    "reason_about_opportunity",
    "reason_about_opportunities",
]