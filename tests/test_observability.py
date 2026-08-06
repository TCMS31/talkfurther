"""Tests for the trace ring and the sink seam.

The README claims a real deployment can swap the JSONL sink without touching
callers, and that a logging failure never takes a conversation down. Both are
asserted here rather than asserted in prose.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.models import TurnTrace
from app.observability import trace as trace_sink


def _trace(session: str = "s1", index: int = 0) -> TurnTrace:
    return TurnTrace(
        session_id=session,
        turn_index=index,
        timestamp="2025-03-04T05:40:00",
        user_message=f"message {index}",
        final_response=f"reply {index}",
    )


class RecordingSink:
    """A sink that keeps everything in memory. Also the proof that the seam works."""

    def __init__(self) -> None:
        self.written: list[TurnTrace] = []
        self.closed = False

    def write(self, trace: TurnTrace) -> None:
        self.written.append(trace)

    def close(self) -> None:
        self.closed = True


class BrokenSink:
    def write(self, trace: TurnTrace) -> None:
        raise OSError("disk full")

    def close(self) -> None:
        return None


# ─── The seam ─────────────────────────────────────────────────────────────────


def test_a_custom_sink_receives_every_trace() -> None:
    sink = RecordingSink()
    trace_sink.set_sink(sink)
    trace_sink.emit(_trace(index=0))
    trace_sink.emit(_trace(index=1))
    assert [t.turn_index for t in sink.written] == [0, 1]


def test_a_recording_sink_satisfies_the_protocol() -> None:
    assert isinstance(RecordingSink(), trace_sink.TraceSink)
    assert isinstance(trace_sink.JsonlTraceSink(Path("unused.jsonl")), trace_sink.TraceSink)
    assert isinstance(trace_sink.NullTraceSink(), trace_sink.TraceSink)


def test_swapping_the_sink_closes_the_previous_one() -> None:
    first = RecordingSink()
    trace_sink.set_sink(first)
    trace_sink.set_sink(RecordingSink())
    assert first.closed


def test_a_failing_sink_never_breaks_a_turn() -> None:
    """A logging outage degrades observability, not the conversation."""
    trace_sink.set_sink(BrokenSink())
    trace_sink.emit(_trace())  # must not raise
    assert trace_sink.recent(limit=1)[0]["turn_index"] == 0


def test_the_null_sink_writes_nothing_and_still_serves_reads() -> None:
    trace_sink.set_sink(trace_sink.NullTraceSink())
    trace_sink.emit(_trace(index=7))
    assert trace_sink.recent(limit=1)[0]["turn_index"] == 7


# ─── The JSONL sink ───────────────────────────────────────────────────────────


def test_jsonl_sink_writes_one_parseable_record_per_turn(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "traces.jsonl"
    sink = trace_sink.JsonlTraceSink(path)
    for i in range(5):
        sink.write(_trace(index=i))
    sink.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["turn_index"] for r in records] == [0, 1, 2, 3, 4]


def test_jsonl_sink_survives_concurrent_writers(tmp_path: Path) -> None:
    """Appends from FastAPI's threadpool must not tear a record."""
    sink = trace_sink.JsonlTraceSink(tmp_path / "traces.jsonl")

    def write_many(worker: int) -> None:
        for i in range(120):
            sink.write(_trace(session=f"s{worker}", index=i))

    threads = [threading.Thread(target=write_many, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close()

    lines = (tmp_path / "traces.jsonl").read_text().splitlines()
    assert len(lines) == 8 * 120
    assert all(json.loads(line)["turn_index"] is not None for line in lines)


def test_the_sink_follows_a_reconfigured_trace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing TRACE_PATH and clearing the settings cache must redirect output."""
    from app.config import get_settings

    redirected = tmp_path / "elsewhere.jsonl"
    trace_sink.emit(_trace(index=1))  # builds a sink against the original path
    monkeypatch.setenv("TRACE_PATH", str(redirected))
    get_settings.cache_clear()
    trace_sink.emit(_trace(index=2))

    assert redirected.is_file()
    assert [json.loads(line)["turn_index"] for line in redirected.read_text().splitlines()] == [2]


# ─── The ring ─────────────────────────────────────────────────────────────────


def test_recent_returns_newest_first() -> None:
    for i in range(3):
        trace_sink.emit(_trace(index=i))
    assert [t["turn_index"] for t in trace_sink.recent()] == [2, 1, 0]


def test_recent_filters_by_session_before_limiting() -> None:
    trace_sink.emit(_trace(session="a", index=0))
    for i in range(5):
        trace_sink.emit(_trace(session="b", index=i))
    assert [t["turn_index"] for t in trace_sink.recent(limit=2, session_id="a")] == [0]


def test_the_ring_is_bounded() -> None:
    trace_sink.set_sink(trace_sink.NullTraceSink())
    for i in range(trace_sink.RING_SIZE + 50):
        trace_sink.emit(_trace(index=i))
    everything = trace_sink.recent(limit=trace_sink.RING_SIZE)
    assert len(everything) == trace_sink.RING_SIZE
    assert everything[0]["turn_index"] == trace_sink.RING_SIZE + 49


def test_clear_empties_the_ring() -> None:
    trace_sink.emit(_trace())
    trace_sink.clear()
    assert trace_sink.recent() == []
