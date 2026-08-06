"""Shared test fixtures, and the two invariants the suite is built on.

1. **No test touches the network.** `block_network` is autouse and replaces the
   socket primitives with something that raises. That turns "these tests don't
   need a key" from a claim in the README into a property the suite enforces: a
   test that accidentally constructs a real `OpenAI` call fails loudly with
   `NetworkAccessBlocked` instead of quietly billing someone, hanging in CI, or
   passing only on a machine that happens to have credentials.

2. **No test writes to the working tree.** Trace and capture sinks are
   redirected into `tmp_path`. Before this existed, a plain `pytest` run left 29
   trace records and 3 captured leads in the repo root.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings
from app.knowledge.retrieval import get_kb
from app.observability import trace as trace_sink


class NetworkAccessBlocked(RuntimeError):
    """Raised when a test attempts to open a socket."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make outbound network access impossible for the duration of a test."""

    def deny(*args: object, **kwargs: object) -> None:
        raise NetworkAccessBlocked(
            "This suite runs with no API key and no network. If a test needs a "
            "model response, use app.testing.fake_llm.FakeLLMClient."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture(autouse=True)
def isolated_sinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point every on-disk sink at `tmp_path` and reset cached config."""
    monkeypatch.setenv("TRACE_PATH", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("CAPTURE_PATH", str(tmp_path / "captured_records.jsonl"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    trace_sink.set_sink(None)
    trace_sink.clear()
    yield
    trace_sink.set_sink(None)
    trace_sink.clear()
    get_settings.cache_clear()


@pytest.fixture
def kb():
    return get_kb()


@pytest.fixture
def trace_file(tmp_path: Path) -> Path:
    return tmp_path / "traces.jsonl"


@pytest.fixture
def capture_file(tmp_path: Path) -> Path:
    return tmp_path / "captured_records.jsonl"
