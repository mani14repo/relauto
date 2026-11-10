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

DR_MONTHS = {1: 12, 2: 24, 3: 36}  # config/scoring_matrix.yaml tier_thresholds.dr_test_max_age_months


@dataclass(frozen=True)
class Defect:
    summary: str
    p1: tuple[str, ...] = ()
    p2: tuple[str, ...] = ()
    findings: tuple[tuple[str, str], ...] = ()  # (detector prefix, title substring); "{service}" etc. are filled in
    needs: tuple[str, ...] = ()  # profile flags that must be true
    tier1_p2: tuple[str, ...] = ()  # extra P2 gaps only for Tier 1 (stricter thresholds)
    periodic_tier2_p2: tuple[str, ...] = ()  # extra P2 gaps for Tier 2 periodic assessments (CMDB-AT-04 / TH-01)
    triggers: tuple[str, ...] = ()  # the defect's template is only selected for these triggers
    hostings: tuple[str, ...] = ()  # ... or these hosting types


DEFECTS: dict[str, Defect] = {
    # CMDB
    # 6 of 7 required fields = 85.7%: below the rubric's Tier 1 threshold (95%), and below V2 TH-01 for Tier 2 (90%),
    # which applies when CMDB-AT-04 is selected (periodic assessments)
    "cmdb_missing_support_group": Defect("CMDB record has no support group", p1=("P1-01", "P1-02"), tier1_p2=("P2-10",),
                                         periodic_tier2_p2=("P2-10",),
                                         findings=(("rubric:P1-01", "support_group"),)),
    "cmdb_incomplete_attributes": Defect("CMDB record missing environment, operational status, class and region", p2=("P2-10",),
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
    "change_induced_spike": Defect("Burst of incidents right after a release",
                                   findings=(("incident_anomalies:change_induced", "after a change"),)),
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
    "no_resource_limits": Defect("No CPU/memory requests or limits", needs=("containerized",),
                                 findings=(("config_checks:resources", "{service}"),)),
    "no_probes": Defect("No liveness/readiness probes", needs=("containerized",), findings=(("config_checks:probes", "{service}"),)),
    "no_pdb": Defect("No PodDisruptionBudget", needs=("containerized",), findings=(("config_checks:pdb", "{service}"),)),
    "critical_cves": Defect("Image has critical and high CVEs",
                            findings=(("vulnerabilities:trivy", "CVE-2026-1001"), ("vulnerabilities:trivy", "CVE-2026-1002"))),
    "secret_in_config": Defect("Database password committed in a config file",
                               findings=(("vulnerabilities:secrets", "application.properties"),)),
    "eol_jenkins": Defect("Pipeline runs on end-of-life Jenkins", needs=("has_cicd",), findings=(("vulnerabilities:eol", "jenkins"),)),
    "missing_pipeline_gates": Defect("Pipeline has no dependency, code or secret scanning", needs=("has_cicd",),
                                     findings=(("vulnerabilities:pipeline_gates", "sca"),)),
    # Operational data (ServiceNow service-model, SLA and catalog tables; team registers). Each breaks one criterion
    # that sra.detectors.data_rules computes; any finding linked to that criterion counts (detector "" = any).
    "app_service_misnamed": Defect("Production Application Service named the same as the Business Application",
                                   p1=("P1-01",), p2=("P2-10",), findings=(("", "CMDB-AT-01-C04"),)),
    "stale_cloud_cis": Defect("Cloud VM not discovered for weeks but still Installed", p2=("P2-10",), hostings=("cloud", "hybrid"),
                              findings=(("", "CMDB-AT-07-C04"),)),
    "attestation_overdue": Defect("Application Services not attested within the tier interval", p2=("P2-10",), triggers=("periodic",),
                                  findings=(("", "CMDB-AT-11-C01"),)),
    "exceptions_expired": Defect("Policy exception past its expiry date", triggers=("periodic",),
                                 findings=(("", "CMDB-AT-11-C05"),)),
    "remediation_tasks_unowned": Defect("CMDB remediation tasks overdue and unowned", triggers=("periodic",),
                                        findings=(("", "CMDB-AT-04-C05"),)),
    "problem_fix_slow": Defect("P2 Problem took a month from root cause to fix", findings=(("", "ITSM-AT-03-C04"),)),
    "sla_breached": Defect("Incident SLAs breached on a quarter of incidents", needs=("has_sla",),
                           findings=(("", "ITSM-AT-06-C02"),)),
    "corrective_actions_overdue": Defect("Corrective actions overdue and not escalated", needs=("has_sla",),
                                         findings=(("", "ITSM-AT-06-C05"),)),
    "kb_migration_late": Defect("OneNote procedures not migrated within 60 days", triggers=("periodic",),
                                findings=(("", "ITSM-AT-07-C03"),)),
    "improvement_register_incomplete": Defect("Improvement opportunities without owner or score", triggers=("periodic",),
                                              findings=(("", "ITSM-AT-08-C01"),)),
    "access_review_overdue": Defect("Production access not reviewed for seven months", findings=(("", "SRE-AT-11-C05"),)),
    "vendor_concentration_unassessed": Defect("Vendor supporting 7 applications with no concentration risk assessment",
                                              needs=("vendors",), findings=(("", "ITSM-AT-10-C05"),)),
    "toil_rising": Defect("Toil rising quarter over quarter", needs=("sre_toil_work",), triggers=("periodic",),
                          findings=(("", "SRE-AT-10-C05"),)),
    "automation_unowned": Defect("Production automation with no owner activity in months", needs=("has_reliability_automation",),
                                 findings=(("", "SRE-AT-14-C04"),)),
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
    service: str = ""  # Kubernetes deployment / service name
    database: str = ""
    dependency: str = ""  # another application this one calls
    incident_themes: list = field(default_factory=list)  # 4 routine incident short descriptions
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
            df = DEFECTS.get(d, Defect(""))
            for need in df.needs:
                if not self.flags.get(need):
                    errs.append(f"defect {d} needs {need}: true")
            if df.triggers and self.trigger not in df.triggers:
                errs.append(f"defect {d} needs trigger: {' or '.join(df.triggers)}")
            if df.hostings and self.hosting not in df.hostings:
                errs.append(f"defect {d} needs hosting: {' or '.join(df.hostings)}")
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
          # cmdb_incomplete_attributes blanks three of the rubric's P2-10 fields (app CI + its direct child < 85%)
          "operational_status": "" if has("cmdb_incomplete_attributes") else "Operational",
          "business_criticality": {1: "1 - most critical", 2: "2 - somewhat critical", 3: "3 - less critical"}[spec.tier],
          "environment": "" if has("cmdb_incomplete_attributes") else "Production",
          "sys_class_name": "" if has("cmdb_incomplete_attributes") else "cmdb_ci_business_app",
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
                        "close_notes": "" if bad_release else "Deployed; post-deployment validation and smoke test passed",
                        "type": "Normal", "conflict_status": "No Conflict", "approval_set": fmt(start - timedelta(days=1))})
    csv_out("change_request.csv", changes)
    change_ends = [datetime.fromisoformat(c["end_date"]) for c in changes]

    # ---- Incidents
    incidents = []
    humans = ["Priya Shah", "Marcus Lee", "Dana Ortiz"]

    def inc(opened, short, cat, sub, notes, prio=None, hours=1.5, problem="", caller=None, linked=True, caused_by=""):
        n = len(incidents) + 1
        incidents.append({
            "number": f"INC{8100000 + n}", "caller_id": caller or ("Event Management" if n % 2 else humans[n % 3]),
            "short_description": short, "priority": prio or std10_priority(short, sub), "state": "Closed", "assignment_group": spec.support_group,
            "opened_at": fmt(opened), "resolved_at": fmt(opened + timedelta(hours=hours)),
            "closed_at": fmt(opened + timedelta(hours=hours + 1)), "category": cat, "subcategory": sub,
            "close_code": "Solved (Permanently)", "close_notes": notes, "problem_id": problem,
            "cmdb_ci_business_app": app if linked else "", "caused_by": caused_by})

    def safe_time(days_back):
        t = ago(days=days_back).replace(hour=rnd.randint(8, 18), minute=rnd.randint(0, 59), second=0)
        while any(e <= t <= e + timedelta(hours=30) for e in change_ends):  # keep clear of change windows
            t -= timedelta(days=2)
        return t

    def std10_priority(short: str, sub: str) -> str:
        """A healthy service logs an outage/degradation at the priority CMDB-STD-10 R06 sets for its tier (Tier 1:
        outage P1, degradation P2; Tier 2: P2/P3; Tier 3: P3/P4), never lower than P3 for routine work here."""
        from sra.config import load_yaml
        from sra.detectors.base import Context
        from sra.detectors.criteria_rules import incident_impact
        c = Context(app_name=app, aliases=[], as_of=as_of, cfg=load_yaml("detectors"), tier=spec.tier)
        kind = incident_impact({"short_description": short, "subcategory": sub}, c)
        want = c.cfg["criteria_rules"]["incident_priority_by_tier"][spec.tier]
        p = min(3, want[kind]) if kind else 3
        return {1: "1 - Critical", 2: "2 - High", 3: "3 - Moderate"}[p]

    cats = [("Application", "Performance"), ("Application", "Availability"), ("Access", "Authentication"), ("Database", "Replication")]
    notes = ["Scaled the {svc} deployment and confirmed latency back under the SLO threshold.",
             "Identified a memory leak after the pod restart; raised a fix and verified health checks passed.",
             "Cleared the stale session cache and confirmed users could sign in again.",
             "Resynchronised the replica and verified replication lag returned below 5 seconds."]
    pointer = "Rerun as per the OneNote instructions."
    for k, theme in enumerate(spec.incident_themes):  # 2 per theme over the last 12 weeks
        for j in range(2):
            note = notes[k].format(svc=svc)
            if has("offsystem_resolution_notes") and k < 2:
                note = pointer
            inc(safe_time(7 + k * 9 + j * 38), theme, *cats[k], note, hours=rnd.uniform(0.8, 2.5))
    for j in range(4):  # recurring issue, linked to a Problem when healthy
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
                problem="PRB0071002")  # a Problem was raised for the failed release, so only the anomaly fires
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

    no_dates = {"priority": "3 - Moderate", "u_root_cause_date": "", "resolved_at": "", "rfc": ""}
    fix_days = 30 if has("problem_fix_slow") else 6
    csv_out("problem.csv", [{"number": "PRB0071001", "short_description": spec.recurring_issue, "state": "Open",
                             "workaround": "" if has("problems_without_workaround") else "Restart the job from the failed step",
                             "known_error": "false" if has("problems_without_workaround") else "true", "cmdb_ci": app, **no_dates}]
            + ([{"number": "PRB0071002", "short_description": f"{app} errors after release", "state": "Resolved",
                 "workaround": "Roll back to the previous release", "known_error": "true", "cmdb_ci": app,
                 **no_dates, "priority": "2 - High", "rfc": changes[-1]["number"]}]
               if has("change_induced_spike") else [])
            + [{"number": "PRB0071003", "short_description": f"{spec.database} connection pool exhausted under load",
                "state": "Resolved", "workaround": "Restart the connection pool", "known_error": "true", "cmdb_ci": app,
                "priority": "2 - High", "u_root_cause_date": fmt(ago(days=40)), "resolved_at": fmt(ago(days=40 - fix_days)),
                "rfc": changes[1]["number"]}])
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

    _operational_data(spec, ev, has, as_of, changes, incidents, csv_out)

    # ---- profile, answer key, spec
    with open(out / "profile.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(profile(spec, incidents), f, sort_keys=False)
    with open(out / "expected.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(expected(spec), f, sort_keys=False)
    with open(out / "spec.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(spec.to_dict(), f, sort_keys=False, allow_unicode=True)
    return out


# Operational Data
def _quarter_start(d: datetime, back: int = 0) -> datetime:
    q = (d.month - 1) // 3 - back
    return datetime(d.year + q // 4, (q % 4) * 3 + 1, 1)


def _write_xlsx(path: Path, rows: list[dict], title: str) -> None:
    """A register as a real spreadsheet: a title row above the table, as teams often keep them."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Register"
    ws.append([title])
    ws.append(list(rows[0]))
    for r in rows:
        ws.append([r[k] for k in rows[0]])
    wb.save(path)


def _operational_data(spec: Spec, ev: Path, has, as_of: datetime, changes: list[dict], incidents: list[dict], csv_out) -> None:
    """ServiceNow service-model, discovery, audit, SLA and catalog exports plus team registers (sra.detectors.data_rules).

    Deterministic (no random numbers), so adding it leaves every other generated file as it was. Healthy unless a
    defect below says otherwise.
    """
    app, svc, sg, owner = spec.name, spec.service, spec.support_group, spec.owner_team
    ago = lambda **kw: as_of - timedelta(**kw)  # noqa: E731
    tier_text = {1: "1 - most critical", 2: "2 - somewhat critical", 3: "3 - less critical"}[spec.tier]
    prod_svc = app if has("app_service_misnamed") else f"{app} - Production"
    attested = ago(days=500 if has("attestation_overdue") else 30)

    # ---- service model (CSDM)
    csv_out("cmdb_ci_service_auto.csv", [
        {"name": prod_svc, "environment": "Production", "busines_criticality": tier_text, "u_tier_approved_by": "REO Governance Board",
         "attested_date": fmt(attested), "owned_by": owner, "support_group": sg},
        {"name": f"{app} - UAT", "environment": "UAT", "busines_criticality": tier_text, "u_tier_approved_by": "REO Governance Board",
         "attested_date": fmt(attested), "owned_by": owner, "support_group": sg}])
    offerings = [f"{app} Business Offering", f"{app} Platform Technical Offering"]
    csv_out("service_offering.csv", [{"name": offerings[0], "service_classification": "Business Service", "owned_by": owner},
                                     {"name": offerings[1], "service_classification": "Technical Service", "owned_by": owner}])
    csv_out("cmdb_rel_ci_service_model.csv",
            [{"sys_id": "REL2001", "parent": app, "child": prod_svc, "type": "Consumes::Consumed by", "sys_updated_on": fmt(ago(days=30))},
             {"sys_id": "REL2002", "parent": app, "child": f"{app} - UAT", "type": "Consumes::Consumed by",
              "sys_updated_on": fmt(ago(days=30))}]
            + [{"sys_id": f"REL20{3 + i}", "parent": o, "child": prod_svc, "type": "Depends on::Used by",
                "sys_updated_on": fmt(ago(days=30))} for i, o in enumerate(offerings)])

    # ---- hosting CIs, discovered (classes other than the Business Application)
    hosts = [(f"{svc}-vm01", "cmdb_ci_linux_server"), (f"{svc}-vm02", "cmdb_ci_linux_server"),
             (spec.database, "cmdb_ci_db_postgresql_instance"), (f"{svc}-gce01", "cmdb_ci_vm_instance")]
    if spec.flags.get("containerized"):
        hosts.append((f"{svc}-ns", "cmdb_ci_kubernetes_namespace"))
    seen = {n: ago(days=1) for n, _ in hosts}
    if has("stale_cloud_cis"):
        seen[f"{svc}-gce01"] = ago(days=30)
    csv_out("cmdb_ci_hosting.csv", [{"name": n, "sys_class_name": c, "environment": "Production", "install_status": "Installed",
                                     "operational_status": "Operational", "support_group": sg, "used_for": "Production",
                                     "discovery_source": "ServiceNow", "last_discovered": fmt(seen[n]), "duplicate_of": ""}
                                    for n, c in hosts])
    csv_out("svc_ci_assoc.csv", [{"service_id": prod_svc, "ci_id": n} for n, _ in hosts])
    csv_out("alm_hardware.csv", [{"asset_tag": f"AST-{i:04d}", "ci": n, "install_status": "In use"}
                                 for i, (n, c) in enumerate(hosts) if "server" in c])
    csv_out("cmdb_identifier.csv", [{"name": f"{c} identification", "applies_to": c, "active": "true"}
                                    for c in sorted({c for _, c in hosts})])
    csv_out("reconcile_duplicate_task.csv", [{"number": "DUP0001001", "state": "Closed Complete",
                                              "sys_created_on": fmt(ago(days=50)), "short_description": f"Duplicate {svc}-vm01"}])
    csv_out("discovery_schedule.csv", [{"name": "GCP project sweep", "active": "true"}, {"name": "Linux servers", "active": "true"}])
    csv_out("discovery_status.csv",
            [{"number": f"DIS{900 + i}", "dscheduler": s, "state": "Completed", "sys_created_on": fmt(ago(days=1 + i))}
             for i, s in enumerate(["GCP project sweep", "Linux servers"])])
    csv_out("discovery_device_history.csv", [{"source": n, "status": "Completed", "sys_created_on": fmt(ago(days=1))} for n, _ in hosts])
    csv_out("discovery_credentials.csv",
            [{"name": "linux-ssh", "active": "true", "credential_storage_id": "CyberArk", "u_owner": "Platform Team",
              "sys_updated_on": fmt(ago(days=30))}])
    csv_out("cmdb_sam_sw_install.csv", [{"installed_on": f"{svc}-vm0{i}", "display_name": sw, "norm_product": sw, "software_model": sw}
                                        for i in (1, 2) for sw in ("OpenJDK 21", "Nginx 1.26")])
    csv_out("cmdb_software_product_model.csv", [{"name": "OpenJDK 21", "end_of_life_date": "2031-09-30"},
                                                {"name": "Nginx 1.26", "end_of_life_date": "2027-04-30"}])

    # ---- change: impacted services before approval, CI links, audited state change inside a change window
    csv_out("task_cmdb_ci_service.csv", [{"task": c["number"], "cmdb_ci_service": prod_svc,
                                          "sys_created_on": fmt(datetime.fromisoformat(c["start_date"]) - timedelta(days=2))} for c in
                                         changes])
    csv_out("task_ci.csv", [{"task": changes[-1]["number"], "ci_item": f"{svc}-vm02"}])
    csv_out("sys_audit.csv", [{"tablename": "cmdb_ci_linux_server", "documentkey": f"{svc}-vm02", "fieldname": "operational_status",
                               "oldvalue": "Operational", "newvalue": "Repair in Progress",
                               "sys_created_on": fmt(datetime.fromisoformat(changes[-1]["start_date"]) + timedelta(minutes=20))}])

    # ---- SLAs and catalog
    csv_out("contract_sla.csv", [{"name": f"P{p} {k}", "collection": "incident", "type": "SLA", "target": k, "active": "true",
                                  "start_condition": f"priority={p}"} for p in (1, 2, 3, 4) for k in ("response", "resolution")])
    breached = {r["number"] for r in incidents[: len(incidents) // 4]} if has("sla_breached") else set()
    csv_out("task_sla.csv", [{"task": r["number"], "task.priority": r["priority"], "sla": f"P{r['priority'][0]} resolution",
                              "has_breached": "true" if r["number"] in breached else "false", "stage": "Completed"} for r in incidents])
    csv_out("sc_cat_item.csv", [{"name": f"Request access to {app}", "active": "true", "group": sg, "owner": owner,
                                 "u_sla_target": "2 business days", "cmdb_ci": app, "category": "Access"}])

    # ---- registers
    qs_last, qs_now = _quarter_start(as_of, 1), _quarter_start(as_of)
    d = lambda x: x.strftime("%Y-%m-%d")  # noqa: E731
    risks = [{"Risk ID": "R-001", "Description": f"{spec.database} single region", "Likelihood": "Medium", "Impact": "High",
              "Risk Rating": "High", "Mitigation": "Cross-region replica", "Owner": owner, "Target Date": d(ago(days=-60)),
              "Status": "Open", "Application": app, "Accepted By": "", "Acceptance Expiry": ""},
             {"Risk ID": "R-002", "Description": "Jenkins agents near end of support", "Likelihood": "Low", "Impact": "Medium",
              "Risk Rating": "Medium", "Mitigation": "Move to GitHub Actions", "Owner": owner, "Target Date": d(ago(days=-30)),
              "Status": "Open", "Application": app, "Accepted By": "", "Acceptance Expiry": ""},
             {"Risk ID": "R-003", "Description": "Legacy batch scheduler upgrade deferred", "Likelihood": "Low", "Impact": "Low",
              "Risk Rating": "Low", "Mitigation": "Upgrade in next release train", "Owner": owner, "Target Date": d(ago(days=-120)),
              "Status": "Deferred", "Application": app, "Accepted By": "REO Risk Committee", "Acceptance Expiry": d(ago(days=-90))}]
    _write_xlsx(ev / "risk_register.xlsx", risks, f"{app} risk register")
    csv_out("exception_register.csv", [{"ID": "EX-001", "Description": "TLS 1.2 on internal batch endpoint", "Approved By": "CISO delegate",
                                        "Expiry Date": d(ago(days=20 if has("exceptions_expired") else -120)),
                                        "Compensating Control": "Network segmentation", "Application": app}])
    ios = [{"ID": f"IO-{i:02d}", "Title": t, "Owner": "" if has("improvement_register_incomplete") and i == 1 else owner,
            "Score": "" if has("improvement_register_incomplete") and i == 1 else str(sc), "Planned Date": pd, "Completed Date": cd,
            "Status": st, "Baseline": bl, "Result": rs, "Application": app}
           for i, (t, sc, pd, cd, st, bl, rs) in enumerate([
            ("Automate failover drill", 90, d(qs_now + timedelta(days=40)), "", "In progress", "", ""),
            ("Alert noise reduction", 85, d(qs_now + timedelta(days=50)), "", "Planned", "", ""),
            ("Runbook automation for restarts", 70, d(qs_now + timedelta(days=60)), "", "Planned", "", ""),
            ("Capacity forecast in dashboards", 60, d(qs_now + timedelta(days=70)), "", "Planned", "", ""),
            ("Close-note quality check", 55, d(qs_now + timedelta(days=80)), "", "Planned", "", ""),
            ("Dependency map refresh", 50, d(qs_last + timedelta(days=40)), d(qs_last + timedelta(days=35)), "Done",
             "MTTR 3.1 h", "MTTR 2.2 h")], 1)]
    csv_out("improvement_register.csv", ios)
    big = [f"App {n}" for n in "ABCDEF"] + [app]
    csv_out("vendor_register.csv", [
        {"Vendor": "CloudCo", "Services Provided": "Managed PostgreSQL", "Dependent Applications": ", ".join(big), "Critical": "Yes",
         "Concentration Risk": "no" if has("vendor_concentration_unassessed") else "Yes",
         "Mitigation": "" if has("vendor_concentration_unassessed") else "Exit plan to self-managed PostgreSQL"},
        {"Vendor": "PagerCo", "Services Provided": "On-call paging", "Dependent Applications": app, "Critical": "Yes",
         "Concentration Risk": "No", "Mitigation": ""}])
    csv_out("contract_register.csv", [{"Vendor": v, "Critical": "Yes", "Contract End": d(ago(days=-400)), "SLA": "Yes",
                                       "Penalty Clause": "Yes", "Review Date": d(ago(days=60))} for v in ("CloudCo", "PagerCo")])
    csv_out("technical_debt_register.csv", [
        {"ID": f"TD-{i}", "Title": t, "Severity": s, "Application": app, "Opened Date": d(o), "Completed Date": d(c) if c else "",
         "Status": "Closed" if c else "Open"}
        for i, (t, s, o, c) in enumerate([("Unpinned base image", "High", qs_last - timedelta(days=60), qs_last + timedelta(days=30)),
                                          ("Manual certificate rotation", "High", qs_last - timedelta(days=90), None),
                                          ("Hard-coded retry limits", "Critical", qs_last - timedelta(days=20), None)], 1)])

    def actions(fname, label, overdue):
        rows = [{"ID": f"{label}-{i}", "Title": f"{label} action {i}", "Owner": owner, "Due Date": d(ago(days=40 - i * 5)),
                 "Completed Date": d(ago(days=42 - i * 5)), "Status": "Done", "Escalated": "", "Application": app} for i in range(1, 6)]
        if overdue:
            for r in rows[:3]:
                r.update({"Completed Date": "", "Status": "Open", "Owner": "" if fname.startswith("cmdb") else owner})
        csv_out(fname, rows)

    actions("corrective_actions.csv", "CA", has("corrective_actions_overdue"))
    actions("postmortem_actions.csv", "PM", False)
    actions("cmdb_remediation_tasks.csv", "DQ", has("remediation_tasks_unowned"))
    csv_out("audit_findings.csv", [{"ID": "AF-1", "Title": "Support group missing on 2 CIs", "Owner": "CMDB Steward", "Severity": "Medium",
                                    "Opened Date": d(ago(days=50)), "Completed Date": d(ago(days=30)), "Application": app}])
    csv_out("sla_breach_register.csv", [{"ID": f"B-{i}", "Breach Date": d(ago(days=10 * i)), "Root Cause Category": cat,
                                         "Improvement Action": act, "Application": app}
                                        for i, (cat, act) in enumerate([("Wrong assignment group", "IO-02 routing rules"),
                                                                        ("Vendor delay", "Contract review with CloudCo")], 1)])
    late = has("kb_migration_late")
    csv_out("knowledge_migration_tracker.csv", [
        {"Procedure": f"{app} restart steps", "Identified Date": d(ago(days=90)), "Migrated Date": "" if late else d(ago(days=70)),
         "KB Number": "" if late else "KB0031001", "Application": app}])
    csv_out("pse_roster.csv", [{"Queue": sg, "Agent": a, "Critical": "Yes", "Shift": s, "Trained": "Yes"}
                               for a, s in (("Priya Shah", "Day"), ("Marcus Lee", "Night"), ("Dana Ortiz", "Day"))])
    csv_out("access_reviews.csv", [{"Review Date": d(ago(days=210 if has("access_review_overdue") else 30)), "Scope": "Production",
                                    "Least Privilege": "Yes", "Shared Accounts": "0", "Application": app}])
    csv_out("automation_registry.csv", [{"Name": f"{svc} auto-restart", "Owner": "" if has("automation_unowned") else owner,
                                         "Status": "Live", "Last Activity": d(ago(days=120 if has("automation_unowned") else 5)),
                                         "Before Hours": "6", "After Hours": "1", "Deployed Date": d(ago(days=200)), "Application": app}])
    pcts = [31, 34, 37, 41, 46] if has("toil_rising") else [46, 41, 37, 34, 31]
    quarters = [_quarter_start(as_of, k) for k in (5, 4, 3, 2, 1)]
    csv_out("toil_measurements.csv", [{"Quarter": f"{q.year} Q{(q.month - 1) // 3 + 1}", "Toil %": str(p), "Application": app}
                                      for q, p in zip(quarters, pcts)])
    csv_out("deployment_log.csv", [{"Deployed At": c["end_date"], "Commit At": fmt(datetime.fromisoformat(c["end_date"]) - timedelta(days=2)),
                                    "Environment": "Production", "Application": app} for c in changes])
