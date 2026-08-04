"""Runtime configuration.

Everything tunable lives here so the cost/latency/safety tradeoffs are inspectable
in one place rather than scattered as literals through the pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# `override=True` so the project's .env beats an inherited shell variable.
#
# This inverts the usual 12-factor precedence, deliberately. The failure it
# prevents is silent and expensive: a stale OPENAI_API_KEY exported from a
# developer's shell profile shadows the .env key, and the only symptom is an
# auth or quota error attributed to the wrong credential. For a self-contained
# repo whose deployment story is "clone it and run it", the checked-out config
# should be the source of truth.
#
# For a real deployment — where the platform injects secrets as environment
# variables and there is no .env on disk — drop `override` back to the default.
load_dotenv(override=True)

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def offline_mode() -> bool:
    """Whether the scripted double replaces the real model.

    Read live rather than cached on `Settings`, because it is toggled per process
    invocation (`make demo`, `make eval-safety`) and per test, and a cached value
    would make `monkeypatch.setenv` silently ineffective. It was previously
    re-parsed inline in three places, which is two places too many for a flag
    that decides whether anything reaches the network.
    """
    return _flag("OFFLINE_MODE", False)


@dataclass(frozen=True)
class Settings:
    """All env reads go through `default_factory`.

    A bare `os.getenv(...)` as a dataclass default is evaluated once, at class
    creation — i.e. at import. That silently ignores any environment set after
    the first import, which breaks both `.env` loading order and per-test
    overrides. `default_factory` defers the read to instantiation, so
    `get_settings.cache_clear()` genuinely re-reads.
    """

    # ── Models ────────────────────────────────────────────────────────────────
    # The agent model does the talking. The guard model handles classification
    # and verification: high volume, narrow tasks, so it is deliberately cheaper.
    agent_model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", "gpt-4o"))
    guard_model: str = field(default_factory=lambda: os.getenv("GUARD_MODEL", "gpt-4o-mini"))

    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    request_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_S", "30"))
    )

    # ── Guardrails ────────────────────────────────────────────────────────────
    # The deterministic ingress fast-path cannot be disabled: it is the floor that
    # survives an API outage. Only the LLM stages are switchable, so their cost
    # can be measured rather than assumed.
    enable_crisis_classifier: bool = field(
        default_factory=lambda: _flag("ENABLE_CRISIS_CLASSIFIER", True)
    )
    enable_grounding_verifier: bool = field(
        default_factory=lambda: _flag("ENABLE_GROUNDING_VERIFIER", True)
    )

    # Bounded at 1 by design. A second failure implies the KB is missing the fact,
    # which is a handoff condition rather than a retry condition.
    max_repair_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_REPAIR_ATTEMPTS", "1"))
    )

    # ── Tours ─────────────────────────────────────────────────────────────────
    tour_open_hour: int = 9
    tour_close_hour: int = 18  # last tour starts at 17:00
    facility_timezone: str = field(
        default_factory=lambda: os.getenv("FACILITY_TIMEZONE", "America/New_York")
    )

    # Pins the clock for reproducible evals. ISO-8601; unset means "use real time".
    frozen_now: str | None = field(default_factory=lambda: os.getenv("FROZEN_NOW"))

    # ── Paths ─────────────────────────────────────────────────────────────────
    knowledge_path: Path = APP_DIR / "knowledge" / "facility.yaml"
    core_prompt_path: Path = APP_DIR / "prompts" / "system_core.md"
    baseline_prompt_path: Path = APP_DIR / "prompts" / "baseline.txt"
    trace_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("TRACE_PATH", str(REPO_ROOT / "traces.jsonl"))
        )
    )
    # Where captured leads / insurance checks / assessments land. Configurable
    # for the same reason as the trace path: the repo root is not writable in a
    # container, and a test run must not litter the working tree.
    capture_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("CAPTURE_PATH", str(REPO_ROOT / "captured_records.jsonl"))
        )
    )

    def now(self) -> datetime:
        """Current facility-local time.

        Injected rather than embedded in the prompt. The baseline hardcoded the
        date as a prompt string, which made date arithmetic both wrong and
        untestable.
        """
        tz = ZoneInfo(self.facility_timezone)
        if self.frozen_now:
            parsed = datetime.fromisoformat(self.frozen_now)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        return datetime.now(UTC).astimezone(tz)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
