"""Conversation session state.

Behind an interface so the in-memory implementation can be swapped for Redis or
Postgres without touching the pipeline. That boundary is drawn deliberately: the
demo does not need durable sessions, but pretending the seam does not exist would
make it expensive to add later.

The slot tracking here is what lets the agent honour "don't re-ask for something
already given" — the baseline stated that rule but gave the model nothing but the
raw transcript to enforce it against.
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from app.models import Assessment, InsuranceCheck, Intent, Lead


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class Session:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    turn_index: int = 0

    # Whether the opening greeting / recording disclosure have been delivered.
    # Tracked in state rather than left to the model, because "greet exactly once"
    # is a property the model has no reliable way to verify about itself.
    greeted: bool = False
    disclosed: bool = False

    # Who care is being sought for. Drives pronoun selection.
    subject: Literal["self", "other", "unclear"] = "unclear"

    # Slots gathered so far, keyed by field name.
    slots: dict[str, Any] = field(default_factory=dict)

    # Records emitted by tools during the conversation.
    lead: Lead | None = None
    insurance: InsuranceCheck | None = None
    assessment: Assessment | None = None
    booked_tours: list[dict[str, Any]] = field(default_factory=list)

    # Set once a crisis has been detected. The pipeline uses this to keep the
    # tone appropriate for the rest of the conversation rather than snapping
    # back to sales cadence on the next turn.
    crisis_flagged: bool = False
    escalated: bool = False

    last_intent: Intent | None = None

    def add_user(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self.messages.append(Message(role="assistant", content=content))

    def transcript(self, limit: int = 20) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in self.messages[-limit:]]

    def known_slots(self) -> dict[str, Any]:
        return {k: v for k, v in self.slots.items() if v not in (None, "")}

    def remember(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if value not in (None, ""):
                self.slots[key] = value


class SessionStore(ABC):
    @abstractmethod
    def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    def save(self, session: Session) -> None: ...

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
        session = Session(session_id=session_id or uuid.uuid4().hex[:12])
        self.save(session)
        return session


class InMemorySessionStore(SessionStore):
    """Process-local. Fine for a demo; explicitly not durable or multi-worker safe.

    The dict is guarded because FastAPI runs sync endpoints on a threadpool.
    That protects the container only — the pipeline holds a per-session lock for
    the duration of a turn, which is what makes read-modify-write on a Session
    safe.
    """

    def __init__(self) -> None:
        self._data: dict[str, Session] = {}
        self._guard = threading.Lock()

    def get(self, session_id: str) -> Session | None:
        with self._guard:
            return self._data.get(session_id)

    def save(self, session: Session) -> None:
        with self._guard:
            self._data[session.session_id] = session

    def all_sessions(self) -> list[Session]:
        with self._guard:
            return list(self._data.values())


_store: SessionStore = InMemorySessionStore()


def get_store() -> SessionStore:
    return _store
