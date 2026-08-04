"""Egress guardrail — semantic grounding verifier.

Covers what the deterministic checks structurally cannot: paraphrased claims with
no literal to match on. "Each room has its own thermostat" contains no number, no
URL and no phone number, but it is a fabrication — the knowledge base says rooms
have air conditioning and explicitly records that per-room control is unknown.

This is deliberately NOT a general-purpose LLM judge. It is a narrow entailment
check: given a small closed set of facts and one response, is every claim in the
response supported? That is the regime where model judges are actually reliable,
as opposed to open-ended "is this a good answer" scoring, which is not.

Cost control: the verifier only runs on claim-bearing turns. A reply that is
purely a question or an acknowledgement has nothing to verify, and paying for a
second model call on it would be waste.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.config import get_settings
from app.knowledge.retrieval import Fact, KnowledgeBase
from app.llm import LLMClient
from app.models import GuardVerdict, Violation


class _VerifierOutput(BaseModel):
    grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)


_SYSTEM = """You verify that a response from a facility's admissions assistant only \
states things supported by a given set of facts.

You will receive:
1. FACTS — everything the assistant was permitted to assert this turn.
2. TOOL RESULTS — live data returned to the assistant (dates, availability, confirmations).
3. RESPONSE — what the assistant said.

Flag a claim as unsupported ONLY if the response asserts something factual about the \
facility that is neither in FACTS nor in TOOL RESULTS.

Do NOT flag:
- questions, greetings, empathy, or acknowledgements
- offers to connect the person with a team member, or to have someone follow up
- statements that the assistant does not know something
- rephrasing of a fact in different words (this is expected and fine)
- general conversational filler carrying no factual claim about the facility
- reasonable inference directly entailed by a fact (if a fact says dogs under \
25 lbs are welcome, saying a golden retriever is too large is supported)

DO flag:
- specific prices, fees, numbers, dates, or contact details not present above
- amenities, services, policies, or care types not present above
- any claim that insurance coverage is confirmed or guaranteed
- invented staff names, credentials, accreditations, or outcome statistics
- committing to a specific tour time that does not appear in TOOL RESULTS

Quote the offending span for each unsupported claim. Be precise; false alarms are \
costly. When genuinely uncertain, treat the claim as grounded."""


# A sentence boundary good enough for deciding "did this assert anything".
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Pure acknowledgements, thanks and offers to help. These carry no factual claim
# about the facility, so verifying them is spend with nothing to find.
_ACKNOWLEDGEMENT = re.compile(
    r"^(?:"
    r"ok(?:ay)?|sure(?:\s+thing)?|great|perfect|understood|got\s+it"
    r"|thanks?|thank\s+you|you'?re\s+welcome|no\s+problem|of\s+course|absolutely"
    r"|happy\s+to\s+help|glad\s+to\s+help|let\s+me\s+help"
    r"|i'?m\s+(?:sorry|really\s+sorry|glad)"
    r")\b[^.!?]*[.!]?$",
    re.IGNORECASE,
)


def is_claim_bearing(response: str) -> bool:
    """Whether a response asserts anything worth verifying.

    Decided by FORM, not by length. The first implementation gated on word
    count — six words or fewer had to contain a digit to be verified — and that
    is backwards in exactly the cases this verifier exists for. Every one of

        "Each room has its own thermostat."   (this module's own example)
        "We accept Medicaid."                 (we do not)
        "Kosher meals are available."
        "All meals are chef-prepared."

    is a short, literal-free fabrication that the deterministic layer cannot see
    and that the length gate skipped outright, while a content-free "Happy to
    help with that. What else can I tell you?" was long enough to be billed for
    a verification call.

    The rule now: a turn is claim-bearing unless every sentence in it is either a
    question or a bare acknowledgement. Conservative by construction — when
    unsure, verify. An unnecessary check costs a fraction of a cent; a skipped
    one costs the guarantee.
    """
    text = " ".join(response.split())
    if not text:
        return False
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        if _ACKNOWLEDGEMENT.match(sentence):
            continue
        return True
    return False


class GroundingVerifier:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._settings = get_settings()

    def verify(
        self,
        response: str,
        *,
        kb: KnowledgeBase,
        facts: list[Fact],
        tool_results: list | None = None,
    ) -> GuardVerdict | None:
        """Returns None when verification was skipped (not run), so the trace can
        distinguish "checked and passed" from "not checked"."""
        if not self._settings.enable_grounding_verifier:
            return None
        if not is_claim_bearing(response):
            return None

        payload = (
            f"FACTS:\n{kb.render(facts)}\n\n"
            f"TOOL RESULTS:\n{tool_results if tool_results else '(none)'}\n\n"
            f"RESPONSE:\n{response}"
        )

        try:
            result = self._llm.structured(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": payload},
                ],
                _VerifierOutput,
                stage="grounding_verifier",
            )
        except Exception as exc:
            # Verifier unreachable. Warn rather than block: the deterministic
            # layer has already passed, and refusing to answer because a
            # secondary check is down would be a worse outage than a slightly
            # weaker guarantee. The trace records the degradation.
            return GuardVerdict(
                passed=True,
                violations=[
                    Violation(
                        code="verifier_unavailable",
                        detail=f"Grounding verifier failed to run: {exc}",
                        severity="warn",
                    )
                ],
            )

        if result is None:
            return GuardVerdict(
                passed=True,
                violations=[
                    Violation(
                        code="verifier_unparseable",
                        detail="Verifier returned no parseable verdict.",
                        severity="warn",
                    )
                ],
            )

        if result.grounded and not result.unsupported_claims:
            return GuardVerdict(passed=True)

        return GuardVerdict(
            passed=False,
            violations=[
                Violation(code="ungrounded_claim", detail=claim)
                for claim in result.unsupported_claims
            ]
            or [Violation(code="ungrounded_claim", detail="Response was not grounded.")],
        )


def repair_instruction(violations: list[Violation]) -> str:
    """Correction context for the repair attempt.

    Fed back as an additional system message rather than a rewritten prompt, so
    the model sees its own draft and what was wrong with it — which repairs more
    reliably than regenerating blind.
    """
    listed = "\n".join(f"- {v.detail}" for v in violations)
    return (
        "Your draft reply was rejected by the grounding check for these reasons:\n\n"
        f"{listed}\n\n"
        "Rewrite it. Remove or correct every flagged claim. If a detail is not in "
        "the facts you were given, do not state it — say you don't have that "
        "detail and offer to connect them with a team member. Keep the tone and "
        "keep it brief. Reply with the corrected message only."
    )
