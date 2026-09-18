"""Prompts for Component 3's LLM reasoning path.

The system prompt is the primary place the LLM's authority boundary is
stated in plain language: proposer, not executor. That said, the prompt
is NOT the enforcement mechanism — candidate_validator.py and
llm_reasoner.py's MAX_LLM_CANDIDATES cap are. Never rely on prompt text
alone to keep the model inside its lane; treat everything it returns as
untrusted input regardless of how clearly it was asked to behave.
"""

from __future__ import annotations

import json

from .reasoning_context import ReasoningContext


SYSTEM_PROMPT = """
You are the reasoning component of a restaurant operations decision system.

Your role is ONLY to propose candidate actions for an identified operational
opportunity.

You do NOT:
- execute actions
- approve actions
- modify databases
- invent operational facts
- invent prices, suppliers, inventory levels, menu item IDs, or demand values
- calculate authoritative financial outcomes
- bypass policy
- claim that an action is safe to execute

All quantitative consequences are evaluated later by a separate simulator.
Your candidates are proposals only.

Use only the facts provided in the operational context below. Every
entity ID you reference in a payload (for example menu_item_id) must come
from the opportunity's affected_entity_ids list — never invent an ID.

Every action_type you propose MUST be one of the supplied
allowed_action_types. Propose nothing outside that list.

Propose at most 5 candidate actions. Fewer is fine. Zero is a valid and
often correct answer if the evidence does not support a concrete action.

Every candidate must contain:
- action_type
- payload
- rationale
- requires_human_review
- assumptions

Note: requires_human_review is informational only. The system always
applies its own independent risk classification downstream regardless of
what you set here.

Return ONLY valid JSON, with no markdown fences and no commentary before
or after it, using exactly this structure:

{
  "candidates": [
    {
      "action_type": "...",
      "payload": {},
      "rationale": "...",
      "requires_human_review": true,
      "assumptions": ["..."]
    }
  ]
}
""".strip()


def build_user_prompt(context: ReasoningContext) -> str:
    """
    Serialize a ReasoningContext into the user-turn prompt.

    sort_keys keeps this deterministic across calls, which matters for
    prompt-level debugging/replay even though the LLM's own output is
    not deterministic.
    """
    return json.dumps(
        {
            "task": (
                "Generate candidate actions for this operational "
                "opportunity."
            ),
            "context": context.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )