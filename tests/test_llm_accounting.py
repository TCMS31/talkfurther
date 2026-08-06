"""Tests for usage and cost accounting.

The tradeoff table's central claim — that a second verification call is worth its
latency and cost — is only defensible if the cost figure in the trace is right.
Expected values here are derived by hand from the published per-million rates,
not by re-running the implementation's own arithmetic.
"""

from __future__ import annotations

import pytest

from app.llm import PRICING, Usage


def test_a_fresh_ledger_is_empty() -> None:
    usage = Usage()
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (0, 0, 0)
    assert usage.cost_usd == 0.0


@pytest.mark.parametrize(
    ("model", "prompt", "completion", "expected_usd"),
    [
        # gpt-4o: $2.50 / 1M in, $10.00 / 1M out.
        # 1,000,000 * 2.50/1e6 = 2.50 ; 1,000,000 * 10.00/1e6 = 10.00
        ("gpt-4o", 1_000_000, 1_000_000, 12.50),
        # 2,000 in = 0.005 ; 500 out = 0.005
        ("gpt-4o", 2_000, 500, 0.01),
        # gpt-4o-mini: $0.15 / 1M in, $0.60 / 1M out.
        # 100,000 in = 0.015 ; 10,000 out = 0.006
        ("gpt-4o-mini", 100_000, 10_000, 0.021),
        # gpt-4.1: $2.00 / 1M in, $8.00 / 1M out.
        ("gpt-4.1", 500_000, 250_000, 3.00),
    ],
)
def test_cost_matches_the_published_rate(
    model: str, prompt: int, completion: int, expected_usd: float
) -> None:
    usage = Usage()
    usage.add("agent", model, prompt, completion)
    assert usage.cost_usd == pytest.approx(expected_usd, rel=1e-9)


def test_an_unknown_model_falls_back_to_the_default_rate() -> None:
    """Never silently free. A model we have no price for is billed at the
    top-tier rate so a trace under-reports nothing."""
    known = Usage()
    known.add("agent", "gpt-4o", 1_000_000, 0)
    unknown = Usage()
    unknown.add("agent", "some-model-we-have-not-priced", 1_000_000, 0)
    assert unknown.cost_usd == pytest.approx(known.cost_usd)


def test_usage_accumulates_across_stages() -> None:
    usage = Usage()
    usage.add("router", "gpt-4o-mini", 600, 20)
    usage.add("crisis_classifier", "gpt-4o-mini", 900, 15)
    usage.add("agent", "gpt-4o", 2_400, 180)

    assert usage.calls == 3
    assert usage.prompt_tokens == 3_900
    assert usage.completion_tokens == 215
    assert usage.by_stage == {
        "router": 620,
        "crisis_classifier": 915,
        "agent": 2_580,
    }


def test_repeated_calls_in_one_stage_are_summed() -> None:
    usage = Usage()
    usage.add("agent", "gpt-4o", 100, 10)
    usage.add("agent", "gpt-4o", 200, 20)
    assert usage.by_stage["agent"] == 330
    assert usage.calls == 2


def test_the_guard_model_is_cheaper_than_the_agent_model() -> None:
    """The reason classification and verification run on the cheap tier."""
    agent_in, agent_out = PRICING["gpt-4o"]
    guard_in, guard_out = PRICING["gpt-4o-mini"]
    assert guard_in < agent_in
    assert guard_out < agent_out


# ─── The client wrapper ───────────────────────────────────────────────────────
#
# `app/llm.py` is the only module that touches the network. It is exercised here
# against a stub that mimics the OpenAI SDK's response shape, so the wrapper's
# own logic — usage recording, tool-call passthrough, refusal handling — is
# covered without a key and without a request. `conftest.block_network`
# guarantees the second part.


class _StubUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _StubMessage:
    def __init__(self, content=None, tool_calls=None, parsed=None) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.parsed = parsed


class _StubResponse:
    def __init__(self, message, usage=None) -> None:
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = usage


class _StubOpenAI:
    """Enough of the SDK surface for the wrapper, and a record of what it sent."""

    def __init__(self, response) -> None:
        self.response = response
        self.seen: dict = {}
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.seen = kwargs
                return outer.response

            def parse(self, **kwargs):
                outer.seen = kwargs
                return outer.response

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _client(response):
    from app.llm import LLMClient

    client = LLMClient()
    client._client = _StubOpenAI(response)
    return client


def test_complete_returns_text_and_records_usage() -> None:
    client = _client(
        _StubResponse(_StubMessage(content="  Treatment starts at $30,000.  "),
                      usage=_StubUsage(1_000, 200))
    )
    completion = client.complete([{"role": "user", "content": "cost?"}], stage="agent")

    assert completion.text == "Treatment starts at $30,000."
    assert completion.tool_calls == []
    assert client.usage.calls == 1
    assert client.usage.by_stage["agent"] == 1_200


def test_complete_passes_tools_and_sets_tool_choice() -> None:
    client = _client(_StubResponse(_StubMessage(content="ok")))
    client.complete(
        [{"role": "user", "content": "book a tour"}],
        stage="agent",
        tools=[{"type": "function", "function": {"name": "book_tour"}}],
    )
    assert client._client.seen["tool_choice"] == "auto"
    assert client._client.seen["tools"]


def test_complete_omits_tools_when_none_are_offered() -> None:
    client = _client(_StubResponse(_StubMessage(content="ok")))
    client.complete([{"role": "user", "content": "hi"}], stage="baseline")
    assert "tools" not in client._client.seen
    assert "tool_choice" not in client._client.seen


def test_complete_surfaces_tool_calls() -> None:
    call = type("Call", (), {"id": "c1"})()
    client = _client(_StubResponse(_StubMessage(content=None, tool_calls=[call])))
    completion = client.complete([{"role": "user", "content": "tour"}], stage="agent")
    assert completion.text == ""
    assert completion.tool_calls == [call]


def test_a_response_without_usage_is_not_billed() -> None:
    client = _client(_StubResponse(_StubMessage(content="ok"), usage=None))
    client.complete([{"role": "user", "content": "hi"}], stage="agent")
    assert client.usage.calls == 0
    assert client.usage.cost_usd == 0.0


def test_structured_returns_the_parsed_model() -> None:
    from app.models import Intent, RouteDecision

    parsed = RouteDecision(intent=Intent.PRICING, subject="self")
    client = _client(_StubResponse(_StubMessage(parsed=parsed), usage=_StubUsage(60, 15)))
    result = client.structured(
        [{"role": "user", "content": "cost?"}], RouteDecision, stage="router"
    )
    assert result is parsed
    assert client.usage.by_stage["router"] == 75


def test_structured_returns_none_on_a_refusal() -> None:
    """Every caller is a guardrail and each must handle None by failing safe."""
    from app.models import RouteDecision

    client = _client(_StubResponse(_StubMessage(parsed=None)))
    assert client.structured([], RouteDecision, stage="router") is None


def test_structured_defaults_to_the_guard_model_and_complete_to_the_agent_model() -> None:
    from app.config import get_settings
    from app.models import RouteDecision

    settings = get_settings()
    client = _client(_StubResponse(_StubMessage(content="x", parsed=None)))
    client.complete([], stage="agent")
    assert client._client.seen["model"] == settings.agent_model
    client.structured([], RouteDecision, stage="router")
    assert client._client.seen["model"] == settings.guard_model
