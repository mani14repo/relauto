"""Persistence: assessments, evidence blobs, audit log, outbox.

    FileStore  - local directory (dev, tests, single-user CLI)
    PgStore    - PostgreSQL (Cloud SQL) with outbox; blobs in GCS (gs://) or a mounted path

Chosen by settings: DATABASE_URL set -> PgStore, else FileStore(SRA_DATA_DIR).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import config
from .events.bus import EVENTS_TOPIC, Bus, InMemoryBus, envelope
from .evidence.bundle import EvidenceBundle
from .evidence.model import evidence_id
from .models import Assessment, ScoreCard

SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


# ------------------------------------------------------------------ blobs
class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, uri: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> int: ...


class LocalBlobs:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, key: str, data: bytes) -> str:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p.as_uri()

    def get(self, uri: str) -> bytes:
        from urllib.parse import unquote, urlparse
        u = urlparse(uri)
        path = unquote(u.path)
        if re.match(r"^/[A-Za-z]:/", path):   # file:///C:/...
            path = path[1:]
        return Path(path).read_bytes()

    def delete_prefix(self, prefix: str) -> int:
        """Delete every blob under `prefix` (an assessment id followed by '/'). Returns how many files went."""
        d = self.root / prefix.rstrip("/")
        if not d.is_dir():
            return 0
        n = sum(1 for p in d.rglob("*") if p.is_file())
        _rmtree(d)
        return n


def _rmtree(path: Path) -> None:
    """shutil.rmtree that tolerates Windows briefly holding a file open."""
    for i in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.1)


class GcsBlobs:
    def __init__(self, bucket: str, prefix: str = "sra") -> None:
        from google.cloud import storage   # optional dependency in the container image
        self.bucket = storage.Client().bucket(bucket)
        self.prefix = prefix

    def put(self, key: str, data: bytes) -> str:
        blob = self.bucket.blob(f"{self.prefix}/{key}")
        blob.upload_from_string(data)
        return f"gs://{self.bucket.name}/{blob.name}"

    def get(self, uri: str) -> bytes:
        name = uri.split("/", 3)[3]
        return self.bucket.blob(name).download_as_bytes()

    def delete_prefix(self, prefix: str) -> int:
        blobs = list(self.bucket.list_blobs(prefix=f"{self.prefix}/{prefix}"))
        for b in blobs:
            b.delete()
        return len(blobs)


def blobs_from_settings() -> BlobStore:
    bucket = config.setting("SRA_GCS_BUCKET")
    return GcsBlobs(bucket) if bucket else LocalBlobs(config.DATA_DIR / "blobs")


# ------------------------------------------------------------------ store
class Store(Protocol):
    bus: Bus

    def save(self, a: Assessment, scorecard: ScoreCard | None = None, events: list[dict] = (), audit: list[dict] = ()) -> None: ...
    def get(self, aid: str) -> Assessment: ...
    def list(self, limit: int = 100) -> list[dict]: ...
    def scorecard(self, aid: str) -> dict | None: ...
    def add_evidence(self, aid: str, name: str, data: bytes, hint: str | None = None) -> dict: ...
    def evidence(self, aid: str) -> list[dict]: ...
    def bundle(self, aid: str, as_of: datetime | None = None) -> EvidenceBundle: ...
    def audit_log(self, aid: str) -> list[dict]: ...
    # SRE decisions on criteria, kept as training data: kind is confirm (agreed with the automated result), override
    # (changed it), fill (decided a criterion nothing had evaluated) or revise (changed an earlier human decision).
    def feedback(self, aid: str, criterion_id: str, ai: dict | None, human: dict, kind: str = "override") -> None: ...
    def feedback_rows(self, aid: str) -> list[dict]: ...
    def set_job(self, aid: str, job: dict) -> None: ...
    def get_job(self, aid: str) -> dict | None: ...
    # Cancel requests live apart from the job record so the running job's progress updates cannot overwrite them.
    def request_cancel(self, aid: str) -> None: ...
    def cancel_requested(self, aid: str) -> bool: ...
    def clear_cancel(self, aid: str) -> None: ...
    # Permanently removes the assessment, its evidence files, findings, job and audit trail. A minimal record
    # (id, application, who, when) is kept so the deletion itself is traceable, and a "deleted" event is sent.
    # keep_feedback: the feedback rows to keep (with evidence excerpts added by the caller), or None to delete them.
    def delete(self, aid: str, actor: str, keep_feedback: list[dict] | None = None) -> dict: ...


def _bundle_from(rows: list[dict], blobs: BlobStore, as_of: datetime | None) -> EvidenceBundle:
    b = EvidenceBundle()
    for r in rows:
        b.ingest(r["name"], blobs.get(r["blob_uri"]), hint=r.get("hint"), as_of=as_of)
    return b


class FileStore:
    def __init__(self, root: Path | None = None, bus: Bus | None = None, blobs: BlobStore | None = None) -> None:
        self.root = Path(root or config.DATA_DIR)
        (self.root / "assessments").mkdir(parents=True, exist_ok=True)
        self.bus = bus or InMemoryBus()
        self.blobs = blobs or LocalBlobs(self.root / "blobs")
        self._lock = threading.RLock()

    def _dir(self, aid: str) -> Path:
        if not re.fullmatch(r"SRA-[0-9a-f]{10}", aid):
            raise KeyError(aid)
        return self.root / "assessments" / aid

    def save(self, a, scorecard=None, events=(), audit=()):
        with self._lock:
            d = self._dir(a.id)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "assessment.json.tmp"
            tmp.write_text(a.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(d / "assessment.json")
            if scorecard is not None:
                (d / "scorecard.json").write_text(scorecard.model_dump_json(indent=1), encoding="utf-8")
            with open(d / "audit.jsonl", "a", encoding="utf-8") as f:
                for row in audit:
                    f.write(json.dumps({**row, "at": datetime.now(timezone.utc).isoformat()}, default=str) + "\n")
        for e in events:
            self.bus.publish(EVENTS_TOPIC, a.id, e)

    def set_job(self, aid, job):
        d = self._dir(aid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"job.json.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(job, default=str), encoding="utf-8")
        for _ in range(5):          # Windows: the reader may hold the file open for a moment
            try:
                tmp.replace(d / "job.json")
                return
            except PermissionError:
                time.sleep(0.05)
        tmp.unlink(missing_ok=True)

    def get_job(self, aid):
        p = self._dir(aid) / "job.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return None

    def request_cancel(self, aid):
        d = self._dir(aid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cancel").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")

    def cancel_requested(self, aid):
        return (self._dir(aid) / "cancel").exists()

    def clear_cancel(self, aid):
        (self._dir(aid) / "cancel").unlink(missing_ok=True)

    def get(self, aid):
        p = self._dir(aid) / "assessment.json"
        if not p.exists():
            raise KeyError(aid)
        return Assessment.model_validate_json(p.read_text(encoding="utf-8"))

    def list(self, limit=100):
        out = []
        for p in sorted((self.root / "assessments").glob("SRA-*/assessment.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            a = Assessment.model_validate_json(p.read_text(encoding="utf-8"))
            sc = self.scorecard(a.id) or {}
            out.append({"id": a.id, "application": a.application.name, "status": a.status, "date": a.assessment_date.isoformat(),
                        "templates": len(a.templates), "findings": len(a.findings), "rating": sc.get("rating"),
                        "index": sc.get("index"), "maturity": sc.get("maturity_name")})
        return out

    def scorecard(self, aid):
        p = self._dir(aid) / "scorecard.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def add_evidence(self, aid, name, data, hint=None):
        eid, sha = evidence_id(data)
        safe = SAFE.sub("_", Path(name).name)[:120]
        uri = self.blobs.put(f"{aid}/{eid}__{safe}", data)
        row = {"id": eid, "name": name, "sha256": sha, "blob_uri": uri, "hint": hint, "size": len(data),
               "ingested_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            idx = self._dir(aid) / "evidence.json"
            rows = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else []
            if not any(r["id"] == eid for r in rows):
                rows.append(row)
                idx.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        return row

    def evidence(self, aid):
        idx = self._dir(aid) / "evidence.json"
        return json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else []

    def bundle(self, aid, as_of=None):
        return _bundle_from(self.evidence(aid), self.blobs, as_of)

    def audit_log(self, aid):
        p = self._dir(aid) / "audit.jsonl"
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def _feedback_file(self, aid: str) -> Path:
        self._dir(aid)                        # validates the id
        d = self.root / "feedback"            # outside the assessment folder, so it can outlive a deletion
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{aid}.jsonl"

    def feedback(self, aid, criterion_id, ai, human, kind="override"):
        row = {"criterion_id": criterion_id, "kind": kind, "ai": ai, "human": human, "at": datetime.now(timezone.utc).isoformat()}
        with self._lock, open(self._feedback_file(aid), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def feedback_rows(self, aid):
        rows = []
        for p in (self._dir(aid) / "feedback.jsonl", self._feedback_file(aid)):   # older versions kept it in the folder
            if p.exists():
                rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return rows

    def delete(self, aid, actor, keep_feedback=None):
        a = self.get(aid)                     # KeyError if it does not exist
        with self._lock:
            fb = self._feedback_file(aid)
            if keep_feedback:
                fb.write_text("".join(json.dumps(r, default=str) + "\n" for r in keep_feedback), encoding="utf-8")
            else:
                fb.unlink(missing_ok=True)
            files = self.blobs.delete_prefix(f"{aid}/")
            _rmtree(self._dir(aid))
            row = {"id": aid, "application": a.application.name, "actor": actor, "evidence_files": files,
                   "feedback_kept": len(keep_feedback or []), "at": datetime.now(timezone.utc).isoformat()}
            with open(self.root / "deleted.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        self.bus.publish(EVENTS_TOPIC, aid, envelope("deleted", aid, {"application": a.application.name}, actor))
        return row


class PgStore:
    """PostgreSQL store. Events go to the outbox in the same transaction (relayed by events/relay.py)."""

    def __init__(self, dsn: str, blobs: BlobStore | None = None) -> None:
        from psycopg_pool import ConnectionPool
        self.pool = ConnectionPool(dsn, min_size=1, max_size=int(config.setting("DB_POOL_MAX", "5")), open=True)
        self.blobs = blobs or blobs_from_settings()
        self.bus = self   # publish() writes to the outbox

    def init(self) -> None:
        sql = (Path(__file__).parent / "db" / "schema.sql").read_text(encoding="utf-8")
        with self.pool.connection() as c:
            c.execute(sql)

    def set_job(self, aid, job):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO jobs (assessment_id, state, updated_at) VALUES (%s, %s, now()) "
                      "ON CONFLICT (assessment_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                      (aid, Jsonb(job)))

    def get_job(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT state FROM jobs WHERE assessment_id = %s", (aid,)).fetchone()
        return row[0] if row else None

    def request_cancel(self, aid):
        with self.pool.connection() as c:
            c.execute("INSERT INTO jobs (assessment_id, state, cancel_requested) VALUES (%s, '{}'::jsonb, true) "
                      "ON CONFLICT (assessment_id) DO UPDATE SET cancel_requested = true", (aid,))

    def cancel_requested(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT cancel_requested FROM jobs WHERE assessment_id = %s", (aid,)).fetchone()
        return bool(row and row[0])

    def clear_cancel(self, aid):
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET cancel_requested = false WHERE assessment_id = %s", (aid,))

    def publish(self, topic, key, payload):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO outbox (topic, key, payload) VALUES (%s, %s, %s)", (topic, key, Jsonb(payload)))

    def save(self, a, scorecard=None, events=(), audit=()):
        from psycopg.types.json import Jsonb
        doc = json.loads(a.model_dump_json())
        app_id = "APP-" + re.sub(r"[^a-z0-9]+", "-", a.application.name.lower()).strip("-")
        with self.pool.connection() as c, c.transaction():
            c.execute("""INSERT INTO applications (id, name, cmdb_id, profile) VALUES (%s, %s, %s, %s)
                         ON CONFLICT (name) DO UPDATE SET profile = EXCLUDED.profile, cmdb_id = EXCLUDED.cmdb_id""",
                      (app_id, a.application.name, a.application.cmdb_id, Jsonb(a.profile)))
            c.execute("""INSERT INTO assessments (id, application_id, status, doc, scorecard, matrix_version)
                         VALUES (%s, (SELECT id FROM applications WHERE name = %s), %s, %s, %s, %s)
                         ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, doc = EXCLUDED.doc,
                           scorecard = COALESCE(EXCLUDED.scorecard, assessments.scorecard),
                           matrix_version = EXCLUDED.matrix_version, rev = assessments.rev + 1, updated_at = now()""",
                      (a.id, a.application.name, a.status, Jsonb(doc),
                       Jsonb(json.loads(scorecard.model_dump_json())) if scorecard else None, a.matrix_version))
            c.execute("DELETE FROM findings WHERE assessment_id = %s", (a.id,))
            for f in a.findings:
                rec = f.recommendation
                c.execute("""INSERT INTO findings (id, assessment_id, type, severity, status, title, criterion_ids, rubric_ids,
                             detector, due_date, owner, doc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (f.id, a.id, f.type.value, f.severity.value, f.status.value, f.title, f.criterion_ids, f.rubric_ids,
                           f.detector, rec.due_date if rec else None, rec.owner if rec else "", Jsonb(json.loads(f.model_dump_json()))))
            for row in audit:
                c.execute("INSERT INTO audit_log (assessment_id, actor, action, target, before, after) VALUES (%s,%s,%s,%s,%s,%s)",
                          (a.id, row.get("actor", "system"), row["action"], row.get("target", ""),
                           Jsonb(row.get("before")), Jsonb(row.get("after"))))
            for e in events:
                c.execute("INSERT INTO outbox (topic, key, payload) VALUES (%s, %s, %s)", (EVENTS_TOPIC, a.id, Jsonb(e)))

    def get(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT doc FROM assessments WHERE id = %s", (aid,)).fetchone()
        if not row:
            raise KeyError(aid)
        return Assessment.model_validate(row[0])

    def list(self, limit=100):
        with self.pool.connection() as c:
            rows = c.execute("""SELECT a.id, p.name, a.status, a.doc->>'assessment_date', jsonb_array_length(a.doc->'templates'),
                                jsonb_array_length(a.doc->'findings'), a.scorecard->>'rating', a.scorecard->>'index',
                                a.scorecard->>'maturity_name'
                                FROM assessments a JOIN applications p ON p.id = a.application_id
                                ORDER BY a.updated_at DESC LIMIT %s""", (limit,)).fetchall()
        keys = ["id", "application", "status", "date", "templates", "findings", "rating", "index", "maturity"]
        return [dict(zip(keys, r)) for r in rows]

    def scorecard(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT scorecard FROM assessments WHERE id = %s", (aid,)).fetchone()
        return row[0] if row else None

    def add_evidence(self, aid, name, data, hint=None):
        from psycopg.types.json import Jsonb
        eid, sha = evidence_id(data)
        safe = SAFE.sub("_", Path(name).name)[:120]
        uri = self.blobs.put(f"{aid}/{eid}__{safe}", data)
        with self.pool.connection() as c:
            c.execute("""INSERT INTO evidence (id, assessment_id, name, sha256, blob_uri, hint, meta)
                         VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (eid, aid, name, sha, uri, hint, Jsonb({"size": len(data)})))
        return {"id": eid, "name": name, "sha256": sha, "blob_uri": uri, "hint": hint, "size": len(data)}

    def evidence(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT id, name, sha256, blob_uri, hint, meta, ingested_at FROM evidence WHERE assessment_id = %s "
                             "ORDER BY ingested_at", (aid,)).fetchall()
        return [{"id": r[0], "name": r[1], "sha256": r[2], "blob_uri": r[3], "hint": r[4], "size": (r[5] or {}).get("size"),
                 "ingested_at": r[6].isoformat()} for r in rows]

    def bundle(self, aid, as_of=None):
        return _bundle_from(self.evidence(aid), self.blobs, as_of)

    def audit_log(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT actor, action, target, before, after, at FROM audit_log WHERE assessment_id = %s ORDER BY id",
                             (aid,)).fetchall()
        return [{"actor": r[0], "action": r[1], "target": r[2], "before": r[3], "after": r[4], "at": r[5].isoformat()} for r in rows]

    def feedback(self, aid, criterion_id, ai, human, kind="override"):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO feedback (assessment_id, criterion_id, kind, ai_result, human_result) VALUES (%s,%s,%s,%s,%s)",
                      (aid, criterion_id, kind, Jsonb(ai), Jsonb(human)))

    def feedback_rows(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT criterion_id, kind, ai_result, human_result, at FROM feedback WHERE assessment_id = %s "
                             "ORDER BY id", (aid,)).fetchall()
        return [{"criterion_id": r[0], "kind": r[1], "ai": r[2], "human": r[3], "at": r[4].isoformat()} for r in rows]

    def delete(self, aid, actor, keep_feedback=None):
        from psycopg.types.json import Jsonb
        a = self.get(aid)                     # KeyError if it does not exist
        with self.pool.connection() as c, c.transaction():
            n = c.execute("SELECT count(*) FROM evidence WHERE assessment_id = %s", (aid,)).fetchone()[0]
            # evidence, evidence_chunks and findings go with the assessment (ON DELETE CASCADE)
            c.execute("DELETE FROM assessments WHERE id = %s", (aid,))
            c.execute("DELETE FROM jobs WHERE assessment_id = %s", (aid,))
            c.execute("DELETE FROM feedback WHERE assessment_id = %s", (aid,))
            for r in keep_feedback or []:     # re-inserted with the evidence excerpts they were decided on
                c.execute("""INSERT INTO feedback (assessment_id, criterion_id, kind, ai_result, human_result, at, application, excerpts)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (aid, r["criterion_id"], r.get("kind", "override"), Jsonb(r.get("ai")), Jsonb(r["human"]),
                           r.get("at") or datetime.now(timezone.utc).isoformat(), r.get("application", ""), Jsonb(r.get("excerpts"))))
            c.execute("DELETE FROM audit_log WHERE assessment_id = %s", (aid,))
            c.execute("INSERT INTO audit_log (assessment_id, actor, action, target, after) VALUES (%s,%s,'deleted',%s,%s)",
                      (aid, actor, a.application.name, Jsonb({"evidence_files": n})))
            c.execute("INSERT INTO outbox (topic, key, payload) VALUES (%s, %s, %s)",
                      (EVENTS_TOPIC, aid, Jsonb(envelope("deleted", aid, {"application": a.application.name}, actor))))
        files = self.blobs.delete_prefix(f"{aid}/")   # after the commit: a failure here leaves orphan files, not a half-deleted record
        return {"id": aid, "application": a.application.name, "actor": actor, "evidence_files": files,
                "feedback_kept": len(keep_feedback or [])}


def store_from_settings() -> Store:
    dsn = config.setting("DATABASE_URL")
    if dsn:
        s = PgStore(dsn)
        s.init()
        return s
    return FileStore()
