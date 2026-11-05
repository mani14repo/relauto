"""API end to end with FileStore and inline jobs (no Kafka, no Postgres, no LLM)."""
import io
import os
import zipfile
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
def client(tmp_path):
    store = FileStore(tmp_path)
    return TestClient(create_app(store=store, run_jobs_inline=True)), store


def test_full_flow(client):
    c, store = client
    assert c.get("/healthz").json()["templates"] == 39
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    r = c.post("/api/assessments", json={"profile": profile}, headers=H)
    assert r.status_code == 201, r.text
    aid = r.json()["assessment"]["id"]
    assert r.json()["assessment"]["assessor"] == "alice@example.com"
    assert any(x["template_id"] == "SRE-AT-01" and x["selected"] for x in r.json()["candidates"])

    files = [("files", (p.name, p.read_bytes())) for p in sorted((SCENARIO / "evidence").iterdir())]
    r = c.post(f"/api/assessments/{aid}/evidence", files=files, headers=H)
    assert r.status_code == 201 and len(r.json()) == len(files)
    ev = c.get(f"/api/assessments/{aid}/evidence").json()
    assert not ev["errors"] and ev["summary"]["incident"] == 1

    r = c.put(f"/api/assessments/{aid}/templates", json={"templates": ["SRE-AT-01", "SRE-AT-07", "SRE-AT-11"]}, headers=H)
    assert r.json()["templates"] == ["SRE-AT-01", "SRE-AT-07", "SRE-AT-11"]

    r = c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    j = r.json()
    assert j["state"] == "done" and j["summary"]["rating"] == "Not Ready" and not j["stale"]
    msgs = [x["msg"] for x in j["log"]]
    assert msgs[0].startswith("Queued by") and any("rule-based detectors" in m for m in msgs)
    assert any("Local LLM not requested" in m for m in msgs) and msgs[-1].startswith("Finished: rated Not Ready")
    assert c.get(f"/api/assessments/{aid}/job").json()["state"] == "done"
    body = c.get(f"/api/assessments/{aid}").json()
    a, sc = body["assessment"], body["scorecard"]
    assert sc["rating"] == "Not Ready" and a["findings"]

    # Human override is kept across a re-run and recorded as feedback
    r = c.patch(f"/api/assessments/{aid}/criteria/SRE-AT-01-C02", json={"result": "Met", "rationale": "Dashboards reviewed live"}, headers=H)
    assert r.status_code == 200 and r.json()["result"]["source"] == "human"
    c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    a = c.get(f"/api/assessments/{aid}").json()["assessment"]
    assert a["results"]["SRE-AT-01-C02"]["result"] == "Met" and a["results"]["SRE-AT-01-C02"]["reviewer"] == "alice@example.com"

    # N/A requires justification
    r = c.patch(f"/api/assessments/{aid}/criteria/SRE-AT-11-C05", json={"result": "Not Applicable"}, headers=H)
    assert r.status_code == 422

    # Dispute requires a reason; disposition survives re-run
    fid = a["findings"][0]["id"]
    assert c.patch(f"/api/assessments/{aid}/findings/{fid}", json={"status": "disputed"}, headers=H).status_code == 422
    assert c.patch(f"/api/assessments/{aid}/findings/{fid}", json={"status": "accepted"}, headers=H).status_code == 200

    gate = c.get(f"/api/assessments/{aid}/gate").json()
    assert not gate["ok"]
    assert c.post(f"/api/assessments/{aid}/signoffs", json={"role": "SRE Reviewer", "name": "Bob", "decision": "Approved"},
                  headers=H).status_code == 409

    r = c.get(f"/api/assessments/{aid}/report", headers=H)
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert any(n.startswith("SRE-AT-07") for n in names) and any(n.startswith("Assessment Summary") for n in names)
    assert "DRAFT-NOT-COMPLETE.txt" in names

    audit = c.get(f"/api/assessments/{aid}/audit").json()
    assert {"create", "add_evidence", "set_templates", "run", "override_criterion", "update_finding", "report"} <= {x["action"] for x in audit}
    events = [m[2]["event"] for m in store.bus.messages]
    assert {"created", "evidence_ingested", "templates_selected", "evaluated", "scored", "finding_raised",
            "criterion_overridden", "report_generated"} <= set(events)
    assert c.get("/api/assessments").json()[0]["rating"] == "Not Ready"


def test_unknown_assessment_404(client):
    c, _ = client
    assert c.get("/api/assessments/SRA-0000000000").status_code == 404
    assert c.get("/api/assessments/../../etc").status_code == 404
