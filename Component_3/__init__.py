"""Component 3: tenant-scoped opportunity detection and reasoning."""

from .opportunity_detector import detect_opportunities
from .reasoning import generate_candidate_actions, reason_about_opportunities
from .models import CandidateAction, Opportunity, OpportunityType

__all__ = [
    "CandidateAction",
    "Opportunity",
    "OpportunityType",
    "detect_opportunities",
    "generate_candidate_actions",
    "reason_about_opportunities",
]
