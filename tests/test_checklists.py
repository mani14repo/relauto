"""Document checklists (sra.detectors.checklists): criteria answered by finding required items in documents."""
import copy
import json
from datetime import datetime, timedelta

import pytest

from sra.catalog.models import Result
from sra.config import load_yaml
from sra.detectors import checklists as cl
from sra.detectors.base import Context
from sra.evidence.bundle import EvidenceBundle

AS_OF = datetime(2026, 11, 4, 9)
MET, GAP = Result.MET, Result.GAP
RECENT = (AS_OF - timedelta(days=30)).strftime("%Y-%m-%d")
OLD = (AS_OF - timedelta(days=500)).strftime("%Y-%m-%d")


def ctx(tier=2):
    return Context(app_name="Orders", aliases=["Orders Support"], as_of=AS_OF, cfg=copy.deepcopy(load_yaml("detectors")), tier=tier)


def bundle(**docs):
    b = EvidenceBundle()
    for name, text in docs.items():
        name = name.replace("__", ".").replace("_", "-")
        data = json.dumps({"result": text}).encode() if name.endswith(".json") else text.encode()
        assert b.ingest(name, data, as_of=AS_OF), b.errors
    return b


def run(b, tier=2):
    return cl.run(b, ctx(tier)).results


TOIL = f"""# Orders toil inventory
Last reviewed: {RECENT}
Baseline date: {RECENT}. Categories: deploy, access, data fixes. Top 5 ranked by effort.

| Item | Category | Frequency | Effort | Automatable |
| Restart batch job | data fixes | daily | 3 h per week | automatable |
| Grant read access | access | weekly | 2 h per week | manual |
| Reprocess failed orders | data fixes | weekly | 4 h per week | automatable |
| Rotate service certificates | deploy | monthly | 1 h per week | repetitive |
| Clear stuck queue | data fixes | weekly | 2 h per week | automatable |

Reduction target: 30% reduction by Q2 2027. Owner: Orders SRE lead. Progress reviewed each quarter (Q4: 12 h -> 10 h).
Automation candidates: restart job (8 story points, saves 3 hours, priority 1, ROI payback in 2 months, owner Priya).
Capacity released: before 12 h/week, after 7 h/week; 5 hours saved reinvested in reliability work this sprint.
"""

AUTOMATION = f"""# Orders auto-remediation runbook
Last reviewed: {RECENT}
Approved through change CHG0012345 (CAB). Boundary conditions: the bot only restarts pods; anything else needs human approval.
Escalation: unexpected state pages the on-call SRE and the automation stops.
Test evidence: tested in staging on 2026-09-02 with results attached. Monitoring raises an alert on failure of any run.
On failure: the job stops and logs the error. Manual recovery steps: restart the deployment by hand.
Disable: set the kill switch flag. Rollback restores the prior state. Validate the service health check afterwards.
"""


def test_toil_criteria_met_by_a_complete_inventory():
    res = run(bundle(**{"orders_toil_inventory__md": TOIL}))
    for cid in ("SRE-AT-10-C01", "SRE-AT-16-C01", "SRE-AT-16-C02", "SRE-AT-16-C03", "SRE-AT-16-C04"):
        assert res[cid].result == MET, (cid, res[cid].rationale)


def test_toil_inventory_without_effort_or_target_is_a_gap_naming_the_missing_items():
    thin = "# Orders toil inventory\nCategories: deploy, access. Restart job daily. Owner: SRE.\n"
    res = run(bundle(**{"orders_toil_inventory__md": thin}))
    r = res["SRE-AT-16-C01"]
    assert r.result == GAP and r.needs_human_review and "3 items with weekly effort" in r.rationale
    assert res["SRE-AT-16-C02"].result == GAP and "reduction target" in res["SRE-AT-16-C02"].rationale


def test_stale_baseline_fails_the_fresh_item():
    res = run(bundle(**{"orders_toil_inventory__md": TOIL.replace(RECENT, OLD)}))
    assert res["SRE-AT-16-C01"].result == GAP and "within 6 months" in res["SRE-AT-16-C01"].rationale


def test_automation_runbook_criteria():
    res = run(bundle(**{"orders_automation_runbook__md": AUTOMATION}))
    for cid in ("SRE-AT-14-C01", "SRE-AT-14-C02", "SRE-AT-14-C03"):
        assert res[cid].result == MET, (cid, res[cid].rationale)
    res = run(bundle(**{"orders_automation_runbook__md": "# Orders automation runbook\nRestart the pods when the alert fires.\n"}))
    assert res["SRE-AT-14-C03"].result == GAP


def test_ordinary_runbook_is_not_an_automation_document():
    rb = "# Orders runbook\nLast reviewed: 2026-10-01\nIf automation fails, restart the job.\n"
    res = run(bundle(**{"orders_runbook__md": rb}))
    assert not {"SRE-AT-14-C01", "SRE-AT-14-C02", "SRE-AT-14-C03"} & set(res)


def test_raci_blank_cells_are_a_gap():
    full = ("# Orders RACI\n| Activity | PSE | SRE | Eng |\n| Incident | R | A | C |\n| Problem | I | A | R |\n"
            "| Change | C | A | R |\nConfirmed by PSE, SRE and engineering leads.\n")
    assert run(bundle(**{"orders_raci__md": full}))["SRE-AT-08-C05"].result == MET
    blank = full.replace("| Problem | I | A | R |", "| Problem | I |  | R |")
    r = run(bundle(**{"orders_raci__md": blank}))["SRE-AT-08-C05"]
    assert r.result == GAP and "no blank cells" in r.rationale


def test_kpi_catalogue():
    doc = ("# Orders KPI catalogue\nApproved by REO leadership.\nMTTR: calculation opened_at to resolved_at; data source: incident; owner: PSE lead.\n"
           "Incident volume by priority. Problem recurrence rate. Change success rate. SLO attainment.\n")
    assert run(bundle(**{"orders_kpi_catalogue__md": doc}))["ITSM-AT-11-C01"].result == MET
    r = run(bundle(**{"orders_kpi_catalogue__md": doc.replace("SLO attainment.", "")}))["ITSM-AT-11-C01"]
    assert r.result == GAP and "SLO attainment" in r.rationale


def test_monthly_cadence_needs_each_of_the_last_three_months():
    minutes = ("# Orders governance meeting minutes\n2026-10-14: improvement register reviewed; decisions distributed to attendees.\n"
               "2026-09-12: improvement register reviewed; decision log sent to REO.\n2026-08-10: improvement register reviewed.\n")
    assert run(bundle(**{"orders_governance_minutes__md": minutes}))["ITSM-AT-08-C05"].result == MET
    r = run(bundle(**{"orders_governance_minutes__md": minutes.replace("2026-09-12", "2026-06-12")}))["ITSM-AT-08-C05"]
    assert r.result == GAP and "2026-09" not in r.rationale.split("found:")[1]


def test_recovery_objectives_and_tier_limits():
    plan = f"# Orders disaster recovery plan\nLast reviewed: {RECENT}\nRTO 2 hours, RPO 30 minutes. Approved by the business owner.\n"
    test = f"# Orders DR test results\nDate reviewed: {RECENT}\nFailover test executed; restored within RTO.\n"
    res = run(bundle(**{"orders_dr_plan__md": plan, "orders_dr_test__md": test}), tier=1)
    assert res["ITSM-AT-12-C01"].result == MET and res["SRE-AT-02-C04"].result == MET
    assert res["CMDB-AT-10-C03"].result == MET and "RTO 2 h" in res["CMDB-AT-10-C03"].measured_value
    slow = run(bundle(**{"orders_dr_plan__md": plan.replace("RTO 2 hours", "RTO 12 hours")}), tier=1)["CMDB-AT-10-C03"]
    assert slow.result == GAP and "exceeds the Tier 1 maximum" in slow.rationale


def test_p1_records_need_bridge_notification_and_timeline():
    inc = [{"number": "INC1", "priority": "1 - Critical", "short_description": "Orders down", "opened_at": "2026-10-20 10:00:00",
            "close_notes": "Bridge opened 10:05, stakeholders notified 10:10, timeline attached."},
           {"number": "INC2", "priority": "1 - Critical", "short_description": "Orders down", "opened_at": "2026-10-25 10:00:00",
            "close_notes": "Restarted the service."}]
    r = run(bundle(incident__json=inc))["ITSM-AT-02-C05"]
    assert r.result == GAP and "1 of 2" in r.rationale and "INC2" in r.rationale
    assert run(bundle(incident__json=inc[:1]))["ITSM-AT-02-C05"].result == MET


def test_nothing_is_set_without_matching_documents():
    assert run(bundle(**{"notes__md": "# Team lunch\nPizza on Friday.\n"})) == {}


def test_every_configured_pattern_compiles():
    import re
    for cid, spec in load_yaml("checklists")["criteria"].items():
        for p in spec.get("find", []):
            re.compile(p)
        for it in spec["items"]:
            # an unquoted name with commas would be split into stray keys by YAML
            assert set(it) <= {"name", "any", "none", "min_count", "means", "fresh_months"}, (cid, it)
            for p in it.get("any", []) + it.get("none", []):
                re.compile(p)


@pytest.mark.parametrize("value", [False, "off"])
def test_suggestions_off_in_either_yaml_form(value):
    s = cl.Suggester({"suggest": value}, {})
    assert s.scorer is None and "turned off" in s.reason


class FakeScorer:
    """Entails the requirement only for lines containing `phrase`."""
    repo = "fake/nli"

    def __init__(self, phrase):
        self.phrase, self.calls = phrase, 0

    def entailment(self, premises, hypothesis):
        import numpy as np
        self.calls += 1
        return np.array([0.97 if self.phrase in p else 0.05 for p in premises])


def suggesting(monkeypatch, phrase):
    monkeypatch.setenv("SRA_SUGGEST", "auto")            # tests/conftest.py turns suggestions off by default
    fake = FakeScorer(phrase)
    from sra.retrieval import nli
    monkeypatch.setattr(nli, "get_scorer", lambda repo, f: (fake, ""))
    return fake


ROLLBACK = ("# Orders rollback procedure\nRoll back if error rate exceeds 2% for 5 minutes.\n"
            "Only the release manager may revert a release, without asking anyone.\nRun helm rollback orders.\n")


def test_suggestion_points_at_a_line_but_never_makes_the_item_present(monkeypatch):
    fake = suggesting(monkeypatch, "may revert a release")
    rep = cl.run(bundle(**{"orders_rollback__md": ROLLBACK}), ctx())
    r = rep.results["SRE-AT-15-C04"]
    assert r.result == GAP and r.needs_human_review                       # still missing for the decision
    assert "named authority (not found by keywords; possibly covered by 'Only the release manager may revert a release" in r.rationale
    assert "model suggestion 0.97, please confirm" in r.rationale and "1 line suggestion(s) from fake/nli" in rep.status
    assert fake.calls >= 1


def test_no_suggestion_below_the_threshold_or_without_a_requirement(monkeypatch):
    suggesting(monkeypatch, "nothing in this document")
    r = cl.run(bundle(**{"orders_rollback__md": ROLLBACK}), ctx()).results["SRE-AT-15-C04"]
    assert "possibly covered" not in r.rationale
    s = cl.Suggester({}, {"SRE-AT-15-C04": {}})
    s.scorer = FakeScorer("anything")
    assert s.suggest("SRE-AT-15-C04", "named authority", []) is None     # no requirement written for it


def test_without_the_model_checklists_run_on_keywords(monkeypatch):
    monkeypatch.setenv("SRA_SUGGEST", "auto")
    from sra.retrieval import nli
    monkeypatch.setattr(nli, "get_scorer", lambda repo, f: (None, "not available locally"))
    rep = cl.run(bundle(**{"orders_rollback__md": ROLLBACK}), ctx())
    assert rep.results["SRE-AT-15-C04"].result == GAP and "no line suggestions (not available locally)" in rep.status


def test_every_requirement_names_a_real_checklist_item():
    items = {cid: {i["name"] for i in spec["items"]} for cid, spec in load_yaml("checklists")["criteria"].items()}
    for cid, reqs in load_yaml("checklist_requirements").items():
        assert cid in items and set(reqs) <= items[cid], cid


def test_real_model_if_present():
    from sra.retrieval.nli import get_scorer
    scorer, why = get_scorer("cross-encoder/nli-deberta-v3-xsmall", "onnx/model_quint8_avx2.onnx")
    if scorer is None:
        pytest.skip(why)
    p = scorer.entailment(["The on-call engineer may revert a release without asking anyone.", "Lunch is at noon."],
                          "This describes who has the authority to decide a rollback.")
    assert p[0] > 0.5 > p[1]
