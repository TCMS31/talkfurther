"""Tests for routing, context assembly, and the grounding verifier's cost gate.

These three sit between the guardrails and the model and were previously covered
only indirectly, through whole-pipeline tests. Each has a failure mode that a
pipeline test hides: a router that silently guesses, a prompt that leaks the
whole knowledge base, and a verifier that bills for turns with nothing to verify.
"""

from __future__ import annotations

import pytest

from app.guardrails.grounding import GroundingVerifier, is_claim_bearing, repair_instruction
from app.knowledge.retrieval import get_kb
from app.models import GuardVerdict, Intent, RouteDecision, Violation
from app.prompts.render import build_messages, build_system_prompt
from app.router import IntentRouter
from app.session import Session
from app.testing.fake_llm import FakeLLMClient


@pytest.fixture
def session() -> Session:
    return Session(session_id="test")


# ─── Router ───────────────────────────────────────────────────────────────────


class _UnavailableLLM(FakeLLMClient):
    def structured(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("model unreachable")


class _UnparseableLLM(FakeLLMClient):
    def structured(self, *args, **kwargs):  # type: ignore[override]
        return None


@pytest.mark.parametrize("llm_class", [_UnavailableLLM, _UnparseableLLM])
def test_an_unroutable_turn_degrades_to_unknown_not_a_guess(
    session: Session, llm_class: type
) -> None:
    """UNKNOWN retrieves general + contact facts, which is the honest-handoff
    path. Guessing a topic would put the agent on a subject it cannot support."""
    decision = IntentRouter(llm_class()).route(session, "do you take Medicaid?")
    assert decision.intent is Intent.UNKNOWN


def test_router_failure_preserves_an_established_subject(session: Session) -> None:
    session.subject = "other"
    decision = IntentRouter(_UnavailableLLM()).route(session, "and the cost?")
    assert decision.subject == "other"


class _UnclearSubjectLLM(FakeLLMClient):
    def structured(self, *args, **kwargs):  # type: ignore[override]
        return RouteDecision(intent=Intent.PRICING, subject="unclear")


def test_an_unclear_subject_inherits_the_established_one(session: Session) -> None:
    """Once we know someone is asking for a relative, a later bare 'how much?'
    must not flip the agent back to second-person pronouns."""
    session.subject = "other"
    decision = IntentRouter(_UnclearSubjectLLM()).route(session, "how much?")
    assert decision.subject == "other"


def test_an_explicit_subject_is_not_overwritten_by_the_session(session: Session) -> None:
    session.subject = "unclear"
    decision = IntentRouter(FakeLLMClient()).route(session, "my mom needs detox")
    assert isinstance(decision, RouteDecision)
    assert decision.subject == "other"


# ─── Context assembly ─────────────────────────────────────────────────────────


def test_the_prompt_carries_only_the_retrieved_facts(session: Session) -> None:
    """The substantive change from the baseline: not the whole fact sheet."""
    kb = get_kb()
    facts = kb.for_intent(Intent.CAREERS)
    prompt = build_system_prompt(session, Intent.CAREERS, facts, kb)

    assert len(facts) < len(kb.facts)
    for fact in facts:
        assert fact.id in prompt
    absent = [f for f in kb.facts.values() if f not in facts]
    assert any(f.id not in prompt for f in absent)


def test_first_turn_asks_for_a_greeting_and_a_disclosure(session: Session) -> None:
    prompt = build_system_prompt(session, Intent.SMALLTALK, [], get_kb())
    assert "This is the first turn" in prompt
    assert "recording disclosure" in prompt


def test_a_greeted_session_is_told_not_to_greet_again(session: Session) -> None:
    session.greeted = True
    session.disclosed = True
    prompt = build_system_prompt(session, Intent.SMALLTALK, [], get_kb())
    assert "ALREADY greeted" in prompt
    assert "Do not repeat it" in prompt


def test_known_slots_are_listed_so_they_are_not_re_asked(session: Session) -> None:
    session.remember(first_name="James", email="james@example.com")
    prompt = build_system_prompt(session, Intent.CONTACT, [], get_kb())
    assert "do NOT ask for these again" in prompt
    assert "James" in prompt
    assert "james@example.com" in prompt


def test_a_crisis_earlier_in_the_conversation_changes_the_instructions(
    session: Session,
) -> None:
    session.crisis_flagged = True
    prompt = build_system_prompt(session, Intent.PRICING, [], get_kb())
    assert "disclosed a crisis" in prompt
    assert "Do not push scheduling or pricing" in prompt


def test_an_empty_retrieval_becomes_an_explicit_i_do_not_know(session: Session) -> None:
    prompt = build_system_prompt(session, Intent.UNKNOWN, [], get_kb())
    assert "No facts retrieved" in prompt


@pytest.mark.parametrize(
    ("subject", "needle"),
    [("self", "THEMSELVES"), ("other", "SOMEONE ELSE"), ("unclear", "not yet clear")],
)
def test_pronoun_guidance_follows_the_subject(
    session: Session, subject: str, needle: str
) -> None:
    session.subject = subject  # type: ignore[assignment]
    assert needle in build_system_prompt(session, Intent.PRICING, [], get_kb())


def test_messages_put_the_system_prompt_first_and_do_not_duplicate_the_turn(
    session: Session,
) -> None:
    session.add_user("How much does it cost?")
    messages = build_messages(session, Intent.PRICING, [], get_kb())
    assert messages[0]["role"] == "system"
    assert [m["content"] for m in messages[1:]].count("How much does it cost?") == 1


def test_history_is_truncated_to_the_limit(session: Session) -> None:
    for i in range(30):
        session.add_user(f"turn {i}")
    messages = build_messages(session, Intent.PRICING, [], get_kb(), history_limit=4)
    assert len(messages) == 5  # system + 4


# ─── Grounding verifier cost gate ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "response",
    [
        "What's the best number to reach you on?",
        "Sure thing!",
        "Got it. What's your last name?",
        "Happy to help with that. What else can I tell you?",
        "Thanks! Which day suits you?",
        "I'm really sorry to hear that. Would you like me to connect you?",
        "",
        "   ",
    ],
)
def test_claim_free_turns_are_not_sent_to_the_verifier(response: str) -> None:
    assert not is_claim_bearing(response)


@pytest.mark.parametrize(
    "response",
    [
        "Treatment starts at $30,000 a month.",
        "We have 8 rooms",
        "Tours run Monday through Friday, and I can check a time for you if you "
        "let me know which day suits.",
    ],
)
def test_claim_bearing_turns_are_verified(response: str) -> None:
    assert is_claim_bearing(response)


@pytest.mark.parametrize(
    "response",
    [
        # grounding.py's own motivating example: no number, no URL, no phone —
        # nothing the deterministic layer can match on — and a fabrication.
        "Each room has its own thermostat.",
        "We accept Medicaid.",  # we do not
        "Kosher meals are available.",
        "All meals are chef-prepared.",
        "We have an on-site gym.",
        "Pets are not allowed.",
        "Yes, we take Aetna.",
    ],
)
def test_short_literal_free_fabrications_reach_the_verifier(response: str) -> None:
    """Regression: the gate used to skip any reply of six words or fewer that
    contained no digit, which is precisely the shape of an unverifiable
    fabrication. Every string here was silently unverified."""
    assert is_claim_bearing(response)


def test_the_verifier_can_be_switched_off_for_cost_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("ENABLE_GROUNDING_VERIFIER", "false")
    get_settings.cache_clear()
    verifier = GroundingVerifier(FakeLLMClient())
    assert verifier.verify("Treatment starts at $30,000.", kb=get_kb(), facts=[]) is None


def test_a_skipped_verification_is_none_not_a_pass() -> None:
    """The trace has to distinguish 'checked and passed' from 'not checked'."""
    verifier = GroundingVerifier(FakeLLMClient())
    assert verifier.verify("Sure!", kb=get_kb(), facts=[]) is None


def test_an_unreachable_verifier_warns_rather_than_blocking() -> None:
    """The deterministic floor has already passed; refusing to answer because a
    secondary check is down would be the worse outage."""
    verdict = GroundingVerifier(_UnavailableLLM()).verify(
        "Treatment starts at $30,000 a month.", kb=get_kb(), facts=[]
    )
    assert isinstance(verdict, GuardVerdict)
    assert verdict.passed
    assert [v.code for v in verdict.violations] == ["verifier_unavailable"]
    assert verdict.blocking == []


def test_an_unparseable_verdict_warns_rather_than_blocking() -> None:
    verdict = GroundingVerifier(_UnparseableLLM()).verify(
        "Treatment starts at $30,000 a month.", kb=get_kb(), facts=[]
    )
    assert verdict is not None and verdict.passed
    assert [v.code for v in verdict.violations] == ["verifier_unparseable"]


def test_the_repair_instruction_quotes_every_violation() -> None:
    text = repair_instruction(
        [
            Violation(code="ungrounded_number", detail="Stated the number '4,200'."),
            Violation(code="coverage_claim", detail="Implied coverage is confirmed."),
        ]
    )
    assert "Stated the number '4,200'." in text
    assert "Implied coverage is confirmed." in text
    assert "Rewrite it" in text
