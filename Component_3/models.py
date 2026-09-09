"""Immutable contracts shared by Component 3's detector and reasoner."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from Component_1.models import ActionType


class OpportunityType(str, Enum):
    """Operational conditions Component 3 can identify from live state."""

    MARGIN_DETERIORATION = "margin_deterioration"
    STOCKOUT_EXPOSURE = "stockout_exposure"
    DEMAND_DECLINE = "demand_decline"


@dataclass(frozen=True)
class Opportunity:
    """A read-only, tenant-scoped explanation of an actionable condition."""

    id: str
    tenant_id: str
    opportunity_type: OpportunityType
    title: str
    cause: str
    affected_entity_ids: tuple[str, ...]
    estimated_monthly_impact: float
    confidence: float
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.tenant_id:
            raise ValueError("opportunity id and tenant_id are required")

        if not isinstance(self.opportunity_type, OpportunityType):
            raise TypeError("opportunity_type must be an OpportunityType")

        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("opportunity title is required")

        if not isinstance(self.cause, str) or not self.cause.strip():
            raise ValueError("opportunity cause is required")

        if not self.affected_entity_ids:
            raise ValueError("an opportunity must name an affected entity")

        for field_name in ("estimated_monthly_impact", "confidence"):
            value = getattr(self, field_name)

            # bool is a subclass of int in Python and must never be accepted
            # as an economic/numeric value.
            if isinstance(value, bool):
                raise TypeError(f"{field_name} must be numeric, not bool")

            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"{field_name} must be numeric") from exc

            if not math.isfinite(numeric_value):
                raise ValueError(f"{field_name} must be finite")

        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "opportunity_type": self.opportunity_type.value,
            "title": self.title,
            "cause": self.cause,
            "affected_entity_ids": list(self.affected_entity_ids),
            "estimated_monthly_impact": self.estimated_monthly_impact,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class CandidateAction:
    """A proposed action; it is not authority to execute that action."""

    opportunity_id: str
    action_type: ActionType
    payload: Mapping[str, Any]
    rationale: str
    requires_human_review: bool = False
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.opportunity_id:
            raise ValueError("opportunity_id is required")
        if not isinstance(self.action_type, ActionType):
            raise TypeError("action_type must be an ActionType")
        if not isinstance(self.payload, Mapping) or not self.payload:
            raise ValueError("payload must be a non-empty mapping")
        if not self.rationale.strip():
            raise ValueError("rationale is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "action_type": self.action_type.value,
            "payload": dict(self.payload),
            "rationale": self.rationale,
            "requires_human_review": self.requires_human_review,
            "assumptions": list(self.assumptions),
        }
