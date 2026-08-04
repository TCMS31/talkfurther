"""Intent router.

Replaces the baseline's Step 4, which was a dispatch table written in prose with
edges pointing at steps that were never defined (Step 7 for insurance, Step 9 for
images). Classifying into a closed enum makes dangling routes structurally
impossible and turns the routing decision into a logged, evaluable field.

Also resolves whether care is being sought for the user or a third party. The
baseline specified pronoun handling but had no mechanism to persist the answer
across turns, so it degraded as the conversation grew.
"""

from __future__ import annotations

from app.llm import LLMClient
from app.models import Intent, RouteDecision
from app.session import Session

_SYSTEM = """You classify messages sent to a behavioral health facility's admissions chat.

Return the single best intent:

- pricing: cost, fees, what's included in the price, affordability
- insurance: coverage, Medicaid, VA benefits, payment assistance, verifying benefits
- amenities: rooms, dining, food, activities, outdoor areas, fitness, accessibility, facilities
- care_types: what conditions/addictions are treated, levels of care, mental health services, clinical scope
- policy: pets, smoking, visitors, guests, transportation, parking, rules
- tour_scheduling: visiting, touring, booking or changing an appointment, availability, dates and times
- assessment: wanting to enter treatment, describing substance use, seeking help for themselves or someone else
- careers: jobs, hiring, employment, applications
- contact: phone number, address, directions, how to reach the facility
- brochure: requesting materials be sent, mailing information
- human_handoff: asking for a person, expressing frustration, asking if this is a bot
- smalltalk: greetings, thanks, acknowledgements with no informational request
- unknown: anything else, or a topic an admissions assistant would not hold information on

Also determine who care is for:
- self: the sender is seeking care for themselves
- other: the sender is asking on behalf of someone else (parent, partner, child, friend)
- unclear: not determinable from this message

Use the conversation history for context. If a message is a short reply like "yes"
or "Tuesday at 2pm", classify by what it is responding to.

Prefer a specific intent over `unknown`. Reserve `unknown` for topics genuinely
outside an admissions assistant's scope."""


class IntentRouter:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def route(self, session: Session, message: str) -> RouteDecision:
        history = session.transcript(limit=8)
        context = "\n".join(f"{m['role']}: {m['content']}" for m in history)

        user_block = (
            f"Conversation so far:\n{context or '(none)'}\n\n"
            f"Classify this message:\n{message}"
        )

        try:
            decision = self._llm.structured(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user_block},
                ],
                RouteDecision,
                stage="router",
            )
        except Exception:
            # Routing is not worth failing a turn over. UNKNOWN retrieves general
            # + contact facts, which is the honest-handoff path — degraded, but
            # still a safe answer rather than a 500.
            decision = None

        if decision is None:
            # Fail safe: an unroutable turn goes down the honest "I don't have
            # that, let me connect you" path rather than guessing a topic.
            return RouteDecision(intent=Intent.UNKNOWN, subject=session.subject)

        # Subject is sticky. Once established, a later ambiguous message must not
        # silently flip the agent back to the wrong pronouns.
        if decision.subject == "unclear" and session.subject != "unclear":
            decision.subject = session.subject

        return decision
