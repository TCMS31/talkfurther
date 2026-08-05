"""HTTP surface.

Deliberately small: a chat endpoint, a trace endpoint, and static hosting for the
UI. The brief explicitly deprioritises demo polish, so the API exists to make the
agent and its traces reachable, not to be a product.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import REPO_ROOT, get_settings, offline_mode
from app.knowledge.retrieval import get_kb
from app.observability import trace as trace_sink
from app.pipeline import Pipeline

WEB_DIR = Path(REPO_ROOT / "web")
MAX_TRACE_PAGE = trace_sink.RING_SIZE


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Nothing to warm at boot; the trace sink needs closing at shutdown.

    Deliberately no network call here. A fresh clone with no OPENAI_API_KEY must
    still start, serve the UI and answer /health — the key is only needed when a
    turn actually reaches the model.
    """
    yield
    trace_sink.get_sink().close()


app = FastAPI(title="Further BH Admissions Agent", version="1.0.0", lifespan=lifespan)
_pipeline = Pipeline()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    trace: dict


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        result = _pipeline.handle(request.message, request.session_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Turn failed: {exc}") from exc
    return ChatResponse(
        response=result.response,
        session_id=result.session_id,
        trace=result.trace.model_dump(mode="json"),
    )


@app.get("/traces")
def traces(
    limit: int = Query(50, ge=1, le=MAX_TRACE_PAGE),
    session_id: str | None = None,
) -> dict:
    """Recent turns, newest first.

    `limit` is bounded rather than free. The ring buffer holds at most
    RING_SIZE turns, so a larger number could never return more rows, and an
    unvalidated one was worse than useless: `limit=0` fell through to a negative
    slice and returned the whole buffer instead of nothing.
    """
    return {"traces": trace_sink.recent(limit=limit, session_id=session_id)}


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    kb = get_kb()
    return {
        "status": "ok",
        "facts_loaded": len(kb.facts),
        "agent_model": settings.agent_model,
        "guard_model": settings.guard_model,
        "crisis_classifier": settings.enable_crisis_classifier,
        "grounding_verifier": settings.enable_grounding_verifier,
        "api_key_configured": bool(settings.openai_api_key),
        # Surfaced so the UI can label itself. A demo running on the scripted
        # double must say so on screen: the deterministic layers are real, the
        # wording is not, and a screenshot that hides the difference is a lie.
        "offline_mode": offline_mode(),
    }


@app.get("/")
def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        # A slim image that ships only the API has no `web/`. A 404 is the
        # honest answer; the previous code raised inside FileResponse and
        # surfaced it as a 500.
        raise HTTPException(status_code=404, detail="UI not bundled in this build.")
    return FileResponse(page)


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
