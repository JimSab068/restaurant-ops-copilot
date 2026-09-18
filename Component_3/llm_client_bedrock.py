"""
Amazon Bedrock LLMClient adapter for Component 3, targeting Gemma 4.

Import of boto3 is intentionally lazy (inside __init__, not at module
scope) so nothing else in Component 3 — including every test that uses
FakeLLMClient from llm_client.py — requires boto3 or AWS credentials to
be present. The deterministic reasoning path in reasoning.py stays
usable with no AWS dependency at all.

Invocation
----------
Uses the Bedrock Runtime **Converse** API (`bedrock-runtime.converse`),
which is model-agnostic: the same request shape works across Gemma,
Claude, Llama, Nova and others, so swapping models later is a model-ID
change rather than a rewrite. Gemma 4 is also reachable through
Bedrock's OpenAI-compatible endpoint, but Converse is the more stable
target for this codebase since it does not tie us to another provider's
request schema.

The system prompt is passed via Converse's dedicated `system` parameter
rather than being folded into the user turn, which keeps the
proposer-not-executor framing in llm_prompts.py in the position the
model treats as instructions.

Credentials
-----------
Resolved by boto3's normal chain (env vars, shared config, instance/task
role). This module never takes an access key as an argument — on a
deployed tenant-aware worker the task role is the intended path.

Environment
-----------
COMPONENT3_BEDROCK_MODEL_ID
    Bedrock model identifier. Default targets Gemma 4 26B-A4B, the
    cost/latency-oriented MoE variant, which is the right default for
    short structured-JSON reasoning calls like ours. Use the 31B dense
    variant if you want stronger reasoning on margin tradeoffs and can
    absorb the cost; E2B if latency dominates.

COMPONENT3_BEDROCK_REGION
    AWS region. Gemma 4 on Bedrock is available in us-east-1, us-east-2,
    us-west-2 and eu-central-1 (plus GovCloud us-gov-west-1). Defaults
    to AWS_REGION, then us-east-1.

COMPONENT3_LLM_MAX_TOKENS / COMPONENT3_LLM_TEMPERATURE
    Generation bounds. Temperature defaults low because this call must
    return parseable JSON, not prose.
"""

from __future__ import annotations

import json
import math
import os

from .llm_client import LLMClientError, LLMResponse


DEFAULT_MODEL_ID = "google.gemma-4-26b-a4b-instruct-v1:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.2


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise LLMClientError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMClientError(f"{name} must be a number") from exc


class BedrockLLMClient:
    """
    Bedrock Converse-API adapter satisfying the LLMClient Protocol.

    Construction failures (missing boto3, unresolvable credentials) raise
    LLMClientError rather than a boto3-specific exception, so callers
    never need to know which provider is behind the Protocol.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region_name: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv(
            "COMPONENT3_BEDROCK_MODEL_ID",
            DEFAULT_MODEL_ID,
        )

        self.region_name = (
            region_name
            or os.getenv("COMPONENT3_BEDROCK_REGION")
            or os.getenv("AWS_REGION")
            or DEFAULT_REGION
        )

        self.max_tokens = (
            max_tokens
            if max_tokens is not None
            else _int_env("COMPONENT3_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS)
        )

        self.temperature = (
            temperature
            if temperature is not None
            else _float_env(
                "COMPONENT3_LLM_TEMPERATURE",
                DEFAULT_TEMPERATURE,
            )
        )

        if self.max_tokens <= 0:
            raise LLMClientError("max_tokens must be positive")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 1:
            raise LLMClientError("temperature must be a finite value from 0 to 1")

        try:
            import boto3
        except ImportError as exc:
            raise LLMClientError(
                "boto3 is required for BedrockLLMClient"
            ) from exc

        try:
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region_name,
            )
        except Exception as exc:
            raise LLMClientError(
                "failed to create the Bedrock runtime client"
            ) from exc

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        try:
            response = self._client.converse(
                modelId=self.model_id,
                system=[{"text": system_prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": user_prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": self.max_tokens,
                    "temperature": self.temperature,
                },
            )
        except Exception as exc:
            # Deliberately broad: botocore raises a wide family of
            # client/throttling/validation errors, and Component 3's
            # contract is that any of them degrades to the deterministic
            # fallback rather than surfacing provider internals upward.
            raise LLMClientError("Bedrock request failed") from exc

        text = _extract_text(response)

        if not text.strip():
            raise LLMClientError("Bedrock returned empty output")

        request_id = (
            response.get("ResponseMetadata", {}).get("RequestId")
        )

        return LLMResponse(
            text=text,
            model=self.model_id,
            request_id=request_id,
        )


def _extract_text(response: dict) -> str:
    """
    Concatenate the text blocks from a Converse response.

    Converse returns content as a list of typed blocks. Gemma 4 supports
    a built-in reasoning mode, which can emit reasoning blocks alongside
    the answer — those are skipped here, since only the final text
    should ever reach the JSON parser in llm_reasoner.py.
    """
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise LLMClientError(
            "unexpected Bedrock response shape"
        ) from exc

    if not isinstance(content, list):
        raise LLMClientError("unexpected Bedrock content shape")

    parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue

        text = block.get("text")

        if isinstance(text, str):
            parts.append(text)

    return "".join(parts)


def _strip_json_fences(text: str) -> str:
    """
    Remove ```json fences an open-weight model may emit despite being
    told not to.

    Kept here rather than in llm_reasoner.py so the reasoner's parsing
    path stays provider-neutral: this is a known quirk of instruction-
    following in smaller open models, not a property of Component 3's
    contract. Call it from a subclass if your chosen Gemma variant turns
    out to fence its output consistently.
    """
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    return cleaned


class BedrockJSONLLMClient(BedrockLLMClient):
    """
    BedrockLLMClient variant that strips markdown fences from output.

    Use this if the Gemma variant you settle on wraps its JSON in
    ```json fences despite the system prompt forbidding it. Prefer the
    plain BedrockLLMClient first and only switch if you actually observe
    fencing — silently rewriting model output is a behavior worth opting
    into deliberately rather than by default.
    """

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        response = super().generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        cleaned = _strip_json_fences(response.text)

        if not cleaned:
            raise LLMClientError(
                "Bedrock output was empty after fence stripping"
            )

        return LLMResponse(
            text=cleaned,
            model=response.model,
            request_id=response.request_id,
        )
