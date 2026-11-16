"""
Orchestration.

Decisions come from policy.py and confidence.py; the LLM in llm.py only
classifies inbound text and rewrites an approved draft. Side effects live in
execution.py and are stubbed.

    raw case -> enrich -> classify -> rule match -> guards -> confidence
             -> route -> comms -> audit -> Decision

No business logic here; an `if` that decides an outcome belongs in policy.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import comms, confidence as conf, llm
from .audit import AuditLog
from .event_state import EventStateStore, PendingQueue
from .execution import Executor
from .policy import POLICY_VERSION, guard_queue, match_rule, run_guards, top_alternatives
from .schema import (
    Case,
    Decision,
    EventStatus,
    FanContext,
    InboundReport,
    IssueType,
    OrderContext,
    ResolutionAction,
    RouteDecision,
)


def _label_rationale(rationale: list[str],
                     displaced: ResolutionAction | None) -> list[str]:
    """Label a rationale whose action a guard vetoed, so it is not read as a
    record of what happened."""
    if not displaced:
        return list(rationale)
    return [
        f"NOT APPLIED — the rule argued for {displaced.value}; a guard vetoed "
        f"it. The reasoning below is kept for review, not as a description of "
        f"what was done."
    ] + list(rationale)


class TriageEngine:
    def __init__(
        self,
        events_path: str | Path = "data/events.json",
        orders_path: str | Path = "data/orders.json",
        fans_path: str | Path = "data/fans.json",
        audit_path: str | Path = "outputs/audit_log.jsonl",
        use_llm: bool = True,
    ):
        self.store = EventStateStore(events_path)
        self.orders = {o["order_id"]: o for o in json.loads(Path(orders_path).read_text())}
        self.fans = {f["fan_id"]: f for f in json.loads(Path(fans_path).read_text())}
        self.audit = AuditLog(audit_path)
        self.executor = Executor()
        self.use_llm = use_llm and llm.llm_available()
        self.pending = PendingQueue(self.store, re_evaluate=self._re_evaluate)
        self._case_cache: dict[str, dict] = {}
        # Already-broadcast (order_id, event_state_version) pairs. On the
        # engine so a re-firing poller cannot re-notify the same fan.
        self.swept: set[tuple[str, int]] = set()

        self.store.subscribe(
            lambda eid, new, old: self.audit.state_change({
                "event_id": eid, "version": new.state_version,
                "status": new.status.value, "note": new.status_note,
            })
        )

    def build_case(self, raw_case: dict[str, Any], now: datetime | None = None) -> Case:
        now = now or datetime.now(timezone.utc)
        self._case_cache[raw_case["case_id"]] = raw_case

        report = InboundReport(**raw_case["report"])

        order_raw = self.orders.get(report.order_id)
        if order_raw is None:
            raise KeyError(f"Unknown order {report.order_id}")
        order = OrderContext(**order_raw)

        fan_raw = self.fans.get(order.fan_id)
        if fan_raw is None:
            raise KeyError(f"Unknown fan {order.fan_id}")
        fan = FanContext(**fan_raw)

        event = self.store.get(order.event_id, now=now)

        case = Case(report=report, order=order, fan=fan, event=event, now=now)

        result = llm.classify(report.raw_text, fallback=report.fan_stated_issue)
        case.issue_type = result["issue_type"]
        case.classification_confidence = result["confidence"]
        case.classification_source = result["source"]

        # A tapped button beats a parsed sentence.
        if not report.explicit_refund_request:
            case.report.explicit_refund_request = result["explicit_refund_request"]
        if not report.at_venue:
            case.report.at_venue = result["at_venue"]

        return case

    def decide(self, case: Case, polish: bool | None = None) -> Decision:
        # Rule first: guards need to know which action they are policing.
        rule, result = match_rule(case)
        guards = run_guards(case, candidate_action=result.action)

        score, breakdown = conf.score(case, result.base_confidence, action=result.action)
        route, route_notes = conf.route(
            case, result.action, score, guards, auto_threshold=result.auto_threshold
        )

        action = result.action
        secondary = list(result.secondary)
        refund = result.refund_usd
        goodwill = result.goodwill_usd

        forced = [g for g in guards if g.forces == RouteDecision.ESCALATE]
        displaced = None
        if forced and action not in {ResolutionAction.ESCALATE_HUMAN,
                                     ResolutionAction.ESCALATE_URGENT}:
            # Displaced, not secondary: a secondary would mean it also fires.
            displaced = action
            action = (ResolutionAction.ESCALATE_URGENT
                      if case.time_bucket.value in {"t_minus_critical", "in_event"}
                      else ResolutionAction.ESCALATE_HUMAN)
            secondary = []     # vetoed along with the action they accompany
            refund = 0.0       # never pre-authorise money on a forced escalation
            goodwill = 0.0

        queue = guard_queue(guards) or result.queue
        sla = conf.effective_sla(result.sla_minutes, queue)

        decision = Decision(
            case_id=case.report.case_id,
            order_id=case.order.order_id,
            event_id=case.event.event_id,
            action=action,
            secondary_actions=secondary,
            displaced_action=displaced,
            route=route,
            queue=queue,
            sla_minutes=sla,
            confidence=score,
            confidence_breakdown=breakdown,
            rule_id=rule.rule_id,
            rationale=_label_rationale(result.rationale, displaced) + route_notes,
            guards_hit=guards,
            alternatives=top_alternatives(case, rule.rule_id),
            refund_amount_usd=round(refund, 2),
            goodwill_credit_usd=round(goodwill, 2),
            event_state_version=case.event.state_version,
            event_state_age_s=round(case.event_state_age_s, 1),
            policy_version=POLICY_VERSION,
        )

        template_key = comms.select_template(case, decision, result.comms_template)
        draft = comms.render(case, decision, template_key)

        do_polish = self.use_llm if polish is None else (polish and llm.llm_available())
        if do_polish:
            draft, _ = llm.polish(draft, comms.tone_hint(case))

        decision.comms_template = template_key
        decision.fan_message = draft
        decision.message_channel = comms.choose_channel(case)

        self.audit.decision(decision)

        if decision.route in {RouteDecision.RECOMMEND, RouteDecision.ESCALATE}:
            self.pending.add(decision)

        return decision

    def triage(self, raw_case: dict[str, Any], now: datetime | None = None) -> Decision:
        return self.decide(self.build_case(raw_case, now=now))

    def _re_evaluate(self, case_id: str) -> Decision:
        """Called by PendingQueue when event state moves under a pending case."""
        raw = self._case_cache[case_id]
        d = self.decide(self.build_case(raw))
        d.rationale.insert(0, "RE-EVALUATED: event state changed while this case "
                              "was awaiting review.")
        return d

    def refresh_events(self, now: datetime | None = None) -> list[str]:
        return self.store.refresh(now=now)

    def approve(self, decision: Decision, agent_id: str):
        """Log the approval and run the (stubbed) side effect, at most once."""
        self.audit.approval(decision, agent_id)
        self.pending.remove(decision.case_id)
        return self.execute(decision)

    def execute(self, decision: Decision):
        """
        Apply a decision's side effects. Idempotent on
        (order_id, action, event_state_version); a repeat call is a no-op.
        """
        result = self.executor.execute(decision)
        self.audit.execution(decision, result)
        return result

    def override(self, decision: Decision, new_action: str, agent_id: str, reason: str) -> None:
        if not reason or not reason.strip():
            raise ValueError("Override reason is required.")
        self.audit.override(decision, new_action, agent_id, reason)
        self.pending.remove(decision.case_id)


def incident_sweep(engine: TriageEngine, event_id: str, now: datetime | None = None) -> list[Decision]:
    """
    One synthesised case per affected order when an event is postponed or
    cancelled. Dedup key (order_id, event_state_version): a new version is a
    new broadcast, the same version never sends twice.
    """
    now = now or datetime.now(timezone.utc)
    ctx = engine.store.get(event_id, now=now)
    out: list[Decision] = []

    # Only a postponement or cancellation is an incident. Sweeping an on-time
    # event would tell every ticket holder it had been postponed.
    if ctx.status not in (EventStatus.POSTPONED, EventStatus.CANCELLED):
        return out

    for oid, order in engine.orders.items():
        if order["event_id"] != event_id:
            continue
        key = (oid, ctx.state_version)
        if key in engine.swept:
            continue

        synthetic = {
            "case_id": f"SWEEP-{event_id}-v{ctx.state_version}-{oid}",
            "report": {
                "case_id": f"SWEEP-{event_id}-v{ctx.state_version}-{oid}",
                "order_id": oid,
                "channel": "app_chat",
                "raw_text": "",
                "fan_stated_issue": (IssueType.EVENT_CANCELLED.value
                                     if ctx.status.value == "cancelled"
                                     else IssueType.EVENT_POSTPONED.value),
            },
        }
        out.append(engine.triage(synthetic, now=now))
        # Mark only after triage succeeds, so a failure is retried on the next
        # sweep instead of silently skipping this fan.
        engine.swept.add(key)

    return out
