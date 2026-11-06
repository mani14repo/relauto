"""Parse an OIPReqs assessment template (.docx) into a catalog Template.

Two layouts exist in V1:
  Variant A (32 templates): 9-row metadata table, unnumbered criteria in 10-row tables
      whose first row is "Criterion | 1. Name"; Risk/Recommendation rows carry example text.
  Variant B (7 templates): numbered sections, criteria "<ID>-C01.." in 13-row tables
      with [bracket] placeholders.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..ooxml import Para, Table, docx_blocks
from .models import Criterion, Domain, Method, Risk, Template

REF_RE = re.compile(r"\b(?:SRE|ITSM|CMDB)-(?:STD|POL|PRC|PB|AT)-\d{2}\b")
ID_RE = re.compile(r"\b((?:SRE|ITSM|CMDB)-AT-\d{2})\b")
RISK_RE = re.compile(r"^\s*(Critical|High|Medium|Low)\b", re.I)

RUBRIC_RE = re.compile(r"\bP[12]-\d{2}\b")
VERSION_RE = re.compile(r"[—-]\s*(V\d+)\s*$", re.I)
SECTION_C = re.compile(r"^\d+[A-Z]?\.\s+(Document Control|Purpose|FSI REO Relevance|Scope & Assessment Unit|Data Pulls.*|"
                       r"How to Score|Assessment Criteria|Results Summary|Remediation Plan|Sign-Off|Version History.*|.+)$")
CRIT_HEAD_C = re.compile(r"^((?:SRE|ITSM|CMDB)-AT-\d{2}-C\d{2})\s*:\s*(.+)$")

HEADINGS_A = ("Purpose", "FSI REO Relevance", "How to Use This Template", "Assessment Criteria", "Assessment Sign-Off")
HEADING_B = re.compile(r"^\d\.\s+(Document Control|Purpose|Scope & Applicability|Assessment Criteria|Governance & Ownership)")


def _methods(text: str) -> list[Method]:
    low = text.lower()
    found = []
    for key, m in (("deterministic", Method.DETERMINISTIC), ("ai-assisted", Method.AI),
                   ("ai assisted", Method.AI), ("human review", Method.HUMAN)):
        pos = low.find(key)
        if pos >= 0 and m not in [f[1] for f in found]:
            found.append((pos, m))
    return [m for _, m in sorted(found)] or [Method.HUMAN]


def _refs(*texts: str) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for r in REF_RE.findall(t or ""):
            if r not in seen:
                seen.append(r)
    return seen


def _heading_a(text: str) -> str | None:
    for h in HEADINGS_A:
        if text == h or text.startswith(h + " "):
            return h
    return None


def _is_criterion_table(t: Table) -> bool:
    labels = set(t.labels)
    return "Acceptance Criteria" in labels and "Result" in labels


def _rubric_refs(text: str) -> tuple[list[str], list[str]]:
    """'P1-03, P1-05 (enabler)' -> (['P1-03'], ['P1-05']). Enablers support a rubric criterion but do not decide it."""
    direct, enablers = [], []
    for m in re.finditer(r"\b(P[12]-\d{2})\b(\s*\(enabler\))?", text or ""):
        (enablers if m.group(2) else direct).append(m.group(1))
    return direct, enablers


def file_version(name: str) -> str:
    """'... — V2.docx' -> 'V2'; V1 files carry no suffix."""
    m = VERSION_RE.search(Path(name).stem)
    return m.group(1).upper() if m else "V1"


def _domain(text: str, tid: str) -> Domain:
    """'CMDB / CSDM' -> CMDB; falls back to the ID prefix."""
    head = re.split(r"[\s/]", (text or "").strip(), maxsplit=1)[0]
    return Domain(head) if head in Domain.__members__ else Domain(tid.split("-")[0])


def parse_template(path: str | Path) -> Template:
    path = Path(path)
    blocks = docx_blocks(path)
    m = ID_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot find template ID in file name: {path.name}")
    tid = m.group(1)
    if any(isinstance(b, Table) and "Measurement / formula" in b.labels for b in blocks):
        return _parse_variant_c(path, tid, blocks)
    variant = "B" if any(isinstance(b, Table) and "Criterion ID" in b.labels for b in blocks) else "A"

    tables = [b for b in blocks if isinstance(b, Table)]
    meta = tables[0].kv() if tables else {}

    # Section text by heading
    sections: dict[str, list[str]] = {}
    current = None
    for b in blocks:
        if not isinstance(b, Para) or not b.text:
            continue
        head = _heading_a(b.text) if variant == "A" else (
            HEADING_B.match(b.text).group(1) if HEADING_B.match(b.text) else None)
        if head:
            current = head
            sections.setdefault(head, [])
        elif current:
            sections[current].append(b.text)

    if variant == "A":
        first = next(b.text for b in blocks if isinstance(b, Para) and b.text)
        title = first.split("|", 1)[1].strip() if "|" in first else first
    else:
        title = meta.get("Title") or tid

    criteria: list[Criterion] = []
    for t in tables:
        if not _is_criterion_table(t):
            continue
        kv = t.kv()
        n = len(criteria) + 1
        if variant == "A":
            raw_name = kv.get("Criterion", "")
            num = re.match(r"^\s*(\d+)\.\s*(.*)$", raw_name)
            if num:
                n, name = int(num.group(1)), num.group(2).strip()
            else:
                name = raw_name
            cid = f"{tid}-C{n:02d}"
        else:
            cid = kv.get("Criterion ID") or f"{tid}-C{n:02d}"
            name = kv.get("Criterion Name", "")
            cm = re.search(r"-C(\d+)$", cid)
            n = int(cm.group(1)) if cm else n
        risk_text = kv.get("Risk Classification", "")
        rm = RISK_RE.match(risk_text)
        is_placeholder = "/" in risk_text.split("—")[0] if risk_text else True
        default_risk = Risk(rm.group(1).capitalize()) if rm and not is_placeholder else Risk.HIGH
        rec = kv.get("Recommendation", "")
        criteria.append(Criterion(
            id=cid,
            number=n,
            name=name,
            standard_ref=kv.get("Standard Reference", ""),
            applicability=kv.get("Applicability", ""),
            required_evidence=kv.get("Required Evidence", ""),
            evaluation_method=kv.get("Evaluation Method", ""),
            methods=_methods(kv.get("Evaluation Method", "")),
            acceptance_criteria=kv.get("Acceptance Criteria", ""),
            default_risk=default_risk,
            default_risk_assumed=bool(is_placeholder or not rm),
            accountable_owner=kv.get("Accountable Owner", ""),
            reassessment=kv.get("Reassessment", ""),
            example_risk="" if is_placeholder else risk_text,
            example_recommendation="" if rec.startswith("[") else rec,
            references=_refs(kv.get("Standard Reference", ""), kv.get("Evaluation Method", ""),
                             kv.get("Required Evidence", "")),
        ))

    signoff = next((t for t in tables if t.rows and t.rows[0][:1] == ["Role"]), None)
    governance = next((t.kv() for t in tables if "Assessment Owner" in t.labels), {})

    domain = _domain(meta.get("Domain", ""), tid)
    template = Template(
        id=tid,
        title=title,
        domain=domain,
        variant=variant,
        priority=meta.get("Priority", ""),
        applicability=meta.get("Applicability", "") or " ".join(sections.get("Scope & Applicability", [])[1:]),
        accountable_owner=meta.get("Accountable Owner", "") or governance.get("Assessment Owner", ""),
        purpose=" ".join(sections.get("Purpose", [])),
        relevance=" ".join(sections.get("FSI REO Relevance", [])),
        reassessment_trigger=meta.get("Reassessment Trigger", ""),
        scope=sections.get("Scope & Applicability", [])[1:] if variant == "B" else [],
        signoff_roles=[r[0] for r in signoff.rows[1:]] if signoff else [],
        criteria=criteria,
        source_file=path.name,
    )
    template.references = _refs(*(c.standard_ref for c in criteria), template.purpose, template.relevance,
                                *(" ".join(r) for t in tables for r in t.rows))
    template.references = [r for r in template.references if r != tid]
    template.version = file_version(path.name)
    return template


# ---------------------------------------------------------------- Variant C (CMDB/CSDM V2)
def _parse_variant_c(path: Path, tid: str, blocks: list) -> Template:
    """V2 layout: a 'Document Control' key/value table; per criterion a heading paragraph
    '<ID>-Cnn: Name', a 14-row definition table (criterion ID, rubric criterion, formula, acceptance,
    maturity bands, ...) and a 6-row result table (Measured value, Result, Maturity level, ...)."""
    tables = [b for b in blocks if isinstance(b, Table)]
    meta = next((t.kv() for t in tables if "Assessment ID" in t.labels), {})
    sections: dict[str, list[str]] = {}
    current, heading = None, ""
    criteria: list[Criterion] = []
    for b in blocks:
        if isinstance(b, Para):
            if not b.text:
                continue
            hm = SECTION_C.match(b.text)
            cm = CRIT_HEAD_C.match(b.text)
            if cm:
                heading = cm.group(2).strip()
                current = None
            elif hm:
                current = hm.group(1).strip()
                sections.setdefault(current, [])
            elif current:
                sections[current].append(b.text)
            continue
        if "Measurement / formula" not in b.labels:
            continue
        kv = b.kv()
        cid = kv.get("Criterion ID", "").strip()
        num = re.search(r"-C(\d+)$", cid)
        risk_text = kv.get("Risk if Gap", "")
        rm = RISK_RE.match(risk_text)
        method = kv.get("Evaluation method", "")
        playbooks = REF_RE.findall(kv.get("Remediation playbook", ""))
        criteria.append(Criterion(
            id=cid,
            number=int(num.group(1)) if num else len(criteria) + 1,
            name=heading or cid,
            standard_ref=kv.get("Standard reference", ""),
            applicability=kv.get("Applicability", ""),
            required_evidence=kv.get("Required evidence", ""),
            evaluation_method=method,
            methods=_methods(method),
            acceptance_criteria=kv.get("Acceptance criteria", ""),
            default_risk=Risk(rm.group(1).capitalize()) if rm else Risk.HIGH,
            default_risk_assumed=not rm,
            rubric_refs=_rubric_refs(kv.get("Rubric criterion", ""))[0],
            rubric_enablers=_rubric_refs(kv.get("Rubric criterion", ""))[1],
            csdm_layer=kv.get("CSDM layer", ""),
            data_source=kv.get("Data source", ""),
            formula=kv.get("Measurement / formula", ""),
            maturity_bands=kv.get("Maturity bands", ""),
            look_for=kv.get("What to look for", ""),
            risk_if_gap=risk_text,
            playbooks=playbooks,
            references=_refs(kv.get("Standard reference", ""), kv.get("Remediation playbook", "")),
        ))
        heading = ""

    scope = next((t.kv() for t in tables if "In scope" in t.labels), {})
    signoff = next((t for t in tables if t.rows and t.rows[0][:1] == ["Role"]), None)
    title = re.sub(r"\s*\(NEW\)\s*$", "", meta.get("Title") or tid)
    template = Template(
        id=tid,
        title=title,
        domain=_domain(meta.get("Domain", ""), tid),
        variant="C",
        priority=meta.get("Priority", "").strip(),
        applicability=" ".join(filter(None, [meta.get("Assessment unit", ""), scope.get("In scope", "")])),
        accountable_owner=meta.get("Assessment owner", ""),
        purpose=" ".join(sections.get("Purpose", [])),
        relevance=" ".join(sections.get("FSI REO Relevance", [])),
        reassessment_trigger=meta.get("Cadence / reassessment", ""),
        scope=[s for s in (scope.get("In scope", ""), scope.get("Out of scope", "")) if s],
        signoff_roles=[r[0] for r in signoff.rows[1:]] if signoff else [],
        criteria=criteria,
        source_file=path.name,
        version=file_version(path.name),
        rubric_refs=RUBRIC_RE.findall(meta.get("Readiness Rubric criteria fed", "")),
    )
    template.references = [r for r in _refs(*(c.standard_ref for c in criteria), *(" ".join(c.playbooks) for c in criteria))
                           if r != tid]
    return template
