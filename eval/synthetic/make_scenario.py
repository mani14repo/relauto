"""Generate a synthetic assessment scenario with seeded defects (no real ticket data).

    python eval/synthetic/make_scenario.py            # -> eval/scenarios/orders_batch/

The expected outcomes live in eval/scenarios/orders_batch/expected.yaml and are used by
tests/test_detectors.py and the golden eval (eval/run_eval.py).
"""
from __future__ import annotations

import csv
import json
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import yaml

AS_OF = datetime(2026, 11, 4, 9, 0, 0)
APP = "Orders Batch"
OUT = Path(__file__).resolve().parents[1] / "scenarios" / "orders_batch"
EVID = OUT / "evidence"  # the files to upload; profile.yaml (form answers) and expected.yaml (answer key) stay in OUT
fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")


def write_csv(name, rows):
    with open(EVID / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    rnd = random.Random(7)
    if OUT.exists():
        shutil.rmtree(OUT)  # generated output; start clean so no stale files remain
    EVID.mkdir(parents=True)

    # CMDB: record exists, owner set, support_group EMPTY (seeded: P1-01, P1-02, P2-10)
    write_csv("cmdb_ci.csv", [
        {"name": APP, "sys_class_name": "cmdb_ci_business_app", "owned_by": "Orders Engineering", "support_group": "",
         "operational_status": "Operational", "business_criticality": "1 - most critical", "environment": "Production",
         "u_region": "", "version": ""},
        {"name": "Billing API", "sys_class_name": "cmdb_ci_business_app", "owned_by": "Billing", "support_group": "Billing Support",
         "operational_status": "Operational", "business_criticality": "2", "environment": "Production", "u_region": "A", "version": ""},
    ])
    write_csv("cmdb_rel_ci.csv", [
        {"sys_id": "REL001", "parent": APP, "child": "Billing API", "type": "Depends on::Used by", "sys_updated_on": fmt(AS_OF - timedelta(days=500))},
    ])

    # Incidents: baseline noise, a recurring abend with no problem (P2-02), a change-induced spike, pointer close notes
    incidents, n = [], 0

    def inc(opened, short, cat, sub, close_notes, problem="", caller="Event Management", prio="3 - Moderate", hours=2.0, caused_by=""):
        nonlocal n
        n += 1
        incidents.append({
            "number": f"INC{9000000 + n}", "caller_id": caller, "short_description": short, "priority": prio,
            "state": "Closed", "assignment_group": "Orders Support", "opened_at": fmt(opened),
            "resolved_at": fmt(opened + timedelta(hours=hours)), "closed_at": fmt(opened + timedelta(hours=hours + 1)),
            "category": cat, "subcategory": sub, "close_code": "Solved (Permanently)", "close_notes": close_notes,
            "problem_id": problem, "cmdb_ci_business_app": APP if rnd.random() > 0.2 else "", "caused_by": caused_by,
            "comments_and_work_notes": f"{fmt(opened)} - Jane Doe (Work notes)\nRestarted the job. password=hunter2\n",
        })

    for w in range(12):                                  # steady 2/week background
        for _ in range(2):
            t = AS_OF - timedelta(weeks=w, days=rnd.randint(0, 6), hours=rnd.randint(0, 20))
            inc(t, "URL https://orders.example/health tested DOWN", "Application", "Availability",
                "Restarted the orders-api pod and verified health endpoint returned 200 OK.", hours=rnd.uniform(0.5, 3))
    for i in range(6):                                   # recurring abend, no problem record
        t = AS_OF - timedelta(days=10 + i * 12)
        inc(t, "TWS job OR_SETTLE1J abended RC=12", "Batch", "Scheduler", "Rerun as per the OneNote instructions.", hours=5)
    change_end = AS_OF - timedelta(days=15)
    for i in range(9):                                   # change-induced spike in one week
        inc(change_end + timedelta(hours=1 + i * 2), "Orders API 500 errors after release", "Application", "Error",
            "Rolled back release 4.2 and the error rate returned to baseline after cache warm-up.", prio="2 - High", hours=3)
    inc(AS_OF - timedelta(days=40), "Orders DB connection pool exhausted", "Database", "Performance",
        "Increased pool size from 20 to 50 and restarted the service; monitored for 2 hours.", prio="2 - High", hours=60)
    with open(EVID / "incident.json", "w", encoding="utf-8") as f:
        json.dump({"result": incidents}, f, indent=1)

    # Changes: 5 in 90d, 2 missing backout plan (P1-07); last release not validated (P2-08)
    changes = []
    for i in range(5):
        start = AS_OF - timedelta(days=80 - i * 16) if i < 4 else change_end - timedelta(hours=1)
        changes.append({"number": f"CHG{700000 + i}", "short_description": f"{APP} release 4.{i}", "cmdb_ci": APP,
                        "start_date": fmt(start), "end_date": fmt(start + timedelta(hours=1)),
                        "backout_plan": "" if i in (1, 3) else "Redeploy previous image tag",
                        "test_plan": "Smoke tests in staging", "close_code": "Successful" if i < 4 else "Unsuccessful",
                        "close_notes": "" if i == 4 else "Deployed"})
    write_csv("change_request.csv", changes)

    write_csv("problem.csv", [
        {"number": "PRB0040001", "short_description": "Orders DB pool exhaustion", "state": "Open", "workaround": "",
         "known_error": "false", "cmdb_ci": APP},
    ])
    write_csv("kb_knowledge.csv", [
        {"number": "KB0012001", "short_description": "Orders Batch runbook - restart settlement job", "kb_category": "Runbook",
         "sys_updated_on": fmt(AS_OF - timedelta(days=700)), "u_application": APP},
    ])
    write_csv("em_alert.csv", [
        {"number": f"Alert{100 + i}", "source": "AppDynamics", "cmdb_ci": APP, "description": "Orders API error rate",
         "sys_created_on": fmt(AS_OF - timedelta(days=i * 3))} for i in range(6)
    ])

    # Documents
    (EVID / "orders-batch-runbook.md").write_text(
        "# Orders Batch Runbook\n\nLast reviewed: 2025-01-10\n\n## Recovery steps\n1. Restart the settlement job.\n"
        "2. Verify the output file.\n", encoding="utf-8")
    (EVID / "orders-architecture.md").write_text(
        "# Orders Architecture\n\nUpdated: 2026-06-01\n\nThe orders API calls Billing API and the Orders DB.\n"
        "Single point of failure: the Orders DB primary.\n", encoding="utf-8")

    # Kubernetes: single replica, no limits, no probes, no PDB
    (EVID / "orders-api-deployment.yaml").write_text("""apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
spec:
  replicas: 1
  selector:
    matchLabels: {app: orders-api}
  template:
    metadata:
      labels: {app: orders-api}
    spec:
      containers:
        - name: api
          image: registry.example/orders-api:4.4
          env:
            - name: BILLING_URL
              value: http://billing-api
""", encoding="utf-8")
    (EVID / "application.properties").write_text("db.url=jdbc:postgresql://orders-db/orders\ndb.password=SuperSecret123\n",
                                                encoding="utf-8")

    # CI: Jenkins without security gates
    (EVID / "Jenkinsfile").write_text("pipeline {\n  agent any\n  stages {\n    stage('Build') { steps { sh 'mvn package' } }\n"
                                     "    stage('Deploy') { steps { sh './deploy.sh' } }\n  }\n}\n", encoding="utf-8")

    # Scanner output (Trivy)
    trivy = {"SchemaVersion": 2, "ArtifactName": "registry.example/orders-api:4.4", "Results": [{
        "Target": "orders-api (debian 12)", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2026-0001", "PkgName": "openssl", "InstalledVersion": "3.0.11", "FixedVersion": "3.0.15",
             "Severity": "CRITICAL", "Title": "OpenSSL remote code execution"},
            {"VulnerabilityID": "CVE-2026-0002", "PkgName": "log4j-core", "InstalledVersion": "2.17.0", "FixedVersion": "2.17.2",
             "Severity": "HIGH", "Title": "Log4j JNDI lookup"},
            {"VulnerabilityID": "CVE-2026-0003", "PkgName": "jackson-databind", "InstalledVersion": "2.13.0", "FixedVersion": "",
             "Severity": "HIGH", "Title": "Deserialization flaw"},
            {"VulnerabilityID": "CVE-2026-0004", "PkgName": "zlib", "InstalledVersion": "1.2.13", "FixedVersion": "1.3.1",
             "Severity": "LOW", "Title": "Minor zlib issue"},
        ]}]}
    with open(EVID / "trivy-orders-api.json", "w", encoding="utf-8") as f:
        json.dump(trivy, f, indent=1)

    profile = {"name": APP, "cmdb_id": "", "aliases": ["Orders Support"], "tier": 1, "priority_class": "P1", "trigger": "major_change", "hosting": "hybrid",
               "containerized": True, "has_cicd": True, "ci_tool": "jenkins", "external_integrations": True,
               "cross_team_dependencies": True, "batch_or_peak_load": True, "incidents_90d": len(incidents),
               "p1_p2_incidents_90d": 10, "recurring_patterns": 1, "has_sla": True, "uses_pse_queue": True}
    with open(OUT / "profile.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(profile, f, sort_keys=False)

    expected = {
        "as_of": AS_OF.isoformat(),
        "rating": "Not Ready",
        "p1_gaps_include": ["P1-01", "P1-02", "P1-04", "P1-05", "P1-06", "P1-07", "P1-08"],
        "p2_gaps_include": ["P2-01", "P2-02", "P2-04", "P2-05", "P2-07", "P2-08", "P2-09", "P2-10"],
        "templates_include": ["SRE-AT-01", "SRE-AT-05", "SRE-AT-06", "SRE-AT-07", "SRE-AT-09", "SRE-AT-11", "SRE-AT-12",
                              "SRE-AT-13", "ITSM-AT-01", "ITSM-AT-03", "CMDB-AT-06"],
        "seeded_findings": [   # (detector prefix, substring of title) - every one must be detected
            ["vulnerabilities:trivy", "CVE-2026-0001"],
            ["vulnerabilities:trivy", "CVE-2026-0002"],
            ["vulnerabilities:trivy", "CVE-2026-0003"],
            ["vulnerabilities:trivy", "CVE-2026-0004"],
            ["vulnerabilities:secrets", "application.properties"],
            ["vulnerabilities:eol", "jenkins"],
            ["vulnerabilities:pipeline_gates", "sca"],
            ["config_checks:replicas", "orders-api"],
            ["config_checks:resources", "orders-api"],
            ["config_checks:probes", "orders-api"],
            ["config_checks:resilience", "P2-04"],
            ["incident_anomalies:recurrence", "or_settle1j"],
            ["incident_anomalies:change_induced", "after a change"],
            ["incident_anomalies:pointer_resolutions", "OneNote"],
            ["incident_anomalies:ci_linkage", "attributed to the Business Application"],
            ["incident_anomalies:mttr_outliers", "restore time"],
            ["rubric:P1-04", "not reviewed"],
            ["rubric:P1-07", "backout"],
        ],
    }
    with open(OUT / "expected.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(expected, f, sort_keys=False)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
