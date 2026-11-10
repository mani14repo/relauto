"""Editing an assessment's details and deleting an assessment (API + FileStore)."""
import json
import os
import time
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra.api.app import create_app  # noqa: E402
from sra.store import FileStore  # noqa: E402

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"
H = {"X-User": "alice@example.com"}


@pytest.fixture()
def env(tmp_path):
    store = FileStore(tmp_path)
    c = TestClient(create_app(store=store, run_jobs_inline=True))
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    aid = c.post("/api/assessments", json={"profile": profile}, headers=H).json()["assessment"]["id"]
    return c, store, aid, tmp_path


def upload(c, aid):
    files = [("files", (p.name, p.read_bytes())) for p in sorted((SCENARIO / "evidence").iterdir())]
    assert c.post(f"/api/assessments/{aid}/evidence", files=files, headers=H).status_code == 201


def running(store, aid, updated=None):
    store.set_job(aid, {"state": "running", "updated_at": updated or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        "job": {"id": "r1"}, "log": []})


# ---------------------------------------------------------------- edit
def test_edit_details_and_audit(env):
    c, store, aid, _ = env
    r = c.patch(f"/api/assessments/{aid}", json={"profile": {"name": "Orders Platform", "tier": 1},
                                                 "assessment_date": "2026-10-01", "assessor": "bob@example.com"}, headers=H)
    assert r.status_code == 200 and r.json()["rerun_needed"] is False            # nothing evaluated yet
    a = store.get(aid)
    assert a.application.name == "Orders Platform" and a.profile["tier"] == 1 and a.profile["trigger"]   # partial merge
    assert a.assessment_date.isoformat() == "2026-10-01" and a.assessor == "bob@example.com"
    entry = c.get(f"/api/assessments/{aid}/audit").json()[-1]
    assert entry["action"] == "update_details" and entry["actor"] == "alice@example.com"
    assert set(entry["target"].split(", ")) == {"profile", "assessment_date", "assessor"}
    assert ("sra.assessment.events", aid) in {(t, k) for t, k, p in store.bus.messages if p["event"] == "updated"}


def test_edit_after_a_run_says_results_need_a_rerun(env):
    c, store, aid, _ = env
    upload(c, aid)
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    assert c.patch(f"/api/assessments/{aid}", json={"assessor": "carol"}, headers=H).json()["rerun_needed"] is False
    r = c.patch(f"/api/assessments/{aid}", json={"profile": {"tier": 3}}, headers=H).json()
    assert r["rerun_needed"] is True and r["assessment"]["results"]                # results kept, flagged as out of date


def test_edit_validation(env):
    c, _, aid, _ = env
    assert c.patch(f"/api/assessments/{aid}", json={"status": "signed_off"}, headers=H).status_code == 422
    assert c.patch(f"/api/assessments/{aid}", json={"profile": {"name": "  "}}, headers=H).status_code == 422
    assert c.patch(f"/api/assessments/{aid}", json={"assessment_date": "yesterday"}, headers=H).status_code == 422
    assert c.patch("/api/assessments/SRA-0000000000", json={"assessor": "x"}, headers=H).status_code == 404


def test_no_edit_while_running_or_after_sign_off(env):
    c, store, aid, _ = env
    running(store, aid)
    r = c.patch(f"/api/assessments/{aid}", json={"assessor": "x"}, headers=H)
    assert r.status_code == 409 and "running" in r.json()["detail"]
    store.set_job(aid, {"state": "done", "log": []})
    a = store.get(aid)
    a.status = "signed_off"
    store.save(a)
    assert c.patch(f"/api/assessments/{aid}", json={"assessor": "x"}, headers=H).status_code == 409


# ---------------------------------------------------------------- delete
def test_delete_removes_everything_but_a_record_of_the_deletion(env):
    c, store, aid, root = env
    upload(c, aid)
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    assert (root / "blobs" / aid).is_dir() and (root / "assessments" / aid).is_dir()
    r = c.delete(f"/api/assessments/{aid}", headers=H)
    assert r.status_code == 200 and r.json()["evidence_files"] > 0
    assert not (root / "blobs" / aid).exists() and not (root / "assessments" / aid).exists()
    assert c.get(f"/api/assessments/{aid}").status_code == 404
    assert all(row["id"] != aid for row in c.get("/api/assessments").json())
    gone = [json.loads(line) for line in (root / "deleted.jsonl").read_text(encoding="utf-8").splitlines()]
    assert gone[-1]["id"] == aid and gone[-1]["actor"] == "alice@example.com"
    assert any(p["event"] == "deleted" and k == aid for _, k, p in store.bus.messages)
    assert c.delete(f"/api/assessments/{aid}", headers=H).status_code == 404


def test_other_assessments_are_untouched(env):
    c, store, aid, root = env
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    other = c.post("/api/assessments", json={"profile": profile}, headers=H).json()["assessment"]["id"]
    upload(c, other)
    c.delete(f"/api/assessments/{aid}", headers=H)
    assert c.get(f"/api/assessments/{other}").status_code == 200 and (root / "blobs" / other).is_dir()


def test_no_delete_while_running_unless_the_run_is_stale(env):
    c, store, aid, _ = env
    running(store, aid)
    assert c.delete(f"/api/assessments/{aid}", headers=H).status_code == 409
    running(store, aid, updated="2026-01-01T00:00:00+00:00")                       # stopped reporting long ago
    assert c.delete(f"/api/assessments/{aid}", headers=H).status_code == 200
