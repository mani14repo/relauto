"""Deterministic rules for template criteria that previously went to the LLM (sra.detectors.criteria_rules)."""
import copy
import csv
import io
import json
from datetime import datetime, timedelta

import pytest
import yaml

from sra.catalog.models import Result
from sra.config import load_yaml
from sra.detectors import criteria_rules as cr
from sra.detectors.base import Context
from sra.evidence.bundle import EvidenceBundle

AS_OF = datetime(2026, 11, 4, 9)
MET, GAP = Result.MET, Result.GAP


def day(n):
    return (AS_OF - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def bundle(**files):
    """files: name -> list of row dicts (CSV), dict (JSON), list of k8s objects (YAML) or text."""
    b = EvidenceBundle()
    for name, data in files.items():
        name = name.replace("__", ".").replace("_dash_", "-")
        if name.endswith(".csv"):
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=list({k: 1 for r in data for k in r}))
            w.writeheader()
            w.writerows(data)
            raw = out.getvalue().encode()
        elif name.endswith(".json"):
            raw = json.dumps({"result": data}).encode()
        elif name.endswith(".yaml"):
            raw = yaml.safe_dump_all(data).encode()
        else:
            raw = data.encode()
        assert b.ingest(name, raw, as_of=AS_OF), b.errors
    return b


def ctx(tier=2, **over):
    cfg = copy.deepcopy(load_yaml("detectors"))
    cfg["criteria_rules"].update(over)
    return Context(app_name="Orders", aliases=["Orders Support"], as_of=AS_OF, cfg=cfg, tier=tier)


def run(b, c=None):
    return cr.run(b, c or ctx()).results


def inc(n, **kw):
    return {"number": f"INC{n:07d}", "short_description": "orders slow", "priority": "3 - Moderate", "state": "Closed",
            "category": "Application", "subcategory": "Performance", "assignment_group": "Orders Support",
            "opened_at": day(10 + n), "resolved_at": day(10 + n), "close_code": "Solved", "cmdb_ci_business_app": "Orders", **kw}


def deploy(labels=None, env=None):
    c = {"name": "api", "image": "x"}
    if env:
        c["env"] = [{"name": "OTEL_RESOURCE_ATTRIBUTES", "value": env}]
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "orders-api", "labels": labels or {}},
            "spec": {"replicas": 3, "template": {"metadata": {"labels": labels or {}}, "spec": {"containers": [c]}}}}


def test_rules_do_nothing_without_their_data():
    assert run(EvidenceBundle()) == {}


# ---------------------------------------------------------------- CMDB
def test_placement_and_region_none():
    ci = {"name": "Orders", "sys_class_name": "cmdb_ci_business_app", "owned_by": "x", "business_criticality": "1 - most critical",
          "u_region": "us-east", "u_portfolio": "Retail", "u_sub_portfolio": "Orders"}
    r = run(bundle(cmdb_ci__csv=[ci]))["CMDB-AT-01-C03"]
    assert r.result == MET and r.maturity_level == 4 and r.needs_human_review
    r = run(bundle(cmdb_ci__csv=[{**ci, "u_region": "None"}]))["CMDB-AT-01-C03"]
    assert r.result == GAP and "Region = None: 1" in r.rationale


def test_stale_cis_against_th10():
    rows = [{"name": f"srv{i}", "sys_class_name": "cmdb_ci_server", "last_discovered": day(2)} for i in range(19)]
    ok = run(bundle(cmdb_ci__csv=rows + [{"name": "old", "sys_class_name": "cmdb_ci_server", "last_discovered": day(60)}]))
    assert ok["CMDB-AT-02-C04"].result == MET            # 5% stale = TH-10 for Tier 2
    bad = run(bundle(cmdb_ci__csv=rows[:9] + [{"name": "old", "sys_class_name": "cmdb_ci_server", "last_discovered": day(60)}]))
    assert bad["CMDB-AT-02-C04"].result == GAP and bad["CMDB-AT-02-C04"].measured_value.startswith("10.0%")


def test_workload_labels_and_otel_keys():
    keys = {k: "x" for k in load_yaml("detectors")["criteria_rules"]["required_labels"]}
    res = run(bundle(**{"orders_dash_api__yaml": [deploy(keys, "service.name=orders,service.namespace=retail,deployment.environment.name=prod")]}))
    assert res["CMDB-AT-07-C03"].result == MET and res["CMDB-AT-12-C04"].result == MET
    res = run(bundle(**{"orders_dash_api__yaml": [deploy({"app.kubernetes.io/name": "orders"})]}))
    assert res["CMDB-AT-07-C03"].result == GAP and res["CMDB-AT-12-C04"].result == GAP


def test_outage_priority_follows_tier():
    rows = [inc(i, short_description="orders site DOWN", priority="3 - Moderate") for i in range(3)]
    assert run(bundle(incident__json=rows), ctx(tier=1))["CMDB-AT-10-C04"].result == GAP
    rows = [inc(i, short_description="orders site DOWN", priority="2 - High") for i in range(3)]
    assert run(bundle(incident__json=rows), ctx(tier=1))["CMDB-AT-10-C04"].result == MET
    assert "CMDB-AT-10-C04" not in run(bundle(incident__json=[inc(1)]))   # no outages: nothing to judge


def test_change_ci_and_alert_binding():
    ch = [{"number": f"CHG{i}", "short_description": "Orders release", "cmdb_ci": "Orders" if i else "", "start_date": day(5),
           "end_date": day(5), "backout_plan": "redeploy", "test_plan": "suite"} for i in range(4)]
    al = [{"number": f"A{i}", "cmdb_ci": "Orders" if i else "", "description": "x", "sys_created_on": day(1)} for i in range(10)]
    res = run(bundle(change_request__csv=ch, em_alert__csv=al))
    assert res["CMDB-AT-08-C01"].result == GAP and res["CMDB-AT-12-C05"].result == MET   # 3/4 vs 9/10


# ---------------------------------------------------------------- ITSM
def test_jenkins_and_policy_compliance():
    res = run(bundle(Jenkinsfile="pipeline { stages { stage('b') { steps { sh 'make' } } } }"))
    assert res["ITSM-AT-01-C05"].result == GAP and res["ITSM-AT-09-C02"].result == GAP and res["SRE-AT-12-C01"].result == GAP


def test_time_worked_fcr_and_reporting_quality():
    rows = [inc(i, time_worked="00:30:00" if i % 5 else "0", reassignment_count="0" if i % 2 else "2") for i in range(10)]
    res = run(bundle(incident__json=rows))
    assert res["ITSM-AT-02-C03"].result == GAP          # 8 of 10 = 80%; the criterion asks for more than 80%
    assert res["ITSM-AT-04-C01"].result == MET and res["ITSM-AT-04-C01"].needs_human_review   # 50%; no PSE groups set
    assert res["ITSM-AT-11-C02"].result == MET
    rows = [inc(i, time_worked="00:30:00" if i else "0") for i in range(10)]
    assert run(bundle(incident__json=rows))["ITSM-AT-02-C03"].result == MET    # 90%


def test_backlog_needs_open_records():
    assert "ITSM-AT-04-C03" not in run(bundle(incident__json=[inc(1)]))
    res = run(bundle(incident__json=[inc(1), inc(2, state="In Progress", priority="2 - High")]))
    assert res["ITSM-AT-04-C03"].result == GAP


def test_problem_change_link():
    pr = [{"number": "PRB1", "short_description": "Orders job", "state": "Resolved", "rfc": "CHG1", "cmdb_ci": "Orders"},
          {"number": "PRB2", "short_description": "Orders db", "state": "Fix in Progress", "rfc": "", "cmdb_ci": "Orders"}]
    assert run(bundle(problem__csv=pr))["ITSM-AT-03-C03"].result == GAP
    assert run(bundle(problem__csv=pr[:1]))["ITSM-AT-03-C03"].result == MET


def test_knowledge_rules():
    kb = [{"number": f"KB{i}", "short_description": f"Orders performance runbook {i}", "kb_category": "Performance",
           "sys_updated_on": day(20 + i), "u_application": "Orders"} for i in range(6)]
    res = run(bundle(incident__json=[inc(1)], kb_knowledge__csv=kb))
    assert res["ITSM-AT-07-C01"].result == MET and res["ITSM-AT-07-C04"].result == MET
    assert res["ITSM-AT-07-C02"].result == MET and res["ITSM-AT-07-C02"].needs_human_review     # no owner column
    res = run(bundle(incident__json=[inc(1, subcategory="Replication")], kb_knowledge__csv=[{**kb[0], "sys_updated_on": day(400)}]))
    assert res["ITSM-AT-07-C01"].result == GAP and res["ITSM-AT-07-C02"].result == GAP and res["ITSM-AT-07-C04"].result == GAP


# ---------------------------------------------------------------- SRE
def test_autoscaling_target():
    hpa = {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": "orders-api"},
           "spec": {"scaleTargetRef": {"name": "orders-api"},
                    "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"averageUtilization": 85}}}]}}
    assert run(bundle(**{"orders_dash_api__yaml": [deploy()]}))["SRE-AT-06-C05"].result == GAP
    assert run(bundle(**{"orders_dash_api__yaml": [deploy(), hpa]}))["SRE-AT-06-C05"].result == GAP
    hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] = 70
    assert "SRE-AT-06-C05" not in run(bundle(**{"orders_dash_api__yaml": [deploy(), hpa]}))   # left for the scaling test


def test_pipeline_gates_and_rollback():
    good = """on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: semgrep scan
      - run: pytest
      - run: ./smoke-test.sh
      - run: helm rollback orders || true
"""
    res = run(bundle(**{"ci__yml": good}))
    assert res["SRE-AT-12-C01"].result == MET and res["SRE-AT-12-C02"].result == MET and res["SRE-AT-12-C05"].result == MET
    res = run(bundle(**{"ci__yml": "on: push\njobs:\n  b:\n    runs-on: x\n    steps:\n      - run: make\n"}))
    assert res["SRE-AT-12-C02"].result == GAP and res["SRE-AT-12-C05"].result == GAP


def test_postmortems_for_p1():
    rows = [inc(1, priority="1 - Critical", problem_id="PRB1"), inc(2, priority="1 - Critical")]
    assert run(bundle(incident__json=rows))["SRE-AT-09-C01"].result == GAP
    assert run(bundle(incident__json=rows[:1]))["SRE-AT-09-C01"].result == MET


@pytest.mark.parametrize("cid", ["CMDB-AT-04-C02", "CMDB-AT-11-C04"])
def test_completeness_feeds_both_v2_criteria(cid):
    from sra.detectors import rubric_rules
    ci = {"name": "Orders", "sys_class_name": "cmdb_ci_business_app", "owned_by": "x", "support_group": "y",
          "operational_status": "Operational", "business_criticality": "2", "environment": "Production", "u_region": "us"}
    rep = rubric_rules.run(bundle(cmdb_ci__csv=[ci]), ctx())
    assert rep.results[cid].result == MET and rep.results[cid].criterion_id == cid
