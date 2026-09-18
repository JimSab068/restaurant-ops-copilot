"""Provider-agnostic LLM interface for Component 3.

Nothing in Component 3 outside this file (and optional provider adapters
such as llm_client_openai.py) should import a concrete LLM SDK. That
keeps the deterministic reasoning path, and every test that only needs
FakeLLMClient below, usable with zero LLM dependencies installed —
and keeps a future provider swap to one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class LLMClientError(RuntimeError):
    """
    Raised when an LLM request fails or returns unusable output.

    llm_reasoner.py catches this narrowly and falls back to the
    deterministic reasoner — it is never meant to propagate further.
    """


@dataclass(frozen=True)
class LLMResponse:
    """
    Raw text response from an LLM provider, before any JSON parsing or
    candidate validation happens.
    """

    text: str
    model: str
    request_id: str | None = None


class LLMClient(Protocol):
    """Structural interface every LLM provider adapter implements."""

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        ...


@dataclass
class FakeLLMClient:
    """
    Deterministic in-memory LLMClient for tests and local development.

    Returns a fixed JSON string (or raises LLMClientError) regardless of
    the prompts it receives, so tests can exercise llm_reasoner.py and
    orchestrator.py without network access or an API key.

    Not imported by any production code path — tests and examples only.

    Usage:

        client = FakeLLMClient(response_text=json.dumps({
            "candidates": [{
                "action_type": "price_change",
                "payload": {"menu_item_id": "menu-1", "new_price": 12.5},
                "rationale": "test",
                "requires_human_review": True,
                "assumptions": [],
            }],
        }))
        reasoner = LLMReasoner(client)
    """

    response_text: str = '{"candidates": []}'
    raise_error: bool = False
    model: str = "fake-model"
    calls: list[tuple[str, str]] = field(default_factory=list, compare=False)

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        self.calls.append((system_prompt, user_prompt))

        if self.raise_error:
            raise LLMClientError("FakeLLMClient configured to fail")

        return LLMResponse(
            text=self.response_text,
            model=self.model,
            request_id="fake-request",
        )