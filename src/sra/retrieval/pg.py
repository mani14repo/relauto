"""Hybrid retrieval in PostgreSQL over the standing knowledge store (sra_documents + sra_chunks; see
docs/Hybrid Retrieval Architecture Overview.png, box 4).

    1. keyword search (tsvector + GIN)        2. semantic vector search (pgvector, HNSW)
    3. Reciprocal Rank Fusion in Python       4. optional local cross-encoder rerank
    5. access check and source references on every result

Writing the store is the ingestion service's job (sra/knowledge/ingest.py). Without the pgvector extension, or
without the embedding model, steps 2-3 fall away and keyword search is used alone.
"""
from __future__ import annotations

import logging
import re

from .chunks import CANDIDATES, Chunk, Reranker, finish, rrf

log = logging.getLogger(__name__)

WORD = re.compile(r"[a-z0-9]{2,}")
MAX_QUERY_TERMS = 48
COLUMNS = ("d.id, c.id, d.assessment_id, COALESCE(d.evidence_id, d.id), d.title, d.kind, d.doc_type, d.application, "
           "d.source_system, c.text")
FROM = "sra_chunks c JOIN sra_documents d ON d.id = c.document_id"
# What may serve as evidence about an application. Standards and playbooks describe what should exist, not what
# does, so they are never retrieved as evidence for a criterion (they are searchable through the API).
EVIDENCE_SOURCES = ("upload", "servicenow")


def vector_literal(v: list[float]) -> str:
    """pgvector's text form; passed as a parameter and cast with ::vector (no adapter registration needed)."""
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def keyword_query(query: str) -> str:
    """The query as OR-ed words for websearch_to_tsquery: any term may match, ts_rank_cd puts the best first.
    (The default AND would return nothing for a long criterion text.)"""
    seen = dict.fromkeys(WORD.findall(query.lower()))
    return " or ".join(list(seen)[:MAX_QUERY_TERMS])


class PgHybridIndex:
    """search(query, k) over the knowledge store, with the same interface as the in-memory HybridIndex.

    Scope
      assessment_id   the files uploaded to that assessment, plus (app_patterns) the standing records about its
                      application from the evidence sources, e.g. synced ServiceNow incidents.
      otherwise       the whole store, narrowed by application / kinds / doc_types / source_systems.
    principals        the caller's user name, groups and roles: a document is returned when its access list is
                      empty or names one of them. None = a trusted internal caller (the assessment's automated pass).
    """

    def __init__(self, pool, assessment_id: str | None = None, embedder=None, reranker: Reranker | None = None,
                 has_vector: bool = True, iterative_scan: bool = False, principals: list[str] | None = None,
                 application: str | None = None, kinds: list[str] | None = None, doc_types: list[str] | None = None,
                 source_systems: list[str] | None = None, app_patterns: list[str] | None = None):
        self.pool, self.assessment_id, self.embedder, self.reranker = pool, assessment_id, embedder, reranker
        self.has_vector, self.iterative_scan, self.principals = has_vector, iterative_scan, principals
        self.application, self.kinds, self.doc_types = application, kinds, doc_types
        self.source_systems, self.app_patterns = source_systems, app_patterns
        self.last: dict = {}              # how the last search was answered (for the API and logs)

    def _scope(self) -> tuple[str, list]:
        where, params = [], []
        if self.assessment_id:
            if self.app_patterns:
                where.append("(d.assessment_id = %s OR (d.assessment_id IS NULL AND d.source_system = ANY(%s) "
                             "AND d.scope_text LIKE ANY(%s)))")
                params += [self.assessment_id, list(EVIDENCE_SOURCES), list(self.app_patterns)]
            else:
                where.append("d.assessment_id = %s")
                params.append(self.assessment_id)
        if self.application:
            where.append("lower(d.application) = lower(%s)")
            params.append(self.application)
        if self.kinds:
            where.append("d.kind = ANY(%s)")
            params.append(list(self.kinds))
        if self.doc_types:
            where.append("d.doc_type = ANY(%s)")
            params.append(list(self.doc_types))
        if self.source_systems:
            where.append("d.source_system = ANY(%s)")
            params.append(list(self.source_systems))
        if self.principals is not None:
            where.append("(d.acl = '{}' OR d.acl && %s::text[])")
            params.append(list(self.principals))
        return (" AND ".join(where) or "true"), params

    def _keyword(self, conn, query: str, limit: int) -> list[tuple]:
        q = keyword_query(query)
        if not q:
            return []
        scope, params = self._scope()
        return conn.execute(
            f"SELECT {COLUMNS}, ts_rank_cd(c.tsv, q, 32) AS score FROM {FROM}, websearch_to_tsquery('english', %s) q "
            f"WHERE {scope} AND c.tsv @@ q ORDER BY score DESC, c.id LIMIT %s", [q] + params + [limit]).fetchall()

    def _vector(self, conn, query: str, limit: int) -> list[tuple]:
        if not (self.has_vector and self.embedder is not None):
            return []
        qv = vector_literal(self.embedder.embed([query])[0])
        scope, params = self._scope()
        with conn.transaction():
            if self.iterative_scan:           # pgvector >= 0.8: keep scanning the HNSW index until the filter is satisfied
                conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
            return conn.execute(
                f"SELECT {COLUMNS}, 1 - (c.embedding <=> %s::vector) AS score FROM {FROM} "
                f"WHERE {scope} AND c.embedding IS NOT NULL ORDER BY c.embedding <=> %s::vector LIMIT %s",
                [qv] + params + [qv, limit]).fetchall()

    def search(self, query: str, k: int = 6, rrf_k: int = 60) -> list[Chunk]:
        with self.pool.connection() as conn:
            lexical = self._keyword(conn, query, CANDIDATES)
            dense = self._vector(conn, query, CANDIDATES)
        key = lambda r: (r[0], r[1])  # noqa: E731  (document id, chunk id)
        rows = {key(r): r for r in lexical + dense}
        rankings = [[key(r) for r in lexical]] + ([[key(r) for r in dense]] if dense else [])
        ranked = [Chunk(document_id=r[0], id=r[1], assessment_id=r[2] or "", evidence_id=r[3], source=r[4], kind=r[5],
                        doc_type=r[6], application=r[7], source_system=r[8], text=r[9])
                  for r in (rows[x] for x in rrf(rankings, rrf_k))]
        out = finish(query, ranked, k, self.reranker)
        self.last = {"keyword_hits": len(lexical), "vector_hits": len(dense),
                     "mode": "hybrid (keyword + vector, RRF)" if dense else "keyword only",
                     "reranked": self.reranker is not None and len(ranked) > 1,
                     "keyword_rank": {key(r): n for n, r in enumerate(lexical, 1)},
                     "vector_rank": {key(r): n for n, r in enumerate(dense, 1)}}
        return out
