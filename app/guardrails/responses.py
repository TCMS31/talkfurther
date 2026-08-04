"""Crisis response templates.

These are hand-written, version-controlled, and returned verbatim. The generative
model is bypassed entirely for crisis turns.

Rationale: a language model's average-case output on these topics is decent, but
the failure distribution has a tail we cannot accept and cannot bound. Sampling
means the response is never quite the reviewed one. For the small set of turns
where a wrong answer could contribute to someone's death, fixed text reviewed by
a human is strictly better than good text generated fresh each time.

The tradeoff is real and worth stating plainly: these replies are less
personalised than a generated response would be. That is the price, and for this
category it is worth paying.

Any change here should be reviewed by a clinician before shipping. The resource
numbers below are US-specific and would need localising for other markets.
"""

from __future__ import annotations

from app.models import CrisisCategory

# US national resources.
LIFELINE = "988 (call or text the Suicide & Crisis Lifeline)"
EMERGENCY = "911"
SAMHSA = "1-800-662-4357 (SAMHSA's free, confidential helpline, 24/7)"

# Tokens the deterministic egress check requires to be present in a crisis reply.
REQUIRED_CRISIS_TOKENS: dict[CrisisCategory, tuple[str, ...]] = {
    CrisisCategory.SUICIDAL_IDEATION: ("988",),
    CrisisCategory.SELF_HARM: ("988",),
    CrisisCategory.OVERDOSE_MEDICAL: ("911",),
    CrisisCategory.WITHDRAWAL_RISK: ("911",),
    CrisisCategory.THIRD_PARTY_DANGER: ("911",),
}


_RESPONSES: dict[CrisisCategory, str] = {
    CrisisCategory.SUICIDAL_IDEATION: (
        "I'm really glad you told me that, and I want you to know I'm taking it "
        "seriously.\n\n"
        "I'm not able to give you the kind of support you deserve right now, but "
        f"someone can, right away. Please reach out to {LIFELINE}. They're "
        "available 24/7, it's free, and you can talk to a real person who is "
        "trained for exactly this.\n\n"
        f"If you're in immediate danger, please call {EMERGENCY} or go to your "
        "nearest emergency room.\n\n"
        "I'm still here if you want to keep talking."
    ),
    CrisisCategory.SELF_HARM: (
        "Thank you for telling me — that took something to say.\n\n"
        f"I want to make sure you're safe. Please reach out to {LIFELINE}. It's "
        "free, available around the clock, and staffed by people trained to help "
        "with exactly what you're going through.\n\n"
        f"If you're hurt or in immediate danger, please call {EMERGENCY} or get "
        "to an emergency room.\n\n"
        "I'm here if you want to keep talking."
    ),
    CrisisCategory.OVERDOSE_MEDICAL: (
        "This sounds like a medical emergency, and I want you to get help right "
        "now.\n\n"
        f"Please call {EMERGENCY} immediately, or have someone nearby call for "
        "you. If you can, don't be alone — stay with someone until help arrives.\n\n"
        "You can also reach Poison Control at 1-800-222-1222 for immediate "
        "guidance.\n\n"
        "Please make that call now. Everything else can wait."
    ),
    CrisisCategory.WITHDRAWAL_RISK: (
        "I'm glad you reached out — withdrawal can be genuinely dangerous, "
        "especially with alcohol or benzodiazepines, and it isn't something to "
        "get through alone.\n\n"
        f"If you're having seizures, chest pain, severe confusion, or "
        f"hallucinations, please call {EMERGENCY} right now.\n\n"
        f"Otherwise, please get in front of a medical professional today — an "
        f"urgent care, an ER, or {SAMHSA}, which can point you to detox support "
        "near you.\n\n"
        "I can also have someone from our team reach out to you directly. Would "
        "that help?"
    ),
    CrisisCategory.THIRD_PARTY_DANGER: (
        "That sounds frightening, and I'm sorry you're carrying it.\n\n"
        f"If anyone is in immediate danger, please call {EMERGENCY} right away.\n\n"
        f"For support with what you're describing, {SAMHSA} can help — it's free, "
        "confidential, and available 24/7, including for family members trying to "
        "help someone else.\n\n"
        "I'm here if you want to talk through options once things are safe."
    ),
}

_FALLBACK = (
    "I'm concerned about what you've shared, and I want to make sure you get "
    f"real support. Please reach out to {LIFELINE}, or call {EMERGENCY} if "
    "anyone is in immediate danger.\n\n"
    "I'm here if you'd like to keep talking."
)


def crisis_response(category: CrisisCategory) -> str:
    return _RESPONSES.get(category, _FALLBACK)


# Used when the egress guardrail cannot certify a generated response after the
# repair budget is exhausted. Failing to a handoff is the correct terminal state:
# a second failure implies the knowledge base lacks the fact, which is not
# something another retry can fix.
GROUNDING_FALLBACK = (
    "I want to make sure I get this right, and I don't want to tell you "
    "something I'm not certain of. Let me have someone from our team follow up "
    "with you directly — could I get the best number or email to reach you?"
)
