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


def docx_to_text(data: bytes) -> tuple[str, datetime | None]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        blocks = body_blocks(z.read("word/document.xml"))
        modified = None
        if "docProps/core.xml" in z.namelist():
            m = re.search(rb"<dcterms:modified[^>]*>([^<]+)<", z.read("docProps/core.xml"))
            modified = parse_datetime(m.group(1).decode()) if m else None
    lines = []
    for b in blocks:
        if isinstance(b, Para):
            if b.text:
                lines.append(b.text)
        elif isinstance(b, Table):
            lines.extend(" | ".join(r) for r in b.rows)
    return "\n".join(lines), modified


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


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
        comps = {c.get("bom-ref"): c for c in obj.get("components", [])}
        components = [{"name": c.get("name", ""), "version": c.get("version", "")} for c in comps.values()]
        for v in obj.get("vulnerabilities", []) or []:
            ratings = v.get("ratings") or [{}]
            sev = ratings[0].get("severity") or ratings[0].get("score") or "unknown"
            for aff in v.get("affects") or [{}]:
                c = comps.get(aff.get("ref"), {})
                findings.append(ScanFinding(tool="cyclonedx", rule_id=v.get("id", ""),
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
