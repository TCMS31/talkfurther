"""Context assembly — the context-engineering layer.

The prompt is built in three tiers ordered by volatility:

  Tier 1  Core persona, style, safety policy      stable across all turns
  Tier 2  Session state (what we already know)    changes slowly
  Tier 3  Retrieved facts for this intent         changes every turn

Ordering matters for two reasons. Prefix stability is what makes prompt caching
effective, and it keeps the expensive-to-review policy text in one auditable file
rather than interleaved with per-turn data.

The substantive change from the baseline: the baseline shipped its entire fact
sheet on every turn (~150 lines, internally contradictory). Here a turn carries
only the facts its routed intent needs — typically 5-10 — which shrinks the
contradiction surface, cuts cost, and gives the egress guardrail a precise set of
IDs to check the response against.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.knowledge.retrieval import Fact, KnowledgeBase
from app.models import Intent
from app.session import Session

_SUBJECT_GUIDANCE = {
    "self": (
        "This person is seeking care for THEMSELVES. Address them directly as "
        '"you" throughout.'
    ),
    "other": (
        "This person is seeking care for SOMEONE ELSE. Refer to that person as "
        '"they/them" — never assume gender — and address the sender as "you".'
    ),
    "unclear": (
        "It is not yet clear whether they are seeking care for themselves or "
        "someone else. Use neutral phrasing until you know; if it becomes "
        "relevant, ask."
    ),
}


@lru_cache(maxsize=1)
def core_prompt() -> str:
    return get_settings().core_prompt_path.read_text(encoding="utf-8")


def build_system_prompt(
    session: Session,
    intent: Intent,
    facts: list[Fact],
    kb: KnowledgeBase,
) -> str:
    settings = get_settings()
    now = settings.now()

    # ── Tier 1: stable ────────────────────────────────────────────────────────
    parts = [core_prompt()]

    # ── Tier 2: session state ─────────────────────────────────────────────────
    state_lines = [
        f"Current date and time: {now.strftime('%A, %B %d, %Y at %-I:%M %p')} "
        f"({settings.facility_timezone}).",
        "Never calculate dates yourself — use the availability tool.",
        "",
        _SUBJECT_GUIDANCE[session.subject],
    ]

    if session.greeted:
        state_lines.append(
            "You have ALREADY greeted this person. Do not greet again — reply "
            "directly to what they just said."
        )
    else:
        state_lines.append("This is the first turn. Open with the standard greeting.")

    if session.disclosed:
        state_lines.append(
            "The recording disclosure has already been given. Do not repeat it."
        )
    else:
        state_lines.append(
            "Include the brief recording disclosure once in this reply, after the "
            "substance of your answer."
        )

    known = session.known_slots()
    if known:
        rendered = ", ".join(f"{k}={v!r}" for k, v in known.items())
        state_lines.append(
            f"Already provided by this person — do NOT ask for these again: {rendered}."
        )

    if session.booked_tours:
        booked = "; ".join(t.get("human", "") for t in session.booked_tours)
        state_lines.append(f"Tour(s) already confirmed: {booked}.")

    if session.crisis_flagged:
        state_lines.append(
            "IMPORTANT: this person disclosed a crisis earlier in this "
            "conversation. Stay gentle and unhurried. Do not push scheduling or "
            "pricing. Following their lead is more important than advancing the "
            "conversation."
        )

    parts.append("# Current state\n\n" + "\n".join(f"- {line}" if line else "" for line in state_lines))

    # ── Tier 3: retrieved facts ───────────────────────────────────────────────
    parts.append(
        "# <facts>\n\n"
        f"Topic: {intent.value}\n\n"
        f"{kb.render(facts)}\n\n"
        "</facts>\n\n"
        "These are the ONLY facts you may assert this turn. Anything not listed "
        "above, you do not know."
    )

    if kb.known_limitations:
        limits = "\n".join(f"- {item}" for item in kb.known_limitations)
        parts.append(
            "# Known gaps\n\n"
            "We hold no information on the following. If asked, say so plainly "
            "and offer to connect them with the team:\n\n" + limits
        )

    return "\n\n---\n\n".join(parts)


def build_messages(
    session: Session,
    intent: Intent,
    facts: list[Fact],
    kb: KnowledgeBase,
    history_limit: int = 16,
) -> list[dict[str, str]]:
    """Assemble the full message list.

    Expects the current user message to already be appended to the session, so
    the transcript is the single source of conversational order and the turn's
    message cannot appear twice.
    """
    system = build_system_prompt(session, intent, facts, kb)
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(session.transcript(limit=history_limit))
    return messages
