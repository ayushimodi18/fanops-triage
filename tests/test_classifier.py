"""
Deterministic classifier, which runs when no provider is configured or a call
fails. Includes the negation cases substring matching gets backwards.

    python tests/test_classifier.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.llm import classify
from src.schema import IssueType

IT = IssueType


# (message, expected_label, note)
LABEL_CASES = [
    # clear signals
    ("I have nothing in the app for tonight and we're already parked. No barcode.",
     IT.MOBILE_DELIVERY_FAILURE, "whole-order failure"),
    ("Only 2 of the 4 tickets showed up in my account. My brother has nothing.",
     IT.PARTIAL_DELIVERY_FAILURE, "partial, not whole"),
    ("We're at the gate and the scanner won't take either ticket, it shows a red X.",
     IT.SCAN_FAILURE_AT_GATE, "at a turnstile"),
    ("The seller hasn't released the tickets yet and the game is soon.",
     IT.TRANSFER_STUCK, "seller-side"),
    ("I just saw the game is called off. What happens to my money?",
     IT.EVENT_CANCELLED, "event-level beats refund ask"),
    ("You charged me twice for the same seats.",
     IT.DUPLICATE_ORDER, "duplicate"),
    ("I got a different section than what I booked.",
     IT.WRONG_SEATS_DELIVERED, "wrong inventory"),

    # negation: substring matching gets these backwards
    ("My tickets arrived fine but I want a refund anyway.",
     IT.REFUND_REQUEST, "negation suppresses delivery failure"),
    ("The scan worked but we got the wrong seats.",
     IT.WRONG_SEATS_DELIVERED, "negation suppresses scan failure"),
    ("Tickets came through, I'm not asking for a refund, just confused about the time.",
     IT.EVENT_TIME_CHANGED, "two negations, weak signal wins"),

    # ambiguous: must report unknown rather than guess
    ("hey so about my order for saturday — something seems off, can someone check?",
     IT.UNKNOWN, "no evidence at all"),
]


def test_labels():
    failures = []
    for text, expected, note in LABEL_CASES:
        got = classify(text)["issue_type"]
        if got != expected:
            failures.append(f"{note}: expected {expected.value}, got {got.value}\n"
                            f"        {text!r}")
    assert not failures, "\n      " + "\n      ".join(failures)


def test_ambiguous_low_confidence():
    """Ambiguous text must score below the escalation threshold."""
    ambiguous = [
        "hey so about my order for saturday — something seems off, can someone check?",
        "there's a problem with my order",
        "can someone help me",
    ]
    for text in ambiguous:
        r = classify(text)
        assert r["confidence"] < 0.60, \
            f"{text!r} scored {r['confidence']} — would bypass escalation"


def test_clear_high_confidence():
    clear = [
        "I have no tickets in the app and no barcode for tonight.",
        "The scanner won't scan either ticket, we're at the turnstile.",
        "The seller hasn't sent the transfer.",
    ]
    for text in clear:
        r = classify(text)
        assert r["confidence"] >= 0.80, f"{text!r} only scored {r['confidence']}"


def test_conflicting_evidence_lowers_confidence():
    """Conflicting phrases are less certain than one decisive phrase."""
    decisive = classify("The scanner won't scan, red X at the turnstile.")
    conflicted = classify(
        "My tickets never arrived and the scanner won't take them and "
        "the seller hasn't sent anything and I want a refund."
    )
    assert conflicted["confidence"] < decisive["confidence"], (
        f"conflicted {conflicted['confidence']} not below "
        f"decisive {decisive['confidence']}"
    )


def test_refund_ask_is_not_a_diagnosis():
    """A refund ask is a proposed remedy, not a diagnosis."""
    r = classify("I'd like a refund please.")
    assert r["confidence"] <= 0.60, \
        f"bare refund ask scored {r['confidence']} — too confident for a remedy-only message"
    assert r["explicit_refund_request"] is True


def test_at_venue_detection():
    assert classify("We're at the gate and it won't scan")["at_venue"] is True
    assert classify("I'm worried it won't scan tomorrow")["at_venue"] is False


def test_explicit_fallback_beats_inference():

    r = classify("", fallback=IT.EVENT_POSTPONED)
    assert r["issue_type"] == IT.EVENT_POSTPONED
    assert r["source"] == "explicit"
    assert r["confidence"] >= 0.85


def test_empty_input_low_confidence():
    r = classify("")
    assert r["issue_type"] == IT.UNKNOWN
    assert r["confidence"] < 0.5


def test_signals_returned():
    """Matched phrases are returned so a classification can be audited."""
    r = classify("Only 2 of the 4 tickets showed up, the rest of my tickets are missing.")
    assert r["signals"], "no signals returned — classification is unauditable"


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
