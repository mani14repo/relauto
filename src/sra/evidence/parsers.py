"""Parsers for uploaded evidence: documents, scanner output, Kubernetes manifests and CI config."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime

import yaml

from ..ooxml import body_blocks, Para, Table
from .model import DocType, ScanFinding
from .redact import parse_datetime

# ------------------------------------------------------------------ documents
DOC_KEYWORDS: list[tuple[DocType, list[str]]] = [
    (DocType.DR_TEST, ["dr test", "disaster recovery test", "failover test", "dr exercise", "recovery test results"]),
    (DocType.DR_PLAN, ["disaster recovery plan", "dr plan", "business continuity plan", "continuity plan"]),
    (DocType.LOAD_TEST, ["load test", "performance test", "stress test", "p95", "p99", "requests per second", "rps"]),
    (DocType.ERROR_BUDGET, ["error budget policy", "error budget review", "burn rate"]),
    (DocType.SLO, ["service level objective", "slo", "sli"]),
    (DocType.CAPACITY, ["capacity plan", "capacity forecast", "headroom", "growth forecast"]),
    (DocType.POSTMORTEM, ["postmortem", "post-mortem", "post incident review", "root cause analysis", "5 whys"]),
    (DocType.ARCHITECTURE, ["architecture", "failure mode", "risk storming", "single point of failure", "spof"]),
    (DocType.RUNBOOK, ["runbook", "playbook", "procedure", "recovery steps", "sop"]),
    (DocType.OBSERVABILITY, ["dashboard", "alerting", "monitoring", "observability", "structured log"]),
    (DocType.SECURITY, ["penetration test", "vulnerability", "access review", "security assessment"]),
]

REVIEW_DATE = re.compile(
    r"(?:last\s+reviewed|review(?:ed)?\s+(?:date|on)|last\s+updated|date\s+reviewed|revision\s+date|updated)"
    r"\s*[:|\-–]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{2}-\d{2}-\d{4}|\d{1,2}-[A-Za-z]{3}-\d{4})", re.I)


def classify_document(name: str, text: str) -> DocType:
    low_name = name.lower().replace("_", " ").replace("-", " ")
    head = text[:4000].lower()
    best, best_score = DocType.OTHER, 0
    for dt, words in DOC_KEYWORDS:
        score = sum(3 for w in words if w in low_name) + sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", head))
        if score > best_score:
            best, best_score = dt, score
    return best


def review_date(text: str) -> datetime | None:
    dates = [parse_datetime(m.group(1)) for m in REVIEW_DATE.finditer(text)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
O_OLE = "{urn:schemas-microsoft-com:office:office}OLEObject"
VECTOR_EXT = (".emf", ".wmf", ".svg")   # drawings OCR cannot read without a renderer


class ImageReader:
    """OCRs the images inside one document, within limits so a picture-heavy file can't tie up the server:
    at most SRA_OCR_MAX_IMAGES images (default 20) and SRA_OCR_MAX_SECONDS of OCR (default 60; a dense diagram
    with ~150 labels takes ~30 s on CPU), icons and logos skipped by size, duplicates read once.
    SRA_OCR=false turns image reading off (images are then only noted)."""

    MIN_W, MIN_H, MIN_BYTES = 120, 60, 2048

    def __init__(self):
        from ..config import flag, setting
        self.enabled = flag("SRA_OCR", True)
        self.left = int(setting("SRA_OCR_MAX_IMAGES", "20") or 20)
        self.budget = float(setting("SRA_OCR_MAX_SECONDS", "60") or 60)
        self.spent = 0.0
        self.out_of_time = False
        self.seen: set[str] = set()
        self.stats = {"images_read": 0, "images_small": 0, "images_over_limit": 0, "images_unreadable": 0}

    def section(self, name: str, data: bytes) -> str | None:
        """The text block for one embedded image, or None when it is skipped (small or a duplicate)."""
        import hashlib
        h = hashlib.sha256(data).hexdigest()
        if h in self.seen:
            return None
        self.seen.add(h)
        if name.lower().endswith(VECTOR_EXT):
            self.stats["images_unreadable"] += 1
            return f"[Image {name}: vector drawing (EMF/WMF/SVG), its text could not be read]"
        img = _decode(data)
        if img is None:
            self.stats["images_unreadable"] += 1
            return f"[Image {name}: format could not be read]"
        if len(data) < self.MIN_BYTES or img.width < self.MIN_W or img.height < self.MIN_H:
            self.stats["images_small"] += 1
            return None
        if self.spent >= self.budget:
            self.out_of_time = True
        if not self.enabled or self.left <= 0 or self.out_of_time:
            self.stats["images_over_limit"] += 1
            return f"[Image {name}: not read{'' if self.enabled else ' (OCR is off)'}]"
        import time
        self.left -= 1
        self.stats["images_read"] += 1
        buf = io.BytesIO()
        img.save(buf, "PNG")
        t = time.monotonic()
        text = image_to_text(buf.getvalue())
        self.spent += time.monotonic() - t
        return f"[Image {name}]\n{text}\n[End of image]" if text.strip() else f"[Image {name}: no text found]"

    def meta(self) -> dict:
        n = self.stats
        out = {k: v for k, v in n.items() if v}
        notes = []
        if n["images_over_limit"]:
            why = ("OCR is off (SRA_OCR)" if not self.enabled
                   else "the per-document OCR time limit was reached (SRA_OCR_MAX_SECONDS)" if self.out_of_time
                   else "over the per-document limit (SRA_OCR_MAX_IMAGES)")
            notes.append(f"{n['images_over_limit']} image(s) not read: {why}")
        if n["images_unreadable"]:
            notes.append(f"{n['images_unreadable']} image(s) in a format OCR cannot read (e.g. EMF/WMF drawings)")
        if notes:
            out["image_notes"] = notes
        return out


def _decode(data: bytes):
    """A Pillow RGB image, or None. Handles PNG/JPEG/GIF/TIFF/BMP/WebP, CMYK and palette images."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _rels(z: zipfile.ZipFile, part: str = "word/document.xml") -> dict[str, tuple[str, bool]]:
    """Relationship id -> (zip path or URL, is external) for one document part."""
    from lxml import etree
    folder, base = part.rsplit("/", 1)
    rel_path = f"{folder}/_rels/{base}.rels"
    if rel_path not in z.namelist():
        return {}
    out = {}
    for r in etree.fromstring(z.read(rel_path)):
        target, external = r.get("Target", ""), r.get("TargetMode") == "External"
        if not external:
            path = target[1:] if target.startswith("/") else f"{folder}/{target}"
            parts: list[str] = []
            for seg in path.split("/"):
                if seg == "..":
                    if parts:
                        parts.pop()
                elif seg and seg != ".":
                    parts.append(seg)
            target = "/".join(parts)
        out[r.get("Id")] = (target, external)
    return out


def docx_to_text(data: bytes) -> tuple[str, datetime | None, dict]:
    """Text of a .docx in reading order, with the text of embedded pictures (OCR) placed where they appear.
    Returns (text, last modified, meta with image counts and notes)."""
    images = ImageReader()
    objects = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        blocks = body_blocks(z.read("word/document.xml"))
        rels = _rels(z)
        modified = None
        if "docProps/core.xml" in names:
            m = re.search(rb"<dcterms:modified[^>]*>([^<]+)<", z.read("docProps/core.xml"))
            modified = parse_datetime(m.group(1).decode()) if m else None
        lines = []
        for b in blocks:
            if isinstance(b, Para):
                if b.text:
                    lines.append(b.text)
            elif isinstance(b, Table):
                lines.extend(" | ".join(r) for r in b.rows)
            if b.el is None:
                continue
            for el in b.el.iter(A_BLIP, V_IMAGEDATA, O_OLE):
                if el.tag == O_OLE:
                    objects += 1
                    prog = el.get("ProgID") or "object"
                    hint = " (upload the .vsdx or .drawio file to have the diagram checked)" if "visio" in prog.lower() else ""
                    lines.append(f"[Embedded {prog}: its content is not read{hint}]")
                    continue
                target, external = rels.get(el.get(R_ID + "embed") or el.get(R_ID + "id"), ("", True))
                if external or target not in names:
                    continue
                sec = images.section(target.rsplit("/", 1)[-1], z.read(target))
                if sec:
                    lines.append(sec)
    meta = images.meta()
    if objects:
        meta["embedded_objects"] = objects
    return "\n".join(lines), modified, meta


def pdf_to_text(data: bytes) -> tuple[str, dict]:
    """Text of a PDF page by page, with the text of each page's images (OCR) after that page's text. A scanned
    PDF (pages that are only images) is therefore read entirely by OCR, within the same image limit."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    images = ImageReader()
    out = []
    for n, page in enumerate(reader.pages, 1):
        out.append(page.extract_text() or "")
        try:
            page_images = list(page.images)
        except Exception:                      # unusual image encodings; the page text is still kept
            page_images = []
            images.stats["images_unreadable"] += 1
        for img in page_images:
            sec = images.section(f"page {n} {img.name}", img.data)
            if sec:
                out.append(sec)
    return "\n".join(t for t in out if t), images.meta()


_OCR = None


def image_to_text(data: bytes, min_confidence: float = 0.5) -> str:
    """Text in an image (screenshot, photo of a whiteboard, scanned page) with RapidOCR, an ONNX model that runs
    on CPU and ships its weights in the package (nothing is downloaded at run time). Lines are returned top to
    bottom; low-confidence fragments are dropped."""
    import hashlib
    key = hashlib.sha256(data + f"|{min_confidence}|{OCR_VERSION}".encode()).hexdigest()
    cached = _ocr_cache_get(key)
    if cached is not None:
        return cached
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    result, _ = _OCR(data)
    lines = sorted((r for r in result or [] if r[2] >= min_confidence), key=lambda r: (r[0][0][1], r[0][0][0]))
    text = "\n".join(r[1] for r in lines)
    _ocr_cache_put(key, text)
    return text


# Evidence is re-parsed whenever a bundle is built (each run, the evidence list, chat), so OCR text is cached by
# image hash: in memory, and on disk under SRA_DATA_DIR/ocr_cache so it survives restarts. Bump OCR_VERSION when
# the OCR model or post-processing changes.
OCR_VERSION = "1"
_MEM: dict[str, str] = {}
_MEM_MAX = 512


def _ocr_cache_dir():
    from ..config import DATA_DIR
    return DATA_DIR / "ocr_cache"


def _ocr_cache_get(key: str) -> str | None:
    if key in _MEM:
        return _MEM[key]
    try:
        text = (_ocr_cache_dir() / f"{key}.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    _mem_put(key, text)
    return text


def _ocr_cache_put(key: str, text: str) -> None:
    _mem_put(key, text)
    try:
        d = _ocr_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(d / f"{key}.txt")
    except OSError:
        pass                                   # read-only disk: the memory cache still helps


def _mem_put(key: str, text: str) -> None:
    if len(_MEM) >= _MEM_MAX:
        _MEM.pop(next(iter(_MEM)))
    _MEM[key] = text


# ------------------------------------------------------------------ scanners
_SEV = {"critical": "Critical", "high": "High", "medium": "Medium", "moderate": "Medium", "low": "Low",
        "negligible": "Low", "info": "Low", "informational": "Low", "unknown": "Medium",
        "error": "High", "warning": "Medium", "note": "Low", "none": "Low"}


def _sev(value) -> str:
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value)):
        v = float(value)
        return "Critical" if v >= 9 else "High" if v >= 7 else "Medium" if v >= 4 else "Low"
    return _SEV.get(str(value).strip().lower(), "Medium")


def detect_scan_format(obj) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("runs"), list) and ("$schema" in obj or str(obj.get("version", "")).startswith("2.")):
            return "sarif"
        if obj.get("bomFormat") == "CycloneDX":
            return "cyclonedx"
        if "Results" in obj and ("ArtifactName" in obj or "SchemaVersion" in obj):
            return "trivy"
        if "matches" in obj and "source" in obj:
            return "grype"
    return None


def parse_scan(obj, fmt: str) -> tuple[list[ScanFinding], list[dict]]:
    """Return (findings, components). Components feed EOL detection."""
    findings: list[ScanFinding] = []
    components: list[dict] = []
    if fmt == "sarif":
        for run in obj.get("runs", []):
            tool = run.get("tool", {}).get("driver", {}).get("name", "sarif")
            rules = {r.get("id"): r for r in run.get("tool", {}).get("driver", {}).get("rules", [])}
            for res in run.get("results", []):
                rule = rules.get(res.get("ruleId"), {})
                sev = rule.get("properties", {}).get("security-severity") or res.get("level") or "warning"
                loc = ""
                if res.get("locations"):
                    pl = res["locations"][0].get("physicalLocation", {})
                    loc = f"{pl.get('artifactLocation', {}).get('uri', '')}:{pl.get('region', {}).get('startLine', '')}"
                findings.append(ScanFinding(tool=tool, rule_id=res.get("ruleId", ""),
                                            title=(res.get("message", {}).get("text") or rule.get("shortDescription", {}).get("text", ""))[:300],
                                            severity=_sev(sev), location=loc))
    elif fmt == "trivy":
        for r in obj.get("Results", []) or []:
            for v in r.get("Vulnerabilities", []) or []:
                findings.append(ScanFinding(tool="trivy", rule_id=v.get("VulnerabilityID", ""),
                                            title=(v.get("Title") or v.get("Description", ""))[:300],
                                            severity=_sev(v.get("Severity", "unknown")), package=v.get("PkgName", ""),
                                            installed_version=v.get("InstalledVersion", ""),
                                            fixed_version=v.get("FixedVersion", ""), location=r.get("Target", "")))
            for s in r.get("Secrets", []) or []:
                findings.append(ScanFinding(tool="trivy", rule_id=f"secret:{s.get('RuleID', '')}",
                                            title=s.get("Title", "Secret detected"), severity=_sev(s.get("Severity", "high")),
                                            location=f"{r.get('Target', '')}:{s.get('StartLine', '')}"))
            for p in r.get("Packages", []) or []:
                components.append({"name": p.get("Name", ""), "version": p.get("Version", "")})
    elif fmt == "cyclonedx":
        # the producing tool, when the BOM names one (CycloneDX 1.4 lists tools; 1.5+ nests them under components)
        tools = (obj.get("metadata") or {}).get("tools") or []
        tools = tools.get("components") or [] if isinstance(tools, dict) else tools
        tool = next((t.get("name") for t in tools if isinstance(t, dict) and t.get("name")), None) or "cyclonedx"
        comps = {c.get("bom-ref"): c for c in obj.get("components", [])}
        components = [{"name": c.get("name", ""), "version": c.get("version", "")} for c in comps.values()]
        for v in obj.get("vulnerabilities", []) or []:
            ratings = v.get("ratings") or [{}]
            sev = ratings[0].get("severity") or ratings[0].get("score") or "unknown"
            for aff in v.get("affects") or [{}]:
                c = comps.get(aff.get("ref"), {})
                findings.append(ScanFinding(tool=tool, rule_id=v.get("id", ""),
                                            title=(v.get("description") or v.get("detail") or "")[:300],
                                            severity=_sev(sev), package=c.get("name", aff.get("ref", "")),
                                            installed_version=c.get("version", ""),
                                            fixed_version=(v.get("recommendation") or "")[:100]))
    elif fmt == "grype":
        for m in obj.get("matches", []):
            v, art = m.get("vulnerability", {}), m.get("artifact", {})
            findings.append(ScanFinding(tool="grype", rule_id=v.get("id", ""), title=(v.get("description") or "")[:300],
                                        severity=_sev(v.get("severity", "unknown")), package=art.get("name", ""),
                                        installed_version=art.get("version", ""),
                                        fixed_version=", ".join(v.get("fix", {}).get("versions", []))))
            components.append({"name": art.get("name", ""), "version": art.get("version", "")})
    return findings, components


def parse_scan_csv(text: str) -> list[ScanFinding] | None:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    cols = {c.lower(): c for c in rows[0]}
    if "number" in cols or "sys_id" in cols:   # a ServiceNow export, not a scanner report
        return None
    idc =cols.get("cve") or cols.get("vulnerability") or cols.get("vulnerability id") or cols.get("id")
    sevc = cols.get("severity")
    if not (idc and sevc):
        return None
    pkg = cols.get("package") or cols.get("component") or cols.get("pkgname")
    return [ScanFinding(tool="csv", rule_id=r[idc], title=r.get(cols.get("title", ""), "") if cols.get("title") else "",
                        severity=_sev(r[sevc]), package=r.get(pkg, "") if pkg else "",
                        installed_version=r.get(cols.get("version", ""), "") if cols.get("version") else "",
                        fixed_version=r.get(cols.get("fixed version", ""), "") if cols.get("fixed version") else "")
            for r in rows]


# ------------------------------------------------------------------ kubernetes / CI
def parse_k8s(text: str) -> list[dict]:
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []
    out = []
    for d in docs:
        if d.get("kind") == "List":
            out.extend(i for i in d.get("items", []) if isinstance(i, dict))
        elif "kind" in d and "apiVersion" in d:
            out.append(d)
    return out


CI_NAMES = re.compile(r"(^|/)(jenkinsfile|\.gitlab-ci\.ya?ml|azure-pipelines\.ya?ml|cloudbuild\.ya?ml|ci\.ya?ml)$"
                      r"|\.github/workflows/", re.I)


def is_ci_config(name: str, text: str) -> bool:
    if CI_NAMES.search(name.replace("\\", "/")):
        return True
    return bool(re.search(r"^\s*(on|jobs):", text, re.M) and "runs-on" in text) or text.lstrip().startswith("pipeline {")
