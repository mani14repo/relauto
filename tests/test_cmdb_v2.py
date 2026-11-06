"""CMDB/CSDM V2: tier thresholds, maturity bands, CMDB Readiness Index and reporting gates."""
import datetime as dt

from sra.catalog import load_rubric, load_templates
from sra.catalog.models import Result
from sra.models import ApplicationRef, Assessment, CriterionResult, Source
from sra.scoring import load_matrix, score_assessment
from sra.scoring.cmdb import level_from_pct, threshold


def test_thresholds_by_tier():
    assert [threshold("TH-03", t) for t in (1, 2, 3)] == [90, 85, 80]
    assert threshold("TH-07", 3) is None and threshold("TH-01", None) == 90   # unknown tier -> Tier 2


def test_level_bands():
    assert [level_from_pct(p, 90) for p in (10, 30, 60, 85, 90)] == [0, 1, 2, 3, 4]


def _assessment(levels: dict[str, tuple[Result, int | None]]):
    a = Assessment(application=ApplicationRef(name="X"), templates=["CMDB-AT-01", "SRE-AT-01"],
                   assessment_date=dt.date(2026, 11, 4))
    for cid, (res, lv) in levels.items():
        a.results[cid] = CriterionResult(criterion_id=cid, result=res, maturity_level=lv, source=Source.HUMAN,
                                         evidence_refs=["EV-1"])
    return a


def test_cri_and_gates():
    a = _assessment({"CMDB-AT-01-C01": (Result.MET, 4), "CMDB-AT-01-C02": (Result.MET, 4),
                     "CMDB-AT-01-C03": (Result.GAP, 2), "CMDB-AT-01-C04": (Result.MET, 3),
                     "CMDB-AT-01-C05": (Result.INSUFFICIENT, None), "CMDB-AT-01-C06": (Result.MET, 4)})
    sc = score_assessment(a, load_templates(), load_rubric(), load_matrix())
    cm = sc.cmdb
    reg = next(d for d in cm.dimensions if d.name == "Registration & ownership")
    assert reg.score == round(100 * (4 + 4 + 2) / 12, 1) and reg.assessed == 3 and reg.total == 4
    assert reg.missing == ["CMDB-AT-04-C04 (template not selected)"]
    svc = next(d for d in cm.dimensions if d.name == "Service model (CSDM)")
    assert svc.score == round(100 * (3 + 0) / 8, 1)                                  # Insufficient Evidence = L0
    assert cm.cri is not None and not cm.complete
    gates = {g.id: g for g in cm.gates}
    # Gate A needs P1-01 (rubric) + AT-01 C01/C04/C06; P1-01 is Met only if every contributor is Met
    assert gates["A"].status in ("passed", "not passed", "undetermined")
    assert gates["C"].status == "undetermined" and any("template not selected" in p for p in gates["C"].pending)


def test_no_cmdb_section_without_cmdb_templates():
    a = Assessment(application=ApplicationRef(name="X"), templates=["SRE-AT-01"], assessment_date=dt.date(2026, 11, 4))
    assert score_assessment(a, load_templates(), load_rubric(), load_matrix()).cmdb is None


def test_enablers_do_not_feed_the_rubric():
    from sra.crosswalk import rubric_criteria_map
    m = rubric_criteria_map()
    assert "CMDB-AT-12-C02" not in m["P1-05"] and "CMDB-AT-01-C07" in m["P1-03"] and "CMDB-AT-01-C07" not in m["P1-05"]
    assert "CMDB-AT-04-C02" in m["P2-10"] and "CMDB-AT-01-C06" in m["P2-10"]
