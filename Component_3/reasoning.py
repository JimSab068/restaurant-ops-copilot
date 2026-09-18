# """Bounded reasoning from Component 3 opportunities to Component 2 inputs.

# This module deliberately uses transparent rules for its first iteration.  An
# LLM can later rank or explain these candidates, but must consume the same
# tenant-scoped evidence and cannot obtain execution authority from this layer.
# """

# from __future__ import annotations
# import math
# from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
# from typing import Iterable, Any

# from Component_1.models import ActionType

# from .models import CandidateAction, Opportunity, OpportunityType


# PRICE_STEP = Decimal("0.25")
# TARGET_CONTRIBUTION_MARGIN = Decimal("0.35")
# DEMAND_RECOVERY_DISCOUNT = Decimal("0.05")



# class ReasonerError(ValueError): 
#     """Raised when an opportunity cannot be safely converted into a candidate."""


# def _finite_decimal(value: Any, field_name: str) -> Decimal: 
#     """ Convert a numeric value into a finite Decimal. Booleans are explicitly rejected because bool is a subclass of int in Python and must never silently become an economic value. """ 
#     if isinstance(value, bool): 
#         raise ReasonerError(f"{field_name} must be numeric, not bool") 

#     if value is None: 
#         raise ReasonerError(f"{field_name} is required") 
#     try: 
#         numeric = Decimal(str(value)) 
#     except (InvalidOperation, ValueError, TypeError) as exc: 
#         raise ReasonerError(f"{field_name} must be numeric") from exc 
#     if not numeric.is_finite(): 
#         raise ReasonerError(f"{field_name} must be finite") 
    
#     return numeric

# def _price_to_step(price: Decimal) -> float: 
#     """Round a finite price to the configured $0.25 price step.""" 
#     if not isinstance(price, Decimal): 
#         raise TypeError("price must be a Decimal") 
#     if not price.is_finite(): 
#         raise ReasonerError("price must be finite") 
    
#     rounded = ( (price / PRICE_STEP) .quantize(Decimal("1"), rounding=ROUND_HALF_UP) * PRICE_STEP ) 

#     if not rounded.is_finite(): 
#         raise ReasonerError("rounded price must be finite") 

#     return float(rounded)



# def _validated_opportunity_evidence( opportunity: Opportunity, ) -> dict[str, Any]: 
#     """ Return a defensive copy of opportunity evidence. The caller must still validate fields required for the particular opportunity type. """ 
#     try: return dict(opportunity.evidence) 
#     except (TypeError, ValueError) as exc: 
#         raise ReasonerError("opportunity evidence must be a mapping") from exc 

# def _margin_candidate(opportunity: Opportunity) -> list[CandidateAction]: 
#     evidence = _validated_opportunity_evidence(opportunity) 
#     menu_item_id = evidence.get("menu_item_id") 

#     if not isinstance(menu_item_id, str) or not menu_item_id.strip(): 
#         return [] 
#     try: 
#         price = _finite_decimal( evidence.get("current_price"), "current_price", ) 
#         recipe_cost = _finite_decimal( evidence.get("recipe_cost"), "recipe_cost", ) 
        
#     except ReasonerError: 
#         return [] # A non-positive current price cannot safely support a pricing action. 
#     if price <= 0: 
#         return [] # A negative recipe cost is economically invalid and must fail closed. 
#     if recipe_cost < 0: 
#         return [] 
#     denominator = Decimal("1") - TARGET_CONTRIBUTION_MARGIN 

#     if denominator <= 0: # Defensive protection if configuration is ever corrupted. 
#         return [] 
#     try: 
#         proposed_price_decimal = recipe_cost / denominator 

#     except (InvalidOperation, ZeroDivisionError): 
#         return [] 

#     if not proposed_price_decimal.is_finite(): 
#         return [] 
#     if proposed_price_decimal <= price: 
#         proposed_price_decimal = price + PRICE_STEP 
#     try: 
#         proposed_price = _price_to_step(proposed_price_decimal) 
#     except (ReasonerError, InvalidOperation, ValueError): 
#         return [] 
        
#     if not math.isfinite(proposed_price) or proposed_price <= 0: 
#         return [] 
#     return [ 
#         CandidateAction( opportunity_id=opportunity.id, action_type=ActionType.PRICE_CHANGE, payload={ "menu_item_id": menu_item_id, "new_price": proposed_price, }, rationale=( "Raise price only enough to restore the configured " "contribution-margin target; Component 2 must estimate " "demand impact." ), ) ] 


# def _demand_candidate(opportunity: Opportunity) -> list[CandidateAction]: 
#         evidence = _validated_opportunity_evidence(opportunity) 
#         menu_item_id = evidence.get("menu_item_id") 
#         if not isinstance(menu_item_id, str) or not menu_item_id.strip(): 
#             return [] 

#         try: price = _finite_decimal( evidence.get("current_price"), "current_price", ) 
#         except ReasonerError: 
#             return [] # Pricing below zero or from a non-positive starting price is unsafe. 
#         if price <= 0: 
#             return [] 
            
#         try: proposed_price_decimal = ( price * (Decimal("1") - DEMAND_RECOVERY_DISCOUNT) ) 
#         except InvalidOperation: 
#             return [] 
#         if not proposed_price_decimal.is_finite(): 
#             return [] 
#         if proposed_price_decimal <= 0: 
#             return [] 
#         try: proposed_price = _price_to_step(proposed_price_decimal) 
#         except (ReasonerError, InvalidOperation, ValueError): 
#             return [] 
            
#         if not math.isfinite(proposed_price) or proposed_price <= 0: 
#             return [] 
        
#         return [
#     CandidateAction(
#         opportunity_id=opportunity.id,
#         action_type=ActionType.PRICE_CHANGE,
#         payload={
#             "menu_item_id": menu_item_id,
#             "new_price": proposed_price,
#         },
#         rationale=(
#             "Test a small, reversible price decrease against the "
#             "detected demand decline."
#         ),
#         assumptions=(
#             "Demand decline is price-sensitive; simulate the proposed "
#             "price change with Component 2 before execution.",
#         ),
#     )
# ]
    
# def generate_candidate_actions( opportunity: Opportunity, ) -> list[CandidateAction]: 
#     """ Produce safe-to-simulate candidates, never an execution instruction. Invalid or malformed economic evidence fails closed by returning no candidate. This prevents bad detector/LLM input from becoming an actionable recommendation. """ 
#     if not isinstance(opportunity, Opportunity): 
#         raise TypeError("opportunity must be an Opportunity") 
#     if opportunity.opportunity_type is OpportunityType.MARGIN_DETERIORATION: 
#         return _margin_candidate(opportunity) 
#     if opportunity.opportunity_type is OpportunityType.DEMAND_DECLINE: 
#         return _demand_candidate(opportunity) 
#     if opportunity.opportunity_type is OpportunityType.STOCKOUT_EXPOSURE: 
#         # The current Component 2 supplier-switch contract requires a 
#         # # verified `new_supplier_price`. Returning no executable-shaped # payload is safer than fabricating a price for a real procurement 
#         # # action; the caller can surface the opportunity for human review. return [] 
#         # # Defensive fail-closed behavior for future/invalid enum values. 
#         return [] 
        
        
# def reason_about_opportunities( opportunities: Iterable[Opportunity], ) -> dict[str, list[CandidateAction]]: 
#     """ Generate candidates grouped by opportunity ID without crossing tenants. No execution or database side effects occur here. """ 
#     result: dict[str, list[CandidateAction]] = {} 

#     for opportunity in opportunities: 
#         if not isinstance(opportunity, Opportunity): 
#             raise TypeError("all opportunities must be Opportunity instances") 

#         result[opportunity.id] = generate_candidate_actions(opportunity) 

#     return result


"""Bounded reasoning from Component 3 opportunities to Component 2 inputs.

This module deliberately uses transparent rules and is the system's
non-negotiable fallback: it has no external dependency (no LLM, no
network) and must keep working even if llm_reasoner.py is entirely
unavailable. See orchestrator.py for how this is combined with the
optional LLM reasoning path.
"""

from __future__ import annotations
import math
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Iterable, Any

from Component_1.models import ActionType

from .models import CandidateAction, CandidateSource, Opportunity, OpportunityType


PRICE_STEP = Decimal("0.25")
TARGET_CONTRIBUTION_MARGIN = Decimal("0.35")
DEMAND_RECOVERY_DISCOUNT = Decimal("0.05")



class ReasonerError(ValueError): 
    """Raised when an opportunity cannot be safely converted into a candidate."""


def _finite_decimal(value: Any, field_name: str) -> Decimal: 
    """ Convert a numeric value into a finite Decimal. Booleans are explicitly rejected because bool is a subclass of int in Python and must never silently become an economic value. """ 
    if isinstance(value, bool): 
        raise ReasonerError(f"{field_name} must be numeric, not bool") 

    if value is None: 
        raise ReasonerError(f"{field_name} is required") 
    try: 
        numeric = Decimal(str(value)) 
    except (InvalidOperation, ValueError, TypeError) as exc: 
        raise ReasonerError(f"{field_name} must be numeric") from exc 
    if not numeric.is_finite(): 
        raise ReasonerError(f"{field_name} must be finite") 
    
    return numeric

def _price_to_step(price: Decimal) -> float: 
    """Round a finite price to the configured $0.25 price step.""" 
    if not isinstance(price, Decimal): 
        raise TypeError("price must be a Decimal") 
    if not price.is_finite(): 
        raise ReasonerError("price must be finite") 
    
    rounded = ( (price / PRICE_STEP) .quantize(Decimal("1"), rounding=ROUND_HALF_UP) * PRICE_STEP ) 

    if not rounded.is_finite(): 
        raise ReasonerError("rounded price must be finite") 

    return float(rounded)



def _validated_opportunity_evidence( opportunity: Opportunity, ) -> dict[str, Any]: 
    """ Return a defensive copy of opportunity evidence. The caller must still validate fields required for the particular opportunity type. """ 
    try: return dict(opportunity.evidence) 
    except (TypeError, ValueError) as exc: 
        raise ReasonerError("opportunity evidence must be a mapping") from exc 

def _margin_candidate(opportunity: Opportunity) -> list[CandidateAction]: 
    evidence = _validated_opportunity_evidence(opportunity) 
    menu_item_id = evidence.get("menu_item_id") 

    if not isinstance(menu_item_id, str) or not menu_item_id.strip(): 
        return [] 
    if menu_item_id not in opportunity.affected_entity_ids:
        return []
    try: 
        price = _finite_decimal( evidence.get("current_price"), "current_price", ) 
        recipe_cost = _finite_decimal( evidence.get("recipe_cost"), "recipe_cost", ) 
        
    except ReasonerError: 
        return [] # A non-positive current price cannot safely support a pricing action. 
    if price <= 0: 
        return [] # A negative recipe cost is economically invalid and must fail closed. 
    if recipe_cost < 0: 
        return [] 
    denominator = Decimal("1") - TARGET_CONTRIBUTION_MARGIN 

    if denominator <= 0: # Defensive protection if configuration is ever corrupted. 
        return [] 
    try: 
        proposed_price_decimal = recipe_cost / denominator 

    except (InvalidOperation, ZeroDivisionError): 
        return [] 

    if not proposed_price_decimal.is_finite(): 
        return [] 
    if proposed_price_decimal <= price: 
        proposed_price_decimal = price + PRICE_STEP 
    try: 
        proposed_price = _price_to_step(proposed_price_decimal) 
    except (ReasonerError, InvalidOperation, ValueError): 
        return [] 
        
    if not math.isfinite(proposed_price) or proposed_price <= 0: 
        return [] 
    return [ 
        CandidateAction(
            opportunity_id=opportunity.id,
            action_type=ActionType.PRICE_CHANGE,
            payload={
                "menu_item_id": menu_item_id,
                "new_price": proposed_price,
            },
            rationale=(
                "Raise price only enough to restore the configured "
                "contribution-margin target; Component 2 must estimate "
                "demand impact."
            ),
            source=CandidateSource.DETERMINISTIC,
        )
    ] 


def _demand_candidate(opportunity: Opportunity) -> list[CandidateAction]: 
        evidence = _validated_opportunity_evidence(opportunity) 
        menu_item_id = evidence.get("menu_item_id") 
        if not isinstance(menu_item_id, str) or not menu_item_id.strip(): 
            return [] 
        if menu_item_id not in opportunity.affected_entity_ids:
            return []

        try: price = _finite_decimal( evidence.get("current_price"), "current_price", ) 
        except ReasonerError: 
            return [] # Pricing below zero or from a non-positive starting price is unsafe. 
        if price <= 0: 
            return [] 
            
        try: proposed_price_decimal = ( price * (Decimal("1") - DEMAND_RECOVERY_DISCOUNT) ) 
        except InvalidOperation: 
            return [] 
        if not proposed_price_decimal.is_finite(): 
            return [] 
        if proposed_price_decimal <= 0: 
            return [] 
        try: proposed_price = _price_to_step(proposed_price_decimal) 
        except (ReasonerError, InvalidOperation, ValueError): 
            return [] 
            
        if not math.isfinite(proposed_price) or proposed_price <= 0: 
            return [] 
        
        return [
    CandidateAction(
        opportunity_id=opportunity.id,
        action_type=ActionType.PRICE_CHANGE,
        payload={
            "menu_item_id": menu_item_id,
            "new_price": proposed_price,
        },
        rationale=(
            "Test a small, reversible price decrease against the "
            "detected demand decline."
        ),
        assumptions=(
            "Demand decline is price-sensitive; simulate the proposed "
            "price change with Component 2 before execution.",
        ),
        source=CandidateSource.DETERMINISTIC,
    )
]
    
def generate_candidate_actions( opportunity: Opportunity, ) -> list[CandidateAction]: 
    """ Produce safe-to-simulate candidates, never an execution instruction. Invalid or malformed economic evidence fails closed by returning no candidate. This prevents bad detector/LLM input from becoming an actionable recommendation. """ 
    if not isinstance(opportunity, Opportunity): 
        raise TypeError("opportunity must be an Opportunity") 
    if opportunity.opportunity_type is OpportunityType.MARGIN_DETERIORATION: 
        return _margin_candidate(opportunity) 
    if opportunity.opportunity_type is OpportunityType.DEMAND_DECLINE: 
        return _demand_candidate(opportunity) 
    if opportunity.opportunity_type is OpportunityType.STOCKOUT_EXPOSURE: 
        # The current Component 2 supplier-switch contract requires a 
        # # verified `new_supplier_price`. Returning no executable-shaped # payload is safer than fabricating a price for a real procurement 
        # # action; the caller can surface the opportunity for human review. return [] 
        # # Defensive fail-closed behavior for future/invalid enum values. 
        return [] 
        
        
def reason_about_opportunities( opportunities: Iterable[Opportunity], ) -> dict[str, list[CandidateAction]]: 
    """ Generate candidates grouped by opportunity ID without crossing tenants. No execution or database side effects occur here. """ 
    result: dict[str, list[CandidateAction]] = {} 

    for opportunity in opportunities: 
        if not isinstance(opportunity, Opportunity): 
            raise TypeError("all opportunities must be Opportunity instances") 

        result[opportunity.id] = generate_candidate_actions(opportunity) 

    return result
