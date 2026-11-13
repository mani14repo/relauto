"""Evidence chunking and hybrid retrieval: keyword search + semantic vector search, fused with Reciprocal Rank
Fusion (RRF), optionally reranked by a local cross-encoder.

Two indexes share the chunking, the RRF and the reranker in this module:
  * HybridIndex (here): in memory (BM25 + cosine). Used without a database: the CLI, tests, FileStore.
  * PgHybridIndex (retrieval/pg.py): PostgreSQL full-text search (tsvector + GIN) and pgvector (HNSW) over the
    knowledge store (sra_documents + sra_chunks). Used whenever the service runs on Postgres.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..evidence.bundle import EvidenceBundle
from ..evidence.model import Kind

TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-\.]{1,}")
CHUNK_CHARS = 1200
OVERLAP = 150


@dataclass
class Chunk:
    id: str            # <evidence id>#<n>
    evidence_id: str
    source: str        # file name
    kind: str
    text: str
    doc_type: str | None = None    # runbook, architecture, ... (documents only)
    assessment_id: str = ""        # set by the Postgres index (searches can span assessments)
    application: str = ""
    document_id: str = ""          # sra_documents.id in the knowledge store
    source_system: str = "upload"  # upload | servicenow | oipreqs


def _split(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        cut = text.rfind("\n", start + size // 2, end)
        if cut == -1 or end == len(text):
            cut = end
        parts.append(text[start:cut].strip())
        if cut >= len(text):
            break
        start = max(cut - overlap, start + 1)
    return [p for p in parts if p]


def _table_text(item) -> list[str]:
    """Tables become a summary chunk plus compact row chunks (dates and key fields only)."""
    rows = item.records
    cols = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_")]
    head = f"ServiceNow {item.table} export '{item.name}': {len(rows)} rows. Columns: {', '.join(cols[:40])}."
    out = [head]
    keep = [c for c in cols if c in ("number", "name", "short_description", "category", "subcategory", "priority", "state",
                                     "opened_at", "resolved_at", "close_code", "close_notes", "problem_id", "backout_plan",
                                     "test_plan", "start_date", "end_date", "owned_by", "support_group", "kb_category",
                                     "sys_updated_on", "workaround", "known_error", "parent", "child", "type", "cmdb_ci",
                                     "cmdb_ci_business_app", "description", "source", "u_region")] or cols[:8]
    lines = ["; ".join(f"{c}={str(r.get(c, ''))[:160]}" for c in keep if r.get(c)) for r in rows]
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) > CHUNK_CHARS:
            out.append(buf)
            buf = ""
        buf += ln + "\n"
    if buf:
        out.append(buf)
    return out


def chunk_bundle(bundle: EvidenceBundle) -> list[Chunk]:
    chunks: list[Chunk] = []
    for it in bundle.items.values():
        if it.kind == Kind.SN_TABLE:
            parts = _table_text(it)
        elif it.kind == Kind.SCAN:
            fs = [f for f in bundle.scan_findings if f.evidence_id == it.id]
            parts = [f"Scanner report '{it.name}' ({it.meta.get('format')}): {len(fs)} findings.\n" +
                     "\n".join(f"{f.rule_id} {f.severity} {f.package} {f.installed_version} fix={f.fixed_version or 'none'} {f.title[:80]}"
                               for f in fs[:60])]
        else:
            label = f"[{it.kind.value}{'/' + it.doc_type.value if it.doc_type else ''} '{it.name}'" + \
                    (f", reviewed {it.reviewed_at.date().isoformat()}" if it.reviewed_at else "") + "]\n"
            parts = [label + p for p in _split(it.text)]
        for n, p in enumerate(parts):
            chunks.append(Chunk(id=f"{it.id}#{n}", evidence_id=it.id, source=it.name, kind=it.kind.value, text=p,
                                doc_type=it.doc_type.value if it.doc_type else None))
    return chunks


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def rrf(rankings: list[list], rrf_k: int = 60) -> list:
    """Reciprocal Rank Fusion: every ranking gives an item 1 / (rrf_k + rank); items are returned best first."""
    fused: Counter = Counter()
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            fused[key] += 1 / (rrf_k + rank + 1)
    return [key for key, _ in fused.most_common()]


CANDIDATES = 50          # how many results each of keyword and vector search contributes to the fusion
RERANK_POOL = 20         # how many fused results the optional reranker re-orders


class Reranker:
    """Optional last ranking step: a local cross-encoder scores (query, passage) pairs together, which is more
    accurate than comparing separately computed embeddings. ONNX on CPU through fastembed; only loaded from the
    local model cache (never downloaded here)."""

    def __init__(self, model: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self.name = model
        self.model = TextCrossEncoder(model_name=model, local_files_only=True)

    def order(self, query: str, chunks: list["Chunk"]) -> list["Chunk"]:
        if len(chunks) < 2:
            return chunks
        scores = list(self.model.rerank(query, [c.text for c in chunks]))
        return [c for _, _, c in sorted(zip(scores, range(len(chunks)), chunks), key=lambda t: (-t[0], t[1]))]


_RERANKER: list = []


def shared_reranker() -> Reranker | None:
    """The reranker when RETRIEVAL_RERANK is on/auto and its model is in the local cache; else None.
    off (default) = none. auto = use it if available. on = same, and log loudly when it is missing."""
    from ..config import setting
    mode = str(setting("RETRIEVAL_RERANK", "off") or "off").strip().lower()
    if mode in ("", "off", "0", "false", "no"):
        return None
    if not _RERANKER:
        try:
            _RERANKER.append(Reranker(setting("RETRIEVAL_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")))
        except Exception as exc:  # noqa: BLE001 - optional stage; retrieval works without it
            if mode == "on":
                import logging
                logging.getLogger(__name__).warning("RETRIEVAL_RERANK=on but the reranker could not be loaded: %s", exc)
            _RERANKER.append(None)
    return _RERANKER[0]


def finish(query: str, ranked: list["Chunk"], k: int, reranker: Reranker | None) -> list["Chunk"]:
    """Steps 4-5 after fusion: optional rerank of the best candidates, then the top k."""
    if reranker is not None:
        ranked = reranker.order(query, ranked[:RERANK_POOL]) + ranked[RERANK_POOL:]
    return ranked[:k]


class HybridIndex:
    """BM25 over chunks; if an embedder is supplied, dense cosine ranks are fused with Reciprocal Rank Fusion."""

    def __init__(self, chunks: list[Chunk], embedder=None, k1: float = 1.4, b: float = 0.75, reranker: Reranker | None = None):
        self.chunks = chunks
        self.reranker = reranker
        self.docs = [Counter(tokens(c.text)) for c in chunks]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0
        self.df = Counter(t for d in self.docs for t in d)
        self.k1, self.b = k1, b
        self.embedder = embedder
        self.vectors = embedder.embed([c.text for c in chunks]) if (embedder and chunks) else None

    def _bm25(self, q: list[str]) -> list[tuple[float, int]]:
        n = len(self.docs)
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                if t in d:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    tf = d[t]
                    s += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.lens[i] / (self.avg or 1)))
            if s > 0:
                out.append((s, i))
        return sorted(out, reverse=True)

    def search(self, query: str, k: int = 6, rrf_k: int = 60) -> list[Chunk]:
        if not self.chunks:
            return []
        lexical = [i for _, i in self._bm25(tokens(query))[:CANDIDATES]]
        rankings = [lexical]
        if self.vectors is not None:
            qv = self.embedder.embed([query])[0]
            dense = sorted(((sum(a * b for a, b in zip(qv, v)), i) for i, v in enumerate(self.vectors)), reverse=True)
            rankings.append([i for _, i in dense[:CANDIDATES]])
        ranked = [self.chunks[i] for i in rrf(rankings, rrf_k)]
        return finish(query, ranked, k, self.reranker)


class FastEmbedder:
    """Local ONNX embeddings (BAAI/bge-small-en-v1.5, 384-d). Optional dependency: fastembed."""

    DIM = 384

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self.name = model
        self.model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self.model.embed(texts)]


_EMBEDDER: list = []          # [FastEmbedder] once loaded, [None] if it cannot be loaded


def shared_embedder() -> FastEmbedder | None:
    """One embedding model per process (loading it takes seconds); None when fastembed or the model is missing."""
    if not _EMBEDDER:
        try:
            _EMBEDDER.append(FastEmbedder())
        except Exception:  # noqa: BLE001 - optional dependency; BM25 alone still works
            _EMBEDDER.append(None)
    return _EMBEDDER[0]


_INDEXES: dict[tuple, HybridIndex] = {}


def cached_index(bundle: EvidenceBundle, embedder=None, keep: int = 8) -> HybridIndex:
    """The index for this exact evidence (ids are content hashes), built once; embeddings are the slow part."""
    key = (tuple(sorted(bundle.items)), embedder is not None)
    if key not in _INDEXES:
        if len(_INDEXES) >= keep:
            _INDEXES.pop(next(iter(_INDEXES)))
        _INDEXES[key] = HybridIndex(chunk_bundle(bundle), embedder=embedder, reranker=shared_reranker())
    return _INDEXES[key]


def focus(text: str, query: str, max_chars: int) -> str:
    """The part of a chunk (up to max_chars, cut at line ends) that shares the most words with the query."""
    if len(text) <= max_chars:
        return text
    want = set(tokens(query))
    lines = text.splitlines(keepends=True)
    head = lines[0] if lines and lines[0].startswith("[") else ""      # keep the "[document 'name', reviewed …]" label
    body = lines[1:] if head else lines
    best, best_score = "", -1
    for i in range(len(body)):
        window, n = "", 0
        for ln in body[i:]:
            if len(head) + len(window) + len(ln) > max_chars:
                break
            window += ln
            n += 1
        if not window:
            window = body[i][: max_chars - len(head)]
        score = len(want & set(tokens(window)))
        if score > best_score:
            best, best_score = window, score
    return (head + best).rstrip()
