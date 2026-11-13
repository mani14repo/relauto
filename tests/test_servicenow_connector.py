"""The ServiceNow connector without a database or a real instance: record handling, paging, retries, credentials."""
import httpx
import pytest

from sra.config import load_yaml
from sra.connectors import servicenow as sn


def v(display, value=None):
    return {"display_value": display, "value": display if value is None else value}


RAW = {"sys_id": v("abc123"), "number": v("INC0042"), "short_description": v("Orders checkout slow"),
       "description": v("Users report slowness. api_key=sk-live-1234567890abcdef contact bob@example.com"),
       "priority": v("2 - High", "2"), "state": v("Closed", "7"), "caller_id": v("Bob Jones", "u9"),
       "assignment_group": v("Orders Support", "g1"), "cmdb_ci": v("Orders", "ci1"), "problem_id": v("PRB0007", "prb7"),
       "rfc": v("", ""), "opened_at": v("21/10/2026 03:00:00 CEST", "2026-10-21 01:00:00"),
       "comments_and_work_notes": v("2026-10-21 01:05:00 - Priya Shah (Work notes)\nRestarted the pod."),
       "sys_updated_on": v("21/10/2026 04:31:00 CEST", "2026-10-21 02:31:00")}
CFG = load_yaml("sources")["servicenow"]


def test_record_becomes_a_redacted_linked_document():
    doc = sn.to_document("incident", RAW, sn._settings(CFG, "incident"))
    rec = doc["record"]
    assert doc["source_id"] == "incident/abc123" and doc["title"] == "INC0042" and doc["application"] == "Orders"
    assert "orders support" in doc["scope_text"].lower() and doc["acl"] == []
    assert rec["priority"] == "2 - High" and rec["state"] == "Closed"                     # display values, as in an export
    assert rec["opened_at"] == "2026-10-21 01:00:00"                                      # dates keep the stored UTC value
    assert "caller_id" not in rec and "sk-live-1234567890abcdef" not in str(rec) and "bob@example.com" not in str(rec)
    assert rec["_human_notes"] and "Restarted the pod." in rec["_human_notes"][0]
    assert ["problem_id", "problem/prb7"] in doc["links"] and ["cmdb_ci", "cmdb_ci/ci1"] in doc["links"]
    assert not any(link[0] == "rfc" for link in doc["links"])                             # empty reference: no link
    assert doc["watermark"] == "2026-10-21 02:31:00" and doc["updated"].tzinfo is not None
    row = sn.restore(rec)                                                                 # back to a rule-ready row
    assert row["_opened_at"].hour == 1 and row["number"] == "INC0042" and row["_human_notes"] == rec["_human_notes"]
    assert sn.to_document("incident", {"number": v("INC1")}, {}) is None                  # no sys_id: skipped


def test_knowledge_article_carries_its_read_restrictions():
    raw = {"sys_id": v("kb1"), "number": v("KB0001"), "short_description": v("Failover"), "u_application": v("Orders"),
           "roles": v("itil, sre"), "can_read_user_criteria": v("FSI Engineers"), "sys_updated_on": v("2026-09-01 00:00:00")}
    doc = sn.to_document("kb_knowledge", raw, sn._settings(CFG, "kb_knowledge"))
    assert doc["acl"] == ["FSI Engineers", "itil", "sre"] and doc["application"] == "Orders"


def test_relationship_row_links_the_two_items():
    raw = {"sys_id": v("rel1"), "parent": v("Orders", "ci1"), "child": v("orders-db-01", "ci2"),
           "type": v("Depends on::Used by", "t1"), "sys_updated_on": v("2026-09-01 00:00:00")}
    doc = sn.to_document("cmdb_rel_ci", raw, sn._settings(CFG, "cmdb_rel_ci"))
    assert doc["ci_link"] == {"from": "cmdb_ci/ci1", "to": "cmdb_ci/ci2", "type": "Depends on::Used by"}
    assert "orders" in doc["scope_text"].lower()


def client_for(handler, **kw):
    return sn.ServiceNowClient("https://example.service-now.com/", auth=("svc", "x"), transport=httpx.MockTransport(handler),
                               sleep=lambda s: None, **kw)


def test_paging_query_and_headers():
    seen = []

    def handler(request):
        seen.append(request)
        off = int(request.url.params["sysparm_offset"])
        return httpx.Response(200, json={"result": [{"sys_id": v(f"r{off + i}")} for i in range(3 if off < 6 else 1)]})
    got = list(client_for(handler).fetch("incident", "active=true", "2026-10-01 00:00:00", page_size=3))
    assert len(got) == 7 and [int(r.url.params["sysparm_offset"]) for r in seen] == [0, 3, 6]
    p = seen[0].url.params
    assert p["sysparm_query"] == "active=true^sys_updated_on>=2026-10-01 00:00:00^ORDERBYsys_updated_on"
    assert p["sysparm_display_value"] == "all" and seen[0].url.path == "/api/now/table/incident"
    assert seen[0].headers["authorization"].startswith("Basic ")
    seen.clear()
    assert len(list(client_for(handler).fetch("incident", page_size=3, max_records=4))) == 4       # stops at the cap
    assert seen[0].url.params["sysparm_query"] == "ORDERBYsys_updated_on"


def test_retries_then_clear_errors():
    hits = {"n": 0}

    def flaky(request):
        hits["n"] += 1
        return httpx.Response(503) if hits["n"] < 3 else httpx.Response(200, json={"result": [{"sys_id": v("a")}]})
    assert len(list(client_for(flaky).fetch("incident"))) == 1 and hits["n"] == 3
    with pytest.raises(sn.ConnectorError, match="429"):
        list(client_for(lambda r: httpx.Response(429, headers={"Retry-After": "1"}), retries=1).fetch("incident"))
    with pytest.raises(sn.ConnectorError, match="refused the credentials"):
        list(client_for(lambda r: httpx.Response(401)).fetch("incident"))
    with pytest.raises(sn.ConnectorError, match="not found"):
        list(client_for(lambda r: httpx.Response(404)).fetch("u_missing"))

    def down(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(sn.ConnectorError, match="could not reach"):
        list(client_for(down, retries=1).fetch("incident"))


def test_client_from_environment(monkeypatch):
    env = {}
    monkeypatch.setattr(sn, "setting", lambda name, default=None: env.get(name, default))
    assert sn.ServiceNowClient.from_settings() is None                                    # not configured: connector is off
    env["SERVICENOW_INSTANCE_URL"] = "https://example.service-now.com"
    with pytest.raises(sn.ConnectorError, match="no credentials"):
        sn.ServiceNowClient.from_settings()
    env["SERVICENOW_TOKEN"] = "test-token"
    c = sn.ServiceNowClient.from_settings()
    assert c.http.headers["authorization"] == "Bearer test-token"
    c.close()


def test_library_lists_the_real_oipreqs_documents():
    from sra.knowledge.standards import library
    docs = library()
    by = {d["code"]: d for d in docs}
    assert len(docs) > 150 and len({d["code"] for d in docs}) == len(docs)
    assert by["SRE-STD-03"]["doc_type"] == "standard" and by["SRE-PB-04"]["doc_type"] == "playbook"
    assert by["ITSM-POL-01"]["doc_type"] == "policy" and by["CMDB-PRC-05"]["doc_type"] == "procedure"
    assert by["CMDB-AT-10"]["version"] == 2 and "V2" in by["CMDB-AT-10"]["path"]           # the newer version wins


def test_record_text_for_retrieval():
    from sra.knowledge.ingest import record_text
    doc = sn.to_document("incident", RAW, sn._settings(CFG, "incident"))
    text = record_text("incident", doc["record"])
    assert text.startswith("[ServiceNow incident INC0042]") and "priority=2 - High" in text
    assert "short_description: Orders checkout slow" in text and "notes: " in text and "sys_id" not in text
