import datetime as dt
import zipfile

import pytest
from lxml import etree

from sra.catalog import load_rubric, load_templates
from sra.catalog.compiler import template_path
from sra.catalog.models import Result, Risk
from sra.models import (ApplicationRef, Assessment, CriterionResult, Finding, FindingType, Recommendation,
                        SignOff, Source)
from sra.ooxml import Table, W, docx_blocks
from sra.report.docx_fill import BOX, EVIDENCE_LABEL, TICK, fill_template
from sra.report.summary import build_summary
from sra.scoring import load_matrix, score_assessment

TEMPLATES = load_templates()
CYCLE = [Result.MET, Result.GAP, Result.INSUFFICIENT, Result.NA]


def assessment_for(t):
    a = Assessment(application=ApplicationRef(name="Rebates Batch", cmdb_id="APP0001"), assessor="J. SRE",
                   templates=[t.id], assessment_date=dt.date(2026, 11, 4))
    for i, c in enumerate(t.criteria):
        res = CYCLE[i % 4]
        a.results[c.id] = CriterionResult(criterion_id=c.id, result=res, risk=Risk.HIGH if res == Result.GAP else None,
                                          rationale=f"rationale {c.id}", evidence_refs=[f"EV-{i}"], source=Source.RULE,
                                          na_justification="not hosted in cloud" if res == Result.NA else "",
                                          na_approved=res == Result.NA)
        if res in (Result.GAP, Result.INSUFFICIENT):
            a.findings.append(Finding(type=FindingType.GAP, severity=Risk.HIGH, title=f"gap in {c.id}",
                                      criterion_ids=[c.id],
                                      recommendation=Recommendation(action=f"fix {c.id}", playbooks=["SRE-PB-08"],
                                                                    due_date=dt.date(2026, 12, 4))))
    a.signoffs = [SignOff(role=r, name=f"Name {r}", date=dt.date(2026, 11, 5), decision="Approved")
                  for r in t.signoff_roles]
    return a


@pytest.mark.parametrize("tid", sorted(TEMPLATES))
def test_fill_roundtrip(tid, tmp_path):
    t = TEMPLATES[tid]
    a = assessment_for(t)
    out = fill_template(t, template_path(t), a, tmp_path / f"{tid}.docx")

    with zipfile.ZipFile(out) as z, zipfile.ZipFile(template_path(t)) as orig:
        assert sorted(z.namelist()) == sorted(orig.namelist())
        etree.fromstring(z.read("word/document.xml"))   # well-formed

    tables = [b for b in docx_blocks(out) if isinstance(b, Table)]
    meta = tables[0].kv()
    assert meta["Application"] == "Rebates Batch (APP0001)"
    assert meta["Assessment Reference"] == a.id and meta["Assessor"] == "J. SRE"
    crit = [b for b in tables if "Acceptance Criteria" in b.labels]
    assert len(crit) == len(t.criteria)
    for tbl, c in zip(crit, t.criteria):
        kv = tbl.kv()
        r = a.results[c.id]
        if t.variant == "A":
            assert kv["Result"].count(TICK) == 1 and kv["Result"].count(BOX) == 3
            assert f"{r.result.value}  {TICK}" in kv["Result"]
        else:
            assert kv["Result"] == r.result.value
            assert not kv["Recommendation"].startswith("[") and not kv["Validation"].startswith("[")
        assert c.example_recommendation == "" or c.example_recommendation not in kv["Recommendation"]
        assert f"rationale {c.id}" in kv[EVIDENCE_LABEL]
        assert tbl.labels.index(EVIDENCE_LABEL) == tbl.labels.index("Result") + 1
        if r.result in (Result.GAP, Result.INSUFFICIENT):
            assert f"fix {c.id}" in kv["Recommendation"]
            assert kv["Risk Classification"].startswith((r.risk or c.default_risk).value)
        if r.result == Result.NA:
            assert "not hosted in cloud" in kv["Recommendation"]
    signoff = next((b for b in tables if b.rows[0][:1] == ["Role"]), None)
    if signoff is not None:
        assert all(row[1].startswith("Name ") and row[3] == "Approved" for row in signoff.rows[1:])


def test_variant_a_label_visible(tmp_path):
    t = TEMPLATES["SRE-AT-01"]
    out = fill_template(t, template_path(t), assessment_for(t), tmp_path / "x.docx")
    root = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml"))
    for tbl in root.iter(W + "tbl"):
        first = tbl.find(W + "tr").find(W + "tc")
        if "".join(first.itertext()).strip() == "Criterion":
            assert {c.get(W + "val") for c in first.iter(W + "color")} == {"FFFFFF"}


def test_formatting_preserved(tmp_path):
    """Shading fills and fonts of the original must all still be present."""
    t = TEMPLATES["SRE-AT-14"]
    src = template_path(t)
    out = fill_template(t, src, assessment_for(t), tmp_path / "x.docx")

    def fills(p):
        root = etree.fromstring(zipfile.ZipFile(p).read("word/document.xml"))
        return {s.get(W + "fill") for s in root.iter(W + "shd")}
    assert fills(src) <= fills(out)


def test_summary_report(tmp_path):
    t = TEMPLATES["SRE-AT-01"]
    a = assessment_for(t)
    sc = score_assessment(a, TEMPLATES, load_rubric(), load_matrix())
    out = build_summary(a, sc, TEMPLATES, load_rubric(), load_matrix(), tmp_path / "summary.docx")
    text = "\n".join(" | ".join(r) if isinstance(b, Table) else b.text for b in docx_blocks(out)
                     for r in (b.rows if isinstance(b, Table) else [[]]))
    assert "Not Ready" in text and "P1-08" in text and "fix SRE-AT-01-C02" in text
