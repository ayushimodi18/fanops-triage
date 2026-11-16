#!/usr/bin/env python3
"""
Batch triage and event-day simulation.

Triage only: this CLI prints decisions and does not run the (stubbed) execution
layer in src/execution.py.

    python run.py                 triage every case in data/cases.json
    python run.py --case CASE-001 one case
    python run.py --simulate      event-day timeline simulation
    python run.py --md            write outputs/sample_outputs.md
    python run.py --no-llm        force deterministic mode
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.pipeline import TriageEngine, incident_sweep
from src.policy import POLICY_VERSION
from src.schema import Decision, RouteDecision

ROOT = Path(__file__).parent

C = {
    "auto_execute": "\033[92m", "recommend": "\033[93m", "escalate": "\033[91m",
    "dim": "\033[2m", "bold": "\033[1m", "reset": "\033[0m", "cyan": "\033[96m",
}


def colour(route: RouteDecision) -> str:
    return C.get(route.value, "")


def render(d: Decision, label: str = "", plain: bool = False) -> str:
    c = "" if plain else colour(d.route)
    b = "" if plain else C["bold"]
    dim = "" if plain else C["dim"]
    r = "" if plain else C["reset"]

    lines = [
        f"{b}{'─' * 78}{r}",
        f"{b}{d.case_id}{r}  {dim}{label}{r}",
        f"{b}{'─' * 78}{r}",
        f"  ROUTE       {c}{b}{d.route.value.upper()}{r}   "
        f"action: {b}{d.action.value}{r}",
    ]
    if d.secondary_actions:
        lines.append(f"  ALSO FIRES  {', '.join(a.value for a in d.secondary_actions)}")
    if d.displaced_action:
        lines.append(f"  VETOED      {d.displaced_action.value}   "
                     f"{dim}(rule proposed this; a guard stopped it){r}")
    _product = {k: v for k, v in d.confidence_breakdown.items()
                if k not in ("final",) and not k.startswith("note_")}
    _corr = d.confidence_breakdown.get("note_corroborated")
    _corr_txt = "" if _corr is None else (
        f"   {dim}corroborated by our records: {'yes' if _corr else 'no'}{r}")
    lines += [
        f"  CONFIDENCE  {d.confidence:.2f}   "
        f"{dim}({' × '.join(f'{k}={v}' for k, v in _product.items())}){r}"
        + _corr_txt,
        f"  QUEUE       {d.queue.value}   SLA {d.sla_minutes}m",
        f"  RULE        {d.rule_id}   policy v{d.policy_version}   "
        f"event state v{d.event_state_version} ({d.event_state_age_s:.0f}s old)",
    ]
    if d.refund_amount_usd or d.goodwill_credit_usd:
        lines.append(f"  MONEY       refund ${d.refund_amount_usd:,.2f}   "
                     f"goodwill ${d.goodwill_credit_usd:,.2f}")
    if d.guards_hit:
        lines.append(f"  {b}GUARDS{r}")
        for g in d.guards_hit:
            flag = "FORCES→" + g.forces.value if g.forces else "blocks auto"
            lines.append(f"    • {g.guard_id} [{flag}]")
            lines.append(f"      {dim}{g.reason}{r}")
    lines.append(f"  {b}WHY{r}")
    for reason in d.rationale:
        lines.append(f"    • {reason}")
    if d.alternatives:
        lines.append(f"  {dim}ALSO CONSIDERED: " +
                     "; ".join(f"{a['rule_id']}→{a['action']} ({a['confidence']:.2f})"
                               for a in d.alternatives) + f"{r}")
    lines += [
        f"  {b}FAN MESSAGE{r}  {dim}[{d.message_channel}] template={d.comms_template}{r}",
    ]
    for chunk in _wrap(d.fan_message, 72):
        lines.append(f"    {chunk}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def load_cases() -> list[dict]:
    return json.loads((ROOT / "data" / "cases.json").read_text())


# --------------------------------------------------------------------------

def run_all(engine: TriageEngine, only: str | None = None, plain: bool = False):
    decisions = []
    for raw in load_cases():
        if only and raw["case_id"] != only:
            continue
        d = engine.triage(raw)
        decisions.append((d, raw.get("label", "")))
        print(render(d, raw.get("label", ""), plain=plain))
        print()
    return decisions


def summary(decisions, plain: bool = False):
    from collections import Counter
    b = "" if plain else C["bold"]
    r = "" if plain else C["reset"]
    routes = Counter(d.route.value for d, _ in decisions)
    total = len(decisions)
    if not total:
        print("No cases to summarise.")
        return
    auto = routes.get("auto_execute", 0)
    print(f"{b}SUMMARY{r}  {total} cases")
    for k, v in routes.items():
        c = "" if plain else colour(RouteDecision(k))
        print(f"  {c}{k:<14}{r} {v:>3}  ({v/total:.0%})")
    print(f"\n  Auto-resolved without a human: {b}{auto/total:.0%}{r}")
    exposure = sum(d.refund_amount_usd for d, _ in decisions
                   if d.route == RouteDecision.AUTO_EXECUTE)
    print(f"  Money moved without human approval: ${exposure:,.2f}")


# --------------------------------------------------------------------------

def simulate(engine: TriageEngine, plain: bool = False):
    """
    Event-day timeline: staleness blocking, incident sweep, in-flight
    invalidation.

    Drives the venue feed by writing to data/events.json, which is how the
    store would see a real change, and restores it afterwards so later runs
    are unaffected.
    """
    C = {k: "" for k in globals()["C"]} if plain else globals()["C"]
    events_path = ROOT / "data" / "events.json"
    original_feed = events_path.read_text()
    try:
        _simulate(engine, events_path, C)
    finally:
        events_path.write_text(original_feed)
        engine.refresh_events()
        print(f"\n{C['dim']}(venue feed restored){C['reset']}")


def _simulate(engine: TriageEngine, events_path: Path, C: dict = C):
    print(f"{C['bold']}{C['cyan']}EVENT-DAY SIMULATION{C['reset']}\n")

    cases = {c["case_id"]: c for c in load_cases()}
    now = datetime.now(timezone.utc)

    # ---- T0: normal triage of a Giants case (event on time) ----
    print(f"{C['bold']}[T+0m] Giants game is ON TIME. Fan reports a delivery problem.{C['reset']}")
    d1 = engine.triage(cases["CASE-003"], now=now)
    print(f"  → {d1.route.value} / {d1.action.value} "
          f"(event state v{d1.event_state_version})")
    print(f"  → pending queue holds {len(engine.pending.all_pending())} case(s)\n")

    # ---- T+2: decision made against stale state ----
    print(f"{C['bold']}[T+2m] A case arrives with a 150s-old event snapshot.{C['reset']}")
    print(f"  {C['dim']}(tolerance inside T-90m is 60s){C['reset']}")
    d_stale = engine.triage(cases["CASE-001"], now=now + timedelta(seconds=150))
    stale_guard = [g for g in d_stale.guards_hit if g.guard_id == "G04_STALE_EVENT_STATE"]
    if stale_guard:
        print(f"  → G04 fired: auto-execute blocked, downgraded to "
              f"{C['recommend']}{d_stale.route.value}{C['reset']}")
        print(f"  → {C['dim']}{stale_guard[0].reason}{C['reset']}\n")
    else:
        print(f"  → within tolerance, still {d_stale.route.value}\n")

    # ---- T+3: weather postponement lands ----
    print(f"{C['bold']}[T+3m] Venue feed flips Giants game to POSTPONED (weather).{C['reset']}")
    feed = json.loads(events_path.read_text())
    for e in feed:
        if e["event_id"] == "EVT-1003":
            e["status"] = "postponed"
            e["status_note"] = "Postponed due to severe weather; new date to be confirmed."
            e["rescheduled_to"] = (now + timedelta(days=8)).replace(hour=1, minute=10).isoformat()
    events_path.write_text(json.dumps(feed, indent=2))

    changed = engine.refresh_events(now=now + timedelta(minutes=3))
    print(f"  → state store detected change on {changed}, version bumped")

    changed_decisions = engine.pending.recently_changed()
    print(f"  → {len(changed_decisions)} pending decision(s) flagged and re-evaluated")
    for cd in changed_decisions:
        moved = (f"{cd.superseded_action} → {cd.action.value}"
                 if cd.superseded_action and cd.superseded_action != cd.action.value
                 else f"{cd.action.value} (unchanged)")
        print(f"     {cd.case_id}: {moved}   route={cd.route.value}")
    print()

    # ---- T+3: incident sweep ----
    print(f"{C['bold']}[T+3m] Incident sweep fires.{C['reset']}")
    sweep = incident_sweep(engine, "EVT-1003", now=now + timedelta(minutes=3))
    print(f"  → {len(sweep)} affected order(s) swept as one broadcast")
    if sweep:
        s = sweep[0]
        print(f"  → {s.route.value} / {s.action.value}, confidence {s.confidence:.2f}")
        print(f"  → {C['dim']}{s.fan_message[:150]}...{C['reset']}")
    print()

    print(f"{C['bold']}Event state change log:{C['reset']}")
    for entry in engine.store.change_log[-4:]:
        print(f"  v{entry['version']}  {entry['event_id']}  "
              f"{entry['from_status']} → {entry['to_status']}")


# --------------------------------------------------------------------------

def write_markdown(engine: TriageEngine):
    from src.llm import provider_label
    out = ["# Sample Outputs",
           "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
           f"policy v{POLICY_VERSION} · {provider_label()}",
           "",
           "Full decision record per case, as shown in the console.",
           ""]

    for raw in load_cases():
        d = engine.triage(raw)
        out += [
            f"## {d.case_id} — {raw.get('label','')}",
            "",
            f"**Fan wrote:** _{raw['report']['raw_text']}_",
            "",
            "| | |",
            "|---|---|",
            f"| Route | **{d.route.value.upper()}** |",
            f"| Action | `{d.action.value}` |",
            f"| Also fires | {', '.join(f'`{a.value}`' for a in d.secondary_actions) or '—'} |",
            f"| Vetoed by guard | "
            f"{f'`{d.displaced_action.value}`' if d.displaced_action else '—'} |",
            f"| Confidence | {d.confidence:.2f} |",
            f"| Rule | `{d.rule_id}` |",
            f"| Queue / SLA | {d.queue.value} / {d.sla_minutes} min |",
            f"| Refund | ${d.refund_amount_usd:,.2f} |",
            f"| Goodwill | ${d.goodwill_credit_usd:,.2f} |",
            f"| Event state | v{d.event_state_version}, {d.event_state_age_s:.0f}s old |",
            "",
        ]
        if d.guards_hit:
            out.append("**Guards triggered**")
            out.append("")
            for g in d.guards_hit:
                out.append(f"- `{g.guard_id}` — {g.reason}")
            out.append("")
        out.append("**Reasoning**")
        out.append("")
        for reason in d.rationale:
            out.append(f"- {reason}")
        out += ["", "**Draft fan message** "
                    f"(`{d.comms_template}` via {d.message_channel})", "",
                f"> {d.fan_message}", "", "---", ""]

    path = ROOT / "outputs" / "sample_outputs.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out))
    print(f"Wrote {path}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--plain", action="store_true", help="no ANSI colour")
    args = ap.parse_args()

    engine = TriageEngine(
        events_path=ROOT / "data" / "events.json",
        orders_path=ROOT / "data" / "orders.json",
        fans_path=ROOT / "data" / "fans.json",
        audit_path=ROOT / "outputs" / "audit_log.jsonl",
        use_llm=not args.no_llm,
    )

    from src.llm import provider_label
    dim = "" if args.plain else C["dim"]
    reset = "" if args.plain else C["reset"]
    print(f"{dim}Triage engine · policy v{POLICY_VERSION} · {provider_label()}{reset}\n")

    if args.simulate:
        simulate(engine, plain=args.plain)
        return
    if args.md:
        write_markdown(engine)
        return

    if args.case:
        known = {c["case_id"] for c in load_cases()}
        if args.case not in known:
            print(f"No case {args.case!r} in data/cases.json. "
                  f"Available: {', '.join(sorted(known))}")
            sys.exit(1)

    decisions = run_all(engine, only=args.case, plain=args.plain)
    if not args.case:
        summary(decisions, plain=args.plain)


if __name__ == "__main__":
    main()
