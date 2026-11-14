"""The single-process CAP runtime OIP uses: in-process scheduler, evidence blobs in Postgres, and a database that is
down at start does not stop the process (no network, no database)."""
import os
from datetime import datetime

import pytest

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra import scheduler  # noqa: E402
from sra.api.app import create_app  # noqa: E402
from sra.store import AnyBlobs, FileStore, LocalBlobs, PgBlobs, PgStore  # noqa: E402


# ---------------------------------------------------------------------------------------------------- scheduler
def test_hours_and_next_run():
    assert scheduler.parse_hours("14, 2;2") == [2, 14] and scheduler.parse_hours("") == []
    with pytest.raises(ValueError):
        scheduler.parse_hours("24")
    assert scheduler.next_run(datetime(2026, 11, 14, 1, 59), [2, 14]) == datetime(2026, 11, 14, 2)
    assert scheduler.next_run(datetime(2026, 11, 14, 2, 0), [2, 14]) == datetime(2026, 11, 14, 14)
    assert scheduler.next_run(datetime(2026, 11, 14, 15), [2, 14]) == datetime(2026, 11, 15, 2)


def test_jobs_run_in_order_and_a_failure_does_not_stop_the_scheduler(monkeypatch):
    monkeypatch.setattr(scheduler, "ingest_job", lambda: {"job": "sync"})
    ran = []

    def run(job):
        ran.append(job)
        if len(ran) == 1:
            raise RuntimeError("ServiceNow down")
    times = iter([datetime(2026, 11, 14, 1, 59), datetime(2026, 11, 14, 2, 0, 1), datetime(2026, 11, 14, 2, 0, 1)])
    s = scheduler.Scheduler(run, [2], run_on_start=True, clock=lambda: next(times))
    s.stop.wait = lambda timeout=None: None
    orig = s._run_once

    def once():
        orig()
        if len(ran) == 2:
            s.stop.set()
    s._run_once = once
    s.loop()
    assert ran == [{"job": "sync"}, {"job": "sync"}]       # the failed start-up run did not stop the scheduled one


def test_nothing_scheduled_without_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("SRA_INGEST_HOURS", raising=False)
    monkeypatch.delenv("SRA_RUN_ON_START", raising=False)
    assert scheduler.start_from_settings(FileStore(tmp_path)) is None
    monkeypatch.setenv("SRA_INGEST_HOURS", "2")
    assert scheduler.start_from_settings(FileStore(tmp_path)) is None      # FileStore has no knowledge store


# ---------------------------------------------------------------------------------------------------- blobs
class FakePool:
    """Enough of a psycopg pool for PgBlobs: an in-memory sra_blobs table."""

    def __init__(self):
        self.rows = {}
        outer = self

        class Cur:
            rowcount = 0

            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                if sql.startswith("INSERT"):
                    outer.rows[params[0]] = params[1]
                    return Cur()
                if sql.startswith("SELECT"):
                    return Cur((outer.rows[params[0]],) if params[0] in outer.rows else None)
                prefix = params[0][:-1].replace("\\_", "_").replace("\\%", "%").replace("\\\\", "\\")
                gone = [k for k in outer.rows if k.startswith(prefix)]
                for k in gone:
                    del outer.rows[k]
                c = Cur()
                c.rowcount = len(gone)
                return c
        self.conn = Conn

    def connection(self):
        return self.conn()


def test_blobs_live_in_the_database(tmp_path):
    pool = FakePool()
    b = PgBlobs(pool)
    uri = b.put("A1/EV1__runbook.docx", b"\x00docx")
    assert uri == "pg:A1/EV1__runbook.docx" and b.get(uri) == b"\x00docx"
    b.put("A1/EV2__x.csv", b"a,b")
    b.put("A10/EV1__y.csv", b"c")
    assert b.delete_prefix("A1/") == 2 and list(pool.rows) == ["A10/EV1__y.csv"]
    with pytest.raises(FileNotFoundError):
        b.get(uri)
    # files stored on disk before the switch stay readable
    local = LocalBlobs(tmp_path)
    old = local.put("A2/EV1__old.txt", b"old")
    mixed = AnyBlobs(b, {"file": local})
    assert mixed.get(old) == b"old" and mixed.put("A3/k", b"new") == "pg:A3/k"


# ---------------------------------------------------------------------------------------------------- startup
def down_store():
    s = PgStore.__new__(PgStore)            # no pool: init always fails, as with a database that is down
    s.init = lambda: (_ for _ in ()).throw(ConnectionError("connection refused\nmore"))
    s.pool = object()
    s.bus = s
    return s


def test_database_down_at_start_keeps_the_service_up(tmp_path):
    s = down_store()
    assert s.try_init() is False and s.init_error == "connection refused"
    files = FileStore(tmp_path)
    for name in ("list", "get_job", "cancel_requested", "set_job"):
        setattr(s, name, getattr(files, name))
    c = TestClient(create_app(store=s, run_jobs_inline=True))
    h = c.get("/healthz")
    assert h.status_code == 200 and h.json()["database"] == "error: connection refused"
    r = c.get("/api/assessments")
    assert r.status_code == 503 and "connection refused" in r.json()["detail"]
    # the database comes back: the next attempt (after the retry interval) applies the schema and the API serves
    s.init, s._init_tried = (lambda: None), -1e9
    assert c.get("/api/assessments").status_code == 200 and c.get("/healthz").json()["database"] == "ok"
