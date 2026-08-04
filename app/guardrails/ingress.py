"""Ingress guardrail — crisis detection.

Two stages, deliberately redundant:

  1. Deterministic pattern match. Microseconds, no cost, cannot be prompt-injected,
     and still works when the model API is down. This is the floor.
  2. Cheap LLM classifier. Catches paraphrase and indirect disclosure the pattern
     list will never enumerate.

Either firing routes to a fixed template (guardrails/responses.py); the agent
model never generates crisis text.

Tuned for RECALL, not precision. A false positive costs one overly-cautious
message that a person can simply talk past. A false negative — an overdose
disclosure answered with a lead-capture form, which is the baseline prompt's
literal specified behaviour — is unbounded. The asymmetry is a design input, and
the eval suite treats crisis recall as a pass/fail gate rather than a score.
"""

from __future__ import annotations

import re

from app.config import get_settings
from app.llm import LLMClient
from app.models import CrisisAssessment, CrisisCategory

# ── Stage 1: deterministic patterns ──────────────────────────────────────────
# Ordered by severity: the first match wins, so an explicit overdose beats a
# generic distress phrase appearing in the same message.
#
# Written to tolerate the way people actually type — contractions, missing
# apostrophes, "wanna", "dont". Not exhaustive by design; stage 2 covers the rest.

_PATTERNS: list[tuple[CrisisCategory, str]] = [
    (
        CrisisCategory.OVERDOSE_MEDICAL,
        r"\b(?:"
        r"took\s+(?:too\s+many|a\s+bunch\s+of|a\s+lot\s+of|way\s+too\s+many)"
        r"|overdos(?:e|ed|ing)|od(?:'?d|ed)\b"
        r"|swallowed\s+(?:all|a\s+bunch|the\s+whole)"
        r"|can'?t\s+(?:breathe|stay\s+awake|feel\s+my)"
        r"|(?:chest|stomach)\s+(?:pain|hurts)\s+(?:bad|really|so)"
        r")",
    ),
    (
        CrisisCategory.SUICIDAL_IDEATION,
        r"\b(?:"
        r"(?:don'?t|dont|do\s+not)\s+(?:want|wanna)\s+to\s+(?:live|be\s+here|exist|wake\s+up)"
        r"|(?:want|wanna|going)\s+to\s+(?:die|kill\s+myself|end\s+(?:it|my\s+life))"
        r"|kill(?:ing)?\s+myself|end(?:ing)?\s+my\s+life|take\s+my\s+own\s+life"
        r"|suicid(?:e|al)|better\s+off\s+dead"
        r"|no\s+(?:reason|point)\s+(?:to\s+)?(?:living|go\s+on|be\s+here)"
        r"|(?:everyone|they)'?d?\s+be\s+better\s+off\s+without\s+me"
        r")",
    ),
    (
        CrisisCategory.SELF_HARM,
        r"\b(?:"
        r"(?:hurt|harm|cut|cutting)\s+myself|self[\s-]?harm"
        r"|(?:want|going)\s+to\s+hurt\s+myself"
        r")",
    ),
    (
        CrisisCategory.WITHDRAWAL_RISK,
        r"\b(?:"
        r"withdraw(?:al|als|ing)|dts\b|delirium\s+tremens"
        r"|(?:the\s+)?shakes\s+(?:are|from)"
        r"|detox(?:ing)?\s+(?:alone|at\s+home|by\s+myself)"
        r"|seizure|seizing"
        r")",
    ),
    (
        CrisisCategory.THIRD_PARTY_DANGER,
        r"\b(?:"
        r"(?:hurt|kill|harm)\s+(?:someone|somebody|him|her|them|people)"
        r"|(?:he|she|they)\s+(?:is|are|might)\s+going\s+to\s+(?:die|overdose)"
        r"|(?:threaten(?:ed|ing)?)\s+to\s+(?:kill|hurt)"
        r")",
    ),
]

_COMPILED = [(cat, re.compile(pat, re.IGNORECASE)) for cat, pat in _PATTERNS]

# Past-tense recovery narrative. "I overdosed two years ago but I'm clean now" is
# someone sharing history, not someone in danger, and answering it with "call 911"
# is both wrong and alienating to exactly the person we most want to keep talking.
#
# This does NOT suppress the match — it defers arbitration to the classifier,
# which can read tense. If the classifier is disabled or unreachable, the
# deterministic hit still stands. The floor never drops.
_HISTORICAL = re.compile(
    r"\b(?:"
    r"\d+\s+(?:years?|months?|weeks?|days?)\s+ago"
    r"|(?:years?|months?|weeks?)\s+ago"
    r"|back\s+(?:in|when)|used\s+to\b|in\s+the\s+past"
    r"|(?:am|i'?m|been)\s+(?:clean|sober)\s+(?:now|for)"
    r"|(?:clean|sober)\s+(?:now|since|for\s+\d)"
    r"|(?:have|had)\s+been\s+(?:clean|sober|in\s+recovery)"
    r"|in\s+recovery\s+(?:now|since|for)"
    r")",
    re.IGNORECASE,
)


def deterministic_scan(message: str) -> CrisisAssessment:
    """Stage 1. Cannot be disabled — this is the floor that survives an outage."""
    for category, pattern in _COMPILED:
        match = pattern.search(message)
        if match:
            return CrisisAssessment(
                category=category,
                detected_by="deterministic",
                matched=match.group(0)[:120],
            )
    return CrisisAssessment()


def looks_historical(message: str) -> bool:
    return bool(_HISTORICAL.search(message))


# ── Stage 2: classifier ──────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = """You are a safety classifier for a behavioral health admissions chat.

Read the user's latest message (with prior context) and decide whether it indicates
an active crisis requiring immediate escalation.

Categories:
- suicidal_ideation: wanting to die, not wanting to live, planning suicide, hopelessness framed as wanting to stop existing
- self_harm: intent or act of hurting themselves
- overdose_medical_emergency: has taken a dangerous amount of a substance, or describes acute medical symptoms
- withdrawal_risk: currently experiencing or about to experience substance withdrawal (alcohol and benzodiazepine withdrawal can be fatal)
- third_party_danger: someone else is in immediate danger, or the user may harm someone
- none: no immediate crisis

Guidance:
- Within each category, ERR TOWARD FLAGGING. Missing a real crisis is far worse
  than an unnecessary flag.
- But the categories have entry requirements. Meeting the bar is not optional —
  read the rule for `suicidal_ideation` below before using it.

RULE for suicidal_ideation — you must be able to quote language expressing a wish
to die, to not exist, to not wake up, to end one's life, to be better off dead, or
a plan to act on any of that.

Despair about RECOVERY does not meet that bar. Relapsing, feeling stuck, shame,
exhaustion, "no end in sight", "nothing works", "I've tried everything" — these are
what active addiction sounds like, and they are the normal register of this
conversation, not an emergency.

This distinction is load-bearing. Someone saying they keep relapsing is REACHING
OUT FOR TREATMENT. Answering them with a crisis hotline instead of help pushes
away the exact person this service exists to reach. That is a real harm, not a
harmless precaution.

  none              "I keep relapsing, it seems like there is no end in sight"
  none              "I've been struggling for years and nothing works"
  none              "I'm exhausted, I don't know how much more I can take"
  suicidal_ideation "I keep relapsing and I don't want to be here anymore"
  suicidal_ideation "there's no end in sight, I'd be better off dead"

The first three express despair. The last two express a wish not to live. Only the
last two clear the bar.

RULE for third_party_danger — requires danger that is IMMEDIATE: someone is
overdosing right now, is threatening violence, or is in acute medical crisis.

A family member describing someone's ongoing drinking or drug use does NOT meet
that bar, however painful the description. That is the single most common way a
lead arrives at this service — a relative finally asking for help. Treating it as
an emergency abandons them at the exact moment they reached out.

  none                "He is always drunk, even for his daughter's graduation"
  none                "My son has been using for years and I don't know what to do"
  none                "She drinks every night and it's destroying the family"
  third_party_danger  "He's passed out and I can't wake him up"
  third_party_danger  "He said he's going to hurt himself tonight"

The first three describe chronic addiction — the reason this service exists. The
last two describe someone in danger right now.

Other categories:
- Someone asking about detox services in general is NOT withdrawal_risk. Someone
  saying they are currently withdrawing IS.
- Past-tense recovery narrative ("I overdosed two years ago, I'm clean now") is not
  an active crisis."""


class IngressGuard:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._settings = get_settings()

    def assess(self, message: str, history: str = "") -> CrisisAssessment:
        found = deterministic_scan(message)
        classifier_on = self._settings.enable_crisis_classifier

        if found.is_crisis:
            # A deterministic hit is final and not overridable by a model — that
            # is what keeps the floor intact under adversarial input. The single
            # exception is a past-tense recovery narrative, where we let the
            # classifier arbitrate because tense is exactly what patterns cannot
            # read. If the classifier is off, the hit stands.
            if not (classifier_on and looks_historical(message)):
                return found

        elif not classifier_on:
            return CrisisAssessment()

        try:
            result = self._llm.structured(
                [
                    {"role": "system", "content": _CLASSIFIER_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Recent conversation:\n{history or '(none)'}\n\n"
                            f"Latest message:\n{message}"
                        ),
                    },
                ],
                CrisisAssessment,
                stage="crisis_classifier",
            )
        except Exception:
            # Classifier unavailable. Fall back to whatever stage 1 concluded —
            # never to "no crisis", or an outage would silently disable the floor.
            return found

        if result is None:
            # No parseable verdict — a refusal or a schema failure, NOT a judgment
            # that the message is safe. Treat it exactly like the exception path
            # and fall back to stage 1, or an unparseable response would silently
            # clear a deterministic overdose hit.
            return found

        if not result.is_crisis:
            # Only the historical-deferral path can reach here with a stage-1 hit.
            # The classifier read the tense and cleared it; that is the one case
            # where a model is permitted to downgrade a deterministic match.
            return CrisisAssessment()

        return CrisisAssessment(
            category=result.category,
            detected_by="classifier",
            matched=result.matched[:120],
        )
