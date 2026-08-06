"""Tests for the scripted double's offline reply.

With no reply queued, `FakeLLMClient` answers out of the facts the retrieval
layer put in this turn's system prompt. That is what `OFFLINE_MODE=1` — `make
demo`, the screenshots in `docs/`, and every test in this suite that drives a
full turn — actually shows on screen, so it is worth asserting rather than
assuming.

The property that matters is not phrasing. It is that an offline turn carries
real numbers and URLs and therefore genuinely exercises the egress allowlist.
The previous fallback ("Happy to help with that. What else can I tell you?")
asserted nothing, so it skipped token checking and the grounding verifier alike
and the layers this project exists to demonstrate never ran offline.
"""

from __future__ import annotations

import pytest

from app.guardrails import egress
from app.guardrails.grounding import is_claim_bearing
from app.knowledge.retrieval import get_kb
from app.models import Intent
from app.prompts.render import build_messages
from app.session import Session
from app.testing.fake_llm import FakeLLMClient


def _messages(intent: Intent, question: str) -> list[dict[str, str]]:
    kb = get_kb()
    session = Session(session_id="offline-double")
    session.add_user(question)
    return build_messages(session, intent, kb.for_intent(intent), kb)


@pytest.mark.parametrize(
    ("intent", "question"),
    [
        (Intent.PRICING, "How much does treatment cost?"),
        (Intent.INSURANCE, "Do you take Medicaid?"),
        (Intent.POLICY, "Are dogs allowed?"),
        (Intent.CARE_TYPES, "Do you offer mental health services?"),
        (Intent.AMENITIES, "Can I get kosher and low sodium meals?"),
        (Intent.CONTACT, "What's your phone number?"),
    ],
)
def test_offline_reply_quotes_a_retrieved_fact_verbatim(
    intent: Intent, question: str
) -> None:
    messages = _messages(intent, question)
    reply = FakeLLMClient().complete(messages, stage="agent").text

    statements = [f.statement for f in get_kb().for_intent(intent)]
    assert any(statement in reply for statement in statements), reply


@pytest.mark.parametrize(
    ("intent", "question"),
    [
        (Intent.PRICING, "How much does treatment cost?"),
        (Intent.INSURANCE, "Do you take Medicaid?"),
        (Intent.POLICY, "Are dogs allowed?"),
        (Intent.CARE_TYPES, "Do you offer mental health services?"),
        (Intent.AMENITIES, "Can I get kosher and low sodium meals?"),
        (Intent.CONTACT, "What's your phone number?"),
    ],
)
def test_offline_reply_passes_the_deterministic_egress_guard(
    intent: Intent, question: str
) -> None:
    """Quoting the retrieved facts must never trip the fabrication check.

    If this fails, either the double has started inventing text or the egress
    allowlist has drifted from the knowledge base it is built from.
    """
    kb = get_kb()
    messages = _messages(intent, question)
    reply = FakeLLMClient().complete(messages, stage="agent").text

    verdict = egress.check(
        reply, kb=kb, facts=kb.for_intent(intent), already_greeted=True
    )
    assert verdict.passed, verdict.violations


def test_offline_reply_is_claim_bearing_so_the_verifier_path_runs() -> None:
    """The old fallback was claim-free and skipped verification entirely."""
    reply = FakeLLMClient().complete(
        _messages(Intent.PRICING, "How much does treatment cost?"), stage="agent"
    ).text
    assert is_claim_bearing(reply)


def test_a_queued_scripted_reply_still_wins() -> None:
    client = FakeLLMClient(scripted=["Pinned reply."])
    messages = _messages(Intent.PRICING, "How much does treatment cost?")
    assert client.complete(messages, stage="agent").text == "Pinned reply."


def test_a_prompt_with_no_facts_falls_back_to_the_generic_reply() -> None:
    kb = get_kb()
    session = Session(session_id="offline-double")
    session.add_user("Hello there")
    messages = build_messages(session, Intent.UNKNOWN, [], kb)
    reply = FakeLLMClient().complete(messages, stage="agent").text
    assert reply == "Happy to help with that. What else can I tell you?"
    assert not is_claim_bearing(reply)


def test_an_unrelated_question_falls_back_rather_than_quoting_a_random_fact() -> None:
    """No keyword overlap means no answer — better than a confident non-sequitur."""
    messages = _messages(Intent.PRICING, "Xyzzy plugh?")
    reply = FakeLLMClient().complete(messages, stage="agent").text
    assert reply == "Happy to help with that. What else can I tell you?"


def test_the_double_returns_a_clean_grounding_verdict() -> None:
    """Otherwise every offline trace carries a `verifier_unparseable` warning
    that looks like a defect. The verdict is a double's, not a judgement."""
    from app.guardrails.grounding import GroundingVerifier

    verdict = GroundingVerifier(FakeLLMClient()).verify(
        "Treatment starts at $30,000 a month.", kb=get_kb(), facts=[]
    )
    assert verdict is not None and verdict.passed
    assert verdict.violations == []
