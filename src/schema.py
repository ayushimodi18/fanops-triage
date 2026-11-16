"""
Structured case contract. Policy, confidence, comms and audit read from these
models and nothing else.

Case-time facts (what the fan reported) are separate from system-time facts
(what our records say now). Rules are written against system-time facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

class IssueType(str, Enum):
    """Canonical issue taxonomy. UNKNOWN is a valid outcome."""

    MOBILE_DELIVERY_FAILURE = "mobile_delivery_failure"
    PARTIAL_DELIVERY_FAILURE = "partial_delivery_failure"
    SCAN_FAILURE_AT_GATE = "scan_failure_at_gate"
    TRANSFER_STUCK = "transfer_stuck"
    WRONG_SEATS_DELIVERED = "wrong_seats_delivered"
    EVENT_POSTPONED = "event_postponed"
    EVENT_CANCELLED = "event_cancelled"
    EVENT_TIME_CHANGED = "event_time_changed"
    DUPLICATE_ORDER = "duplicate_order"
    FRAUD_REVIEW_HOLD = "fraud_review_hold"
    REFUND_REQUEST = "refund_request"
    UNKNOWN = "unknown"


class TicketStatus(str, Enum):
    DELIVERED = "delivered"                    # barcodes in fan's wallet
    PARTIALLY_DELIVERED = "partially_delivered"
    PENDING_TRANSFER = "pending_transfer"      # seller hasn't pushed yet
    NOT_DELIVERED = "not_delivered"
    REVOKED = "revoked"
    SCANNED = "scanned"                        # already used at gate
    VOID = "void"


class FanTier(str, Enum):
    NEW = "new"              # 0 prior completed orders
    STANDARD = "standard"    # 1-4
    FREQUENT = "frequent"    # 5-19
    VIP = "vip"              # 20+ or high lifetime GMV


class FraudStatus(str, Enum):
    CLEAR = "clear"
    UNDER_REVIEW = "under_review"
    FLAGGED = "flagged"
    CLEARED_AFTER_REVIEW = "cleared_after_review"


class EventStatus(str, Enum):
    ON_SALE = "on_sale"
    GATES_CLOSED = "gates_closed"
    ON_TIME = "on_time"
    DELAYED = "delayed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    RELOCATED = "relocated"
    COMPLETED = "completed"


class DeliveryMethod(str, Enum):
    MOBILE_TRANSFER = "mobile_transfer"   # Ticketmaster/AXS account transfer
    BARCODE_PDF = "barcode_pdf"
    IN_APP = "in_app"                     # app-native barcode
    WILL_CALL = "will_call"


class TimeBucket(str, Enum):
    """Time-to-event. Every rule is gated on this."""

    T_MINUS_LONG = "t_minus_long"          # > 24h
    T_MINUS_DAY = "t_minus_day"            # 4h – 24h
    T_MINUS_HOURS = "t_minus_hours"        # 90m – 4h
    T_MINUS_CRITICAL = "t_minus_critical"  # 0 – 90m  (the spike window)
    IN_EVENT = "in_event"                  # started, last entry not passed
    POST_LAST_ENTRY = "post_last_entry"    # cannot physically get in
    POST_EVENT = "post_event"


class ResolutionAction(str, Enum):
    AUTO_RESEND_LINK = "auto_resend_link"
    AUTO_REISSUE = "auto_reissue"
    MANUAL_TRANSFER_ASSIST = "manual_transfer_assist"
    REPLACEMENT_TICKETS = "replacement_tickets"
    PARTIAL_REFUND = "partial_refund"
    FULL_REFUND = "full_refund"
    PROACTIVE_NOTIFY = "proactive_notify"
    HOLD_PENDING_REVIEW = "hold_pending_review"
    ESCALATE_HUMAN = "escalate_human"
    ESCALATE_URGENT = "escalate_urgent"
    NO_ACTION_INFORMATIONAL = "no_action_informational"


class RouteDecision(str, Enum):
    AUTO_EXECUTE = "auto_execute"       # system acts, logs, notifies fan
    RECOMMEND = "recommend"             # human one-click approve
    ESCALATE = "escalate"               # human owns it from scratch


class Queue(str, Enum):
    GATE_SIDE = "gate_side"             # SLA 3 min — fan is standing at a gate
    URGENT = "urgent"                   # SLA 10 min
    STANDARD = "standard"               # SLA 4 h
    TRUST_AND_SAFETY = "trust_and_safety"
    SENIOR = "senior"                   # VIP / high-value


# Actions the system may execute without a human. Excludes every money-moving
# action at any confidence level.
AUTO_EXECUTABLE: set[ResolutionAction] = {
    ResolutionAction.AUTO_RESEND_LINK,
    ResolutionAction.AUTO_REISSUE,
    ResolutionAction.PROACTIVE_NOTIFY,
    ResolutionAction.NO_ACTION_INFORMATIONAL,
}


# --------------------------------------------------------------------------
# Sub-models
# --------------------------------------------------------------------------

class SeatRef(BaseModel):
    seat_id: str
    section: str
    row: str
    seat: str
    status: TicketStatus
    face_value_usd: float


class OrderContext(BaseModel):
    """Order state from the orders service."""

    order_id: str
    fan_id: str
    event_id: str
    purchased_at: datetime
    order_total_usd: float
    quantity: int
    seats: list[SeatRef] = Field(default_factory=list)
    delivery_method: DeliveryMethod
    ticket_status: TicketStatus
    fraud_status: FraudStatus = FraudStatus.CLEAR
    is_resale: bool = True
    seller_id: str | None = None
    payment_settled: bool = True
    prior_refund_on_order: bool = False

    @computed_field
    @property
    def undelivered_seats(self) -> list[str]:
        bad = {TicketStatus.NOT_DELIVERED, TicketStatus.PENDING_TRANSFER,
               TicketStatus.REVOKED, TicketStatus.VOID}
        return [s.seat_id for s in self.seats if s.status in bad]

    @computed_field
    @property
    def delivered_seats(self) -> list[str]:
        return [s.seat_id for s in self.seats if s.status == TicketStatus.DELIVERED]

    @computed_field
    @property
    def affected_value_usd(self) -> float:
        bad = {TicketStatus.NOT_DELIVERED, TicketStatus.PENDING_TRANSFER,
               TicketStatus.REVOKED, TicketStatus.VOID}
        return round(sum(s.face_value_usd for s in self.seats if s.status in bad), 2)


class FanContext(BaseModel):
    fan_id: str
    tier: FanTier
    lifetime_orders: int = 0
    lifetime_gmv_usd: float = 0.0
    prior_contacts_24h: int = 0            # any topic
    prior_contacts_this_order_24h: int = 0  # the one that actually matters
    prior_auto_resolutions_this_order: int = 0
    refund_requests_90d: int = 0
    preferred_channel: Literal["sms", "email", "push"] = "push"
    locale: str = "en-US"
    display_name: str = "there"


class EventContext(BaseModel):
    """Event state, with freshness metadata used by G04_STALE_EVENT_STATE."""

    event_id: str
    name: str
    venue: str
    city: str
    # IANA zone of the venue. Times are stored and compared in UTC; this is
    # only used to show fans the wall-clock time at the venue (DST included).
    timezone: str = "UTC"
    starts_at: datetime
    doors_open_at: datetime | None = None
    last_entry_at: datetime | None = None
    status: EventStatus = EventStatus.ON_TIME
    status_note: str | None = None
    rescheduled_to: datetime | None = None
    refund_window_open: bool = False
    state_version: int = 1
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.fetched_at).total_seconds())

    def local(self, dt: datetime) -> datetime:
        """`dt` as venue wall-clock time; unknown zones fall back to UTC."""
        try:
            tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)


class InboundReport(BaseModel):
    """What the fan told us. Treated as signal, not truth."""

    case_id: str
    order_id: str
    channel: Literal["app_chat", "sms", "email", "phone", "gate_scanner"] = "app_chat"
    raw_text: str = ""
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fan_stated_issue: IssueType | None = None
    explicit_refund_request: bool = False
    at_venue: bool = False


# --------------------------------------------------------------------------
# The assembled case
# --------------------------------------------------------------------------

class Case(BaseModel):
    """A fully enriched case: the only object the policy engine sees."""

    report: InboundReport
    order: OrderContext
    fan: FanContext
    event: EventContext

    issue_type: IssueType = IssueType.UNKNOWN
    classification_confidence: float = 1.0
    classification_source: Literal["llm", "heuristic", "explicit", "sensor"] = "heuristic"

    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def minutes_to_event(self) -> float:
        return round((self.event.starts_at - self.now).total_seconds() / 60.0, 1)

    @computed_field
    @property
    def time_bucket(self) -> TimeBucket:
        m = self.minutes_to_event
        if m > 24 * 60:
            return TimeBucket.T_MINUS_LONG
        if m > 4 * 60:
            return TimeBucket.T_MINUS_DAY
        if m > 90:
            return TimeBucket.T_MINUS_HOURS
        if m > 0:
            return TimeBucket.T_MINUS_CRITICAL
        if self.event.last_entry_at and self.now > self.event.last_entry_at:
            return TimeBucket.POST_LAST_ENTRY
        if m < -240:  # assume 4h max event duration if no last-entry data
            return TimeBucket.POST_EVENT
        return TimeBucket.IN_EVENT

    @computed_field
    @property
    def can_still_enter(self) -> bool:
        """If false, the fan cannot get in, so reissue is pointless."""
        return self.time_bucket not in {TimeBucket.POST_LAST_ENTRY, TimeBucket.POST_EVENT}

    @computed_field
    @property
    def event_state_age_s(self) -> float:
        return self.event.age_seconds(self.now)

    @computed_field
    @property
    def data_completeness(self) -> float:
        """Fraction of decision-critical fields we actually have."""
        checks = [
            bool(self.order.order_id),
            bool(self.order.seats),
            self.order.ticket_status is not None,
            self.event.starts_at is not None,
            self.event.status is not None,
            self.fan.tier is not None,
            self.order.order_total_usd > 0,
        ]
        return round(sum(checks) / len(checks), 3)


# --------------------------------------------------------------------------
# Decision output
# --------------------------------------------------------------------------

class GuardHit(BaseModel):
    guard_id: str
    reason: str
    forces: RouteDecision | None = None
    blocks_auto: bool = True


class Decision(BaseModel):
    case_id: str
    order_id: str
    event_id: str

    action: ResolutionAction
    secondary_actions: list[ResolutionAction] = Field(default_factory=list)
    # What a guard vetoed, as opposed to secondary_actions, which fire.
    displaced_action: ResolutionAction | None = None
    route: RouteDecision
    queue: Queue = Queue.STANDARD
    sla_minutes: int = 240

    confidence: float
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)

    rule_id: str
    rationale: list[str] = Field(default_factory=list)
    guards_hit: list[GuardHit] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)

    refund_amount_usd: float = 0.0
    goodwill_credit_usd: float = 0.0

    event_state_version: int
    event_state_age_s: float
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stale: bool = False
    re_evaluated: bool = False
    superseded_action: str | None = None

    comms_template: str = ""
    fan_message: str = ""
    message_channel: str = "push"

    policy_version: str = "1.3.0"

    def is_reversible(self) -> bool:
        return self.action in AUTO_EXECUTABLE
