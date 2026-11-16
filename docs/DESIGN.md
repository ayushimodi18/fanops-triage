# Design

Architecture, data flow, and the reasoning behind the decision table.

## 1. Data flow

```
raw case dict
    │
    ├─ TriageEngine.build_case()          pipeline.py
    │     joins InboundReport + OrderContext + FanContext + EventContext
    │     classifies raw_text -> issue_type, confidence, source   (llm.py)
    │
    ├─ match_rule(case)                   policy.py
    │     ordered table, first match wins -> RuleResult
    │
    ├─ run_guards(case, candidate_action) policy.py
    │     10 guards -> GuardHit list; may veto automation or force a route
    │
    ├─ confidence.score() / .route()      confidence.py
    │     5 factors -> final confidence -> auto / recommend / escalate
    │
    ├─ comms.select_template() / .render()  comms.py
    │     template bound to the final action; optional llm.polish()
    │
    ├─ AuditLog.decision()                audit.py
    │     append-only JSONL, stamped with policy and event-state versions
    │
    └─ Executor.execute()                 execution.py   (on approval only)
          stubbed side effects, idempotent on
          (order_id, action, event_state_version)
```

`EventStateStore` (event_state.py) owns event state and notifies subscribers on
change. `PendingQueue` subscribes to it and re-evaluates decisions waiting for
human approval when their event's version bumps.

## 2. The case contract

Everything the policy engine can see is defined in `src/schema.py`. Fields
split into two categories:

- **Case-time** (`InboundReport`) — what the fan told us. Signal, not truth.
- **System-time** (`OrderContext`, `FanContext`, `EventContext`) — what our
  records say now. Rules are written against these.

| Field | Source | Role |
|---|---|---|
| `issue_type` | classifier | Selects the playbook. `unknown` is a valid value. |
| `minutes_to_event` → `time_bucket` | derived | Every rule is gated on it |
| `ticket_status` | orders service | Corroborates or contradicts the fan's claim |
| `fan.tier` | fan service | Changes queue and tone, never the action |
| `prior_contacts_this_order_24h` | contact history | Evidence our last automated answer was wrong |
| `prior_auto_resolutions_this_order` | audit log | Transient vs systemic failure |
| `fraud_status` | trust & safety | Hard veto on automation |
| `order_total_usd` | orders service | Auto-approval ceiling |
| `event.status` | event feed | Event-level state |
| `event.state_version` / `fetched_at` | state store | Freshness contract |
| `undelivered_seats` / `delivered_seats` | derived | Per-seat handling of partial failures |
| `payment_settled` | payments | Cannot refund an uncaptured charge |
| `can_still_enter` | derived | After last entry, reissue is pointless |

Two derived fields encode judgment rather than data:

- **`time_bucket`** collapses continuous time into seven regimes.
  `T_MINUS_CRITICAL` (0–90m) is where contact volume spikes and a slow fix
  becomes a missed event.
- **`data_completeness`** is the fraction of decision-critical fields present.
  Below 0.7, `G01` escalates rather than acting on a partial order record.

## 3. Guards and rules

Guards run against the candidate action and can veto automation or force a
route; rules choose the action. Guards never pick an action, they only remove
options.

```
issue_type    ─┐
time_bucket    ┼─▶ RULE (first match wins) ─▶ action + base confidence
ticket_status ─┘                                    │
                                                    ▼
fraud_status  ─┐                              GUARDS (9) ─▶ veto / force route
order_total   ─┤                                    │
contacts_24h  ─┤                                    ▼
state_age     ─┘                    CONFIDENCE (5 factors) ─▶ route
                                                    │
                                                    ▼
                                       comms_template ─▶ fan message
```

Rule order:

- Event-level states dominate order-level states.
- Time-critical branches are checked before their relaxed equivalents.
- Exception: `R05_SCAN_FAILURE` above `R04_TIME_CHANGED`. Event-level dominance
  holds for CANCELLED and POSTPONED, where the ticket is moot, but not for
  DELAYED, where the fan still needs to get in.

### Guard exemptions

`G03` (repeat contact) and `G05` (automation exhausted) stop repeated automated
*fixes*, not *information*. Both exempt `INFORMATIONAL_ACTIONS`, and
`contact_factor` skips the confidence penalty for the same actions — otherwise
the fan who has contacted us three times is the one not told their event
moved.

### Guard behaviours

| Guard | Effect |
|---|---|
| `G01` missing critical data | forces escalate |
| `G02` fraud hold | forces escalate to Trust & Safety |
| `G03` repeat contact ≥ 3 | forces escalate (exempt: informational) |
| `G04` stale event state | blocks auto-execute only |
| `G05` automation exhausted | forces escalate (exempt: informational) |
| `G06` post-event claim | forces escalate |
| `G07` order ≥ `HIGH_VALUE_USD` | blocks auto-execute only |
| `G08` payment unsettled | forces escalate |
| `G09` prior refund on order | forces escalate to senior |
| `G10` reissue outside the safety contract | blocks auto-execute (reissue only) |

`G07` blocks rather than forces: value is a question about the cost of a
mistake and needs a second pair of eyes. `G02` forces because fraud is a
question about legitimacy and needs an investigator.

### Reissue safety contract

`AUTO_REISSUE` voids existing barcodes and mints new ones, so it is only safe
without a human when there is no working barcode to destroy.
`REISSUE_SAFE_STATUSES` in `policy.py` names those statuses (`not_delivered`,
`pending_transfer`, `partially_delivered`, `revoked`, `void`). `G10` is scoped
to `AUTO_REISSUE` via `Guard.applies_to` and blocks auto-execution for any
other status, so automation never voids a ticket the fan may still be able to
scan.

## 4. Edge cases

### Day-of postponement across a whole event

- `PROACTIVE_NOTIFY`, auto-executed, as one broadcast rather than N decisions.
- `incident_sweep()` synthesises a case per affected order when event state
  flips, so fans are told before they write in.
- Dedup key `(order_id, event_state_version)`, held on the engine, so repeat
  sweep calls at the same version send nothing.
- Tickets stay valid on a postponement, so the first move is information, not
  money. If the fan asks for a refund (`R02`) and the promoter's window is
  open it is honoured; if not it escalates rather than the system declining.

### Partial delivery on a multi-ticket order

| Runway | Action | Why |
|---|---|---|
| > 4h | `REPLACEMENT_TICKETS` | Time to source comparable seats and keep the group together |
| < 4h, some seats delivered | `ESCALATE_HUMAN` | Partial refund forces the group to choose who doesn't go; that trade-off is offered by a human |
| < 4h, no seats delivered | `PARTIAL_REFUND` of the affected seats | No group to strand |

The schema carries per-seat `SeatRef` objects rather than one order-level
`ticket_status` because "2 of 4 failed" and "all 4 failed" are different
cases.

### Repeat contact on the same order

- `prior_contacts_this_order_24h` measures fan frustration;
  `prior_auto_resolutions_this_order` measures systemic failure.
- Confidence decays `1.0 → 0.92 → 0.80 → 0.50`.
- The third contact forces a human (`G03`); `G05` fires independently once two
  automated fixes have been applied.
- The ceiling is 3 rather than 2 because two contacts is often a fan following
  up before the first fix landed.

### Fraud-flagged or high-value order

- `G02` forces escalation to Trust & Safety; no confidence score overrides it.
  An automated reissue would hand venue access to a possibly-fraudulent buyer,
  and an automated refund would complete a possible cash-out.
- `G07` (≥ $500) downgrades to human approval instead.
- The fan still gets a holding message; silence produces the repeat contact
  `G03` would then escalate.

### Scan failure at the gate

`ESCALATE_URGENT` to a gate-side queue with a 3-minute SLA, reissue firing in
parallel. The only case where escalation is the correct first action: the fan
cannot fix an app login and re-queue before the gate closes. The parallel
reissue sometimes clears a venue-side cache but is not the resolution.

## 5. Auto-resolve vs escalate

### The allowlist

`AUTO_EXECUTABLE` in `schema.py` holds four actions. Every money-moving action
is excluded at the type level, so no threshold can promote one. The criterion
is reversibility and blast radius, not certainty:

| Auto-executable | Why |
|---|---|
| `AUTO_RESEND_LINK` | Worst case, one extra email |
| `AUTO_REISSUE` | Only destructive if a valid barcode exists to void; the rules producing it fire when one doesn't, and `G10` blocks auto-execution if that is not the case |
| `PROACTIVE_NOTIFY` | Information only |
| `NO_ACTION_INFORMATIONAL` | No side effects |

Never auto-executable: refunds (irreversible), replacement tickets (spends
money), manual transfer assist (needs a phone call), anything touching a second
customer's inventory. `full_refund` on a cancelled event scores 0.97 base
confidence and still routes to human approval.

### Confidence

```
final = rule_base × classification × completeness × freshness × contact_history
```

Every factor is ≤ 1.0, so confidence only decays from the rule's ceiling.

| Factor | Measures |
|---|---|
| `rule_base` | The rule's own ceiling for this playbook |
| `classification` | Did we understand the issue? Explicit signals score 1.0; free-text inference is penalised |
| `completeness` | Do we have the fields? |
| `freshness` | Is our event state current? |
| `contact_history` | Has automation already failed here? |

Corroboration — the fan's report agreeing with our order record — recovers 75%
of the classification penalty, capped at 1.0. It is reported as
`note_corroborated` rather than as a factor, so the displayed factors multiply
to the displayed confidence.

### Routing

| Confidence | Route |
|---|---|
| ≥ 0.85, action in allowlist, no blocking guard | `AUTO_EXECUTE` |
| 0.60 – 0.85, or action outside the allowlist | `RECOMMEND` (one-click approval) |
| < 0.60 | `ESCALATE` with context and candidate actions, no recommendation |

Below 0.60 no recommendation is offered, since a low-confidence suggestion
anchors the agent while carrying little information.

`R07` (delivery failure inside T-90m) lowers its own auto-threshold to 0.75 via
`RuleResult.auto_threshold`, justified by two conditions together: a slow fix
costs the fan the event, and the rule pages a human in parallel regardless.

## 6. Real-time adaptation

Prevents this sequence:

```
14:59:30   poll says event ON_TIME
14:59:45   venue posts a weather delay
15:00:02   we auto-refund a fan whose event was merely delayed 40 minutes
```

### Adaptive polling

| Condition | Interval |
|---|---|
| Active incident (delayed/postponed/cancelled/relocated) | 10s |
| T-90m or less | 15s |
| T-4h to T-90m | 60s |
| T-24h to T-4h | 5m |
| Beyond 24h | 15m |

### Freshness contract

Every `EventContext` carries `fetched_at` and `state_version`; every `Decision`
embeds the version it was made against. `G04` refuses to auto-execute past a
tolerance of roughly 4× the poll interval — one missed poll is survivable,
three are not:

| Window | Tolerance |
|---|---|
| Inside T-90m | 60s |
| T-4h to T-90m | 240s |
| Beyond | 1800s |

`G04` blocks auto-execution but does not force escalation, so the
recommendation is still shown to a human with the staleness warning attached.

### In-flight invalidation

- `PendingQueue` subscribes to the state store.
- On a version bump, each pending decision for that event is flagged,
  re-evaluated against fresh state, and marked with `re_evaluated` and
  `superseded_action` so the console can show that the recommendation moved.
- If re-evaluation fails, the decision stays `stale` and the UI blocks approval.
- Only substantive changes bump the version: `_materially_different` watches
  status, times, `rescheduled_to`, `refund_window_open` and `status_note`, so a
  clock tick alone invalidates nothing.

## 7. LLM boundary

The LLM is not the decision authority. `policy.py` and `confidence.py` contain
no model calls, so a misclassification can only lower confidence and route more
work to humans; it cannot choose an action, set an amount, or promote a route.

- `src/llm.py` resolves a backend from the environment (Anthropic, or any
  OpenAI-compatible endpoint) and falls back to deterministic paths otherwise.
- `complete()` returns `None` on every failure — timeout, rate limit, expired
  key, malformed response — which is the signal to use the fallback.
- Classification falls back to weighted evidence scoring: phrases add weight to
  labels, negation phrases subtract, and confidence comes from the margin
  between the top two labels rather than the total evidence found.
- Message polish falls back to the raw template.
- Since the model only classifies and phrases, swapping models affects
  classification quality on paraphrase and nothing about safety.

## 8. Execution

- Side effects are **stubbed**: `src/execution.py` records what would be sent
  to a ticketing, payment or messaging system and returns. Wiring a real
  integration means replacing one handler body in `_HANDLERS`.
- `Executor.execute()` is idempotent on
  `(order_id, action, event_state_version)`. A repeat call returns
  `duplicate` without re-running the handler. The event-state version is part
  of the key so the same action against moved event state is a new execution,
  not a duplicate.
- Actions outside `AUTO_EXECUTABLE` are rejected here as well as in the router,
  so a hand-built `Decision` cannot execute a refund.
- Every attempt is written to the audit log with its status and key.

## 9. Interface

`streamlit run app.py`. Three tabs: case review, event board, audit.

- Reasoning and guards are always visible rather than behind a toggle.
- Approve is one click; override requires a typed reason, enforced in
  `engine.override()` rather than only in the UI.
- Colour follows gate-scanner semantics: green go, amber check, red deny.
- The audit tab reports override rate per rule, the input to the next
  `POLICY_VERSION` bump.
