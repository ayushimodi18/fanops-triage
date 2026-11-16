"""
Deterministic decision core. No LLM runs in this file.

Two layers:
    GUARDS  hard constraints; veto automation or force a route. A guard never
            chooses an action, it only removes options.
    RULES   ordered, first match wins. Each produces a candidate action with a
            base confidence and a rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .event_state import staleness_threshold_seconds
from .schema import (
    Case,
    EventStatus,
    FanTier,
    FraudStatus,
    GuardHit,
    IssueType,
    Queue,
    ResolutionAction,
    RouteDecision,
    TicketStatus,
    TimeBucket,
)

POLICY_VERSION = "1.3.0"

HIGH_VALUE_USD = 500.0
REPEAT_CONTACT_CEILING = 3
MAX_AUTO_REISSUES_PER_ORDER = 2


# ==========================================================================
# GUARDS
# ==========================================================================

@dataclass
class Guard:
    guard_id: str
    predicate: Callable[[Case], bool]
    reason: Callable[[Case], str]
    forces: RouteDecision | None = None
    queue: Queue | None = None
    blocks_auto: bool = True

    # Actions this guard does not police: G03/G05 stop repeated automated
    # fixes, not information.
    exempt_actions: frozenset = frozenset()

    # If set, the guard only applies to these actions (G10 polices reissue).
    applies_to: frozenset | None = None


# Reissue safety contract.
#
# AUTO_REISSUE voids the existing barcodes and mints new ones. That is safe
# only when there is no working barcode to destroy — i.e. the fan cannot
# already be holding something that scans. These are the statuses where that
# holds. Any other status means a reissue could invalidate a ticket the fan is
# about to present at a turnstile, so G10 blocks auto-execution and a human
# confirms.
REISSUE_SAFE_STATUSES = frozenset({
    TicketStatus.NOT_DELIVERED,
    TicketStatus.PENDING_TRANSFER,
    TicketStatus.PARTIALLY_DELIVERED,
    TicketStatus.REVOKED,
    TicketStatus.VOID,
})


def reissue_voids_valid_barcode(case: Case) -> bool:
    """True when a reissue would destroy a barcode the fan can still use."""
    return case.order.ticket_status not in REISSUE_SAFE_STATUSES


def _fraud_active(c: Case) -> bool:
    return c.order.fraud_status in {FraudStatus.UNDER_REVIEW, FraudStatus.FLAGGED}


def _stale(c: Case) -> bool:
    return c.event_state_age_s > staleness_threshold_seconds(c.minutes_to_event)


# Move no money, grant no access, change no inventory.
INFORMATIONAL_ACTIONS = frozenset({
    ResolutionAction.PROACTIVE_NOTIFY,
    ResolutionAction.NO_ACTION_INFORMATIONAL,
})


GUARDS: list[Guard] = [
    Guard(
        guard_id="G01_MISSING_CRITICAL_DATA",
        predicate=lambda c: c.data_completeness < 0.7 or not c.order.seats,
        reason=lambda c: (
            f"Only {c.data_completeness:.0%} of decision-critical fields present. "
            "Acting on an incomplete order record risks reissuing tickets that "
            "were never purchased."
        ),
        forces=RouteDecision.ESCALATE,
        queue=Queue.STANDARD,
    ),
    Guard(
        guard_id="G02_FRAUD_HOLD",
        predicate=_fraud_active,
        reason=lambda c: (
            f"Order is {c.order.fraud_status.value} with Trust & Safety. "
            "Automated reissue would hand venue access to a possibly-fraudulent "
            "buyer; automated refund would complete a possible cash-out."
        ),
        forces=RouteDecision.ESCALATE,
        queue=Queue.TRUST_AND_SAFETY,
    ),
    Guard(
        guard_id="G03_REPEAT_CONTACT_CEILING",
        predicate=lambda c: c.fan.prior_contacts_this_order_24h >= REPEAT_CONTACT_CEILING,
        reason=lambda c: (
            f"Fan has contacted us {c.fan.prior_contacts_this_order_24h}x about this "
            "order in 24h. Automation has already been tried and did not work. "
            "A fourth templated reply is worse than no reply."
        ),
        forces=RouteDecision.ESCALATE,
        queue=Queue.URGENT,
        exempt_actions=INFORMATIONAL_ACTIONS,
    ),
    Guard(
        guard_id="G04_STALE_EVENT_STATE",
        predicate=_stale,
        reason=lambda c: (
            f"Event state is {c.event_state_age_s:.0f}s old, past the "
            f"{staleness_threshold_seconds(c.minutes_to_event)}s tolerance for "
            f"T-{c.minutes_to_event:.0f}m. Decision could be based on a "
            "superseded schedule."
        ),
        forces=None,       # blocks auto-execute; recommendation still shown
        blocks_auto=True,
    ),
    Guard(
        guard_id="G05_AUTOMATION_EXHAUSTED",
        predicate=lambda c: c.fan.prior_auto_resolutions_this_order >= MAX_AUTO_REISSUES_PER_ORDER,
        reason=lambda c: (
            f"{c.fan.prior_auto_resolutions_this_order} automated fixes already "
            "applied to this order. The failure is systemic, not transient — "
            "reissuing a third time repeats a known-broken path."
        ),
        forces=RouteDecision.ESCALATE,
        queue=Queue.URGENT,
        exempt_actions=INFORMATIONAL_ACTIONS,
    ),
    Guard(
        guard_id="G06_POST_EVENT_CLAIM",
        predicate=lambda c: c.time_bucket == TimeBucket.POST_EVENT,
        reason=lambda c: (
            "Event has ended. Retroactive claims require scan-log verification "
            "that the fan did not attend — not something to infer from a message."
        ),
        forces=RouteDecision.ESCALATE,
        queue=Queue.STANDARD,
    ),
    Guard(
        guard_id="G07_HIGH_VALUE",
        # Reads HIGH_VALUE_USD at call time rather than a constant baked into
        # schema.py. Previously the predicate used a hardcoded 500.0 while the
        # reason string formatted HIGH_VALUE_USD, so retuning the ceiling
        # produced a guard that fired at the old value and then printed a
        # sentence contradicting itself.
        predicate=lambda c: c.order.order_total_usd >= HIGH_VALUE_USD,
        reason=lambda c: (
            f"Order value ${c.order.order_total_usd:,.2f} exceeds the "
            f"${HIGH_VALUE_USD:,.0f} auto-approval ceiling."
        ),
        forces=None,
        blocks_auto=True,
    ),
    Guard(
        guard_id="G08_UNSETTLED_PAYMENT",
        predicate=lambda c: not c.order.payment_settled,
        reason=lambda c: "Payment has not settled; refunding an uncaptured charge creates a reconciliation break.",
        forces=RouteDecision.ESCALATE,
        queue=Queue.STANDARD,
    ),
    Guard(
        guard_id="G09_DOUBLE_REFUND",
        predicate=lambda c: c.order.prior_refund_on_order,
        reason=lambda c: "A refund has already been issued on this order. Second refund needs human reconciliation.",
        forces=RouteDecision.ESCALATE,
        queue=Queue.SENIOR,
    ),
    Guard(
        guard_id="G10_REISSUE_VOIDS_VALID_BARCODE",
        predicate=reissue_voids_valid_barcode,
        reason=lambda c: (
            f"Ticket status is {c.order.ticket_status.value}, so a reissue would "
            "void a barcode the fan may still be able to use. Outside the "
            "reissue safety contract; a human confirms."
        ),
        forces=None,
        blocks_auto=True,
        # Only reissue destroys barcodes; every other action is unaffected.
        applies_to=frozenset({ResolutionAction.AUTO_REISSUE}),
    ),
]


def run_guards(case: Case, candidate_action: ResolutionAction | None = None) -> list[GuardHit]:
    """`candidate_action=None` evaluates every guard that is not action-scoped."""
    hits: list[GuardHit] = []
    for g in GUARDS:
        if candidate_action is not None and candidate_action in g.exempt_actions:
            continue
        if g.applies_to is not None and candidate_action not in g.applies_to:
            continue
        try:
            if g.predicate(case):
                hits.append(GuardHit(
                    guard_id=g.guard_id,
                    reason=g.reason(case),
                    forces=g.forces,
                    blocks_auto=g.blocks_auto,
                ))
        except Exception as exc:  # a broken guard fails closed
            hits.append(GuardHit(
                guard_id=g.guard_id,
                reason=f"Guard evaluation error ({exc}); failing closed to human review.",
                forces=RouteDecision.ESCALATE,
                blocks_auto=True,
            ))
    return hits


def guard_queue(hits: list[GuardHit]) -> Queue | None:
    order = [Queue.TRUST_AND_SAFETY, Queue.SENIOR, Queue.URGENT, Queue.STANDARD]
    found = []
    for h in hits:
        for g in GUARDS:
            if g.guard_id == h.guard_id and g.queue:
                found.append(g.queue)
    for q in order:
        if q in found:
            return q
    return None


# ==========================================================================
# RULES
# ==========================================================================

@dataclass
class RuleResult:
    action: ResolutionAction
    base_confidence: float
    rationale: list[str]
    secondary: list[ResolutionAction] = None
    refund_usd: float = 0.0
    goodwill_usd: float = 0.0
    queue: Queue = Queue.STANDARD
    sla_minutes: int = 240
    comms_template: str = ""

    # Overrides confidence.AUTO_THRESHOLD for this rule only. Money-moving
    # actions are excluded from AUTO_EXECUTABLE, so no threshold reaches them.
    auto_threshold: float | None = None

    def __post_init__(self):
        if self.secondary is None:
            self.secondary = []


@dataclass
class Rule:
    rule_id: str
    description: str
    matches: Callable[[Case], bool]
    resolve: Callable[[Case], RuleResult]


# ---- helpers -------------------------------------------------------------

def _goodwill_for(case: Case) -> float:
    """Scales with disruption severity and tenure, capped at $50."""
    if case.time_bucket == TimeBucket.T_MINUS_CRITICAL:
        base = 20.0
    elif case.time_bucket in {TimeBucket.IN_EVENT, TimeBucket.POST_LAST_ENTRY}:
        base = 35.0
    else:
        base = 10.0
    multiplier = {FanTier.NEW: 1.0, FanTier.STANDARD: 1.0,
                  FanTier.FREQUENT: 1.25, FanTier.VIP: 1.5}[case.fan.tier]
    return round(min(base * multiplier, 50.0), 2)


def _tier_queue(case: Case, default: Queue) -> Queue:
    if case.fan.tier == FanTier.VIP and default in {Queue.STANDARD, Queue.URGENT}:
        return Queue.SENIOR
    return default


# ---- rule definitions ----------------------------------------------------

def _r_event_cancelled(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.FULL_REFUND,
        base_confidence=0.97,
        rationale=[
            f"Event status is CANCELLED (state v{c.event.state_version}).",
            "Cancellation is an unambiguous, policy-defined full refund. There is "
            "no judgment call to make and no alternative resolution exists.",
            "Routed for approval rather than auto-executed: refunds are "
            "irreversible, so a human confirms even when the rule is certain.",
        ],
        refund_usd=c.order.order_total_usd,
        secondary=[ResolutionAction.PROACTIVE_NOTIFY],
        queue=_tier_queue(c, Queue.STANDARD),
        sla_minutes=120,
        comms_template="event_cancelled_full_refund",
    )


def _r_postponed_no_refund_ask(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.PROACTIVE_NOTIFY,
        base_confidence=0.93,
        rationale=[
            "Event POSTPONED; tickets remain valid for the new date.",
            "Fan did not request a refund, so the correct first move is "
            "information, not money. Volunteering a refund on a postponement "
            "converts retained revenue into churn.",
            "Handled as one event-level broadcast, not N per-fan decisions — "
            "see incident.py dedup key.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=30,
        comms_template="event_postponed_notify",
    )


def _r_postponed_refund_ask(c: Case) -> RuleResult:
    if c.event.refund_window_open:
        return RuleResult(
            action=ResolutionAction.FULL_REFUND,
            base_confidence=0.90,
            rationale=[
                "Event POSTPONED and the promoter's refund window is open.",
                "Fan explicitly asked for money back — honouring it inside an "
                "open window is policy-compliant and avoids a chargeback.",
            ],
            refund_usd=c.order.order_total_usd,
            queue=_tier_queue(c, Queue.STANDARD),
            sla_minutes=120,
            comms_template="postponed_refund_approved",
        )
    return RuleResult(
        action=ResolutionAction.ESCALATE_HUMAN,
        base_confidence=0.80,
        rationale=[
            "Event POSTPONED but the promoter has not opened a refund window.",
            "Refusing a refund is a relationship decision with chargeback risk. "
            "The system should not be the one to say no.",
        ],
        queue=_tier_queue(c, Queue.STANDARD),
        sla_minutes=180,
        comms_template="",
    )


def _r_delivery_failure_backend_ok(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.AUTO_RESEND_LINK,
        # High because the action is cheap, not the diagnosis certain.
        base_confidence=0.95,
        rationale=[
            "Backend shows tickets DELIVERED but the fan cannot see them — "
            "typically a wrong-account login or an unopened transfer email.",
            "Resending the link is the cheapest correct action: zero inventory "
            "impact, fully reversible, resolves the majority of these. Even a "
            "wrong diagnosis here costs nothing but one extra email.",
            f"T-{c.minutes_to_event:.0f}m leaves room to escalate if it fails.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=60,
        comms_template="delivery_resend_link",
    )


def _r_delivery_failure_not_delivered(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.AUTO_REISSUE,
        # Reissue is only destructive with a barcode to void, and this rule
        # fires when there isn't one. G10 enforces that contract for any
        # ticket status outside REISSUE_SAFE_STATUSES.
        base_confidence=0.93,
        rationale=[
            f"Ticket status is {c.order.ticket_status.value}; fan's report is "
            "corroborated by our own records, so no ambiguity to resolve.",
            "Reissue voids old barcodes and mints new ones, but there is no "
            "valid barcode to destroy at this status, so the action is "
            "effectively additive and carries no refund exposure.",
            "Inside the G10 reissue safety contract; had the ticket been "
            "delivered or scanned, G10 would have blocked auto-execution.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=60,
        comms_template="delivery_reissue",
    )


def _r_delivery_failure_critical(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.AUTO_REISSUE,
        base_confidence=0.88,
        rationale=[
            f"T-{c.minutes_to_event:.0f}m — inside the critical window.",
            "Fix and watch, simultaneously: reissue fires immediately because "
            "waiting for a human costs the fan the event, AND a human is paged "
            "in parallel because if the reissue fails there is no second chance.",
            "Deliberately does not wait for approval — the cost of a wrong "
            "reissue is one voided barcode; the cost of a slow one is a missed event.",
            "G10 still applies: if the ticket status is outside the reissue "
            "safety contract, auto-execution is blocked and a human confirms.",
        ],
        secondary=[ResolutionAction.ESCALATE_URGENT],
        goodwill_usd=_goodwill_for(c),
        queue=_tier_queue(c, Queue.URGENT),
        sla_minutes=10,
        comms_template="delivery_reissue_urgent",
        # Below the global 0.85: a slow fix costs the fan the event, and
        # ESCALATE_URGENT pages a human in parallel either way.
        auto_threshold=0.75,
    )


def _r_partial_delivery(c: Case) -> RuleResult:
    n_bad = len(c.order.undelivered_seats)
    n_good = len(c.order.delivered_seats)
    affected = c.order.affected_value_usd

    # If we cannot keep the party together, a human decides.
    party_would_split = n_good > 0 and n_bad > 0

    if c.time_bucket in {TimeBucket.T_MINUS_LONG, TimeBucket.T_MINUS_DAY}:
        return RuleResult(
            action=ResolutionAction.REPLACEMENT_TICKETS,
            base_confidence=0.78,
            rationale=[
                f"{n_bad} of {c.order.quantity} seats undelivered; {n_good} are fine.",
                f"T-{c.minutes_to_event/60:.1f}h is enough runway to source "
                "comparable seats and keep the group seated together.",
                "Refunding only the broken seats would technically close the "
                "case while leaving the fan with a split party — a bad outcome "
                "dressed up as a resolution.",
            ],
            secondary=[ResolutionAction.ESCALATE_HUMAN] if party_would_split else [],
            queue=_tier_queue(c, Queue.STANDARD),
            sla_minutes=90,
            comms_template="partial_replacement_sourcing",
        )

    if party_would_split:
        return RuleResult(
            action=ResolutionAction.ESCALATE_HUMAN,
            base_confidence=0.72,
            rationale=[
                f"{n_bad} of {c.order.quantity} seats undelivered at "
                f"T-{c.minutes_to_event:.0f}m.",
                "Too late to source matching seats, and a partial refund forces "
                "the group to choose who doesn't go. That trade-off belongs to "
                "the fan, offered by a human, not resolved by a rule.",
                f"Refund exposure if elected: ${affected:,.2f} of "
                f"${c.order.order_total_usd:,.2f}.",
            ],
            refund_usd=0.0,
            queue=_tier_queue(c, Queue.URGENT),
            sla_minutes=15,
            comms_template="",
        )

    return RuleResult(
        action=ResolutionAction.PARTIAL_REFUND,
        base_confidence=0.84,
        rationale=[
            f"All {n_bad} undelivered seats belong to the same order with no "
            "successfully delivered seats to strand.",
            f"Partial refund of ${affected:,.2f} maps exactly to the "
            "undelivered inventory.",
        ],
        refund_usd=affected,
        goodwill_usd=_goodwill_for(c),
        queue=_tier_queue(c, Queue.URGENT),
        sla_minutes=20,
        comms_template="partial_refund_undelivered",
    )


def _r_scan_failure(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.ESCALATE_URGENT,
        base_confidence=0.95,
        rationale=[
            "Fan is physically at the gate with a failing scan.",
            "No message-based resolution works here — they cannot read a push "
            "notification, sort out an app login and re-queue before the gate "
            "closes. This needs a human on a phone with venue box-office contact.",
            "Reissue fires in parallel because a fresh barcode sometimes clears "
            "a venue-side cache, but it is not treated as the resolution.",
        ],
        secondary=[ResolutionAction.AUTO_REISSUE],
        goodwill_usd=_goodwill_for(c),
        queue=Queue.GATE_SIDE,
        sla_minutes=3,
        comms_template="scan_failure_agent_incoming",
    )


def _r_transfer_stuck_early(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.PROACTIVE_NOTIFY,
        base_confidence=0.86,
        rationale=[
            "Seller has not completed the transfer; status is PENDING_TRANSFER.",
            f"T-{c.minutes_to_event/60:.1f}h leaves time for automated seller "
            "chase before we spend money on replacements.",
            "Fan gets a status update so silence doesn't become a second contact.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=120,
        comms_template="transfer_pending_seller_chase",
    )


def _r_transfer_stuck_late(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.MANUAL_TRANSFER_ASSIST,
        base_confidence=0.80,
        rationale=[
            f"Transfer still pending at T-{c.minutes_to_event:.0f}m.",
            "Seller outreach now requires a phone call, not an automated nudge. "
            "If the seller is unreachable the fallback is replacement or refund — "
            "a branch the system cannot pick on its own.",
        ],
        secondary=[ResolutionAction.REPLACEMENT_TICKETS],
        queue=_tier_queue(c, Queue.URGENT),
        sla_minutes=15,
        comms_template="transfer_agent_working",
    )


def _r_wrong_seats(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.ESCALATE_HUMAN,
        base_confidence=0.75,
        rationale=[
            "Fan received seats other than those purchased.",
            "Resolving this means reconciling two inventory records and possibly "
            "displacing another fan who is holding the same seats. Never automate "
            "an action whose blast radius includes a second customer.",
        ],
        queue=_tier_queue(c, Queue.URGENT),
        sla_minutes=30,
        comms_template="",
    )


def _r_duplicate_order(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.FULL_REFUND,
        base_confidence=0.76,
        rationale=[
            "Duplicate purchase detected on the same seats/event for this fan.",
            "Refund the later order. Confidence is held below auto-threshold "
            "because 'duplicate' sometimes means 'deliberately bought two pairs' "
            "— refunding a wanted order is worse than a slow one.",
        ],
        refund_usd=c.order.order_total_usd,
        queue=Queue.STANDARD,
        sla_minutes=180,
        comms_template="duplicate_order_refund",
    )


def _r_event_time_changed(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.PROACTIVE_NOTIFY,
        base_confidence=0.94,
        rationale=[
            "Start time moved; tickets unaffected.",
            "Pure information delivery — no money, no inventory, no access "
            "change. Safe to auto-send, and sending it prevents inbound contacts.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=15,
        comms_template="event_time_changed",
    )


def _r_refund_request_generic(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.ESCALATE_HUMAN,
        base_confidence=0.70,
        rationale=[
            "Fan asked for a refund with no qualifying service failure on record.",
            "All-sales-final is the marketplace default, but declining is a "
            "judgment and retention call. The system surfaces context; a human "
            "makes the call.",
        ],
        queue=_tier_queue(c, Queue.STANDARD),
        sla_minutes=240,
        comms_template="",
    )


def _r_unknown(c: Case) -> RuleResult:
    return RuleResult(
        action=ResolutionAction.ESCALATE_HUMAN,
        base_confidence=0.55,
        rationale=[
            "Issue could not be classified into a known playbook.",
            "No rule matched, so there is nothing to be confident about. "
            "Escalating with full context beats guessing.",
        ],
        queue=Queue.STANDARD,
        sla_minutes=120,
        comms_template="",
    )


# ---- ordered rule table --------------------------------------------------
# Event-level states dominate order-level ones, and time-critical branches come
# before their relaxed equivalents. Exception: R05 above R04, since DELAYED (as
# opposed to CANCELLED/POSTPONED) leaves the ticket usable and the fan still
# needs to get in.

RULES: list[Rule] = [
    Rule("R01_EVENT_CANCELLED",
         "Event cancelled → full refund",
         lambda c: c.event.status == EventStatus.CANCELLED,
         _r_event_cancelled),

    Rule("R02_POSTPONED_REFUND_REQUESTED",
         "Event postponed + fan asked for refund",
         lambda c: c.event.status == EventStatus.POSTPONED and c.report.explicit_refund_request,
         _r_postponed_refund_ask),

    Rule("R03_POSTPONED_NOTIFY",
         "Event postponed → proactive notify, tickets stay valid",
         lambda c: c.event.status == EventStatus.POSTPONED,
         _r_postponed_no_refund_ask),

    Rule("R05_SCAN_FAILURE",
         "Gate scan failure → gate-side human, SLA 3m",
         lambda c: c.issue_type == IssueType.SCAN_FAILURE_AT_GATE,
         _r_scan_failure),

    Rule("R04_TIME_CHANGED",
         "Start time moved, no order-level fault → notify",
         lambda c: (c.event.status == EventStatus.DELAYED
                    or c.issue_type == IssueType.EVENT_TIME_CHANGED)
                   and c.issue_type in {IssueType.EVENT_TIME_CHANGED,
                                        IssueType.UNKNOWN},
         _r_event_time_changed),

    Rule("R06_PARTIAL_DELIVERY",
         "Partial delivery failure → per-seat handling",
         lambda c: c.issue_type == IssueType.PARTIAL_DELIVERY_FAILURE
                   or (c.order.ticket_status == TicketStatus.PARTIALLY_DELIVERED
                       and c.issue_type == IssueType.MOBILE_DELIVERY_FAILURE),
         _r_partial_delivery),

    Rule("R07_DELIVERY_CRITICAL",
         "Delivery failure inside T-90m → reissue now + page a human",
         lambda c: c.issue_type == IssueType.MOBILE_DELIVERY_FAILURE
                   and c.time_bucket in {TimeBucket.T_MINUS_CRITICAL, TimeBucket.IN_EVENT},
         _r_delivery_failure_critical),

    Rule("R08_DELIVERY_BACKEND_OK",
         "Delivery failure but backend says delivered → resend link",
         lambda c: c.issue_type == IssueType.MOBILE_DELIVERY_FAILURE
                   and c.order.ticket_status == TicketStatus.DELIVERED,
         _r_delivery_failure_backend_ok),

    Rule("R09_DELIVERY_NOT_DELIVERED",
         "Delivery failure corroborated by backend → reissue",
         lambda c: c.issue_type == IssueType.MOBILE_DELIVERY_FAILURE,
         _r_delivery_failure_not_delivered),

    Rule("R10_TRANSFER_STUCK_LATE",
         "Seller transfer pending inside T-4h → manual assist",
         lambda c: c.issue_type == IssueType.TRANSFER_STUCK
                   and c.time_bucket in {TimeBucket.T_MINUS_HOURS,
                                         TimeBucket.T_MINUS_CRITICAL,
                                         TimeBucket.IN_EVENT},
         _r_transfer_stuck_late),

    Rule("R11_TRANSFER_STUCK_EARLY",
         "Seller transfer pending with runway → chase seller",
         lambda c: c.issue_type == IssueType.TRANSFER_STUCK,
         _r_transfer_stuck_early),

    Rule("R12_WRONG_SEATS",
         "Wrong seats delivered → human, second customer involved",
         lambda c: c.issue_type == IssueType.WRONG_SEATS_DELIVERED,
         _r_wrong_seats),

    Rule("R13_DUPLICATE_ORDER",
         "Duplicate order → refund the later one, human-approved",
         lambda c: c.issue_type == IssueType.DUPLICATE_ORDER,
         _r_duplicate_order),

    Rule("R14_REFUND_REQUEST",
         "Bare refund request with no service failure → human",
         lambda c: c.issue_type == IssueType.REFUND_REQUEST
                   or c.report.explicit_refund_request,
         _r_refund_request_generic),

    Rule("R99_UNKNOWN",
         "Fallback",
         lambda c: True,
         _r_unknown),
]


def match_rule(case: Case) -> tuple[Rule, RuleResult]:
    for rule in RULES:
        try:
            if rule.matches(case):
                return rule, rule.resolve(case)
        except Exception as exc:
            return (
                Rule("R00_RULE_ERROR", "Rule evaluation failed",
                     lambda c: True, lambda c: None),
                RuleResult(
                    action=ResolutionAction.ESCALATE_HUMAN,
                    base_confidence=0.0,
                    rationale=[f"Rule {rule.rule_id} raised {type(exc).__name__}: {exc}",
                               "Failing closed to human review."],
                    queue=Queue.URGENT,
                    sla_minutes=30,
                ),
            )
    raise RuntimeError("Rule table has no fallback — this is a policy bug.")


def top_alternatives(case: Case, chosen_rule_id: str, k: int = 2) -> list[dict]:
    """Other rules that would have fired, shown in the UI alongside the pick."""
    alts = []
    for rule in RULES:
        if rule.rule_id == chosen_rule_id or rule.rule_id == "R99_UNKNOWN":
            continue
        try:
            if rule.matches(case):
                res = rule.resolve(case)
                alts.append({
                    "rule_id": rule.rule_id,
                    "action": res.action.value,
                    "confidence": res.base_confidence,
                    "why": res.rationale[0] if res.rationale else rule.description,
                })
        except Exception:
            continue
        if len(alts) >= k:
            break
    return alts
