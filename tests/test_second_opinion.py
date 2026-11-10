"""Checklist Gaps that only come from keyword misses get an AI second opinion when the run uses the LLM."""
from datetime import date

from sra.catalog.models import Result
from sra.llm.client import LLMError
from sra.llm.evaluate import Evaluator, settle
from sra.models import ApplicationRef, Assessment, CriterionResult, Source
from sra.pipeline import _merge

from test_checklists import OLD, ROLLBACK, TOIL, bundle, ctx, run
from test_llm import FakeClient, first_chunk_id
from sra.detectors import checklists as cl

GAP, MET = Result.GAP, Result.MET
CID = "SRE-AT-15-C04"


def test_only_keyword_misses_are_marked():
    r = run(bundle(**{"orders_rollback__md": ROLLBACK}))[CID]
    assert r.result == GAP and r.second_opinion                      # "named authority (not found)"
    stale = run(bundle(**{"orders_toil_inventory__md": TOIL.replace("Last reviewed:", "Reviewed:").replace(
        TOIL.split("Baseline date: ")[1][:10], OLD)}))["SRE-AT-16-C01"]
    assert stale.result == GAP and not stale.second_opinion          # a date problem is a real finding
    full = ("# Orders RACI\n| Activity | PSE | SRE | Eng |\n| Incident | R | A | C |\n| Problem | I |  | R |\n"
            "| Change | C | A | R |\nConfirmed by PSE, SRE and engineering leads.\n")
    blank = run(bundle(**{"orders_raci__md": full}))["SRE-AT-08-C05"]
    assert blank.result == GAP and not blank.second_opinion          # a blank RACI cell is a real finding
    met = run(bundle(**{"orders_raci__md": full.replace("| I |  | R |", "| I | A | R |")}))["SRE-AT-08-C05"]
    assert met.result == MET and not met.second_opinion


def test_merge_keeps_the_flag_only_when_every_gap_is_a_keyword_miss():
    kw = CriterionResult(criterion_id=CID, result=GAP, source=Source.RULE, rationale="kw", second_opinion=True)
    data = CriterionResult(criterion_id=CID, result=GAP, source=Source.RULE, rationale="data")
    assert not _merge(kw.model_copy(), data).second_opinion
    assert _merge(kw.model_copy(), kw.model_copy(update={"rationale": "kw2"})).second_opinion


def _with_checklist_result():
    b = bundle(**{"orders_rollback__md": ROLLBACK})
    a = Assessment(application=ApplicationRef(name="Orders"), templates=["SRE-AT-15"], assessment_date=date(2026, 11, 4))
    for cid, r in cl.run(b, ctx()).results.items():
        if cid.startswith("SRE-AT-15"):
            a.results[cid] = r
    assert a.results[CID].second_opinion
    return a, b


def answer(result):
    return lambda u: {"result": result, "rationale": f"Model says {result}.", "cited": [first_chunk_id(u)], "gaps": [],
                      "recommendation": "Name the approver." if result == "Gap" else "", "risk": "Medium", "confidence": 0.9}


def test_model_met_replaces_the_keyword_gap_but_stays_for_review():
    a, b = _with_checklist_result()
    fake = FakeClient(answer("Met"))
    Evaluator(fake).evaluate_pending(a, b)
    r = a.results[CID]
    assert r.result == MET and r.source == Source.LLM and r.needs_human_review and not r.second_opinion
    assert "found the items the keywords missed" in r.rationale and "Checklist: " in r.rationale
    assert any(CID.rsplit("-", 1)[0] in p or "revert" in p for p in fake.prompts)


def test_model_gap_keeps_a_gap_with_its_recommendation():
    a, b = _with_checklist_result()
    Evaluator(FakeClient(answer("Gap"))).evaluate_pending(a, b)
    r = a.results[CID]
    assert r.result == GAP and r.source == Source.LLM and r.recommendation == "Name the approver." and r.needs_human_review
    assert "agrees it is a Gap" in r.rationale


def test_failed_or_unhelpful_model_keeps_the_checklist_gap():
    a, b = _with_checklist_result()
    before = a.results[CID].rationale
    Evaluator(FakeClient(LLMError("timeout"))).evaluate_pending(a, b)
    r = a.results[CID]
    assert r.result == GAP and r.source == Source.RULE and r.rationale.startswith(before) and "call failed" in r.rationale
    prior = CriterionResult(criterion_id=CID, result=GAP, source=Source.RULE, rationale="kw", second_opinion=True)
    na = CriterionResult(criterion_id=CID, result=Result.INSUFFICIENT, source=Source.LLM)
    kept, outcome = settle(prior, na, "verified")
    assert kept.result == GAP and kept.source == Source.RULE and outcome == "kept"


def test_other_rule_results_are_not_sent_to_the_model():
    a, b = _with_checklist_result()
    a.results[CID].second_opinion = False
    fake = FakeClient(answer("Met"))
    Evaluator(fake).evaluate_pending(a, b)
    assert a.results[CID].result == GAP and a.results[CID].source == Source.RULE


def test_rules_only_run_leaves_the_gap_alone():
    a, b = _with_checklist_result()
    assert a.results[CID].result == GAP and a.results[CID].source == Source.RULE   # no evaluator, nothing changes
