"""Golden test: the synthetic scenario's seeded defects must all be detected."""
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from sra.catalog import load_templates
from sra.catalog.models import Result
from sra.cli import _bundle, _load_profile
from sra.detectors.base import Context
from sra.evidence.bundle import EvidenceBundle
from sra.evidence.redact import clean_text, find_secrets
from sra.models import ApplicationRef, Assessment
from sra.pipeline import propose_templates, run_assessment
from sra.selection.selector import evaluate

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"


@pytest.fixture(scope="module")
def scenario():
    if not SCENARIO.exists():
        import runpy
        runpy.run_path(str(SCENARIO.parents[1] / "synthetic" / "make_scenario.py"), run_name="__main__")
    expected = yaml.safe_load((SCENARIO / "expected.yaml").read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(expected["as_of"])
    bundle = _bundle(SCENARIO, as_of)
    profile, cands = propose_templates(_load_profile(SCENARIO, None), bundle, as_of)
    a = Assessment(application=ApplicationRef(name=profile.name), templates=[c.template_id for c in cands if c.selected],
                   assessment_date=as_of.date())
    sc, gate = run_assessment(a, bundle, profile, as_of)
    return expected, bundle, a, sc, gate, cands


def test_ingest_all_files(scenario):
    _, bundle, *_ = scenario
    assert not bundle.errors, bundle.errors
    assert {"incident", "change_request", "cmdb_ci", "problem", "kb_knowledge", "em_alert", "cmdb_rel_ci"} <= set(bundle.summary())


def test_template_selection(scenario):
    expected, *_, cands = scenario
    selected = {c.template_id for c in cands if c.selected}
    missing = set(expected["templates_include"]) - selected
    assert not missing, missing


def test_rating_and_gaps(scenario):
    expected, _, a, sc, *_ = scenario
    assert sc.rating == expected["rating"]
    assert set(expected["p1_gaps_include"]) <= set(sc.p1_gaps)
    assert set(expected["p2_gaps_include"]) <= set(sc.p2_gaps)
    assert sc.maturity_level <= 2


@pytest.mark.parametrize("idx", range(18))
def test_seeded_finding_detected(scenario, idx):
    expected, _, a, *_ = scenario
    if idx >= len(expected["seeded_findings"]):
        pytest.skip()
    detector, needle = expected["seeded_findings"][idx]
    hits = [f for f in a.findings if f.detector.startswith(detector)
            and needle.lower() in f"{f.title} {f.detail}".lower()]
    assert hits, f"not detected: {detector} / {needle}"


def test_every_finding_has_recommendation(scenario):
    _, _, a, *_ = scenario
    for f in a.findings:
        assert f.recommendation and f.recommendation.action and f.recommendation.due_date, f.title
        assert f.recommendation.owner


def test_gaps_have_findings(scenario):
    _, _, a, _, gate, _ = scenario
    assert not [b for b in gate.blockers if "has no finding" in b]
    assert not [b for b in gate.blockers if "scanner finding" in b]


def test_gate_blocks_unevaluated(scenario):
    _, _, a, _, gate, _ = scenario
    assert not gate.ok and any("not evaluated" in b for b in gate.blockers)


def test_template_threshold_conflict(scenario):
    _, _, a, sc, *_ = scenario
    assert a.results["SRE-AT-07-C03"].result == Result.GAP
    assert "6 months" in a.results["SRE-AT-07-C03"].rationale
    assert any("6 months" in c for c in sc.conflicts)


def test_no_secret_values_stored(scenario):
    _, bundle, a, *_ = scenario
    for it in bundle.items.values():
        assert "SuperSecret123" not in it.text
        assert "hunter2" not in str(it.records)
    assert "SuperSecret123" not in a.model_dump_json()


def test_redaction_patterns():
    assert clean_text("password=abc123 token: xyz") == "password=[REDACTED] token: [REDACTED]"
    assert "[REDACTED]@" in clean_text("jdbc://user:pw@host/db")
    assert [k for k, _ in find_secrets("key AKIAABCDEFGHIJKLMNOP")] == ["aws_access_key"]


def test_selection_expressions():
    p = {"tier": 1, "trigger": "deployment", "has_cicd": True, "incidents_90d": 0}
    assert evaluate("tier in [1, 2]", p) and evaluate("trigger == deployment", p) and evaluate("has_cicd", p)
    assert not evaluate("incidents_90d > 0", p) and evaluate("tier <= 2", p) and not evaluate("missing_field", p)
    assert evaluate("trigger not_in [periodic]", p)


def test_scoping_by_alias():
    ctx = Context(app_name="Orders Batch", aliases=["Orders Support"], as_of=datetime(2026, 1, 1), cfg={})
    rows = [{"cmdb_ci_business_app": "", "assignment_group": "Orders Support"}, {"cmdb_ci_business_app": "Billing"}]
    assert len(ctx.scoped(rows)) == 1


def test_unknown_file_is_reported_not_fatal():
    b = EvidenceBundle()
    assert b.ingest("photo.bmp", b"BM...") is None and b.errors


def test_email_addresses_redacted_but_not_reported_as_secrets():
    from sra.evidence.redact import redact
    text = "Escalated to jane.doe@cvs.example.com; clone git@github.com:org/repo.git"
    assert clean_text(text) == "Escalated to [REDACTED-EMAIL]; clone git@github.com:org/repo.git"
    assert "jane.doe" not in redact(text)
    assert find_secrets(text) == []
