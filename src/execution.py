"""
Execution layer for approved decisions.

The side effects here are STUBBED. Nothing in this module talks to a ticketing
platform, a payment processor or a message gateway; each handler records what
would have been sent and returns. Wiring a real integration means replacing the
body of one handler in `_HANDLERS` — the idempotency and audit behaviour around
it stays the same.

Idempotency: every execution is keyed on
`(order_id, action, event_state_version)`. A repeat call with the same key is a
no-op returning the original result. The event-state version is part of the key
on purpose — if the event moves, the same action against the new state is a
legitimately different execution rather than a duplicate.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .schema import AUTO_EXECUTABLE, Decision, ResolutionAction

ExecutionKey = tuple[str, str, int]


@dataclass
class ExecutionResult:
    key: ExecutionKey
    action: ResolutionAction
    status: str                 # executed | duplicate | not_executable
    detail: str
    simulated: bool = True      # no real integration is wired up
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def execution_key(decision: Decision) -> ExecutionKey:
    return (decision.order_id, decision.action.value, decision.event_state_version)


# Stubbed integrations. Each returns the line that would be handed to the real
# system; none of them performs a side effect.
def _reissue(d: Decision) -> str:
    return f"void existing barcodes for {d.order_id} and mint replacements"


def _resend_link(d: Decision) -> str:
    return f"re-send the delivery link for {d.order_id}"


def _notify(d: Decision) -> str:
    return f"send {d.message_channel} message for {d.order_id}"


_HANDLERS = {
    ResolutionAction.AUTO_REISSUE: _reissue,
    ResolutionAction.AUTO_RESEND_LINK: _resend_link,
    ResolutionAction.PROACTIVE_NOTIFY: _notify,
    ResolutionAction.NO_ACTION_INFORMATIONAL: _notify,
}


class Executor:
    """
    Applies a decision's side effects at most once per execution key.

    Actions outside AUTO_EXECUTABLE are rejected here as well as in the router,
    so a caller cannot execute a refund by constructing a Decision by hand.
    """

    def __init__(self):
        self._done: dict[ExecutionKey, ExecutionResult] = {}
        self._lock = threading.Lock()

    def execute(self, decision: Decision) -> ExecutionResult:
        key = execution_key(decision)

        with self._lock:
            previous = self._done.get(key)
            if previous is not None:
                return ExecutionResult(key, previous.action, "duplicate",
                                       f"already executed at {previous.at.isoformat()}")

            if decision.action not in AUTO_EXECUTABLE:
                return ExecutionResult(
                    key, decision.action, "not_executable",
                    f"{decision.action.value} is outside AUTO_EXECUTABLE and "
                    "requires a human to carry out",
                )

            handler = _HANDLERS.get(decision.action)
            detail = handler(decision) if handler else "no side effect defined"
            result = ExecutionResult(key, decision.action, "executed", detail)
            self._done[key] = result
            return result

    def executed_keys(self) -> list[ExecutionKey]:
        return list(self._done)

    def record(self, decision: Decision) -> dict[str, Any] | None:
        """The stored result for a decision's key, as a dict, or None."""
        r = self._done.get(execution_key(decision))
        return None if r is None else {"key": r.key, "action": r.action.value,
                                       "detail": r.detail, "at": r.at.isoformat()}
