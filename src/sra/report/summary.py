"""Assessment Summary Report (.docx): the rubric's Section 8 output plus scores, findings and plan.

Styled to match the OIPReqs Variant A palette (navy 1B2A4A, light E8EEF6).
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from ..catalog.models import Rubric, Template
from ..models import Assessment, FindingStatus, ScoreCard

NAVY = "1B2A4A"
LIGHT = "E8EEF6"
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def _shade(cell, fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _bar(doc, text: str, size: int = 11, fill: str = LIGHT, color: str = NAVY) -> None:
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    pPr.append(shd)
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)


def _table(doc, header: list[str], rows: list[list[str]], widths: list[float] | None = None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        run = c.paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _shade(c, NAVY)
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(v))
            run.font.size = Pt(9)
            if ri % 2:
                _shade(cells[i], "F5F5F5")
    if widths:
        from docx.shared import Inches
        t.autofit = False
        for i, w in enumerate(widths):
            t.columns[i].width = Inches(w)
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Inches(w)
    doc.add_paragraph()
    return t


def _kv(doc, pairs: list[tuple[str, str]]):
    t = doc.add_table(rows=0, cols=2)
    t.style = "Table Grid"
    for k, v in pairs:
        cells = t.add_row().cells
        cells[0].text = ""
        r = cells[0].paragraphs[0].add_run(k)
        r.bold = True
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor.from_string(NAVY)
        _shade(cells[0], LIGHT)
        cells[1].text = ""
        cells[1].paragraphs[0].add_run(v).font.size = Pt(9)
    doc.add_paragraph()


def build_summary(a: Assessment, sc: ScoreCard, templates: dict[str, Template], rubric: Rubric,
                  matrix: dict, out: Path) -> Path:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)

    _bar(doc, f"SRA  |  Reliability Assessment Summary — {a.application.name}", size=14, fill=NAVY, color="FFFFFF")
    doc.add_paragraph(f"Assessment {a.id}  |  {a.assessment_date.isoformat()}  |  Status: {a.status.upper()}  |  "
                      f"Scoring matrix {sc.matrix_version} ({matrix.get('status', '')})")

    _bar(doc, "1. Overall Result")
    _kv(doc, [
        ("Application", a.application.name + (f" ({a.application.cmdb_id})" if a.application.cmdb_id else "")),
        ("Assessment Date", f"{a.assessment_date.isoformat()} — rubric {rubric.version}"),
        ("Overall Rating", f"{sc.rating} — {sc.rating_outcome}"),
        ("Reliability Readiness Index", "n/a" if sc.index is None else f"{sc.index} / 100"),
        ("Maturity Level", "n/a" if sc.maturity_level is None else
            f"Level {sc.maturity_level} — {sc.maturity_name}" + (" (capped by rating)" if sc.maturity_capped else "")),
        ("P1 Gaps", f"{len(sc.p1_gaps)}: {', '.join(sc.p1_gaps) or 'none'}"),
        ("P2 Gaps", f"{len(sc.p2_gaps)}: {', '.join(sc.p2_gaps) or 'none'}"),
        ("Met", str(len(sc.met))),
        ("Not Applicable", f"{len(sc.not_applicable)}: {', '.join(sc.not_applicable) or 'none'}"),
        ("Insufficient Evidence", f"{len(sc.insufficient)}: {', '.join(sc.insufficient[:40]) or 'none'}"
            + (" …" if len(sc.insufficient) > 40 else "")),
        ("Domain Scores", ", ".join(f"{d.value} {v}" for d, v in sc.domain_scores.items()) or "n/a"),
        ("Evidence Coverage", f"{sc.evidence_coverage_pct}% of scorable criteria have direct evidence"),
        ("Templates Applied", ", ".join(a.templates)),
        ("Reassessment Trigger", "; ".join(sorted({templates[t].reassessment_trigger for t in a.templates
                                                   if templates[t].reassessment_trigger})) or
            "Major release, architecture change, or Post-Incident Review recommendation"),
    ])

    _bar(doc, "2. Rubric Criteria (P1 blocking / P2 remediation)")
    _table(doc, ["ID", "Criterion", "Domain", "Result", "Based on"],
           [[o.rubric_id, o.name, o.domain.value, o.result.value, ", ".join(o.contributing) or "no evidence"]
            for o in sc.rubric], widths=[0.6, 2.8, 0.6, 1.1, 1.9])

    _bar(doc, "3. Template Scores")
    _table(doc, ["Template", "Title", "Score", "Met", "Gap", "Insuff.", "N/A"],
           [[s.template_id, templates[s.template_id].title, "n/a" if s.score is None else f"{s.score}",
             s.met, s.gaps, s.insufficient, s.na] for s in sc.template_scores],
           widths=[0.9, 3.1, 0.6, 0.45, 0.45, 0.6, 0.45])

    findings = sorted([f for f in a.findings if f.status != FindingStatus.DISPUTED],
                      key=lambda f: (SEVERITY_ORDER[f.severity.value], f.type.value))
    _bar(doc, f"4. Findings Register ({len(findings)})")
    _table(doc, ["ID", "Type", "Severity", "Finding", "Criteria", "Detector"],
           [[f.id, f.type.value, f.severity.value, f.title, ", ".join(f.criterion_ids + f.rubric_ids), f.detector]
            for f in findings], widths=[0.8, 0.9, 0.7, 2.6, 1.3, 0.9])

    _bar(doc, "5. Recommended Actions & Accountable Owners")
    _table(doc, ["Finding", "Action", "Playbook", "Owner", "Due"],
           [[f.id, f.recommendation.action, ", ".join(f.recommendation.playbooks + f.recommendation.playbook_steps),
             f.recommendation.owner, f.recommendation.due_date.isoformat() if f.recommendation.due_date else ""]
            for f in findings if f.recommendation], widths=[0.8, 3.0, 1.2, 1.0, 0.8])

    disputed = [f for f in a.findings if f.status == FindingStatus.DISPUTED]
    if disputed:
        _bar(doc, "6. Disputed Findings (excluded above)")
        _table(doc, ["ID", "Finding", "Detector"], [[f.id, f.title, f.detector] for f in disputed])

    _bar(doc, "7. Detector Coverage")
    _table(doc, ["Detector", "Status"], [[k, v] for k, v in sorted(a.detector_runs.items())] or [["—", "none run"]])

    if sc.conflicts:
        _bar(doc, "8. Threshold Conflicts Between Template and Rubric")
        for c in sc.conflicts:
            doc.add_paragraph(c, style="List Bullet")

    _bar(doc, "Sign-Off")
    _table(doc, ["Role", "Name", "Date", "Decision"],
           [[s.role, s.name, s.date.isoformat() if s.date else "", s.decision] for s in a.signoffs]
           or [[r, "", "", ""] for r in ("Assessment Lead", "SRE Reviewer", "Engineering Owner", "PSE Representative")])
    p = doc.add_paragraph("Generated by the Site Reliability Assessment Bot. AI-assisted results are verified against "
                          "cited evidence and flagged for human review where verification fails. CONFIDENTIAL — For internal use only.")
    p.runs[0].font.size = Pt(8)
    p.runs[0].italic = True

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out
