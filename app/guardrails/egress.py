"""Egress guardrail — deterministic checks.

Always on, costs roughly nothing, runs before the LLM verifier. This is the floor
of the "no hallucination" guarantee; the semantic verifier in grounding.py sits on
top of it.

Design note on the numeric check. Flagging *every* digit not present in the
knowledge base produces unusable noise — "a couple of options", "2 things to
consider", "one moment". So the check is targeted at the classes where a
fabricated number actually harms someone:

  - currency amounts        wrong price quoted to a family deciding on care
  - numbers >= 100          capacity, phone numbers, distances, invented statistics
  - URLs                    a link that goes somewhere we did not vet
  - phone-shaped strings     the single worst thing to get wrong

Bare small integers are left alone deliberately. That is a real coverage gap, and
the semantic verifier is what covers it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.guardrails.responses import REQUIRED_CRISIS_TOKENS
from app.knowledge.retrieval import Fact, KnowledgeBase, normalize_token
from app.models import CrisisAssessment, GuardVerdict, Violation

# Numbers that are always safe to say regardless of the knowledge base.
_UNIVERSAL_TOKENS = {
    "988", "911", "1-800-662-4357", "18006624357",
    "1-800-222-1222", "18002221222", "662-4357", "222-1222",
    "24", "24/7", "12", "100",
}

_CURRENCY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
# The lookbehind must exclude `,` and `.` as well as digits, or the tail of a
# grounded amount re-matches on its own ("$30,000" would also yield "000").
_LARGE_NUMBER = re.compile(r"(?<![\d$/:,.-])\d[\d,]{2,}(?:\.\d+)?(?![\d/:-])")
_URL = re.compile(r"https?://[^\s,;)\]]+|(?<![\w@.])(?:www\.)[^\s,;)\]]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")

# Clinical guidance an admissions assistant must never give.
_MEDICAL_ADVICE = re.compile(
    r"\b(?:"
    r"you\s+should\s+(?:stop|quit|reduce|increase|keep)\s+(?:taking|using)"
    r"|(?:taper|wean)\s+(?:off|down|yourself)"
    r"|\d+\s*mg\b|\bdosage\b|\bdose\s+of\b"
    r"|(?:safe|okay|fine)\s+to\s+(?:stop|detox)\s+(?:at\s+home|on\s+your\s+own|alone)"
    r"|you\s+(?:don'?t|do\s+not)\s+need\s+(?:medical|a\s+doctor)"
    r"|(?:diagnos|prescrib)(?:e|ed|ing)\s+you"
    r")",
    re.IGNORECASE,
)

# Voice-channel artifacts. The baseline was a phone script deployed as a chat
# agent; these are the specific leaks it produced.
_VOICE_ARTIFACT = re.compile(
    # A bracketed stage direction — "[Pause for 10 seconds]". Deliberately OUTSIDE
    # the \b-anchored group below: `\b` cannot match before `[`, so an alternative
    # starting with a bracket is unreachable in there.
    r"\[[^\]]*pause[^\]]*\]"
    r"|\b(?:"
    r"press\s+(?:0|zero|one|1)\b"
    r"|(?:please\s+)?hold\s+(?:on\s+)?(?:for\s+a|while|please)"
    r"|didn'?t\s+catch\s+that|coming\s+through\s+choppy|cutting\s+in\s+and\s+out"
    r"|static\s+in\s+your|leave\s+a\s+voicemail"
    # An explicit duration — NOT the bare word. "Let me pause there" and "a pause
    # between assessment and admission" are ordinary English, and blocking them
    # burns a repair attempt on a perfectly good response.
    r"|\d+[\s-]?second\s+pause"
    # The baseline's stalling theater. The subject varies ("let me check",
    # "I'll check", "our director is not available"), so anchor on the director
    # rather than on who is speaking — an "I"-anchored pattern missed the
    # baseline's own phrasing, which is the exact string this exists to catch.
    r"|check\s+(?:if\s+)?(?:my|the|our)\s+(?:director|admissions\s+director)"
    r"|(?:my|the|our)\s+(?:admissions\s+)?director(?:\s+of\s+admissions)?\s+"
    r"(?:is\s+)?(?:not\s+)?(?:currently\s+)?(?:available|unavailable)"
    r")",
    re.IGNORECASE,
)

_GREETING = re.compile(
    r"(?:^|\n)\s*(?:hi|hello|hey)\b[^.!?\n]{0,60}"
    r"(?:this\s+is\s+further|my\s+name\s+is\s+sophie)",
    re.IGNORECASE,
)

# Claims of confirmed insurance coverage. We can never know this.
#
# Each alternative is preceded by a negative lookbehind for hedging context, so
# the guard does not block the sentence the policy actually wants:
# "I can't tell you whether you're covered — a team member has to verify."
# Without it, the compliant answer is the one that gets rejected.
_HEDGE = (
    r"(?<!can't\s)(?<!cannot\s)(?<!can not\s)(?<!whether\s)(?<!if\s)"
    r"(?<!not\s)(?<!unsure\s)(?<!know\s)(?<!confirm\s)(?<!verify\s)"
)
_COVERAGE_CLAIM = re.compile(
    r"\b(?:"
    rf"your\s+(?:insurance|plan|policy)\s+{_HEDGE}(?:will|does|should)\s+cover"
    rf"|{_HEDGE}(?:you'?re|you\s+are)\s+covered\b"
    rf"|(?:we|it)\s+{_HEDGE}(?:will|would)\s+be\s+(?:fully\s+)?covered"
    rf"|(?:insurance|they)\s+{_HEDGE}(?:will|should)\s+(?:pay|cover)\s+(?:for\s+)?(?:it|this|everything|all)"
    r")",
    re.IGNORECASE,
)


def _tokens_from_tool_results(tool_results: list[Any]) -> set[str]:
    """Values returned by tools are assertable.

    Without this, every tool-sourced date, time and confirmation number would be
    flagged as fabricated — the tools exist precisely so the agent can state
    things it could not otherwise know.
    """
    blob = json.dumps(tool_results, default=str)
    out: set[str] = set()
    for pattern in (_CURRENCY, _LARGE_NUMBER, _URL, _PHONE):
        for match in pattern.findall(blob):
            out.add(normalize_token(match))
    # Times and day numbers appear as ISO timestamps and human strings; harvest
    # every standalone integer from tool output so "March 17 at 11:00 AM" passes.
    for match in re.findall(r"\d+", blob):
        out.add(normalize_token(match))
    return out


def check(
    response: str,
    *,
    kb: KnowledgeBase,
    facts: list[Fact],
    tool_results: list[Any] | None = None,
    crisis: CrisisAssessment | None = None,
    already_greeted: bool = False,
) -> GuardVerdict:
    violations: list[Violation] = []

    allowed = kb.allowed_tokens(facts) | _UNIVERSAL_TOKENS
    allowed |= _tokens_from_tool_results(tool_results or [])

    # ── Fabricated literals ───────────────────────────────────────────────────
    for match in _CURRENCY.findall(response):
        if normalize_token(match) not in allowed:
            violations.append(
                Violation(
                    code="ungrounded_currency",
                    detail=f"Quoted {match!r}, which is not in the retrieved facts.",
                )
            )

    for match in _LARGE_NUMBER.findall(response):
        if normalize_token(match) not in allowed:
            violations.append(
                Violation(
                    code="ungrounded_number",
                    detail=f"Stated the number {match!r}, which is not in the retrieved facts.",
                )
            )

    for match in _URL.findall(response):
        if normalize_token(match) not in allowed:
            violations.append(
                Violation(
                    code="ungrounded_url",
                    detail=f"Gave the link {match!r}, which is not in the retrieved facts.",
                )
            )

    for match in _PHONE.findall(response):
        if normalize_token(match) not in allowed:
            violations.append(
                Violation(
                    code="ungrounded_phone",
                    detail=f"Gave the phone number {match!r}, which is not in the retrieved facts.",
                )
            )

    # ── Scope violations ──────────────────────────────────────────────────────
    if match := _MEDICAL_ADVICE.search(response):
        violations.append(
            Violation(
                code="medical_advice",
                detail=f"Gave clinical guidance ({match.group(0)!r}). Admissions must not advise on treatment.",
            )
        )

    if match := _COVERAGE_CLAIM.search(response):
        violations.append(
            Violation(
                code="coverage_claim",
                detail=(
                    f"Implied coverage is confirmed ({match.group(0)!r}). Benefits "
                    "can only be verified by a team member."
                ),
            )
        )

    # ── Channel and flow ──────────────────────────────────────────────────────
    if match := _VOICE_ARTIFACT.search(response):
        violations.append(
            Violation(
                code="voice_artifact",
                detail=f"Used voice-channel phrasing ({match.group(0)!r}). This is a text chat.",
            )
        )

    if already_greeted and _GREETING.search(response):
        violations.append(
            Violation(
                code="repeat_greeting",
                detail="Greeted again after the opening turn.",
                severity="warn",
            )
        )

    # ── Crisis obligations ────────────────────────────────────────────────────
    if crisis is not None and crisis.is_crisis:
        required = REQUIRED_CRISIS_TOKENS.get(crisis.category, ())
        for token in required:
            if token not in response:
                violations.append(
                    Violation(
                        code="missing_crisis_resource",
                        detail=f"Crisis reply must include {token}.",
                    )
                )

    return GuardVerdict(
        passed=not any(v.severity == "block" for v in violations),
        violations=violations,
    )
