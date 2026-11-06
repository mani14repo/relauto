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
from ..models import Assessment, CriterionResult, Finding, Source
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
    styled = next((r for r in p.findall(W + "r") if r.find(W + "rPr") is not None), None)
    rpr = copy.deepcopy(styled.find(W + "rPr")) if styled is not None else None
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
    status_row = {"A": "Approval Status", "B": "Status", "C": "Version / Status"}[template.variant]
    template_status = _cell(meta, status_row)
    _set_row(meta, status_row, f"Assessment status: {assessment.status.upper()}"
             + (f" (template {template_status})" if template.variant == "C" and template_status else ""))

    by_crit: dict[str, list[Finding]] = {}
    for f in assessment.findings:
        for cid in f.criterion_ids:
            by_crit.setdefault(cid, []).append(f)
    if template.variant == "C":
        _fill_variant_c(template, tables, assessment, by_crit)
        return _write(root, parts, infos, tables, assessment, out)

    # Criterion tables, in template order
    crit_tables = [t for t in tables if _row(t, "Acceptance Criteria") is not None and _row(t, "Result") is not None]
    if len(crit_tables) != len(template.criteria):
        raise ValueError(f"{template.id}: {len(crit_tables)} criterion tables vs {len(template.criteria)} criteria")

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

    return _write(root, parts, infos, tables, assessment, out)


def _write(root, parts, infos, tables, assessment: Assessment, out: Path) -> Path:
    # Sign-off table
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


# ---------------------------------------------------------------- Variant C (CMDB/CSDM V2)
def result_text_c(result: Result | None) -> str:
    return "   ".join(f"{r.value}  {TICK if r == result else BOX}" for r in RESULT_ORDER)


def level_text(level: int | None) -> str:
    return "  ".join(f"L{i} {TICK if i == level else BOX}" for i in range(5))


def _fill_variant_c(template: Template, tables, assessment: Assessment, by_crit: dict[str, list[Finding]]) -> None:
    defs = [t for t in tables if _row(t, "Measurement / formula") is not None]
    results = [t for t in tables if _label(_rows(t)[0]) == "Measured value"]
    if not (len(defs) == len(results) == len(template.criteria)):
        raise ValueError(f"{template.id}: {len(defs)} criterion / {len(results)} result tables vs {len(template.criteria)} criteria")
    for tbl, c in zip(results, template.criteria):
        r = assessment.results.get(c.id)
        fs = by_crit.get(c.id, [])
        _set_row(tbl, "Measured value", (r.measured_value if r and r.measured_value else
                                         "[Pending evaluation]" if r is None or r.result is None else "Not measured"))
        _set_row(tbl, "Result", result_text_c(r.result if r else None))
        _set_row(tbl, "Maturity level", level_text(r.maturity_level if r else None)
                 + ("" if r and r.maturity_level is not None else "   (not yet assessed)"))
        _set_row(tbl, "Evidence reference", evidence_text(r, fs))
        _set_row(tbl, "Recommendation", recommendation_text(r, fs))
        if r and r.result is not None:
            who = r.reviewer or ("Pending reviewer validation" if r.source != Source.HUMAN else assessment.assessor or "Assessor")
            _set_row(tbl, "Validated by / date", f"{who} — {assessment.assessment_date.isoformat()}")

    # 8. Results Summary
    summary = next((t for t in tables if [c.strip() for c in _texts(_rows(t)[0])][:3] == ["Criterion", "Name", "Result"]), None)
    if summary is not None:
        by_num = {f"C{c.number:02d}": c for c in template.criteria}
        for tr in _rows(summary)[1:]:
            c = by_num.get(_label(tr))
            if not c:
                continue
            r = assessment.results.get(c.id)
            tcs = tr.findall(W + "tc")
            vals = [r.result.value if r and r.result else "Not evaluated",
                    f"L{r.maturity_level}" if r and r.maturity_level is not None else "—",
                    (r.measured_value if r else "") or "—",
                    (r.risk or c.default_risk).value if r and r.result in (Result.GAP, Result.INSUFFICIENT) else "—"]
            for tc, v in zip(tcs[2:6], vals):
                set_cell_text(tc, v)
    p1 = sorted({rid for c in template.criteria for rid in c.rubric_refs if rid.startswith("P1") and _gaplike(assessment, c.id)})
    p2 = sorted({rid for c in template.criteria for rid in c.rubric_refs if rid.startswith("P2") and _gaplike(assessment, c.id)})
    outcome = next((t for t in tables if _row(t, "Rubric outcome contribution") is not None), None)
    if outcome is not None:
        _set_row(outcome, "Rubric outcome contribution",
                 f"P1 gaps: {', '.join(p1) or 'none'}   P2 gaps: {', '.join(p2) or 'none'}")
        levels = [assessment.results[c.id].maturity_level for c in template.criteria
                  if c.id in assessment.results and assessment.results[c.id].maturity_level is not None]
        _set_row(outcome, "Assessment-level maturity",
                 f"L{min(levels)} (lowest of {len(levels)} assessed criteria)" if len(levels) == len(template.criteria)
                 else f"Not determined: {len(levels)} of {len(template.criteria)} criteria have a maturity level")

    # 9. Remediation Plan: one row per open finding on this template's criteria
    plan = next((t for t in tables if [c.strip() for c in _texts(_rows(t)[0])][:2] == ["Finding", "Severity"]), None)
    if plan is not None:
        ids = {c.id for c in template.criteria}
        seen, items = set(), []
        for f in assessment.findings:
            if f.id not in seen and ids & set(f.criterion_ids) and f.status.value != "resolved":
                seen.add(f.id)
                items.append(f)
        rows = _rows(plan)[1:]
        while len(rows) < len(items):
            clone = copy.deepcopy(rows[-1])
            rows[-1].addnext(clone)
            rows.append(clone)
        for tr, f in zip(rows, items):
            rec = f.recommendation
            vals = [f"{f.id}: {f.title}", f.severity.value, (rec.owner if rec else "") or "",
                    rec.due_date.isoformat() if rec and rec.due_date else "",
                    ", ".join(rec.playbooks) if rec and rec.playbooks else "", f.status.value]
            for tc, v in zip(tr.findall(W + "tc"), vals):
                set_cell_text(tc, v)


def _texts(tr) -> list[str]:
    return [text_of(tc) for tc in tr.findall(W + "tc")]


def _gaplike(a: Assessment, cid: str) -> bool:
    r = a.results.get(cid)
    return r is not None and r.result in (Result.GAP, Result.INSUFFICIENT)


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
