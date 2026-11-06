"""Evidence chunking and in-memory hybrid retrieval (BM25 + optional dense embeddings, fused with RRF).

The service uses the same chunking and scoring idea in Postgres (tsvector + pgvector, see db/schema.sql);
this module serves the offline CLI, tests, and small assessments without a database.
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
            chunks.append(Chunk(id=f"{it.id}#{n}", evidence_id=it.id, source=it.name, kind=it.kind.value, text=p))
    return chunks


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class HybridIndex:
    """BM25 over chunks; if an embedder is supplied, dense cosine ranks are fused with Reciprocal Rank Fusion."""

    def __init__(self, chunks: list[Chunk], embedder=None, k1: float = 1.4, b: float = 0.75):
        self.chunks = chunks
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
        lexical = self._bm25(tokens(query))
        if self.vectors is None:
            return [self.chunks[i] for _, i in lexical[:k]]
        qv = self.embedder.embed([query])[0]
        dense = sorted(((sum(a * b for a, b in zip(qv, v)), i) for i, v in enumerate(self.vectors)), reverse=True)
        fused = Counter()
        for rank, (_, i) in enumerate(lexical[:50]):
            fused[i] += 1 / (rrf_k + rank + 1)
        for rank, (_, i) in enumerate(dense[:50]):
            fused[i] += 1 / (rrf_k + rank + 1)
        return [self.chunks[i] for i, _ in fused.most_common(k)]


class FastEmbedder:
    """Local ONNX embeddings (BAAI/bge-small-en-v1.5, 384-d). Optional dependency: fastembed."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
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
        _INDEXES[key] = HybridIndex(chunk_bundle(bundle), embedder=embedder)
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
