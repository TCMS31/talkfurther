"""Tests for tools and pipeline control flow.

Uses the scripted double so control flow — routing, tool dispatch, verification,
repair, fallback — is assertable without a key. These are the paths most likely to
break and least dependent on model quality.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.knowledge.retrieval import get_kb
from app.models import CrisisCategory
from app.pipeline import Pipeline
from app.session import InMemorySessionStore
from app.testing.fake_llm import FakeLLMClient
from app.tools import tour
from app.tools.registry import call_tool

# The reference clock the baseline prompt hardcoded: Tuesday 4 March 2025.
NOW = datetime(2025, 3, 4, 5, 40)


# ─── Date resolution ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("next Sunday", date(2025, 3, 16)),
        ("Sunday", date(2025, 3, 9)),
        # Was asserted as the 17th, which is the Monday of the week *after* next.
        # The assertion encoded the bug rather than the rule; see
        # test_next_weekday_is_always_that_day_of_next_week.
        ("next Monday", date(2025, 3, 10)),
        ("this Friday", date(2025, 3, 7)),
        ("tomorrow", date(2025, 3, 5)),
        ("day after tomorrow", date(2025, 3, 6)),
        ("March 6", date(2025, 3, 6)),
        ("March 6th", date(2025, 3, 6)),
        ("6 March", date(2025, 3, 6)),
        ("2025-03-12", date(2025, 3, 12)),
    ],
)
def test_relative_dates_resolve(expression: str, expected: date) -> None:
    assert tour.resolve_date(expression, NOW) == expected


@pytest.mark.parametrize(
    "expression",
    ["sometime soonish", "a week from now", "in two weeks", "the 6th", "", "next week"],
)
def test_ambiguous_dates_are_refused_not_guessed(expression: str) -> None:
    """Scheduling must ask rather than guess. None is the correct answer here."""
    assert tour.resolve_date(expression, NOW) is None


@pytest.mark.parametrize("expression", ["February 30", "2025-02-30", "April 31"])
def test_impossible_calendar_dates_return_none(expression: str) -> None:
    assert tour.resolve_date(expression, NOW) is None


_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


@pytest.mark.parametrize("offset", range(14))
@pytest.mark.parametrize("target", range(7))
def test_next_weekday_is_always_that_day_of_next_week(offset: int, target: int) -> None:
    """`next <weekday>` = that weekday in the following ISO week, for every
    (reference day, weekday) pair.

    The expected value is derived independently of the implementation: take the
    Monday that starts next week, then add the target weekday index.

    Counting forward from today — what the first implementation did — is correct
    only when the named day is still ahead in the current week. For any day at or
    before today it landed a week late: from Tuesday 4 March, `next Monday`
    resolved to the 17th rather than the 10th. This table covers all 98
    combinations, 42 of which were wrong before the fix.
    """
    reference = datetime(2025, 3, 1) + timedelta(days=offset)
    today = reference.date()
    start_of_next_week = today + timedelta(days=7 - today.weekday())
    expected = start_of_next_week + timedelta(days=target)

    assert tour.resolve_date(f"next {_WEEKDAY_NAMES[target]}", reference) == expected


@pytest.mark.parametrize("offset", range(7))
@pytest.mark.parametrize("target", range(7))
def test_bare_weekday_is_the_next_upcoming_instance(offset: int, target: int) -> None:
    """A bare or `this` weekday rolls forward to the next instance, and today
    counts as already passed (a same-day tour request is too ambiguous to book)."""
    reference = datetime(2025, 3, 1) + timedelta(days=offset)
    today = reference.date()
    delta = (target - today.weekday()) % 7 or 7
    expected = today + timedelta(days=delta)

    assert tour.resolve_date(_WEEKDAY_NAMES[target], reference) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("3pm", 15), ("at 2", 14), ("2:30pm", 14), ("14:00", 14), ("9am", 9)],
)
def test_times_resolve(expression: str, expected: int) -> None:
    assert tour.resolve_time(expression) == expected


def test_unparseable_date_returns_none() -> None:
    """Better to ask than to guess when scheduling."""
    assert tour.resolve_date("sometime soonish", NOW) is None


# ─── Availability rules ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setenv("FROZEN_NOW", "2025-03-04T05:40:00")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_sunday_tour_is_declined_with_alternatives() -> None:
    """The brief's trap case: correct weekday arithmetic still must not book."""
    result = tour.check_availability("next Sunday", "3pm")
    assert result.requested_understood
    assert result.resolved_date == "2025-03-16"
    assert not result.available
    assert "Sunday" in (result.reason or "")
    assert result.alternatives
    assert all("Saturday" not in a.human and "Sunday" not in a.human for a in result.alternatives)


def test_booking_a_sunday_is_refused() -> None:
    assert not tour.book_tour("next Sunday", "3pm").confirmed


def test_weekday_in_hours_is_available() -> None:
    result = tour.check_availability("Tuesday", "2pm")
    assert result.available
    assert result.resolved_date == "2025-03-11"


def test_outside_hours_is_declined() -> None:
    result = tour.check_availability("Tuesday", "8pm")
    assert not result.available
    assert "outside tour hours" in (result.reason or "")


def test_past_date_is_declined() -> None:
    result = tour.check_availability("2024-01-05", "10am")
    assert not result.available
    assert "past" in (result.reason or "")


def test_availability_is_deterministic() -> None:
    """Seeded occupancy — a flaky eval suite would be worse than none."""
    assert tour.check_availability("Tuesday", "2pm") == tour.check_availability("Tuesday", "2pm")


# ─── Validation boundary ─────────────────────────────────────────────────────


def test_invalid_email_is_rejected_as_data_not_exception() -> None:
    result = call_tool("save_lead", {"first_name": "A", "last_name": "B", "email": "nope"})
    assert result["ok"] is False
    assert any("email" in p for p in result["problems"])


def test_invalid_phone_is_rejected() -> None:
    result = call_tool("save_lead", {"first_name": "A", "last_name": "B", "phone": "12"})
    assert result["ok"] is False


def test_valid_lead_is_saved() -> None:
    result = call_tool(
        "save_lead",
        {"first_name": "James", "last_name": "Brown", "email": "j@example.com"},
    )
    assert result["ok"] is True


def test_unknown_tool_and_malformed_args_never_raise() -> None:
    assert call_tool("does_not_exist", {})["ok"] is False
    assert call_tool("save_lead", "{not json")["ok"] is False


def test_insurance_result_never_implies_confirmed_coverage() -> None:
    result = call_tool(
        "submit_insurance_check",
        {
            "full_name": "James Brown",
            "date_of_birth": "1970-01-01",
            "zip_code": "28203",
            "insurance_provider": "Aetna",
            "member_id": "X123",
        },
    )
    assert result["ok"] is True
    assert "NOT confirmed" in result["message"]


# ─── Pipeline control flow ───────────────────────────────────────────────────


@pytest.fixture
def offline_pipeline(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    return Pipeline(
        store=InMemorySessionStore(),
        llm_factory=lambda usage: FakeLLMClient(usage=usage),
    )


def test_crisis_bypasses_generation_entirely(offline_pipeline) -> None:
    """The core safety property: no model output on a crisis turn."""
    result = offline_pipeline.handle("I took too many pills, I don't feel okay")
    assert result.trace.crisis.category is CrisisCategory.OVERDOSE_MEDICAL
    assert "911" in result.response
    assert not result.trace.tool_calls
    # Zero generation cost — the turn short-circuits before any LLM call.
    assert result.trace.tokens.get("calls", 0) == 0


def test_crisis_overrides_an_in_progress_booking(offline_pipeline) -> None:
    sid = None
    for message in ["I'd like a tour", "Maybe Wednesday", "I don't want to live anymore"]:
        result = offline_pipeline.handle(message, sid)
        sid = result.session_id
    assert result.trace.crisis.category is CrisisCategory.SUICIDAL_IDEATION
    assert "988" in result.response
    assert "Wednesday" not in result.response


def test_crisis_flag_persists_into_later_turns(offline_pipeline) -> None:
    first = offline_pipeline.handle("I don't want to live anymore")
    offline_pipeline.handle("sorry, what were the visiting hours", first.session_id)
    session = offline_pipeline._store.get(first.session_id)
    assert session.crisis_flagged


def test_greeting_state_is_tracked_not_left_to_the_model(offline_pipeline) -> None:
    first = offline_pipeline.handle("Hi there")
    session = offline_pipeline._store.get(first.session_id)
    assert session.greeted and session.disclosed


def test_every_turn_emits_a_trace(offline_pipeline) -> None:
    result = offline_pipeline.handle("How much does it cost?")
    trace = result.trace
    assert trace.session_id and trace.timestamp
    assert trace.retrieved_fact_ids
    assert "total" in trace.latency_ms


def test_retrieved_facts_are_a_slice_of_the_kb(offline_pipeline) -> None:
    result = offline_pipeline.handle("How much does treatment cost?")
    assert 0 < len(result.trace.retrieved_fact_ids) < len(get_kb().facts)


def test_tool_results_populate_session_slots(offline_pipeline) -> None:
    """Backs the 'never re-ask' guarantee."""
    offline_pipeline._llm_factory = lambda usage: FakeLLMClient(
        usage=usage,
        scripted=[
            {
                "tool": "save_lead",
                "args": {"first_name": "James", "last_name": "Brown"},
            },
            "Thanks James — someone will be in touch.",
        ],
    )
    result = offline_pipeline.handle("I'm James Brown, please call me")
    session = offline_pipeline._store.get(result.session_id)
    assert session.slots.get("first_name") == "James"
    assert any(c.name == "save_lead" for c in result.trace.tool_calls)


# ─── Regressions from code review ────────────────────────────────────────────


def test_day_after_tomorrow_is_not_tomorrow() -> None:
    """Regression: 'tomorrow' is a substring, so the specific phrase never matched."""
    assert tour.resolve_date("day after tomorrow", NOW) == date(2025, 3, 6)
    assert tour.resolve_date("tomorrow", NOW) == date(2025, 3, 5)


@pytest.mark.parametrize("vague", ["", "afternoon", "sometime", "whenever"])
def test_booking_refuses_when_no_time_was_given(vague: str) -> None:
    """Regression: a vague time booked the first open slot of the day —
    confirming a person into an hour they never named."""
    result = tour.book_tour("Thursday", vague)
    assert not result.confirmed
    assert "time" in (result.reason or "").lower()


def test_booking_still_works_with_an_explicit_time() -> None:
    assert tour.book_tour("Tuesday", "2pm").confirmed


def test_crisis_turn_does_not_consume_the_recording_disclosure(offline_pipeline) -> None:
    """Regression: _finish marked greeted/disclosed on every exit path, so a
    conversation opening with a crisis never received the disclosure at all."""
    first = offline_pipeline.handle("I took too many pills, I don't feel okay")
    session = offline_pipeline._store.get(first.session_id)
    assert not session.disclosed
    assert not session.greeted


def test_generation_failure_does_not_consume_the_recording_disclosure() -> None:
    """The canned error reply carries no greeting or disclosure either, so it
    must not mark them delivered."""

    class _Down:
        def __init__(self, usage):
            self.usage = usage

        def complete(self, *args, **kwargs):
            raise RuntimeError("api down")

        def structured(self, *args, **kwargs):
            return None

    pipeline = Pipeline(store=InMemorySessionStore(), llm_factory=_Down)
    result = pipeline.handle("what does it cost?")
    session = pipeline._store.get(result.session_id)
    assert result.trace.errors
    assert not session.greeted
    assert not session.disclosed


def test_concurrent_turns_on_one_session_do_not_interleave() -> None:
    """Regression: FastAPI runs sync endpoints on a threadpool, so two turns on
    the same session raced on messages.append and turn_index.

    The double sleeps between the read and the write of each turn. Without that
    the race window is a few bytecodes wide and the test passes on broken code
    roughly always — a guard that cannot fail is not a guard.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    class _Slow(FakeLLMClient):
        def complete(self, *args, **kwargs):
            time.sleep(0.01)
            return super().complete(*args, **kwargs)

        def structured(self, *args, **kwargs):
            time.sleep(0.01)
            return super().structured(*args, **kwargs)

    pipeline = Pipeline(
        store=InMemorySessionStore(), llm_factory=lambda usage: _Slow(usage=usage)
    )
    first = pipeline.handle("hello")
    turns = 16
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: pipeline.handle(f"q{i}", first.session_id), range(turns)))

    session = pipeline._store.get(first.session_id)
    assert session.turn_index == turns + 1
    assert len(session.messages) == 2 * (turns + 1)
    assert [m.role for m in session.messages] == ["user", "assistant"] * (turns + 1)


# ─── Capture validation and dispatch errors ──────────────────────────────────


def test_insurance_check_requires_every_field() -> None:
    """All five are needed for a verification request; a partial one is useless."""
    result = call_tool("submit_insurance_check", {"full_name": "James Brown"})
    assert not result["ok"]
    assert {"date_of_birth", "zip_code", "insurance_provider", "member_id"} <= {
        problem.split(":")[0] for problem in result["problems"]
    }


def test_insurance_check_rejects_an_empty_member_id() -> None:
    from app.tools import capture

    result = capture.submit_insurance_check(
        full_name="James Brown", date_of_birth="1980-01-01", zip_code="28203",
        insurance_provider="Aetna", member_id="",
    )
    assert not result["ok"]
    assert any("member_id" in problem for problem in result["problems"])
    assert "one at a time" in result["next_step"]


def test_assessment_requires_substances_used() -> None:
    from app.tools import capture

    result = capture.submit_assessment(reason_for_help="my son needs help")
    assert not result["ok"]
    assert any("substances_used" in problem for problem in result["problems"])


def test_a_partial_assessment_is_accepted() -> None:
    """Never push if someone doesn't want to answer — partial is fine."""
    result = call_tool("submit_assessment", {"substances_used": "alcohol"})
    assert result["ok"]
    assert result["saved"]["substances_used"] == "alcohol"
    assert result["saved"]["on_behalf_of"] == "unclear"


def test_escalation_is_an_event_not_a_phrase() -> None:
    result = call_tool("escalate_to_human", {"reason": "asked for a person"})
    assert result["ok"]
    assert "Escalation logged" in result["message"]


def test_captured_records_land_in_the_configured_sink(capture_file) -> None:
    import json

    call_tool("save_lead", {"first_name": "James", "last_name": "Brown"})
    records = [json.loads(line) for line in capture_file.read_text().splitlines()]
    assert records[-1]["kind"] == "lead"
    assert records[-1]["payload"]["first_name"] == "James"
    assert records[-1]["captured_at"]


def test_a_tool_that_raises_comes_back_as_data(monkeypatch) -> None:
    """An exception here would abort the turn; an error lets the agent recover."""
    def boom(**_kwargs):
        raise RuntimeError("CRM unreachable")

    monkeypatch.setitem(
        __import__("app.tools.registry", fromlist=["_DISPATCH"])._DISPATCH,
        "save_lead", boom,
    )
    result = call_tool("save_lead", {"first_name": "A", "last_name": "B"})
    assert not result["ok"]
    assert "CRM unreachable" in result["error"]


def test_tool_arguments_accept_a_json_string_or_a_dict() -> None:
    from_string = call_tool("check_tour_availability", '{"date_expression": "next Sunday", "time_expression": "3pm"}')
    from_dict = call_tool(
        "check_tour_availability",
        {"date_expression": "next Sunday", "time_expression": "3pm"},
    )
    assert from_string == from_dict
    assert from_string["resolved_date"] == "2025-03-16"
