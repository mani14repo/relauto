"""LLM_ENABLED=false (rules-only mode) and SRE decisions kept as training data (also after a delete)."""
import json
import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra.api import app as app_mod  # noqa: E402
from sra.catalog import load_templates  # noqa: E402
from sra.api.app import create_app  # noqa: E402
from sra.store import FileStore  # noqa: E402

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"
H = {"X-User": "alice@example.com"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "")
    monkeypatch.setenv("SRA_KEEP_FEEDBACK", "")
    store = FileStore(tmp_path)
    c = TestClient(create_app(store=store, run_jobs_inline=True))
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    aid = c.post("/api/assessments", json={"profile": profile}, headers=H).json()["assessment"]["id"]
    files = [("files", (p.name, p.read_bytes())) for p in sorted((SCENARIO / "evidence").iterdir())]
    c.post(f"/api/assessments/{aid}/evidence", files=files, headers=H)
    return c, store, aid, tmp_path


# ---------------------------------------------------------------- LLM off
def test_llm_off_runs_rules_only_and_turns_chat_off(env, monkeypatch):
    c, store, aid, _ = env
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setattr(app_mod, "_evaluator_factory", lambda: pytest.fail("the LLM must not be started"))
    assert c.get("/api/llm/health").json() == {"enabled": False, "model": None, "reachable": False}
    job = c.post(f"/api/assessments/{aid}/run", json={"llm": True}, headers=H).json()
    assert job["state"] == "done" and job["job"]["llm"] is False
    assert "llm" not in store.get(aid).detector_runs
    a = store.get(aid)
    cid = next(iter(a.results))
    r = c.post(f"/api/assessments/{aid}/criteria/{cid}/chat", json={"question": "why?"}, headers=H)
    assert r.status_code == 409 and "LLM_ENABLED=false" in r.json()["detail"]


def test_service_refuses_the_llm_when_off_even_if_asked(env, monkeypatch):
    _, store, aid, _ = env
    from sra.service import AssessmentService
    monkeypatch.setenv("LLM_ENABLED", "false")
    svc = AssessmentService(store, evaluator_factory=lambda: pytest.fail("the LLM must not be started"))
    out = svc.run(aid, use_llm=True)                      # e.g. a job queued before the switch, picked up by the worker
    assert out["llm"] is None


# ---------------------------------------------------------------- decisions as training labels
def kinds(store, aid):
    return [(r["criterion_id"], r["kind"]) for r in store.feedback_rows(aid)]


def test_every_kind_of_decision_is_recorded(env):
    c, store, aid, _ = env
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    a = store.get(aid)
    auto = [cid for cid, r in a.results.items() if r.result and r.source.value == "rule"]
    met, other = auto[0], auto[1]
    unevaluated = next(c.id for t in a.templates for c in load_templates()[t].criteria if c.id not in a.results)
    c.post(f"/api/assessments/{aid}/criteria/{met}/confirm", headers=H)
    flip = "Met" if a.results[other].result.value != "Met" else "Gap"
    c.patch(f"/api/assessments/{aid}/criteria/{other}", json={"result": flip}, headers=H)
    c.patch(f"/api/assessments/{aid}/criteria/{unevaluated}", json={"result": "Gap", "rationale": "no runbook"}, headers=H)
    c.patch(f"/api/assessments/{aid}/criteria/{unevaluated}", json={"result": "Gap", "rationale": "no runbook at all"}, headers=H)
    c.patch(f"/api/assessments/{aid}/criteria/{unevaluated}", json={"result": "Met"}, headers=H)
    assert kinds(store, aid) == [(met, "confirm"), (other, "override"), (unevaluated, "fill"), (unevaluated, "revise")]
    rows = store.feedback_rows(aid)
    assert rows[0]["ai"]["source"] == "rule" and rows[2]["ai"] is None and rows[2]["human"]["reviewer"] == "alice@example.com"


def test_feedback_is_kept_with_evidence_excerpts_after_delete(env):
    c, store, aid, root = env
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    cid = next(cid for cid, r in store.get(aid).results.items() if r.result)
    c.post(f"/api/assessments/{aid}/criteria/{cid}/confirm", headers=H)
    r = c.delete(f"/api/assessments/{aid}", headers=H).json()
    assert r["feedback_kept"] == 1 and not (root / "assessments" / aid).exists()
    kept = [json.loads(line) for line in (root / "feedback" / f"{aid}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert kept[0]["criterion_id"] == cid and kept[0]["kind"] == "confirm" and kept[0]["application"]
    assert kept[0]["excerpts"] and all(e["text"] and e["evidence"] for e in kept[0]["excerpts"])


def test_feedback_can_be_deleted_too(env, monkeypatch):
    c, store, aid, root = env
    monkeypatch.setenv("SRA_KEEP_FEEDBACK", "false")
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    cid = next(cid for cid, r in store.get(aid).results.items() if r.result)
    c.post(f"/api/assessments/{aid}/criteria/{cid}/confirm", headers=H)
    assert c.delete(f"/api/assessments/{aid}", headers=H).json()["feedback_kept"] == 0
    assert not (root / "feedback" / f"{aid}.jsonl").exists()


def test_feedback_from_older_versions_is_still_read(env):
    _, store, aid, root = env
    (root / "assessments" / aid / "feedback.jsonl").write_text(
        json.dumps({"criterion_id": "SRE-AT-01-C01", "ai": None, "human": {"result": "Gap"}}) + "\n", encoding="utf-8")
    assert [r["criterion_id"] for r in store.feedback_rows(aid)] == ["SRE-AT-01-C01"]
