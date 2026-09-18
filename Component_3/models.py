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


class CandidateSource(str, Enum):
    """
    Where a CandidateAction originated.

    Recorded on every candidate so Component 7 can later compare
    deterministic vs. LLM-assisted reasoning (validity rate, simulated
    impact, human-override rate) without having to guess provenance
    after the fact. This is metadata only — it never grants a candidate
    any additional authority; Component 6 classifies risk identically
    regardless of source.
    """

    DETERMINISTIC = "deterministic"
    LLM = "llm"


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
        if (
            not isinstance(self.id, str)
            or not self.id.strip()
            or not isinstance(self.tenant_id, str)
            or not self.tenant_id.strip()
        ):
            raise ValueError("opportunity id and tenant_id are required")

        if not isinstance(self.opportunity_type, OpportunityType):
            raise TypeError("opportunity_type must be an OpportunityType")

        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("opportunity title is required")

        if not isinstance(self.cause, str) or not self.cause.strip():
            raise ValueError("opportunity cause is required")

        if (
            not isinstance(self.affected_entity_ids, tuple)
            or not self.affected_entity_ids
        ):
            raise ValueError("an opportunity must name an affected entity")

        if any(
            not isinstance(entity_id, str) or not entity_id.strip()
            for entity_id in self.affected_entity_ids
        ):
            raise ValueError("affected_entity_ids must contain non-empty strings")

        if not isinstance(self.evidence, Mapping):
            raise TypeError("evidence must be a mapping")

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
    """
    A proposed action.

    A CandidateAction is NEVER execution authority, regardless of source.
    Every candidate — deterministic or LLM — must still pass through
    Component 2 simulation and Component 6 policy/risk classification
    before anything executes.
    """

    opportunity_id: str
    action_type: ActionType
    payload: Mapping[str, Any]
    rationale: str
    requires_human_review: bool = False
    assumptions: tuple[str, ...] = ()
    source: CandidateSource = CandidateSource.DETERMINISTIC

    def __post_init__(self) -> None:
        if not self.opportunity_id:
            raise ValueError("opportunity_id is required")
        if not isinstance(self.action_type, ActionType):
            raise TypeError("action_type must be an ActionType")
        if not isinstance(self.payload, Mapping) or not self.payload:
            raise ValueError("payload must be a non-empty mapping")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("rationale is required")
        if not isinstance(self.requires_human_review, bool):
            raise TypeError("requires_human_review must be bool")
        if not isinstance(self.source, CandidateSource):
            raise TypeError("source must be a CandidateSource")
        if not isinstance(self.assumptions, tuple) or any(
            not isinstance(assumption, str) for assumption in self.assumptions
        ):
            raise TypeError("assumptions must be a tuple of strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "action_type": self.action_type.value,
            "payload": dict(self.payload),
            "rationale": self.rationale,
            "requires_human_review": self.requires_human_review,
            "assumptions": list(self.assumptions),
            "source": self.source.value,
        }
