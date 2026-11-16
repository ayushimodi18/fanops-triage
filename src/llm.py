"""
Issue classification and fan-message polish.

THE LLM IS NOT THE DECISION AUTHORITY. It runs at the edges only: free text ->
IssueType, and approved draft -> readable message. It never selects a
resolution action, sets a refund amount, or changes a route — those come from
policy.py and confidence.py, which contain no model calls. A wrong
classification lowers confidence and routes more work to humans; it cannot move
money or grant venue access.

With no provider configured, or on any failure, both jobs fall back to the
deterministic paths below.
"""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
import urllib.request
from dataclasses import dataclass
from typing import Any

from .schema import IssueType

TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "8"))


@dataclass
class Provider:
    backend: str            # anthropic | openai_compatible | deterministic
    model: str
    label: str
    endpoint: str | None = None


def resolve() -> Provider:
    """Pick a backend from the environment. LLM_PROVIDER=deterministic forces it off."""
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip()
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")

    if forced != "deterministic":
        if forced in {"", "anthropic"} and os.environ.get("ANTHROPIC_API_KEY"):
            model = model or "claude-sonnet-4-6"
            return Provider("anthropic", model, f"Anthropic \u00b7 {model}",
                            "https://api.anthropic.com/v1/messages")

        if forced in {"", "openai_compatible"} and base and os.environ.get("LLM_API_KEY"):
            model = model or "llama-3.3-70b-versatile"
            host = base.split("//")[-1].split("/")[0]
            return Provider("openai_compatible", model, f"{host} \u00b7 {model}",
                            f"{base}/chat/completions")

    return Provider("deterministic", "-", "deterministic")


def llm_available() -> bool:
    return resolve().backend != "deterministic"


def provider_label() -> str:
    return resolve().label


def complete(system: str, user: str, max_tokens: int = 600,
             provider: Provider | None = None) -> str | None:
    """Model text, or None on any failure. None means use the fallback."""
    p = provider or resolve()

    try:
        if p.backend == "anthropic":
            body = {"model": p.model, "max_tokens": max_tokens,
                    "system": system, "messages": [{"role": "user", "content": user}]}
            headers = {"content-type": "application/json",
                       "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                       "anthropic-version": "2023-06-01"}
            data = _post(p.endpoint, body, headers)
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text") or None

        if p.backend == "openai_compatible":
            body = {"model": p.model, "max_tokens": max_tokens, "temperature": 0,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            headers = {"content-type": "application/json",
                       "authorization": f"Bearer {os.environ['LLM_API_KEY']}"}
            data = _post(p.endpoint, body, headers)
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or None

    except Exception:
        return None

    return None


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


# ==========================================================================
# Classification
# ==========================================================================

CLASSIFIER_SYSTEM = """You classify inbound fan support messages for a live-event \
ticket marketplace on event day.

Return ONLY a JSON object, no prose, no markdown fences:
{"issue_type": "<one of the allowed values>", "confidence": <0.0-1.0>, \
"explicit_refund_request": <bool>, "at_venue": <bool>, "signals": ["<short phrase>", ...]}

Allowed issue_type values:
mobile_delivery_failure, partial_delivery_failure, scan_failure_at_gate,
transfer_stuck, wrong_seats_delivered, event_postponed, event_cancelled,
event_time_changed, duplicate_order, fraud_review_hold, refund_request, unknown

Calibration rules:
- Use "unknown" when the message is genuinely ambiguous.
- Report confidence below 0.7 whenever two labels are plausible.
- "partial_delivery_failure" requires the fan to indicate SOME tickets arrived.
  If they say all tickets are missing, that is mobile_delivery_failure.
- "scan_failure_at_gate" requires the fan to be AT the venue trying to enter.
  "I'm worried it won't scan" from home is not a scan failure.
- explicit_refund_request must be true only if the fan actually asks for money
  back. Frustration is not a refund request."""


# Deterministic classifier: weighted evidence, not first-match. The margin
# between the top two labels sets confidence.
#
# (phrase, issue, weight). Weights are ordinal:
#   3 = near-decisive   2 = strong   1 = weak on its own
_EVIDENCE: list[tuple[str, IssueType, int]] = [
    # gate scan: fan is physically at a turnstile
    ("won't scan", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("wont scan", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("not scanning", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("won't take", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("wouldn't accept", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("scanner", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("turnstile", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("red x", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("invalid ticket", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("already scanned", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("staff told us", IssueType.SCAN_FAILURE_AT_GATE, 1),
    ("security won't", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("scan isn't working", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("scan isnt working", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("scan failed", IssueType.SCAN_FAILURE_AT_GATE, 3),
    ("won't let us in", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("wont let us in", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("won't let me in", IssueType.SCAN_FAILURE_AT_GATE, 2),
    ("wont let me in", IssueType.SCAN_FAILURE_AT_GATE, 2),

    # partial: some arrived, some didn't
    ("only 2 of", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("only two of", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("only 3 of", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("only three of", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("2 of the 4", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("3 of the 4", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("only got", IssueType.PARTIAL_DELIVERY_FAILURE, 2),
    ("only received", IssueType.PARTIAL_DELIVERY_FAILURE, 2),
    ("only showed up", IssueType.PARTIAL_DELIVERY_FAILURE, 2),
    ("some of my tickets", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("rest of my tickets", IssueType.PARTIAL_DELIVERY_FAILURE, 3),
    ("missing one", IssueType.PARTIAL_DELIVERY_FAILURE, 2),
    ("one is missing", IssueType.PARTIAL_DELIVERY_FAILURE, 2),
    ("have nothing", IssueType.PARTIAL_DELIVERY_FAILURE, 1),

    # seller transfer stuck
    ("seller hasn't", IssueType.TRANSFER_STUCK, 3),
    ("seller has not", IssueType.TRANSFER_STUCK, 3),
    ("seller still", IssueType.TRANSFER_STUCK, 2),
    ("pending transfer", IssueType.TRANSFER_STUCK, 3),
    ("transfer pending", IssueType.TRANSFER_STUCK, 3),
    ("waiting on the seller", IssueType.TRANSFER_STUCK, 3),
    ("accept transfer", IssueType.TRANSFER_STUCK, 2),
    ("hasn't sent", IssueType.TRANSFER_STUCK, 2),
    ("not released", IssueType.TRANSFER_STUCK, 2),

    # wrong seats
    ("wrong seats", IssueType.WRONG_SEATS_DELIVERED, 3),
    ("wrong section", IssueType.WRONG_SEATS_DELIVERED, 3),
    ("different section", IssueType.WRONG_SEATS_DELIVERED, 2),
    ("not the seats", IssueType.WRONG_SEATS_DELIVERED, 3),
    ("different seats", IssueType.WRONG_SEATS_DELIVERED, 2),
    ("not what i booked", IssueType.WRONG_SEATS_DELIVERED, 2),

    # duplicate
    ("charged twice", IssueType.DUPLICATE_ORDER, 3),
    ("double charged", IssueType.DUPLICATE_ORDER, 3),
    ("charged me twice", IssueType.DUPLICATE_ORDER, 3),
    ("two orders", IssueType.DUPLICATE_ORDER, 2),
    ("duplicate", IssueType.DUPLICATE_ORDER, 3),
    ("ordered twice", IssueType.DUPLICATE_ORDER, 3),
    ("two charges", IssueType.DUPLICATE_ORDER, 2),

    # event-level
    ("postponed", IssueType.EVENT_POSTPONED, 3),
    ("rescheduled", IssueType.EVENT_POSTPONED, 3),
    ("new date", IssueType.EVENT_POSTPONED, 2),
    ("moved to another day", IssueType.EVENT_POSTPONED, 3),
    ("cancelled", IssueType.EVENT_CANCELLED, 3),
    ("canceled", IssueType.EVENT_CANCELLED, 3),
    ("called off", IssueType.EVENT_CANCELLED, 3),
    ("not happening", IssueType.EVENT_CANCELLED, 2),
    ("start time", IssueType.EVENT_TIME_CHANGED, 2),
    ("delayed", IssueType.EVENT_TIME_CHANGED, 2),
    ("what time", IssueType.EVENT_TIME_CHANGED, 2),
    ("confused about the time", IssueType.EVENT_TIME_CHANGED, 3),
    ("time changed", IssueType.EVENT_TIME_CHANGED, 3),
    ("different time", IssueType.EVENT_TIME_CHANGED, 2),
    ("doors open", IssueType.EVENT_TIME_CHANGED, 1),

    # whole-order delivery failure
    ("no tickets", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("nothing in the app", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("nothing in my wallet", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("not in my wallet", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("tickets are gone", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("can't find my ticket", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("cant find my ticket", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("don't see my ticket", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("dont see my ticket", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("haven't received", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("havent received", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("haven't come through", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("havent come through", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("not come through", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("still haven't", IssueType.MOBILE_DELIVERY_FAILURE, 1),
    ("no barcode", IssueType.MOBILE_DELIVERY_FAILURE, 3),
    ("never arrived", IssueType.MOBILE_DELIVERY_FAILURE, 2),
    ("didn't arrive", IssueType.MOBILE_DELIVERY_FAILURE, 2),

    # refund ask: weak on its own, it is a remedy rather than the issue
    ("refund", IssueType.REFUND_REQUEST, 1),
    ("money back", IssueType.REFUND_REQUEST, 1),
    ("cancel my order", IssueType.REFUND_REQUEST, 2),
]

# Substring matching gets these backwards: "the scan worked fine" has "scan".
_NEGATIONS = [
    ("scanned fine", IssueType.SCAN_FAILURE_AT_GATE),
    ("scanned ok", IssueType.SCAN_FAILURE_AT_GATE),
    ("scan worked", IssueType.SCAN_FAILURE_AT_GATE),
    ("got in fine", IssueType.SCAN_FAILURE_AT_GATE),
    ("tickets arrived", IssueType.MOBILE_DELIVERY_FAILURE),
    ("tickets came through", IssueType.MOBILE_DELIVERY_FAILURE),
    ("found my tickets", IssueType.MOBILE_DELIVERY_FAILURE),
    ("all four arrived", IssueType.PARTIAL_DELIVERY_FAILURE),
    ("not asking for a refund", IssueType.REFUND_REQUEST),
    ("don't want a refund", IssueType.REFUND_REQUEST),
    ("dont want a refund", IssueType.REFUND_REQUEST),
]

_AT_VENUE = ["at the gate", "at the venue", "at the entrance", "at the stadium",
             "at the arena", "outside the stadium", "in line", "in the queue",
             "here at", "we're here", "im here", "i'm here", "already parked",
             "standing at"]

_REFUND_ASK = ["refund", "money back", "my money", "cancel my order",
               "want my money"]


def _normalise(text: str) -> str:
    """Lower-case, straighten phone-keyboard apostrophes, collapse whitespace."""
    text = (text or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"\s+", " ", text).strip()


def _word_close(a: str, b: str) -> bool:
    """
    One-typo tolerance per word, kept strict on purpose: short words must match
    exactly (so "no" never becomes "my", "date" never "data") and the first
    letter must agree.
    """
    if a == b:
        return True
    if len(b) < 5 or not a or a[0] != b[0] or abs(len(a) - len(b)) > 2:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def _fuzzy_contains(tokens: list[str], phrase: str) -> bool:
    words = phrase.split()
    n = len(words)
    for i in range(len(tokens) - n + 1):
        if all(_word_close(tokens[i + j], words[j]) for j in range(n)):
            return True
    return False


def _score_labels(text: str) -> tuple[dict[IssueType, int], list[str]]:
    scores: dict[IssueType, int] = {}
    matched: list[str] = []
    tokens = re.findall(r"[a-z0-9']+", text)
    fuzzy_hit: set[IssueType] = set()

    for phrase, issue, weight in _EVIDENCE:
        if phrase in text:
            scores[issue] = scores.get(issue, 0) + weight
            matched.append(phrase)
        elif issue not in fuzzy_hit and _fuzzy_contains(tokens, phrase):
            # A misspelt phrase is weaker evidence than an exact one, and one
            # typo can match several spellings of the same phrase
            # ("cancled" ~ cancelled/canceled), so count one fuzzy hit per label.
            fuzzy_hit.add(issue)
            scores[issue] = scores.get(issue, 0) + max(1, weight - 1)
            matched.append(f"~{phrase}")

    for phrase, issue in _NEGATIONS:
        if phrase in text and issue in scores:
            # Argues against the label, so the score may go below zero.
            scores[issue] -= 4
            matched.append(f"NOT:{phrase}")

    return {k: v for k, v in scores.items() if v > 0}, matched


def _confidence_from_margin(top: int, second: int) -> float:
    """Confidence from the winner's margin, not the total evidence found."""
    if top <= 0:
        return 0.3
    margin = top - second
    if margin >= 4:
        base = 0.90
    elif margin == 3:
        base = 0.86
    elif margin == 2:
        base = 0.80
    elif margin == 1:
        base = 0.66
    else:                       # tie: genuinely ambiguous
        base = 0.48
    # A decisive phrase outweighs an accumulation of weak ones.
    if top >= 5:
        base = min(base + 0.03, 0.93)
    return round(base, 3)


def classify(raw_text: str, fallback: IssueType | None = None) -> dict[str, Any]:
    """
    Returns {issue_type, confidence, source, explicit_refund_request, at_venue, signals}
    """
    text = _normalise(raw_text)

    if not text:
        return {
            "issue_type": fallback or IssueType.UNKNOWN,
            "confidence": 1.0 if fallback else 0.3,
            "source": "explicit" if fallback else "heuristic",
            "explicit_refund_request": False,
            "at_venue": False,
            "signals": [],
        }

    out = complete(CLASSIFIER_SYSTEM, raw_text, max_tokens=300)
    if out:
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
            parsed = json.loads(cleaned)
            return {
                "issue_type": IssueType(parsed["issue_type"]),
                "confidence": float(parsed.get("confidence", 0.7)),
                "source": "llm",
                "explicit_refund_request": bool(parsed.get("explicit_refund_request", False)),
                "at_venue": bool(parsed.get("at_venue", False)),
                "signals": parsed.get("signals", []),
            }
        except Exception:
            pass  # fall through to keywords

    return _keyword_classify(text, fallback)


def _keyword_classify(text: str, fallback: IssueType | None) -> dict[str, Any]:
    scores, matched = _score_labels(text)

    refund_ask = any(p in text for p in _REFUND_ASK) and not any(
        n in text for n, i in _NEGATIONS if i == IssueType.REFUND_REQUEST
    )
    at_venue = any(p in text for p in _AT_VENUE)

    # Being at a venue is strong contextual evidence for a gate scan failure.
    if at_venue and IssueType.SCAN_FAILURE_AT_GATE in scores:
        scores[IssueType.SCAN_FAILURE_AT_GATE] += 2

    if not scores:
        return {
            "issue_type": fallback or IssueType.UNKNOWN,
            "confidence": 0.85 if fallback else 0.32,
            "source": "explicit" if fallback else "heuristic",
            "explicit_refund_request": refund_ask,
            "at_venue": at_venue,
            "signals": [],
        }

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    confidence = _confidence_from_margin(best_score, second_score)

    # A refund ask is a remedy, not a diagnosis: if it only wins because
    # nothing else scored, cap the confidence.
    if best == IssueType.REFUND_REQUEST and best_score <= 2:
        confidence = min(confidence, 0.55)

    return {
        "issue_type": best,
        "confidence": confidence,
        "source": "heuristic",
        "explicit_refund_request": refund_ask,
        "at_venue": at_venue,
        "signals": matched[:4],
    }


# ==========================================================================
# Message polish
# ==========================================================================

COMMS_SYSTEM = """You rewrite a pre-approved customer service message for a \
live-event ticket marketplace so it reads naturally.

Hard constraints:
- Do NOT change any fact, number, dollar amount, seat, time or promise.
- Do NOT add commitments that are not in the draft.
- Do NOT apologise more than once.
- Keep it under 65 words.
- Lead with the outcome, not the apology.
- Plain sentences. No corporate filler, no exclamation marks.

Return only the rewritten message."""


def polish(draft: str, tone_hint: str = "") -> tuple[str, str]:
    """Returns (message, source). Falls back to the template verbatim."""
    if not llm_available():
        return draft, "template"
    out = complete(COMMS_SYSTEM, f"{tone_hint}\n\nDraft:\n{draft}".strip(),
                   max_tokens=300)
    if not out or len(out.strip()) < 10:
        return draft, "template"
    return out.strip(), "llm"
