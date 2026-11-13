"""The ingestion service (docs/Hybrid Retrieval Architecture Overview.png, box 2).

    extract and normalise -> chunk and enrich -> generate embeddings -> store in PostgreSQL

It works through the queue of documents waiting in sra_documents (indexed_at IS NULL), whatever put them there: an
upload, a connector sync, the standards loader. It runs outside the request that saved the document: in the Kafka
worker, in a background thread of a single-process deployment, or as its own process (`sra ingest`).
"""
from __future__ import annotations

import logging
import time

from ..evidence.bundle import EvidenceBundle
from ..retrieval.chunks import CHUNK_CHARS, _split, chunk_bundle
from .store import CODE, OIPREQS, SERVICENOW, UPLOAD, KnowledgeStore, doc_id

log = logging.getLogger(__name__)

EMBED_BATCH = 64
LONG = 80          # a field longer than this is prose (its own paragraph), shorter is an attribute (key=value)


def record_text(table: str, record: dict) -> str:
    """A ServiceNow record as retrievable text: a label, its short attributes on one line, then its prose fields."""
    label = record.get("number") or record.get("name") or record.get("sys_id") or ""
    attrs, prose = [], []
    for k, v in record.items():
        if k.startswith("_") or k in ("sys_id", "number") or v in (None, "", []):
            continue
        s = str(v)
        (prose if len(s) > LONG or k in ("short_description", "description") else attrs).append((k, s))
    lines = [f"[ServiceNow {table} {label}]", "; ".join(f"{k}={v}" for k, v in attrs)]
    lines += [f"{k}: {v}" for k, v in prose]
    notes = record.get("_human_notes") or []
    if notes:
        lines.append("notes: " + " | ".join(str(n) for n in notes[-5:]))
    return "\n".join(x for x in lines if x)


class IngestionService:
    def __init__(self, knowledge: KnowledgeStore, embedder=None):
        self.knowledge, self.embedder = knowledge, embedder

    # ------------------------------------------------------------------------------------------------ extract + chunk
    def _upload(self, doc: dict):
        b = EvidenceBundle()
        item = b.ingest(doc["title"], self.knowledge.blobs.get(doc["blob_uri"]), hint=doc.get("hint"))
        if item is None:
            return [], {"kind": "", "doc_type": None, "table_name": None, "tags": ["unreadable"]}, None
        chunks = [(c.id, c.text) for c in chunk_bundle(b)]
        tags = [t for t in (item.meta.get("format"), "ocr" if item.meta.get("ocr") or item.meta.get("images_read") else None) if t]
        return chunks, {"kind": item.kind.value, "doc_type": item.doc_type.value if item.doc_type else None,
                        "table_name": item.table, "tags": tags}, None

    def _record(self, doc: dict):
        text = record_text(doc["table_name"] or "record", doc["record"] or {})
        chunks = [(f"{doc['id']}#{n}", part) for n, part in enumerate(_split(text, CHUNK_CHARS))]
        refs = (doc.get("meta") or {}).get("links") or []
        links = [(typ, doc_id(SERVICENOW, target)) for typ, target in refs]
        return chunks, {"kind": "record", "doc_type": None, "table_name": doc["table_name"], "tags": None}, links

    def _standard(self, doc: dict):
        from .. import config
        from ..ooxml import docx_text
        path = config.DOCS_DIR / (doc.get("meta") or {})["path"]
        text = docx_text(path)
        label = f"[{doc['doc_type'] or 'document'} {doc['source_id']} '{doc['title']}']\n"
        chunks = [(f"{doc['id']}#{n}", label + part) for n, part in enumerate(_split(text, CHUNK_CHARS))]
        cited = dict.fromkeys(CODE.findall(text))
        links = [("references", doc_id(OIPREQS, code)) for code in cited if code != doc["source_id"]]
        return chunks, {"kind": "standard", "doc_type": doc["doc_type"], "table_name": None, "tags": None}, links

    def _extract(self, doc: dict):
        return {UPLOAD: self._upload, SERVICENOW: self._record, OIPREQS: self._standard}[doc["source_system"]](doc)

    # ------------------------------------------------------------------------------------------------ the loop
    def run_pending(self, limit: int = 200, assessment_id: str | None = None, max_documents: int | None = None,
                    should_stop=None) -> dict:
        """Ingest waiting documents until the queue is empty (or max_documents is reached). Returns counts."""
        k = self.knowledge
        stats = {"documents": 0, "chunks": 0, "failed": 0, "embedded": 0, "seconds": 0.0}
        started = time.monotonic()
        while not (should_stop and should_stop()):
            room = limit if max_documents is None else min(limit, max_documents - stats["documents"] - stats["failed"])
            if room <= 0:
                break
            batch = k.pending(room, assessment_id)
            if not batch:
                break
            work = []
            for doc in batch:
                try:
                    work.append((doc, *self._extract(doc)))
                except Exception as exc:  # noqa: BLE001 - one bad document must not block the queue
                    log.warning("could not ingest %s (%s): %s", doc["title"], doc["id"], exc)
                    k.failed(doc["id"], f"{type(exc).__name__}: {exc}")
                    stats["failed"] += 1
            vectors = None
            texts = [t for _, chunks, _, _ in work for _, t in chunks]
            if self.embedder is not None and k.has_vector and texts:
                vectors = []
                for i in range(0, len(texts), EMBED_BATCH):
                    vectors.extend(self.embedder.embed(texts[i:i + EMBED_BATCH]))
                stats["embedded"] += len(texts)
            at = 0
            for doc, chunks, facts, links in work:
                mine = vectors[at:at + len(chunks)] if vectors is not None else None
                at += len(chunks)
                with k.pool.connection() as c, c.transaction():
                    k.write_chunks(c, doc["id"], chunks, mine, getattr(self.embedder, "name", None), **facts)
                    if links is not None:
                        k.relate(c, doc["id"], links)
                    ci = (doc.get("meta") or {}).get("ci_link")
                    if ci:      # cmdb_rel_ci: the typed link between the two configuration items themselves
                        k.relate_items(c, doc_id(SERVICENOW, ci["from"]), doc_id(SERVICENOW, ci["to"]), ci["type"])
                stats["documents"] += 1
                stats["chunks"] += len(chunks)
            if work:
                with k.pool.connection() as c, c.transaction():
                    k.link_to_services(c, [d["id"] for d, *_ in work])
        if assessment_id is None:
            stats["embedded"] += k.embed_missing(self.embedder)
        stats["seconds"] = round(time.monotonic() - started, 1)
        if stats["documents"] or stats["failed"]:
            log.info("ingested %(documents)s document(s), %(chunks)s chunk(s), %(failed)s failed in %(seconds)ss", stats)
        return stats
