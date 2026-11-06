"""Render a synthetic assessment scenario from a small spec, and derive its answer key from the spec.

A spec names the application, its profile, a few names used in the data, and a list of DEFECTS to plant.
The renderer starts from a healthy baseline (every one of the 18 rubric rules passes) and applies each
defect. The answer key (expected.yaml) comes only from DEFECTS below, never from running the detectors,
so the eval stays an independent check. tests/test_scenario_kit.py verifies the baseline comes out Ready
and that each defect produces exactly the gaps listed here.

Specs can be written by hand or drafted by a model (generate_scenarios.py). Rendering is deterministic.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

DEFAULT_AS_OF = "2026-11-04T09:00:00"
SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
day = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731

DR_MONTHS = {1: 12, 2: 24, 3: 36}          # config/scoring_matrix.yaml tier_thresholds.dr_test_max_age_months


@dataclass(frozen=True)
class Defect:
    summary: str
    p1: tuple[str, ...] = ()
    p2: tuple[str, ...] = ()
    findings: tuple[tuple[str, str], ...] = ()  # (detector prefix, title substring); "{service}" etc. are filled in
    needs: tuple[str, ...] = ()                 # profile flags that must be true
    tier1_p2: tuple[str, ...] = ()              # extra P2 gaps only for Tier 1 (stricter thresholds)
    periodic_tier2_p2: tuple[str, ...] = ()     # extra P2 gaps for Tier 2 periodic assessments (CMDB-AT-04 / TH-01)


DEFECTS: dict[str, Defect] = {
    # CMDB
    # 6 of 7 required fields = 85.7%: below the rubric's Tier 1 threshold (95%), and below V2 TH-01 for Tier 2 (90%),
    # which applies when CMDB-AT-04 is selected (periodic assessments)
    "cmdb_missing_support_group": Defect("CMDB record has no support group", p1=("P1-01", "P1-02"), tier1_p2=("P2-10",),
                                         periodic_tier2_p2=("P2-10",),
                                         findings=(("rubric:P1-01", "support_group"),)),
    "cmdb_incomplete_attributes": Defect("CMDB record missing region and environment", p2=("P2-10",),
                                         findings=(("rubric:P2-10", "completeness"),)),
    "dependencies_unmapped": Defect("No dependency relationships in CMDB", p2=("P2-01",),
                                    findings=(("rubric:P2-01", "No upstream/downstream"),)),
    "dependencies_stale": Defect("Dependency map not reviewed for over a year", p2=("P2-01",),
                                 findings=(("rubric:P2-01", "unreviewed"),)),
    # Documents
    # SRE-AT-01-C05 (runbooks reviewed within 6 months) also feeds P1-06 through the crosswalk
    "runbook_stale": Defect("Runbook last reviewed about two years ago", p1=("P1-04", "P1-06"), findings=(("rubric:P1-04", "not reviewed"),)),
    "no_slo": Defect("No SLO document", p1=("P1-05",), findings=(("rubric:P1-05", "SLO"),)),
    "error_budget_not_reviewed": Defect("Error budget not reviewed for four months", p2=("P2-03",),
                                        findings=(("rubric:P2-03", "not reviewed"),)),
    "recovery_untested": Defect("Recovery documented but never tested (no DR test)", p1=("P1-06",), p2=("P2-07",),
                                findings=(("rubric:P1-06", "no evidence it has been tested"),)),
    "dr_test_overdue": Defect("Last DR test is older than the tier allows", p2=("P2-07",), findings=(("rubric:P2-07", "not exercised"),)),
    "architecture_no_failure_modes": Defect("Architecture document has no failure-mode analysis", p1=("P1-08",),
                                            findings=(("rubric:P1-08", "fewer than"),)),
    "no_load_test": Defect("No load test results", p2=("P2-05",), findings=(("rubric:P2-05", "No load test"),)),
    "capacity_plan_stale": Defect("Capacity plan ten months old", p2=("P2-06",), findings=(("rubric:P2-06", "not updated"),)),
    # ITSM
    "changes_missing_backout": Defect("Two recent changes have no backout plan", p1=("P1-07",), findings=(("rubric:P1-07", "backout"),)),
    "release_not_validated": Defect("Last release failed and has no validation notes", p2=("P2-08",),
                                    findings=(("rubric:P2-08", "No post-deployment validation"),)),
    "recurring_without_problem": Defect("Recurring incident with no Problem record", p2=("P2-02",),
                                        findings=(("incident_anomalies:recurrence", "{recurring_key}"), ("rubric:P2-02", "recurring"))),
    # The same queue problem worded four ways in four categories: the category and exact-wording rules cannot see it,
    # so no rubric gap is expected; only the text-clustering detector (advisory) reports it.
    "hidden_recurrence": Defect("Same issue reported in different words and categories, no Problem record",
                                findings=(("incident_clusters:recurrence", "similar incidents"),)),
    "problems_without_workaround": Defect("Open Problem with no documented workaround", p2=("P2-09",),
                                          findings=(("rubric:P2-09", "without a documented workaround"),)),
    "no_alerting": Defect("No monitoring alerts; all incidents raised by people", p1=("P1-03",),
                          findings=(("rubric:P1-03", "No monitoring alerts"),)),
    "change_induced_spike": Defect("Burst of incidents right after a release", findings=(("incident_anomalies:change_induced", "after a change"),)),
    "mttr_outlier": Defect("One incident took days to restore", findings=(("incident_anomalies:mttr_outliers", "restore time"),)),
    # CMDB-AT-01-C06 (attribution, TH-03) feeds P2-10 in the V2 CMDB templates
    "poor_ci_linkage": Defect("Most incidents not attributed to the Business Application", p2=("P2-10",),
                              findings=(("incident_anomalies:ci_linkage", "attributed to the Business Application"),)),
    "offsystem_resolution_notes": Defect("Close notes point to OneNote instead of KB",
                                         findings=(("incident_anomalies:pointer_resolutions", "OneNote"),)),
    # Configuration and security
    "no_resilience_config": Defect("No timeouts or retries configured", p2=("P2-04",), findings=(("config_checks:resilience", "P2-04"),)),
    # SRE-AT-05-C04 (redundancy) feeds P2-07 through the crosswalk
    "single_replica": Defect("Deployment runs a single replica", p2=("P2-07",), needs=("containerized",),
                             findings=(("config_checks:replicas", "{service}"),)),
    "no_resource_limits": Defect("No CPU/memory requests or limits", needs=("containerized",), findings=(("config_checks:resources", "{service}"),)),
    "no_probes": Defect("No liveness/readiness probes", needs=("containerized",), findings=(("config_checks:probes", "{service}"),)),
    "no_pdb": Defect("No PodDisruptionBudget", needs=("containerized",), findings=(("config_checks:pdb", "{service}"),)),
    "critical_cves": Defect("Image has critical and high CVEs", findings=(("vulnerabilities:trivy", "CVE-2026-1001"), ("vulnerabilities:trivy", "CVE-2026-1002"))),
    "secret_in_config": Defect("Database password committed in a config file", findings=(("vulnerabilities:secrets", "application.properties"),)),
    "eol_jenkins": Defect("Pipeline runs on end-of-life Jenkins", needs=("has_cicd",), findings=(("vulnerabilities:eol", "jenkins"),)),
    "missing_pipeline_gates": Defect("Pipeline has no dependency, code or secret scanning", needs=("has_cicd",),
                                     findings=(("vulnerabilities:pipeline_gates", "sca"),)),
}

# Pairs that describe the same evidence two ways; a spec may use only one of each.
EXCLUSIVE = (("dependencies_unmapped", "dependencies_stale"), ("recovery_untested", "dr_test_overdue"))

RATING_BANDS = (("Not Ready", lambda p1, p2: p1 > 0), ("At Risk", lambda p1, p2: p2 >= 4),
                ("Conditional", lambda p1, p2: p2 >= 1), ("Ready", lambda p1, p2: True))

PROFILE_FLAGS = ("containerized", "has_cicd", "external_integrations", "cross_team_dependencies", "vendors", "regulated",
                 "batch_or_peak_load", "has_reliability_automation", "sre_toil_work", "uses_pse_queue",
                 "service_catalog_scope", "has_sla")


@dataclass
class Spec:
    slug: str = ""
    name: str = ""
    description: str = ""
    tier: int = 2
    priority_class: str = "P2"
    trigger: str = "periodic"
    hosting: str = "cloud"
    flags: dict = field(default_factory=dict)
    owner_team: str = ""
    support_group: str = ""
    service: str = ""            # Kubernetes deployment / service name
    database: str = ""
    dependency: str = ""         # another application this one calls
    incident_themes: list = field(default_factory=list)   # 4 routine incident short descriptions
    recurring_issue: str = ""
    defects: list = field(default_factory=list)
    as_of: str = DEFAULT_AS_OF

    @classmethod
    def load(cls, data: dict) -> "Spec":
        s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        s.slug = re.sub(r"[^a-z0-9_]+", "_", (s.slug or s.name).lower()).strip("_")
        s.service = re.sub(r"[^a-z0-9-]+", "-", (s.service or s.name).lower()).strip("-")
        s.owner_team = s.owner_team or f"{s.name} Engineering"
        s.support_group = s.support_group or f"{s.name} Support"
        s.database = s.database or f"{s.service}-db"
        s.dependency = s.dependency or "Customer Profile API"
        s.recurring_issue = s.recurring_issue or f"{s.service} nightly reconciliation job failed"
        themes = [t for t in s.incident_themes if t][:4]
        defaults = [f"{s.name} slow response reported by users", f"{s.service} pod restarted by Kubernetes",
                    f"{s.name} login errors for a subset of users", f"{s.database} replication lag alert"]
        s.incident_themes = themes + defaults[len(themes):]
        return s

    def validate(self) -> list[str]:
        errs = [f"unknown defect: {d}" for d in self.defects if d not in DEFECTS]
        errs += [f"choose one of {a} / {b}" for a, b in EXCLUSIVE if a in self.defects and b in self.defects]
        for d in self.defects:
            for need in DEFECTS.get(d, Defect("")).needs:
                if not self.flags.get(need):
                    errs.append(f"defect {d} needs {need}: true")
        if self.tier not in (1, 2, 3):
            errs.append("tier must be 1, 2 or 3")
        if self.hosting not in ("cloud", "on_prem", "hybrid"):
            errs.append("hosting must be cloud, on_prem or hybrid")
        if len(set(self.incident_themes)) < 4 or self.recurring_issue in self.incident_themes:
            errs.append("need 4 distinct incident themes, different from the recurring issue")
        return errs

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def expected(spec: Spec) -> dict:
    """The answer key, derived only from the spec's defect list."""
    p1, p2, seeded = set(), set(), []
    subs = {"service": spec.service, "recurring_key": spec.recurring_issue[:40].lower()}
    for d in spec.defects:
        df = DEFECTS[d]
        p1 |= set(df.p1)
        p2 |= set(df.p2) | (set(df.tier1_p2) if spec.tier == 1 else set())
        p2 |= set(df.periodic_tier2_p2) if spec.tier == 2 and spec.trigger == "periodic" else set()
        seeded += [[det, sub.format(**subs)] for det, sub in df.findings]
    rating = next(name for name, test in RATING_BANDS if test(len(p1), len(p2)))
    always = ["SRE-AT-01", "SRE-AT-03", "SRE-AT-08", "SRE-AT-11", "ITSM-AT-02", "CMDB-AT-01", "CMDB-AT-10"]
    return {"as_of": spec.as_of, "rating": rating, "p1_gaps_include": sorted(p1), "p2_gaps_include": sorted(p2),
            "p1_gaps_exact": True, "templates_include": always, "seeded_findings": seeded,
            "defects": {d: DEFECTS[d].summary for d in spec.defects}}


def profile(spec: Spec, incidents: list[dict]) -> dict:
    return {"name": spec.name, "cmdb_id": "", "aliases": [spec.support_group], "tier": spec.tier,
            "priority_class": spec.priority_class, "trigger": spec.trigger, "hosting": spec.hosting,
            **{f: bool(spec.flags.get(f)) for f in PROFILE_FLAGS},
            "ci_tool": ("jenkins" if "eol_jenkins" in spec.defects else "github_actions") if spec.flags.get("has_cicd") else None,
            "incidents_90d": len(incidents),
            "p1_p2_incidents_90d": sum(1 for i in incidents if i["priority"].startswith(("1", "2"))),
            "recurring_patterns": 1, "eol_components": "eol_jenkins" in spec.defects}


# ------------------------------------------------------------------------------------------ rendering
def render(spec: Spec, out: Path | None = None) -> Path:
    errs = spec.validate()
    if errs:
        raise ValueError("; ".join(errs))
    out = out or SCENARIOS / spec.slug
    ev = out / "evidence"
    if out.exists():
        shutil.rmtree(out)
    ev.mkdir(parents=True)
    has = set(spec.defects).__contains__
    as_of = datetime.fromisoformat(spec.as_of)
    rnd = random.Random(int(hashlib.sha256(spec.slug.encode()).hexdigest()[:8], 16))
    app, svc = spec.name, spec.service
    ago = lambda **kw: as_of - timedelta(**kw)  # noqa: E731

    def csv_out(name, rows):
        with open(ev / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def text_out(name, body):
        (ev / name).write_text(body, encoding="utf-8")

    # ---- CMDB
    ci = {"name": app, "sys_class_name": "cmdb_ci_business_app", "owned_by": spec.owner_team,
          "support_group": "" if has("cmdb_missing_support_group") else spec.support_group,
          "operational_status": "Operational", "business_criticality": {1: "1 - most critical", 2: "2 - somewhat critical", 3: "3 - less critical"}[spec.tier],
          "environment": "" if has("cmdb_incomplete_attributes") else "Production",
          "u_region": "" if has("cmdb_incomplete_attributes") else "us-east"}
    csv_out("cmdb_ci.csv", [ci, {"name": spec.dependency, "sys_class_name": "cmdb_ci_business_app", "owned_by": "Platform Services",
                                 "support_group": "Platform Support", "operational_status": "Operational",
                                 "business_criticality": "2 - somewhat critical", "environment": "Production", "u_region": "us-east"}])
    rel_parent = "Unrelated Reporting App" if has("dependencies_unmapped") else app
    csv_out("cmdb_rel_ci.csv", [{"sys_id": "REL1001", "parent": rel_parent, "child": spec.dependency, "type": "Depends on::Used by",
                                 "sys_updated_on": fmt(ago(days=500 if has("dependencies_stale") else 60))}])

    # ---- Changes: four in the last 90 days at 02:00, all planned and validated
    change_days = [80, 60, 40, 20]
    changes = []
    for i, d in enumerate(change_days):
        start = ago(days=d).replace(hour=2, minute=0, second=0)
        last = i == len(change_days) - 1
        bad_release = last and has("release_not_validated")
        changes.append({"number": f"CHG{810000 + i}", "short_description": f"{app} release 3.{i}", "cmdb_ci": app,
                        "start_date": fmt(start), "end_date": fmt(start + timedelta(hours=1)),
                        "backout_plan": "" if has("changes_missing_backout") and i in (1, 2) else "Redeploy the previous image tag",
                        "test_plan": "Automated regression suite in staging",
                        "close_code": "Unsuccessful" if bad_release else "Successful",
                        "close_notes": "" if bad_release else "Deployed; post-deployment validation and smoke test passed"})
    csv_out("change_request.csv", changes)
    change_ends = [datetime.fromisoformat(c["end_date"]) for c in changes]

    # ---- Incidents
    incidents = []
    humans = ["Priya Shah", "Marcus Lee", "Dana Ortiz"]

    def inc(opened, short, cat, sub, notes, prio="3 - Moderate", hours=1.5, problem="", caller=None, linked=True, caused_by=""):
        n = len(incidents) + 1
        incidents.append({
            "number": f"INC{8100000 + n}", "caller_id": caller or ("Event Management" if n % 2 else humans[n % 3]),
            "short_description": short, "priority": prio, "state": "Closed", "assignment_group": spec.support_group,
            "opened_at": fmt(opened), "resolved_at": fmt(opened + timedelta(hours=hours)),
            "closed_at": fmt(opened + timedelta(hours=hours + 1)), "category": cat, "subcategory": sub,
            "close_code": "Solved (Permanently)", "close_notes": notes, "problem_id": problem,
            "cmdb_ci_business_app": app if linked else "", "caused_by": caused_by})

    def safe_time(days_back):
        t = ago(days=days_back).replace(hour=rnd.randint(8, 18), minute=rnd.randint(0, 59), second=0)
        while any(e <= t <= e + timedelta(hours=30) for e in change_ends):   # keep clear of change windows
            t -= timedelta(days=2)
        return t

    cats = [("Application", "Performance"), ("Application", "Availability"), ("Access", "Authentication"), ("Database", "Replication")]
    notes = ["Scaled the {svc} deployment and confirmed latency back under the SLO threshold.",
             "Identified a memory leak after the pod restart; raised a fix and verified health checks passed.",
             "Cleared the stale session cache and confirmed users could sign in again.",
             "Resynchronised the replica and verified replication lag returned below 5 seconds."]
    pointer = "Rerun as per the OneNote instructions."
    for k, theme in enumerate(spec.incident_themes):                 # 2 per theme over the last 12 weeks
        for j in range(2):
            note = notes[k].format(svc=svc)
            if has("offsystem_resolution_notes") and k < 2:
                note = pointer
            inc(safe_time(7 + k * 9 + j * 38), theme, *cats[k], note, hours=rnd.uniform(0.8, 2.5))
    for j in range(4):                                               # recurring issue, linked to a Problem when healthy
        inc(safe_time(12 + j * 17), spec.recurring_issue, "Batch", "Scheduler",
            "Restarted the job from the failed step and verified the reconciliation totals matched.",
            hours=rnd.uniform(1, 2.5), problem="" if has("recurring_without_problem") else "PRB0071001")
    if has("hidden_recurrence"):
        reworded = [f"{svc} message queue backlog growing", f"Message backlog building up in the {svc} queue",
                    f"{app} queue consumer stopped, message backlog", f"Queue backlog: {app} messages not consumed"]
        where = [("Software", "Messaging"), ("Network", "Connectivity"), ("Inquiry / Help", "General"), ("Application", "Integration")]
        for j, (short, (cat, sub)) in enumerate(zip(reworded, where)):
            inc(safe_time(9 + j * 19), short, cat, sub, "Restarted the queue consumer and confirmed the backlog drained.",
                hours=rnd.uniform(1, 3))
    if has("change_induced_spike"):
        end = change_ends[-1]
        for j in range(6):
            inc(end + timedelta(hours=1 + j * 2), f"{app} errors after release", "Application", "Error",
                "Rolled back the release and confirmed the error rate returned to baseline.", prio="2 - High", hours=2,
                problem="PRB0071002")   # a Problem was raised for the failed release, so only the anomaly fires
    if has("mttr_outlier"):
        inc(safe_time(33), f"{spec.database} storage full", "Database", "Capacity",
            "Expanded the volume, purged old partitions and verified writes succeeded.", prio="2 - High", hours=70)
    if has("no_alerting"):
        for i, r in enumerate(incidents):
            r["caller_id"] = humans[i % 3]
    if has("poor_ci_linkage"):
        for i, r in enumerate(incidents):
            if i % 3:
                r["cmdb_ci_business_app"] = ""
    with open(ev / "incident.json", "w", encoding="utf-8") as f:
        json.dump({"result": incidents}, f, indent=1)

    csv_out("problem.csv", [{"number": "PRB0071001", "short_description": spec.recurring_issue, "state": "Open",
                             "workaround": "" if has("problems_without_workaround") else "Restart the job from the failed step",
                             "known_error": "false" if has("problems_without_workaround") else "true", "cmdb_ci": app}]
            + ([{"number": "PRB0071002", "short_description": f"{app} errors after release", "state": "Resolved",
                 "workaround": "Roll back to the previous release", "known_error": "true", "cmdb_ci": app}]
               if has("change_induced_spike") else []))
    kb_age = 700 if has("runbook_stale") else 60
    csv_out("kb_knowledge.csv", [{"number": "KB0031001", "short_description": f"{app} runbook - recover the reconciliation job",
                                  "kb_category": "Runbook", "sys_updated_on": fmt(ago(days=kb_age)), "u_application": app}])
    if not has("no_alerting"):
        csv_out("em_alert.csv", [{"number": f"Alert{500 + i}", "source": "Dynatrace", "cmdb_ci": app,
                                  "description": f"{app} error rate above threshold", "sys_created_on": fmt(ago(days=2 + i * 4))}
                                 for i in range(6)])

    # ---- Documents
    text_out(f"{svc}-runbook.md",
             f"# {app} Runbook\n\nLast reviewed: {day(ago(days=kb_age))}\nOwner: {spec.support_group}\n\n"
             f"## Recovery steps\n1. Check the {svc} health endpoint.\n2. Restart the failed pod and confirm it becomes ready.\n"
             f"3. If {spec.database} is unavailable, promote the standby and restore service.\n4. Confirm with the support lead.\n")
    if not has("no_slo"):
        text_out(f"{svc}-slo.md",
                 f"# {app} Service Level Objectives\n\nLast reviewed: {day(ago(days=20))}\n\n"
                 f"| SLI | Definition | SLO |\n|---|---|---|\n"
                 f"| Availability | successful requests / valid requests | 99.9% over 28 days |\n"
                 f"| Latency | requests served under 400 ms / valid requests | 95% over 28 days |\n")
    eb_age = 120 if has("error_budget_not_reviewed") else 10
    text_out(f"{svc}-error-budget-policy.md",
             f"# {app} Error Budget Policy\n\nLast reviewed: {day(ago(days=eb_age))}\n\n"
             "This error budget policy applies to the availability objective.\n"
             "Monthly error budget review with the product owner. If the burn rate exceeds 2x for an hour, page the on-call; "
             "if the budget is exhausted, freeze feature releases.\n")
    if has("architecture_no_failure_modes"):
        arch = f"The {svc} service calls {spec.dependency} and stores data in {spec.database}.\n"
    else:
        arch = (f"The {svc} service calls {spec.dependency} and stores data in {spec.database}.\n\n## Failure modes\n"
                f"- Failure mode: {spec.database} primary unavailable. Mitigation: automatic failover to the standby.\n"
                f"- If {spec.dependency} fails, requests use a cached profile as a fallback.\n"
                f"- Failure mode: zone outage. Mitigation: replicas spread across three zones (redundant).\n")
    text_out(f"{svc}-architecture.md", f"# {app} Architecture\n\nUpdated: {day(ago(days=90))}\n\n{arch}")
    text_out(f"{svc}-dr-plan.md",
             f"# {app} Disaster Recovery Plan\n\nLast reviewed: {day(ago(days=100))}\n\nRTO 4 hours, RPO 15 minutes. "
             f"Fail over to the secondary region and restore {spec.database} from the latest snapshot.\n")
    if not has("recovery_untested"):
        months = DR_MONTHS[spec.tier] + 6 if has("dr_test_overdue") else 3
        text_out(f"{svc}-dr-test-results.md",
                 f"# {app} DR test results\n\nDate reviewed: {day(ago(days=months * 30))}\n\n"
                 "Failover test executed; service restored in 52 minutes, within RTO. Recovery test results signed off.\n")
    if not has("no_load_test"):
        text_out(f"{svc}-load-test.md",
                 f"# {app} Load test results\n\nLast updated: {day(ago(days=30))}\n\n"
                 "Peak 850 requests per second for 30 minutes. p95 210 ms, p99 480 ms, error rate 0.05%.\n")
    cap_age = 300 if has("capacity_plan_stale") else 60
    text_out(f"{svc}-capacity-plan.md",
             f"# {app} Capacity plan\n\nLast updated: {day(ago(days=cap_age))}\n\n"
             "Growth forecast 25% per year. Current headroom 40% CPU at peak; next scale-up planned for Q2.\n")

    # ---- Configuration
    if has("no_resilience_config"):
        cfg = f"server:\n  port: 8080\n{svc}:\n  dependency_url: http://{spec.dependency.lower().replace(' ', '-')}\n"
    else:
        cfg = (f"server:\n  port: 8080\n{svc}:\n  dependency_url: http://{spec.dependency.lower().replace(' ', '-')}\n"
               "  http:\n    connect_timeout: 2s\n    read_timeout: 5s\n    retries: 3\n    backoff: exponential\n"
               "  circuit_breaker:\n    failure_rate_threshold: 50\n")
    text_out("application.yaml", cfg)
    if has("secret_in_config"):
        text_out("application.properties", f"db.url=jdbc:postgresql://{spec.database}/app\ndb.password=Winter2026!Prod\n")

    if spec.flags.get("containerized"):
        labels = f"{{app: {svc}}}"
        container = f"        - name: api\n          image: registry.example/{svc}:3.3\n"
        if not has("no_resource_limits"):
            container += ("          resources:\n            requests: {cpu: 250m, memory: 512Mi}\n"
                          "            limits: {cpu: '1', memory: 1Gi}\n")
        if not has("no_probes"):
            container += ("          readinessProbe:\n            httpGet: {path: /ready, port: 8080}\n"
                          "          livenessProbe:\n            httpGet: {path: /health, port: 8080}\n")
        manifest = (f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {svc}\nspec:\n"
                    f"  replicas: {1 if has('single_replica') else 3}\n  selector:\n    matchLabels: {labels}\n"
                    f"  template:\n    metadata:\n      labels: {labels}\n    spec:\n      containers:\n{container}")
        if not has("no_pdb"):
            manifest += (f"---\napiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: {svc}\n"
                         f"spec:\n  minAvailable: 2\n  selector:\n    matchLabels: {labels}\n")
        text_out(f"{svc}-deployment.yaml", manifest)

    if spec.flags.get("has_cicd"):
        gates = not has("missing_pipeline_gates")
        if has("eol_jenkins"):
            steps = ("    stage('Scan') { steps { sh 'dependency-check.sh && semgrep ci && gitleaks detect && trivy image app' } }\n"
                     if gates else "")
            text_out("Jenkinsfile", "pipeline {\n  agent any\n  stages {\n    stage('Build') { steps { sh 'mvn package' } }\n"
                     f"{steps}    stage('Deploy') {{ steps {{ sh './deploy.sh' }} }}\n  }}\n}}\n")
        else:
            scan = ("      - uses: github/codeql-action/analyze@v3\n      - run: dependency-check.sh --project app\n"
                    "      - run: gitleaks detect --no-banner\n      - run: trivy image registry.example/app:latest\n") if gates else ""
            text_out("ci.yml", "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                     f"      - uses: actions/checkout@v4\n      - run: mvn package\n{scan}      - run: ./deploy.sh\n")

    vulns = []
    if has("critical_cves"):
        vulns = [{"VulnerabilityID": "CVE-2026-1001", "PkgName": "openssl", "InstalledVersion": "3.0.11", "FixedVersion": "3.0.16",
                  "Severity": "CRITICAL", "Title": "OpenSSL buffer overflow"},
                 {"VulnerabilityID": "CVE-2026-1002", "PkgName": "spring-web", "InstalledVersion": "6.1.2", "FixedVersion": "6.1.14",
                  "Severity": "HIGH", "Title": "Spring request smuggling"}]
    with open(ev / f"trivy-{svc}.json", "w", encoding="utf-8") as f:
        json.dump({"SchemaVersion": 2, "ArtifactName": f"registry.example/{svc}:3.3",
                   "Results": [{"Target": f"{svc} (debian 12)", "Vulnerabilities": vulns}]}, f, indent=1)

    # ---- profile, answer key, spec
    with open(out / "profile.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(profile(spec, incidents), f, sort_keys=False)
    with open(out / "expected.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(expected(spec), f, sort_keys=False)
    with open(out / "spec.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(spec.to_dict(), f, sort_keys=False, allow_unicode=True)
    return out
