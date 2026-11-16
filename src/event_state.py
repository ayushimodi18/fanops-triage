"""
Live event state, and keeping decisions current as it changes.

Three mechanisms:

1. Adaptive polling: poll frequency is a function of time-to-event.
2. Freshness contract: every EventContext carries `fetched_at` and
   `state_version`; every Decision embeds the version it was made against.
3. In-flight invalidation: decisions waiting in the approval queue are
   re-evaluated when their event's state version bumps.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .schema import Decision, EventContext, EventStatus


# --------------------------------------------------------------------------
# Adaptive polling policy
# --------------------------------------------------------------------------

def polling_interval_seconds(minutes_to_event: float, status: EventStatus) -> int:
    """Poll cadence by urgency; contact volume skews to the last hour."""
    if status in {EventStatus.DELAYED, EventStatus.POSTPONED,
                  EventStatus.CANCELLED, EventStatus.RELOCATED}:
        return 10          # active incident
    if minutes_to_event <= 90:
        return 15          # contact spike window
    if minutes_to_event <= 240:
        return 60
    if minutes_to_event <= 24 * 60:
        return 300
    return 900


# Max tolerated staleness before G04 blocks auto-execution. Roughly 4x the
# poll interval, so one missed poll is survivable and three are not.
def staleness_threshold_seconds(minutes_to_event: float) -> int:
    if minutes_to_event <= 90:
        return 60
    if minutes_to_event <= 240:
        return 240
    return 1800


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class EventStateStore:
    """
    Holds the current EventContext per event. `fetch_raw` is the only method
    to replace when pointing at a real venue feed.
    """

    def __init__(self, feed_path: str | Path):
        self.feed_path = Path(feed_path)
        self._state: dict[str, EventContext] = {}
        self._versions: dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[str, EventContext, EventContext | None], None]] = []
        self._change_log: list[dict] = []
        self.refresh()

    # -------------------- feed I/O --------------------

    def fetch_raw(self) -> list[dict]:
        with open(self.feed_path) as f:
            return json.load(f)

    def refresh(self, now: datetime | None = None) -> list[str]:
        """Pull the feed and update state. Returns the event_ids that changed."""
        now = now or datetime.now(timezone.utc)
        changed: list[str] = []

        with self._lock:
            for raw in self.fetch_raw():
                eid = raw["event_id"]
                previous = self._state.get(eid)

                incoming = EventContext(**{**raw, "fetched_at": now})

                if previous is None or self._materially_different(previous, incoming):
                    self._versions[eid] += 1
                    incoming.state_version = self._versions[eid]
                    self._state[eid] = incoming
                    changed.append(eid)
                    self._log_change(eid, previous, incoming, now)
                    for cb in self._subscribers:
                        cb(eid, incoming, previous)
                else:
                    # Same substance: refresh the clock, keep the version.
                    previous.fetched_at = now

        return changed

    @staticmethod
    def _materially_different(a: EventContext, b: EventContext) -> bool:
        """Only substantive changes bump the version; clock ticks do not."""
        watched = ("status", "starts_at", "doors_open_at", "last_entry_at",
                   "rescheduled_to", "refund_window_open", "status_note")
        return any(getattr(a, f) != getattr(b, f) for f in watched)

    def _log_change(self, eid, previous, incoming, now):
        self._change_log.append({
            "at": now.isoformat(),
            "event_id": eid,
            "version": incoming.state_version,
            "from_status": previous.status.value if previous else None,
            "to_status": incoming.status.value,
            "note": incoming.status_note,
        })

    # -------------------- accessors --------------------

    def get(self, event_id: str, now: datetime | None = None) -> EventContext:
        with self._lock:
            ctx = self._state.get(event_id)
        if ctx is None:
            raise KeyError(f"No event state for {event_id}")
        return ctx.model_copy(deep=True)

    def subscribe(self, cb: Callable[[str, EventContext, EventContext | None], None]) -> None:
        self._subscribers.append(cb)

    @property
    def change_log(self) -> list[dict]:
        return list(self._change_log)

    def all_events(self) -> list[EventContext]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self._state.values()]


# --------------------------------------------------------------------------
# In-flight decision invalidation
# --------------------------------------------------------------------------

class PendingQueue:
    """
    Decisions awaiting approval, re-evaluated when their event moves so an
    agent cannot approve one that has since become wrong.
    """

    def __init__(self, store: EventStateStore, re_evaluate: Callable[[str], Decision] | None = None):
        self.store = store
        self.re_evaluate = re_evaluate
        self._pending: dict[str, Decision] = {}
        store.subscribe(self._on_event_change)

    def add(self, decision: Decision) -> None:
        self._pending[decision.case_id] = decision

    def remove(self, case_id: str) -> None:
        self._pending.pop(case_id, None)

    def _on_event_change(self, event_id: str, new_ctx: EventContext, old_ctx) -> None:
        for case_id, d in list(self._pending.items()):
            if d.event_id == event_id and d.event_state_version < new_ctx.state_version:
                d.stale = True
                previous_action = d.action.value
                if self.re_evaluate:
                    try:
                        fresh = self.re_evaluate(case_id)
                        # No longer stale, but the UI must still show it moved.
                        fresh.re_evaluated = True
                        fresh.superseded_action = previous_action
                        self._pending[case_id] = fresh
                    except Exception:
                        # Leave it flagged; the UI blocks approval on stale.
                        d.stale = True

    def recently_changed(self) -> list[Decision]:
        return [d for d in self._pending.values() if d.stale or d.re_evaluated]

    def stale_cases(self) -> list[Decision]:
        return [d for d in self._pending.values() if d.stale]

    def all_pending(self) -> list[Decision]:
        return list(self._pending.values())


# --------------------------------------------------------------------------
# Background poller (used by the CLI simulator; Streamlit polls on rerun)
# --------------------------------------------------------------------------

class AdaptivePoller:
    def __init__(self, store: EventStateStore, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _next_interval(self) -> int:
        now = self.clock()
        intervals = []
        for ctx in self.store.all_events():
            mte = (ctx.starts_at - now).total_seconds() / 60.0
            intervals.append(polling_interval_seconds(mte, ctx.status))
        return min(intervals) if intervals else 900

    def _loop(self):
        while not self._stop.is_set():
            self.store.refresh(now=self.clock())
            self._stop.wait(self._next_interval())

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
