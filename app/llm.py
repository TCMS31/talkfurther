"""Thin OpenAI wrapper.

Two responsibilities beyond passing calls through:

1. **Usage accounting.** Every call records tokens and estimated cost against the
   current turn, so the trace can show what a guardrail actually costs. The
   verification-vs-latency tradeoff is only defensible if it is measured.
2. **Structured output.** Classification and verification use schema-constrained
   parsing rather than free text plus a regex, so a malformed guardrail verdict
   is impossible rather than merely unlikely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.config import get_settings

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens. Config constants, not gospel — update alongside vendor pricing.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
}
_DEFAULT_PRICING = (2.50, 10.00)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def add(self, stage: str, model: str, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1
        self.by_stage[stage] = self.by_stage.get(stage, 0) + prompt + completion
        rate_in, rate_out = PRICING.get(model, _DEFAULT_PRICING)
        self.cost_usd += (prompt * rate_in + completion * rate_out) / 1_000_000


@dataclass
class Completion:
    text: str
    tool_calls: list[Any] = field(default_factory=list)
    raw_message: Any = None
    duration_ms: float = 0.0


class LLMClient:
    def __init__(self, usage: Usage | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.openai_api_key or "missing-key",
            timeout=settings.request_timeout_s,
        )
        self.usage = usage or Usage()

    # ── Free-form / tool-calling ──────────────────────────────────────────────

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        stage: str,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
    ) -> Completion:
        model = model or self._settings.agent_model
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        duration_ms = (time.perf_counter() - started) * 1000
        self._record(stage, model, response)

        message = response.choices[0].message
        return Completion(
            text=(message.content or "").strip(),
            tool_calls=list(message.tool_calls or []),
            raw_message=message,
            duration_ms=duration_ms,
        )

    # ── Schema-constrained ────────────────────────────────────────────────────

    def structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        stage: str,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T | None:
        """Parse into `schema`, or None if the model refused or output was invalid.

        Callers must handle None. Every current caller is a guardrail, and each
        fails safe — a router that cannot classify returns UNKNOWN, a verifier
        that cannot parse is treated as a verification failure.
        """
        model = model or self._settings.guard_model
        response = self._client.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=schema,
            temperature=temperature,
        )
        self._record(stage, model, response)
        return response.choices[0].message.parsed

    # ── Internals ─────────────────────────────────────────────────────────────

    def _record(self, stage: str, model: str, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage.add(
            stage,
            model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )
