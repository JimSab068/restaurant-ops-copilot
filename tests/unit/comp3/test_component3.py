"""Offline security and contract tests for Component 3.

These tests intentionally never open a database connection or invoke an LLM
API.  Database-facing detection is tested through its tenant-scoped boundary;
the Bedrock adapter is exercised with an in-memory boto3 replacement.
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from Component_1.models import ActionType
from Component_3 import opportunity_detector as detector
from Component_3.action_policy import allowed_llm_actions_for
from Component_3.candidate_merger import merge_candidates
from Component_3.candidate_validator import (
    CandidateValidationError,
    validate_candidate_against_opportunity,
    validate_llm_candidate,
)
from Component_3.llm_client import FakeLLMClient, LLMClientError
from Component_3.llm_client_bedrock import (
    BedrockJSONLLMClient,
    BedrockLLMClient,
    _extract_text,
    _strip_json_fences,
)
from Component_3.llm_prompts import SYSTEM_PROMPT, build_user_prompt
from Component_3.llm_reasoner import LLMReasoner, LLMReasonerError, MAX_LLM_CANDIDATES
from Component_3.models import CandidateAction, CandidateSource, Opportunity, OpportunityType
from Component_3.orchestrator import reason_about_opportunities, reason_about_opportunity
from Component_3.reasoning import generate_candidate_actions
from Component_3.reasoning_context import ReasoningContextError, build_reasoning_context


TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def clean_db():
    """Override the repository DB fixture: this module is intentionally offline."""
    yield


def opportunity(kind=OpportunityType.MARGIN_DETERIORATION, tenant_id=TENANT_A, **evidence):
    base = {"menu_item_id": "menu-a", "current_price": 10, "recipe_cost": 8}
    base.update(evidence)
    return Opportunity(
        id=f"{kind.value}:menu-a", tenant_id=tenant_id, opportunity_type=kind,
        title="Material operational change", cause="Verified source data",
        affected_entity_ids=("menu-a",), estimated_monthly_impact=-42.0,
        confidence=0.8, evidence=base,
    )


def raw_price(menu_item_id="menu-a", new_price=11, **extra):
    result = {"action_type": "price_change", "payload": {"menu_item_id": menu_item_id, "new_price": new_price}, "rationale": "Test after simulation", "requires_human_review": False, "assumptions": ["Demand remains stable"]}
    result.update(extra)
    return result


@pytest.mark.parametrize("bad", ["", " ", None, 7])
def test_opportunity_rejects_malformed_identifiers(bad):
    with pytest.raises((TypeError, ValueError)):
        Opportunity(bad, TENANT_A, OpportunityType.MARGIN_DETERIORATION, "t", "c", ("menu-a",), 1, .5)


def test_opportunity_contract_rejects_non_mapping_evidence_and_entity_string():
    with pytest.raises(TypeError):
        Opportunity("id", TENANT_A, OpportunityType.MARGIN_DETERIORATION, "t", "c", ("menu-a",), 1, .5, evidence=[])
    with pytest.raises(ValueError):
        Opportunity("id", TENANT_A, OpportunityType.MARGIN_DETERIORATION, "t", "c", "menu-a", 1, .5)


def test_models_serialize_defensive_copies_and_validate_candidate_metadata():
    item = opportunity()
    serialized = item.to_dict()
    serialized["evidence"]["menu_item_id"] = "attacker"
    assert item.evidence["menu_item_id"] == "menu-a"
    with pytest.raises(TypeError):
        CandidateAction("id", ActionType.PRICE_CHANGE, {"x": 1}, "why", assumptions=["not tuple"])


@pytest.mark.parametrize("value", [True, "NaN", float("inf"), 0, -1])
def test_llm_price_validation_fails_closed_for_dangerous_values(value):
    candidate = validate_llm_candidate(raw_price(new_price=value), opportunity())
    assert not validate_candidate_against_opportunity(candidate, opportunity())


def test_llm_validator_blocks_cross_tenant_or_hallucinated_entity_and_forces_review():
    own = opportunity()
    parsed = validate_llm_candidate(raw_price(menu_item_id="menu-b"), own)
    assert parsed.requires_human_review is True
    assert parsed.source is CandidateSource.LLM
    assert not validate_candidate_against_opportunity(parsed, own)
    wrong_opportunity = Opportunity("other", TENANT_B, OpportunityType.MARGIN_DETERIORATION, "t", "c", ("menu-a",), 1, .5)
    assert not validate_candidate_against_opportunity(parsed, wrong_opportunity)


@pytest.mark.parametrize("raw", [{}, {"action_type": "unknown", "payload": {"x": 1}, "rationale": "x"}, raw_price(assumptions="not-list")])
def test_llm_structural_validator_rejects_untrusted_shapes(raw):
    with pytest.raises(CandidateValidationError):
        validate_llm_candidate(raw, opportunity())


def test_policy_is_fail_closed_and_stockout_never_calls_llm():
    assert allowed_llm_actions_for("margin_deterioration") == ()
    assert allowed_llm_actions_for(OpportunityType.STOCKOUT_EXPOSURE) == ()
    client = FakeLLMClient(response_text="not json")
    assert reason_about_opportunity(opportunity(OpportunityType.STOCKOUT_EXPOSURE), llm_reasoner=LLMReasoner(client)) == []
    assert client.calls == []


@pytest.mark.parametrize("kind,evidence,expected", [
    (OpportunityType.MARGIN_DETERIORATION, {}, 12.25),
    (OpportunityType.DEMAND_DECLINE, {"recipe_cost": 1}, 9.5),
])
def test_deterministic_reasoning_is_offline_and_rounds_to_price_steps(kind, evidence, expected):
    candidates = generate_candidate_actions(opportunity(kind, **evidence))
    assert len(candidates) == 1
    assert candidates[0].payload["new_price"] == expected
    assert candidates[0].source is CandidateSource.DETERMINISTIC


@pytest.mark.parametrize("bad_evidence", [
    {"menu_item_id": "menu-b"}, {"current_price": True}, {"current_price": "NaN"}, {"recipe_cost": -1},
])
def test_deterministic_reasoner_fails_closed_on_tampered_evidence(bad_evidence):
    assert generate_candidate_actions(opportunity(**bad_evidence)) == []


def test_llm_reasoner_accepts_only_allowed_valid_candidates_and_enforces_cap():
    payload = {"candidates": [raw_price(new_price=i + 1) for i in range(MAX_LLM_CANDIDATES + 3)]}
    client = FakeLLMClient(response_text=json.dumps(payload))
    candidates = LLMReasoner(client).reason(opportunity(), allowed_action_types=("price_change",))
    assert len(candidates) == MAX_LLM_CANDIDATES
    assert len(client.calls) == 1
    prompt = client.calls[0][1]
    assert TENANT_B not in prompt and '"allowed_action_types":["price_change"]' in prompt


def test_llm_reasoner_rejects_malformed_json_disallowed_actions_and_provider_errors():
    with pytest.raises(LLMReasonerError):
        LLMReasoner(FakeLLMClient(response_text="[]")).reason(opportunity(), allowed_action_types=("price_change",))
    disallowed = FakeLLMClient(response_text=json.dumps({"candidates": [dict(raw_price(), action_type="supplier_switch")]}))
    assert LLMReasoner(disallowed).reason(opportunity(), allowed_action_types=("price_change",)) == []
    with pytest.raises(LLMReasonerError):
        LLMReasoner(FakeLLMClient(raise_error=True)).reason(opportunity(), allowed_action_types=("price_change",))


def test_prompt_context_is_bounded_deterministic_and_requires_actions():
    with pytest.raises(ReasoningContextError):
        build_reasoning_context(opportunity(), allowed_action_types=())
    context = build_reasoning_context(opportunity(), allowed_action_types=["price_change"], constraints={"limit": 2})
    assert json.loads(build_user_prompt(context))["context"]["opportunity"]["tenant_id"] == TENANT_A
    assert "do NOT" in SYSTEM_PROMPT


def test_merger_keeps_deterministic_candidate_and_handles_nested_untrusted_payloads():
    deterministic = CandidateAction("op", ActionType.PRICE_CHANGE, {"a": {"nested": [1]}}, "rule")
    llm = CandidateAction("op", ActionType.PRICE_CHANGE, {"a": {"nested": [1]}}, "model", source=CandidateSource.LLM)
    assert merge_candidates([deterministic], [llm]) == [deterministic]
    mixed_set = CandidateAction("other", ActionType.PRICE_CHANGE, {"values": {1, "one"}}, "rule")
    assert merge_candidates([mixed_set], []) == [mixed_set]


def test_orchestrator_fallback_and_batch_do_not_mix_tenants():
    bad_llm = LLMReasoner(FakeLLMClient(response_text="broken"))
    a, b = opportunity(), opportunity(tenant_id=TENANT_B)
    with pytest.raises(ValueError, match="share a tenant_id"):
        reason_about_opportunities([a, b], llm_reasoner=bad_llm)


def test_detector_validates_time_and_scopes_every_stage_to_canonical_tenant(monkeypatch):
    calls = []
    @contextmanager
    def fake_context(tenant_id):
        calls.append(("context", tenant_id)); yield object()
    def stage(name):
        def run(_session, tenant_id, *args):
            calls.append((name, tenant_id)); return []
        return run
    monkeypatch.setattr(detector, "get_db_context", fake_context)
    monkeypatch.setattr(detector, "_margin_opportunities", stage("margin"))
    monkeypatch.setattr(detector, "_stockout_opportunities", stage("stock"))
    monkeypatch.setattr(detector, "_demand_opportunities", stage("demand"))
    assert detector.detect_opportunities(TENANT_A, now=datetime.now(timezone.utc)) == []
    assert {tenant for _, tenant in calls} == {TENANT_A}
    with pytest.raises(detector.OpportunityDetectorError):
        detector.detect_opportunities("' OR true --")
    with pytest.raises(detector.OpportunityDetectorError):
        detector.detect_opportunities(TENANT_A, now=datetime.now())


def test_detector_math_helpers_normalize_window_and_reject_invalid_windows():
    assert detector._to_monthly(Decimal("14")) == Decimal("30")
    assert detector._money(Decimal("1.235")) == 1.24
    with pytest.raises(detector.OpportunityDetectorError):
        detector._to_monthly(Decimal(1), 0)


class _FakeBedrockRuntime:
    def __init__(self, response): self.response, self.requests = response, []
    def converse(self, **kwargs): self.requests.append(kwargs); return self.response


def test_bedrock_adapter_is_fully_testable_without_network(monkeypatch):
    runtime = _FakeBedrockRuntime({"output": {"message": {"content": [{"text": "{\"candidates\":[]}"}, {"reasoningContent": "hidden"}]}}, "ResponseMetadata": {"RequestId": "req"}})
    fake_boto3 = types.SimpleNamespace(client=lambda service, region_name: runtime)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    client = BedrockLLMClient(model_id="model", region_name="test", max_tokens=8, temperature=.2)
    response = client.generate(system_prompt="system", user_prompt="user")
    assert response.text == '{"candidates":[]}' and response.request_id == "req"
    assert runtime.requests[0]["messages"][0]["content"][0]["text"] == "user"
    with pytest.raises(LLMClientError): BedrockLLMClient(max_tokens=0)
    with pytest.raises(LLMClientError): BedrockLLMClient(temperature=float("nan"))
    assert _strip_json_fences("```json\n{}\n```") == "{}"
    with pytest.raises(LLMClientError): _extract_text({})


def test_bedrock_json_variant_strips_fences_offline(monkeypatch):
    runtime = _FakeBedrockRuntime({"output": {"message": {"content": [{"text": "```json\n{}\n```"}]}}})
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *_args, **_kwargs: runtime))
    assert BedrockJSONLLMClient().generate(system_prompt="s", user_prompt="u").text == "{}"
