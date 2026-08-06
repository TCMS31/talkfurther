"""Tests for the HTTP surface.

Previously untested end to end: the three endpoints were only ever exercised by
hand. Everything here runs against the scripted double, so a fresh clone with no
`OPENAI_API_KEY` is enough — which is also the property being asserted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.observability import trace as trace_sink
from app.pipeline import Pipeline
from app.session import InMemorySessionStore
from app.testing.fake_llm import FakeLLMClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose pipeline is pinned to the scripted double.

    The app builds its pipeline at import, so the double is installed by
    swapping the module-level instance rather than by relying on the
    `OFFLINE_MODE` environment variable having been set before the import.
    """
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setattr(
        "app.main._pipeline",
        Pipeline(
            store=InMemorySessionStore(),
            llm_factory=lambda usage: FakeLLMClient(usage=usage),
        ),
    )
    with TestClient(app) as test_client:
        yield test_client


# ─── /health ──────────────────────────────────────────────────────────────────


def test_health_reports_a_loaded_knowledge_base(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["facts_loaded"] > 0
    assert body["offline_mode"] is True


def test_health_works_with_no_api_key(client: TestClient) -> None:
    """A fresh clone with no credentials must still boot and answer.

    `conftest.isolated_sinks` deletes OPENAI_API_KEY, and `block_network` makes
    an outbound call impossible, so a 200 here means neither startup nor /health
    reaches the network.
    """
    body = client.get("/health").json()
    assert body["api_key_configured"] is False


# ─── /chat ────────────────────────────────────────────────────────────────────


def test_chat_returns_a_response_and_a_trace(client: TestClient) -> None:
    body = client.post("/chat", json={"message": "How much does treatment cost?"}).json()
    assert body["response"]
    assert body["session_id"]
    assert body["trace"]["intent"] == "pricing"
    assert body["trace"]["retrieved_fact_ids"]


def test_chat_continues_a_session(client: TestClient) -> None:
    first = client.post("/chat", json={"message": "Hi"}).json()
    second = client.post(
        "/chat", json={"message": "What does it cost?", "session_id": first["session_id"]}
    ).json()
    assert second["session_id"] == first["session_id"]
    assert second["trace"]["turn_index"] == 1


def test_chat_crisis_turn_never_reaches_the_model(client: TestClient) -> None:
    """The safety property, asserted through the public API rather than the unit."""
    body = client.post(
        "/chat", json={"message": "I took too many pills, I don't feel okay"}
    ).json()
    assert body["trace"]["crisis"]["category"] == "overdose_medical_emergency"
    assert "911" in body["response"]
    assert body["trace"]["tokens"].get("calls", 0) == 0


@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "x" * 4001}])
def test_chat_rejects_malformed_input_with_422(client: TestClient, payload: dict) -> None:
    assert client.post("/chat", json=payload).status_code == 422


def test_chat_maps_a_pipeline_failure_to_500_not_a_traceback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("knowledge base unavailable")

    monkeypatch.setattr("app.main._pipeline.handle", boom)
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 500
    assert "knowledge base unavailable" in response.json()["detail"]


# ─── /traces ──────────────────────────────────────────────────────────────────


def test_traces_returns_newest_first(client: TestClient) -> None:
    session = client.post("/chat", json={"message": "Hi"}).json()["session_id"]
    client.post("/chat", json={"message": "What does it cost?", "session_id": session})

    traces = client.get("/traces").json()["traces"]
    assert [t["turn_index"] for t in traces] == [1, 0]


def test_traces_filter_by_session(client: TestClient) -> None:
    one = client.post("/chat", json={"message": "Hi"}).json()["session_id"]
    two = client.post("/chat", json={"message": "Hello"}).json()["session_id"]
    assert one != two

    traces = client.get("/traces", params={"session_id": one}).json()["traces"]
    assert [t["session_id"] for t in traces] == [one]


@pytest.mark.parametrize("limit", [0, -1, trace_sink.RING_SIZE + 1])
def test_traces_rejects_an_out_of_range_limit(client: TestClient, limit: int) -> None:
    """`limit=0` used to fall through to a negative slice and return everything."""
    assert client.get("/traces", params={"limit": limit}).status_code == 422


def test_traces_honours_a_valid_limit(client: TestClient) -> None:
    for _ in range(4):
        client.post("/chat", json={"message": "Hi"})
    assert len(client.get("/traces", params={"limit": 2}).json()["traces"]) == 2


# ─── Static UI ────────────────────────────────────────────────────────────────


def test_root_serves_the_chat_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Further BH Admissions Agent" in response.text


def test_root_is_404_when_the_ui_is_not_bundled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An API-only image has no `web/`. That is a 404, not a 500."""
    monkeypatch.setattr("app.main.WEB_DIR", tmp_path / "absent")
    assert client.get("/").status_code == 404
