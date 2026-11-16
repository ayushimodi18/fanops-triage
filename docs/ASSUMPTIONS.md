# Assumptions and Limitations

## Assumptions

| Assumption | If wrong |
|---|---|
| Resale marketplace: many delivery failures start with a seller who hasn't transferred | `TRANSFER_STUCK` and the seller-chase path don't fit primary issuance |
| We can void a barcode and mint a new one | `AUTO_REISSUE`, the most-used auto action, doesn't work with third-party-only transfer (Ticketmaster/AXS) |
| $500 is a sensible auto-approval ceiling | Should come from the order-value distribution, roughly p90 |
| All-sales-final by default, with service-failure exceptions | `R14` escalates bare refund requests; a more generous policy adds avoidable escalation volume |
| Goodwill up to $50 needs no approval | Needs sign-off from finance |
| Order, ticket, event and fan data are readable synchronously with low latency | A slow call needs a pre-warmed cache to keep the T-90m window usable |
| Contact history is queryable per-order, not just per-fan | `G03` and `G05` get much blunter |
| The event feed reports status changes rather than us inferring them | Inference from scan-rate or social signals is a much harder problem, not attempted |
| `payment_settled` is available at triage time | `G08` becomes dead code |
| Agents work a queue, not chat-per-agent | The console is built around triaging a list |
| A gate-side path can reach a venue box office within 3 minutes | `R05`'s SLA is a promise about a human process, not software |
| Someone reads the override log | Policy never improves |

## Limitations

- **Rules don't compose.** First match wins, so a case that is both a partial
  delivery failure and inside the critical window gets `R06`, not a blend.
- **The offline classifier is a fixed phrase table.** Typos, non-English text
  and unlisted paraphrases miss; they score below 0.35 and escalate rather than
  acting wrongly.
- **No idempotency on execution.** The side-effect layer is a stub; the same
  case submitted twice triages twice. Needs a dedup key on
  `(order_id, issue_type, event_state_version)`.
- **The incident sweep is synchronous and unbatched.** 4,000 orders means 4,000
  sequential triage calls; needs a work queue and send-rate limiting.
- **Single-process state.** `EventStateStore` and `PendingQueue` are in-memory,
  so in-flight invalidation stops working across workers.
- **No feedback loop.** The override log is displayed but nothing consumes it.
- **Confidence thresholds are asserted, not calibrated.** 0.85 and 0.60 are
  chosen numbers; per-rule agreement data would set them.
- **UTC internally, venue-local for fans.** Scheduling and staleness use UTC;
  fan messages render times in the event's IANA `timezone` (DST-aware). An
  event feed that omits `timezone` falls back to UTC.
- **Clock skew against the venue feed** makes `G04` fire constantly or never,
  since staleness uses our clock.
- **One order per case.** A fan with two orders for the same event is not
  modelled.
- **Fees are ignored in partial refunds.** `affected_value_usd` sums face
  values only.
- **An event-feed outage** climbs staleness, so `G04` blocks all
  auto-execution and every case routes to humans.

## Next, in order

1. Shadow mode: full triage, zero execution, then per-rule thresholds from
   agreement rates.
2. Idempotent execution layer with a dedup key.
3. Move the store and pending queue to Redis so invalidation survives multiple
   workers.
4. Batch the incident sweep with a work queue and send-rate limiting.
5. Weekly job surfacing rules above a 25% override rate with reason clusters.
6. Compound-case handling, sized by what the override log shows.
7. Corpus mining for phrasing coverage; A/B the offline classifier against the
   LLM one.

## Worth challenging

- **Refunds never auto-execute, at any confidence.** Costs deflection on
  cancelled events; mass-cancellation volume argues for a bounded exception.
- **`R07` lowers its own auto-threshold to 0.75.** Justified only because a
  human is paged in parallel; the mechanism invites misuse.
- **The 90-minute critical window** is set to match doors-open, not measured
  contact volume.
