# Sample Outputs

Generated 2026-08-15 21:26 UTC · policy v1.3.0 · deterministic

Full decision record per case, as shown in the console.

## CASE-001 — Delivery failure, 45 min to tip-off

**Fan wrote:** _I have nothing in the app for tonight and we're already parked. No barcode, nothing. Tickets are gone._

| | |
|---|---|
| Route | **AUTO_EXECUTE** |
| Action | `auto_reissue` |
| Also fires | `escalate_urgent` |
| Vetoed by guard | — |
| Confidence | 0.87 |
| Rule | `R07_DELIVERY_CRITICAL` |
| Queue / SLA | urgent / 10 min |
| Refund | $0.00 |
| Goodwill | $20.00 |
| Event state | v1, 0s old |

**Reasoning**

- T-35m — inside the critical window.
- Fix and watch, simultaneously: reissue fires immediately because waiting for a human costs the fan the event, AND a human is paged in parallel because if the reissue fails there is no second chance.
- Deliberately does not wait for approval — the cost of a wrong reissue is one voided barcode; the cost of a slow one is a missed event.
- G10 still applies: if the ticket status is outside the reissue safety contract, auto-execution is blocked and a human confirms.
- Confidence 0.87 ≥ 0.75, action is reversible, no guards blocking → executing automatically (rule lowered its own bar from 0.85 to 0.75; a human is paged in parallel).

**Draft fan message** (`delivery_reissue_urgent` via sms)

> Marcus — your 2 tickets are reissued and live in the app right now. Open My Tickets and pull up the barcodes before you reach the gate. A Fan Ops specialist is on this case too and will call you in the next few minutes if anything still looks wrong. We've added $20 credit to your account for the scare.

---

## CASE-002 — Partial delivery, 4 seats, 6h runway

**Fan wrote:** _Only 2 of the 4 tickets showed up in my account. My brother and his wife have nothing. Can you sort this before tonight?_

| | |
|---|---|
| Route | **RECOMMEND** |
| Action | `replacement_tickets` |
| Also fires | `escalate_human` |
| Vetoed by guard | — |
| Confidence | 0.71 |
| Rule | `R06_PARTIAL_DELIVERY` |
| Queue / SLA | standard / 90 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- 2 of 4 seats undelivered; 2 are fine.
- T-5.8h is enough runway to source comparable seats and keep the group seated together.
- Refunding only the broken seats would technically close the case while leaving the fan with a split party — a bad outcome dressed up as a resolution.
- replacement_tickets is outside the auto-executable set — it is either irreversible or affects a party other than this fan. Human approval required regardless of confidence.

**Draft fan message** (`partial_replacement_sourcing` via email)

> Hi Priya — 2 of your 4 tickets for Hozier — Unreal Unearth Tour at The Greek Theatre didn't transfer from the seller. The other 2 are confirmed and in your app. We're sourcing replacement seats in the same section so your group stays together, and we'll confirm within the hour. You won't pay anything extra.

---

## CASE-003 — Third contact today, same order

**Fan wrote:** _This is the third time I'm messaging. You keep saying the tickets are reissued and I still can't find my tickets anywhere._

| | |
|---|---|
| Route | **ESCALATE** |
| Action | `escalate_human` |
| Also fires | — |
| Vetoed by guard | `auto_reissue` |
| Confidence | 0.46 |
| Rule | `R09_DELIVERY_NOT_DELIVERED` |
| Queue / SLA | urgent / 10 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Guards triggered**

- `G03_REPEAT_CONTACT_CEILING` — Fan has contacted us 3x about this order in 24h. Automation has already been tried and did not work. A fourth templated reply is worse than no reply.
- `G05_AUTOMATION_EXHAUSTED` — 2 automated fixes already applied to this order. The failure is systemic, not transient — reissuing a third time repeats a known-broken path.

**Reasoning**

- NOT APPLIED — the rule argued for auto_reissue; a guard vetoed it. The reasoning below is kept for review, not as a description of what was done.
- Ticket status is not_delivered; fan's report is corroborated by our own records, so no ambiguity to resolve.
- Reissue voids old barcodes and mints new ones, but there is no valid barcode to destroy at this status, so the action is effectively additive and carries no refund exposure.
- Inside the G10 reissue safety contract; had the ticket been delivered or scanned, G10 would have blocked auto-execution.
- Route forced to escalate by G03_REPEAT_CONTACT_CEILING.

**Draft fan message** (`escalation_holding` via sms)

> Hi Dan — thanks for flagging this. Your case needs a specialist rather than an automated fix, so a Fan Ops agent has it now and will come back to you within 10 minutes.

---

## CASE-004 — High-value order under fraud review

**Fan wrote:** _I bought 4 pit tickets this morning and they still haven't come through. It's a lot of money, please confirm they're real._

| | |
|---|---|
| Route | **ESCALATE** |
| Action | `escalate_human` |
| Also fires | — |
| Vetoed by guard | `auto_reissue` |
| Confidence | 0.91 |
| Rule | `R09_DELIVERY_NOT_DELIVERED` |
| Queue / SLA | trust_and_safety / 60 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Guards triggered**

- `G02_FRAUD_HOLD` — Order is under_review with Trust & Safety. Automated reissue would hand venue access to a possibly-fraudulent buyer; automated refund would complete a possible cash-out.
- `G07_HIGH_VALUE` — Order value $1,240.00 exceeds the $500 auto-approval ceiling.
- `G08_UNSETTLED_PAYMENT` — Payment has not settled; refunding an uncaptured charge creates a reconciliation break.

**Reasoning**

- NOT APPLIED — the rule argued for auto_reissue; a guard vetoed it. The reasoning below is kept for review, not as a description of what was done.
- Ticket status is not_delivered; fan's report is corroborated by our own records, so no ambiguity to resolve.
- Reissue voids old barcodes and mints new ones, but there is no valid barcode to destroy at this status, so the action is effectively additive and carries no refund exposure.
- Inside the G10 reissue safety contract; had the ticket been delivered or scanned, G10 would have blocked auto-execution.
- Route forced to escalate by G02_FRAUD_HOLD.

**Draft fan message** (`fraud_review_holding` via email)

> Hi Alicia — your order is going through a standard security check before we can release the tickets. Our team is reviewing it now and will come back to you within 60 minutes. You don't need to do anything in the meantime.

---

## CASE-005 — Scan failure at the gate

**Fan wrote:** _We're at the gate and the scanner won't take either ticket, it just shows a red X. Staff told us to contact you._

| | |
|---|---|
| Route | **ESCALATE** |
| Action | `escalate_urgent` |
| Also fires | `auto_reissue` |
| Vetoed by guard | — |
| Confidence | 0.94 |
| Rule | `R05_SCAN_FAILURE` |
| Queue / SLA | gate_side / 3 min |
| Refund | $0.00 |
| Goodwill | $20.00 |
| Event state | v1, 0s old |

**Reasoning**

- Fan is physically at the gate with a failing scan.
- No message-based resolution works here — they cannot read a push notification, sort out an app login and re-queue before the gate closes. This needs a human on a phone with venue box-office contact.
- Reissue fires in parallel because a fresh barcode sometimes clears a venue-side cache, but it is not treated as the resolution.
- escalate_urgent routes straight to a human queue — there is no system action to approve.

**Draft fan message** (`scan_failure_agent_incoming` via sms)

> Tomas — we can see the scan issue. A Fan Ops specialist is calling you in the next 2 minutes on the number on your order. Step to the side of the queue, stay near the gate, and keep your phone unlocked. We've also pushed fresh barcodes to the app — try those while you wait.

---

## CASE-007 — Ambiguous free text

**Fan wrote:** _hey so about my order for saturday — something seems off with it, can someone check?_

| | |
|---|---|
| Route | **ESCALATE** |
| Action | `escalate_human` |
| Also fires | — |
| Vetoed by guard | — |
| Confidence | 0.36 |
| Rule | `R99_UNKNOWN` |
| Queue / SLA | standard / 120 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- Issue could not be classified into a known playbook.
- No rule matched, so there is nothing to be confident about. Escalating with full context beats guessing.
- escalate_human routes straight to a human queue — there is no system action to approve.

**Draft fan message** (`escalation_holding` via push)

> Hi Marcus — thanks for flagging this. Your case needs a specialist rather than an automated fix, so a Fan Ops agent has it now and will come back to you within 120 minutes.

---

## CASE-008 — Backend delivered, fan can't see tickets

**Fan wrote:** _I don't see my tickets in the app for tonight's show. My friend got hers fine._

| | |
|---|---|
| Route | **AUTO_EXECUTE** |
| Action | `auto_resend_link` |
| Also fires | — |
| Vetoed by guard | — |
| Confidence | 0.93 |
| Rule | `R08_DELIVERY_BACKEND_OK` |
| Queue / SLA | standard / 60 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- Backend shows tickets DELIVERED but the fan cannot see them — typically a wrong-account login or an unopened transfer email.
- Resending the link is the cheapest correct action: zero inventory impact, fully reversible, resolves the majority of these. Even a wrong diagnosis here costs nothing but one extra email.
- T-350m leaves room to escalate if it fails.
- Confidence 0.93 ≥ 0.85, action is reversible, no guards blocking → executing automatically.

**Draft fan message** (`delivery_resend_link` via push)

> Hi Nina — your 2 tickets for Hozier — Unreal Unearth Tour at The Greek Theatre are issued and we've just re-sent the transfer link to the email on your order. Open it on the phone you'll use at the gate and accept the transfer. If they still aren't showing in 10 minutes, reply here and we'll reissue them.

---

## CASE-009 — VIP, seller transfer stuck, 45 min out

**Fan wrote:** _The seller hasn't sent the courtside tickets yet and the game starts in under an hour. What's happening?_

| | |
|---|---|
| Route | **RECOMMEND** |
| Action | `manual_transfer_assist` |
| Also fires | `replacement_tickets` |
| Vetoed by guard | — |
| Confidence | 0.79 |
| Rule | `R10_TRANSFER_STUCK_LATE` |
| Queue / SLA | senior / 15 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Guards triggered**

- `G07_HIGH_VALUE` — Order value $720.00 exceeds the $500 auto-approval ceiling.

**Reasoning**

- Transfer still pending at T-35m.
- Seller outreach now requires a phone call, not an automated nudge. If the seller is unreachable the fallback is replacement or refund — a branch the system cannot pick on its own.
- manual_transfer_assist is outside the auto-executable set — it is either irreversible or affects a party other than this fan. Human approval required regardless of confidence.

**Draft fan message** (`transfer_agent_working` via sms)

> Jordan — the seller hasn't released your tickets yet and we're now contacting them directly. A Fan Ops specialist owns your case and will update you within 15 minutes with either the transfer or replacement seats. You are covered — you will not be left without tickets or out of pocket. (You're on our priority list, so this is being handled first.)

---

## CASE-011 — Routine delivery failure, 6h runway

**Fan wrote:** _I haven't received my tickets for tonight yet, nothing in my wallet._

| | |
|---|---|
| Route | **AUTO_EXECUTE** |
| Action | `auto_reissue` |
| Also fires | — |
| Vetoed by guard | — |
| Confidence | 0.92 |
| Rule | `R09_DELIVERY_NOT_DELIVERED` |
| Queue / SLA | standard / 60 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- Ticket status is not_delivered; fan's report is corroborated by our own records, so no ambiguity to resolve.
- Reissue voids old barcodes and mints new ones, but there is no valid barcode to destroy at this status, so the action is effectively additive and carries no refund exposure.
- Inside the G10 reissue safety contract; had the ticket been delivered or scanned, G10 would have blocked auto-execution.
- Confidence 0.92 ≥ 0.85, action is reversible, no guards blocking → executing automatically.

**Draft fan message** (`delivery_reissue` via push)

> Hi Tomas — we found the problem with your 2 tickets for Hozier — Unreal Unearth Tour at The Greek Theatre and have reissued them. They're in the app now under My Tickets. Your original barcodes are void, so use the new ones at the gate.

---

## CASE-012 — Routine transfer confusion, 30h out

**Fan wrote:** _I can't find my ticket for the Mulaney show, is something wrong with my order?_

| | |
|---|---|
| Route | **AUTO_EXECUTE** |
| Action | `auto_resend_link` |
| Also fires | — |
| Vetoed by guard | — |
| Confidence | 0.93 |
| Rule | `R08_DELIVERY_BACKEND_OK` |
| Queue / SLA | standard / 60 min |
| Refund | $0.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- Backend shows tickets DELIVERED but the fan cannot see them — typically a wrong-account login or an unopened transfer email.
- Resending the link is the cheapest correct action: zero inventory impact, fully reversible, resolves the majority of these. Even a wrong diagnosis here costs nothing but one extra email.
- T-1790m leaves room to escalate if it fails.
- Confidence 0.93 ≥ 0.85, action is reversible, no guards blocking → executing automatically.

**Draft fan message** (`delivery_resend_link` via email)

> Hi Marcus — your 2 tickets for John Mulaney — Live at The Masonic are issued and we've just re-sent the transfer link to the email on your order. Open it on the phone you'll use at the gate and accept the transfer. If they still aren't showing in 10 minutes, reply here and we'll reissue them.

---

## CASE-010 — Cancelled event

**Fan wrote:** _I just saw the game is called off. What happens to my money?_

| | |
|---|---|
| Route | **RECOMMEND** |
| Action | `full_refund` |
| Also fires | `proactive_notify` |
| Vetoed by guard | — |
| Confidence | 0.83 |
| Rule | `R01_EVENT_CANCELLED` |
| Queue / SLA | standard / 120 min |
| Refund | $264.00 |
| Goodwill | $0.00 |
| Event state | v1, 0s old |

**Reasoning**

- Event status is CANCELLED (state v1).
- Cancellation is an unambiguous, policy-defined full refund. There is no judgment call to make and no alternative resolution exists.
- Routed for approval rather than auto-executed: refunds are irreversible, so a human confirms even when the rule is certain.
- full_refund is outside the auto-executable set — it is either irreversible or affects a party other than this fan. Human approval required regardless of confidence.

**Draft fan message** (`event_cancelled_full_refund` via push)

> Hi Priya — Sharks vs Kings at SAP Center has been cancelled by the organiser. You don't need to do anything: we're refunding your full $264.00 to your original payment method, and it typically lands in 5-7 business days. We'll email confirmation the moment it's sent.

---
