"""
Provider wiring for src/llm.py, against a local mock server so both wire
formats are exercised without a key or network access.

    python tests/test_llm.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src import llm

RECEIVED: list[dict] = []


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        # urllib normalises header case, so compare lowercased.
        RECEIVED.append({"path": self.path, "body": body,
                         "headers": {k.lower(): v for k, v in self.headers.items()}})

        if "/v1/messages" in self.path:
            self._json({"content": [{"type": "text", "text": "ANTHROPIC_OK"}]})
        elif "/chat/completions" in self.path:
            self._json({"choices": [{"message": {"content": "OPENAI_OK"}}]})
        else:
            self.send_error(404)

    def _json(self, obj):
        payload = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_server() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class env:
    """Sets env vars for the block, restoring them after."""

    KEYS = ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
            "ANTHROPIC_API_KEY")

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k in self.KEYS:
            self.old[k] = os.environ.get(k)
            os.environ.pop(k, None)
        os.environ.update(self.kw)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Private per-run temp dir, so audit logs never collide on a shared /tmp path.
TMP = Path(tempfile.mkdtemp(prefix="fanops-llm-"))

SRV, BASE = start_server()


# ==========================================================================

def test_deterministic_when_unconfigured():
    with env():
        assert llm.resolve().backend == "deterministic"
        assert llm.llm_available() is False
        assert llm.complete("sys", "user") is None


def test_deterministic_can_be_forced():
    """Switch the model off without unsetting keys."""
    with env(LLM_PROVIDER="deterministic", ANTHROPIC_API_KEY="sk-ant-test"):
        assert llm.resolve().backend == "deterministic"


def test_anthropic_request_shape():
    RECEIVED.clear()
    with env(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test",
             LLM_MODEL="test-model"):
        p = llm.resolve()
        p.endpoint = f"{BASE}/v1/messages"
        out = llm.complete("SYSTEM", "USER", provider=p)

    assert out == "ANTHROPIC_OK", f"got {out!r}"
    sent = RECEIVED[-1]
    assert sent["body"]["system"] == "SYSTEM"
    assert sent["body"]["messages"][0]["content"] == "USER"
    assert sent["body"]["model"] == "test-model"
    assert sent["headers"].get("x-api-key") == "sk-ant-test"
    assert sent["headers"].get("anthropic-version") == "2023-06-01"


def test_openai_compatible_request_shape():
    """Covers Groq, Together, OpenRouter, vLLM, LM Studio, Ollama's /v1."""
    RECEIVED.clear()
    with env(LLM_BASE_URL=BASE, LLM_API_KEY="gsk_test",
             LLM_MODEL="llama-3.3-70b-versatile"):
        out = llm.complete("SYSTEM", "USER")

    assert out == "OPENAI_OK", f"got {out!r}"
    sent = RECEIVED[-1]
    msgs = sent["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYSTEM"}
    assert msgs[1] == {"role": "user", "content": "USER"}
    assert sent["body"]["model"] == "llama-3.3-70b-versatile"
    assert sent["body"]["temperature"] == 0, "classification must be deterministic"
    assert sent["headers"].get("authorization") == "Bearer gsk_test"


def test_anthropic_wins_when_both_configured():
    with env(ANTHROPIC_API_KEY="sk-ant-test", LLM_BASE_URL=BASE, LLM_API_KEY="x"):
        assert llm.resolve().backend == "anthropic"


def test_unreachable_provider_returns_none():
    """Must not raise; the fallback path depends on None."""
    with env(LLM_BASE_URL="http://127.0.0.1:9",   # discard port, always refuses
             LLM_API_KEY="x"):
        assert llm.complete("s", "u") is None


def test_bad_host_returns_none():
    with env(LLM_BASE_URL="http://no-such-host.invalid/v1", LLM_API_KEY="x"):
        assert llm.complete("s", "u") is None


def test_pipeline_triages_with_a_dead_provider():
    """A configured but unreachable provider still yields every decision."""
    from src.pipeline import TriageEngine

    with env(LLM_BASE_URL="http://127.0.0.1:9", LLM_API_KEY="x"):
        e = TriageEngine(ROOT / "data" / "events.json",
                         ROOT / "data" / "orders.json",
                         ROOT / "data" / "fans.json",
                         TMP / "audit.jsonl",
                         use_llm=True)
        cases = json.loads((ROOT / "data" / "cases.json").read_text())
        for raw in cases:
            d = e.triage(raw)
            assert d.action is not None
            assert d.fan_message and len(d.fan_message) > 40, \
                f"{d.case_id} produced no message with a dead provider"


def test_polish_falls_back_to_the_template():
    with env():
        draft = "Your tickets are in the app."
        assert llm.polish(draft) == (draft, "template")


def test_provider_label_is_readable():
    with env(ANTHROPIC_API_KEY="sk-ant-test", LLM_MODEL="test-model"):
        label = llm.provider_label()
        assert "test-model" in label and "Anthropic" in label, label


# ==========================================================================

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
    SRV.shutdown()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
