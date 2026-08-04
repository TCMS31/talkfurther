"""Lead, insurance, and assessment capture.

The point of lead qualification is a record something downstream can act on. The
baseline collected all of this as prose in the transcript, which meant the CRM
got nothing and the admissions team had to re-read chat logs.

Pydantic is the validation boundary: an email that does not parse never reaches
storage, and the failure surfaces to the agent as a retryable tool error rather
than being silently swallowed.

Persistence is intentionally a stub — writing JSONL where a CRM client would go.
The seam is what matters; the implementation behind it is a day's integration work.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.models import Assessment, InsuranceCheck, Lead

# Serialises appends from FastAPI's threadpool. CPython's GIL plus O_APPEND
# makes interleaving unlikely rather than impossible, and a torn record here is
# a lost lead.
_write_lock = threading.Lock()


def _persist(kind: str, payload: dict[str, Any]) -> None:
    record = {
        "kind": kind,
        "captured_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
    path: Path = get_settings().capture_path
    line = json.dumps(record, default=str) + "\n"
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _error(exc: ValidationError) -> dict[str, Any]:
    """Turn a validation failure into something the agent can act on.

    The message is phrased for the model, not for a log reader — it needs to know
    which field to re-ask for, not which validator tripped.
    """
    problems = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in exc.errors()
    ]
    return {
        "ok": False,
        "error": "Some details didn't validate.",
        "problems": problems,
        "next_step": "Ask the person to confirm the fields listed above. Ask one at a time.",
    }


def save_lead(**kwargs: Any) -> dict[str, Any]:
    try:
        lead = Lead(**{k: v for k, v in kwargs.items() if v not in (None, "")})
    except ValidationError as exc:
        return _error(exc)
    _persist("lead", lead.model_dump())
    return {
        "ok": True,
        "saved": lead.model_dump(),
        "message": "Lead saved. The admissions team will follow up.",
    }


def submit_insurance_check(**kwargs: Any) -> dict[str, Any]:
    try:
        check = InsuranceCheck(**{k: v for k, v in kwargs.items() if v not in (None, "")})
    except ValidationError as exc:
        return _error(exc)
    _persist("insurance_check", check.model_dump())
    return {
        "ok": True,
        "saved": check.model_dump(),
        # Explicit, because the agent must never imply coverage is confirmed.
        "message": (
            "Insurance details submitted for verification. Coverage is NOT "
            "confirmed — a team member will verify benefits and follow up."
        ),
    }


def submit_assessment(**kwargs: Any) -> dict[str, Any]:
    try:
        assessment = Assessment(**{k: v for k, v in kwargs.items() if v not in (None, "")})
    except ValidationError as exc:
        return _error(exc)
    _persist("assessment", assessment.model_dump())
    return {
        "ok": True,
        "saved": assessment.model_dump(),
        "message": (
            "Assessment saved for the clinical team. This is intake information "
            "only — it is not a clinical evaluation."
        ),
    }


def escalate_to_human(reason: str, urgency: str = "normal") -> dict[str, Any]:
    """Hand off to a person. An event, not a phrase — so it can be measured."""
    _persist("escalation", {"reason": reason, "urgency": urgency})
    return {
        "ok": True,
        "message": (
            "Escalation logged. Tell the person a team member will reach out, and "
            "offer the facility phone number if they'd rather call now."
        ),
    }
