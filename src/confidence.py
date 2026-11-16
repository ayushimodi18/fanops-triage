"""
Turns a rule match into a routing decision.

Confidence measures how much of what we needed to know we actually knew, not
how likely the action is to succeed.

    final = base_rule_confidence
          x classification_factor      (did we understand the issue?)
          x completeness_factor        (do we have the fields?)
          x freshness_factor           (is our event state current?)
          x contact_factor             (has automation already failed here?)

Every factor is <= 1.0, so confidence only decays from the rule's ceiling.
"""

from __future__ import annotations

from .event_state import staleness_threshold_seconds
from .schema import (
    AUTO_EXECUTABLE,
    Case,
    GuardHit,
    Queue,
    ResolutionAction,
    RouteDecision,
)

AUTO_THRESHOLD = 0.85
RECOMMEND_THRESHOLD = 0.60

INFORMATIONAL_ACTIONS = {
    ResolutionAction.PROACTIVE_NOTIFY,
    ResolutionAction.NO_ACTION_INFORMATIONAL,
}


def corroborated(case: Case) -> bool:
    """Whether our order record independently agrees with the fan's report."""
    from .schema import IssueType as IT, TicketStatus as TS

    agreement = {
        IT.MOBILE_DELIVERY_FAILURE: {TS.NOT_DELIVERED, TS.DELIVERED,
                                     TS.PENDING_TRANSFER},
        IT.PARTIAL_DELIVERY_FAILURE: {TS.PARTIALLY_DELIVERED},
        IT.TRANSFER_STUCK: {TS.PENDING_TRANSFER},
        IT.SCAN_FAILURE_AT_GATE: {TS.DELIVERED, TS.SCANNED, TS.REVOKED},
    }
    expected = agreement.get(case.issue_type)
    return bool(expected and case.order.ticket_status in expected)


def classification_factor(case: Case) -> float:
    # A tapped button or a gate-scanner event beats an inference from text.
    if case.classification_source in {"explicit", "sensor"}:
        return 1.0

    # Gentler curve so a 0.80 classifier score cannot alone drag a decision
    # under threshold.
    base = 0.5 + 0.5 * case.classification_confidence

    # Corroboration recovers most of the penalty; contradiction is penalised
    # harder than mere absence of agreement.
    if corroborated(case):
        base = base + (1.0 - base) * 0.75
    elif case.issue_type.value != "unknown" and case.classification_source == "llm":
        base *= 0.9

    return round(min(base, 1.0), 3)


def completeness_factor(case: Case) -> float:
    dc = case.data_completeness
    if dc >= 0.95:
        return 1.0
    if dc >= 0.85:
        return 0.95
    if dc >= 0.70:
        return 0.85
    return 0.6


def freshness_factor(case: Case) -> float:
    tol = staleness_threshold_seconds(case.minutes_to_event)
    age = case.event_state_age_s
    if age <= tol * 0.5:
        return 1.0
    if age <= tol:
        return 0.95
    if age <= tol * 2:
        return 0.8
    return 0.5


def contact_factor(case: Case, action: ResolutionAction | None = None) -> float:
    """
    Each prior contact is evidence our last automated answer was wrong.
    Informational actions are exempt, as in G03/G05.
    """
    if action in INFORMATIONAL_ACTIONS:
        return 1.0
    n = case.fan.prior_contacts_this_order_24h
    return {0: 1.0, 1: 0.92, 2: 0.8}.get(n, 0.5)


def score(
    case: Case,
    base_confidence: float,
    action: ResolutionAction | None = None,
) -> tuple[float, dict[str, float]]:
    factors = {
        "rule_base": round(base_confidence, 3),
        "classification": classification_factor(case),
        "completeness": completeness_factor(case),
        "freshness": freshness_factor(case),
        "contact_history": contact_factor(case, action),
    }
    final = base_confidence
    for k, v in factors.items():
        if k != "rule_base":
            final *= v
    # An input to classification_factor, not a multiplicand. The note_ prefix
    # keeps renderers from splicing it into the displayed product.
    factors["note_corroborated"] = 1.0 if corroborated(case) else 0.0
    factors["final"] = round(final, 3)
    return round(final, 3), factors


def route(
    case: Case,
    action: ResolutionAction,
    confidence: float,
    guards: list[GuardHit],
    auto_threshold: float | None = None,
) -> tuple[RouteDecision, list[str]]:
    """Resolve the final route. Guards always win over confidence."""
    notes: list[str] = []
    threshold = AUTO_THRESHOLD if auto_threshold is None else auto_threshold

    forced = [g for g in guards if g.forces is not None]
    if forced:
        g = forced[0]
        notes.append(f"Route forced to {g.forces.value} by {g.guard_id}.")
        return g.forces, notes

    blocking = [g for g in guards if g.blocks_auto]

    # An escalation is a route, not a recommendation to be approved.
    if action in {ResolutionAction.ESCALATE_HUMAN, ResolutionAction.ESCALATE_URGENT,
                  ResolutionAction.HOLD_PENDING_REVIEW}:
        notes.append(
            f"{action.value} routes straight to a human queue — there is no "
            "system action to approve."
        )
        return RouteDecision.ESCALATE, notes

    if action not in AUTO_EXECUTABLE:
        notes.append(
            f"{action.value} is outside the auto-executable set — it is either "
            "irreversible or affects a party other than this fan. Human approval "
            "required regardless of confidence."
        )
        decision = (RouteDecision.RECOMMEND if confidence >= RECOMMEND_THRESHOLD
                    else RouteDecision.ESCALATE)
        if confidence < RECOMMEND_THRESHOLD:
            notes.append(f"Confidence {confidence:.2f} below {RECOMMEND_THRESHOLD:.2f}; "
                         "presenting evidence without a recommendation.")
        return decision, notes

    if blocking:
        notes.append(
            "Auto-execution blocked by: "
            + ", ".join(g.guard_id for g in blocking)
        )
        return RouteDecision.RECOMMEND, notes

    if confidence >= threshold:
        extra = (f" (rule lowered its own bar from {AUTO_THRESHOLD:.2f} to "
                 f"{threshold:.2f}; a human is paged in parallel)"
                 if auto_threshold is not None and auto_threshold < AUTO_THRESHOLD else "")
        notes.append(
            f"Confidence {confidence:.2f} ≥ {threshold:.2f}, action is "
            f"reversible, no guards blocking → executing automatically{extra}."
        )
        return RouteDecision.AUTO_EXECUTE, notes

    if confidence >= RECOMMEND_THRESHOLD:
        notes.append(
            f"Confidence {confidence:.2f} sits between {RECOMMEND_THRESHOLD:.2f} "
            f"and {threshold:.2f} → recommend with one-click approval."
        )
        return RouteDecision.RECOMMEND, notes

    notes.append(
        f"Confidence {confidence:.2f} below {RECOMMEND_THRESHOLD:.2f} → escalating "
        "with context and candidate actions, no recommendation."
    )
    return RouteDecision.ESCALATE, notes


def effective_sla(base_sla: int, queue: Queue) -> int:
    floors = {
        Queue.GATE_SIDE: 3,
        Queue.URGENT: 10,
        Queue.SENIOR: 30,
        Queue.TRUST_AND_SAFETY: 60,
        Queue.STANDARD: base_sla,
    }
    return min(base_sla, floors.get(queue, base_sla))
