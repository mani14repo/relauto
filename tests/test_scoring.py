import pytest

from sra.catalog import load_rubric, load_templates
from sra.catalog.models import Result, Risk
from sra.models import ApplicationRef, Assessment, CriterionResult
from sra.scoring import load_matrix, rate, score_assessment
from sra.scoring.engine import maturity

M = load_matrix()


def make(templates, default=Result.MET, overrides=()):
    ts = load_templates()
    a = Assessment(application=ApplicationRef(name="demo"), templates=templates)
    for tid in templates:
        for c in ts[tid].criteria:
            a.results[c.id] = CriterionResult(criterion_id=c.id, result=default, evidence_refs=["E1"])
    for r in overrides:
        a.results[r.criterion_id] = r
    return a


def score(a):
    return score_assessment(a, load_templates(), load_rubric(), M)


@pytest.mark.parametrize("p1,p2,rating", [
    (0, 0, "Ready"), (0, 1, "Conditional"), (0, 3, "Conditional"),
    (0, 4, "At Risk"), (0, 10, "At Risk"), (1, 0, "Not Ready"), (2, 5, "Not Ready"),
])
def test_rating_bands(p1, p2, rating):
    assert rate(p1, p2, M)["rating"] == rating


def test_rubric_unmapped_is_insufficient():
    # Only SRE-AT-01 assessed: rubric criteria with no mapped evidence are Insufficient -> gaps.
    sc = score(make(["SRE-AT-01"]))
    assert sc.template_scores[0].score == 100.0
    assert "P1-05" not in sc.p1_gaps        # covered by SRE-AT-01-C01 Met
    assert "P1-08" in sc.p1_gaps            # nothing maps from SRE-AT-01
    assert sc.rating == "Not Ready"
    assert sc.maturity_level == 2 and sc.maturity_capped   # index 100 capped by Not Ready


def test_all_met_with_direct_rubric_is_ready():
    a = make(["SRE-AT-01"])
    for rc in load_rubric().criteria:
        a.rubric_direct[rc.id] = CriterionResult(criterion_id=rc.id, result=Result.MET, evidence_refs=["E"])
    sc = score(a)
    assert sc.rating == "Ready" and sc.index == 100.0 and sc.maturity_level == 5 and not sc.maturity_capped


def test_direct_met_overridden_by_mapped_gap():
    a = make(["SRE-AT-01"], overrides=[CriterionResult(criterion_id="SRE-AT-01-C01", result=Result.GAP)])
    a.rubric_direct["P1-05"] = CriterionResult(criterion_id="P1-05", result=Result.MET)
    assert "P1-05" in score(a).p1_gaps


def test_weighted_template_score():
    # SRE-AT-01: C01..C05 High (3), C06 Critical (4) -> total weight 19. Gap on C06 -> 15/19.
    sc = score(make(["SRE-AT-01"], overrides=[CriterionResult(criterion_id="SRE-AT-01-C06", result=Result.GAP)]))
    assert sc.template_scores[0].score == round(100 * 15 / 19, 1)
    assert "P1-01" in sc.p1_gaps


def test_risk_override_changes_weight():
    r = CriterionResult(criterion_id="SRE-AT-01-C01", result=Result.GAP, risk=Risk.LOW)
    sc = score(make(["SRE-AT-01"], overrides=[r]))
    assert sc.template_scores[0].score == round(100 * 16 / 17, 1)


def test_na_requires_approval():
    na = CriterionResult(criterion_id="SRE-AT-01-C05", result=Result.NA, na_justification="batch only")
    a = make(["SRE-AT-01"], overrides=[na])
    s1 = score(a).template_scores[0]
    assert s1.insufficient == 1 and s1.na == 0
    na.na_approved = True
    s2 = score(a).template_scores[0]
    assert s2.na == 1 and s2.score == 100.0


def test_unevaluated_counts_as_insufficient():
    a = make(["SRE-AT-01"])
    del a.results["SRE-AT-01-C02"]
    s = score(a).template_scores[0]
    assert s.unevaluated == 1 and s.insufficient == 1 and s.score < 100


def test_domain_weights_renormalised():
    a = make(["SRE-AT-01", "ITSM-AT-02"])
    a.results["ITSM-AT-02-C01"].result = Result.GAP   # Critical
    sc = score(a)
    itsm = sc.domain_scores["ITSM"]
    assert sc.domain_scores["SRE"] == 100.0 and itsm < 100
    assert sc.index == round((100 * 0.5 + itsm * 0.3) / 0.8, 1)
    assert "CMDB" not in sc.domain_scores


def test_evidence_coverage():
    a = make(["SRE-AT-01"])
    a.results["SRE-AT-01-C01"].evidence_refs = []
    assert score(a).evidence_coverage_pct == round(100 * 5 / 6, 1)


def test_maturity_bands():
    assert maturity(39.9, M) == (1, "Initial")
    assert maturity(40, M) == (2, "Developing")
    assert maturity(74.9, M) == (3, "Defined")
    assert maturity(89.9, M) == (4, "Managed")
    assert maturity(90, M) == (5, "Optimized")
