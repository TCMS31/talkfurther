"""Shared domain models.

Closed enums are used deliberately throughout. The baseline prompt's control flow
was a prose dispatch table with edges pointing at steps that did not exist
(Step 4 routed to Steps 7 and 9; neither was defined). Making the routing space an
enum turns that class of bug into an impossibility rather than a review finding.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

# ─── Routing ──────────────────────────────────────────────────────────────────


class Intent(str, Enum):
    """Closed set of conversational intents. Replaces the baseline's `go to Step N`."""

    PRICING = "pricing"
    INSURANCE = "insurance"
    AMENITIES = "amenities"
    CARE_TYPES = "care_types"
    POLICY = "policy"
    TOUR_SCHEDULING = "tour_scheduling"
    ASSESSMENT = "assessment"
    CAREERS = "careers"
    CONTACT = "contact"
    BROCHURE = "brochure"
    HUMAN_HANDOFF = "human_handoff"
    SMALLTALK = "smalltalk"
    UNKNOWN = "unknown"
    """No fact covers this. Routes to honest 'I don't know' + handoff."""


# Intent → knowledge-base topics. Retrieval selects facts by these tags, so a turn
# carries roughly 5-10 facts instead of the entire fact sheet.
INTENT_TOPICS: dict[Intent, tuple[str, ...]] = {
    Intent.PRICING: ("pricing", "insurance"),
    Intent.INSURANCE: ("insurance", "pricing"),
    Intent.AMENITIES: (
        "amenities",
        "services",
        "activities",
        "dining",
        "rooms",
        "outdoor",
        "fitness",
        "religious",
        "accessibility",
    ),
    Intent.CARE_TYPES: ("care_types", "services", "limitations"),
    Intent.POLICY: ("policy", "pets", "visiting", "transport", "accessibility"),
    Intent.TOUR_SCHEDULING: ("tour", "contact"),
    Intent.ASSESSMENT: ("care_types", "insurance", "limitations"),
    Intent.CAREERS: ("careers",),
    Intent.CONTACT: ("contact", "general"),
    Intent.BROCHURE: ("contact", "general"),
    Intent.HUMAN_HANDOFF: ("contact",),
    Intent.SMALLTALK: ("general",),
    Intent.UNKNOWN: ("general", "contact"),
}


class RouteDecision(BaseModel):
    """Structured output of the intent router."""

    intent: Intent
    # Whether the user is asking for themselves or a third party. Drives pronoun
    # selection — the baseline specified this but gave the model no way to persist it.
    subject: Literal["self", "other", "unclear"] = "unclear"
    reasoning: str = Field(default="", max_length=300)


# ─── Safety ───────────────────────────────────────────────────────────────────


class CrisisCategory(str, Enum):
    NONE = "none"
    SUICIDAL_IDEATION = "suicidal_ideation"
    SELF_HARM = "self_harm"
    OVERDOSE_MEDICAL = "overdose_medical_emergency"
    WITHDRAWAL_RISK = "withdrawal_risk"
    THIRD_PARTY_DANGER = "third_party_danger"


class CrisisAssessment(BaseModel):
    category: CrisisCategory = CrisisCategory.NONE
    # "deterministic" wins over "classifier" — a regex hit is not overridable by
    # a model, which is what keeps the floor intact under prompt injection.
    detected_by: Literal["deterministic", "classifier", "none"] = "none"
    matched: str = ""

    @property
    def is_crisis(self) -> bool:
        return self.category is not CrisisCategory.NONE


# ─── Guardrail verdicts ───────────────────────────────────────────────────────


class Violation(BaseModel):
    code: str
    detail: str
    severity: Literal["block", "warn"] = "block"


class GuardVerdict(BaseModel):
    passed: bool = True
    violations: list[Violation] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "block"]


# ─── Captured records ─────────────────────────────────────────────────────────
# These are the point of lead qualification. The baseline collected them as prose
# in the transcript, which nothing downstream could consume.


class Lead(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr | None = None
    phone: str | None = None
    address: str | None = None
    best_time_to_reach: str | None = None
    notes: str | None = None

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = [c for c in v if c.isdigit()]
        if not 10 <= len(digits) <= 15:
            raise ValueError("phone number must contain 10-15 digits")
        return v


class InsuranceCheck(BaseModel):
    full_name: str = Field(min_length=1, max_length=160)
    date_of_birth: str
    zip_code: str = Field(min_length=3, max_length=10)
    insurance_provider: str = Field(min_length=1, max_length=120)
    member_id: str = Field(min_length=1, max_length=64)


class Assessment(BaseModel):
    """Intake assessment. Explicitly not a clinical instrument — it exists so a
    human clinician has context before they call back."""

    substances_used: str = Field(min_length=1)
    last_use: str | None = None
    withdrawal_risk: str | None = None  # only relevant for opioids/alcohol
    reason_for_help: str | None = None
    mental_health_notes: str | None = None
    on_behalf_of: Literal["self", "other", "unclear"] = "unclear"


# ─── Tours ────────────────────────────────────────────────────────────────────


class TourSlot(BaseModel):
    start_iso: str
    human: str  # e.g. "Thursday, March 6 at 2:00 PM"


class AvailabilityResult(BaseModel):
    requested_understood: bool
    resolved_date: str | None = None
    available: bool = False
    reason: str | None = None
    alternatives: list[TourSlot] = Field(default_factory=list)


class BookingResult(BaseModel):
    confirmed: bool
    slot: TourSlot | None = None
    confirmation_id: str | None = None
    reason: str | None = None


# ─── Tracing ──────────────────────────────────────────────────────────────────


class ToolCallTrace(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0


class TurnTrace(BaseModel):
    """One record per turn. This is the observability product — it is what makes
    'previewability' mean an auditable decision path rather than just a chat window."""

    session_id: str
    turn_index: int
    timestamp: str
    user_message: str

    crisis: CrisisAssessment = Field(default_factory=CrisisAssessment)
    intent: Intent | None = None
    subject: str = "unclear"
    retrieved_fact_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)

    deterministic_verdict: GuardVerdict | None = None
    grounding_verdict: GuardVerdict | None = None
    repair_attempts: int = 0
    fallback_used: bool = False

    final_response: str = ""
    latency_ms: dict[str, float] = Field(default_factory=dict)
    tokens: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)
