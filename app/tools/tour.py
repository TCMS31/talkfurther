"""Tour availability and booking.

Mocked per the brief (no real availability API), but the *rules* are real and the
date arithmetic is genuine.

This is the clearest case in the assignment for moving work out of the model. The
baseline pinned the current date into the prompt as a string and asked the model
to resolve "next Sunday" against it. Models do this wrong confidently and often,
and the brief's own example is a trap: "next Sunday at 3pm" must be DECLINED,
because tours run Monday-Friday. A model that gets the weekday arithmetic right
can still cheerfully book a Sunday.

So the model does not resolve dates. It passes the user's phrasing through
verbatim and this module answers.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timedelta

from app.config import get_settings
from app.models import AvailabilityResult, BookingResult, TourSlot

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


# ─── Date resolution ─────────────────────────────────────────────────────────


def resolve_date(expression: str, now: datetime) -> date | None:
    """Resolve a natural-language date expression against a reference time.

    Handles the forms people actually use in chat. Returns None when the
    expression is not confidently resolvable — the agent then asks rather than
    guessing, which is the correct failure mode for scheduling.
    """
    text = expression.strip().lower()
    today = now.date()

    if not text:
        return None
    # Order matters: "tomorrow" is a substring of "day after tomorrow", so the
    # more specific phrase must be tested first or it can never match.
    if "day after tomorrow" in text:
        return today + timedelta(days=2)
    if "tomorrow" in text:
        return today + timedelta(days=1)
    if "today" in text:
        return today

    # ISO form: 2025-03-06
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    # "March 6", "6 March", "March 6th"
    month_first = re.search(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    day_first = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b", text)
    if month_first or day_first:
        if month_first:
            month, day = _MONTHS[month_first.group(1)], int(month_first.group(2))
        else:
            month, day = _MONTHS[day_first.group(2)], int(day_first.group(1))
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        # A bare month/day in the past almost always means next year.
        if candidate < today:
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return None
        return candidate

    # Weekday names, with or without a this/next qualifier.
    weekday_match = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", text)
    if weekday_match:
        target = _WEEKDAYS[weekday_match.group(1)]
        current = today.weekday()
        delta = (target - current) % 7

        if "next" in text:
            # "next Tuesday" = the Tuesday of *next week*, anchored to the week
            # rather than counted forward from today.
            #
            # Counting forward is what the first implementation did, and it was
            # wrong for every weekday that falls at or before today in the
            # current week: `(target - current) % 7` has already wrapped into
            # next week for those, and adding another 7 landed two weeks out.
            # From Tuesday 4 March, "next Monday" resolved to the 17th instead
            # of the 10th. Anchoring to the start of next week makes the rule
            # the code implements identical to the rule the docstring states.
            start_of_next_week = today + timedelta(days=7 - current)
            return start_of_next_week + timedelta(days=target)
        else:
            # Bare or "this": the next upcoming instance. Today counts as
            # already passed — same-day tour requests are ambiguous enough that
            # rolling forward is the safer read.
            if delta == 0:
                delta = 7
        return today + timedelta(days=delta)

    return None


def resolve_time(expression: str) -> int | None:
    """Extract an hour (24h) from a time expression. Returns None if absent."""
    text = expression.strip().lower()

    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        return hour

    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m:
        hour = int(m.group(1))
        return hour if 0 <= hour <= 23 else None

    m = re.search(r"\b(?:at\s+)(\d{1,2})\b", text)
    if m:
        hour = int(m.group(1))
        # Bare "at 2" for a facility open 9-18 means 14:00, not 02:00.
        if 1 <= hour <= 8:
            return hour + 12
        if 9 <= hour <= 18:
            return hour
    return None


# ─── Mock availability ───────────────────────────────────────────────────────


def _is_booked(day: date, hour: int) -> bool:
    """Deterministic pseudo-random occupancy.

    Seeded from the date so the same query always returns the same answer —
    non-determinism here would make the eval suite flaky for no benefit. Roughly
    a third of slots are taken, so the "unavailable, here are alternatives" path
    is actually exercised in a demo rather than being dead code.
    """
    seed = f"{day.isoformat()}:{hour}"
    digest = hashlib.sha256(seed.encode()).digest()
    return digest[0] % 3 == 0


def _slot(day: date, hour: int) -> TourSlot:
    start = datetime(day.year, day.month, day.day, hour)
    return TourSlot(
        start_iso=start.isoformat(),
        human=start.strftime("%A, %B %-d at %-I:%M %p"),
    )


def _open_hours() -> range:
    settings = get_settings()
    # Last tour starts an hour before close.
    return range(settings.tour_open_hour, settings.tour_close_hour)


def _next_available(after: date, limit: int = 3) -> list[TourSlot]:
    out: list[TourSlot] = []
    day = after
    for _ in range(21):  # scan up to three weeks out
        if day.weekday() < 5:
            for hour in _open_hours():
                if not _is_booked(day, hour):
                    out.append(_slot(day, hour))
                    if len(out) >= limit:
                        return out
                    break  # at most one suggestion per day, for variety
        day += timedelta(days=1)
    return out


def check_availability(date_expression: str, time_expression: str = "") -> AvailabilityResult:
    """Check whether a requested tour time is available.

    `date_expression` and `time_expression` are the user's own words, passed
    through unmodified. All resolution happens here.
    """
    settings = get_settings()
    now = settings.now()
    today = now.date()

    combined = f"{date_expression} {time_expression}".strip()
    resolved = resolve_date(date_expression, now) or resolve_date(combined, now)

    if resolved is None:
        return AvailabilityResult(
            requested_understood=False,
            reason="Could not determine which date was meant.",
            alternatives=_next_available(today + timedelta(days=1)),
        )

    if resolved < today:
        return AvailabilityResult(
            requested_understood=True,
            resolved_date=resolved.isoformat(),
            available=False,
            reason=f"{resolved.strftime('%A, %B %-d')} is in the past.",
            alternatives=_next_available(today + timedelta(days=1)),
        )

    # The rule the baseline's example is designed to catch.
    if resolved.weekday() >= 5:
        return AvailabilityResult(
            requested_understood=True,
            resolved_date=resolved.isoformat(),
            available=False,
            reason=(
                f"{resolved.strftime('%B %-d')} falls on a "
                f"{resolved.strftime('%A')} — tours run Monday through Friday only."
            ),
            alternatives=_next_available(resolved + timedelta(days=1)),
        )

    hour = resolve_time(time_expression) or resolve_time(combined)

    if hour is None:
        open_slots = [_slot(resolved, h) for h in _open_hours() if not _is_booked(resolved, h)]
        return AvailabilityResult(
            requested_understood=True,
            resolved_date=resolved.isoformat(),
            available=bool(open_slots),
            reason=(
                None if open_slots
                else f"No openings on {resolved.strftime('%A, %B %-d')}."
            ),
            alternatives=open_slots[:3] or _next_available(resolved + timedelta(days=1)),
        )

    if hour not in _open_hours():
        return AvailabilityResult(
            requested_understood=True,
            resolved_date=resolved.isoformat(),
            available=False,
            reason=(
                f"{hour}:00 is outside tour hours — we run "
                f"{settings.tour_open_hour}:00 to {settings.tour_close_hour}:00."
            ),
            alternatives=[
                _slot(resolved, h) for h in _open_hours() if not _is_booked(resolved, h)
            ][:3],
        )

    if _is_booked(resolved, hour):
        same_day = [
            _slot(resolved, h)
            for h in _open_hours()
            if h != hour and not _is_booked(resolved, h)
        ]
        return AvailabilityResult(
            requested_understood=True,
            resolved_date=resolved.isoformat(),
            available=False,
            reason=f"That slot is already taken on {resolved.strftime('%A, %B %-d')}.",
            alternatives=same_day[:3] or _next_available(resolved + timedelta(days=1)),
        )

    return AvailabilityResult(
        requested_understood=True,
        resolved_date=resolved.isoformat(),
        available=True,
        alternatives=[_slot(resolved, hour)],
    )


def book_tour(date_expression: str, time_expression: str) -> BookingResult:
    """Book a tour. Re-checks availability — never trusts the caller's belief."""
    # A booking needs an unambiguous hour. Without this guard, a vague time
    # ("afternoon", "sometime", "") makes check_availability return the day's
    # open slots as `alternatives`, and we would confirm the first one — booking
    # a person into a time they never named. Refuse and let the agent ask.
    combined = f"{date_expression} {time_expression}".strip()
    if resolve_time(time_expression) is None and resolve_time(combined) is None:
        return BookingResult(
            confirmed=False,
            reason="No specific time was given — ask which time they'd like before booking.",
        )

    result = check_availability(date_expression, time_expression)
    if not result.available or not result.alternatives:
        return BookingResult(
            confirmed=False,
            reason=result.reason or "That time isn't available.",
        )
    slot = result.alternatives[0]
    return BookingResult(
        confirmed=True,
        slot=slot,
        confirmation_id=f"FBH-{uuid.uuid4().hex[:8].upper()}",
    )
