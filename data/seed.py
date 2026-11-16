"""
Regenerates the sample dataset with event times relative to the run time, so
every case lands in a meaningful time bucket.

    python data/seed.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def mins(n: float) -> datetime:
    return NOW + timedelta(minutes=n)


# ==========================================================================
# Events
# ==========================================================================

EVENTS = [
    {
        "event_id": "EVT-1001",
        "name": "Warriors vs Lakers",
        "venue": "Chase Center",
        "city": "San Francisco",
        "timezone": "America/Los_Angeles",
        "starts_at": iso(mins(45)),          # critical window
        "doors_open_at": iso(mins(-30)),
        "last_entry_at": iso(mins(105)),
        "status": "on_time",
        "refund_window_open": False,
    },
    {
        "event_id": "EVT-1002",
        "name": "Hozier — Unreal Unearth Tour",
        "venue": "The Greek Theatre",
        "city": "Berkeley",
        "timezone": "America/Los_Angeles",
        "starts_at": iso(mins(6 * 60)),
        "doors_open_at": iso(mins(5 * 60)),
        "last_entry_at": iso(mins(7 * 60 + 30)),
        "status": "on_time",
        "refund_window_open": False,
    },
    {
        "event_id": "EVT-1003",
        "name": "Giants vs Padres",
        "venue": "Oracle Park",
        "city": "San Francisco",
        "timezone": "America/Los_Angeles",
        "starts_at": iso(mins(3 * 60)),
        "doors_open_at": iso(mins(2 * 60)),
        "last_entry_at": iso(mins(5 * 60)),
        "status": "on_time",                  # flipped to postponed in the demo
        "refund_window_open": False,
    },
    {
        "event_id": "EVT-1004",
        "name": "John Mulaney — Live",
        "venue": "The Masonic",
        "city": "San Francisco",
        "timezone": "America/Los_Angeles",
        "starts_at": iso(mins(30 * 60)),
        "doors_open_at": iso(mins(29 * 60)),
        "last_entry_at": iso(mins(31 * 60)),
        "status": "on_time",
        "refund_window_open": False,
    },
    {
        "event_id": "EVT-1005",
        "name": "Sharks vs Kings",
        "venue": "SAP Center",
        "city": "San Jose",
        "timezone": "America/Los_Angeles",
        "starts_at": iso(mins(20 * 60)),
        "doors_open_at": iso(mins(19 * 60)),
        "last_entry_at": iso(mins(21 * 60)),
        "status": "cancelled",
        "status_note": "Cancelled by the venue due to an arena systems failure.",
        "refund_window_open": True,
    },
]


# ==========================================================================
# Fans
# ==========================================================================

FANS = [
    {"fan_id": "FAN-001", "display_name": "Marcus Webb", "tier": "standard",
     "lifetime_orders": 3, "lifetime_gmv_usd": 640, "prior_contacts_24h": 0,
     "prior_contacts_this_order_24h": 0, "preferred_channel": "push"},

    {"fan_id": "FAN-002", "display_name": "Priya Raman", "tier": "frequent",
     "lifetime_orders": 11, "lifetime_gmv_usd": 3120, "prior_contacts_24h": 1,
     "prior_contacts_this_order_24h": 1, "preferred_channel": "email"},

    {"fan_id": "FAN-003", "display_name": "Dan Kowalczyk", "tier": "standard",
     "lifetime_orders": 4, "lifetime_gmv_usd": 890, "prior_contacts_24h": 3,
     "prior_contacts_this_order_24h": 3, "prior_auto_resolutions_this_order": 2,
     "preferred_channel": "sms"},

    {"fan_id": "FAN-004", "display_name": "Alicia Thorne", "tier": "new",
     "lifetime_orders": 0, "lifetime_gmv_usd": 0, "prior_contacts_24h": 0,
     "prior_contacts_this_order_24h": 0, "preferred_channel": "email"},

    {"fan_id": "FAN-005", "display_name": "Jordan Reyes", "tier": "vip",
     "lifetime_orders": 41, "lifetime_gmv_usd": 18400, "prior_contacts_24h": 0,
     "prior_contacts_this_order_24h": 0, "preferred_channel": "sms"},

    {"fan_id": "FAN-006", "display_name": "Tomas Ibarra", "tier": "standard",
     "lifetime_orders": 2, "lifetime_gmv_usd": 310, "prior_contacts_24h": 0,
     "prior_contacts_this_order_24h": 0, "preferred_channel": "push"},

    {"fan_id": "FAN-007", "display_name": "Nina Achebe", "tier": "frequent",
     "lifetime_orders": 8, "lifetime_gmv_usd": 2100, "prior_contacts_24h": 0,
     "prior_contacts_this_order_24h": 0, "preferred_channel": "push"},
]


def seats(n, section, row, status, price, start=1):
    return [
        {"seat_id": f"{section}-{row}-{start+i}", "section": section, "row": row,
         "seat": str(start + i), "status": status, "face_value_usd": price}
        for i in range(n)
    ]


# ==========================================================================
# Orders
# ==========================================================================

ORDERS = [
    # C-001, delivery failure inside the critical window
    {"order_id": "ORD-5001", "fan_id": "FAN-001", "event_id": "EVT-1001",
     "purchased_at": iso(mins(-2880)), "order_total_usd": 184.00, "quantity": 2,
     "seats": seats(2, "212", "8", "not_delivered", 92.00),
     "delivery_method": "mobile_transfer", "ticket_status": "not_delivered",
     "fraud_status": "clear", "seller_id": "SEL-77", "payment_settled": True},

    # C-002, partial delivery on a 4-seat order with runway
    {"order_id": "ORD-5002", "fan_id": "FAN-002", "event_id": "EVT-1002",
     "purchased_at": iso(mins(-7200)), "order_total_usd": 476.00, "quantity": 4,
     "seats": (seats(2, "GA-B", "—", "delivered", 119.00, start=1)
               + seats(2, "GA-B", "—", "pending_transfer", 119.00, start=3)),
     "delivery_method": "mobile_transfer", "ticket_status": "partially_delivered",
     "fraud_status": "clear", "seller_id": "SEL-91", "payment_settled": True},

    # C-003, third contact today, automation already tried twice
    {"order_id": "ORD-5003", "fan_id": "FAN-003", "event_id": "EVT-1003",
     "purchased_at": iso(mins(-4320)), "order_total_usd": 214.00, "quantity": 2,
     "seats": seats(2, "118", "22", "not_delivered", 107.00),
     "delivery_method": "mobile_transfer", "ticket_status": "not_delivered",
     "fraud_status": "clear", "seller_id": "SEL-12", "payment_settled": True},

    # C-004, high value + fraud review, morning of the event
    {"order_id": "ORD-5004", "fan_id": "FAN-004", "event_id": "EVT-1002",
     "purchased_at": iso(mins(-180)), "order_total_usd": 1240.00, "quantity": 4,
     "seats": seats(4, "PIT", "A", "not_delivered", 310.00),
     "delivery_method": "mobile_transfer", "ticket_status": "not_delivered",
     "fraud_status": "under_review", "seller_id": "SEL-04", "payment_settled": False},

    # C-005, scan failure at the gate
    {"order_id": "ORD-5005", "fan_id": "FAN-006", "event_id": "EVT-1001",
     "purchased_at": iso(mins(-10080)), "order_total_usd": 156.00, "quantity": 2,
     "seats": seats(2, "224", "14", "delivered", 78.00),
     "delivery_method": "in_app", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-33", "payment_settled": True},

    # C-006, will be swept by the postponement incident
    {"order_id": "ORD-5006", "fan_id": "FAN-007", "event_id": "EVT-1003",
     "purchased_at": iso(mins(-20160)), "order_total_usd": 298.00, "quantity": 2,
     "seats": seats(2, "204", "5", "delivered", 149.00),
     "delivery_method": "in_app", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-51", "payment_settled": True},

    # C-007, ambiguous free text
    {"order_id": "ORD-5007", "fan_id": "FAN-001", "event_id": "EVT-1004",
     "purchased_at": iso(mins(-1440)), "order_total_usd": 118.00, "quantity": 2,
     "seats": seats(2, "ORCH", "M", "delivered", 59.00),
     "delivery_method": "in_app", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-19", "payment_settled": True},

    # C-008 — backend says delivered, fan can't see them
    {"order_id": "ORD-5008", "fan_id": "FAN-007", "event_id": "EVT-1002",
     "purchased_at": iso(mins(-5760)), "order_total_usd": 238.00, "quantity": 2,
     "seats": seats(2, "TERR", "C", "delivered", 119.00),
     "delivery_method": "mobile_transfer", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-62", "payment_settled": True},

    # C-009, VIP with a stuck seller transfer, close to doors
    {"order_id": "ORD-5009", "fan_id": "FAN-005", "event_id": "EVT-1001",
     "purchased_at": iso(mins(-600)), "order_total_usd": 720.00, "quantity": 2,
     "seats": seats(2, "CRT", "1", "pending_transfer", 360.00),
     "delivery_method": "mobile_transfer", "ticket_status": "pending_transfer",
     "fraud_status": "clear", "seller_id": "SEL-88", "payment_settled": True},

    # C-011 / C-012, the boring majority: simple delivery failures with runway
    {"order_id": "ORD-5011", "fan_id": "FAN-006", "event_id": "EVT-1002",
     "purchased_at": iso(mins(-3000)), "order_total_usd": 142.00, "quantity": 2,
     "seats": seats(2, "LAWN", "—", "not_delivered", 71.00),
     "delivery_method": "mobile_transfer", "ticket_status": "not_delivered",
     "fraud_status": "clear", "seller_id": "SEL-21", "payment_settled": True},

    {"order_id": "ORD-5012", "fan_id": "FAN-001", "event_id": "EVT-1004",
     "purchased_at": iso(mins(-960)), "order_total_usd": 96.00, "quantity": 2,
     "seats": seats(2, "BALC", "F", "delivered", 48.00),
     "delivery_method": "mobile_transfer", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-08", "payment_settled": True},

    # C-010, cancelled event
    {"order_id": "ORD-5010", "fan_id": "FAN-002", "event_id": "EVT-1005",
     "purchased_at": iso(mins(-8640)), "order_total_usd": 264.00, "quantity": 2,
     "seats": seats(2, "116", "9", "delivered", 132.00),
     "delivery_method": "in_app", "ticket_status": "delivered",
     "fraud_status": "clear", "seller_id": "SEL-40", "payment_settled": True},
]


# ==========================================================================
# Inbound cases
# ==========================================================================

CASES = [
    {"case_id": "CASE-001", "label": "Delivery failure, 45 min to tip-off",
     "report": {"case_id": "CASE-001", "order_id": "ORD-5001", "channel": "app_chat",
                "raw_text": "I have nothing in the app for tonight and we're already parked. No barcode, nothing. Tickets are gone.",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-002", "label": "Partial delivery, 4 seats, 6h runway",
     "report": {"case_id": "CASE-002", "order_id": "ORD-5002", "channel": "email",
                "raw_text": "Only 2 of the 4 tickets showed up in my account. My brother and his wife have nothing. Can you sort this before tonight?",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-003", "label": "Third contact today, same order",
     "report": {"case_id": "CASE-003", "order_id": "ORD-5003", "channel": "sms",
                "raw_text": "This is the third time I'm messaging. You keep saying the tickets are reissued and I still can't find my tickets anywhere.",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-004", "label": "High-value order under fraud review",
     "report": {"case_id": "CASE-004", "order_id": "ORD-5004", "channel": "email",
                "raw_text": "I bought 4 pit tickets this morning and they still haven't come through. It's a lot of money, please confirm they're real.",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-005", "label": "Scan failure at the gate",
     "report": {"case_id": "CASE-005", "order_id": "ORD-5005", "channel": "app_chat",
                "raw_text": "We're at the gate and the scanner won't take either ticket, it just shows a red X. Staff told us to contact you.",
                "received_at": iso(NOW), "at_venue": True}},

    {"case_id": "CASE-007", "label": "Ambiguous free text",
     "report": {"case_id": "CASE-007", "order_id": "ORD-5007", "channel": "app_chat",
                "raw_text": "hey so about my order for saturday — something seems off with it, can someone check?",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-008", "label": "Backend delivered, fan can't see tickets",
     "report": {"case_id": "CASE-008", "order_id": "ORD-5008", "channel": "app_chat",
                "raw_text": "I don't see my tickets in the app for tonight's show. My friend got hers fine.",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-009", "label": "VIP, seller transfer stuck, 45 min out",
     "report": {"case_id": "CASE-009", "order_id": "ORD-5009", "channel": "sms",
                "raw_text": "The seller hasn't sent the courtside tickets yet and the game starts in under an hour. What's happening?",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-011", "label": "Routine delivery failure, 6h runway",
     "report": {"case_id": "CASE-011", "order_id": "ORD-5011", "channel": "app_chat",
                "raw_text": "I haven't received my tickets for tonight yet, nothing in my wallet.",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-012", "label": "Routine transfer confusion, 30h out",
     "report": {"case_id": "CASE-012", "order_id": "ORD-5012", "channel": "email",
                "raw_text": "I can't find my ticket for the Mulaney show, is something wrong with my order?",
                "received_at": iso(NOW)}},

    {"case_id": "CASE-010", "label": "Cancelled event",
     "report": {"case_id": "CASE-010", "order_id": "ORD-5010", "channel": "app_chat",
                "raw_text": "I just saw the game is called off. What happens to my money?",
                "received_at": iso(NOW), "explicit_refund_request": True}},
]


def main(out: Path = HERE):
    out.mkdir(parents=True, exist_ok=True)
    (out / "events.json").write_text(json.dumps(EVENTS, indent=2))
    (out / "orders.json").write_text(json.dumps(ORDERS, indent=2))
    (out / "fans.json").write_text(json.dumps(FANS, indent=2))
    (out / "cases.json").write_text(json.dumps(CASES, indent=2))
    # Written last, so a reader that sees it knows the four files are complete.
    (out / ".anchor").write_text(iso(NOW))
    print(f"Seeded {len(EVENTS)} events, {len(ORDERS)} orders, "
          f"{len(FANS)} fans, {len(CASES)} cases  (anchor: {NOW:%Y-%m-%d %H:%M UTC})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE,
                    help="directory to write the dataset to (default: data/)")
    main(ap.parse_args().out)
