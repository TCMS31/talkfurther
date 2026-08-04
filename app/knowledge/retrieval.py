"""Intent-keyed fact retrieval.

Deliberately not vector search. With ~50 facts and a known intent taxonomy, a
topic index is exact, deterministic, and debuggable — three properties a system
whose selling point is "no hallucination" should not trade away for recall it
does not need. The boundary at which this decision flips (multi-facility KBs,
free-text source documents) is documented in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings
from app.models import INTENT_TOPICS, Intent


@dataclass(frozen=True)
class Fact:
    id: str
    topics: tuple[str, ...]
    statement: str
    tokens: tuple[str, ...] = ()
    kind: str = "fact"


@dataclass
class KnowledgeBase:
    facility_name: str
    agent_name: str
    agent_role: str
    human_contact: str
    facts: dict[str, Fact]
    known_limitations: tuple[str, ...]
    _by_topic: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        index: dict[str, list[str]] = {}
        for fact in self.facts.values():
            for topic in fact.topics:
                index.setdefault(topic, []).append(fact.id)
        self._by_topic = index

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def for_intent(self, intent: Intent) -> list[Fact]:
        topics = INTENT_TOPICS.get(intent, ("general",))
        seen: set[str] = set()
        out: list[Fact] = []
        for topic in topics:
            for fact_id in self._by_topic.get(topic, []):
                if fact_id not in seen:
                    seen.add(fact_id)
                    out.append(self.facts[fact_id])
        return out

    def by_topics(self, topics: tuple[str, ...]) -> list[Fact]:
        seen: set[str] = set()
        out: list[Fact] = []
        for topic in topics:
            for fact_id in self._by_topic.get(topic, []):
                if fact_id not in seen:
                    seen.add(fact_id)
                    out.append(self.facts[fact_id])
        return out

    # ── Grounding support ─────────────────────────────────────────────────────

    def allowed_tokens(self, facts: list[Fact]) -> set[str]:
        """Literals (numbers, URLs, phone numbers) assertable given these facts.

        The deterministic egress check treats anything outside this set as a
        probable fabrication. Numbers embedded in statements are harvested
        automatically, so the KB author only declares tokens needing alternate
        formatting (e.g. "30000" alongside "30,000").

        Values are normalized — "$3,500." and "3500" collapse to the same key —
        so formatting differences never register as hallucinations.
        """
        allowed: set[str] = set()
        for fact in facts:
            for token in fact.tokens:
                allowed.add(normalize_token(token))
            for match in _NUMERIC.findall(fact.statement):
                allowed.add(normalize_token(match))
            for match in _URL.findall(fact.statement):
                allowed.add(normalize_token(match))
        return {t for t in allowed if t}

    def render(self, facts: list[Fact]) -> str:
        """Render facts for prompt injection, tagged with their IDs.

        IDs are included so the grounding verifier and the trace can refer to the
        same identifiers the response was generated from.
        """
        if not facts:
            return (
                "(No facts retrieved for this turn. You do not have information "
                "on this topic — say so and offer to connect them with the team.)"
            )
        lines = [f"- [{f.id}] {' '.join(f.statement.split())}" for f in facts]
        return "\n".join(lines)


_NUMERIC = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
_URL = re.compile(r"https?://[^\s,;)\]]+")
_TRAILING_PUNCT = ".,;:!?)]}\"'"


def normalize_token(token: str) -> str:
    """Canonical form for grounding comparison.

    Shared by the KB (building the allowlist) and the egress guard (checking a
    response against it), so the two can never drift apart.
    """
    token = token.strip().lower().rstrip(_TRAILING_PUNCT)
    if token.startswith(("http://", "https://")):
        return token.rstrip("/")
    # Numbers: strip currency/separators so "$3,500." == "3500".
    stripped = token.lstrip("$").replace(",", "")
    if stripped.endswith(".0") or stripped.endswith(".00"):
        stripped = stripped.split(".")[0]
    return stripped or token


def _load(path: Path) -> KnowledgeBase:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    identity = raw.get("identity", {})

    facts: dict[str, Fact] = {}
    for entry in raw.get("facts", []):
        fact = Fact(
            id=entry["id"],
            topics=tuple(entry.get("topics", ())),
            statement=" ".join(str(entry["statement"]).split()),
            tokens=tuple(str(t) for t in entry.get("tokens", ())),
            kind=entry.get("kind", "fact"),
        )
        if fact.id in facts:
            raise ValueError(f"duplicate fact id in knowledge base: {fact.id}")
        facts[fact.id] = fact

    return KnowledgeBase(
        facility_name=identity.get("facility_name", "the community"),
        agent_name=identity.get("agent_name", "Sophie"),
        agent_role=identity.get("agent_role", "virtual admissions assistant"),
        human_contact=identity.get("human_contact", "our admissions team"),
        facts=facts,
        known_limitations=tuple(raw.get("known_limitations", ())),
    )


@lru_cache(maxsize=1)
def get_kb() -> KnowledgeBase:
    return _load(get_settings().knowledge_path)
