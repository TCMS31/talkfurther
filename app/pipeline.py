"""Turn orchestration.

The six layers, in order:

  1. Ingress guardrail    crisis detection; short-circuits everything below
  2. Intent router        closed-enum classification
  3. Context assembly     persona + state + intent-relevant facts only
  4. Agent turn           generation with tool calling
  5. Egress guardrail     deterministic checks, then semantic grounding, then repair
  6. Trace                one structured record per turn

Written as an explicit sequence rather than an agent loop deciding its own control
flow. For this product the ordering is a safety property — crisis detection must
precede generation, and verification must precede delivery — and a property you
want guaranteed does not belong in a model's discretion.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings, offline_mode
from app.guardrails import egress
from app.guardrails.grounding import GroundingVerifier, repair_instruction
from app.guardrails.ingress import IngressGuard
from app.guardrails.responses import GROUNDING_FALLBACK, crisis_response
from app.knowledge.retrieval import get_kb
from app.llm import LLMClient, Usage
from app.models import (
    ToolCallTrace,
    TurnTrace,
)
from app.observability import trace as trace_sink
from app.prompts.render import build_messages
from app.router import IntentRouter
from app.session import Session, SessionStore, get_store
from app.tools.registry import TOOL_SCHEMAS, call_tool

MAX_TOOL_ITERATIONS = 4


def _default_llm_factory(usage: Usage) -> Any:
    """Real client, unless OFFLINE_MODE is set.

    Offline mode exists so the pipeline, traces and UI are demoable and testable
    without an API key. It is a test double, not a simulator — response *quality*
    can never be assessed from an offline run, and the eval harness refuses to
    score judged dimensions in this mode.
    """
    if offline_mode():
        from app.testing.fake_llm import FakeLLMClient

        return FakeLLMClient(usage=usage)
    return LLMClient(usage=usage)


@dataclass
class TurnResult:
    response: str
    session_id: str
    trace: TurnTrace
    tool_results: list[Any] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        store: SessionStore | None = None,
        llm_factory: Callable[[Usage], Any] | None = None,
    ) -> None:
        self._store = store or get_store()
        self._settings = get_settings()
        self._kb = get_kb()
        # Injection point for the scripted double (tests, and OFFLINE_MODE demos).
        self._llm_factory = llm_factory or _default_llm_factory
        # Per-session locks; `_locks_guard` protects the dict itself.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── Public entry point ────────────────────────────────────────────────────

    def handle(self, message: str, session_id: str | None = None) -> TurnResult:
        # FastAPI dispatches sync endpoints to a threadpool, so two turns on the
        # same session can interleave `messages.append`, `turn_index += 1` and
        # slot writes on shared mutable state. Serialise per session: turns
        # within one conversation are inherently sequential anyway, and
        # different sessions still run concurrently.
        with self._session_lock(session_id):
            return self._handle_locked(message, session_id)

    def _session_lock(self, session_id: str | None) -> threading.Lock:
        key = session_id or "__new__"
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _handle_locked(self, message: str, session_id: str | None) -> TurnResult:
        session = self._store.get_or_create(session_id)

        usage = Usage()
        llm = self._llm_factory(usage)
        started = time.perf_counter()

        trace = TurnTrace(
            session_id=session.session_id,
            turn_index=session.turn_index,
            timestamp=datetime.now(UTC).isoformat(),
            user_message=message,
        )
        stage_ms: dict[str, float] = {}

        session.add_user(message)
        session.turn_index += 1

        # ── Layer 1: ingress ──────────────────────────────────────────────────
        t0 = time.perf_counter()
        history = "\n".join(
            f"{m['role']}: {m['content']}" for m in session.transcript(limit=6)[:-1]
        )
        crisis = IngressGuard(llm).assess(message, history)
        stage_ms["ingress"] = (time.perf_counter() - t0) * 1000
        trace.crisis = crisis

        if crisis.is_crisis:
            response = crisis_response(crisis.category)
            session.crisis_flagged = True
            return self._finish(
                session, trace, response, usage, stage_ms, started,
                # The scripted reply is reviewed text, not generated output; it is
                # the guardrail's own artifact and is not subject to its checks.
                skip_verification=True,
                delivered_greeting=False,
            )

        # ── Layer 2: routing ──────────────────────────────────────────────────
        t0 = time.perf_counter()
        decision = IntentRouter(llm).route(session, message)
        stage_ms["router"] = (time.perf_counter() - t0) * 1000

        session.subject = decision.subject
        session.last_intent = decision.intent
        trace.intent = decision.intent
        trace.subject = decision.subject

        # ── Layer 3: context assembly ─────────────────────────────────────────
        facts = self._kb.for_intent(decision.intent)
        trace.retrieved_fact_ids = [f.id for f in facts]
        messages = build_messages(session, decision.intent, facts, self._kb)

        # ── Layer 4: generation ───────────────────────────────────────────────
        t0 = time.perf_counter()
        response, tool_results = self._generate(llm, messages, trace)
        stage_ms["generation"] = (time.perf_counter() - t0) * 1000

        # ── Layer 5: egress ───────────────────────────────────────────────────
        t0 = time.perf_counter()
        response = self._verify_and_repair(
            llm, response, messages, facts, tool_results, session, trace
        )
        stage_ms["egress"] = (time.perf_counter() - t0) * 1000

        # The canned replies — generation error, tool-limit, grounding fallback —
        # also carry no greeting or disclosure, so they must not mark them given.
        return self._finish(
            session,
            trace,
            response,
            usage,
            stage_ms,
            started,
            tool_results=tool_results,
            delivered_greeting=not (trace.fallback_used or bool(trace.errors)),
        )

    # ── Layer 4 ───────────────────────────────────────────────────────────────

    def _generate(
        self, llm: LLMClient, messages: list[dict], trace: TurnTrace
    ) -> tuple[str, list[Any]]:
        """Generate a reply, resolving tool calls until the model stops asking.

        Bounded by MAX_TOOL_ITERATIONS. An unbounded loop is a cost and latency
        hazard, and in practice a model still calling tools after four rounds is
        stuck rather than working.
        """
        working = list(messages)
        tool_results: list[Any] = []

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                completion = llm.complete(
                    working, stage="agent", tools=TOOL_SCHEMAS
                )
            except Exception as exc:
                trace.errors.append(f"generation failed: {exc}")
                return (
                    "I'm having trouble on my end at the moment. Could I take your "
                    "number and have someone from our team reach out?",
                    tool_results,
                )

            if not completion.tool_calls:
                return completion.text, tool_results

            working.append(completion.raw_message)

            for call in completion.tool_calls:
                t0 = time.perf_counter()
                result = call_tool(call.function.name, call.function.arguments)
                duration = (time.perf_counter() - t0) * 1000

                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": call.function.arguments}

                trace.tool_calls.append(
                    ToolCallTrace(
                        name=call.function.name,
                        arguments=arguments,
                        result=result,
                        error=None if result.get("ok", True) else str(result.get("error")),
                        duration_ms=duration,
                    )
                )
                tool_results.append({"tool": call.function.name, "result": result})

                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        trace.errors.append("tool iteration limit reached")
        return (
            "Let me get a team member to help with this — could I grab the best "
            "number to reach you?",
            tool_results,
        )

    # ── Layer 5 ───────────────────────────────────────────────────────────────

    def _verify_and_repair(
        self,
        llm: LLMClient,
        response: str,
        messages: list[dict],
        facts: list,
        tool_results: list[Any],
        session: Session,
        trace: TurnTrace,
    ) -> str:
        verifier = GroundingVerifier(llm)

        for attempt in range(self._settings.max_repair_attempts + 1):
            deterministic = egress.check(
                response,
                kb=self._kb,
                facts=facts,
                tool_results=tool_results,
                crisis=None,
                already_greeted=session.greeted,
            )
            grounding = verifier.verify(
                response, kb=self._kb, facts=facts, tool_results=tool_results
            )

            trace.deterministic_verdict = deterministic
            trace.grounding_verdict = grounding

            blocking = list(deterministic.blocking)
            if grounding is not None:
                blocking.extend(grounding.blocking)

            if not blocking:
                return response

            if attempt >= self._settings.max_repair_attempts:
                break

            trace.repair_attempts = attempt + 1
            try:
                repaired = llm.complete(
                    [
                        *messages,
                        {"role": "assistant", "content": response},
                        {"role": "system", "content": repair_instruction(blocking)},
                    ],
                    stage="repair",
                    temperature=0.2,
                )
                response = repaired.text or response
            except Exception as exc:
                trace.errors.append(f"repair failed: {exc}")
                break

        # Repair budget exhausted. Fall back rather than deliver an unverified
        # claim — a second failure implies the fact is missing from the knowledge
        # base, which is a handoff condition, not something a third try fixes.
        trace.fallback_used = True
        return GROUNDING_FALLBACK

    # ── Layer 6 ───────────────────────────────────────────────────────────────

    def _finish(
        self,
        session: Session,
        trace: TurnTrace,
        response: str,
        usage: Usage,
        stage_ms: dict[str, float],
        started: float,
        *,
        skip_verification: bool = False,
        tool_results: list[Any] | None = None,
        delivered_greeting: bool = True,
    ) -> TurnResult:
        if skip_verification:
            # Scripted crisis text is the guardrail's own reviewed artifact, so it
            # is not subject to grounding. It IS still audited for the required
            # resource numbers — that turns REQUIRED_CRISIS_TOKENS into a live
            # assertion, so an editing mistake that dropped 988 from a template
            # would surface in the trace rather than silently shipping.
            trace.deterministic_verdict = egress.check(
                response,
                kb=self._kb,
                facts=[],
                tool_results=[],
                crisis=trace.crisis,
                already_greeted=session.greeted,
            )
            if not trace.deterministic_verdict.passed:
                trace.errors.append(
                    "crisis template failed its own resource-token audit"
                )

        session.add_assistant(response)
        # Only a generated reply carries the greeting and the recording
        # disclosure. Setting these unconditionally marked them delivered on
        # crisis short-circuits, error replies and grounding fallbacks — so a
        # conversation that opened with a crisis would never get the disclosure
        # at all, because the next turn was told it had already been given.
        if delivered_greeting:
            session.greeted = True
            session.disclosed = True
        self._sync_slots(session, tool_results or [])
        self._store.save(session)

        stage_ms["total"] = (time.perf_counter() - started) * 1000
        trace.final_response = response
        trace.latency_ms = {k: round(v, 1) for k, v in stage_ms.items()}
        trace.tokens = {
            "prompt": usage.prompt_tokens,
            "completion": usage.completion_tokens,
            "calls": usage.calls,
            **{f"stage:{k}": v for k, v in usage.by_stage.items()},
        }
        trace.estimated_cost_usd = round(usage.cost_usd, 6)

        trace_sink.emit(trace)
        return TurnResult(
            response=response,
            session_id=session.session_id,
            trace=trace,
            tool_results=tool_results or [],
        )

    @staticmethod
    def _sync_slots(session: Session, tool_results: list[Any]) -> None:
        """Mirror tool-captured fields into session slots.

        This is what makes "never re-ask for something already given" enforceable
        — the next turn's prompt lists what we hold, so the model isn't relying on
        re-reading the transcript to notice.
        """
        for entry in tool_results:
            result = entry.get("result", {})
            if not result.get("ok"):
                continue
            saved = result.get("saved") or {}
            if isinstance(saved, dict):
                session.remember(**saved)
            if entry.get("tool") == "book_tour" and result.get("ok"):
                slot = result.get("slot")
                if isinstance(slot, dict):
                    session.booked_tours.append(slot)
            if entry.get("tool") == "escalate_to_human":
                session.escalated = True
