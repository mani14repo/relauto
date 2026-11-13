"""Put the OIPReqs library (standards, policies, procedures, playbooks, assessment templates) into the knowledge
store, so "retrieve reliability standards and guidance" is served by search.

Registering is cheap and idempotent: a document whose file content is unchanged is not queued again. The ingestion
service does the chunking and embedding. These documents describe what should exist, so they are never used as
evidence about an application (see retrieval.pg.EVIDENCE_SOURCES).
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .. import config
from .store import CODE, OIPREQS, KnowledgeStore

log = logging.getLogger(__name__)

TYPES = {"STD": "standard", "POL": "policy", "PRC": "procedure", "PB": "playbook", "AT": "assessment_template"}
VERSION = re.compile(r"\bV(\d+)\b")


def library(docs_dir: Path | None = None) -> list[dict]:
    """One entry per document; when two versions of a coded document sit side by side, the highest wins."""
    docs_dir = docs_dir or config.DOCS_DIR
    best: dict[str, dict] = {}
    for path in sorted(docs_dir.rglob("*.docx")):
        if path.name.startswith("~$"):
            continue
        stem = path.stem
        m = CODE.match(stem)
        code = m.group(1) if m else stem
        version = max((int(v) for v in VERSION.findall(stem)), default=1)
        parts = [p.strip() for p in stem.split("—")]
        title = parts[1] if m and len(parts) > 1 else parts[0]
        entry = {"code": code, "title": title, "version": version, "path": path.relative_to(docs_dir).as_posix(),
                 "doc_type": TYPES.get(code.split("-")[1], "reference") if m else "reference",
                 "domain": code.split("-")[0] if m else "", "file": path}
        if code not in best or version > best[code]["version"]:
            best[code] = entry
    return list(best.values())


def register_standards(knowledge: KnowledgeStore, docs_dir: Path | None = None) -> dict:
    """Queue new or changed OIPReqs documents for ingestion. Returns {"documents": n, "queued": n}."""
    entries = library(docs_dir)
    queued = 0
    with knowledge.pool.connection() as c, c.transaction():
        for e in entries:
            sha = hashlib.sha256(e["file"].read_bytes()).hexdigest()
            _, changed = knowledge.upsert(
                c, source_system=OIPREQS, source_id=e["code"], title=e["title"], content_hash=sha, kind="standard",
                doc_type=e["doc_type"], tags=[t for t in (e["domain"], f"V{e['version']}") if t],
                meta={"path": e["path"], "version": e["version"], "domain": e["domain"]})
            queued += changed
    log.info("OIPReqs library: %s documents, %s queued for ingestion", len(entries), queued)
    return {"documents": len(entries), "queued": queued}
