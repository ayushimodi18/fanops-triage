"""
Regression tests for previously fixed bugs.

    python tests/test_regressions.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import comms                                    # noqa: E402
from src import policy                                   # noqa: E402
from src.pipeline import TriageEngine, incident_sweep    # noqa: E402
from src.schema import ResolutionAction, RouteDecision   # noqa: E402

DATA = ROOT / "data"

# Private per-run temp dir, so audit logs never collide on a shared /tmp path.
TMP = Path(tempfile.mkdtemp(prefix="fanops-regressions-"))


def engine(events_path: Path | None = None) -> TriageEngine:
    return TriageEngine(
        events_path=events_path or DATA / "events.json",
        orders_path=DATA / "orders.json",
        fans_path=DATA / "fans.json",
        audit_path=TMP / "audit.jsonl",
        use_llm=False,
    )


def cases() -> list[dict]:
    return json.loads((DATA / "cases.json").read_text())


def case(cid: str) -> dict:
    return copy.deepcopy(next(c for c in cases() if c["case_id"] == cid))


def _events_with(event_id: str, **overrides) -> Path:
    feed = json.loads((DATA / "events.json").read_text())
    for e in feed:
        if e["event_id"] == event_id:
            e.update(overrides)
    tmp = Path(tempfile.mkdtemp(prefix="fanops-events-")) / "events.json"
    tmp.write_text(json.dumps(feed, indent=2, default=str))
    return tmp


# ==========================================================================
# 1. Comms grounding
# ==========================================================================

# If one of these appears in a message, the action must have been taken.
_CLAIMS = {
    "reissued": {ResolutionAction.AUTO_REISSUE},
    "are void": {ResolutionAction.AUTO_REISSUE},
    "re-sent the transfer": {ResolutionAction.AUTO_RESEND_LINK},
    "we've refunded": {ResolutionAction.FULL_REFUND, ResolutionAction.PARTIAL_REFUND},
    "we're refunding": {ResolutionAction.FULL_REFUND, ResolutionAction.PARTIAL_REFUND},
    "approved your refund": {ResolutionAction.FULL_REFUND},
    "sourcing replacement seats": {ResolutionAction.REPLACEMENT_TICKETS},
}


def _unbacked_claims(decision) -> list[str]:
    """Chosen action plus firing secondaries; `displaced_action` is excluded
    because a guard stopped it."""
    msg = decision.fan_message.lower()
    taken = {decision.action} | set(decision.secondary_actions)
    return [phrase for phrase, needs in _CLAIMS.items()
            if phrase in msg and not (needs & taken)]


# Describe an inbound human, so they stay correct on an escalation.
_ESCALATION_SAFE = {
    "escalation_holding",
    "fraud_review_holding",
    "scan_failure_agent_incoming",
}


def test_escalation_never_sends_a_fix_template():
    """Original defect: action=escalate_human paired with
    template=delivery_reissue, telling a fan their tickets had been reissued."""
    e = engine()
    offenders = []
    for raw in cases():
        d = e.triage(raw)
        if d.action in (ResolutionAction.ESCALATE_HUMAN,
                        ResolutionAction.ESCALATE_URGENT,
                        ResolutionAction.HOLD_PENDING_REVIEW):
            if d.comms_template not in _ESCALATION_SAFE:
                offenders.append(
                    f"{d.case_id}: action={d.action.value} "
                    f"template={d.comms_template}")
    assert not offenders, "escalation sending a fix template:\n  " + "\n  ".join(offenders)


def test_vetoed_action_not_listed_as_firing():
    """secondary_actions used to mean both "also fires" and "was vetoed", so
    both rendered identically."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        if d.displaced_action:
            assert d.displaced_action not in d.secondary_actions, (
                f"{d.case_id}: {d.displaced_action.value} was vetoed but is "
                f"listed as also firing"
            )


def test_message_claims_only_actions_taken():
    """A guard could force an escalation while the rule's template survived,
    so the message described a fix that never happened."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        bad = _unbacked_claims(d)
        assert not bad, (
            f"{d.case_id}: action={d.action.value} but message claims {bad} "
            f"(template={d.comms_template})"
        )


def test_grounding_holds_under_forcing_guards():
    """Every case against each forcing guard."""
    mutations = [
        ("G03_REPEAT_CONTACT_CEILING", "fan", "prior_contacts_this_order_24h", 3),
        ("G05_AUTOMATION_EXHAUSTED", "fan", "prior_auto_resolutions_this_order", 2),
        ("G08_UNSETTLED_PAYMENT", "order", "payment_settled", False),
        ("G09_DOUBLE_REFUND", "order", "prior_refund_on_order", True),
    ]
    failures = []
    for raw in cases():
        for guard_id, target, field, value in mutations:
            e = engine()
            oid = raw["report"]["order_id"]
            if target == "order":
                e.orders[oid][field] = value
            else:
                e.fans[e.orders[oid]["fan_id"]][field] = value
            d = e.triage(copy.deepcopy(raw))
            for phrase in _unbacked_claims(d):
                failures.append(f"{raw['case_id']}+{guard_id} claims {phrase!r}")
    assert not failures, "unbacked claims:\n  " + "\n  ".join(failures)


def test_every_template_declares_actions():
    """A template with no declared binding bypasses the check above."""
    undeclared = set(comms.TEMPLATES) - set(comms.TEMPLATE_VALID_FOR)
    assert not undeclared, f"templates missing from TEMPLATE_VALID_FOR: {sorted(undeclared)}"


# ==========================================================================
# 2. Rule ordering — event-level status must not swallow order-level faults
# ==========================================================================

def test_gate_case_reaches_human_during_delay():
    """R04_TIME_CHANGED sat above R05_SCAN_FAILURE and matched on event status
    alone, so a weather hold turned a turnstile case into a schedule bulletin:
    gate_side -> standard, 3m -> 15m, nobody paged."""
    delayed = _events_with("EVT-1001", status="delayed",
                           status_note="Start pushed 40 minutes, lightning hold.")
    for path, label in ((None, "on time"), (delayed, "weather delay")):
        d = engine(path).triage(case("CASE-005"))
        assert d.queue.value == "gate_side", f"{label}: queue was {d.queue.value}"
        assert d.sla_minutes <= 3, f"{label}: SLA was {d.sla_minutes}m"
        assert d.route == RouteDecision.ESCALATE, f"{label}: route was {d.route.value}"


def test_delivery_failure_not_masked_by_delay():
    """A DELAYED event must not mask an order-level fault. Must use EVT-1001,
    the event CASE-001's order belongs to, or the test is vacuous."""
    delayed = _events_with("EVT-1001", status="delayed", status_note="Rain delay.")
    d = engine(delayed).triage(case("CASE-001"))
    assert d.action != ResolutionAction.PROACTIVE_NOTIFY, (
        f"delivery failure resolved as {d.action.value} because the event was delayed"
    )
    assert d.rule_id != "R04_TIME_CHANGED", (
        f"{d.case_id} matched R04_TIME_CHANGED despite an order-level fault"
    )


# ==========================================================================
# 3. Incident sweep idempotency
# ==========================================================================

def test_sweep_dedups_same_event_version():
    """The dedup set was function-local, so a poller re-firing the sweep
    re-notified every fan on every tick."""
    postponed = _events_with("EVT-1003", status="postponed",
                             status_note="Severe weather.")
    e = engine(postponed)
    first = incident_sweep(e, "EVT-1003")
    assert first, "sweep should broadcast on the first call"
    assert incident_sweep(e, "EVT-1003") == [], "re-notified at the same state version"
    assert incident_sweep(e, "EVT-1003") == [], "re-notified at the same state version"


def test_sweep_fires_on_new_event_version():
    """Dedup must not become suppression: a new version is a new broadcast."""
    tmp = _events_with("EVT-1003", status="postponed", status_note="Severe weather.")
    e = engine(tmp)
    assert incident_sweep(e, "EVT-1003")

    feed = json.loads(tmp.read_text())
    for ev in feed:
        if ev["event_id"] == "EVT-1003":
            ev["status"] = "cancelled"
            ev["status_note"] = "Called off."
    tmp.write_text(json.dumps(feed, indent=2, default=str))
    e.refresh_events()

    assert incident_sweep(e, "EVT-1003"), "a real state change must broadcast again"


# ==========================================================================
# 4. Tunable constants
# ==========================================================================

def test_high_value_ceiling_is_tunable():
    """G07's predicate read a hardcoded 500.0 while its reason string
    formatted HIGH_VALUE_USD, so retuning the constant did nothing."""
    original = policy.HIGH_VALUE_USD
    try:
        policy.HIGH_VALUE_USD = 2000.0
        d = engine().triage(case("CASE-004"))          # a $1,240 order
        hits = [g for g in d.guards_hit if g.guard_id == "G07_HIGH_VALUE"]
        assert not hits, "G07 fired below its configured ceiling"

        policy.HIGH_VALUE_USD = 500.0
        d = engine().triage(case("CASE-004"))
        hits = [g for g in d.guards_hit if g.guard_id == "G07_HIGH_VALUE"]
        assert hits, "G07 did not fire above its configured ceiling"
        assert "$500" in hits[0].reason
    finally:
        policy.HIGH_VALUE_USD = original


# ==========================================================================
# 5. Confidence breakdown arithmetic
# ==========================================================================

def test_confidence_factors_multiply_out():
    """`corroborated` is a flag, not a multiplicand, but was emitted into the
    dict the CLI renders as a product."""
    e = engine()
    for raw in cases():
        d = e.triage(raw)
        product = 1.0
        for k, v in d.confidence_breakdown.items():
            if k == "final" or k.startswith("note_"):
                continue
            product *= v
        assert abs(round(product, 3) - d.confidence) < 0.002, (
            f"{d.case_id}: shown factors multiply to {product:.3f} "
            f"but confidence is {d.confidence}"
        )


# ==========================================================================
# 6. The simulation must not mutate the committed dataset
# ==========================================================================

def test_simulate_restores_the_venue_feed():
    """`--simulate` wrote `postponed` into data/events.json without restoring
    it, so later runs produced different numbers."""
    import run as runner

    events_path = DATA / "events.json"
    before = events_path.read_text()
    try:
        runner.simulate(engine())
    finally:
        after = events_path.read_text()
    assert after == before, "simulate() left the venue feed modified"


# ==========================================================================
# 6. Fixes from the limitations review
# ==========================================================================

def test_sweep_ignores_on_time_event():
    """Sweeping an on-time event used to synthesise "postponed" cases for
    every ticket holder."""
    on_time = _events_with("EVT-1003", status="on_time")
    assert incident_sweep(engine(on_time), "EVT-1003") == []


def test_sweep_retries_order_after_triage_failure():
    """The order was marked swept before triage, so one exception meant that
    fan never got the notice."""
    tmp = _events_with("EVT-1003", status="postponed", status_note="Weather.")
    e = engine(tmp)
    real = e.triage

    def boom(*a, **k):
        raise RuntimeError("transient")

    e.triage = boom
    try:
        incident_sweep(e, "EVT-1003")
    except RuntimeError:
        pass
    e.triage = real
    assert incident_sweep(e, "EVT-1003"), "failed order was never retried"


def test_fan_message_uses_venue_local_time():
    """Times went out in UTC with no zone, so 5:10 PM PST read as 01:10 AM."""
    tmp = _events_with("EVT-1003", status="postponed",
                       timezone="America/Los_Angeles",
                       rescheduled_to="2026-11-24T01:10:00+00:00")
    d = incident_sweep(engine(tmp), "EVT-1003")[0]
    assert "05:10 PM PST" in d.fan_message, d.fan_message
    assert "01:10 AM" not in d.fan_message, d.fan_message


def test_classifier_tolerates_typos_without_false_matches():
    from src.llm import _keyword_classify, _normalise
    from src.schema import IssueType

    def k(t):
        return _keyword_classify(_normalise(t), None)

    assert k("my tickts never arived")["issue_type"] == IssueType.MOBILE_DELIVERY_FAILURE
    assert k("I can\u2019t find my tickets")["issue_type"] == IssueType.MOBILE_DELIVERY_FAILURE
    assert k("scan isnt workng at the gate")["issue_type"] == IssueType.SCAN_FAILURE_AT_GATE
    # One typo must not count as several pieces of evidence.
    assert k("event got cancled")["confidence"] < k("event got cancelled")["confidence"]
    # Short words stay exact: these must not match "no tickets" / "new date".
    assert k("my tickets are here thanks")["issue_type"] == IssueType.UNKNOWN
    assert k("new data please")["issue_type"] == IssueType.UNKNOWN


def test_simulate_plain_has_no_ansi():
    import contextlib
    import io
    import run as runner

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runner.simulate(engine(), plain=True)
    assert "\033[" not in buf.getvalue()


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
