"""The PostgreSQL store, the standing knowledge store and hybrid retrieval against a real database (+ pgvector).

Skipped unless SRA_TEST_DATABASE_URL is set, e.g. with the docker-compose database:
    docker compose up -d postgres
    SRA_TEST_DATABASE_URL=postgresql://sra:sra-dev-only@localhost:5432/sra  python -m pytest tests/test_postgres.py
Every test removes what it created (assessments, and the standing documents it synced or registered).
"""
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

DSN = os.environ.get("SRA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set SRA_TEST_DATABASE_URL to run the PostgreSQL tests")

CONCEPTS = [("failover", r"failover|fail over|standby|secondary region"), ("latency", r"latency|slow|response time"),
            ("backup", r"backup|restore|snapshot"), ("oncall", r"on-call|pager|escalat"), ("deploy", r"deploy|release|rollback")]


class ConceptEmbedder:
    """A stand-in embedding model: texts about the same concept get the same direction, whatever words they use.
    384 dimensions like bge-small, so the real vector column and HNSW index are exercised."""
    name = "test/concepts"

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 384
            for i, (_, pat) in enumerate(CONCEPTS):
                if re.search(pat, t, re.I):
                    v[i] = 1.0
            if not any(v):
                v[100] = 1.0
            out.append(v)
        return out


def _wipe_standing(store):
    with store.pool.connection() as c:
        c.execute("DELETE FROM sra_documents WHERE assessment_id IS NULL")
        c.execute("DELETE FROM sra_sync_state")


@pytest.fixture(scope="module")
def store():
    """A PgStore on a scratch database created for this run and dropped afterwards, so the tests never touch (or
    clear) the data in the database SRA_TEST_DATABASE_URL points at."""
    import psycopg
    from sra.store import LocalBlobs, PgStore
    db = f"sra_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {db}")
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"cannot create a scratch database with SRA_TEST_DATABASE_URL: {exc}")
    scratch = psycopg.conninfo.make_conninfo(**{**psycopg.conninfo.conninfo_to_dict(DSN), "dbname": db})
    s = PgStore(scratch, blobs=LocalBlobs(Path(tempfile.mkdtemp())))
    try:
        s.init()
        s.init()                               # idempotent
        yield s
    finally:
        s.close()
        admin.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        admin.close()


@pytest.fixture()
def svc(store, monkeypatch):
    from sra.retrieval import chunks
    from sra.service import AssessmentService
    monkeypatch.setattr(chunks, "_EMBEDDER", [ConceptEmbedder()])       # what shared_embedder() returns
    monkeypatch.setenv("RETRIEVAL_RERANK", "off")
    service = AssessmentService(store, None)
    made = []
    service._made = made
    yield service
    for aid in made:
        try:
            store.delete(aid, "test")
        except KeyError:
            pass
    _wipe_standing(store)


def new_assessment(svc, app=None, aliases=()):
    from sra.selection.profile import ApplicationProfile
    a, _ = svc.create(ApplicationProfile(name=app or f"App {uuid.uuid4().hex[:8]}", tier=2, aliases=list(aliases)), "tester", "tester")
    svc._made.append(a.id)
    return a


def ingest(store):
    return store.ingest_pending(None, ConceptEmbedder())


def one(store, sql, *params):
    with store.pool.connection() as c:
        return c.execute(sql, params).fetchone()


def rows(store, sql, *params):
    with store.pool.connection() as c:
        return c.execute(sql, params).fetchall()


RUNBOOK = b"# Orders runbook\nLast reviewed: 2026-10-01\nIf the primary database is unavailable, promote the standby and verify writes.\n"
DR_PLAN = b"# Orders DR plan\nThe failover procedure moves traffic to the secondary region. Failover was tested in March.\n"
PERF = b"# Orders load test\nP95 response time stayed under 300 ms at 500 requests per second.\n"
SECRET = b"# Orders security review\nPenetration test findings are tracked by the security team. Failover of the HSM is manual.\n"


# ================================================================================================ schema
def test_tables_are_prefixed_and_coexist_with_cog(store):
    with store.pool.connection() as c:
        c.execute("CREATE TABLE IF NOT EXISTS cog_run (id text PRIMARY KEY)")          # another application's table
        names = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        store.init()
        assert "cog_run" in names
        ours = [n for n in names if not n.startswith("cog_")]
        assert ours and all(n.startswith("sra_") for n in ours), ours
        assert {"sra_documents", "sra_chunks", "sra_relations", "sra_sync_state"} <= set(ours) and "sra_evidence_chunks" not in ours
        idx = [r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename LIKE 'sra_%'").fetchall()]
        assert all(i.startswith("sra_") for i in idx), idx
        c.execute("DROP TABLE cog_run")
    assert store.has_vector and store.knowledge.has_vector


# ================================================================================================ ingestion service
def test_upload_only_queues_and_the_ingestion_service_does_the_work(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK), ("orders-dr-plan.md", DR_PLAN)], None, "tester")
    # the upload request stored the files and queued them; nothing was chunked or embedded yet
    assert store.knowledge.pending_count(a.id) == 2
    assert one(store, "SELECT count(*) FROM sra_chunks c JOIN sra_documents d ON d.id = c.document_id WHERE d.assessment_id = %s", a.id)[0] == 0
    stats = ingest(store)
    assert stats["documents"] == 2 and stats["chunks"] == 2 and stats["failed"] == 0 and stats["embedded"] == 2
    got = rows(store, "SELECT d.title, d.source_system, d.kind, d.doc_type, d.application, d.evidence_id, c.id, c.embedding IS NOT NULL, "
                      "c.embedding_model FROM sra_chunks c JOIN sra_documents d ON d.id = c.document_id "
                      "WHERE d.assessment_id = %s ORDER BY d.title", a.id)
    assert [r[0] for r in got] == ["orders-dr-plan.md", "orders-runbook.md"]
    assert all(r[1] == "upload" and r[2] == "document" and r[4] == a.application.name and r[7] and r[8] == "test/concepts" for r in got)
    assert {r[3] for r in got} == {"dr_plan", "runbook"} and all(r[6] == f"{r[5]}#0" for r in got)     # chunk id = <evidence id>#n
    assert store.knowledge.pending_count() == 0 and ingest(store)["documents"] == 0


def test_a_bad_document_does_not_block_the_queue(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK)], None, "tester")
    with store.pool.connection() as c:                       # a queued document whose file has gone missing
        store.knowledge.upsert(c, source_system="upload", source_id=f"{a.id}/EV-missing", title="lost.md", content_hash="x",
                               assessment_id=a.id, evidence_id="EV-missing", blob_uri="file:///nowhere/lost.md")
    stats = ingest(store)
    assert stats["documents"] == 1 and stats["failed"] == 1
    assert one(store, "SELECT index_error FROM sra_documents WHERE evidence_id = 'EV-missing'")[0]
    assert store.knowledge.pending_count() == 0             # the failure is recorded, not retried forever


def test_embeddings_are_filled_in_later(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK)], None, "tester")
    store.ingest_pending(None, None)                         # ingested while no embedding model was loaded
    count = lambda: one(store, "SELECT count(*) FILTER (WHERE c.embedding IS NULL), count(*) FROM sra_chunks c "  # noqa: E731
                               "JOIN sra_documents d ON d.id = c.document_id WHERE d.assessment_id = %s", a.id)
    assert count() == (1, 1)
    assert ingest(store)["embedded"] == 1 and count() == (0, 1)


# ================================================================================================ retrieval
def test_keyword_vector_and_fusion(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK), ("orders-dr-plan.md", DR_PLAN), ("orders-load-test.md", PERF)], None, "tester")
    r = svc.search("failover", a.id)                         # the assessment's waiting files are ingested first
    assert r["mode"].startswith("hybrid")
    top = r["results"][0]
    assert top["source"] == "orders-dr-plan.md" and top["keyword_rank"] == 1 and top["vector_rank"] is not None   # found by both
    by_name = {x["source"]: x for x in r["results"]}
    # the runbook never says "failover": only the vector search finds it ("promote the standby")
    assert by_name["orders-runbook.md"]["keyword_rank"] is None and by_name["orders-runbook.md"]["vector_rank"] is not None
    assert all(x["chunk_id"].startswith(x["evidence_id"] + "#") and x["assessment_id"] == a.id and x["source_system"] == "upload"
               for x in r["results"])
    idx = store.retriever(a.id, None)                        # keyword search alone (no embedding model)
    assert [c.source for c in idx.search("failover")] == ["orders-dr-plan.md"] and idx.last["mode"] == "keyword only"
    from sra.retrieval.pg import PgHybridIndex
    assert PgHybridIndex(store.pool, a.id, ConceptEmbedder(), has_vector=False).search("failover")[0].source == "orders-dr-plan.md"


def test_long_criterion_text_still_finds_evidence(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-load-test.md", PERF), ("orders-runbook.md", RUNBOOK)], None, "tester")
    q = ("Performance baselines established and documented: performance test report with throughput baseline, P95 latency "
         "target, P99 latency target and acceptable error rate threshold, within 90 days of the last major release")
    assert svc.search(q, a.id, k=1)["results"][0]["source"] == "orders-load-test.md"


def test_filters_and_search_across_the_store(store, svc):
    app = f"Orders {uuid.uuid4().hex[:6]}"
    a1, a2 = new_assessment(svc, app), new_assessment(svc)
    svc.add_evidence(a1.id, [("orders-dr-plan.md", DR_PLAN), ("orders-runbook.md", RUNBOOK)], None, "tester")
    svc.add_evidence(a2.id, [("other-dr-plan.md", DR_PLAN.replace(b"Orders", b"Other"))], None, "tester")
    ingest(store)
    everywhere = svc.search("failover procedure", None, k=20, principals=["tester"])
    assert {a1.id, a2.id} <= {x["assessment_id"] for x in everywhere["results"]}
    only_app = svc.search("failover procedure", None, k=20, principals=["tester"], application=app.upper())
    assert {x["assessment_id"] for x in only_app["results"]} == {a1.id}
    runbooks = svc.search("failover procedure", a1.id, doc_types=["runbook"])
    assert [x["source"] for x in runbooks["results"]] == ["orders-runbook.md"]


def test_access_lists_are_enforced(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-dr-plan.md", DR_PLAN)], None, "tester")
    svc.add_evidence(a.id, [("orders-security-review.md", SECRET)], None, "tester", acl=["security-team"])
    names = lambda who: {x["source"] for x in svc.search("failover", a.id, principals=who)["results"]}  # noqa: E731
    assert names(["bob"]) == {"orders-dr-plan.md"}
    assert names(["bob", "security-team"]) == {"orders-dr-plan.md", "orders-security-review.md"}
    assert names(None) == {"orders-dr-plan.md", "orders-security-review.md"}      # the assessment's own automated pass


def test_evaluator_uses_the_postgres_index(store, svc):
    from sra.catalog.models import Result
    from sra.llm.evaluate import Evaluator
    from test_llm import FakeClient, first_chunk_id
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("observability.md", b"# Dashboards\nLast reviewed: 2026-10-01\nStructured JSON logs, request rate and "
                                                 b"error rate metrics in AppDynamics; alert routed to Orders on-call.\n")], None, "tester")
    a = store.get(a.id)
    a.templates, a.assessment_date = ["SRE-AT-03"], date(2026, 11, 4)
    ev = Evaluator(FakeClient(lambda u: {"result": "Met", "rationale": "Structured logs present.", "cited": [first_chunk_id(u)],
                                         "gaps": [], "recommendation": "", "risk": "High", "confidence": 0.9}),
                   embedder=ConceptEmbedder(), retriever=store.retriever)
    ev.evaluate_pending(a, store.bundle(a.id))
    r = a.results["SRE-AT-03-C01"]
    assert r.result == Result.MET and r.evidence_refs and "observability.md" in r.evidence_refs[0]
    assert ev.stats["verified"] >= 1 and ev.stats["rejected"] == 0     # citations from Postgres chunks verify


# ================================================================================================ ServiceNow connector
def _v(display, value=None):
    return {"display_value": display, "value": display if value is None else value}


def fake_servicenow(calls):
    """A stand-in ServiceNow Table API with a small, linked data set for the 'Orders' application."""
    import httpx
    ci = {"sys_id": _v("ci1"), "name": _v("Orders"), "sys_class_name": _v("Business Application", "cmdb_ci_business_app"),
          "owned_by": _v("Pat Owner", "u1"), "support_group": _v("Orders Support", "g1"), "operational_status": _v("Operational", "1"),
          "sys_updated_on": _v("2026-10-01 08:00:00")}
    db = {**ci, "sys_id": _v("ci2"), "name": _v("orders-db-01"), "sys_class_name": _v("Database", "cmdb_ci_db_instance")}
    other = {**ci, "sys_id": _v("ci9"), "name": _v("Billing"), "support_group": _v("Billing Support", "g9")}
    data = {
        "cmdb_ci": [ci, db, other],
        "cmdb_rel_ci": [{"sys_id": _v("rel1"), "parent": _v("Orders", "ci1"), "child": _v("orders-db-01", "ci2"),
                         "type": _v("Depends on::Used by", "t1"), "sys_updated_on": _v("2026-09-01 08:00:00")}],
        "change_request": [{"sys_id": _v("chg1"), "number": _v("CHG0001"), "short_description": _v("Orders release 4.2"),
                            "cmdb_ci": _v("Orders", "ci1"), "state": _v("Closed", "3"), "backout_plan": _v("redeploy 4.1"),
                            "test_plan": _v("regression suite"), "start_date": _v("2026-10-20 02:00:00"),
                            "end_date": _v("2026-10-20 03:00:00"), "sys_updated_on": _v("2026-10-20 03:10:00")}],
        "problem": [{"sys_id": _v("prb1"), "number": _v("PRB0001"), "short_description": _v("Orders nightly job fails after release"),
                     "cmdb_ci": _v("Orders", "ci1"), "rfc": _v("CHG0001", "chg1"), "state": _v("Resolved", "4"),
                     "workaround": _v("Restart the job from the failed step"), "sys_updated_on": _v("2026-10-22 09:00:00")}],
        "incident": [
            {"sys_id": _v(f"inc{i}"), "number": _v(f"INC000{i}"), "short_description": _v("Orders nightly reconciliation job failed"),
             "description": _v("The job stopped at step 3. Contact jane.doe@example.com; password=Hunter2Secret99"),
             "priority": _v("3 - Moderate", "3"), "state": _v("Closed", "7"), "category": _v("Batch"),
             "assignment_group": _v("Orders Support", "g1"), "cmdb_ci": _v("Orders", "ci1"), "caller_id": _v("Jane Doe", "u7"),
             "problem_id": _v("PRB0001", "prb1") if i < 3 else _v(""), "opened_at": _v(f"2026-10-2{i} 01:00:00"),
             "resolved_at": _v(f"2026-10-2{i} 02:30:00"), "close_code": _v("Solved"), "sys_updated_on": _v(f"2026-10-2{i} 02:31:00")}
            for i in range(1, 5)] + [
            {"sys_id": _v("incB"), "number": _v("INC0900"), "short_description": _v("Billing export slow"),
             "priority": _v("4 - Low", "4"), "state": _v("Closed", "7"), "assignment_group": _v("Billing Support", "g9"),
             "cmdb_ci": _v("Billing", "ci9"), "opened_at": _v("2026-10-15 01:00:00"), "resolved_at": _v("2026-10-15 02:00:00"),
             "sys_updated_on": _v("2026-10-15 02:01:00")}],
        "kb_knowledge": [{"sys_id": _v("kb1"), "number": _v("KB0001"), "short_description": _v("Orders failover runbook"),
                          "text": _v("To fail over Orders, promote the standby database and verify writes."), "u_application": _v("Orders"),
                          "workflow_state": _v("Published", "published"), "roles": _v("sre,itil"),
                          "sys_updated_on": _v("2026-09-15 10:00:00")}],
        "em_alert": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        q = request.url.params.get("sysparm_query", "")
        calls.append((table, q, int(request.url.params.get("sysparm_offset", 0))))
        since = re.search(r"sys_updated_on>=([0-9: -]+)", q)
        out = [r for r in data.get(table, []) if not since or r["sys_updated_on"]["value"] >= since.group(1).strip()]
        out.sort(key=lambda r: r["sys_updated_on"]["value"])
        off, lim = int(request.url.params.get("sysparm_offset", 0)), int(request.url.params.get("sysparm_limit", 100))
        return httpx.Response(200, json={"result": out[off:off + lim]})
    return httpx.MockTransport(handler), data


def run_sync(store, transport, **cfg_over):
    import copy

    from sra.config import load_yaml
    from sra.connectors.servicenow import ServiceNowClient, sync
    cfg = copy.deepcopy(load_yaml("sources")["servicenow"])
    cfg.update(cfg_over)
    client = ServiceNowClient("https://example.service-now.com", auth=("svc", "not-a-real-password"), transport=transport, sleep=lambda s: None)
    try:
        return sync(store.knowledge, client, cfg=cfg, now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    finally:
        client.close()


def test_servicenow_sync_fills_the_standing_store(store, svc):
    calls = []
    transport, _ = fake_servicenow(calls)
    report = run_sync(store, transport, page_size=2)
    assert all(t["status"] == "ok" for t in report.values()), report
    assert report["incident"]["records"] == 5 and report["incident"]["queued"] == 5 and report["incident"]["watermark"] == "2026-10-24 02:31:00"
    assert [c[2] for c in calls if c[0] == "incident"] == [0, 2, 4]                 # paged, 2 at a time
    assert all("ORDERBYsys_updated_on" in c[1] for c in calls)
    assert any(c[0] == "incident" and "sys_updated_on>=2025-11-04" in c[1] for c in calls)     # first sync: 365-day lookback
    assert any(c[0] == "cmdb_ci" and "sys_updated_on" not in c[1].replace("ORDERBYsys_updated_on", "") for c in calls)   # CMDB: everything
    assert any(c[0] == "kb_knowledge" and c[1].startswith("workflow_state=published") for c in calls)
    assert store.knowledge.pending_count() == 12
    stats = ingest(store)
    assert stats["documents"] == 12 and stats["failed"] == 0

    doc = one(store, "SELECT title, application, scope_text, acl, record, assessment_id FROM sra_documents WHERE source_id = 'incident/inc1'")
    assert doc[0] == "INC0001" and doc[1] == "Orders" and "orders support" in doc[2] and doc[3] == [] and doc[5] is None
    rec = doc[4]
    assert "caller_id" not in rec and "Hunter2Secret99" not in str(rec) and "jane.doe@example.com" not in str(rec)   # redacted like an export
    assert rec["priority"] == "3 - Moderate" and rec["state"] == "Closed" and rec["opened_at"] == "2026-10-21 01:00:00"
    # access list carried over from the source for knowledge articles
    assert one(store, "SELECT acl FROM sra_documents WHERE source_id = 'kb_knowledge/kb1'")[0] == ["itil", "sre"]

    # a second sync only asks for what changed since the watermark, and unchanged records are not re-ingested
    calls.clear()
    again = run_sync(store, transport)
    assert any(c[0] == "incident" and "sys_updated_on>=2026-10-24 02:31:00" in c[1] for c in calls)
    assert again["incident"]["records"] == 1 and again["incident"]["queued"] == 0 and store.knowledge.pending_count() == 0
    status = store.knowledge.status()
    assert {s["stream"]: s["status"] for s in status["sync"]}["incident"] == "ok"
    assert any(d["source_system"] == "servicenow" and d["type"] == "incident" and d["documents"] == 5 for d in status["documents"])


def test_records_are_linked_to_each_other(store, svc):
    from sra.knowledge.store import doc_id
    run_sync(store, fake_servicenow([])[0])
    ingest(store)
    inc, prb, ci = (doc_id("servicenow", x) for x in ("incident/inc1", "problem/prb1", "cmdb_ci/ci1"))
    rel = store.knowledge.related([inc, prb, ci], None, per_doc=50)
    links = lambda d: {(x["type"], x["direction"], x["title"]) for x in rel[d]}  # noqa: E731
    assert ("problem_id", "to", "PRB0001") in links(inc) and ("cmdb_ci", "to", "Orders") in links(inc)
    assert ("rfc", "to", "CHG0001") in links(prb) and ("problem_id", "from", "INC0001") in links(prb)       # both directions
    assert ("Depends on::Used by", "to", "orders-db-01") in links(ci)                                       # from cmdb_rel_ci
    assert ("about", "from", "KB0001") in links(ci)                                                         # the article is about the service
    # search results carry the links: a hit on the incident shows its problem
    hit = next(x for x in svc.search("nightly reconciliation job failed", None, k=10, principals=["sre"])["results"]
               if x["source"] == "INC0001")
    assert hit["source_system"] == "servicenow" and any(x["type"] == "problem_id" and x["title"] == "PRB0001" for x in hit["related"])
    # the access list also applies to linked documents: without the sre/itil role the article is not shown
    assert not any(x["title"] == "KB0001" for x in store.knowledge.related([ci], ["bob"], per_doc=50)[ci])
    assert not any(x["source"] == "KB0001" for x in svc.search("failover runbook", None, principals=["bob"])["results"])
    assert any(x["source"] == "KB0001" for x in svc.search("failover runbook", None, principals=["bob", "itil"])["results"])


def test_an_assessment_uses_synced_records_without_uploads(store, svc):
    run_sync(store, fake_servicenow([])[0])
    ingest(store)
    a = new_assessment(svc, "Orders", aliases=["Orders Support"])
    b = store.bundle(a.id)
    inc = b.table("incident")
    assert len(inc) == 4 and all(r["cmdb_ci"] == "Orders" for r in inc)                 # the Billing incident is not ours
    assert inc[0]["_opened_at"].year == 2026 and len(b.table("change_request")) == 1 and len(b.table("problem")) == 1
    out = svc.run(a.id, use_llm=False, actor="tester")                                  # the rules run on synced data alone
    assert out["scorecard"]["rating"]
    got = store.get(a.id)
    assert got.rubric_direct["P1-02"].result.value == "Met" and "4 incidents" in got.rubric_direct["P1-02"].rationale
    assert got.rubric_direct["P1-07"].result.value == "Met"                             # the synced change has backout and test plans
    # retrieval for the assessment includes the standing records about its application, and nothing else
    hits = svc.search("nightly reconciliation job failed", a.id, k=20)["results"]
    assert {"INC0001", "PRB0001"} <= {x["source"] for x in hits} and "INC0900" not in {x["source"] for x in hits}
    # an uploaded export of a table replaces the synced copy of that table
    export = b"number,short_description,opened_at,priority,cmdb_ci\nINC777,Orders page slow,2026-10-30 10:00:00,3,Orders\n"
    svc.add_evidence(a.id, [("incident.csv", export)], None, "tester")
    b = store.bundle(a.id)
    assert [r["number"] for r in b.table("incident")] == ["INC777"] and any("uploaded incident export is used" in n for n in b.notes)


def test_standing_documents_outlive_an_assessment(store, svc):
    run_sync(store, fake_servicenow([])[0])
    a = new_assessment(svc, "Orders")
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK)], None, "tester")
    ingest(store)
    count = lambda where: one(store, f"SELECT count(*) FROM sra_documents WHERE {where}")[0]  # noqa: E731
    assert count(f"assessment_id = '{a.id}'") == 1 and count("source_system = 'servicenow'") == 12
    svc.delete(a.id, "tester")
    assert count(f"assessment_id = '{a.id}'") == 0                                       # the upload goes with its assessment
    assert count("source_system = 'servicenow'") == 12                                   # the knowledge store stands
    assert svc.search("nightly reconciliation", None, principals=["x"])["results"]


def test_a_failing_table_does_not_stop_the_others(store, svc):
    import httpx

    def handler(request):
        if request.url.path.endswith("/problem"):
            return httpx.Response(403, json={"error": "no access"})
        return httpx.Response(200, json={"result": []})
    report = run_sync(store, httpx.MockTransport(handler))
    assert report["problem"]["status"] == "failed" and "403" in report["problem"]["error"] and report["incident"]["status"] == "ok"
    assert store.knowledge.watermark("servicenow", "problem") is None                   # retried from the same point next time


# ================================================================================================ standards and playbooks
def _library(tmp_path):
    import docx
    for name, lines in {
        "Standards Policies Procedures/SRE-STD-11 — Operational Runbooks Standard.docx":
            ["Every Tier 1 service keeps a runbook reviewed within 12 months.", "Runbooks follow SRE-PB-08 and policy SRE-POL-11."],
        "Standards Policies Procedures/SRE-POL-11 — Operational Runbooks Policy.docx": ["Runbooks are mandatory for production services."],
        "Playbooks/SRE-PB-08 — Operational Runbook Engineering Playbook.docx": ["Step 1: list failure modes. Step 2: write recovery steps."],
        "Assessment Templates/CMDB-AT-10 — Service Criticality Assessment.docx": ["Version one of the tiering assessment."],
        "Assessment Templates/CMDB-AT-10 — Service Criticality Assessment — V2.docx": ["Version two: tier drives failover priority."],
    }.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        d = docx.Document()
        for line in lines:
            d.add_paragraph(line)
        d.save(p)
    return tmp_path


def test_standards_and_playbooks_are_searchable_but_never_evidence(store, svc, tmp_path, monkeypatch):
    from sra import config
    from sra.knowledge.standards import register_standards
    monkeypatch.setattr(config, "DOCS_DIR", _library(tmp_path))
    assert register_standards(store.knowledge) == {"documents": 4, "queued": 4}          # V2 replaces V1 of CMDB-AT-10
    assert ingest(store)["documents"] == 4
    assert register_standards(store.knowledge) == {"documents": 4, "queued": 0}          # unchanged files are not re-ingested
    kinds = dict(rows(store, "SELECT source_id, doc_type FROM sra_documents WHERE source_system = 'oipreqs'"))
    assert kinds == {"SRE-STD-11": "standard", "SRE-POL-11": "policy", "SRE-PB-08": "playbook", "CMDB-AT-10": "assessment_template"}
    r = svc.search("runbook reviewed within 12 months", None, principals=["anyone"], source_systems=["oipreqs"])
    top = r["results"][0]
    assert top["source"] == "Operational Runbooks Standard" and top["doc_type"] == "standard" and top["source_system"] == "oipreqs"
    assert {(x["type"], x["title"]) for x in top["related"]} == {("references", "Operational Runbook Engineering Playbook"),
                                                                 ("references", "Operational Runbooks Policy")}
    assert "Version two" in svc.search("tier drives failover priority", None, doc_types=["assessment_template"])["results"][0]["text"]
    # a standard says what should exist; it must not be retrieved as evidence that an application has it
    a = new_assessment(svc, "Runbook")                                                   # even an app whose name matches the text
    svc.add_evidence(a.id, [("orders-dr-plan.md", DR_PLAN)], None, "tester")
    assert {x["source_system"] for x in svc.search("runbook reviewed within 12 months", a.id, k=20)["results"]} <= {"upload"}


# ================================================================================================ fallbacks and API
def test_delete_removes_the_uploaded_documents(store, svc):
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK)], None, "tester")
    ingest(store)
    svc.delete(a.id, "tester")
    assert one(store, "SELECT count(*) FROM sra_documents WHERE assessment_id = %s", a.id)[0] == 0
    assert one(store, "SELECT count(*) FROM sra_chunks c WHERE NOT EXISTS (SELECT 1 FROM sra_documents d WHERE d.id = c.document_id)")[0] == 0


def test_real_embedding_model_when_cached(store, svc, monkeypatch):
    """bge-small through FastEmbed, as in production; skipped when the model is not in the local cache."""
    from sra.retrieval import chunks
    monkeypatch.setattr(chunks, "_EMBEDDER", [])
    if chunks.shared_embedder() is None:
        pytest.skip("embedding model not available locally")
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK), ("orders-load-test.md", PERF)], None, "tester")
    r = svc.search("how do we recover if the main database goes down", a.id, k=1)
    assert r["mode"].startswith("hybrid") and r["results"][0]["source"] == "orders-runbook.md"


def test_without_pgvector_permission_retrieval_is_keyword_only(store):
    """A database user that may not create extensions (a shared CAP database before an admin enables pgvector)."""
    import psycopg
    from sra.selection.profile import ApplicationProfile
    from sra.service import AssessmentService
    from sra.store import LocalBlobs, PgStore
    db, role = f"sra_novec_{uuid.uuid4().hex[:6]}", f"limited_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")
        admin.execute(f"CREATE DATABASE {db}")
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"cannot create a scratch database here: {exc}")
    base = psycopg.conninfo.conninfo_to_dict(DSN)
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(**{**base, "dbname": db}), autocommit=True) as c:
            c.execute(f"GRANT CREATE, USAGE ON SCHEMA public TO {role}")
        s = PgStore(psycopg.conninfo.make_conninfo(**{**base, "dbname": db, "user": role, "password": "x"}),
                    blobs=LocalBlobs(Path(tempfile.mkdtemp())))
        try:
            s.init()
            assert s.has_vector is False and s.knowledge.has_vector is False
            service = AssessmentService(s, None)
            a, _ = service.create(ApplicationProfile(name="Orders", tier=2), "t", "t")
            service.add_evidence(a.id, [("orders-dr-plan.md", DR_PLAN), ("orders-runbook.md", RUNBOOK)], None, "t")
            r = service.search("failover", a.id)
            assert r["mode"] == "keyword only" and [x["source"] for x in r["results"]] == ["orders-dr-plan.md"]
        finally:
            s.close()
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        admin.execute(f"DROP ROLE IF EXISTS {role}")
        admin.close()


def test_rerank_stage_with_the_local_cross_encoder(store, svc, monkeypatch):
    from sra.retrieval import chunks
    monkeypatch.setenv("RETRIEVAL_RERANK", "auto")
    monkeypatch.setattr(chunks, "_RERANKER", [])
    if chunks.shared_reranker() is None:
        pytest.skip("reranker model not in the local cache")
    a = new_assessment(svc)
    svc.add_evidence(a.id, [("orders-runbook.md", RUNBOOK), ("orders-dr-plan.md", DR_PLAN), ("orders-load-test.md", PERF)], None, "tester")
    r = svc.search("what was the P95 response time in the load test", a.id, k=3)
    assert r["reranked"] and r["results"][0]["source"] == "orders-load-test.md"


def test_endpoints_on_postgres(store, svc, monkeypatch):
    from fastapi.testclient import TestClient
    from sra.api.app import create_app
    monkeypatch.setattr("sra.connectors.servicenow.setting", lambda name, default=None: None)   # no instance configured
    monkeypatch.setattr("sra.knowledge.standards.library", lambda docs_dir=None: [])           # keep this test fast
    c = TestClient(create_app(store=store, run_jobs_inline=True))        # inline: the ingestion job runs within the request
    made = c.post("/api/assessments", json={"profile": {"name": f"Web {uuid.uuid4().hex[:6]}", "tier": 2}}).json()["assessment"]
    svc._made.append(made["id"])
    up = c.post(f"/api/assessments/{made['id']}/evidence", files=[("files", ("orders-dr-plan.md", DR_PLAN, "text/markdown"))])
    assert up.status_code == 201, up.text
    locked = c.post(f"/api/assessments/{made['id']}/evidence", data={"acl": "security-team"},
                    files=[("files", ("orders-security-review.md", SECRET, "text/markdown"))])
    assert locked.status_code == 201, locked.text
    assert store.knowledge.pending_count() == 0                          # the upload triggered the ingestion service
    r = c.post(f"/api/assessments/{made['id']}/search", json={"query": "failover", "k": 5}, headers={"X-User": "bob"})
    assert r.status_code == 200, r.text
    assert {x["source"] for x in r.json()["results"]} == {"orders-dr-plan.md"}
    r = c.post("/api/search", json={"query": "failover", "application": made["application"]["name"]},
               headers={"X-User": "bob", "X-Groups": "sre, security-team"})
    hits = r.json()["results"]
    assert {x["source"] for x in hits} == {"orders-dr-plan.md", "orders-security-review.md"}
    assert c.post("/api/search", json={"query": ""}).status_code == 422
    # knowledge store endpoints
    st = c.get("/api/knowledge/status").json()
    assert st["vector_search"] is True and sum(d["documents"] for d in st["documents"] if d["source_system"] == "upload") >= 2
    assert all(d["pending"] == 0 and d["failed"] == 0 for d in st["documents"])
    sync = c.post("/api/knowledge/sync", json={"standards": True, "servicenow": True})
    assert sync.status_code == 202 and sync.json()["queued"] is True
    open_doc = next(x for x in hits if x["source"] == "orders-dr-plan.md")["document_id"]
    locked_doc = next(x for x in hits if x["source"] == "orders-security-review.md")["document_id"]
    assert c.get(f"/api/knowledge/documents/{open_doc}", headers={"X-User": "bob"}).json()["title"] == "orders-dr-plan.md"
    assert c.get(f"/api/knowledge/documents/{locked_doc}", headers={"X-User": "bob"}).status_code == 404
    assert c.get(f"/api/knowledge/documents/{locked_doc}", headers={"X-User": "bob", "X-Groups": "security-team"}).status_code == 200
    ev = c.get(f"/api/assessments/{made['id']}/evidence").json()
    assert ev["synced"] == [] and len(ev["files"]) == 2
