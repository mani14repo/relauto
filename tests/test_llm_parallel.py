"""Criteria evaluated in parallel against local HTTP servers that behave like a slow Ollama (no real model)."""
import json
import re
import socket
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sra.evidence.bundle import EvidenceBundle
from sra.llm.client import OllamaClient, clients_from_settings
from sra.llm.evaluate import Evaluator
from sra.models import ApplicationRef, Assessment
from sra.progress import JobCancelled, Progress


def make_handler(delay: float, log: list, stop: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):                                     # /api/tags health check
            body = json.dumps({"models": [{"name": "fake:latest", "model": "fake:latest"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            user = req["messages"][-1]["content"]
            cid = re.search(r'<evidence id="([^"]+)"', user).group(1)
            log.append(time.monotonic())
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            answer = json.dumps({"result": "Met", "rationale": "ok", "cited": [cid], "gaps": [], "recommendation": "",
                                 "risk": "High", "confidence": 0.9})
            try:
                end = time.monotonic() + delay
                while time.monotonic() < end:                 # stream filler tokens while "thinking"
                    if stop.is_set():
                        return
                    self.wfile.write(b'{"message": {"content": ""}, "done": false}\n')
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write((json.dumps({"message": {"content": answer}, "done": False}) + "\n").encode())
                self.wfile.write(b'{"message": {"content": ""}, "done": true, "prompt_eval_count": 10, "eval_count": 5}\n')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    return Handler


@pytest.fixture()
def servers():
    made, stop = [], threading.Event()

    def start(delay=0.6):
        log = []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(delay, log, stop))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        made.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", log
    yield start
    stop.set()
    for s in made:
        s.shutdown()


def bundle():
    b = EvidenceBundle()
    b.ingest("observability.md", b"# Dashboards\nLast reviewed: 2026-10-01\nStructured JSON logs, request rate and error rate "
                                 b"metrics in AppDynamics; traces with correlation IDs; alert routed to Orders on-call.\n")
    return b


def assessment():
    return Assessment(application=ApplicationRef(name="Orders"), templates=["SRE-AT-03"], assessment_date=date(2026, 11, 4))


def evaluator(urls_per_slot: list[str]) -> Evaluator:
    pool = [OllamaClient(base_url=u, model="fake", timeout=30) for u in urls_per_slot]
    return Evaluator(client=pool[0], clients=pool, use_cache=False)


def timed(ev, a, b, progress=None):
    t0 = time.monotonic()
    ev.evaluate_pending(a, b, progress=progress)
    return time.monotonic() - t0


def test_parallel_calls_finish_sooner_with_the_same_results(servers):
    url, _ = servers(0.6)
    one, three = assessment(), assessment()
    t1 = timed(evaluator([url]), one, bundle())
    t3 = timed(evaluator([url] * 3), three, bundle())
    assert {k: r.result for k, r in one.results.items()} == {k: r.result for k, r in three.results.items()}
    assert len(three.results) == 5 and t3 < t1 * 0.7, (t1, t3)


def test_work_is_spread_across_servers(servers):
    (u1, log1), (u2, log2) = servers(0.5), servers(0.5)
    a = assessment()
    ev = evaluator([u1, u2])
    ev.evaluate_pending(a, bundle())
    assert log1 and log2 and len(log1) + len(log2) == ev.stats["calls"]
    assert "2 at a time across 2 servers" in a.detector_runs["llm"]


def test_a_server_that_is_down_is_skipped(servers):
    url, log = servers(0.2)
    with socket.socket() as s:                                  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    lines = []
    p = Progress(sink=lambda st: lines.extend(st["log"]))
    a = assessment()
    evaluator([url, dead]).evaluate_pending(a, bundle(), progress=p)
    assert log and any(dead in ln["msg"] and "continuing without them" in ln["msg"] for ln in lines)
    assert all(r.result.value == "Met" for r in a.results.values() if r.evidence_refs)


def test_cancel_stops_every_call_in_flight(servers):
    url, log = servers(30)                                      # calls that would take 30 s
    flag = threading.Event()
    threading.Timer(0.8, flag.set).start()
    p = Progress(cancelled=flag.is_set)
    t0 = time.monotonic()
    with pytest.raises(JobCancelled):
        evaluator([url] * 3).evaluate_pending(assessment(), bundle(), progress=p)
    assert time.monotonic() - t0 < 4 and len(log) <= 3        # released quickly; queued criteria never started


def test_progress_shows_what_is_in_flight(servers):
    url, _ = servers(0.6)
    seen = []
    p = Progress(sink=lambda st: seen.append(st["current"]))
    evaluator([url] * 2).evaluate_pending(assessment(), bundle(), progress=p)
    assert any(re.match(r"2 in progress: SRE-AT-03-C\d+, SRE-AT-03-C\d+", s or "") for s in seen)


def test_pool_from_settings(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://a:11434, http://b:11434")
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    pool = clients_from_settings()
    assert [c.base_url for c in pool] == ["http://a:11434", "http://b:11434", "http://a:11434", "http://b:11434"]
    assert len({id(c) for c in pool}) == 4                      # never shared between calls in flight
    monkeypatch.setenv("LLM_CONCURRENCY", "")                  # set explicitly: a developer's .env must not leak in
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://a:11434")
    assert len(clients_from_settings()) == 1


@pytest.mark.parametrize("own, server, provider, want", [
    ("", "3", "ollama", 3),            # only the Ollama variable is set: the app follows it
    ("2", "3", "ollama", 2),           # the app's own setting wins
    ("", "", "ollama", 1),             # neither: one at a time
    ("", "3", "openai_compat", 1),     # OLLAMA_NUM_PARALLEL means nothing to a vLLM server
    ("", "two", "ollama", 1),          # not a number: ignored
])
def test_concurrency_falls_back_to_ollama_num_parallel(monkeypatch, own, server, provider, want):
    monkeypatch.setenv("LLM_BASE_URL", "http://a:11434")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("LLM_CONCURRENCY", own)
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", server)
    assert len(clients_from_settings()) == want


def test_parallel_eta_uses_throughput():
    p = Progress()
    p.phase("x", total=10)
    p._phase_started -= 20                                      # 20 s into the phase
    p.items_done(4, "2 in progress: a, b")
    assert p.state["eta_s"] == 30 and p.state["current"].startswith("2 in progress")
