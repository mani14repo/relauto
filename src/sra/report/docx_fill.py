"""Fill an OIPReqs assessment template in place, preserving its direct formatting.

Works on a copy of the original .docx: edits word/document.xml with lxml and re-zips every
other part unchanged. Handles both template layouts (Variant A and B).

What is written per criterion table:
  Result               Variant A: chosen option ticked (U+2612), others left as U+2610.
                       Variant B: the chosen option only.
  Risk Classification  "<Risk> — <rationale>" for Gap / Insufficient Evidence, else "Not applicable (…)".
  Recommendation       assessment recommendation; never the template's example text.
  Accountable Owner    kept unless the assessment overrides it.
  Validation (B)       reviewer, decision and evidence.
  + a new row "Assessment Evidence & Rationale" (cloned row formatting) after Result.
Plus: Application / Assessment Reference / Assessor rows in the metadata table, the sign-off table,
and a fix for the invisible "Criterion" header label in Variant A (navy text on navy fill).
"""
from __future__ import annotations

import copy
import zipfile
from pathlib import Path

from lxml import etree

from ..catalog.models import Result, Risk, Template
from ..models import Assessment, CriterionResult, Finding
from ..ooxml import W, text_of

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
BOX, TICK = "☐", "☒"
RESULT_ORDER = [Result.MET, Result.GAP, Result.INSUFFICIENT, Result.NA]
EVIDENCE_LABEL = "Assessment Evidence & Rationale"


# ---------------------------------------------------------------- low-level cell helpers
def set_cell_text(tc, text: str) -> None:
    """Replace a cell's text, keeping the first paragraph's pPr and first run's rPr."""
    paras = tc.findall(W + "p")
    p = paras[0]
    for extra in paras[1:]:
        tc.remove(extra)
    runs = p.findall(W + "r")
    rpr = copy.deepcopy(runs[0].find(W + "rPr")) if runs and runs[0].find(W + "rPr") is not None else None
    for child in list(p):
        if child.tag != W + "pPr":
            p.remove(child)
    lines = (text or "").split("\n")
    r = etree.SubElement(p, W + "r")
    if rpr is not None:
        r.append(rpr)
    for i, line in enumerate(lines):
        if i:
            etree.SubElement(r, W + "br")
        t = etree.SubElement(r, W + "t")
        t.set(XML_SPACE, "preserve")
        t.text = line


def _rows(tbl):
    return tbl.findall(W + "tr")


def _label(tr) -> str:
    tcs = tr.findall(W + "tc")
    return text_of(tcs[0]).strip() if tcs else ""


def _row(tbl, label: str):
    return next((tr for tr in _rows(tbl) if _label(tr) == label), None)


def _set_row(tbl, label: str, value: str) -> bool:
    tr = _row(tbl, label)
    if tr is None:
        return False
    set_cell_text(tr.findall(W + "tc")[1], value)
    return True


def _insert_row_after(tbl, after_label: str, label: str, value: str, like_label: str | None = None):
    """Insert a new 2-column row after `after_label`, cloned from `like_label` (or after_label)."""
    anchor = _row(tbl, after_label)
    proto = _row(tbl, like_label) if like_label else anchor
    if anchor is None or proto is None:
        return None
    existing = _row(tbl, label)
    if existing is not None:
        set_cell_text(existing.findall(W + "tc")[1], value)
        return existing
    new = copy.deepcopy(proto)
    tcs = new.findall(W + "tc")
    set_cell_text(tcs[0], label)
    set_cell_text(tcs[1], value)
    anchor.addnext(new)
    return new


def _recolor_label(tr, color: str = "FFFFFF") -> None:
    tc = tr.findall(W + "tc")[0]
    for c in tc.iter(W + "color"):
        c.set(W + "val", color)


# ---------------------------------------------------------------- value formatting
def result_text(variant: str, result: Result | None) -> str:
    if variant == "A":
        parts = [f"{r.value}  {TICK if r == result else BOX}" for r in RESULT_ORDER]
        return "   |   ".join(parts)
    return result.value if result else "Met  /  Gap  /  Insufficient Evidence  /  Not Applicable"


def risk_text(r: CriterionResult | None, default_risk: Risk) -> str:
    if r is None or r.result is None:
        return "Not yet evaluated"
    if r.result in (Result.GAP, Result.INSUFFICIENT):
        risk = (r.risk or default_risk).value
        why = "insufficient evidence is treated as a gap" if r.result == Result.INSUFFICIENT else ""
        return f"{risk} — {why}" if why else risk
    if r.result == Result.NA:
        return "Not applicable — " + (r.na_justification or "justification required")
    return "Not applicable — criterion met"


def recommendation_text(r: CriterionResult | None, findings: list[Finding]) -> str:
    if r is None or r.result is None:
        return "[Pending evaluation]"
    if r.result == Result.MET:
        return r.recommendation or "No corrective action required — criterion met. Maintain current controls."
    if r.result == Result.NA:
        status = "approved" if r.na_approved else "PENDING APPROVAL"
        return f"Not Applicable ({status}): {r.na_justification or '[justification required]'}"
    lines = [r.recommendation] if r.recommendation else []
    for f in findings:
        if f.recommendation and f.recommendation.action not in lines:
            rec = f.recommendation
            ref = f" (see {', '.join(rec.playbooks)})" if rec.playbooks else ""
            due = f"; due {rec.due_date.isoformat()}" if rec.due_date else ""
            lines.append(f"• {rec.action}{ref}{due}")
    return "\n".join(lines) or "[Recommendation required for this gap]"


def evidence_text(r: CriterionResult | None, findings: list[Finding]) -> str:
    if r is None or r.result is None:
        return "[Pending evaluation]"
    out = []
    if r.rationale:
        out.append(r.rationale)
    if r.evidence_refs:
        out.append("Evidence: " + "; ".join(r.evidence_refs))
    for f in findings:
        out.append(f"Finding {f.id} [{f.type.value}, {f.severity.value}]: {f.title}")
    src = f"Evaluated by: {r.source.value}" if r.source else ""
    if r.reviewer:
        src += f"; reviewed by {r.reviewer}"
    if r.needs_human_review:
        src += "; HUMAN REVIEW REQUIRED"
    if src:
        out.append(src)
    return "\n".join(out) or "No evidence recorded."


# ---------------------------------------------------------------- main entry
def fill_template(template: Template, src: Path, assessment: Assessment, out: Path,
                  owners: dict[str, str] | None = None) -> Path:
    owners = owners or {}
    with zipfile.ZipFile(src) as z:
        parts = {n: z.read(n) for n in z.namelist()}
        infos = {i.filename: i for i in z.infolist()}
    root = etree.fromstring(parts["word/document.xml"])
    body = root.find(W + "body")
    tables = body.findall(W + "tbl")

    # Metadata table (first table)
    meta = tables[0]
    id_label = "Assessment ID"
    _insert_row_after(meta, id_label, "Assessment Reference", assessment.id)
    _insert_row_after(meta, id_label, "Application",
                      assessment.application.name + (f" ({assessment.application.cmdb_id})" if assessment.application.cmdb_id else ""))
    _insert_row_after(meta, "Application", "Assessor", assessment.assessor or "[Assessor]")
    _set_row(meta, "Date", f"{assessment.assessment_date.isoformat()} (template: {_cell(meta, 'Date')})")
    if template.variant == "A":
        _set_row(meta, "Approval Status", f"Assessment status: {assessment.status.upper()}")
    else:
        _set_row(meta, "Status", f"Assessment status: {assessment.status.upper()}")

    # Criterion tables, in template order
    crit_tables = [t for t in tables if _row(t, "Acceptance Criteria") is not None and _row(t, "Result") is not None]
    if len(crit_tables) != len(template.criteria):
        raise ValueError(f"{template.id}: {len(crit_tables)} criterion tables vs {len(template.criteria)} criteria")
    by_crit: dict[str, list[Finding]] = {}
    for f in assessment.findings:
        for cid in f.criterion_ids:
            by_crit.setdefault(cid, []).append(f)

    for tbl, c in zip(crit_tables, template.criteria):
        r = assessment.results.get(c.id)
        fs = by_crit.get(c.id, [])
        if template.variant == "A":
            head = _rows(tbl)[0]
            if _label(head) == "Criterion":
                _recolor_label(head)
        _set_row(tbl, "Result", result_text(template.variant, r.result if r else None))
        _set_row(tbl, "Risk Classification", risk_text(r, c.default_risk) + _risk_detail(r))
        _set_row(tbl, "Recommendation", recommendation_text(r, fs))
        if c.id in owners:
            _set_row(tbl, "Accountable Owner", owners[c.id])
        if template.variant == "B":
            _set_row(tbl, "Validation", _validation(r))
        _insert_row_after(tbl, "Result", EVIDENCE_LABEL, evidence_text(r, fs), like_label="Required Evidence")

    # Sign-off table (Variant A)
    signoff = next((t for t in tables if _label(_rows(t)[0]) == "Role"), None)
    if signoff is not None:
        by_role = {s.role: s for s in assessment.signoffs}
        for tr in _rows(signoff)[1:]:
            s = by_role.get(_label(tr))
            if s:
                tcs = tr.findall(W + "tc")
                set_cell_text(tcs[1], s.name)
                set_cell_text(tcs[2], s.date.isoformat() if s.date else "")
                set_cell_text(tcs[3], s.decision)

    parts["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(infos[name], data, compress_type=zipfile.ZIP_DEFLATED)
    return out


def _cell(tbl, label: str) -> str:
    tr = _row(tbl, label)
    return text_of(tr.findall(W + "tc")[1]).strip() if tr is not None else ""


def _risk_detail(r: CriterionResult | None) -> str:
    if r and r.result == Result.GAP and r.rationale:
        first = r.rationale.split("\n")[0]
        return f" — {first[:200]}" if first else ""
    return ""


def _validation(r: CriterionResult | None) -> str:
    if r is None or r.result is None:
        return "[Reviewer name, decision, date, and supporting evidence]"
    who = r.reviewer or "Pending reviewer validation"
    ev = "; ".join(r.evidence_refs) or "none recorded"
    return r.validation or f"{who} — {r.result.value} ({r.source.value if r.source else 'unknown'}); evidence: {ev}"
