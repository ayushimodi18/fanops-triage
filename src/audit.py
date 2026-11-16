"""
Append-only JSONL log of decisions, approvals and overrides. One line each,
stamped with the policy version. Override rate per rule feeds policy revisions.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Decision


class AuditLog:
    def __init__(self, path: str | Path = "outputs/audit_log.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _write(self, record: dict[str, Any]) -> None:
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")

    def decision(self, d: Decision) -> None:
        self._write({
            "type": "decision",
            "case_id": d.case_id,
            "order_id": d.order_id,
            "event_id": d.event_id,
            "action": d.action.value,
            "secondary_actions": [x.value for x in d.secondary_actions],
            "displaced_action": d.displaced_action.value if d.displaced_action else None,
            "route": d.route.value,
            "queue": d.queue.value,
            "confidence": d.confidence,
            "confidence_breakdown": d.confidence_breakdown,
            "rule_id": d.rule_id,
            "guards": [g.guard_id for g in d.guards_hit],
            "refund_usd": d.refund_amount_usd,
            "goodwill_usd": d.goodwill_credit_usd,
            "event_state_version": d.event_state_version,
            "event_state_age_s": d.event_state_age_s,
            "policy_version": d.policy_version,
        })

    def override(self, d: Decision, new_action: str, agent_id: str, reason: str) -> None:
        """Reason is required; TriageEngine.override rejects a blank one."""
        self._write({
            "type": "override",
            "case_id": d.case_id,
            "rule_id": d.rule_id,
            "system_action": d.action.value,
            "system_confidence": d.confidence,
            "agent_action": new_action,
            "agent_id": agent_id,
            "reason": reason,
            "policy_version": d.policy_version,
        })

    def approval(self, d: Decision, agent_id: str) -> None:
        self._write({
            "type": "approval",
            "case_id": d.case_id,
            "rule_id": d.rule_id,
            "action": d.action.value,
            "confidence": d.confidence,
            "agent_id": agent_id,
            "policy_version": d.policy_version,
        })

    def execution(self, d: Decision, result) -> None:
        self._write({
            "type": "execution",
            "case_id": d.case_id,
            "order_id": d.order_id,
            "action": result.action.value,
            "status": result.status,          # executed | duplicate | not_executable
            "detail": result.detail,
            "simulated": result.simulated,
            "execution_key": list(result.key),
            "policy_version": d.policy_version,
        })

    def state_change(self, entry: dict) -> None:
        self._write({"type": "event_state_change", **entry})

    # ---------------- read side ----------------

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def override_rate_by_rule(self) -> dict[str, dict[str, Any]]:
        """Decisions, approvals and overrides per rule, with override rate."""
        stats: dict[str, dict[str, Any]] = {}
        for r in self.read():
            if r["type"] not in {"decision", "override", "approval"}:
                continue
            rid = r.get("rule_id", "?")
            s = stats.setdefault(rid, {"decisions": 0, "approvals": 0,
                                       "overrides": 0, "reasons": []})
            if r["type"] == "decision":
                s["decisions"] += 1
            elif r["type"] == "approval":
                s["approvals"] += 1
            else:
                s["overrides"] += 1
                if r.get("reason"):
                    s["reasons"].append(r["reason"])
        for rid, s in stats.items():
            reviewed = s["approvals"] + s["overrides"]
            s["override_rate"] = round(s["overrides"] / reviewed, 3) if reviewed else None
        return stats
