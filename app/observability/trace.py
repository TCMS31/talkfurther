"""Trace emission.

One JSONL record per turn. This is the observability product: it is what turns
"previewability" into an auditable decision path rather than just a chat window a
customer can watch.

JSONL over a database on purpose — it is greppable, diffable, trivially shipped to
any log pipeline, and adds no infrastructure to a demo.

Two structures, deliberately separate:

  * an in-memory ring of the most recent turns, which is what `/traces` serves;
  * a durable sink behind the `TraceSink` protocol, which is what survives a
    restart.

Keeping them apart is what lets the read path stay fast while the write path
touches a disk. It is also the seam: swapping the JSONL sink for stdout, S3 or an
OTLP exporter is `set_sink(...)` and nothing else changes.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections import deque
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.models import TurnTrace

RING_SIZE = 200


# ─── The seam ─────────────────────────────────────────────────────────────────


@runtime_checkable
class TraceSink(Protocol):
    """Durable destination for turn traces.

    Implementations must be safe to call from several threads and must never
    raise: a logging failure has to degrade, not take a conversation down with
    it. `emit` enforces that with a catch-all, but a sink that handles its own
    errors gives a better message in the trace.
    """

    def write(self, trace: TurnTrace) -> None: ...

    def close(self) -> None: ...


class JsonlTraceSink:
    """Append-only JSONL, one open handle, one dedicated lock.

    The handle is held open rather than reopened per turn — at demo volumes that
    is noise, but it also means the I/O lock is held for a buffered write rather
    than for an `open`/`write`/`close` cycle, which is what keeps it from
    becoming the process's contention point.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._handle = None

    def write(self, trace: TurnTrace) -> None:
        line = trace.model_dump_json()
        with self._lock:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


class NullTraceSink:
    """Drops everything. For tests and for anyone running without a writable disk."""

    def write(self, trace: TurnTrace) -> None:
        return None

    def close(self) -> None:
        return None


# ─── Module state ─────────────────────────────────────────────────────────────
#
# `_ring_lock` guards ONLY the deque. Serialising a trace to JSON and writing it
# to disk both happen outside it, so a slow disk cannot add latency to `/traces`.

_ring_lock = threading.Lock()
_recent: deque[TurnTrace] = deque(maxlen=RING_SIZE)

_sink_lock = threading.Lock()
_sink: TraceSink | None = None
_sink_path: Path | None = None


def set_sink(sink: TraceSink | None) -> None:
    """Install a sink, or pass None to fall back to the configured JSONL file."""
    global _sink, _sink_path
    with _sink_lock:
        if _sink is not None and _sink is not sink:
            _sink.close()
        _sink = sink
        _sink_path = None


def get_sink() -> TraceSink:
    """The active sink, built lazily from configuration on first use.

    Rebuilt when `TRACE_PATH` changes, so `get_settings.cache_clear()` in a test
    genuinely redirects output instead of writing to a handle opened against the
    previous path.
    """
    global _sink, _sink_path
    with _sink_lock:
        configured = get_settings().trace_path
        if _sink is None or (_sink_path is not None and _sink_path != configured):
            if _sink is not None:
                _sink.close()
            _sink = JsonlTraceSink(configured)
            _sink_path = configured
        return _sink


def emit(trace: TurnTrace) -> None:
    with _ring_lock:
        _recent.append(trace)
    # Never let a logging failure take down a conversation: observability is
    # worth a disk, a conversation is not.
    with contextlib.suppress(Exception):
        get_sink().write(trace)


def recent(limit: int = 50, session_id: str | None = None) -> list[dict]:
    """Most recent turns, newest first.

    The deque is copied under the lock and everything else — filtering and JSON
    serialisation — happens outside it.
    """
    with _ring_lock:
        items = list(_recent)
    if session_id:
        items = [t for t in items if t.session_id == session_id]
    return [json.loads(t.model_dump_json()) for t in reversed(items[-limit:])]


def clear() -> None:
    with _ring_lock:
        _recent.clear()
