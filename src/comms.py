"""
Fan-message templates.

Variables come from the Case or Decision, and TEMPLATE_VALID_FOR binds each
template to the actions it may describe, so a message cannot claim something
the decision did not authorise. llm.polish rewrites wording only.
"""

from __future__ import annotations

from .schema import (
    Case,
    Decision,
    FanTier,
    ResolutionAction,
    RouteDecision,
    TimeBucket,
)


# --------------------------------------------------------------------------
# Channel selection
# --------------------------------------------------------------------------

def choose_channel(case: Case) -> str:
    """Urgency beats stated preference: push needs the app foregrounded."""
    if case.report.at_venue or case.time_bucket in {
        TimeBucket.T_MINUS_CRITICAL, TimeBucket.IN_EVENT
    }:
        return "sms"
    if case.report.channel in {"app_chat", "sms", "email"}:
        return case.report.channel if case.report.channel != "app_chat" else "push"
    return case.fan.preferred_channel


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

def _name(case: Case) -> str:
    n = (case.fan.display_name or "").strip()
    return n.split()[0] if n and n.lower() != "there" else "there"


def _at(case: Case, dt, fmt: str) -> str:
    """Venue-local time with its zone, e.g. "07:10 PM PST"."""
    return case.event.local(dt).strftime(fmt + " %Z")


def _event_line(case: Case) -> str:
    return f"{case.event.name} at {case.event.venue}"


TEMPLATES = {

    "delivery_resend_link": lambda c, d: (
        f"Hi {_name(c)} — your {c.order.quantity} tickets for {_event_line(c)} are "
        f"issued and we've just re-sent the transfer link to the email on your order. "
        f"Open it on the phone you'll use at the gate and accept the transfer. "
        f"If they still aren't showing in 10 minutes, reply here and we'll reissue them."
    ),

    "delivery_reissue": lambda c, d: (
        f"Hi {_name(c)} — we found the problem with your {c.order.quantity} tickets for "
        f"{_event_line(c)} and have reissued them. They're in the app now under "
        f"My Tickets. Your original barcodes are void, so use the new ones at the gate."
    ),

    "delivery_reissue_urgent": lambda c, d: (
        f"{_name(c)} — your {c.order.quantity} tickets are reissued and live in the app "
        f"right now. Open My Tickets and pull up the barcodes before you reach the gate. "
        f"A Fan Ops specialist is on this case too and will call you in the next few "
        f"minutes if anything still looks wrong."
        + (f" We've added ${d.goodwill_credit_usd:.0f} credit to your account for the scare."
           if d.goodwill_credit_usd else "")
    ),

    "scan_failure_agent_incoming": lambda c, d: (
        f"{_name(c)} — we can see the scan issue. A Fan Ops specialist is calling you "
        f"in the next 2 minutes on the number on your order. Step to the side of the "
        f"queue, stay near the gate, and keep your phone unlocked. We've also pushed "
        f"fresh barcodes to the app — try those while you wait."
    ),

    "partial_replacement_sourcing": lambda c, d: (
        f"Hi {_name(c)} — {len(c.order.undelivered_seats)} of your {c.order.quantity} "
        f"tickets for {_event_line(c)} didn't transfer from the seller. The other "
        f"{len(c.order.delivered_seats)} are confirmed and in your app. "
        f"We're sourcing replacement seats in the same section so your group stays "
        f"together, and we'll confirm within the hour. You won't pay anything extra."
    ),

    "partial_refund_undelivered": lambda c, d: (
        f"Hi {_name(c)} — we couldn't deliver "
        f"{len(c.order.undelivered_seats)} of your {c.order.quantity} tickets for "
        f"{_event_line(c)} and it's too close to start time to source replacements. "
        f"We've refunded ${d.refund_amount_usd:,.2f} for those seats to your original "
        f"payment method, plus ${d.goodwill_credit_usd:.0f} in credit. "
        f"Your remaining tickets are valid and ready in the app."
    ),

    "event_cancelled_full_refund": lambda c, d: (
        f"Hi {_name(c)} — {_event_line(c)} has been cancelled by the organiser. "
        f"You don't need to do anything: we're refunding your full "
        f"${d.refund_amount_usd:,.2f} to your original payment method, and it typically "
        f"lands in 5-7 business days. We'll email confirmation the moment it's sent."
    ),

    "event_postponed_notify": lambda c, d: (
        f"Hi {_name(c)} — {_event_line(c)} has been postponed"
        + (f" to {_at(c, c.event.rescheduled_to, '%a %d %b, %I:%M %p')}."
           if c.event.rescheduled_to else " and a new date is being confirmed.")
        + f" Your {c.order.quantity} tickets stay valid for the new date — no action "
          f"needed and nothing to re-download. "
        + (f"{c.event.status_note} " if c.event.status_note else "")
        + "If the new date doesn't work for you, reply here and we'll go through options."
    ),

    "postponed_refund_approved": lambda c, d: (
        f"Hi {_name(c)} — sorry {_event_line(c)} moved. We've approved your refund of "
        f"${d.refund_amount_usd:,.2f} back to your original payment method, typically "
        f"5-7 business days. Your tickets for the rescheduled date have been released."
    ),

    "event_time_changed": lambda c, d: (
        f"Hi {_name(c)} — heads up, {_event_line(c)} is now starting at "
        f"{_at(c, c.event.starts_at, '%I:%M %p')}"
        + (f" ({c.event.status_note})" if c.event.status_note else "")
        + ". Your tickets are unchanged and still in the app. "
        + (f"Gates open at {_at(c, c.event.doors_open_at, '%I:%M %p')}."
           if c.event.doors_open_at else "")
    ),

    "transfer_pending_seller_chase": lambda c, d: (
        f"Hi {_name(c)} — your {c.order.quantity} tickets for {_event_line(c)} are "
        f"confirmed and paid, and we're waiting on the seller to release the transfer. "
        f"We've chased them and you'll get a notification the second it lands. "
        f"If it isn't done 3 hours before doors, we'll source replacement seats at no "
        f"cost to you — you're covered either way."
    ),

    "transfer_agent_working": lambda c, d: (
        f"{_name(c)} — the seller hasn't released your tickets yet and we're now "
        f"contacting them directly. A Fan Ops specialist owns your case and will update "
        f"you within 15 minutes with either the transfer or replacement seats. "
        f"You are covered — you will not be left without tickets or out of pocket."
    ),

    "duplicate_order_refund": lambda c, d: (
        f"Hi {_name(c)} — you're right, we found two orders for the same seats at "
        f"{_event_line(c)}. We've refunded the duplicate (${d.refund_amount_usd:,.2f}) "
        f"to your original payment method. Your first order is untouched and your "
        f"tickets are ready in the app."
    ),

    "escalation_holding": lambda c, d: (
        f"Hi {_name(c)} — thanks for flagging this. Your case needs a specialist rather "
        f"than an automated fix, so a Fan Ops agent has it now and will come back to you "
        f"within {d.sla_minutes} minutes"
        + (" — we've marked it urgent given how close we are to start time."
           if c.time_bucket in {TimeBucket.T_MINUS_CRITICAL, TimeBucket.IN_EVENT}
           else ".")
    ),

    # Never ask the fan for card details here: an inbound support message is
    # exactly the channel a phishing attempt imitates, and the review does not
    # need them.
    "fraud_review_holding": lambda c, d: (
        f"Hi {_name(c)} — your order is going through a standard security check before "
        f"we can release the tickets. Our team is reviewing it now and will come back to "
        f"you within {d.sla_minutes} minutes. You don't need to do anything in the "
        f"meantime."
    ),
}


# When a guard forces the route, the fan hears about the guard, not the rule.
GUARD_TEMPLATE_OVERRIDES = {
    "G02_FRAUD_HOLD": "fraud_review_holding",
}


_ESCALATIONS = frozenset({
    ResolutionAction.ESCALATE_HUMAN,
    ResolutionAction.ESCALATE_URGENT,
    ResolutionAction.HOLD_PENDING_REVIEW,
})


# Without this binding a guard can change the action while the rule's template
# survives, telling the fan about a fix that never happened.
TEMPLATE_VALID_FOR: dict[str, frozenset] = {
    "delivery_resend_link":          frozenset({ResolutionAction.AUTO_RESEND_LINK}),
    "delivery_reissue":              frozenset({ResolutionAction.AUTO_REISSUE}),
    "delivery_reissue_urgent":       frozenset({ResolutionAction.AUTO_REISSUE}),
    "partial_replacement_sourcing":  frozenset({ResolutionAction.REPLACEMENT_TICKETS}),
    "partial_refund_undelivered":    frozenset({ResolutionAction.PARTIAL_REFUND}),
    "event_cancelled_full_refund":   frozenset({ResolutionAction.FULL_REFUND}),
    "postponed_refund_approved":     frozenset({ResolutionAction.FULL_REFUND}),
    "duplicate_order_refund":        frozenset({ResolutionAction.FULL_REFUND}),
    "event_postponed_notify":        frozenset({ResolutionAction.PROACTIVE_NOTIFY,
                                                ResolutionAction.NO_ACTION_INFORMATIONAL}),
    "event_time_changed":            frozenset({ResolutionAction.PROACTIVE_NOTIFY,
                                                ResolutionAction.NO_ACTION_INFORMATIONAL}),
    "transfer_pending_seller_chase": frozenset({ResolutionAction.PROACTIVE_NOTIFY}),
    "transfer_agent_working":        frozenset({ResolutionAction.MANUAL_TRANSFER_ASSIST}),
    # Describe an inbound human rather than a completed system action.
    "scan_failure_agent_incoming":   _ESCALATIONS,
    "escalation_holding":            _ESCALATIONS,
    "fraud_review_holding":          _ESCALATIONS,
}


def select_template(case: Case, decision: Decision, rule_template: str) -> str:
    for hit in decision.guards_hit:
        if hit.guard_id in GUARD_TEMPLATE_OVERRIDES:
            return GUARD_TEMPLATE_OVERRIDES[hit.guard_id]

    if not rule_template:
        return "escalation_holding"

    # If guards changed the action, the rule's template no longer describes
    # what happened.
    permitted = TEMPLATE_VALID_FOR.get(rule_template)
    if permitted is not None and decision.action not in permitted:
        return "escalation_holding"

    if decision.route == RouteDecision.ESCALATE and decision.action in _ESCALATIONS:
        if rule_template not in TEMPLATE_VALID_FOR or \
                decision.action not in TEMPLATE_VALID_FOR[rule_template]:
            return "escalation_holding"

    return rule_template


def render(case: Case, decision: Decision, template_key: str) -> str:
    fn = TEMPLATES.get(template_key)
    if fn is None:
        return TEMPLATES["escalation_holding"](case, decision)
    try:
        msg = fn(case, decision)
    except Exception:
        msg = TEMPLATES["escalation_holding"](case, decision)

    if case.fan.tier == FanTier.VIP and decision.route != RouteDecision.ESCALATE:
        msg += " (You're on our priority list, so this is being handled first.)"

    return " ".join(msg.split())


def tone_hint(case: Case) -> str:
    bits = []
    if case.report.at_venue:
        bits.append("Fan is physically at the venue right now — be extremely brief and directive.")
    if case.time_bucket == TimeBucket.T_MINUS_CRITICAL:
        bits.append(f"Only {case.minutes_to_event:.0f} minutes to start time.")
    if case.fan.prior_contacts_this_order_24h >= 2:
        bits.append("Fan has contacted us repeatedly — acknowledge that directly, do not restart from scratch.")
    if case.fan.tier == FanTier.NEW:
        bits.append("First-time buyer — do not assume they know how mobile transfer works.")
    return " ".join(bits)
