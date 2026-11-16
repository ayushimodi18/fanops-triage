# Fan Ops Event-Day Triage

Takes an inbound event-day support case, decides the resolution, drafts the
fan-facing message, and either executes it or hands it to a human with the
reasoning attached.

Plain Python. Runs with no external services.

## Decision philosophy

Five rules the system is built around, in order of precedence:

1. **Deterministic code decides, not the model.** Every action comes from an
   ordered rule table in `src/policy.py`. The LLM classifies inbound text and
   rewrites an approved draft; it never selects an action, sets a refund
   amount, or changes a route. A wrong classification lowers confidence and
   sends more work to humans — it cannot move money or grant venue access.
2. **Guards veto, they never choose.** Ten hard constraints (fraud, high
   value, repeat contact, stale state, unsettled payment, and so on) can block
   automation or force a route, but cannot invent an action of their own.
3. **Only reversible actions auto-execute.** `AUTO_EXECUTABLE` in
   `src/schema.py` holds four actions. Every money-moving action is excluded at
   the type level, so no confidence score can promote one. A cancelled-event
   refund scores 0.97 and still goes to a human.

   To be explicit about refunds: **no refund, partial or full, is ever issued
   without a human approving it**, regardless of confidence, fan tier, or how
   unambiguous the cause is. The system computes the amount and the exposure
   and presents them; a person authorises the money. Refund figures shown on a
   decision are proposed, not executed — `src/execution.py` rejects any
   money-moving action outright, so even a hand-built `Decision` cannot move
   funds.
4. **Confidence measures what we knew, not what will work.** Five factors
   multiply down from the rule's own ceiling; below 0.60 the system offers no
   recommendation at all rather than anchoring the agent with a weak one.
5. **Every decision is explainable and reversible after the fact.** Each one
   names its rule, policy version and event-state version, and lands in an
   append-only audit log. Overrides require a typed reason.

## How it works

Resolution actions come from a deterministic, versioned rules engine. With no
LLM provider configured, classification and message polish both fall back to
deterministic paths and every case is still triaged.

```
inbound case
    ├─ ENRICH      join order, ticket, fan and live event records
    ├─ CLASSIFY    free text → IssueType + confidence      [LLM, keyword fallback]
    ├─ MATCH       ordered rule table → candidate action    [deterministic]
    ├─ GUARD       10 hard constraints veto or force routes [deterministic]
    ├─ SCORE       5 confidence factors → auto / review / escalate
    ├─ DRAFT       grounded template → fan message          [LLM polish, optional]
    └─ AUDIT       append-only decision record
            │
            ▼
        Decision  ──▶  auto-execute   (reversible actions only)
                  ──▶  recommend      (one-click human approval)
                  ──▶  escalate       (human owns it, with full context)
```

## Features

- Ordered rule table (15 rules) with a guard layer (10 guards) that can veto
  automation or force a route.
- Confidence scoring over five factors, routing to auto / review / escalate.
- Money-moving actions are excluded from the auto-executable set at the type
  level, so no confidence score can promote one.
- Live event state with adaptive polling, a staleness contract, and
  re-evaluation of pending decisions when event state changes underneath them.
- Incident sweep: one broadcast per affected order when an event is postponed
  or cancelled, deduped on `(order_id, event_state_version)`.
- Grounded fan-message templates, each bound to the actions it may describe.
- Reissue safety contract: `AUTO_REISSUE` may only auto-execute when no usable
  barcode exists to void. `G10` blocks auto-execution otherwise.
- Idempotent execution layer keyed on
  `(order_id, action, event_state_version)`; a repeat call is a no-op.
- Append-only JSONL audit log with override rate per rule.
- Streamlit console for agents; CLI for batch triage and simulation.

**Side effects are simulated.** `src/execution.py` records what would be sent
to a ticketing, payment or messaging system and returns; no external
integration is wired up. Swapping in a real one means replacing a handler body
— the idempotency and audit behaviour around it is unchanged.

## Setup

```bash
pip install -r requirements.txt
python data/seed.py          # regenerate sample data relative to now
```

## Running

```bash
python run.py                    # triage every sample case
python run.py --case CASE-001    # one case in detail
python run.py --simulate         # event-day timeline: staleness, postponement, sweep
python run.py --md               # write outputs/sample_outputs.md
python run.py --no-llm           # force deterministic mode
python run.py --plain            # no ANSI colour
streamlit run app.py             # agent console
```

`data/seed.py` anchors event times to the run time so cases land in meaningful
time buckets. `--simulate` writes to `data/events.json` and restores it on exit.

## Configuration

> **No API keys are included in this repository.** The keys shown below are placeholders. Copy `.env.example` to `.env` (git-ignored) and fill in your own.

No API key is required. If none is configured, classification uses the keyword
classifier and messages are sent as raw templates. `run.py` prints the active
provider on startup.

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-your-api-key-here

# Any OpenAI-compatible endpoint (Groq, Together, OpenRouter, vLLM, LM Studio,
# Ollama's /v1)
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_API_KEY=your-api-key-here
export LLM_MODEL=<model-name>          # optional

# Force the model off without unsetting keys
export LLM_PROVIDER=deterministic
```

`LLM_TIMEOUT_S` defaults to 8. See `.env.example`.

Tunable constants live at the top of `src/policy.py` (`HIGH_VALUE_USD`,
`REPEAT_CONTACT_CEILING`, `MAX_AUTO_REISSUES_PER_ORDER`) and
`src/confidence.py` (`AUTO_THRESHOLD`, `RECOMMEND_THRESHOLD`). Changing any of
them should mean bumping `POLICY_VERSION`, since every audit row is stamped
with it.

## Testing

```bash
python -m pytest tests/ -q       # full suite
python tests/test_invariants.py  # safety invariants
python tests/test_classifier.py  # offline classifier
python tests/test_llm.py         # provider wiring (mock server, no network)
python tests/test_regressions.py # regressions
```

Each test file also runs standalone without pytest.

## Results on the sample set

| Case | Route | Action | Conf | Notes |
|---|---|---|---|---|
| CASE-001 | auto | `auto_reissue` | 0.87 | Critical window: fix now and page a human |
| CASE-002 | review | `replacement_tickets` | 0.71 | Partial delivery, group kept together |
| CASE-003 | escalate | `escalate_human` | 0.46 | Third contact today |
| CASE-004 | escalate | `escalate_human` | 0.91 | $1,240 under fraud review → Trust & Safety |
| CASE-005 | escalate | `escalate_urgent` | 0.94 | At the gate, 3-minute SLA |
| CASE-007 | escalate | `escalate_human` | 0.36 | Ambiguous text, no recommendation offered |
| CASE-008 | auto | `auto_resend_link` | 0.93 | Backend says delivered |
| CASE-009 | review | `manual_transfer_assist` | 0.79 | VIP, high value, stuck seller |
| CASE-010 | review | `full_refund` | 0.83 | Cancellation: certain, still human-approved |
| CASE-011 | auto | `auto_reissue` | 0.92 | Routine |
| CASE-012 | auto | `auto_resend_link` | 0.93 | Routine |

36% auto-resolved, 27% one-click review, 36% escalated, $0.00 moved without a
human. Figures are from deterministic mode; the sample set over-weights hard
cases.

## Console

`streamlit run app.py`. The console keeps its own sample dataset in
`outputs/live_data/` and regenerates it when it is over an hour old, so event
times stay meaningful on a long-running deployment; `data/` is left alone. Screenshots in [docs/screenshots/](docs/screenshots/).

- Queue sorts escalations first, then by SLA.
- Each decision shows action, rule id, confidence, guards and reasoning;
  "also considered" lists the runner-up rules.
- Approve is one click. Override requires a reason; `engine.override()` raises
  on a blank one, so the rule holds outside the UI too.
- Draft message is editable in place before sending.
- Event board shows status, minutes to gates, state version and snapshot age
  per event, plus the change log. Sidebar controls drive a postponement and
  reset the feed afterwards.
- Audit tab shows override rate per rule and the raw decision log.

## Repository

```
src/schema.py        case contract; if a field isn't here it can't drive a decision
src/policy.py        10 guards + 15 ordered rules, no LLM in this file
src/confidence.py    5-factor scoring and auto/review/escalate routing
src/event_state.py   adaptive polling, freshness contract, in-flight invalidation
src/comms.py         grounded fan-message templates
src/execution.py     idempotent execution of approved actions (stubbed side effects)
src/llm.py           provider resolution, classification, polish, fallbacks
src/audit.py         append-only decision log, override-rate reporting
src/pipeline.py      orchestration and the incident sweep
app.py               Streamlit console
run.py               CLI: batch triage, simulation, markdown export
tests/               test suite
docs/DESIGN.md       architecture and data flow
docs/ASSUMPTIONS.md  assumptions, failure modes, open questions
outputs/             sample_outputs.md and the audit log
```

## Pointing it at real data

Three integration seams:

- **Event feed** — `EventStateStore.fetch_raw()` reads `data/events.json`.
  Replace the body with an HTTP call or a consumer; versioning, change
  detection and subscriber notification are handled by the store.
- **Order and fan records** — `TriageEngine.__init__` loads two dicts. Anything
  validating against `OrderContext` and `FanContext` works.
- **Case intake** — `TriageEngine.triage(raw_case)` takes one dict.

```python
engine = TriageEngine(events_path=..., orders_path=..., fans_path=...)
poller = AdaptivePoller(engine.store); poller.start()   # 15s cadence inside T-90m

for case in inbound_stream():
    decision = engine.triage(case)
    if decision.route == RouteDecision.AUTO_EXECUTE:
        execute(decision)          # reissue/notify side effects
    else:
        queue_for_human(decision)
```

## Limitations

- External side effects are stubbed. `src/execution.py` is idempotent and
  audited, but no ticketing, payment or messaging integration exists.
- `EventStateStore` and `PendingQueue` are in-memory and single-process.
  In-flight invalidation does not work across workers.
- The incident sweep is synchronous and unbatched: one triage call per order.
  It only fires for postponed or cancelled events and retries any order whose
  triage failed.
- Rules do not compose. First match wins, so a compound case gets one playbook.
- The keyword classifier is a fixed phrase table. It tolerates one typo per
  word (words of 5+ letters, first letter must match) and curly apostrophes,
  but misses non-English text and unlisted paraphrases; unmatched messages
  score low and escalate.
- Confidence thresholds (0.85 / 0.60) are asserted, not calibrated against
  outcome data.
- Times are stored and compared in UTC. Fan messages show venue-local time
  (with DST) from each event's `timezone` field; events without one fall back
  to UTC.

More detail in [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md).
