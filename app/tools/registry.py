"""Tool registry — OpenAI function schemas plus dispatch.

Schema descriptions are written for the model, and they carry policy the prompt
should not have to restate on every turn. `check_tour_availability` says outright
that the model must not compute dates itself; `submit_insurance_check` says the
result never confirms coverage. Putting the constraint next to the thing it
constrains is more reliable than hoping a rule 300 lines up in the system prompt
survives.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.tools import capture, tour

ToolFn = Callable[..., Any]


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _fn(
        "check_tour_availability",
        "Check whether a tour date/time is available. ALWAYS call this before "
        "discussing any specific date or time — never work out dates yourself. "
        "Pass the person's own words through unchanged (e.g. 'next Sunday', "
        "'this Friday'); this tool resolves them against the real calendar and "
        "applies the facility's tour hours.",
        {
            "date_expression": {
                "type": "string",
                "description": "The date exactly as the person expressed it, e.g. 'next Sunday', 'tomorrow', 'March 6'.",
            },
            "time_expression": {
                "type": "string",
                "description": "The time as expressed, e.g. '3pm', '2:30', 'morning'. Empty string if not given.",
            },
        },
        ["date_expression", "time_expression"],
    ),
    _fn(
        "book_tour",
        "Confirm a tour booking. Only call after check_tour_availability has "
        "shown the slot is available AND the person has agreed to it. Re-checks "
        "availability internally, so a booking can still fail.",
        {
            "date_expression": {"type": "string"},
            "time_expression": {"type": "string"},
        },
        ["date_expression", "time_expression"],
    ),
    _fn(
        "save_lead",
        "Save contact details once collected. Call when you have at least a first "
        "and last name. Ask for fields one at a time before calling.",
        {
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "email": {"type": "string", "description": "Omit if not provided."},
            "phone": {"type": "string", "description": "Omit if not provided."},
            "address": {"type": "string", "description": "Only for brochure requests."},
            "best_time_to_reach": {"type": "string"},
            "notes": {"type": "string", "description": "Brief context for the admissions team."},
        },
        ["first_name", "last_name"],
    ),
    _fn(
        "submit_insurance_check",
        "Submit insurance details for benefit verification. Requires all five "
        "fields — ask for them one at a time. IMPORTANT: this does NOT confirm "
        "coverage. Never tell anyone their plan is accepted based on this result.",
        {
            "full_name": {"type": "string"},
            "date_of_birth": {"type": "string"},
            "zip_code": {"type": "string"},
            "insurance_provider": {"type": "string"},
            "member_id": {"type": "string"},
        },
        ["full_name", "date_of_birth", "zip_code", "insurance_provider", "member_id"],
    ),
    _fn(
        "submit_assessment",
        "Save intake assessment answers for the clinical team. Ask one question "
        "at a time and never push if someone doesn't want to answer — partial is "
        "fine. Only ask about withdrawal risk if opioids or alcohol are involved. "
        "This is intake information, not a clinical evaluation.",
        {
            "substances_used": {"type": "string"},
            "last_use": {"type": "string"},
            "withdrawal_risk": {"type": "string"},
            "reason_for_help": {"type": "string"},
            "mental_health_notes": {"type": "string"},
            "on_behalf_of": {"type": "string", "enum": ["self", "other", "unclear"]},
        },
        ["substances_used"],
    ),
    _fn(
        "escalate_to_human",
        "Hand off to a team member. Call when the person asks for a human, is "
        "frustrated, needs information you don't hold, or the topic is clinical.",
        {
            "reason": {"type": "string", "description": "Why the handoff is needed."},
            "urgency": {"type": "string", "enum": ["normal", "high"]},
        },
        ["reason"],
    ),
]


_DISPATCH: dict[str, ToolFn] = {
    "check_tour_availability": lambda **kw: tour.check_availability(**kw).model_dump(),
    "book_tour": lambda **kw: tour.book_tour(**kw).model_dump(),
    "save_lead": capture.save_lead,
    "submit_insurance_check": capture.submit_insurance_check,
    "submit_assessment": capture.submit_assessment,
    "escalate_to_human": capture.escalate_to_human,
}


def call_tool(name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call. Never raises — errors come back as data.

    An exception here would abort the turn; a returned error lets the agent
    recover conversationally, which is almost always the better outcome for the
    person on the other end.
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Malformed tool arguments: {exc}"}

    try:
        return fn(**arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"{name} failed: {exc}"}
