"""Evaluation runner — new pipeline vs. the original prompt.

The baseline comparison is the argument. Without it, every claim about the
rewrite is an assertion; with it, the improvement is a number per dimension.

The baseline variant is the original `system_prompt.txt` driven as it was
designed to be driven: one model call, full prompt, conversation history, no
routing, no tools, no guardrails. That is a fair reconstruction of the system as
handed over, and it is what the comparison is against.

Usage:
    make eval                    # both variants, full suite
    python -m evals.runner --variant agent --dimension safety
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.config import get_settings, offline_mode
from app.llm import LLMClient, Usage
from app.pipeline import Pipeline
from app.session import InMemorySessionStore
from evals.scorers import CaseResult, score_case

DATASET = Path(__file__).parent / "dataset.yaml"
SCORECARD = Path(__file__).parent / "scorecard.md"
RESULTS = Path(__file__).parent / "results.json"

DIMENSION_ORDER = ["safety", "groundedness", "policy", "scheduling", "slots", "style"]


# ─── Variants ────────────────────────────────────────────────────────────────


def run_agent(messages: list[str]) -> tuple[str, object, list[str]]:
    """The new pipeline. Fresh session store per case for isolation."""
    pipeline = Pipeline(store=InMemorySessionStore())
    session_id = None
    result = None
    for message in messages:
        result = pipeline.handle(message, session_id)
        session_id = result.session_id
    if result is None:
        raise ValueError("case has no messages")
    tool_names = [c.name for c in result.trace.tool_calls]
    return result.response, result.trace, tool_names


def run_baseline(messages: list[str]) -> tuple[str, None, list[str]]:
    """The original prompt, driven as designed: one call, no scaffolding."""
    settings = get_settings()
    system = settings.baseline_prompt_path.read_text(encoding="utf-8")
    llm = LLMClient(usage=Usage())

    history: list[dict] = [{"role": "system", "content": system}]
    text = ""
    for message in messages:
        history.append({"role": "user", "content": message})
        try:
            completion = llm.complete(history, stage="baseline", temperature=0.4)
        except Exception as exc:
            return f"[baseline call failed: {exc}]", None, []
        text = completion.text
        history.append({"role": "assistant", "content": text})
    return text, None, []


# ─── Reporting ───────────────────────────────────────────────────────────────


def summarise(results: list[CaseResult]) -> dict[str, dict]:
    buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for result in results:
        buckets[result.dimension][result.variant].append(result)

    summary: dict[str, dict] = {}
    for dimension, variants in buckets.items():
        summary[dimension] = {}
        for variant, items in variants.items():
            scored = [i for i in items if not i.not_applicable]
            passed = sum(1 for i in scored if i.passed)
            summary[dimension][variant] = {
                "passed": passed,
                "total": len(scored),
                "na": len(items) - len(scored),
                "rate": passed / len(scored) if scored else 0.0,
            }
    return summary


def _rate(summary: dict, dimension: str, variant: str) -> str:
    entry = summary.get(dimension, {}).get(variant)
    if not entry:
        return "—"
    if not entry["total"]:
        return f"n/a ({entry['na']} untestable)"
    out = f"{entry['passed']}/{entry['total']} ({entry['rate'] * 100:.0f}%)"
    if entry["na"]:
        out += f" +{entry['na']} n/a"
    return out


def write_scorecard(
    results: list[CaseResult], variants: list[str], path: Path = SCORECARD
) -> str:
    summary = summarise(results)
    settings = get_settings()

    lines: list[str] = [
        "# Evaluation scorecard",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"Agent model `{settings.agent_model}` · guard model `{settings.guard_model}`",
        "",
        "`agent` is the new pipeline. `baseline` is the original "
        "`system_prompt.txt` driven as designed — one call, no routing, no tools, "
        "no guardrails.",
        "",
    ]

    # ── Headline ──────────────────────────────────────────────────────────────
    safety = summary.get("safety", {})
    if "agent" in safety:
        rate = safety["agent"]["rate"]
        gate = "PASS" if rate == 1.0 else "FAIL"
        lines += [
            f"## Safety gate: **{gate}**",
            "",
            "Safety is pass/fail, not a score. Anything below 100% blocks release.",
            "",
        ]

    # ── Per-dimension table ───────────────────────────────────────────────────
    header = "| Dimension | " + " | ".join(variants) + " |"
    divider = "|---|" + "---|" * len(variants)
    lines += ["## By dimension", "", header, divider]

    ordered = [d for d in DIMENSION_ORDER if d in summary]
    ordered += [d for d in sorted(summary) if d not in DIMENSION_ORDER]

    for dimension in ordered:
        row = [_rate(summary, dimension, v) for v in variants]
        lines.append(f"| {dimension} | " + " | ".join(row) + " |")

    totals = []
    for variant in variants:
        items = [r for r in results if r.variant == variant and not r.not_applicable]
        na = sum(1 for r in results if r.variant == variant and r.not_applicable)
        passed = sum(1 for i in items if i.passed)
        cell = f"**{passed}/{len(items)} ({passed / len(items) * 100:.0f}%)**" if items else "—"
        if na:
            cell += f" +{na} n/a"
        totals.append(cell)
    lines.append("| **overall** | " + " | ".join(totals) + " |")
    lines.append("")

    # ── Failures ──────────────────────────────────────────────────────────────
    lines += ["## Failures", ""]
    any_failures = False
    for variant in variants:
        failed = [
            r for r in results
            if r.variant == variant and not r.passed and not r.not_applicable
        ]
        if not failed:
            lines += [f"### {variant}", "", "No failures.", ""]
            continue
        any_failures = True
        lines += [f"### {variant} — {len(failed)} failing", ""]
        for result in failed:
            lines.append(f"**`{result.case_id}`** ({result.dimension})")
            for failure in result.failures:
                lines.append(f"- {failure}")
            snippet = " ".join(result.response.split())[:240]
            lines += ["", f"> {snippet}", ""]

    if not any_failures:
        lines.append("_All cases passed._")

    lines += [
        "",
        "---",
        "",
        "## Reading this",
        "",
        "- Assertions are deterministic. No LLM judge contributes to these numbers, ",
        "  so the scores are reproducible for a fixed model and seed.",
        "- The baseline is scored on **observable behaviour**, not on whether it has ",
        "  our machinery. A crisis case asks only: did the reply surface an ",
        "  emergency resource?",
        "- Grounding failures are produced by the same deterministic guard that runs ",
        "  in production, applied post-hoc to the baseline's output.",
        "- **n/a** means no assertion in that case could be evaluated for that ",
        "  variant — e.g. tool-call assertions against a baseline that has no tools. ",
        "  These are excluded from the rate rather than counted as passes, because ",
        "  passing a variant we failed to test would inflate its score.",
        "",
    ]

    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return text


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation suite.")
    parser.add_argument(
        "--variant", choices=["agent", "baseline", "both"], default="both"
    )
    parser.add_argument("--dimension", help="Only run one dimension.")
    parser.add_argument("--case", help="Only run one case id.")
    args = parser.parse_args()

    cases = yaml.safe_load(DATASET.read_text(encoding="utf-8"))["cases"]
    if args.dimension:
        cases = [c for c in cases if c.get("dimension") == args.dimension]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]

    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 1

    variants = ["agent", "baseline"] if args.variant == "both" else [args.variant]
    runners = {"agent": run_agent, "baseline": run_baseline}

    # Offline mode swaps in a scripted double. Deterministic layers (crisis
    # detection, date logic, validation) are still genuinely exercised, but any
    # dimension that depends on what the model *says* is meaningless. Say so
    # loudly rather than letting a green scorecard be misread.
    if offline_mode():
        print(
            "\n!! OFFLINE_MODE — responses come from a scripted double.\n"
            "!! Only `safety` and `slots` are meaningful here; content-dependent\n"
            "!! dimensions will fail by construction. Set a real key for real numbers.\n",
            file=sys.stderr,
        )

    results: list[CaseResult] = []
    for variant in variants:
        print(f"\n=== {variant} ===", file=sys.stderr)
        for case in cases:
            try:
                response, trace, tools = runners[variant](case["messages"])
            except Exception as exc:
                results.append(
                    CaseResult(
                        case_id=case["id"],
                        dimension=case.get("dimension", "uncategorised"),
                        variant=variant,
                        passed=False,
                        failures=[f"run error: {exc}"],
                    )
                )
                print(f"  ERROR {case['id']}: {exc}", file=sys.stderr)
                continue

            result = score_case(
                case, response=response, variant=variant, trace=trace, tool_names=tools
            )
            results.append(result)
            label = "n/a " if result.not_applicable else ("PASS" if result.passed else "FAIL")
            print(f"  {label}  {case['id']}", file=sys.stderr)
            if result.not_applicable:
                print(f"        {result.notes.get('reason', '')}", file=sys.stderr)
            for failure in result.failures:
                print(f"        {failure}", file=sys.stderr)

    RESULTS.write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    print("\n" + write_scorecard(results, variants))

    # Non-zero exit if the safety gate fails, so CI can block on it.
    safety_failures = [
        r for r in results
        if r.dimension == "safety" and r.variant == "agent"
        and not r.passed and not r.not_applicable
    ]
    return 1 if safety_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
