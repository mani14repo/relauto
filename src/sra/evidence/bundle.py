"""EvidenceBundle: everything ingested for one assessment, with typed accessors for detectors."""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .model import DocType, EvidenceItem, Kind, ScanFinding, evidence_id
from .parsers import (classify_document, detect_scan_format, docx_to_text, image_to_text, is_ci_config, parse_k8s,
                      parse_scan, parse_scan_csv, pdf_to_text, review_date)
from .redact import find_secrets, redact
from . import registers
from .servicenow import guess_table, normalize, read_rows

TEXT_EXT = {".md", ".txt", ".rst", ".log", ".adoc", ".html", ".htm"}
YAML_EXT = {".yaml", ".yml"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}   # text read with OCR
CONFIG_EXT = {".properties", ".conf", ".ini", ".toml", ".cfg", ".env", ".xml", ".tf", ".hcl", ".tfvars"}


class EvidenceBundle:
    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self.scan_findings: list[ScanFinding] = []
        self.components: list[dict] = []
        self.k8s: list[tuple[str, dict]] = []          # (evidence id, object)
        self.errors: list[str] = []

    # ------------------------------------------------------------ ingestion
    def ingest(self, name: str, data: bytes, hint: str | None = None, as_of: datetime | None = None) -> EvidenceItem | None:
        """Ingest one file. `hint` may be a ServiceNow table name or a DocType value."""
        eid, sha = evidence_id(data)
        if eid in self.items:
            return self.items[eid]
        now = as_of or datetime.now()
        ext = Path(name).suffix.lower()
        try:
            item = self._parse(eid, sha, name, data, ext, hint, now)
        except Exception as exc:  # keep going; the gap shows up as missing evidence
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None
        if item:
            self.items[eid] = item
        return item

    def ingest_path(self, path: str | Path, hint: str | None = None) -> EvidenceItem | None:
        p = Path(path)
        return self.ingest(p.name, p.read_bytes(), hint)

    def _parse(self, eid, sha, name, data, ext, hint, now) -> EvidenceItem | None:
        base = dict(id=eid, name=name, sha256=sha, ingested_at=now)
        reg = hint[len(registers.PREFIX):] if hint and hint.startswith(registers.PREFIX) else None
        if ext in (".csv", ".xlsx") and (reg or (not hint and registers.register_type(name))):
            reg = reg or registers.register_type(name)
            raw_rows = registers.read_xlsx(data) if ext == ".xlsx" else list(
                csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace"), newline="")))
            rows = registers.normalize(raw_rows, reg)
            return EvidenceItem(kind=Kind.SN_TABLE, table=registers.PREFIX + reg, records=rows,
                                meta={"rows": len(rows), "register": reg}, **base)
        if ext == ".xlsx":                # a spreadsheet export of a ServiceNow table
            rows = [normalize(r) for r in registers.read_xlsx(data)]
            table = hint if hint and hint not in [d.value for d in DocType] else guess_table(name, rows)
            return EvidenceItem(kind=Kind.SN_TABLE, table=table, records=rows, meta={"rows": len(rows)}, **base)
        if ext in (".csv", ".json"):
            if ext == ".json":
                obj = json.loads(data.decode("utf-8-sig"))
                fmt = detect_scan_format(obj)
                if fmt:
                    findings, comps = parse_scan(obj, fmt)
                    return self._scan(base, fmt, findings, comps)
            else:
                findings = parse_scan_csv(data.decode("utf-8-sig", errors="replace"))
                if findings is not None and (hint in (None, "scan")):
                    return self._scan(base, "csv", findings, [])
            rows = read_rows(data, name)
            table = hint if hint and hint not in [d.value for d in DocType] else guess_table(name, rows)
            return EvidenceItem(kind=Kind.SN_TABLE, table=table, records=rows, meta={"rows": len(rows)}, **base)
        if ext in YAML_EXT or ext in CONFIG_EXT or ext == "" or name.lower() == "jenkinsfile":
            raw = data.decode("utf-8", errors="replace")
            hits = find_secrets(raw)
            text = redact(raw)
            if is_ci_config(name, text):
                return EvidenceItem(kind=Kind.CI_CONFIG, text=text, meta={"tool": _ci_tool(name, text), "raw_secret_scan": hits}, **base)
            objs = parse_k8s(raw) if ext in YAML_EXT else []
            if objs:
                self.k8s.extend((eid, o) for o in objs)
                return EvidenceItem(kind=Kind.K8S, text=text, meta={"objects": len(objs), "raw_secret_scan": hits}, **base)
            return EvidenceItem(kind=Kind.OTHER, text=text, meta={"raw_secret_scan": hits}, **base)
        meta = {}
        if ext == ".docx":
            text, modified, meta = docx_to_text(data)
        elif ext == ".pdf":
            (text, meta), modified = pdf_to_text(data), None
            if not text.strip():
                self.errors.append(f"{name}: no readable text in this PDF; it is kept, but nothing in it can be checked")
        elif ext in IMAGE_EXT:
            text, modified = image_to_text(data), None
            meta["ocr"] = True
            if not text.strip():
                self.errors.append(f"{name}: no readable text in this image; it is kept, but nothing in it can be checked")
        elif ext in TEXT_EXT:
            text, modified = data.decode("utf-8", errors="replace"), None
        else:
            self.errors.append(f"{name}: unsupported file type {ext}")
            return None
        self.errors.extend(f"{name}: {n}" for n in meta.get("image_notes", []))
        hits = find_secrets(text)
        text = redact(text)
        doc_type = DocType(hint) if hint in [d.value for d in DocType] else classify_document(name, text)
        return EvidenceItem(kind=Kind.DOCUMENT, doc_type=doc_type, text=text, meta={**meta, "raw_secret_scan": hits},
                            reviewed_at=review_date(text) or modified, **base)

    def _scan(self, base, fmt, findings, comps) -> EvidenceItem:
        for f in findings:
            f.evidence_id = base["id"]
        self.scan_findings.extend(findings)
        self.components.extend(comps)
        return EvidenceItem(kind=Kind.SCAN, meta={"format": fmt, "findings": len(findings), "components": len(comps)}, **base)

    # ------------------------------------------------------------ accessors
    def table(self, name: str) -> list[dict]:
        rows = []
        for it in self.items.values():
            if it.kind == Kind.SN_TABLE and it.table == name:
                rows.extend(dict(r, _evidence=it.id) for r in it.records)
        return rows

    def has_table(self, name: str) -> bool:
        return any(it.kind == Kind.SN_TABLE and it.table == name for it in self.items.values())

    def table_ids(self, name: str) -> list[str]:
        return [it.ref() for it in self.items.values() if it.kind == Kind.SN_TABLE and it.table == name]

    def documents(self, *types: DocType) -> list[EvidenceItem]:
        return [it for it in self.items.values() if it.kind == Kind.DOCUMENT and (not types or it.doc_type in types)]

    def of_kind(self, kind: Kind) -> list[EvidenceItem]:
        return [it for it in self.items.values() if it.kind == kind]

    def summary(self) -> dict:
        by = defaultdict(int)
        for it in self.items.values():
            by[it.table or (it.doc_type.value if it.doc_type else it.kind.value)] += 1
        return dict(by)


def _ci_tool(name: str, text: str) -> str:
    low = name.lower()
    if "jenkinsfile" in low or text.lstrip().startswith("pipeline {"):
        return "jenkins"
    if ".github/workflows" in low.replace("\\", "/") or "runs-on" in text:
        return "github_actions"
    if "gitlab" in low:
        return "gitlab"
    return "unknown"
