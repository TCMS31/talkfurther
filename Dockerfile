# syntax=docker/dockerfile:1

# ── Stage 1: build the virtualenv ────────────────────────────────────────────
# Dependencies are resolved in a throwaway layer so the runtime image carries no
# compiler, no pip cache and no build metadata.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# The repo root is not writable for the app user, so both on-disk sinks are
# pointed at a directory that is. The compose file mounts a volume there so a
# restart does not erase the audit trail.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TRACE_PATH=/var/lib/further/traces.jsonl \
    CAPTURE_PATH=/var/lib/further/captured_records.jsonl

COPY --from=builder /opt/venv /opt/venv

# Non-root. The process needs to read the app and write one directory.
RUN groupadd --system further \
    && useradd --system --gid further --home-dir /app --shell /usr/sbin/nologin further \
    && mkdir -p /var/lib/further \
    && chown further:further /var/lib/further

WORKDIR /app
COPY --chown=further:further app ./app
COPY --chown=further:further evals ./evals
COPY --chown=further:further web ./web

USER further
EXPOSE 8000

# /health loads the knowledge base and reports how many facts parsed, so a
# healthy container is one that can actually answer a turn — not merely one
# whose port is open. It needs no API key, by design.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
b=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)); \
sys.exit(0 if b.get('status')=='ok' and b.get('facts_loaded',0)>0 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
