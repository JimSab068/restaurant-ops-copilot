"""
Production-grade unit tests for Component 3 contracts.

Covers:
- Opportunity validation
- CandidateAction validation
- serialization
- immutability
- numeric edge cases
- malicious input
- type confusion
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
import math

import pytest

from Component_1.models import ActionType

from Component_3.models import (
    CandidateAction,
    Opportunity,
    OpportunityType,
)


TENANT_ID = "11111111-1111-1111-1111-111111111111"
OPPORTUNITY_ID = "margin_deterioration:menu-1"


def make_opportunity(**overrides) -> Opportunity:
    values = {
        "id": OPPORTUNITY_ID,
        "tenant_id": TENANT_ID,
        "opportunity_type": OpportunityType.MARGIN_DETERIORATION,
        "title": "Pizza margin is below target",
        "cause": "Ingredient costs increased.",
        "affected_entity_ids": ("menu-1",),
        "estimated_monthly_impact": -100.0,
        "confidence": 0.85,
        "evidence": {
            "menu_item_id": "menu-1",
            "current_price": 12.0,
            "recipe_cost": 9.0,
        },
    }
    values.update(overrides)
    return Opportunity(**values)


def make_candidate(**overrides) -> CandidateAction:
    values = {
        "opportunity_id": OPPORTUNITY_ID,
        "action_type": ActionType.PRICE_CHANGE,
        "payload": {
            "menu_item_id": "menu-1",
            "new_price": 13.0,
        },
        "rationale": "Restore contribution margin.",
    }
    values.update(overrides)
    return CandidateAction(**values)


# ---------------------------------------------------------------------------
# Opportunity: normal construction
# ---------------------------------------------------------------------------

def test_opportunity_constructs_valid_contract():
    opportunity = make_opportunity()

    assert opportunity.id == OPPORTUNITY_ID
    assert opportunity.tenant_id == TENANT_ID
    assert opportunity.opportunity_type is OpportunityType.MARGIN_DETERIORATION
    assert opportunity.confidence == 0.85


def test_opportunity_to_dict_serializes_enum_as_value():
    result = make_opportunity().to_dict()

    assert result["opportunity_type"] == "margin_deterioration"


def test_opportunity_to_dict_serializes_tuple_as_list():
    result = make_opportunity().to_dict()

    assert result["affected_entity_ids"] == ["menu-1"]


def test_opportunity_to_dict_copies_evidence_mapping():
    result = make_opportunity().to_dict()

    result["evidence"]["attacker"] = "modified"

    assert "attacker" not in make_opportunity().evidence


def test_opportunity_allows_zero_monthly_impact():
    opportunity = make_opportunity(estimated_monthly_impact=0.0)

    assert opportunity.estimated_monthly_impact == 0.0


def test_opportunity_allows_negative_monthly_impact():
    opportunity = make_opportunity(estimated_monthly_impact=-999999.99)

    assert opportunity.estimated_monthly_impact == -999999.99


def test_opportunity_allows_confidence_zero():
    opportunity = make_opportunity(confidence=0.0)

    assert opportunity.confidence == 0.0


def test_opportunity_allows_confidence_one():
    opportunity = make_opportunity(confidence=1.0)

    assert opportunity.confidence == 1.0


# ---------------------------------------------------------------------------
# Opportunity: required fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,value",
    [
        ("id", ""),
        ("tenant_id", ""),
        ("title", ""),
        ("title", "   "),
        ("cause", ""),
        ("cause", "   "),
    ],
)
def test_opportunity_rejects_missing_or_blank_required_fields(field, value):
    with pytest.raises(ValueError):
        make_opportunity(**{field: value})


def test_opportunity_rejects_empty_affected_entities():
    with pytest.raises(ValueError, match="affected entity"):
        make_opportunity(affected_entity_ids=())


def test_opportunity_rejects_wrong_opportunity_type():
    with pytest.raises(TypeError, match="OpportunityType"):
        make_opportunity(opportunity_type="margin_deterioration")


# ---------------------------------------------------------------------------
# Opportunity: numeric safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_opportunity_rejects_non_finite_monthly_impact(value):
    with pytest.raises(ValueError, match="estimated_monthly_impact"):
        make_opportunity(estimated_monthly_impact=value)


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
    ],
)
def test_opportunity_rejects_non_finite_confidence(value):
    with pytest.raises(ValueError, match="confidence"):
        make_opportunity(confidence=value)


@pytest.mark.parametrize(
    "value",
    [-0.0001, 1.0001, -1.0, 2.0, 100.0],
)
def test_opportunity_rejects_confidence_outside_range(value):
    with pytest.raises(ValueError, match="confidence"):
        make_opportunity(confidence=value)


def test_opportunity_accepts_decimal_numeric_values():
    opportunity = make_opportunity(
        estimated_monthly_impact=Decimal("-10.25"),
        confidence=Decimal("0.75"),
    )

    assert opportunity.confidence == Decimal("0.75")


@pytest.mark.parametrize("value", [True, False])
def test_opportunity_rejects_boolean_confidence(value):
    """
    Security contract:

    bool is a subclass of int in Python. A boolean must not silently become
    a valid confidence value.
    """
    with pytest.raises((TypeError, ValueError)):
        make_opportunity(confidence=value)


@pytest.mark.parametrize("value", [True, False])
def test_opportunity_rejects_boolean_monthly_impact(value):
    with pytest.raises((TypeError, ValueError)):
        make_opportunity(estimated_monthly_impact=value)


# ---------------------------------------------------------------------------
# Opportunity: immutability
# ---------------------------------------------------------------------------

def test_opportunity_is_frozen():
    opportunity = make_opportunity()

    with pytest.raises(FrozenInstanceError):
        opportunity.confidence = 0.99


def test_opportunity_cannot_replace_tenant_id():
    opportunity = make_opportunity()

    with pytest.raises(FrozenInstanceError):
        opportunity.tenant_id = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# CandidateAction: normal construction
# ---------------------------------------------------------------------------

def test_candidate_action_constructs_valid_contract():
    candidate = make_candidate()

    assert candidate.action_type is ActionType.PRICE_CHANGE
    assert candidate.payload["new_price"] == 13.0


def test_candidate_action_to_dict_serializes_action_type():
    result = make_candidate().to_dict()

    assert result["action_type"] == "price_change"


def test_candidate_action_to_dict_serializes_assumptions():
    candidate = make_candidate(
        assumptions=("Simulation must validate demand response.",)
    )

    result = candidate.to_dict()

    assert result["assumptions"] == [
        "Simulation must validate demand response."
    ]


# ---------------------------------------------------------------------------
# CandidateAction: validation
# ---------------------------------------------------------------------------

def test_candidate_action_rejects_missing_opportunity_id():
    with pytest.raises(ValueError):
        make_candidate(opportunity_id="")


def test_candidate_action_rejects_wrong_action_type():
    with pytest.raises(TypeError, match="ActionType"):
        make_candidate(action_type="price_change")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        [],
        (),
        "malicious payload",
        123,
    ],
)
def test_candidate_action_rejects_invalid_payload(payload):
    with pytest.raises((ValueError, TypeError)):
        make_candidate(payload=payload)


@pytest.mark.parametrize(
    "rationale",
    [
        "",
        " ",
        "\t",
        "\n",
    ],
)
def test_candidate_action_rejects_blank_rationale(rationale):
    with pytest.raises(ValueError, match="rationale"):
        make_candidate(rationale=rationale)


# ---------------------------------------------------------------------------
# CandidateAction: malicious / adversarial values
# ---------------------------------------------------------------------------

def test_candidate_action_accepts_mapping_subclass():
    class MaliciousMapping(dict):
        pass

    candidate = make_candidate(
        payload=MaliciousMapping(
            menu_item_id="menu-1",
            new_price=13.0,
        )
    )

    assert candidate.payload["menu_item_id"] == "menu-1"


def test_candidate_action_is_frozen():
    candidate = make_candidate()

    with pytest.raises(FrozenInstanceError):
        candidate.rationale = "Execute immediately."


def test_candidate_action_serialization_does_not_mutate_original_payload():
    candidate = make_candidate()

    serialized = candidate.to_dict()
    serialized["payload"]["new_price"] = 999999999

    assert candidate.payload["new_price"] == 13.0


def test_candidate_action_preserves_untrusted_payload_as_data():
    malicious_id = "' OR 1=1 --"

    candidate = make_candidate(
        payload={
            "menu_item_id": malicious_id,
            "new_price": 13.0,
        }
    )

    assert candidate.payload["menu_item_id"] == malicious_id
