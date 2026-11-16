"""
Safety properties asserted across every case and over permutations of fraud
state, order value and contact history.

    python -m pytest tests/ -q
    python tests/test_invariants.py     (runs without pytest)
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import TriageEngine
from src.policy import REISSUE_SAFE_STATUSES
from src.schema import (
    AUTO_EXECUTABLE,
    FraudStatus,
    ResolutionAction,
    RouteDecision,
    TicketStatus,
)

# Private per-run temp dir, so audit logs never collide on a shared /tmp path.
TMP = Path(tempfile.mkdtemp(prefix="fanops-invariants-"))

MONEY_ACTIONS = {
    ResolutionAction.FULL_REFUND,
    ResolutionAction.PARTIAL_REFUND,
    ResolutionAction.REPLACEMENT_TICKETS,
}


def engine() -> TriageEngine:
    return TriageEngine(
        events_path=ROOT / "data" / "events.json",
        orders_path=ROOT / "data" / "orders.json",
        fans_path=ROOT / "data" / "fans.json",
        audit_path=TMP / "audit.jsonl",
        use_llm=False,
    )


def cases() -> list[dict]:
    return json.loads((ROOT / "data" / "cases.json").read_text())


# ==========================================================================

def test_no_auto_money_movement():
    """No auto-executed decision may move money."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.route == RouteDecision.AUTO_EXECUTE:
            assert d.action not in MONEY_ACTIONS, \
                f"{d.case_id}: auto-executed money action {d.action}"
            assert d.refund_amount_usd == 0.0, \
                f"{d.case_id}: auto-executed with ${d.refund_amount_usd} refund"


def test_auto_execute_allowlist_only():
    """Confidence must not promote an action out of its risk class."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.route == RouteDecision.AUTO_EXECUTE:
            assert d.action in AUTO_EXECUTABLE, \
                f"{d.case_id}: {d.action} auto-executed but is not in AUTO_EXECUTABLE"


def test_fraud_blocks_auto_resolve():
    """Every case against every active fraud state."""
    e = engine()
    for raw in cases():
        for status in (FraudStatus.UNDER_REVIEW, FraudStatus.FLAGGED):
            order_id = raw["report"]["order_id"]
            original = e.orders[order_id]["fraud_status"]
            e.orders[order_id]["fraud_status"] = status.value
            try:
                d = e.triage(raw)
                assert d.route != RouteDecision.AUTO_EXECUTE, \
                    f"{d.case_id} auto-resolved while fraud={status.value}"
                assert d.refund_amount_usd == 0.0
            finally:
                e.orders[order_id]["fraud_status"] = original


def test_repeat_contact_escalates_fixes_not_news():
    """Escalate repeat contacters for fixes, but still tell them news."""
    e = engine()
    for raw in cases():
        order_id = raw["report"]["order_id"]
        fan_id = e.orders[order_id]["fan_id"]
        original = e.fans[fan_id].get("prior_contacts_this_order_24h", 0)
        e.fans[fan_id]["prior_contacts_this_order_24h"] = 5
        try:
            d = e.triage(raw)
            if d.action not in {ResolutionAction.PROACTIVE_NOTIFY,
                                ResolutionAction.NO_ACTION_INFORMATIONAL}:
                assert d.route == RouteDecision.ESCALATE, \
                    f"{d.case_id} did not escalate at 5 prior contacts"
        finally:
            e.fans[fan_id]["prior_contacts_this_order_24h"] = original


def test_high_value_blocks_auto_resolve():
    e = engine()
    for raw in cases():
        order_id = raw["report"]["order_id"]
        original = e.orders[order_id]["order_total_usd"]
        e.orders[order_id]["order_total_usd"] = 2500.0
        try:
            d = e.triage(raw)
            assert d.route != RouteDecision.AUTO_EXECUTE, \
                f"{d.case_id} auto-resolved a $2500 order"
        finally:
            e.orders[order_id]["order_total_usd"] = original


def test_stale_state_blocks_auto_execution():
    """Decisions made against out-of-date event state must not auto-execute."""
    e = engine()
    late = datetime.now(timezone.utc) + timedelta(minutes=45)
    for raw in cases():
        d = e.triage(raw, now=late)
        if d.event_state_age_s > 1800:
            assert d.route != RouteDecision.AUTO_EXECUTE, \
                f"{d.case_id} auto-executed on {d.event_state_age_s:.0f}s-old state"


def test_every_decision_carries_provenance():
    """Every decision names a rule, a policy version and a state version."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        assert d.rule_id, f"{d.case_id} has no rule_id"
        assert d.rationale, f"{d.case_id} has no rationale"
        assert d.event_state_version >= 1, f"{d.case_id} has no state version"
        assert d.policy_version, f"{d.case_id} has no policy version"
        assert 0.0 <= d.confidence <= 1.0, f"{d.case_id} confidence out of range"


def test_every_decision_has_a_fan_message():

    e = engine()
    for raw in cases():
        d = e.triage(raw)
        assert d.fan_message and len(d.fan_message) > 40, \
            f"{d.case_id} produced no usable fan message"
        assert d.comms_template, f"{d.case_id} has no template attribution"


def test_message_money_matches_decision():
    """No dollar figure in a message that is not in the decision record."""
    import re
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        amounts = {float(x.replace(",", ""))
                   for x in re.findall(r"\$([0-9,]+(?:\.[0-9]{2})?)", d.fan_message)}
        authorised = {d.refund_amount_usd, d.goodwill_credit_usd,
                      round(d.refund_amount_usd), round(d.goodwill_credit_usd)}
        for amt in amounts:
            assert amt in authorised, \
                f"{d.case_id}: message mentions ${amt} but decision authorised {authorised}"


def test_escalations_route_to_escalate():
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.action in {ResolutionAction.ESCALATE_HUMAN, ResolutionAction.ESCALATE_URGENT}:
            assert d.route == RouteDecision.ESCALATE, \
                f"{d.case_id}: escalation action with route {d.route}"


def test_gate_side_sla():
    """Stated about the fan, not the queue: a conditional on
    `queue == gate_side` cannot fail if a case never reaches that queue."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.queue.value == "gate_side":
            assert d.sla_minutes <= 3, f"{d.case_id}: gate-side SLA is {d.sla_minutes}m"

    # A fan at a turnstile must reach a human whatever the event status.
    at_the_gate = [c for c in cases()
                   if c["report"].get("at_venue")
                   or "gate" in c["report"].get("raw_text", "").lower()]
    assert at_the_gate, "sample set no longer contains a gate-side case"
    for raw in at_the_gate:
        d = e.triage(raw)
        assert d.queue.value == "gate_side", (
            f"{d.case_id}: fan is at the gate but queued as {d.queue.value}")
        assert d.sla_minutes <= 3, f"{d.case_id}: gate-side SLA is {d.sla_minutes}m"


def test_confidence_monotonic_in_contact_history():
    """More failed prior attempts must not increase confidence."""
    e = engine()
    raw = next(c for c in cases() if c["case_id"] == "CASE-011")
    fan_id = e.orders[raw["report"]["order_id"]]["fan_id"]
    original = e.fans[fan_id].get("prior_contacts_this_order_24h", 0)
    scores = []
    try:
        for n in (0, 1, 2):
            e.fans[fan_id]["prior_contacts_this_order_24h"] = n
            scores.append(e.triage(raw).confidence)
    finally:
        e.fans[fan_id]["prior_contacts_this_order_24h"] = original
    assert scores == sorted(scores, reverse=True), \
        f"confidence not monotonic in contact history: {scores}"


def test_execution_is_idempotent():
    """Repeat execution on the same key must not re-run the side effect."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        first = e.execute(d)
        second = e.execute(d)
        assert second.status in {"duplicate", "not_executable"}, \
            f"{d.case_id}: second execution returned {second.status}"
        if first.status == "executed":
            assert second.status == "duplicate"
        assert len(e.executor.executed_keys()) == len(set(e.executor.executed_keys()))


def test_execution_refuses_actions_outside_the_allowlist():
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.action not in AUTO_EXECUTABLE:
            assert e.execute(d).status == "not_executable", \
                f"{d.case_id}: {d.action.value} was executed"


def test_reissue_never_auto_voids_a_valid_barcode():
    """The reissue safety contract: G10 blocks auto-execute outside it."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.action != ResolutionAction.AUTO_REISSUE:
            continue
        status = e.orders[d.order_id]["ticket_status"]
        if d.route == RouteDecision.AUTO_EXECUTE:
            assert TicketStatus(status) in REISSUE_SAFE_STATUSES, \
                f"{d.case_id}: auto-reissued over ticket_status={status}"


def test_message_never_asks_for_card_details():
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        msg = d.fan_message.lower()
        for phrase in ("last 4 digits", "card number", "cvv", "security code"):
            assert phrase not in msg, f"{d.case_id}: message asks for {phrase}"


def test_override_requires_a_reason():
    e = engine()
    d = e.triage(cases()[0])
    try:
        e.override(d, "full_refund", "agent-1", "")
        assert False, "override accepted an empty reason"
    except ValueError:
        pass


# ==========================================================================

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {t.__name__}\n        {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
