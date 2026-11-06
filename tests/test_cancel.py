"""Cancelling an automated pass: the streamed model call, the job runner and the API."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra.api.app import create_app  # noqa: E402
from sra.llm.client import _ollama_piece, stream_call  # noqa: E402
from sra.llm.evaluate import Evaluator  # noqa: E402
from sra.progress import JobCancelled  # noqa: E402
from sra.selection.profile import ApplicationProfile  # noqa: E402
from sra.service import AssessmentService  # noqa: E402
from sra.store import FileStore  # noqa: E402

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"
H = {"X-User": "alice@example.com"}


# ---------------------------------------------------------------- a slow local "model server"
class Slow(BaseHTTPRequestHandler):
    """Waits `silence` seconds (prompt reading), then streams one token per 0.2s for up to 10s."""
    silence = 0.0
    disconnected = threading.Event()

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(self.silence)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for i in range(50):
                self.wfile.write((json.dumps({"message": {"content": f"t{i} "}, "done": False}) + "\n").encode())
                self.wfile.flush()
                time.sleep(0.2)
            self.wfile.write(b'{"message": {"content": ""}, "done": true}\n')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            type(self).disconnected.set()


@pytest.fixture()
def slow_server():
    Slow.disconnected = threading.Event()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}/api/chat"
    srv.shutdown()


def test_stream_returns_full_text(slow_server, monkeypatch):
    _, url = slow_server
    monkeypatch.setattr(Slow, "silence", 0.0)
    out = stream_call(url, {}, timeout=30, piece=_ollama_piece)
    assert out.startswith("t0 t1 ") and out.strip().endswith("t49")


def test_cancel_while_model_is_still_reading_the_prompt(slow_server, monkeypatch):
    _, url = slow_server
    monkeypatch.setattr(Slow, "silence", 8.0)          # sends nothing for 8 seconds
    flag = threading.Event()
    threading.Timer(0.5, flag.set).start()
    t0 = time.monotonic()
    with pytest.raises(JobCancelled):
        stream_call(url, {}, timeout=30, cancel_check=flag.is_set, piece=_ollama_piece)
    assert time.monotonic() - t0 < 2.5                  # released long before the server answers


def test_cancel_mid_stream_closes_the_connection(slow_server, monkeypatch):
    _, url = slow_server
    monkeypatch.setattr(Slow, "silence", 0.0)
    flag = threading.Event()
    threading.Timer(1.0, flag.set).start()
    with pytest.raises(JobCancelled):
        stream_call(url, {}, timeout=30, cancel_check=flag.is_set, piece=_ollama_piece)
    assert Slow.disconnected.wait(5), "server kept streaming after the cancel"


# ---------------------------------------------------------------- job runner
class CancelOnFirstCall:
    """Fake model that asks for cancellation the first time it is called."""
    model = "fake"

    def __init__(self, store, aid):
        self.store, self.aid, self.calls = store, aid, 0
        self.cancel_check = None

    def json_chat(self, system, user, schema):
        self.calls += 1
        self.store.request_cancel(self.aid)
        cid = user.split('<evidence id="')[1].split('"')[0]
        return {"result": "Met", "rationale": "x", "cited": [cid], "gaps": [], "recommendation": "", "risk": "High",
                "confidence": 0.9}

    def chat(self, system, user):
        return ""


def _assessment(store):
    svc = AssessmentService(store)
    profile = ApplicationProfile.model_validate(yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8")))
    a, _ = svc.create(profile, "alice")
    svc.add_evidence(a.id, [(p.name, p.read_bytes()) for p in sorted((SCENARIO / "evidence").iterdir())], None, "alice")
    return svc, a.id


def test_cancel_stops_the_run_and_saves_nothing(tmp_path):
    store = FileStore(tmp_path)
    svc, aid = _assessment(store)
    fake = CancelOnFirstCall(store, aid)
    svc.evaluator_factory = lambda: Evaluator(fake)
    before = store.get(aid).model_dump(mode="json")
    store.set_job(aid, {"state": "queued", "job": {"id": "j1"}, "log": []})
    assert svc.run_job({"job": "run", "assessment_id": aid, "llm": True, "actor": "alice", "id": "j1"}) is None
    job = store.get_job(aid)
    assert job["state"] == "cancelled" and "Nothing from this run was saved" in job["log"][-1]["msg"]
    assert fake.calls == 1                                       # stopped before the second criterion
    assert store.get(aid).model_dump(mode="json") == before     # assessment untouched
    assert not store.cancel_requested(aid)                       # flag cleared for the next run


def test_superseded_job_is_skipped(tmp_path):
    store = FileStore(tmp_path)
    svc, aid = _assessment(store)
    store.set_job(aid, {"state": "queued", "job": {"id": "new"}, "log": []})
    assert svc.run_job({"job": "run", "assessment_id": aid, "llm": False, "id": "old"}) is None
    assert store.get_job(aid)["state"] == "queued"               # the newer run's record is left alone


# ---------------------------------------------------------------- API
def test_cancel_api(tmp_path):
    store = FileStore(tmp_path)
    c = TestClient(create_app(store=store, run_jobs_inline=True))
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    aid = c.post("/api/assessments", json={"profile": profile}, headers=H).json()["assessment"]["id"]
    assert c.post(f"/api/assessments/{aid}/cancel", headers=H).status_code == 409      # nothing running

    # a queued run is closed at once
    store.set_job(aid, {"state": "queued", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        "job": {"id": "q1"}, "log": []})
    r = c.post(f"/api/assessments/{aid}/cancel", headers=H).json()
    assert r["state"] == "cancelled" and "before it started" in r["log"][-1]["msg"]

    # a run that stopped reporting (server restarted) is closed too
    store.clear_cancel(aid)
    store.set_job(aid, {"state": "running", "updated_at": "2026-01-01T00:00:00+00:00", "job": {"id": "r1"}, "log": []})
    r = c.post(f"/api/assessments/{aid}/cancel", headers=H).json()
    assert r["state"] == "cancelled" and "stopped reporting" in r["log"][-1]["msg"] and not store.cancel_requested(aid)

    # a live run gets the flag and shows it
    store.set_job(aid, {"state": "running", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        "job": {"id": "r2"}, "log": []})
    r = c.post(f"/api/assessments/{aid}/cancel", headers=H).json()
    assert r["state"] == "running" and r["cancel_requested"] is True

    # starting a new run clears an old cancel request
    store.set_job(aid, {"state": "cancelled", "job": {"id": "r2"}, "log": []})
    r = c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H).json()
    assert r["state"] == "done" and not store.cancel_requested(aid)
