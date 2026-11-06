"""LLM efficiency: reused answers, smaller prompts, output caps, keep-alive and timing capture (no network)."""
from datetime import date

import pytest

from sra.evidence.bundle import EvidenceBundle
from sra.llm import client as mod
from sra.llm.client import LLMError, OllamaClient, OpenAICompatClient, _ollama_piece, _openai_piece
from sra.llm.evaluate import Evaluator
from sra.models import ApplicationRef, Assessment
from sra.retrieval.chunks import cached_index, focus


class FakeClient:
    model = "fake"
    num_ctx = 8192

    def __init__(self, fail=False):
        self.prompts, self.fail = [], fail
        self.last_usage = {}

    def json_chat(self, system, user, schema):
        self.prompts.append(user)
        self.last_usage = {"prompt_tokens": 900, "reply_tokens": 120, "prompt_s": 12.0, "reply_s": 6.0, "load_s": 0.0}
        if self.fail:
            raise LLMError("timeout")
        cid = user.split('<evidence id="')[1].split('"')[0]
        return {"result": "Met", "rationale": "ok", "cited": [cid], "gaps": [], "recommendation": "", "risk": "High", "confidence": 0.9}

    def chat(self, system, user):
        self.prompts.append(user)
        return "answer"


def bundle(extra=""):
    b = EvidenceBundle()
    b.ingest("observability.md", ("# Dashboards\nLast reviewed: 2026-10-01\nStructured JSON logs, request rate and error rate "
                                  "metrics in AppDynamics; alert routed to Orders on-call.\n" + extra).encode())
    return b


def assessment():
    return Assessment(application=ApplicationRef(name="Orders"), templates=["SRE-AT-03"], assessment_date=date(2026, 11, 4))


# ---------------------------------------------------------------- reuse across runs
def test_rerun_with_unchanged_evidence_makes_no_model_calls():
    a, b = assessment(), bundle()
    first = Evaluator(FakeClient())
    first.evaluate_pending(a, b)
    calls = first.stats["calls"]
    assert calls > 0 and len(a.llm_cache) == first.stats["verified"]
    a.results = {}                                   # what a re-run does before the AI pass
    second = Evaluator(FakeClient())
    second.evaluate_pending(a, b)
    assert second.stats["calls"] == 0 and second.stats["reused"] == calls
    assert all(r.result.value == "Met" for r in a.results.values() if r.evidence_refs)


def test_changed_evidence_model_or_explicit_reevaluation_asks_again():
    a = assessment()
    Evaluator(FakeClient()).evaluate_pending(a, bundle())
    a.results = {}
    changed = Evaluator(FakeClient())
    changed.evaluate_pending(a, bundle(extra="Added: traces with correlation IDs across services.\n"))
    assert changed.stats["calls"] > 0
    other = FakeClient()
    other.model = "other-model"
    a.results = {}
    Evaluator(other).evaluate_pending(a, bundle(extra="Added: traces with correlation IDs across services.\n"))
    assert other.prompts
    forced = Evaluator(FakeClient())
    forced.evaluate_pending(a, bundle(extra="Added: traces with correlation IDs across services.\n"), only=["SRE-AT-03-C01"])
    assert forced.stats["calls"] == 1 and forced.stats["reused"] == 0


def test_failed_calls_are_not_reused():
    a = assessment()
    Evaluator(FakeClient(fail=True)).evaluate_pending(a, bundle())
    assert a.llm_cache == {}


def test_cache_can_be_turned_off():
    a, b = assessment(), bundle()
    Evaluator(FakeClient(), use_cache=False).evaluate_pending(a, b)
    assert a.llm_cache == {}


# ---------------------------------------------------------------- smaller prompts
def test_focus_keeps_the_label_and_the_best_matching_lines():
    text = "[document/runbook 'rb.md']\n" + "".join(f"filler line {i} about lunch menus\n" for i in range(40)) + \
           "Rollback: helm rollback orders to the previous release.\n" + "".join(f"more filler {i}\n" for i in range(20))
    out = focus(text, "rollback procedure previous release", 300)
    assert out.startswith("[document/runbook 'rb.md']") and "helm rollback" in out and len(out) <= 300


def test_prompt_is_trimmed_to_fit_a_small_context():
    big = "".join(f"Structured logs line {i}: request rate, error rate, alert routing, dashboards.\n" for i in range(300))
    a = assessment()
    small = FakeClient()
    small.num_ctx = 900
    ev = Evaluator(small, top_k=6, chunk_chars=1200)
    ev.evaluate_pending(a, bundle(extra=big))
    assert ev.stats["trimmed"] > 0
    assert all(p.count("<evidence id=") >= 1 for p in small.prompts)


def test_default_passages_are_fewer_and_shorter():
    a = assessment()
    fake = FakeClient()
    Evaluator(fake).evaluate_pending(a, bundle(extra="".join(f"metric {i} error rate request rate\n" for i in range(400))))
    assert max(p.count("<evidence id=") for p in fake.prompts) <= 4


def test_index_is_built_once_per_evidence_set():
    b = bundle()
    assert cached_index(b) is cached_index(b)
    assert cached_index(b) is not cached_index(bundle(extra="new line\n"))


# ---------------------------------------------------------------- server settings and timings
def test_ollama_payload_caps_output_and_keeps_the_model_loaded():
    p = OllamaClient(num_predict=500, keep_alive="1h", num_thread=8)._payload("s", "u", 0, {"type": "object"})
    assert p["keep_alive"] == "1h" and p["options"]["num_predict"] == 500 and p["options"]["num_thread"] == 8
    assert "num_thread" not in OllamaClient()._payload("s", "u", 0)["options"]


def test_openai_payload_caps_output():
    assert OpenAICompatClient(num_predict=400)._payload("s", "u", 0)["max_tokens"] == 400


def test_ollama_timings_are_read_from_the_final_message():
    usage = {}
    _ollama_piece('{"message": {"content": "x"}, "done": false}', usage)
    assert usage == {}
    _ollama_piece('{"message": {"content": ""}, "done": true, "done_reason": "length", "prompt_eval_count": 1500, '
                  '"eval_count": 200, "prompt_eval_duration": 20000000000, "eval_duration": 8000000000, '
                  '"load_duration": 3000000000, "total_duration": 31000000000}', usage)
    assert usage["prompt_tokens"] == 1500 and usage["prompt_s"] == 20.0 and usage["load_s"] == 3.0 and usage["stopped_at_limit"]


def test_openai_usage_is_read_from_the_last_chunk():
    usage = {}
    _openai_piece('data: {"choices": [], "usage": {"prompt_tokens": 800, "completion_tokens": 90}}', usage)
    assert usage == {"prompt_tokens": 800, "reply_tokens": 90}


def test_settings(monkeypatch):
    monkeypatch.setenv("LLM_NUM_PREDICT", "640")
    monkeypatch.setenv("LLM_KEEP_ALIVE", "-1")
    monkeypatch.setenv("LLM_NUM_THREAD", "6")
    c = mod.from_settings()
    assert c.num_predict == 640 and c.keep_alive == "-1" and c.num_thread == 6


def test_progress_log_shows_tokens_and_seconds():
    lines = []

    class P:
        def __getattr__(self, name):
            return lambda *a, **k: lines.append(" ".join(str(x) for x in a))

        def is_cancelled(self):
            return False

    Evaluator(FakeClient()).evaluate_pending(assessment(), bundle(), progress=P())
    assert any("read 900 prompt tokens in 12s, wrote 120 in 6s" in ln for ln in lines)
    assert any("Per call on average: 900 prompt tokens" in ln for ln in lines)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("test tried to reach a real model server")
    monkeypatch.setattr(mod.httpx, "stream", blocked)
    monkeypatch.setattr(mod.httpx, "post", blocked)
