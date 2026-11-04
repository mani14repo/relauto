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


def parse_template(path: str | Path) -> Template:
    path = Path(path)
    blocks = docx_blocks(path)
    m = ID_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot find template ID in file name: {path.name}")
    tid = m.group(1)
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

    domain = Domain(meta.get("Domain", tid.split("-")[0]).strip())
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
    return template
