"""Tests for the deterministic layers.

These need no API key and no network. That is deliberate — the safety floor, the
date logic and the validation boundary are the parts that must not regress, and
they are exactly the parts that can be pinned in CI.
"""

from __future__ import annotations

import pytest

from app.guardrails import egress
from app.guardrails.ingress import deterministic_scan, looks_historical
from app.guardrails.responses import REQUIRED_CRISIS_TOKENS, crisis_response
from app.knowledge.retrieval import get_kb, normalize_token
from app.models import CrisisAssessment, CrisisCategory, Intent

# ─── Crisis detection ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("I took too many pills, I don't feel okay", CrisisCategory.OVERDOSE_MEDICAL),
        ("I don't want to live anymore", CrisisCategory.SUICIDAL_IDEATION),
        ("i wanna kill myself", CrisisCategory.SUICIDAL_IDEATION),
        ("im better off dead", CrisisCategory.SUICIDAL_IDEATION),
        ("I'm going through withdrawals", CrisisCategory.WITHDRAWAL_RISK),
        ("I've been cutting myself again", CrisisCategory.SELF_HARM),
    ],
)
def test_crisis_detected_without_llm(message: str, expected: CrisisCategory) -> None:
    """The floor must hold with no model available at all."""
    assert deterministic_scan(message).category is expected


@pytest.mark.parametrize(
    "message",
    [
        "Hello, my name is James and I am looking to learn how much treatment costs?",
        "Wow! That is really expensive. do you take Medicaid?",
        "I keep relapsing, it seems like there is no end in sight",
        "I need treatment asap",
        "Will insurance cover the cost of treatment?",
        "Do you offer mental health services?",
        "Can my sister come with me?",
        "Are dogs allowed? My mom has a golden retriever she would like to bring with her",
        "Can I get Kosher and low sodium meals?",
        "He is always drunk, even for his daughter's graduation",
        "I would like to come for a tour, does next Sunday at 3pm work?",
        "Yes, Tuesday at 2pm might work",
    ],
)
def test_non_crisis_messages_not_flagged(message: str) -> None:
    """Precision matters too — over-flagging makes the agent useless."""
    assert not deterministic_scan(message).is_crisis


def test_historical_narrative_is_deferred_not_suppressed() -> None:
    message = "I overdosed two years ago but I've been clean since then"
    # Stage 1 still fires: the floor never silently drops.
    assert deterministic_scan(message).is_crisis
    # But it is marked for the classifier to arbitrate on tense.
    assert looks_historical(message)


def test_present_tense_is_never_deferred() -> None:
    assert not looks_historical("I took too many pills, I don't feel okay")


def test_every_crisis_template_carries_its_required_resource() -> None:
    """Guards the templates themselves — an edit dropping 988 must fail here."""
    kb = get_kb()
    for category, required in REQUIRED_CRISIS_TOKENS.items():
        response = crisis_response(category)
        for token in required:
            assert token in response, f"{category.value} template missing {token}"
        verdict = egress.check(
            response,
            kb=kb,
            facts=[],
            crisis=CrisisAssessment(category=category, detected_by="deterministic"),
        )
        assert verdict.passed, f"{category.value}: {verdict.violations}"


# ─── Egress ──────────────────────────────────────────────────────────────────


def _check(kb, response: str, intent: Intent = Intent.PRICING, **kwargs):
    return egress.check(response, kb=kb, facts=kb.for_intent(intent), **kwargs)


def test_grounded_prices_pass(kb) -> None:
    verdict = _check(
        kb, "Treatment starts at $30,000 a month, plus a one-time $3,500 entrance fee."
    )
    assert verdict.passed, verdict.violations


def test_fabricated_price_blocked(kb) -> None:
    verdict = _check(kb, "Rooms run about $4,200 a month depending on the floor plan.")
    assert not verdict.passed
    assert any(v.code == "ungrounded_currency" for v in verdict.violations)


def test_fabricated_phone_blocked(kb) -> None:
    verdict = _check(kb, "Give us a call at 704-555-0199.", intent=Intent.CONTACT)
    assert not verdict.passed
    assert any(v.code == "ungrounded_phone" for v in verdict.violations)


def test_real_phone_passes(kb) -> None:
    assert _check(kb, "You can reach us at 850-445-8362.", intent=Intent.CONTACT).passed


def test_fabricated_url_blocked(kb) -> None:
    verdict = _check(kb, "See https://furtherbh.com/pricing for details.")
    assert not verdict.passed
    assert any(v.code == "ungrounded_url" for v in verdict.violations)


def test_real_careers_url_passes(kb) -> None:
    assert _check(
        kb,
        "Our openings are listed at https://www.talkfurther.com/events-demo",
        intent=Intent.CAREERS,
    ).passed


def test_medical_advice_blocked(kb) -> None:
    verdict = _check(kb, "You should taper off slowly, maybe 10mg a day to start.")
    assert not verdict.passed
    assert any(v.code == "medical_advice" for v in verdict.violations)


def test_coverage_claim_blocked(kb) -> None:
    verdict = _check(kb, "Good news — your insurance will cover the cost of treatment.")
    assert not verdict.passed
    assert any(v.code == "coverage_claim" for v in verdict.violations)


@pytest.mark.parametrize(
    "response",
    [
        "You can leave a voicemail at any time by pressing 0.",
        "Let me check if my director is available. Please hold while I check.",
        "Sorry, I didn't catch that.",
    ],
)
def test_voice_artifacts_blocked(kb, response: str) -> None:
    """The baseline was a phone script; none of this belongs in a text chat."""
    verdict = _check(kb, response)
    assert not verdict.passed
    assert any(v.code == "voice_artifact" for v in verdict.violations)


def test_tool_sourced_values_are_assertable(kb) -> None:
    """Without this, every tool-returned date would read as a hallucination."""
    verdict = _check(
        kb,
        "I have Monday, March 17 at 11:00 AM open — does that work?",
        intent=Intent.TOUR_SCHEDULING,
        tool_results=[
            {
                "tool": "check_tour_availability",
                "result": {"alternatives": [{"human": "Monday, March 17 at 11:00 AM"}]},
            }
        ],
    )
    assert verdict.passed, verdict.violations


def test_ordinary_small_numbers_are_not_flagged(kb) -> None:
    """A known, accepted gap: bare small integers are left alone to avoid noise."""
    assert _check(kb, "There are a couple of options, and 2 of them might suit you.").passed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("$3,500.", "3500"), ("3500", "3500"), ("$30,000", "30000"), ("30,000", "30000")],
)
def test_token_normalisation(raw: str, expected: str) -> None:
    assert normalize_token(raw) == expected


# ─── Knowledge base ──────────────────────────────────────────────────────────


def test_no_duplicate_fact_ids() -> None:
    kb = get_kb()
    assert len(kb.facts) == len({f.id for f in kb.facts.values()})


def test_retrieval_is_a_subset_not_the_whole_kb() -> None:
    """The core context-engineering claim: a turn carries a slice, not everything."""
    kb = get_kb()
    pricing = kb.for_intent(Intent.PRICING)
    assert 0 < len(pricing) < len(kb.facts)


def test_pets_contradiction_resolved() -> None:
    """The baseline both banned pets and listed allowed pets."""
    kb = get_kb()
    pets = kb.facts["policy.pets"]
    assert "25" in pets.statement
    assert "not allowed" not in pets.statement.lower()


# ─── Regressions from code review ────────────────────────────────────────────


def test_unparseable_classifier_does_not_drop_the_deterministic_floor() -> None:
    """A None verdict is a refusal/parse failure, not a finding of safety.

    Regression: on the historical-deferral path, `result is None` fell into the
    same branch as "the classifier cleared it", so an unparseable response
    silently downgraded an overdose disclosure to no-crisis.
    """
    from app.guardrails.ingress import IngressGuard
    from app.llm import Usage

    class _NoVerdict:
        def __init__(self) -> None:
            self.usage = Usage()

        def structured(self, *args, **kwargs):
            return None

    message = "I took too many pills. I used to be clean."
    assert deterministic_scan(message).is_crisis
    assert looks_historical(message)
    assert IngressGuard(_NoVerdict()).assess(message).category is CrisisCategory.OVERDOSE_MEDICAL


@pytest.mark.parametrize(
    "response",
    [
        "Let me pause there and make sure I understood you.",
        "There can be a pause between assessment and admission.",
    ],
)
def test_bare_word_pause_is_not_a_voice_artifact(kb, response: str) -> None:
    """Regression: the optional bracket/duration made any 'pause' blocking."""
    assert _check(kb, response, intent=Intent.AMENITIES).passed


@pytest.mark.parametrize(
    "response",
    ["[10-second pause]", "[Pause for 10 seconds]", "Add a 1-second pause"],
)
def test_stage_direction_pause_is_still_blocked(kb, response: str) -> None:
    verdict = _check(kb, response, intent=Intent.AMENITIES)
    assert not verdict.passed
    assert any(v.code == "voice_artifact" for v in verdict.violations)


@pytest.mark.parametrize(
    "response",
    [
        "I can't tell you whether you're covered — a team member has to verify.",
        "I am not able to confirm if you are covered until benefits are checked.",
    ],
)
def test_compliant_coverage_disclaimer_is_not_blocked(kb, response: str) -> None:
    """Regression: the guard blocked the exact sentence the policy requires."""
    assert _check(kb, response, intent=Intent.INSURANCE).passed


@pytest.mark.parametrize(
    "response",
    [
        "Good news, your insurance will cover the cost of treatment.",
        "You are covered, no need to worry.",
    ],
)
def test_real_coverage_claims_still_blocked(kb, response: str) -> None:
    verdict = _check(kb, response, intent=Intent.INSURANCE)
    assert not verdict.passed
    assert any(v.code == "coverage_claim" for v in verdict.violations)


@pytest.mark.parametrize(
    "response",
    [
        "Let me check if my director of admissions is available for a conversation.",
        "Our admissions director is not currently available.",
        "I'll check if my director is available.",
    ],
)
def test_director_stalling_theatre_is_blocked(kb, response: str) -> None:
    """The baseline's own phrasing must be caught at runtime, not just in evals.

    Regression: the pattern was anchored on a leading "I", so "Let me check if my
    director..." — the exact string from the baseline prompt — slipped through.
    """
    verdict = _check(kb, response)
    assert not verdict.passed
    assert any(v.code == "voice_artifact" for v in verdict.violations)


@pytest.mark.parametrize(
    "response",
    [
        "Our director will call you back once benefits are verified.",
        "I can check availability for Tuesday.",
    ],
)
def test_legitimate_director_mentions_are_not_blocked(kb, response: str) -> None:
    assert _check(kb, response).passed


# ─── Ingress arbitration ─────────────────────────────────────────────────────
#
# `deterministic_scan` and `looks_historical` were each covered above, but the
# rule that combines them — when a model is allowed to overrule the pattern
# layer, and when it is not — was only exercised through the None-verdict
# regression. A mutation that sent *every* deterministic hit to the classifier
# for arbitration passed the whole suite. These pin the arbitration itself.


class _ScriptedClassifier:
    """Records whether it was consulted, and returns a fixed verdict."""

    def __init__(self, verdict: CrisisAssessment | None = None) -> None:
        from app.llm import Usage

        self.usage = Usage()
        self.verdict = verdict
        self.calls = 0

    def structured(self, *_args: object, **_kwargs: object) -> CrisisAssessment | None:
        self.calls += 1
        return self.verdict


class _BrokenClassifier(_ScriptedClassifier):
    def structured(self, *_args: object, **_kwargs: object) -> CrisisAssessment | None:
        self.calls += 1
        raise RuntimeError("classifier unreachable")


PRESENT_TENSE_CRISIS = "I took too many pills, I don't feel okay"
HISTORICAL_CRISIS = "I overdosed two years ago but I've been clean since then"


def test_a_present_tense_hit_never_reaches_the_classifier() -> None:
    """Not merely "is not overruled" — is not *asked about*.

    Consulting the model here would spend money to arbitrate a decision that is
    not the model's to make, and would open the exact prompt-injection surface
    the deterministic floor exists to close.
    """
    from app.guardrails.ingress import IngressGuard

    classifier = _ScriptedClassifier(CrisisAssessment())  # would say "no crisis"
    verdict = IngressGuard(classifier).assess(PRESENT_TENSE_CRISIS)

    assert classifier.calls == 0
    assert verdict.category is CrisisCategory.OVERDOSE_MEDICAL
    assert verdict.detected_by == "deterministic"


def test_a_classifier_cannot_clear_a_present_tense_hit() -> None:
    from app.guardrails.ingress import IngressGuard

    for verdict in (CrisisAssessment(), None):
        assessed = IngressGuard(_ScriptedClassifier(verdict)).assess(PRESENT_TENSE_CRISIS)
        assert assessed.is_crisis, verdict


def test_the_classifier_may_clear_a_past_tense_narrative() -> None:
    """The one permitted downgrade: tense is what patterns cannot read."""
    from app.guardrails.ingress import IngressGuard

    classifier = _ScriptedClassifier(CrisisAssessment())
    assessed = IngressGuard(classifier).assess(HISTORICAL_CRISIS)

    assert classifier.calls == 1
    assert not assessed.is_crisis


def test_a_past_tense_narrative_stands_when_the_classifier_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.guardrails.ingress import IngressGuard

    monkeypatch.setenv("ENABLE_CRISIS_CLASSIFIER", "false")
    get_settings.cache_clear()

    classifier = _ScriptedClassifier(CrisisAssessment())
    assessed = IngressGuard(classifier).assess(HISTORICAL_CRISIS)

    assert classifier.calls == 0
    assert assessed.category is CrisisCategory.OVERDOSE_MEDICAL


def test_an_unreachable_classifier_falls_back_to_stage_one() -> None:
    """An outage must never silently disable the floor."""
    from app.guardrails.ingress import IngressGuard

    assessed = IngressGuard(_BrokenClassifier()).assess(HISTORICAL_CRISIS)
    assert assessed.category is CrisisCategory.OVERDOSE_MEDICAL

    quiet = IngressGuard(_BrokenClassifier()).assess("How much does treatment cost?")
    assert not quiet.is_crisis


def test_the_classifier_can_still_catch_what_the_patterns_miss() -> None:
    """The second stage earns its cost on paraphrase the regexes do not hold."""
    from app.guardrails.ingress import IngressGuard

    message = "honestly there is not much point in any of this anymore"
    assert not deterministic_scan(message).is_crisis

    classifier = _ScriptedClassifier(
        CrisisAssessment(category=CrisisCategory.SUICIDAL_IDEATION, matched=message)
    )
    assessed = IngressGuard(classifier).assess(message)

    assert classifier.calls == 1
    assert assessed.category is CrisisCategory.SUICIDAL_IDEATION
    assert assessed.detected_by == "classifier"


def test_a_quiet_turn_skips_the_classifier_when_it_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.guardrails.ingress import IngressGuard

    monkeypatch.setenv("ENABLE_CRISIS_CLASSIFIER", "false")
    get_settings.cache_clear()

    classifier = _ScriptedClassifier(CrisisAssessment())
    assert not IngressGuard(classifier).assess("Are dogs allowed?").is_crisis
    assert classifier.calls == 0
