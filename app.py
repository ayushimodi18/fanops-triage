"""
Fan Ops triage console.

    streamlit run app.py

Three tabs: case review (decision, guards, reasoning, draft message, approve /
override), event board (live event state and change log), and audit (override
rate per rule and the raw decision log).

Approving runs src/execution.py, whose side effects are stubbed: nothing is
sent to a ticketing, payment or messaging system.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from src.pipeline import TriageEngine, incident_sweep
from src.policy import POLICY_VERSION
from src.schema import Decision, ResolutionAction, RouteDecision

st.set_page_config(page_title="Fan Ops Triage", page_icon="◎",
                   layout="wide", initial_sidebar_state="expanded")


# Colours follow gate-scanner semantics: green go, amber check, red deny.

st.markdown("""
<style>
:root{
  --go:#0F7B4F; --go-bg:#E8F5EE;
  --check:#B45309; --check-bg:#FDF3E3;
  --deny:#B02A37; --deny-bg:#FBEAEC;
  --ink:#16181D; --muted:#5A6270; --rule:#DFE2E7; --panel:#F5F6F8;
}
html,body,[class*="css"]{font-feature-settings:"tnum";}
.block-container{padding-top:1.6rem;padding-bottom:3rem;max-width:1500px;}

.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;}

.hdr{display:flex;align-items:baseline;gap:.75rem;border-bottom:2px solid var(--ink);
     padding-bottom:.5rem;margin-bottom:1.1rem;}
.hdr h1{font-size:1.15rem;margin:0;letter-spacing:-.01em;font-weight:650;}
.hdr .sub{font-size:.75rem;color:var(--muted);font-family:ui-monospace,monospace;}

.badge{display:inline-block;padding:.14rem .5rem;border-radius:3px;font-size:.68rem;
       font-weight:700;letter-spacing:.05em;text-transform:uppercase;
       font-family:ui-monospace,monospace;}
.b-go{background:var(--go-bg);color:var(--go);border:1px solid var(--go);}
.b-check{background:var(--check-bg);color:var(--check);border:1px solid var(--check);}
.b-deny{background:var(--deny-bg);color:var(--deny);border:1px solid var(--deny);}
.b-flat{background:var(--panel);color:var(--muted);border:1px solid var(--rule);}

.decision{border:1px solid var(--rule);border-left:5px solid var(--rule);
          padding:.9rem 1.1rem;border-radius:4px;background:#fff;margin-bottom:.9rem;}
.decision.go{border-left-color:var(--go);}
.decision.check{border-left-color:var(--check);}
.decision.deny{border-left-color:var(--deny);}
.decision .act{font-size:1.5rem;font-weight:660;letter-spacing:-.02em;margin:.2rem 0 .1rem;}
.decision .meta{font-size:.74rem;color:var(--muted);font-family:ui-monospace,monospace;}

.sla{margin:.55rem 0 .2rem;}
.sla-track{height:7px;background:var(--panel);border-radius:4px;overflow:hidden;
           border:1px solid var(--rule);}
.sla-fill{height:100%;transition:width .4s ease;}
.sla-label{font-size:.7rem;font-family:ui-monospace,monospace;color:var(--muted);
           margin-top:.22rem;display:flex;justify-content:space-between;}

.fanmsg{background:var(--panel);border-left:3px solid var(--rule);padding:.7rem .9rem;
        border-radius:0 4px 4px 0;font-size:.87rem;line-height:1.5;}

.why{font-size:.84rem;line-height:1.55;margin:.18rem 0 .18rem 0;padding-left:.85rem;
     border-left:2px solid var(--rule);color:var(--ink);}
.guard{font-size:.8rem;line-height:1.45;padding:.5rem .7rem;border-radius:4px;
       background:var(--deny-bg);border:1px solid #F0C7CC;margin-bottom:.35rem;}
.guard b{font-family:ui-monospace,monospace;font-size:.74rem;}

.stalebar{background:var(--check-bg);border:1px solid var(--check);color:var(--check);
          padding:.55rem .8rem;border-radius:4px;font-size:.82rem;margin-bottom:.8rem;
          font-weight:560;}

.qrow{padding:.42rem .55rem;border-left:3px solid var(--rule);margin-bottom:.22rem;
      font-size:.78rem;line-height:1.3;background:#fff;}
.qrow.go{border-left-color:var(--go);}
.qrow.check{border-left-color:var(--check);}
.qrow.deny{border-left-color:var(--deny);}
.qrow .cid{font-family:ui-monospace,monospace;font-weight:650;font-size:.74rem;}
.qrow .lbl{color:var(--muted);font-size:.72rem;}

.kv{display:flex;justify-content:space-between;font-size:.78rem;padding:.2rem 0;
    border-bottom:1px dotted var(--rule);font-family:ui-monospace,monospace;}
.kv span:first-child{color:var(--muted);}
hr{margin:.9rem 0;border:none;border-top:1px solid var(--rule);}
.sec{font-size:.7rem;font-weight:750;letter-spacing:.1em;text-transform:uppercase;
     color:var(--muted);margin:1.1rem 0 .4rem;}
</style>
""", unsafe_allow_html=True)


TONE = {
    RouteDecision.AUTO_EXECUTE: ("go", "b-go", "#0F7B4F"),
    RouteDecision.RECOMMEND:    ("check", "b-check", "#B45309"),
    RouteDecision.ESCALATE:     ("deny", "b-deny", "#B02A37"),
}


@st.cache_resource
def get_engine() -> TriageEngine:
    return TriageEngine(
        events_path=ROOT / "data" / "events.json",
        orders_path=ROOT / "data" / "orders.json",
        fans_path=ROOT / "data" / "fans.json",
        audit_path=ROOT / "outputs" / "audit_log.jsonl",
        use_llm=True,
    )


def load_raw_cases() -> list[dict]:
    return json.loads((ROOT / "data" / "cases.json").read_text())


EVENTS_PATH = ROOT / "data" / "events.json"
FEED_SNAPSHOT = ROOT / "outputs" / ".events_snapshot.json"


@st.cache_resource
def snapshot_feed() -> str:
    """The postpone control writes to the venue feed; snapshot it on first
    load so "Reset venue feed" can undo that."""
    FEED_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    original = EVENTS_PATH.read_text()
    FEED_SNAPSHOT.write_text(original)
    return original


engine = get_engine()
snapshot_feed()

if "decisions" not in st.session_state:
    st.session_state.decisions = {}
    st.session_state.raw = {c["case_id"]: c for c in load_raw_cases()}
    for cid, raw in st.session_state.raw.items():
        try:
            st.session_state.decisions[cid] = engine.triage(raw)
        except Exception as exc:
            st.warning(f"{cid} failed to triage: {exc}")
    st.session_state.handled = {}
    st.session_state.selected = next(iter(st.session_state.decisions), None)
    st.session_state.agent_id = "agent-am"

D: dict[str, Decision] = st.session_state.decisions


def retriage_all():
    for cid, raw in st.session_state.raw.items():
        try:
            st.session_state.decisions[cid] = engine.triage(raw)
        except Exception:
            pass


with st.sidebar:
    st.markdown('<div class="sec" style="margin-top:0">Live queue</div>',
                unsafe_allow_html=True)

    order = {RouteDecision.ESCALATE: 0, RouteDecision.RECOMMEND: 1,
             RouteDecision.AUTO_EXECUTE: 2}
    queue = sorted(
        [(cid, d) for cid, d in D.items() if cid not in st.session_state.handled],
        key=lambda kv: (order[kv[1].route], kv[1].sla_minutes),
    )

    counts = {r: sum(1 for _, d in queue if d.route == r) for r in RouteDecision}
    c1, c2, c3 = st.columns(3)
    c1.metric("Escalated", counts[RouteDecision.ESCALATE])
    c2.metric("To review", counts[RouteDecision.RECOMMEND])
    c3.metric("Auto", counts[RouteDecision.AUTO_EXECUTE])

    st.markdown("---")

    for cid, d in queue:
        tone, badge, _ = TONE[d.route]
        flag = " ⟳" if (d.stale or d.re_evaluated) else ""
        label = st.session_state.raw[cid].get("label", "")
        st.markdown(
            f'<div class="qrow {tone}">'
            f'<span class="cid">{cid}{flag}</span> '
            f'<span class="badge {badge}">{d.queue.value.replace("_"," ")}</span>'
            f'<br><span class="lbl">{label} · SLA {d.sla_minutes}m · '
            f'conf {d.confidence:.2f}</span></div>',
            unsafe_allow_html=True,
        )
        if st.button("Open", key=f"open_{cid}", use_container_width=True):
            st.session_state.selected = cid
            st.rerun()

    if st.session_state.handled:
        st.markdown('<div class="sec">Handled this session</div>', unsafe_allow_html=True)
        for cid, what in st.session_state.handled.items():
            st.markdown(f'<div class="qrow"><span class="cid">{cid}</span> '
                        f'<span class="lbl">{what}</span></div>',
                        unsafe_allow_html=True)

    st.markdown('<div class="sec">Event day controls</div>', unsafe_allow_html=True)

    if st.button("Refresh event data", use_container_width=True):
        changed = engine.refresh_events()
        retriage_all()
        st.success(f"Polled feed · {len(changed)} event(s) changed")
        st.rerun()

    if st.button("Simulate: postpone Giants game", use_container_width=True):
        feed = json.loads(EVENTS_PATH.read_text())
        for e in feed:
            if e["event_id"] == "EVT-1003":
                e["status"] = "postponed"
                e["status_note"] = "Postponed due to severe weather; new date TBC."
                e["rescheduled_to"] = (
                    datetime.now(timezone.utc) + timedelta(days=8)
                ).replace(hour=1, minute=10).isoformat()
        EVENTS_PATH.write_text(json.dumps(feed, indent=2))
        engine.refresh_events()
        sweep = incident_sweep(engine, "EVT-1003")
        for s in sweep:
            st.session_state.decisions[s.case_id] = s
            st.session_state.raw[s.case_id] = {
                "case_id": s.case_id, "label": "Incident sweep — postponement",
                "report": {"case_id": s.case_id, "order_id": s.order_id,
                           "channel": "app_chat", "raw_text": "",
                           "fan_stated_issue": "event_postponed"},
            }
        retriage_all()
        st.warning(f"EVT-1003 postponed · {len(sweep)} orders swept · "
                   f"pending decisions re-evaluated")
        st.rerun()

    if st.button("Reset venue feed", use_container_width=True,
                 help="Undo simulated feed changes and restore the committed dataset"):
        if FEED_SNAPSHOT.exists():
            EVENTS_PATH.write_text(FEED_SNAPSHOT.read_text())
        engine.refresh_events()
        for cid in [c for c in st.session_state.raw if c.startswith("SWEEP-")]:
            st.session_state.raw.pop(cid, None)
            st.session_state.decisions.pop(cid, None)
        retriage_all()
        st.success("Venue feed restored")
        st.rerun()

    from src.llm import provider_label
    st.caption(f"Signed in as `{st.session_state.agent_id}` · policy v{POLICY_VERSION} · "
               f"{provider_label()}")


st.markdown(
    '<div class="hdr"><h1>Fan Ops Triage</h1>'
    f'<span class="sub">{datetime.now():%a %d %b · %H:%M} local · '
    f'{len(queue)} open</span></div>',
    unsafe_allow_html=True,
)

tab_case, tab_events, tab_audit = st.tabs(["Case review", "Event board", "Audit"])


# case review
with tab_case:
    cid = st.session_state.selected
    if not cid or cid not in D:
        st.info("Select a case from the queue.")
    else:
        d = D[cid]
        raw = st.session_state.raw[cid]
        tone, badge, hexc = TONE[d.route]

        if d.stale or d.re_evaluated:
            moved = (f" Recommendation moved from <b>{d.superseded_action}</b> to "
                     f"<b>{d.action.value}</b>."
                     if d.superseded_action and d.superseded_action != d.action.value
                     else " Recommendation re-checked and unchanged.")
            st.markdown(
                f'<div class="stalebar">⟳ Event state changed while this case was '
                f'in the queue.{moved} Re-evaluated against v{d.event_state_version}.</div>',
                unsafe_allow_html=True,
            )

        left, right = st.columns([1.45, 1])

        # left: the decision
        with left:
            # Drains with elapsed time since the decision, so a 3-minute case
            # empties faster than a relaxed one.
            elapsed_m = (datetime.now(timezone.utc) - d.decided_at).total_seconds() / 60
            left_m = d.sla_minutes - elapsed_m
            pct = max(0, min(100, int(100 * left_m / max(d.sla_minutes, 1))))
            sla_text = (f"SLA {d.sla_minutes} min · {left_m:.0f} min left" if left_m > 0
                        else f"SLA {d.sla_minutes} min · BREACHED {-left_m:.0f} min ago")
            if left_m <= 0:
                hexc = "#d64545"
            st.markdown(
                f'<div class="decision {tone}">'
                f'<span class="badge {badge}">{d.route.value.replace("_"," ")}</span> '
                f'<span class="badge b-flat">{d.queue.value.replace("_"," ")}</span>'
                f'<div class="act">{d.action.value.replace("_"," ")}</div>'
                f'<div class="meta">{d.rule_id} · confidence {d.confidence:.2f} · '
                f'event state v{d.event_state_version} ({d.event_state_age_s:.0f}s old)</div>'
                f'<div class="sla"><div class="sla-track">'
                f'<div class="sla-fill" style="width:{pct}%;background:{hexc}"></div></div>'
                f'<div class="sla-label"><span>{sla_text}</span>'
                f'<span>{d.order_id} · {d.event_id}</span></div></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            if d.secondary_actions:
                st.markdown(
                    "Also firing: " +
                    " ".join(f'<span class="badge b-flat">{a.value.replace("_"," ")}</span>'
                             for a in d.secondary_actions),
                    unsafe_allow_html=True,
                )

            if d.displaced_action:
                st.markdown(
                    'Vetoed by a guard — <b>not</b> executed: '
                    f'<span class="badge b-deny">'
                    f'{d.displaced_action.value.replace("_"," ")}</span>',
                    unsafe_allow_html=True,
                )

            if d.refund_amount_usd or d.goodwill_credit_usd:
                m1, m2 = st.columns(2)
                m1.metric("Refund", f"${d.refund_amount_usd:,.2f}")
                m2.metric("Goodwill credit", f"${d.goodwill_credit_usd:,.2f}")

            if d.guards_hit:
                st.markdown('<div class="sec">Guards triggered</div>', unsafe_allow_html=True)
                for g in d.guards_hit:
                    verb = (f"forces {g.forces.value}" if g.forces else "blocks auto-execute")
                    st.markdown(f'<div class="guard"><b>{g.guard_id}</b> — {verb}<br>'
                                f'{g.reason}</div>', unsafe_allow_html=True)

            st.markdown('<div class="sec">Why</div>', unsafe_allow_html=True)
            for reason in d.rationale:
                st.markdown(f'<div class="why">{reason}</div>', unsafe_allow_html=True)

            if d.alternatives:
                st.markdown('<div class="sec">Also considered</div>', unsafe_allow_html=True)
                for a in d.alternatives:
                    st.markdown(
                        f'<div class="why"><span class="mono">{a["rule_id"]}</span> → '
                        f'<b>{a["action"].replace("_"," ")}</b> '
                        f'({a["confidence"]:.2f}) — {a["why"]}</div>',
                        unsafe_allow_html=True,
                    )

            with st.expander("Confidence breakdown"):
                for k, v in d.confidence_breakdown.items():
                    if k == "note_corroborated":
                        st.markdown(f'<div class="kv"><span>fan report matches our records'
                                    f'</span><span>{"yes" if v else "no"}</span></div>',
                                    unsafe_allow_html=True)
                    else:
                        st.markdown(f'<div class="kv"><span>{k.replace("_"," ")}</span>'
                                    f'<span>{v}</span></div>', unsafe_allow_html=True)

        # right: fan, order, actions
        with right:
            st.markdown('<div class="sec">Fan wrote</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="fanmsg">{raw["report"].get("raw_text") or "—"}</div>',
                        unsafe_allow_html=True)

            order = engine.orders.get(d.order_id, {})
            fan = engine.fans.get(order.get("fan_id"), {})
            ev = engine.store.get(d.event_id)

            st.markdown('<div class="sec">Context</div>', unsafe_allow_html=True)
            rows = [
                ("fan", f'{fan.get("display_name","?")} · {fan.get("tier","?")}'),
                ("lifetime", f'{fan.get("lifetime_orders",0)} orders · '
                             f'${fan.get("lifetime_gmv_usd",0):,.0f}'),
                ("contacts 24h", f'{fan.get("prior_contacts_this_order_24h",0)} this order'),
                ("order", f'${order.get("order_total_usd",0):,.2f} · '
                          f'{order.get("quantity",0)} seats'),
                ("ticket status", order.get("ticket_status", "?")),
                ("fraud", order.get("fraud_status", "?")),
                ("event", f'{ev.name}'),
                ("status", ev.status.value),
            ]
            for k, v in rows:
                st.markdown(f'<div class="kv"><span>{k}</span><span>{v}</span></div>',
                            unsafe_allow_html=True)

            st.markdown('<div class="sec">Draft fan message</div>', unsafe_allow_html=True)
            st.caption(f"template `{d.comms_template}` · sending via **{d.message_channel}**")
            edited = st.text_area("message", value=d.fan_message, height=170,
                                  label_visibility="collapsed", key=f"msg_{cid}")

            st.markdown('<div class="sec">Act</div>', unsafe_allow_html=True)
            st.caption("Side effects are simulated: no ticketing, payment or "
                       "messaging system is wired up. Execution is idempotent "
                       "on (order, action, event state version).")

            if d.stale:
                st.error("Blocked: event state changed and re-evaluation failed. "
                         "Refresh event data before acting.")
            else:
                verb = ("Confirm auto-executed action"
                        if d.route == RouteDecision.AUTO_EXECUTE else "Approve and send")
                if st.button(verb, type="primary", use_container_width=True,
                             key=f"ok_{cid}"):
                    d.fan_message = edited
                    result = engine.approve(d, st.session_state.agent_id)
                    st.session_state.handled[cid] = (
                        f"approved · {d.action.value} · {result.status}")
                    st.session_state.selected = None
                    st.rerun()

            with st.expander("Override"):
                new_action = st.selectbox(
                    "Correct action",
                    [a.value for a in ResolutionAction],
                    index=[a.value for a in ResolutionAction].index(d.action.value),
                    key=f"ov_a_{cid}",
                )
                reason = st.text_input(
                    "Why was the recommendation wrong?",
                    placeholder="Required",
                    key=f"ov_r_{cid}",
                )
                if st.button("Submit override", use_container_width=True, key=f"ov_b_{cid}"):
                    if not reason.strip():
                        st.error("A reason is required.")
                    else:
                        engine.override(d, new_action, st.session_state.agent_id, reason)
                        st.session_state.handled[cid] = f"overridden → {new_action}"
                        st.session_state.selected = None
                        st.rerun()


# event board
with tab_events:
    st.markdown('<div class="sec">Event state</div>', unsafe_allow_html=True)
    now = datetime.now(timezone.utc)
    for ev in sorted(engine.store.all_events(), key=lambda e: e.starts_at):
        mte = (ev.starts_at - now).total_seconds() / 60
        open_cases = sum(1 for x in D.values()
                         if x.event_id == ev.event_id and x.case_id not in st.session_state.handled)
        tone = ("deny" if ev.status.value in {"cancelled", "postponed"}
                else "check" if mte <= 90 else "go")
        st.markdown(
            f'<div class="decision {tone}" style="padding:.6rem .9rem">'
            f'<span class="badge b-flat">{ev.event_id}</span> '
            f'<span class="badge {"b-deny" if tone=="deny" else "b-check" if tone=="check" else "b-go"}">'
            f'{ev.status.value.replace("_"," ")}</span>'
            f'<div style="font-weight:620;margin:.25rem 0 .1rem">{ev.name}</div>'
            f'<div class="meta">{ev.venue} · '
            f'{"T-" + str(int(mte)) + "m" if mte > 0 else "started " + str(int(-mte)) + "m ago"} · '
            f'state v{ev.state_version} · {ev.age_seconds(now):.0f}s old · '
            f'{open_cases} open case(s)</div>'
            + (f'<div class="meta" style="color:var(--deny);margin-top:.25rem">'
               f'{ev.status_note}</div>' if ev.status_note else "")
            + '</div>',
            unsafe_allow_html=True,
        )

    if engine.store.change_log:
        st.markdown('<div class="sec">State change log</div>', unsafe_allow_html=True)
        for entry in reversed(engine.store.change_log[-12:]):
            st.markdown(
                f'<div class="kv"><span>{entry["at"][11:19]} · {entry["event_id"]}</span>'
                f'<span>v{entry["version"]} · {entry["from_status"]} → '
                f'{entry["to_status"]}</span></div>',
                unsafe_allow_html=True,
            )


# audit
with tab_audit:
    st.markdown('<div class="sec">Override rate by rule</div>', unsafe_allow_html=True)
    st.caption("A rule overridden often is a rule worth revising.")

    stats = engine.audit.override_rate_by_rule()
    if not stats:
        st.info("No decisions logged yet.")
    else:
        import pandas as pd
        rows = [{
            "rule": rid,
            "decisions": s["decisions"],
            "approved": s["approvals"],
            "overridden": s["overrides"],
            "override rate": (f'{s["override_rate"]:.0%}'
                              if s["override_rate"] is not None else "—"),
        } for rid, s in sorted(stats.items())]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        reasons = [(rid, r) for rid, s in stats.items() for r in s["reasons"]]
        if reasons:
            st.markdown('<div class="sec">Override reasons</div>', unsafe_allow_html=True)
            for rid, r in reasons[-15:]:
                st.markdown(f'<div class="why"><span class="mono">{rid}</span> — {r}</div>',
                            unsafe_allow_html=True)

    st.markdown('<div class="sec">Raw decision log</div>', unsafe_allow_html=True)
    log = engine.audit.read()
    st.caption(f"{len(log)} events · outputs/audit_log.jsonl")
    if log:
        st.json(log[-6:], expanded=False)
