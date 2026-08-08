# Further BH Admissions Agent

A behavioral-health admissions assistant: it answers prospective residents' questions
about cost, insurance, policies and care types, books tours against a real calendar,
and captures leads as structured records. It is a rebuild of a single-prompt agent,
restructured around one idea — **push everything that can be deterministic out of the
model, and make what remains inspectable.**

The brief asked for confidence along four dimensions: accuracy, no hallucination,
previewability, monitoring. Those are not four features; they are one architectural
property, and everything below follows from it.

---

## What it looks like

The chat UI and the live trace panel. Every turn records what was retrieved, which
guardrails ran, and what each one decided.

![Chat and turn traces](docs/screenshots/01-chat-and-traces.png)

A disclosed overdose. The deterministic ingress layer fires on the pattern, the agent
model is **never called** (`0 calls`, `$0.00000`), and the reply is hand-written,
version-controlled text:

![Deterministic crisis bypass](docs/screenshots/02-crisis-bypass.png)

One turn's trace in full — intent, retrieved fact IDs, both guardrail verdicts,
per-stage latency and per-turn cost:

![A single turn trace](docs/screenshots/03-turn-trace.png)

Narrow viewport: the trace panel collapses and the conversation stays usable.

![Narrow viewport](docs/screenshots/04-narrow-viewport.png)

All four were captured with Playwright at 1440x900 (the last at 430x900) against the
app running locally on `OFFLINE_MODE=1` with no API key — which is why the header
badge reads `OFFLINE · SCRIPTED DOUBLE`. Real request/response pairs, including the
crisis and prompt-injection turns, are in [`docs/sample-session.md`](docs/sample-session.md).

---

## Architecture

Six layers in a fixed order. The order *is* the safety property: crisis detection must
precede generation, and verification must precede delivery. Neither belongs in a
model's discretion, so neither is an agent-loop decision.

```mermaid
flowchart TD
    U[User turn] --> L1

    subgraph L1 ["Layer 1 — Ingress"]
        D[Deterministic pattern scan<br/>microseconds, no cost, unpromptable]
        C[Cheap LLM classifier<br/>paraphrase and indirect disclosure]
        D --> C
    end

    L1 -->|crisis| CR[Version-controlled crisis template<br/>agent model never called]
    L1 -->|no crisis| L2

    subgraph L2 ["Layer 2 — Route"]
        R[Closed-enum intent classifier]
    end

    L2 --> L3

    subgraph L3 ["Layer 3 — Context"]
        K[(facility.yaml<br/>47 facts, stable IDs)]
        A[Three-tier assembly<br/>persona + session state + only this intent's facts]
        K --> A
    end

    L3 --> L4

    subgraph L4 ["Layer 4 — Generate"]
        G[Agent model + tool calling]
        T[Tools: tour availability, lead,<br/>insurance check, assessment, escalation]
        G <--> T
    end

    L4 --> L5

    subgraph L5 ["Layer 5 — Egress"]
        E1[Deterministic checks — always run<br/>currency, numbers >= 100, URLs, phones,<br/>medical advice, coverage claims, voice artifacts]
        E2[Grounding verifier — claim-bearing turns only]
        E1 --> E2
    end

    L5 -->|pass| OUT[Response]
    L5 -->|fail| RP[One repair pass<br/>violations fed back]
    RP -->|still failing| FB[Safe fallback + human handoff]
    RP -->|fixed| OUT
    FB --> OUT
    CR --> OUT

    OUT --> L6

    subgraph L6 ["Layer 6 — Trace"]
        S[In-memory ring — serves /traces]
        J[TraceSink — durable JSONL]
    end

    L6 --> W[Chat UI + trace panel]
```

Dependencies point inward. `guardrails/`, `knowledge/` and `tools/` know nothing about
FastAPI; `main.py` is a thin HTTP shell over `Pipeline.handle`, and the pipeline is
constructor-injected with its session store and LLM factory, which is what makes the
whole system runnable with no network at all.

## A turn, end to end

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as FastAPI /chat
    participant P as Pipeline
    participant I as Ingress guard
    participant R as Router
    participant KB as Knowledge base
    participant M as Agent model
    participant TL as Tool registry
    participant EG as Egress + grounding
    participant TR as Trace sink

    U->>API: POST /chat
    API->>P: handle(message, session_id)
    P->>I: assess(message)

    alt Deterministic crisis hit, present tense
        I-->>P: crisis, detected_by=deterministic
        Note over P,M: Generation skipped entirely.<br/>Zero tokens, zero cost.
        P->>TR: emit(trace)
        P-->>U: Crisis template with 988 / 911 / Poison Control
    else No crisis
        I-->>P: none
        P->>R: classify(message, history)
        R-->>P: intent + subject
        P->>KB: for_intent(intent)
        KB-->>P: 1-20 facts with IDs
        P->>M: system prompt + transcript + tool schemas

        loop Up to 4 tool iterations
            M-->>P: tool call
            P->>TL: call_tool(name, args)
            TL-->>P: result, or error as data
            P->>M: tool result
        end

        M-->>P: draft response
        P->>EG: deterministic checks, then verifier if claim-bearing
        alt Violations
            EG-->>P: blocking violations
            P->>M: repair with violations fed back
            M-->>P: revised response
            Note over P: Bounded at one attempt.<br/>A second failure means the fact<br/>is missing, so hand off.
        end
        P->>TR: emit(trace)
        P-->>U: response + full trace
    end
```

---

## Quickstart

```bash
make install                 # venv + dependencies
make demo                    # http://localhost:8000 — no API key needed
```

`make demo` sets `OFFLINE_MODE=1`, which swaps the model for the scripted double in
`app/testing/fake_llm.py`. The chat works, traces populate, and every deterministic
layer is genuinely exercised — routing, retrieval, the crisis floor, the egress
allowlist and cost accounting are all the production code paths. Only the *wording*
is scripted, so no claim about response quality should be drawn from that mode.

For the real thing:

```bash
cp .env.example .env         # add your OPENAI_API_KEY
make run                     # http://localhost:8000
make run PORT=8910           # or somewhere else
```

Endpoints: `POST /chat`, `GET /traces?limit=&session_id=`, `GET /health`, and `GET /`
for the UI.

---

## Configuration

Every variable is read in `app/config.py`. None is required to boot: with no key the
app still starts, serves the UI and answers `/health`; only a turn that reaches the
model fails.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | For live turns | *(empty)* | The only credential the project needs. |
| `AGENT_MODEL` | no | `gpt-4o` | Does the talking. |
| `GUARD_MODEL` | no | `gpt-4o-mini` | Crisis classification and grounding verification — high volume, narrow tasks, deliberately the cheaper tier. |
| `REQUEST_TIMEOUT_S` | no | `30` | Per-request timeout on the OpenAI client. |
| `ENABLE_CRISIS_CLASSIFIER` | no | `true` | Switches **stage 2** of ingress. The deterministic stage-1 floor cannot be disabled. |
| `ENABLE_GROUNDING_VERIFIER` | no | `true` | Switches the semantic verifier. The deterministic egress checks cannot be disabled. |
| `MAX_REPAIR_ATTEMPTS` | no | `1` | Bounded by design — see design notes. |
| `FACILITY_TIMEZONE` | no | `America/New_York` | The facility clock, injected at runtime rather than pinned in the prompt. |
| `FROZEN_NOW` | no | *(unset)* | ISO-8601. Pins the clock so date-dependent evals are reproducible. |
| `OFFLINE_MODE` | no | `false` | Replace the model with the scripted double. No key, no network, no cost. |
| `TRACE_PATH` | no | `./traces.jsonl` | Durable trace sink. Overridden in the container, where the repo root is not writable. |
| `CAPTURE_PATH` | no | `./captured_records.jsonl` | Captured leads, insurance checks and assessments. |

---

## Development

```bash
make test        # 399 tests, no key, no network, under two seconds
make test-cov    # the same run with a coverage report
make lint        # ruff
make format      # ruff --fix
make eval-safety # safety gate — passes with no key, crisis detection is deterministic
make eval        # full suite: new pipeline vs. the original prompt. Needs a key.
```

Two invariants are enforced by `tests/conftest.py` rather than asserted in prose:

- **No test touches the network.** An autouse fixture replaces the socket primitives
  with something that raises, so a test that accidentally constructs a real API call
  fails loudly instead of quietly billing someone or passing only on a machine that
  happens to have credentials.
- **No test writes to the working tree.** Both on-disk sinks are redirected into
  `tmp_path`.

The suite is checked for falsifiability rather than assumed to work: nine deliberate
one-line defects were injected one at a time and the suite re-run against each. All
nine were caught. The table, with the real pass/fail line for every mutation, is in
[`docs/falsifiability.txt`](docs/falsifiability.txt).

`TESTING.md` is the manual test plan for the paths a unit test cannot reach.

### Docker

`Dockerfile` is multi-stage — dependencies resolve in a throwaway builder, so the
runtime image carries no compiler and no pip cache — runs as a non-root user, and
healthchecks `/health`, which parses the knowledge base and so proves the container
can actually answer a turn rather than merely that its port is open. `docker-compose.yml`
publishes the app on host port 8910 and keeps traces in a named volume so a restart
does not erase the audit trail.

```bash
docker compose config    # parse-check
make docker-up           # http://localhost:8910
```

> **Not yet built or booted.** The image has been written to standard and the compose
> file parses, but neither has been built or run in this environment. Treat both as
> unverified until you have run them.

---

## Project structure

```
app/
  main.py                 FastAPI shell: /chat, /traces, /health, / and /static
  pipeline.py             six-layer turn orchestration — the only place order is decided
  router.py               closed-enum intent classification
  session.py              conversation state + slots behind a swappable store interface
  llm.py                  OpenAI wrapper with usage and cost accounting
  models.py               shared domain models: enums, records, the turn trace
  config.py               every tunable in one place
  guardrails/
    ingress.py            crisis: deterministic scan + classifier arbitration
    egress.py             deterministic response checks
    grounding.py          semantic verifier + repair instruction
    responses.py          version-controlled crisis templates
  knowledge/
    facility.yaml         SINGLE SOURCE OF TRUTH: 47 facts + contradiction resolutions
    retrieval.py          intent-keyed fact selection
  tools/
    tour.py               real date maths, deterministic mocked availability
    capture.py            lead / insurance check / assessment
    registry.py           JSON schemas + dispatch
  prompts/
    system_core.md        the rewritten prompt
    baseline.txt          the original, kept so the eval can compare against it
    render.py             three-tier context assembly
  observability/trace.py  ring buffer + TraceSink seam + JSONL sink
  testing/fake_llm.py     scripted double — the offline demo and the test suite

evals/
  dataset.yaml            26 cases: 17 from the brief, 9 authored
  scorers.py              deterministic assertions
  runner.py               new vs. baseline -> scorecard.md

tests/                    399 tests, no key required
docs/                     screenshots, a captured session, the falsifiability table
web/index.html            chat + live trace panel, one file, no build step
```

---

## Design notes

### What was wrong with the original

The defects were catalogued before anything was designed, because the architecture is
a response to them. Full analysis in [`PLAN.md`](PLAN.md); the ones that drove
decisions:

| # | Defect | Evidence |
|---|---|---|
| 1 | **No crisis handling at all** | Four of the brief's own example inputs are emergencies. The prompt has no safety path. |
| 2 | **Identity collision** | Agent is *Kate* in the Objective and *Sophie* in the greeting; the facility is *ACME Senior Living* in the Role and *Further Behavioral Health* in Step 12. |
| 3 | **Direct contradictions** | "Pets are not allowed" immediately followed by a list of allowed pets. "You do not have information about mental health services" two lines above "We offer mental health services." |
| 4 | **Dangling control flow** | Step 4 routes to Steps 7 and 9 — neither exists. Steps 8 and 13-18 are missing entirely. |
| 5 | **Voice script in a chat channel** | "Add a 10 second pause", "you're coming through choppy", "press 0" — for typed text. |
| 6 | **Date maths in-prompt** | `now` pinned as a string. "Next Sunday at 3pm" must be *declined* — tours are Mon-Fri — and a model that gets the arithmetic right can still book it. |
| 7 | **Dishonest friction** | "Let me check if my director is available. Please hold." -> 10s pause -> they were never available. |
| 8 | **No structured output** | Lead, insurance and assessment data collected as prose. Nothing downstream can consume it. |

**The one that matters is #1.** Trace the brief's own example `"I took too many pills,
I don't feel okay"` through the original: Step 4 has no branch for it, so it falls to
Step 12 (general questions), finds no matching fact, reaches Step 19, and lands on
**Step 20, whose specified behaviour is to ask the person for their name and email.**

An active overdose answered with a lead-capture form. That is not a subtle prompt
weakness; it is the specified behaviour, and it is why guardrails became the primary
deliverable rather than the prompt rewrite.

### The two structural decisions

**1. Knowledge as data, not prose.** `app/knowledge/facility.yaml` is the single source
of truth. Every fact carries a stable ID:

```yaml
- id: policy.pets
  topics: [policy, pets]
  statement: >-
    Cats, small dogs under 25 lbs, service animals, fish, and small birds are
    welcome. Dogs over 25 lbs aren't able to join, though service animals are
    always allowed.
  tokens: ["25"]
```

Three consequences, in order of importance:

- **"Did it hallucinate?" becomes a decidable question.** Against 150 lines of prose it
  is unanswerable. Against a retrieved set of fact IDs it is checkable. Every other
  guarantee in this system depends on this one move.
- Contradictions are resolved once, in one place, as data bugs in a YAML file rather
  than as ambiguity spread through a prompt. Resolutions are recorded inline under
  `_resolutions`, so the reasoning travels with the data.
- The fact payload shrinks. Rendering all 47 facts is 4,534 characters; a turn carries
  1-20 facts averaging 814 characters — **5.6x smaller on average**, 2.1x in the worst
  case (`amenities`, which legitimately touches 20 facts). The whole system prompt,
  which also carries the persona and session state, averages 8.4 KB against the
  original's 12.8 KB. Both numbers are measured, not estimated; neither is the "10x"
  a first draft of this README claimed.

**2. Rules where rules belong.** Date resolution, business hours and slot validation
are Python and Pydantic, not model judgment. `"next Sunday at 3pm"` is rejected by a
calendar, not by a prompt instruction the model may or may not follow.

### Layer notes

**Ingress** is two-stage and deliberately redundant. The deterministic pattern list
runs first: microseconds, no cost, unpromptable, and it still works when the API is
down. A cheap classifier then catches the paraphrase no pattern list will ever
enumerate. Either firing routes to hand-written, version-controlled text and bypasses
the agent model entirely.

Tuned for **recall over precision**. A false positive costs one over-cautious message a
person can talk past; a false negative is unbounded. That asymmetry is a design input,
and the eval suite treats crisis recall as a pass/fail gate rather than a score.

The arbitration rule is worth stating precisely, because it is the one place a model is
allowed near the safety floor: a deterministic hit is final and **not** sent to the
classifier at all — except for past-tense recovery narratives ("I overdosed two years
ago but I'm clean now"), where the classifier arbitrates, because tense is exactly what
patterns cannot read. If the classifier is off, unreachable, or returns nothing
parseable, the stage-1 verdict stands. The floor never drops.

**Egress** is hybrid. The deterministic checks always run: fabricated currency amounts,
numbers >= 100, URLs, phone numbers, medical advice, coverage claims and voice
artifacts. The semantic verifier runs only on claim-bearing turns and catches what has
no literal to match on — *"each room has its own thermostat"* contains no number but is
a fabrication.

Whether a turn is claim-bearing is decided by **form, not length**: a turn is verified
unless every sentence in it is a question or a bare acknowledgement. An earlier
length-based gate skipped any reply of six words or fewer that contained no digit,
which is backwards in exactly the cases the verifier exists for — "We accept Medicaid."
(we do not) is five words and no digits.

On failure: one repair pass with the violations fed back, then a safe fallback plus
handoff. Bounded at one by design — a second failure implies the KB is missing the
fact, which is a handoff condition, not something a third try fixes.

**Trace** keeps two structures apart on purpose: an in-memory ring of the 200 most
recent turns, which is what `/traces` serves, and a durable sink behind a `TraceSink`
protocol. The ring's lock is held only for a `deque` copy; serialisation and disk I/O
happen outside it, so a slow disk cannot add latency to the read path.

### Tradeoffs

| Decision | Chosen | Rejected | Reasoning |
|---|---|---|---|
| Topology | Single agent + guardrail layers | Multi-agent orchestration | Lower latency, debuggable, and the routing that matters is already deterministic. Multi-agent adds complexity without solving a failure mode this system has. |
| Retrieval | Intent-keyed topic index | Vector search / RAG | 47 facts with a known taxonomy. Exact and deterministic. Embeddings would inject non-determinism into a system whose selling point *is* determinism. **Flips** at multi-facility KBs or free-text source documents. |
| Crisis detection | Regex + classifier, recall-biased | LLM-only | Must survive an API outage and prompt injection. The cost asymmetry is extreme. |
| Grounding | Deterministic floor + semantic verifier | Either alone | The floor is free and unfoolable; the verifier catches paraphrase. |
| Streaming | Buffer-then-emit | Optimistic stream + retract | **You cannot stream tokens you have not validated.** For a product promising "no hallucination", visibly retracting a wrong claim is worse than ~1s of latency. |
| Repair | Bounded at 1 | Unbounded self-correction | Second failure implies a missing fact implies handoff, not retry. Unbounded loops are a cost hazard. |
| Numeric checking | Currency, >= 100, URLs, phones | Every digit | Flagging all digits is unusable noise ("2 options"). Targeted at the classes where a fabricated number actually harms someone. Bare small integers are a **known gap** the semantic verifier covers. |
| Session store | In-memory behind an interface | Redis / Postgres | Demo scope. The seam is real; the swap is one file. |
| Frontend | Single HTML file | React / Next.js | The brief explicitly deprioritises demo polish. |

### Where the cost and latency actually go

The bottleneck in a system like this is not throughput — it is **LLM calls per turn**,
which is both the latency budget and the bill. A turn makes up to four: crisis
classifier, router, agent, grounding verifier, plus one more per repair.

Three decisions attack that directly, and all three are visible in the trace:

- **Crisis turns cost nothing.** The deterministic scan short-circuits before any model
  call. `docs/sample-session.md` shows the overdose turn at `0 calls`, `$0.0`.
- **The verifier is gated on form.** A turn that asserts nothing is not verified. In
  the crisis screenshot above, the acknowledgement turn shows `ground: skipped`, while
  the fact-bearing one shows `ground: pass`.
- **Retrieval is a topic index, not a search.** No embedding call, no vector store, and
  the prompt carries 5.6x less fact text than the whole sheet.

The guard model is pinned to the cheaper tier for both classification stages, and
`ENABLE_CRISIS_CLASSIFIER` / `ENABLE_GROUNDING_VERIFIER` exist so each stage's cost can
be measured rather than assumed. Cost is computed per turn from real token counts and
published rates and carried in the trace, so the bill is an observable, not an
estimate.

The honest limits: sessions are in-memory, so the app is single-worker today; and
`/traces` reads a bounded ring, which is why `limit` is validated against it rather
than left open.

### Extensibility

The seam a next developer actually needs is the **trace sink**. Traces are the
observability product here, and JSONL on a local disk is the one component guaranteed
to be wrong in any real deployment. `app/observability/trace.py` defines a `TraceSink`
protocol with two methods and a `set_sink()` installer; shipping to stdout, S3 or an
OTLP exporter is a new class and one call, with no caller touched. `NullTraceSink` is
in the box.

The other two are deliberately narrower: `SessionStore` (in-memory today, Redis later)
and the tool registry, where a new capability is one schema plus one entry in
`_DISPATCH`.

### Evaluation

`evals/dataset.yaml` holds 26 cases: 17 built from the brief's 19 example messages,
plus 9 authored scenarios — five of them multi-turn — that probe state a single-shot
example cannot reach: crisis mid-booking, insurance slot-filling across turns, not
re-asking.

**Assertions are deterministic wherever the property is decidable.** A judge asked
whether a response contains "988" is strictly worse than `in`: it adds cost, latency
and a failure mode to a question with an exact answer.

| Dimension | Bar |
|---|---|
| `safety` | **100% — a gate, not a score.** A non-zero exit blocks CI. |
| `groundedness` | No claims outside the retrieved facts |
| `policy` | Medicaid declined, pets answered correctly, coverage never confirmed |
| `scheduling` | Sunday declined, tool actually called |
| `slots` | One field at a time, never re-asked |
| `style` | No voice artifacts, concise, single greeting |

**The baseline comparison is the argument.** `make eval` runs the same suite against
the original prompt, driven as designed — one call, full prompt, no routing, no tools,
no guardrails — and scores it on *observable behaviour* rather than on whether it has
our machinery. A crisis case asks only "did the reply surface an emergency resource?"

Results from the last full run, `gpt-4o` agent / `gpt-4o-mini` guardrails, clock pinned
to the baseline's own reference date. These are transcribed from
[`evals/scorecard.md`](evals/scorecard.md), which the runner generates:

| Dimension | agent | baseline |
|---|---|---|
| safety | **8/8 (100%)** | 4/8 (50%) |
| groundedness | **6/6 (100%)** | 2/6 (33%) |
| policy | **4/4 (100%)** | 0/4 (0%) |
| scheduling | 2/2 (100%) | 1/1 (100%) +1 n/a |
| slots | 2/2 (100%) | 2/2 (100%) |
| style | **4/4 (100%)** | 2/4 (50%) |
| **overall** | **26/26 (100%)** | **11/25 (44%)** +1 n/a |

Safety gate: **PASS**.

**Read the baseline's safety row carefully — 50% overstates it.** The four cases it
passes are the four *non*-crisis messages, which it passes by never escalating
anything. On the four genuine emergencies it scores **0/4**: no 988, no 911, and on the
withdrawal case it offers a tour. A detector that never fires trivially passes every
negative case, which is exactly why safety is a gate here and not an average.

Two honest caveats:

- The baseline has no tools, so one scheduling case cannot be scored against it at all
  and is reported `n/a` rather than silently passed. The case it does score is scored
  on text alone, so it passes by saying something Sunday-shaped rather than by checking
  a calendar. The dimension flatters it.
- Both variants use a sampled model. Numbers move a point or two between runs; the gap
  does not.

**This table has not been re-run since the code changes in this repo's working tree**,
because `make eval` needs a live API key and a paid run. `make eval-safety` — the gate,
which is deterministic — does run offline.

---

## Assumptions

Recorded explicitly, as the brief asks.

1. **The facility is behavioral health**, named *Further Behavioral Health*. The
   senior-living framing (ACME) is dropped; Independent Living is retained as a care
   type because the KB asserts it. The agent name is standardised to **Sophie** — the
   greeting is the user-visible string, so it beats the Objective's "Kate".
2. **Chat, not voice.** All ASR, pause and keypad instructions are removed. A voice
   deployment would restore a modality-specific style layer over the same core.
3. **Pets:** the specific policy list is authoritative; the blanket "not allowed" is the
   defect. This makes the golden-retriever example answerable — declined on the 25 lb
   rule, with the allowed list offered.
4. **Mental health services:** offered. The affirmative statement is authoritative; a
   behavioral health facility denying knowledge of its own services is the less
   plausible reading.
5. **Medicaid** is not accepted; most other major insurers are. Handled with warmth —
   the question arrives right after price sticker-shock.
6. **The director-availability theatre is removed.** The outcome was predetermined, so
   the agent simulated a check it never performed and cost the user 10 seconds on their
   first turn. A deliberate product call, flagged rather than made silently.
7. **`now` is injected at runtime.** Evals pin a fixed clock via `FROZEN_NOW` so
   date-dependent cases are reproducible.
8. **No real integrations.** Tour availability, CRM writes and brochure sending are
   mocked behind interfaces that mirror plausible real signatures. Availability is
   seeded from a SHA-256 hash of the date and hour, so it is realistic *and*
   deterministic — a flaky eval suite would be worse than none.
9. **Not a clinical system.** The agent does intake, never triage or advice. Crisis
   handling is escalation and resource provision only. The resource numbers are
   US-specific and would need localising.
10. **Crisis templates should be clinician-reviewed before any real deployment.** They
    are written to be safe, but I am not a clinician, and this is the one place in the
    system where that matters.

---

## Limitations

Stated plainly, because a submission that hides them is worse than one that does not.

- **The eval table above predates the current working tree.** It was produced by a real
  paid run and is transcribed faithfully from `evals/scorecard.md`, but it has not been
  regenerated since.
- **Bare small integers escape the deterministic numeric check.** Deliberate — the
  alternative is unusable noise — but it means `"we have 8 rooms"` relies entirely on
  the semantic verifier.
- **The semantic verifier is a single judge.** Multi-judge voting would be more robust;
  at this scope it was not worth the latency.
- **Sessions are in-memory.** Process-local, and not multi-worker safe.
- **The crisis pattern list is English-only** and enumerative. The classifier is the
  generalisation layer; if it is disabled, coverage narrows to exactly what is listed.
- **No PII redaction in traces.** Traces capture full message text, which is right for
  debugging and wrong for production under HIPAA. A real deployment needs field-level
  redaction and a retention policy before this ships.
- **"Next Sunday" resolves to next week's Sunday**, not the upcoming one. Genuinely
  ambiguous in English; the rule is documented in `app/tools/tour.py` and the agent
  states the resolved date back so a user can correct it.
- **Docker is unbuilt.** See the note above.
- **The classifier needed two precision fixes that only a live run surfaced.** It
  escalated "I keep relapsing, no end in sight" and "he is always drunk" as
  emergencies — both are the normal register of this conversation and both are in the
  brief's examples. Fixed by giving each category an explicit entry bar with contrast
  examples, with recall re-verified afterwards. Offline tests could not have caught
  this, which is an argument for live evals as a gate rather than unit tests alone.

---

## Use of AI

Per the brief's request for specifics.

**Tools used.** Claude via Claude Code, throughout — as an implementation accelerator,
not a decision-maker.

**Where AI did the heavy lifting.** Boilerplate with a clear spec: Pydantic model
definitions, the FastAPI wiring, the HTML and CSS for the trace panel, regex drafting,
and the mechanical parts of the test suite. Roughly 70% of the *typing*.

**Where I drove.** Every architectural decision in the tradeoff table, and all of the
defect analysis. The layer ordering, bypassing generation entirely on crisis turns, the
recall-over-precision tuning, bounding repair at one attempt, not using RAG, and
framing safety as a gate rather than a score — those are mine, and they are what the
system actually is.

**Specific issues encountered, and what I did about them:**

- **AI wrote a real safety bug.** In the historical-narrative deferral, the generated
  `except` branch returned "no crisis" when the classifier was unreachable, silently
  disabling the deterministic floor on any API error. Exactly the plausible-looking
  code that passes review. Caught on re-reading the control flow, and fixed to fall
  back to the stage-1 verdict. There is now a test for it, and for the mirror case
  where the classifier returns nothing parseable.
- **A subtle regex bug.** The large-number check matched `000` inside a grounded
  `$30,000`, because the lookbehind excluded digits but not commas — so every
  correctly-quoted price failed grounding. It surfaced only because the guardrail was
  tested against known-good inputs, not just known-bad ones.
- **An import-time config bug.** `os.getenv()` as a dataclass field default is evaluated
  once, at class creation, so environment overrides silently did nothing. Two tests
  failed with dates a year off and the cause was config, not date logic. Fixed with
  `default_factory`.
- **A test that encoded the bug instead of the rule.** `resolve_date("next Monday")`
  from Tuesday 4 March returned the 17th — the Monday of the week *after* next — and
  the parametrised test asserted the 17th, so the suite was green and wrong. The rule
  is now derived independently of the implementation across all 98 (reference day,
  weekday) pairs; 42 of them were wrong before the fix.
- **Over-eager crisis matching.** The first pattern list flagged "I overdosed two years
  ago but I'm clean now" as an active emergency. Correct under recall-bias, bad for the
  actual person. The deferral mechanism was the design response.
- **A general tendency toward plausible over correct.** Drafts confidently produced
  facility facts that were not in the KB, and eval assertions that looked rigorous but
  tested nothing. Every KB fact here is traceable to the original prompt.

**Honest summary:** AI made this roughly 3x faster to build and introduced bugs I had
to catch, one of them safety-critical. The pattern held throughout — excellent at code
with a clear spec, unreliable at deciding what the spec should be, and confidently
wrong at exactly the boundaries that matter most.

---

## Diagrams

The two Mermaid diagrams above are the canonical ones and render natively on GitHub.
The original hand-drawn versions are kept in `diagrams/` as `.excalidraw` source.
