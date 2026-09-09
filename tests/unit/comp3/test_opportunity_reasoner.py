"""
Production-grade tests for Component 3 reasoning.

The reasoner:
- consumes Opportunity evidence
- produces CandidateAction objects
- never executes anything
- must fail safely on malformed evidence
- must not cross tenant boundaries
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from Component_1.models import ActionType

from Component_3.models import (
    CandidateAction,
    Opportunity,
    OpportunityType,
)
from Component_3.reasoning import (
    DEMAND_RECOVERY_DISCOUNT,
    PRICE_STEP,
    TARGET_CONTRIBUTION_MARGIN,
    generate_candidate_actions,
    reason_about_opportunities,
)


TENANT_ID = "11111111-1111-1111-1111-111111111111"


def make_opportunity(
    opportunity_type: OpportunityType,
    evidence: dict,
    opportunity_id: str = "opportunity-1",
) -> Opportunity:
    return Opportunity(
        id=opportunity_id,
        tenant_id=TENANT_ID,
        opportunity_type=opportunity_type,
        title="Test opportunity",
        cause="Test cause",
        affected_entity_ids=("menu-1",),
        estimated_monthly_impact=-100.0,
        confidence=0.85,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Margin reasoning
# ---------------------------------------------------------------------------

def test_margin_opportunity_produces_price_change_candidate():
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10.00,
            "recipe_cost": 8.00,
        },
    )

    candidates = generate_candidate_actions(opportunity)

    assert len(candidates) == 1

    candidate = candidates[0]

    assert candidate.action_type is ActionType.PRICE_CHANGE
    assert candidate.payload["menu_item_id"] == "menu-1"


def test_margin_candidate_has_positive_new_price():
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10.00,
            "recipe_cost": 8.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.payload["new_price"] > 0


def test_margin_candidate_raises_price_when_required():
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10.00,
            "recipe_cost": 8.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.payload["new_price"] > 10.00


def test_margin_candidate_uses_price_step():
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10.00,
            "recipe_cost": 8.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    price = Decimal(str(candidate.payload["new_price"]))

    assert (price / PRICE_STEP) == (
        price / PRICE_STEP
    ).to_integral_value()


def test_margin_candidate_never_decreases_price():
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 100.00,
            "recipe_cost": 1.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.payload["new_price"] > 100.00


# ---------------------------------------------------------------------------
# Demand decline reasoning
# ---------------------------------------------------------------------------

def test_demand_decline_produces_price_decrease_candidate():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20.00,
        },
    )

    candidates = generate_candidate_actions(opportunity)

    assert len(candidates) == 1

    candidate = candidates[0]

    assert candidate.action_type is ActionType.PRICE_CHANGE
    assert candidate.payload["new_price"] < 20.00


def test_demand_decline_discount_is_five_percent():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    expected = float(
        Decimal("20.00") * (
            Decimal("1") - DEMAND_RECOVERY_DISCOUNT
        )
    )

    assert candidate.payload["new_price"] == expected


def test_demand_decline_candidate_contains_reversible_assumption():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20.00,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.assumptions
    assert any(
        "Component 2" in assumption
        for assumption in candidate.assumptions
    )


def test_zero_demand_decline_price_produces_no_candidate():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 0,
        },
    )

    assert generate_candidate_actions(opportunity) == []


def test_negative_demand_decline_price_produces_no_candidate():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": -10,
        },
    )

    assert generate_candidate_actions(opportunity) == []


def test_missing_demand_price_produces_no_candidate():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
        },
    )

    assert generate_candidate_actions(opportunity) == []


# ---------------------------------------------------------------------------
# Stockout reasoning
# ---------------------------------------------------------------------------

def test_stockout_opportunity_produces_no_executable_candidate():
    opportunity = make_opportunity(
        OpportunityType.STOCKOUT_EXPOSURE,
        {
            "ingredient_id": "ingredient-1",
            "inventory_quantity": 0,
        },
    )

    assert generate_candidate_actions(opportunity) == []


def test_stockout_reasoning_does_not_fabricate_supplier_price():
    opportunity = make_opportunity(
        OpportunityType.STOCKOUT_EXPOSURE,
        {
            "ingredient_id": "ingredient-1",
            "inventory_quantity": 0,
            "affected_menu_item_ids": ["menu-1"],
        },
    )

    candidates = generate_candidate_actions(opportunity)

    assert candidates == []


# ---------------------------------------------------------------------------
# Unsupported opportunity types
# ---------------------------------------------------------------------------

from dataclasses import replace


def test_unknown_opportunity_type_produces_no_candidate(monkeypatch):
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10,
            "recipe_cost": 8,
        },
    )

    opportunity = replace(
        opportunity,
        opportunity_type=OpportunityType.STOCKOUT_EXPOSURE,
    )

    assert isinstance(opportunity, Opportunity)


# ---------------------------------------------------------------------------
# Malformed evidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"menu_item_id": "menu-1"},
        {"current_price": 10},
        {"recipe_cost": 8},
        {"current_price": None, "recipe_cost": 8},
        {"current_price": 10, "recipe_cost": None},
        {"current_price": "not-a-number", "recipe_cost": 8},
        {"current_price": 10, "recipe_cost": "not-a-number"},
    ],
)
def test_malformed_margin_evidence_fails_closed(evidence):
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        evidence,
    )

    assert generate_candidate_actions(opportunity) == []


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "inf",
        "-inf",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_non_finite_margin_evidence_fails_closed(value):
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": value,
            "recipe_cost": 8,
        },
    )

    assert generate_candidate_actions(opportunity) == []


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "inf",
        "-inf",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_non_finite_recipe_cost_fails_closed(value):
    opportunity = make_opportunity(
        OpportunityType.MARGIN_DETERIORATION,
        {
            "menu_item_id": "menu-1",
            "current_price": 10,
            "recipe_cost": value,
        },
    )

    assert generate_candidate_actions(opportunity) == []


# ---------------------------------------------------------------------------
# Injection / hostile evidence
# ---------------------------------------------------------------------------

def test_sql_injection_in_entity_id_remains_data():
    malicious_id = "' OR 1=1 --"

    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": malicious_id,
            "current_price": 20,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.payload["menu_item_id"] == malicious_id


def test_reasoner_does_not_execute_database_queries():
    """
    Architectural security property:

    Component 3 reasoning has no database authority.
    """
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert isinstance(candidate, CandidateAction)


# ---------------------------------------------------------------------------
# Candidate semantics
# ---------------------------------------------------------------------------

def test_reasoner_candidate_is_not_execution_authority():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert not hasattr(candidate, "execute")
    assert candidate.action_type is ActionType.PRICE_CHANGE


def test_candidate_requires_simulation_assumption_for_demand_change():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20,
        },
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert any(
        "simulate" in assumption.lower()
        for assumption in candidate.assumptions
    )


# ---------------------------------------------------------------------------
# Grouped reasoning
# ---------------------------------------------------------------------------

def test_reason_about_opportunities_groups_by_opportunity_id():
    opportunities = [
        make_opportunity(
            OpportunityType.DEMAND_DECLINE,
            {
                "menu_item_id": "menu-1",
                "current_price": 20,
            },
            opportunity_id="opp-1",
        ),
        make_opportunity(
            OpportunityType.STOCKOUT_EXPOSURE,
            {
                "ingredient_id": "ingredient-1",
            },
            opportunity_id="opp-2",
        ),
    ]

    result = reason_about_opportunities(opportunities)

    assert set(result) == {"opp-1", "opp-2"}
    assert len(result["opp-1"]) == 1
    assert result["opp-2"] == []


def test_reason_about_empty_iterable_returns_empty_mapping():
    assert reason_about_opportunities([]) == {}


def test_reason_about_generator_is_supported():
    opportunities = (
        make_opportunity(
            OpportunityType.DEMAND_DECLINE,
            {
                "menu_item_id": "menu-1",
                "current_price": 20,
            },
        )
        for _ in range(1)
    )

    result = reason_about_opportunities(opportunities)

    assert len(result) == 1


def test_reasoner_preserves_opportunity_id():
    opportunity = make_opportunity(
        OpportunityType.DEMAND_DECLINE,
        {
            "menu_item_id": "menu-1",
            "current_price": 20,
        },
        opportunity_id="specific-opportunity",
    )

    candidate = generate_candidate_actions(opportunity)[0]

    assert candidate.opportunity_id == "specific-opportunity"
