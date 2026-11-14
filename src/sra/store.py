"""Persistence: assessments, evidence blobs, audit log, outbox.

    FileStore  - local directory (dev, tests, single-user CLI)
    PgStore    - PostgreSQL (Cloud SQL) with outbox; blobs in the database (sra_blobs, the default) or GCS (gs://)

Chosen by settings: DATABASE_URL (or CAP's CAP_POSTGRES_* settings) set -> PgStore, else FileStore(SRA_DATA_DIR).
"""
from __future__ import annotations

import json
import logging
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

log = logging.getLogger(__name__)
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


class PgBlobs:
    """Evidence files in the database itself (sra_blobs), as OIP keeps everything in its one CAP Postgres database.

    The default for PgStore: the container's disk is scratch space (lost on restart) and CAP provisions no bucket for
    OIP, so the database is the system of record. Uploads are capped by SRA_MAX_UPLOAD_MB (50 MB), well within bytea.
    """

    def __init__(self, pool) -> None:
        self.pool = pool

    def put(self, key: str, data: bytes) -> str:
        with self.pool.connection() as c:
            c.execute("INSERT INTO sra_blobs (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = "
                      "EXCLUDED.data, created_at = now()", (key, data))
        return f"pg:{key}"

    def get(self, uri: str) -> bytes:
        if not uri.startswith("pg:"):
            raise ValueError(f"not a database blob: {uri}")
        with self.pool.connection() as c:
            row = c.execute("SELECT data FROM sra_blobs WHERE key = %s", (uri[3:],)).fetchone()
        if not row:
            raise FileNotFoundError(uri)
        return bytes(row[0])

    def delete_prefix(self, prefix: str) -> int:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.pool.connection() as c:
            return c.execute("DELETE FROM sra_blobs WHERE key LIKE %s", (like,)).rowcount


class AnyBlobs:
    """Writes to `primary`; reads any URI scheme, so files stored before a switch of blob store stay readable."""

    def __init__(self, primary, readers: dict) -> None:
        self.primary, self.readers = primary, readers

    def put(self, key: str, data: bytes) -> str:
        return self.primary.put(key, data)

    def get(self, uri: str) -> bytes:
        scheme = uri.split(":", 1)[0]
        reader = self.readers.get(scheme)
        if reader is None and scheme == "gs":
            reader = self.readers["gs"] = GcsBlobs(uri.split("/", 3)[2])
        return (reader or self.primary).get(uri)

    def delete_prefix(self, prefix: str) -> int:
        return self.primary.delete_prefix(prefix)


def blobs_from_settings(pool=None) -> BlobStore:
    """SRA_GCS_BUCKET set: GCS. Otherwise the database when there is one (OIP/CAP default), else local files."""
    bucket = config.setting("SRA_GCS_BUCKET")
    local = LocalBlobs(config.DATA_DIR / "blobs")
    if bucket:
        return GcsBlobs(bucket)
    if pool is not None:
        return AnyBlobs(PgBlobs(pool), {"file": local})
    return local


# ------------------------------------------------------------------ store
class Store(Protocol):
    bus: Bus

    def save(self, a: Assessment, scorecard: ScoreCard | None = None, events: list[dict] = (), audit: list[dict] = ()) -> None: ...
    def get(self, aid: str) -> Assessment: ...
    def list(self, limit: int = 100) -> list[dict]: ...
    def scorecard(self, aid: str) -> dict | None: ...
    # acl: users/groups allowed to read this file through retrieval (None/empty = everyone with the assessment)
    def add_evidence(self, aid: str, name: str, data: bytes, hint: str | None = None, acl: list[str] | None = None) -> dict: ...
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

    def add_evidence(self, aid, name, data, hint=None, acl=None):
        eid, sha = evidence_id(data)
        safe = SAFE.sub("_", Path(name).name)[:120]
        uri = self.blobs.put(f"{aid}/{eid}__{safe}", data)
        row = {"id": eid, "name": name, "sha256": sha, "blob_uri": uri, "hint": hint, "size": len(data),
               "ingested_at": datetime.now(timezone.utc).isoformat(), **({"acl": sorted(set(acl))} if acl else {})}
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
        self.blobs = blobs or blobs_from_settings(self.pool)
        self.bus = self   # publish() writes to the outbox
        from .knowledge.store import KnowledgeStore
        self.knowledge = KnowledgeStore(self.pool, self.blobs)     # the standing knowledge store (sra/knowledge)

    has_vector = False          # pgvector available: semantic search on; otherwise keyword search only
    iterative_scan = False      # pgvector >= 0.8
    init_error: str | None = None   # set while the schema could not be applied (database down at start)
    _init_tried = 0.0
    RETRY_INIT_S = 30

    def init(self) -> None:
        sql = (Path(__file__).parent / "db" / "schema.sql").read_text(encoding="utf-8")
        with self.pool.connection() as c:
            c.execute(sql)
        self._enable_vector()
        self.init_error = None

    def try_init(self) -> bool:
        """Apply the schema, recording a failure instead of raising (as OIP's prepare_database does): the process and
        its health probe stay up, and the API reports the database error until a later attempt succeeds."""
        self._init_tried = time.monotonic()
        try:
            self.init()
            self.init_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - any connection or SQL failure
            self.init_error = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
            log.error("database not ready: %s", self.init_error)
            return False

    def ready(self) -> bool:
        """True once the schema is applied; retries at most every RETRY_INIT_S seconds while it is not."""
        if self.init_error is None:
            return True
        if time.monotonic() - self._init_tried >= self.RETRY_INIT_S:
            with _INIT_LOCK:
                if self.init_error is not None and time.monotonic() - self._init_tried >= self.RETRY_INIT_S:
                    self.try_init()
        return self.init_error is None

    def close(self) -> None:
        self.pool.close()

    def _enable_vector(self) -> None:
        """Turn on pgvector when possible: create the extension (needs the privilege once per database), then the
        embedding column and its HNSW index. A database without it keeps working with keyword search."""
        import psycopg
        try:
            with self.pool.connection() as c:
                c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error as exc:
            log.warning("could not create the pgvector extension (%s); checking whether it already exists",
                        str(exc).strip().splitlines()[0])
        with self.pool.connection() as c:
            row = c.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
            if not row:
                log.warning("pgvector is not enabled in this database: retrieval is keyword-only until an administrator "
                            "runs CREATE EXTENSION vector")
                self.has_vector = False
                return
            c.execute("ALTER TABLE sra_chunks ADD COLUMN IF NOT EXISTS embedding vector(384)")
            c.execute("CREATE INDEX IF NOT EXISTS sra_chunks_vec_idx ON sra_chunks USING hnsw (embedding vector_cosine_ops)")
        self.has_vector = True
        try:
            self.iterative_scan = tuple(int(x) for x in row[0].split(".")[:2]) >= (0, 8)
        except ValueError:
            self.iterative_scan = False
        self.knowledge.has_vector, self.knowledge.iterative_scan = self.has_vector, self.iterative_scan

    # ------------------------------------------------------------------ knowledge store and hybrid retrieval
    def app_names(self, aid: str) -> list[str]:
        """The application's name and aliases: how standing records (e.g. synced ServiceNow rows) are matched to it."""
        with self.pool.connection() as c:
            row = c.execute("SELECT p.name, p.profile FROM sra_assessments a JOIN sra_applications p ON p.id = a.application_id "
                            "WHERE a.id = %s", (aid,)).fetchone()
        if not row:
            return []
        return [row[0], *[x for x in ((row[1] or {}).get("aliases") or []) if x]]

    def ingest_pending(self, aid: str | None = None, embedder=None) -> dict:
        """Run the ingestion service here and now (for one assessment's uploads, or for everything waiting)."""
        from .knowledge.ingest import IngestionService
        return IngestionService(self.knowledge, embedder).run_pending(assessment_id=aid)

    def retriever(self, aid: str | None, embedder=None, principals: list[str] | None = None, **scope):
        """A hybrid index (keyword + vector + RRF, optional rerank) over the knowledge store.

        With an assessment id: its uploaded files plus the standing records about its application. Anything of the
        assessment still waiting for the ingestion service is ingested first, so an automated pass never runs on a
        half-indexed set. Without one: the whole store, narrowed by `scope` and `principals`."""
        from .knowledge.store import like_patterns
        from .retrieval import pg
        from .retrieval.chunks import shared_reranker
        patterns = None
        if aid:
            if self.knowledge.pending_count(aid):
                self.ingest_pending(aid, embedder)
            patterns = like_patterns(self.app_names(aid))
        return pg.PgHybridIndex(self.pool, aid, embedder, shared_reranker(), self.has_vector, self.iterative_scan,
                                principals, app_patterns=patterns, **scope)

    def set_job(self, aid, job):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO sra_jobs (assessment_id, state, updated_at) VALUES (%s, %s, now()) "
                      "ON CONFLICT (assessment_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                      (aid, Jsonb(job)))

    def get_job(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT state FROM sra_jobs WHERE assessment_id = %s", (aid,)).fetchone()
        return row[0] if row else None

    def request_cancel(self, aid):
        with self.pool.connection() as c:
            c.execute("INSERT INTO sra_jobs (assessment_id, state, cancel_requested) VALUES (%s, '{}'::jsonb, true) "
                      "ON CONFLICT (assessment_id) DO UPDATE SET cancel_requested = true", (aid,))

    def cancel_requested(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT cancel_requested FROM sra_jobs WHERE assessment_id = %s", (aid,)).fetchone()
        return bool(row and row[0])

    def clear_cancel(self, aid):
        with self.pool.connection() as c:
            c.execute("UPDATE sra_jobs SET cancel_requested = false WHERE assessment_id = %s", (aid,))

    def publish(self, topic, key, payload):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO sra_outbox (topic, key, payload) VALUES (%s, %s, %s)", (topic, key, Jsonb(payload)))

    def save(self, a, scorecard=None, events=(), audit=()):
        from psycopg.types.json import Jsonb
        doc = json.loads(a.model_dump_json())
        app_id = "APP-" + re.sub(r"[^a-z0-9]+", "-", a.application.name.lower()).strip("-")
        with self.pool.connection() as c, c.transaction():
            c.execute("""INSERT INTO sra_applications (id, name, cmdb_id, profile) VALUES (%s, %s, %s, %s)
                         ON CONFLICT (name) DO UPDATE SET profile = EXCLUDED.profile, cmdb_id = EXCLUDED.cmdb_id""",
                      (app_id, a.application.name, a.application.cmdb_id, Jsonb(a.profile)))
            c.execute("""INSERT INTO sra_assessments (id, application_id, status, doc, scorecard, matrix_version)
                         VALUES (%s, (SELECT id FROM sra_applications WHERE name = %s), %s, %s, %s, %s)
                         ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, doc = EXCLUDED.doc,
                           scorecard = COALESCE(EXCLUDED.scorecard, sra_assessments.scorecard),
                           matrix_version = EXCLUDED.matrix_version, rev = sra_assessments.rev + 1, updated_at = now()""",
                      (a.id, a.application.name, a.status, Jsonb(doc),
                       Jsonb(json.loads(scorecard.model_dump_json())) if scorecard else None, a.matrix_version))
            c.execute("DELETE FROM sra_findings WHERE assessment_id = %s", (a.id,))
            for f in a.findings:
                rec = f.recommendation
                c.execute("""INSERT INTO sra_findings (id, assessment_id, type, severity, status, title, criterion_ids, rubric_ids,
                             detector, due_date, owner, doc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (f.id, a.id, f.type.value, f.severity.value, f.status.value, f.title, f.criterion_ids, f.rubric_ids,
                           f.detector, rec.due_date if rec else None, rec.owner if rec else "", Jsonb(json.loads(f.model_dump_json()))))
            for row in audit:
                c.execute("INSERT INTO sra_audit_log (assessment_id, actor, action, target, before, after) VALUES (%s,%s,%s,%s,%s,%s)",
                          (a.id, row.get("actor", "system"), row["action"], row.get("target", ""),
                           Jsonb(row.get("before")), Jsonb(row.get("after"))))
            for e in events:
                c.execute("INSERT INTO sra_outbox (topic, key, payload) VALUES (%s, %s, %s)", (EVENTS_TOPIC, a.id, Jsonb(e)))

    def get(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT doc FROM sra_assessments WHERE id = %s", (aid,)).fetchone()
        if not row:
            raise KeyError(aid)
        return Assessment.model_validate(row[0])

    def list(self, limit=100):
        with self.pool.connection() as c:
            rows = c.execute("""SELECT a.id, p.name, a.status, a.doc->>'assessment_date', jsonb_array_length(a.doc->'templates'),
                                jsonb_array_length(a.doc->'findings'), a.scorecard->>'rating', a.scorecard->>'index',
                                a.scorecard->>'maturity_name'
                                FROM sra_assessments a JOIN sra_applications p ON p.id = a.application_id
                                ORDER BY a.updated_at DESC LIMIT %s""", (limit,)).fetchall()
        keys = ["id", "application", "status", "date", "templates", "findings", "rating", "index", "maturity"]
        return [dict(zip(keys, r)) for r in rows]

    def scorecard(self, aid):
        with self.pool.connection() as c:
            row = c.execute("SELECT scorecard FROM sra_assessments WHERE id = %s", (aid,)).fetchone()
        return row[0] if row else None

    def add_evidence(self, aid, name, data, hint=None, acl=None):
        from psycopg.types.json import Jsonb
        eid, sha = evidence_id(data)
        safe = SAFE.sub("_", Path(name).name)[:120]
        uri = self.blobs.put(f"{aid}/{eid}__{safe}", data)
        meta = {"size": len(data), **({"acl": sorted(set(acl))} if acl else {})}
        with self.pool.connection() as c:
            c.execute("""INSERT INTO sra_evidence (id, assessment_id, name, sha256, blob_uri, hint, meta)
                         VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (eid, aid, name, sha, uri, hint, Jsonb(meta)))
            # Queue it for the ingestion service (chunking and embedding do not run in the upload request).
            app = c.execute("SELECT p.name FROM sra_assessments a JOIN sra_applications p ON p.id = a.application_id "
                            "WHERE a.id = %s", (aid,)).fetchone()
            from .knowledge.store import UPLOAD
            self.knowledge.upsert(c, source_system=UPLOAD, source_id=f"{aid}/{eid}", title=name, content_hash=sha,
                                  assessment_id=aid, evidence_id=eid, application=app[0] if app else "",
                                  scope_text=app[0] if app else "", acl=list(acl or []), blob_uri=uri, hint=hint)
        return {"id": eid, "name": name, "sha256": sha, "blob_uri": uri, "hint": hint, "size": len(data)}

    def evidence(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT id, name, sha256, blob_uri, hint, meta, ingested_at FROM sra_evidence WHERE assessment_id = %s "
                             "ORDER BY ingested_at", (aid,)).fetchall()
        return [{"id": r[0], "name": r[1], "sha256": r[2], "blob_uri": r[3], "hint": r[4], "size": (r[5] or {}).get("size"),
                 "ingested_at": r[6].isoformat()} for r in rows]

    def bundle(self, aid, as_of=None):
        """The assessment's uploaded files, plus the synced source records about its application. An uploaded export
        of a table takes precedence over the synced copy of that table, so nothing is counted twice."""
        b = _bundle_from(self.evidence(aid), self.blobs, as_of)
        from .connectors.servicenow import restore
        for table, records in self.knowledge.records_for(self.app_names(aid)).items():
            if b.has_table(table):
                b.notes.append(f"ServiceNow sync has {len(records)} {table} record(s); the uploaded {table} export is used instead")
                continue
            b.add_table(table, [restore(r) for r in records], f"servicenow:{table}", as_of)
        return b

    def audit_log(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT actor, action, target, before, after, at FROM sra_audit_log WHERE assessment_id = %s ORDER BY id",
                             (aid,)).fetchall()
        return [{"actor": r[0], "action": r[1], "target": r[2], "before": r[3], "after": r[4], "at": r[5].isoformat()} for r in rows]

    def feedback(self, aid, criterion_id, ai, human, kind="override"):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as c:
            c.execute("INSERT INTO sra_feedback (assessment_id, criterion_id, kind, ai_result, human_result) VALUES (%s,%s,%s,%s,%s)",
                      (aid, criterion_id, kind, Jsonb(ai), Jsonb(human)))

    def feedback_rows(self, aid):
        with self.pool.connection() as c:
            rows = c.execute("SELECT criterion_id, kind, ai_result, human_result, at FROM sra_feedback WHERE assessment_id = %s "
                             "ORDER BY id", (aid,)).fetchall()
        return [{"criterion_id": r[0], "kind": r[1], "ai": r[2], "human": r[3], "at": r[4].isoformat()} for r in rows]

    def delete(self, aid, actor, keep_feedback=None):
        from psycopg.types.json import Jsonb
        a = self.get(aid)                     # KeyError if it does not exist
        with self.pool.connection() as c, c.transaction():
            n = c.execute("SELECT count(*) FROM sra_evidence WHERE assessment_id = %s", (aid,)).fetchone()[0]
            # evidence, evidence_chunks and findings go with the assessment (ON DELETE CASCADE)
            c.execute("DELETE FROM sra_assessments WHERE id = %s", (aid,))
            c.execute("DELETE FROM sra_jobs WHERE assessment_id = %s", (aid,))
            c.execute("DELETE FROM sra_feedback WHERE assessment_id = %s", (aid,))
            for r in keep_feedback or []:     # re-inserted with the evidence excerpts they were decided on
                c.execute("""INSERT INTO sra_feedback (assessment_id, criterion_id, kind, ai_result, human_result, at, application, excerpts)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (aid, r["criterion_id"], r.get("kind", "override"), Jsonb(r.get("ai")), Jsonb(r["human"]),
                           r.get("at") or datetime.now(timezone.utc).isoformat(), r.get("application", ""), Jsonb(r.get("excerpts"))))
            c.execute("DELETE FROM sra_audit_log WHERE assessment_id = %s", (aid,))
            c.execute("INSERT INTO sra_audit_log (assessment_id, actor, action, target, after) VALUES (%s,%s,'deleted',%s,%s)",
                      (aid, actor, a.application.name, Jsonb({"evidence_files": n})))
            c.execute("INSERT INTO sra_outbox (topic, key, payload) VALUES (%s, %s, %s)",
                      (EVENTS_TOPIC, aid, Jsonb(envelope("deleted", aid, {"application": a.application.name}, actor))))
        files = self.blobs.delete_prefix(f"{aid}/")   # after the commit: a failure here leaves orphan files, not a half-deleted record
        return {"id": aid, "application": a.application.name, "actor": actor, "evidence_files": files,
                "feedback_kept": len(keep_feedback or [])}


_INIT_LOCK = threading.Lock()


def store_from_settings() -> Store:
    dsn = config.database_dsn()      # DATABASE_URL, or the CAP_POSTGRES_* settings CAP injects
    if dsn:
        s = PgStore(dsn)
        s.try_init()                 # a database that is down at start does not stop the process (see PgStore.ready)
        return s
    return FileStore()
