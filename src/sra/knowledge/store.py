"""The standing knowledge store in PostgreSQL (docs/Hybrid Retrieval Architecture Overview.png, box 3).

    sra_documents   one row per source document or record: an uploaded evidence file, a ServiceNow record, an
                    OIPReqs standard/playbook. Content metadata, source ids, access list, tags.
    sra_chunks      its text in retrievable pieces: tsvector (GIN) and, with pgvector, embedding (HNSW).
    sra_relations   links between documents: incident -> problem, problem -> change, record -> configuration item,
                    document -> the service it is about, standard -> the documents it references.
    sra_sync_state  where each connector stream stopped (incremental sync).

A document belongs to an assessment only when it was uploaded to one (assessment_id set); those are deleted with the
assessment. Everything else stands on its own and is shared by every consumer.

Writing a document only queues it (indexed_at IS NULL). The ingestion service (knowledge/ingest.py) does the rest.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

UPLOAD, SERVICENOW, OIPREQS = "upload", "servicenow", "oipreqs"


def doc_id(source_system: str, source_id: str) -> str:
    """Stable id from the source identity, so a link can point at a document that has not been ingested yet."""
    return "DOC-" + hashlib.sha1(f"{source_system}|{source_id}".encode()).hexdigest()[:16]


def like_patterns(names: list[str]) -> list[str]:
    """SQL LIKE patterns matching any of the application's names inside a document's scope text."""
    out = []
    for n in names:
        n = (n or "").strip().lower()
        if n:
            out.append("%" + n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return out


class KnowledgeStore:
    def __init__(self, pool, blobs):
        self.pool, self.blobs = pool, blobs
        self.has_vector = False
        self.iterative_scan = False

    # ------------------------------------------------------------------------------------------------ writing
    def upsert(self, conn, *, source_system: str, source_id: str, title: str, content_hash: str, assessment_id: str | None = None,
               evidence_id: str | None = None, kind: str = "", doc_type: str | None = None, table_name: str | None = None,
               application: str = "", scope_text: str = "", tags: list[str] | None = None, acl: list[str] | None = None,
               record: dict | None = None, meta: dict | None = None, blob_uri: str | None = None, hint: str | None = None,
               source_updated_at: datetime | None = None) -> tuple[str, bool]:
        """Insert or update a document. Returns (id, changed); a new or changed document is queued for ingestion."""
        from psycopg.types.json import Jsonb
        did = doc_id(source_system, source_id)
        row = conn.execute(
            """INSERT INTO sra_documents (id, source_system, source_id, assessment_id, evidence_id, title, kind, doc_type,
                                          table_name, application, scope_text, tags, acl, record, meta, blob_uri, hint, sha256,
                                          source_updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                   title = EXCLUDED.title, kind = EXCLUDED.kind, doc_type = COALESCE(EXCLUDED.doc_type, sra_documents.doc_type),
                   table_name = EXCLUDED.table_name, application = EXCLUDED.application, scope_text = EXCLUDED.scope_text,
                   tags = EXCLUDED.tags, acl = EXCLUDED.acl, record = EXCLUDED.record, meta = EXCLUDED.meta,
                   blob_uri = EXCLUDED.blob_uri, hint = EXCLUDED.hint, source_updated_at = EXCLUDED.source_updated_at,
                   indexed_at = CASE WHEN sra_documents.sha256 = EXCLUDED.sha256 AND sra_documents.acl = EXCLUDED.acl
                                     THEN sra_documents.indexed_at END,
                   index_error = CASE WHEN sra_documents.sha256 = EXCLUDED.sha256 THEN sra_documents.index_error END,
                   sha256 = EXCLUDED.sha256
               RETURNING (indexed_at IS NULL)""",
            (did, source_system, source_id, assessment_id, evidence_id, title, kind, doc_type, table_name, application,
             scope_text.lower(), tags or [], sorted(set(acl or [])), Jsonb(record) if record is not None else None,
             Jsonb(meta or {}), blob_uri, hint, content_hash, source_updated_at)).fetchone()
        return did, bool(row[0])

    def relate(self, conn, from_id: str, links: list[tuple[str, str]]) -> None:
        """Replace the outgoing links of a document: [(type, target document id)]."""
        conn.execute("DELETE FROM sra_relations WHERE from_id = %s AND derived = false", (from_id,))
        for typ, to in dict.fromkeys(links):
            if to and to != from_id:
                conn.execute("INSERT INTO sra_relations (from_id, to_id, type) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                             (from_id, to, typ))

    def relate_items(self, conn, from_id: str, to_id: str, typ: str) -> None:
        """A link stated by a third record (a cmdb_rel_ci row) between two documents. Written once its source document
        exists; kept as 'derived' so re-ingesting either item does not remove it."""
        conn.execute("INSERT INTO sra_relations (from_id, to_id, type, derived) SELECT %s, %s, %s, true "
                     "WHERE EXISTS (SELECT 1 FROM sra_documents WHERE id = %s) ON CONFLICT DO NOTHING",
                     (from_id, to_id, typ, from_id))

    # ------------------------------------------------------------------------------------------------ ingestion queue
    def pending(self, limit: int = 200, assessment_id: str | None = None) -> list[dict]:
        where, params = "indexed_at IS NULL AND index_error IS NULL", []
        if assessment_id:
            where += " AND assessment_id = %s"
            params.append(assessment_id)
        with self.pool.connection() as c:
            rows = c.execute(
                "SELECT id, source_system, source_id, assessment_id, evidence_id, title, table_name, application, acl, record, "
                f"meta, blob_uri, hint, doc_type FROM sra_documents WHERE {where} ORDER BY ingested_at, id LIMIT %s",
                params + [limit]).fetchall()
        keys = ("id", "source_system", "source_id", "assessment_id", "evidence_id", "title", "table_name", "application", "acl",
                "record", "meta", "blob_uri", "hint", "doc_type")
        return [dict(zip(keys, r)) for r in rows]

    def pending_count(self, assessment_id: str | None = None) -> int:
        with self.pool.connection() as c:
            if assessment_id:
                return c.execute("SELECT count(*) FROM sra_documents WHERE indexed_at IS NULL AND index_error IS NULL "
                                 "AND assessment_id = %s", (assessment_id,)).fetchone()[0]
            return c.execute("SELECT count(*) FROM sra_documents WHERE indexed_at IS NULL AND index_error IS NULL").fetchone()[0]

    def write_chunks(self, conn, did: str, chunks: list[tuple[str, str]], vectors: list[list[float]] | None, model: str | None,
                     *, kind: str | None = None, doc_type: str | None = None, table_name: str | None = None,
                     tags: list[str] | None = None) -> None:
        """Replace a document's chunks ([(chunk id, text)]) and mark it ingested."""
        from ..retrieval.pg import vector_literal
        conn.execute("DELETE FROM sra_chunks WHERE document_id = %s", (did,))
        for n, (cid, text) in enumerate(chunks):
            text = text.replace("\x00", "")
            if self.has_vector:
                conn.execute("INSERT INTO sra_chunks (document_id, n, id, text, embedding, embedding_model) "
                             "VALUES (%s,%s,%s,%s,%s::vector,%s)",
                             (did, n, cid, text, vector_literal(vectors[n]) if vectors is not None else None,
                              model if vectors is not None else None))
            else:
                conn.execute("INSERT INTO sra_chunks (document_id, n, id, text) VALUES (%s,%s,%s,%s)", (did, n, cid, text))
        conn.execute("UPDATE sra_documents SET indexed_at = now(), index_error = NULL, chunks = %s, "
                     "kind = COALESCE(%s, kind), doc_type = COALESCE(%s, doc_type), table_name = COALESCE(%s, table_name), "
                     "tags = COALESCE(%s, tags) WHERE id = %s", (len(chunks), kind, doc_type, table_name, tags, did))

    def failed(self, did: str, error: str) -> None:
        with self.pool.connection() as c:
            c.execute("UPDATE sra_documents SET index_error = %s WHERE id = %s", (error[:500], did))

    def link_to_services(self, conn, ids: list[str]) -> None:
        """Derived links: a document 'about' an application points at that application's configuration item, in both
        arrival orders (the document first, or the CI first)."""
        if not ids:
            return
        conn.execute(
            """INSERT INTO sra_relations (from_id, to_id, type, derived)
               SELECT d.id, c.id, 'about', true FROM sra_documents d
               JOIN sra_documents c ON c.source_system = 'servicenow' AND c.table_name = 'cmdb_ci'
                                   AND lower(c.title) = lower(d.application)
               WHERE d.application <> '' AND d.id <> c.id AND (d.table_name IS NULL OR d.table_name <> 'cmdb_ci')
                 AND (d.id = ANY(%s) OR c.id = ANY(%s))
               ON CONFLICT DO NOTHING""", (ids, ids))

    def embed_missing(self, embedder, batch: int = 64, limit: int = 2000) -> int:
        """Fill in embeddings for chunks written while no embedding model was loaded."""
        from ..retrieval.pg import vector_literal
        if not self.has_vector or embedder is None:
            return 0
        with self.pool.connection() as c:
            rows = c.execute("SELECT document_id, n, text FROM sra_chunks WHERE embedding IS NULL LIMIT %s", (limit,)).fetchall()
        for start in range(0, len(rows), batch):
            part = rows[start:start + batch]
            vectors = embedder.embed([r[2] for r in part])
            with self.pool.connection() as c, c.transaction():
                for (did, n, _), v in zip(part, vectors):
                    c.execute("UPDATE sra_chunks SET embedding = %s::vector, embedding_model = %s WHERE document_id = %s AND n = %s",
                              (vector_literal(v), getattr(embedder, "name", None), did, n))
        return len(rows)

    # ------------------------------------------------------------------------------------------------ reading
    def records_for(self, names: list[str], source_system: str = SERVICENOW) -> dict[str, list[dict]]:
        """Synced records for an application, by table: {table: [record, ...]} (matched on the scope text, the way the
        rules match exported rows)."""
        pats = like_patterns(names)
        if not pats:
            return {}
        with self.pool.connection() as c:
            rows = c.execute("SELECT table_name, record FROM sra_documents WHERE source_system = %s AND assessment_id IS NULL "
                             "AND record IS NOT NULL AND scope_text LIKE ANY(%s) ORDER BY table_name, source_id",
                             (source_system, pats)).fetchall()
        out: dict[str, list[dict]] = {}
        for table, record in rows:
            out.setdefault(table, []).append(record)
        return out

    def get(self, did: str) -> dict | None:
        with self.pool.connection() as c:
            r = c.execute("SELECT id, source_system, source_id, assessment_id, title, kind, doc_type, table_name, application, "
                          "tags, acl, chunks, source_updated_at, ingested_at, indexed_at, index_error FROM sra_documents "
                          "WHERE id = %s", (did,)).fetchone()
        if not r:
            return None
        keys = ("id", "source_system", "source_id", "assessment_id", "title", "kind", "doc_type", "table", "application", "tags",
                "acl", "chunks", "source_updated_at", "ingested_at", "indexed_at", "index_error")
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(keys, r)}

    def related(self, ids: list[str], principals: list[str] | None = None, per_doc: int = 8) -> dict[str, list[dict]]:
        """Links of each document in both directions, to documents the caller may see."""
        if not ids:
            return {}
        acl, params = "", [ids, ids]
        if principals is not None:
            acl = " AND (o.acl = '{}' OR o.acl && %s::text[])"
            params.append(list(principals))
        with self.pool.connection() as c:
            rows = c.execute(
                f"""SELECT x.doc, x.type, x.direction, o.id, o.title, o.source_system, o.table_name, o.doc_type
                    FROM (SELECT from_id AS doc, to_id AS other, type, 'to' AS direction FROM sra_relations WHERE from_id = ANY(%s)
                          UNION ALL
                          SELECT to_id, from_id, type, 'from' FROM sra_relations WHERE to_id = ANY(%s)) x
                    JOIN sra_documents o ON o.id = x.other WHERE true{acl} ORDER BY x.doc, x.type, o.title""", params).fetchall()
        out: dict[str, list[dict]] = {}
        for doc, typ, direction, oid, title, system, table, doc_type in rows:
            lst = out.setdefault(doc, [])
            if len(lst) < per_doc:
                lst.append({"type": typ, "direction": direction, "document_id": oid, "title": title, "source_system": system,
                            "table": table, "doc_type": doc_type})
        return out

    def status(self) -> dict:
        with self.pool.connection() as c:
            docs = c.execute("SELECT source_system, COALESCE(table_name, doc_type, kind, ''), count(*), "
                             "count(*) FILTER (WHERE indexed_at IS NULL AND index_error IS NULL), "
                             "count(*) FILTER (WHERE index_error IS NOT NULL), COALESCE(sum(chunks), 0) "
                             "FROM sra_documents GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
            sync = c.execute("SELECT source, stream, watermark, last_run, status, records, error FROM sra_sync_state "
                             "ORDER BY source, stream").fetchall()
            rel = c.execute("SELECT type, count(*) FROM sra_relations GROUP BY type ORDER BY type").fetchall()
        return {"vector_search": self.has_vector,
                "documents": [{"source_system": s, "type": t, "documents": n, "pending": p, "failed": f, "chunks": int(ch)}
                              for s, t, n, p, f, ch in docs],
                "relations": {t: n for t, n in rel},
                "sync": [{"source": s, "stream": st, "watermark": w, "last_run": lr.isoformat() if lr else None, "status": status,
                          "records": n, "error": e} for s, st, w, lr, status, n, e in sync]}

    # ------------------------------------------------------------------------------------------------ sync state
    def watermark(self, source: str, stream: str) -> str | None:
        with self.pool.connection() as c:
            r = c.execute("SELECT watermark FROM sra_sync_state WHERE source = %s AND stream = %s", (source, stream)).fetchone()
        return r[0] if r else None

    def set_sync(self, source: str, stream: str, *, watermark: str | None, status: str, records: int, error: str | None = None) -> None:
        with self.pool.connection() as c:
            c.execute("""INSERT INTO sra_sync_state (source, stream, watermark, last_run, status, records, error)
                         VALUES (%s,%s,%s,now(),%s,%s,%s)
                         ON CONFLICT (source, stream) DO UPDATE SET
                             watermark = COALESCE(EXCLUDED.watermark, sra_sync_state.watermark), last_run = now(),
                             status = EXCLUDED.status, records = EXCLUDED.records, error = EXCLUDED.error""",
                      (source, stream, watermark, status, records, error[:500] if error else None))


def content_hash(obj) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


CODE = re.compile(r"\b((?:SRE|ITSM|CMDB)-(?:STD|POL|PRC|PB|AT)-\d{2})\b")
