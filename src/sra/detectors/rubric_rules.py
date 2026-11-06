"""Deterministic rules for the 18 rubric criteria, following the Evidence Catalog's evaluation logic.

Each rule returns a direct rubric result:
  Met                    the pass condition holds on the evidence;
  Gap                    evidence was searched and the gap condition holds;
  Insufficient Evidence  the source the rule needs was not provided.
Criteria whose method is AI-assisted or Human Review get a preliminary result flagged for review.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..catalog.models import Result, Risk
from ..config import load_yaml
from ..evidence.bundle import EvidenceBundle
from ..evidence.model import DocType, Kind
from ..models import FindingType
from ..scoring.cmdb import level_from_pct
from ..scoring.cmdb import threshold as cmdb_threshold
from .base import Context, DetectorReport, finding, ref, result, truthy

IE, MET, GAP = Result.INSUFFICIENT, Result.MET, Result.GAP
PCT = re.compile(r"\b\d{2}(?:\.\d+)?\s*%")


def _sev(rid: str) -> Risk:
    return Risk.CRITICAL if rid.startswith("P1") else Risk.HIGH


def _app_ci(b: EvidenceBundle, ctx: Context, table: str | None = None) -> list[dict]:
    table = table or ctx.cfg["servicenow"]["app_table"]
    rows = b.table(table) + (b.table("u_business_application") if table != "u_business_application" else [])
    return [r for r in rows if ctx.matches(r, ("name", "u_name", "display_name"))]


def _incidents(b, ctx, days):
    since = ctx.days_ago(days)
    return [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= since]


def _changes(b, ctx, days):
    since = ctx.days_ago(days)
    rows = ctx.scoped(b.table("change_request"), ("cmdb_ci", "business_service", "u_business_application",
                                                  "cmdb_ci_business_app", "short_description", "assignment_group"))
    dated = [r for r in rows if (r.get("_start_date") or r.get("_opened_at") or r.get("_sys_created_on"))]
    return [r for r in dated if (r.get("_start_date") or r.get("_opened_at") or r.get("_sys_created_on")) >= since]


def _fresh_docs(b, ctx, types, months):
    docs = b.documents(*types)
    fresh = [d for d in docs if d.reviewed_at and d.reviewed_at >= ctx.months_ago(months)]
    return docs, fresh


def _emit(rep: DetectorReport, rid: str, res: Result, why: str, refs=(), review=False, action="", sev=None):
    rep.rubric[rid] = result(rid, res, why, list(refs), review=review)
    if res in (GAP, IE):
        rep.findings.append(finding(FindingType.GAP, sev or _sev(rid), f"{rid}: {why}", rubric=[rid], refs=refs,
                                    detector=f"rubric:{rid}", action=action))


# ---------------------------------------------------------------- P1
def p1_01(b, ctx, rep):
    cfg = ctx.cfg["servicenow"]
    if not (b.has_table(cfg["app_table"]) or b.has_table("u_business_application")):
        return _emit(rep, "P1-01", IE, "No CMDB application export provided", action="Provide the cmdb_ci / business application record export")
    cis = _app_ci(b, ctx)
    # V2 CMDB-AT-01-C01: exactly one Business Application record for the application
    reg = result("CMDB-AT-01-C01", MET if len(cis) == 1 else GAP,
                 f"{len(cis)} Business Application record(s) match '{ctx.app_name}'" + (" (duplicates)" if len(cis) > 1 else ""),
                 [ref(c, "name") for c in cis[:5]] or b.table_ids(cfg["app_table"]), review=len(cis) == 1)
    reg.measured_value = f"{len(cis)} matching record(s)"
    reg.maturity_level = 4 if len(cis) == 1 else 0
    rep.results["CMDB-AT-01-C01"] = reg
    if not cis:
        return _emit(rep, "P1-01", GAP, f"No CMDB application record found for '{ctx.app_name}'", b.table_ids(cfg["app_table"]),
                     action="Register the business application in CMDB with an accountable owner and support group")
    ci = cis[0]
    missing = [f for f in (cfg["owner_field"], cfg["support_group_field"]) if not truthy(ci.get(f))]
    # V2 CMDB-AT-01-C02 (a): ownership fields populated (TH-02 = 100%). Active-user status and owner
    # concentration need sys_user and the full portfolio, so a Met here still needs the assessor's review.
    own_fields = [f for f in (cfg["owner_field"], cfg["support_group_field"], "it_application_owner", "change_control")
                  if f in ci or f in (cfg["owner_field"], cfg["support_group_field"])]
    own_missing = [f for f in own_fields if not truthy(ci.get(f))]
    pct = 100 * (len(own_fields) - len(own_missing)) / len(own_fields)
    own = result("CMDB-AT-01-C02", GAP if own_missing else MET,
                 (f"Ownership fields empty: {', '.join(own_missing)}" if own_missing else
                  f"Ownership fields populated ({', '.join(own_fields)}); confirm the users/groups are active and owner concentration"),
                 [ref(ci, "name")], review=True)
    own.measured_value = f"{pct:.0f}% of {len(own_fields)} ownership fields populated"
    own.maturity_level = None if not own_missing else (0 if ctx.tier == 1 else 1)
    rep.results["CMDB-AT-01-C02"] = own
    if missing:
        return _emit(rep, "P1-01", GAP, f"CMDB record {ci.get('name')} missing {', '.join(missing)}", [ref(ci, "name")],
                     action=f"Populate {', '.join(missing)} on the CMDB application record")
    _emit(rep, "P1-01", MET, f"CMDB record {ci.get('name')} has owner and support group", [ref(ci, "name")])


def p1_02(b, ctx, rep):
    days = ctx.cfg["rubric_rules"]["incident_window_days"]
    sg_field = ctx.cfg["servicenow"]["support_group_field"]
    cis = _app_ci(b, ctx)
    if not cis and not b.has_table("incident"):
        return _emit(rep, "P1-02", IE, "Neither CMDB record nor incident history provided")
    problems, refs = [], []
    if cis:
        refs.append(ref(cis[0], "name"))
        if not truthy(cis[0].get(sg_field)):
            problems.append("CMDB support_group is empty")
    else:
        problems.append("no CMDB record to confirm the support group")
    inc = _incidents(b, ctx, days)
    if b.has_table("incident"):
        refs += b.table_ids("incident")
        unassigned = [r for r in inc if not truthy(r.get("assignment_group"))]
        if inc and len(unassigned) / len(inc) > 0.05:
            problems.append(f"{len(unassigned)}/{len(inc)} incidents in {days}d have no assignment group")
    if problems:
        return _emit(rep, "P1-02", GAP, "; ".join(problems), refs,
                     action="Establish the support model: support group on CMDB, escalation path and operational contact (ITSM-PB-09)")
    _emit(rep, "P1-02", MET, f"Support group set; {len(inc)} incidents in {days}d all assigned", refs)


def p1_03(b, ctx, rep):
    days = ctx.cfg["rubric_rules"]["alert_window_days"]
    alerts = [r for r in ctx.scoped(b.table("em_alert"), ("cmdb_ci", "source", "description", "node", "u_application"))
              if (r.get("_sys_created_on") or r.get("_opened_at") or ctx.as_of) >= ctx.days_ago(days)]
    mon_inc = [r for r in _incidents(b, ctx, days) if r.get("_from_monitoring")]
    obs_docs = b.documents(DocType.OBSERVABILITY)
    refs = b.table_ids("em_alert") + [d.ref() for d in obs_docs]
    if not (b.has_table("em_alert") or b.has_table("incident") or obs_docs):
        return _emit(rep, "P1-03", IE, "No alert, incident or observability evidence provided")
    if not alerts and not mon_inc and not obs_docs:
        return _emit(rep, "P1-03", GAP, f"No monitoring alerts or monitoring-opened incidents in the last {days} days", refs,
                     action="Instrument metrics, logs and traces and route actionable alerts (SRE-PB-03)")
    _emit(rep, "P1-03", MET, f"{len(alerts)} alerts and {len(mon_inc)} monitoring-opened incidents in {days}d"
          + (f"; {len(obs_docs)} observability document(s)" if obs_docs else ""),
          refs + [ref(r) for r in mon_inc[:3]], review=True)


def p1_04(b, ctx, rep):
    months = ctx.cfg["rubric_rules"]["runbook_max_age_months"]
    docs, fresh = _fresh_docs(b, ctx, [DocType.RUNBOOK], months)
    kb = [r for r in ctx.scoped(b.table("kb_knowledge"), ("short_description", "text", "kb_category", "u_application",
                                                          "cmdb_ci", "meta"))
          if re.search(r"runbook|playbook|procedure|sop|recovery", f"{r.get('short_description', '')} {r.get('kb_category', '')}", re.I)]
    kb_fresh = [r for r in kb if r.get("_sys_updated_on") and r["_sys_updated_on"] >= ctx.months_ago(months)]
    if not docs and not kb and not b.has_table("kb_knowledge"):
        return _emit(rep, "P1-04", IE, "No runbook documents or KB export provided",
                     action="Provide the application runbook(s) or kb_knowledge export")
    refs = [d.ref() for d in docs] + [ref(r) for r in kb]
    if not docs and not kb:
        return _emit(rep, "P1-04", GAP, "No operational runbook found", b.table_ids("kb_knowledge"),
                     action="Author an operational runbook using the Playbook Template (SRE-PB-08)")
    # Template criteria with a stricter (6-month) currency rule - see crosswalk.yaml conflicts
    six = ctx.months_ago(6)
    fresh6 = [d for d in docs if d.reviewed_at and d.reviewed_at >= six] +              [r for r in kb if r.get("_sys_updated_on") and r["_sys_updated_on"] >= six]
    for cid, review in (("SRE-AT-07-C03", False), ("SRE-AT-01-C05", True)):
        rep.results[cid] = result(cid, MET if fresh6 else GAP,
                                  f"{len(fresh6)} runbook(s) reviewed within 6 months (template threshold)", refs, review=review)
    if not fresh and not kb_fresh:
        undated = [d.name for d in docs if not d.reviewed_at]
        why = f"Runbook(s) not reviewed in the last {months} months" + (f" (undated: {', '.join(undated)})" if undated else "")
        return _emit(rep, "P1-04", GAP, why, refs, action="Review and re-date the runbook; record the review in the Lifecycle section")
    _emit(rep, "P1-04", MET, f"{len(fresh) + len(kb_fresh)} runbook(s) reviewed within {months} months", refs)


def p1_05(b, ctx, rep):
    rr = ctx.cfg["rubric_rules"]
    slo_rows = ctx.scoped(b.table(ctx.cfg["servicenow"]["slo_table"]))
    docs = b.documents(DocType.SLO, DocType.ERROR_BUDGET)
    if not slo_rows and not docs:
        why = "No SLI/SLO definitions found" if (b.has_table(ctx.cfg["servicenow"]["slo_table"])) else "No SLO evidence provided"
        return _emit(rep, "P1-05", GAP if b.has_table(ctx.cfg["servicenow"]["slo_table"]) else IE, why,
                     action="Define availability and latency SLIs with SLO targets using the SLO Document Template (SRE-PB-04)")
    if slo_rows:
        active = [r for r in slo_rows if truthy(r.get("measurement_active", "true"))
                  and (r.get("_last_calculated") or r.get("_u_last_calculated") or ctx.as_of) >= ctx.days_ago(rr["slo_measurement_max_age_days"])]
        if not active:
            return _emit(rep, "P1-05", GAP, "SLO records exist but are not actively measured", [ref(r, "name") for r in slo_rows],
                         action="Enable SLO measurement and confirm calculation within the last 7 days")
        return _emit(rep, "P1-05", MET, f"{len(active)} actively measured SLO(s)", [ref(r, "name") for r in active], review=True)
    with_target = [d for d in docs if PCT.search(d.text) and re.search(r"\bSL[IO]\b", d.text)]
    if not with_target:
        return _emit(rep, "P1-05", GAP, "SLO document lacks SLI definitions or numeric targets", [d.ref() for d in docs],
                     action="Add SLI definitions (good/valid events) and numeric SLO targets")
    _emit(rep, "P1-05", MET, "SLO document with SLIs and targets; measurement evidence must be confirmed",
          [d.ref() for d in with_target], review=True)


def p1_06(b, ctx, rep):
    recov = [d for d in b.documents(DocType.RUNBOOK, DocType.DR_PLAN)
             if re.search(r"recover|restor|failover|rollback", d.text, re.I)]
    tests = b.documents(DocType.DR_TEST)
    if not recov and not tests:
        if b.documents():
            return _emit(rep, "P1-06", GAP, "No documented recovery procedure in the provided documents",
                         [d.ref() for d in b.documents()][:5], action="Document recovery steps and validate them in a test (SRE-PB-10)")
        return _emit(rep, "P1-06", IE, "No recovery procedure evidence provided")
    if not tests:
        return _emit(rep, "P1-06", GAP, "Recovery procedure documented but no evidence it has been tested",
                     [d.ref() for d in recov], action="Execute and record a recovery/failover test (SRE-PB-10)")
    _emit(rep, "P1-06", MET, "Recovery procedure documented and test evidence present — correctness needs human review",
          [d.ref() for d in recov + tests], review=True)


def p1_07(b, ctx, rep):
    rr = ctx.cfg["rubric_rules"]
    if not b.has_table("change_request"):
        return _emit(rep, "P1-07", IE, "No change_request export provided", action="Provide 90-day change records for the application")
    window = rr["change_window_days"]
    ch = _changes(b, ctx, window)
    if not ch:
        window = rr["change_fallback_days"]
        ch = _changes(b, ctx, window)
    if not ch:
        return _emit(rep, "P1-07", GAP, "No change records for the application (count = 0)", b.table_ids("change_request"),
                     action="Raise Change records for every production deployment (ITSM-PB-05)")
    missing = [r for r in ch if not (truthy(r.get("backout_plan")) and truthy(r.get("test_plan")))]
    if missing:
        return _emit(rep, "P1-07", GAP, f"{len(missing)}/{len(ch)} changes in {window}d lack a backout or test plan",
                     [ref(r) for r in missing[:10]], action="Require backout_plan and test_plan on every Change before approval")
    _emit(rep, "P1-07", MET, f"{len(ch)} changes in {window}d, all with backout and test plans", [ref(r) for r in ch[:10]])


def p1_08(b, ctx, rep):
    rr = ctx.cfg["rubric_rules"]
    docs, fresh = _fresh_docs(b, ctx, [DocType.ARCHITECTURE], rr["architecture_doc_max_age_months"])
    if not docs:
        return _emit(rep, "P1-08", IE if not b.documents() else GAP, "No architecture / failure-mode analysis on record",
                     action="Run an Architecture Risk Storming session and record failure modes (SRE-PB-02)")
    for d in fresh or docs:
        scenarios = len(re.findall(r"failure (?:mode|scenario)|single point of failure|spof|if .{0,40} fails", d.text, re.I))
        mitig = len(re.findall(r"mitigat|fallback|failover|redundan", d.text, re.I))
        if d in fresh and scenarios >= rr["min_failure_scenarios"] and mitig:
            return _emit(rep, "P1-08", MET, f"{d.name}: {scenarios} failure scenarios with mitigations", [d.ref()], review=True)
    why = (f"Architecture document older than {rr['architecture_doc_max_age_months']} months" if not fresh
           else f"Architecture document lists fewer than {rr['min_failure_scenarios']} failure scenarios with mitigations")
    _emit(rep, "P1-08", GAP, why, [d.ref() for d in docs], action="Update failure-mode analysis for the current architecture (SRE-PB-02)")


# ---------------------------------------------------------------- P2
def p2_01(b, ctx, rep):
    rels = b.table("cmdb_rel_ci")
    if not rels:
        return _emit(rep, "P2-01", IE, "No cmdb_rel_ci (dependency) export provided")
    mine = [r for r in rels if ctx.matches(r, ("parent", "child"))]
    if not mine:
        return _emit(rep, "P2-01", GAP, "No upstream/downstream dependencies mapped for the application", b.table_ids("cmdb_rel_ci"),
                     action="Map upstream and downstream dependencies in CMDB (CMDB-PB-05)")
    months = ctx.cfg["rubric_rules"]["dependency_review_max_age_months"]
    stale = [r for r in mine if r.get("_sys_updated_on") and r["_sys_updated_on"] < ctx.months_ago(months)]
    # V2 CMDB-AT-06-C06 (map currency) uses TH-07: 6 months Tier 1, 12 months Tier 2; Tier 3 not gated
    th07 = cmdb_threshold("TH-07", ctx.tier)
    if th07:
        current = [r for r in mine if r.get("_sys_updated_on") and r["_sys_updated_on"] >= ctx.months_ago(th07)]
        pct = 100 * len(current) / len(mine)
        cur = result("CMDB-AT-06-C06", MET if len(current) == len(mine) else GAP,
                     f"{len(current)} of {len(mine)} dependency relationships updated within {th07:.0f} months "
                     f"(TH-07, Tier {ctx.tier or 2}); last-updated date used as the validation date", [ref(r, "sys_id") for r in mine[:5]],
                     review=True)
        cur.measured_value = f"{pct:.0f}% validated within {th07:.0f} months"
        cur.maturity_level = level_from_pct(pct, 100)
        rep.results["CMDB-AT-06-C06"] = cur
    if stale and len(stale) == len(mine):
        return _emit(rep, "P2-01", GAP, f"All {len(mine)} dependency relationships unreviewed for {months}+ months",
                     [ref(r, "sys_id") for r in mine[:5]], action="Review and recertify the dependency map")
    _emit(rep, "P2-01", MET, f"{len(mine)} dependency relationships mapped", [ref(r, "sys_id") for r in mine[:5]], review=True)


def p2_02(b, ctx, rep):
    rr = ctx.cfg["rubric_rules"]
    if not b.has_table("incident"):
        return _emit(rep, "P2-02", IE, "No incident export provided")
    inc = _incidents(b, ctx, rr["incident_window_days"])
    groups = defaultdict(list)
    for r in inc:
        key = (r.get("category") or "").strip().lower(), (r.get("subcategory") or "").strip().lower()
        if any(key):
            groups[key].append(r)
    bad = {k: v for k, v in groups.items() if len(v) >= rr["recurrence_min_incidents"] and not any(truthy(r.get("problem_id")) for r in v)}
    if bad:
        desc = "; ".join(f"{'/'.join(filter(None, k))} ×{len(v)}" for k, v in sorted(bad.items(), key=lambda kv: -len(kv[1]))[:5])
        return _emit(rep, "P2-02", GAP, f"{len(bad)} recurring incident pattern(s) with no Problem record: {desc}",
                     [ref(r) for v in bad.values() for r in v[:3]],
                     action="Open Problem records for recurring patterns and link incidents (ITSM-PB-03)")
    _emit(rep, "P2-02", MET, f"All recurring patterns in {rr['incident_window_days']}d are linked to Problems", b.table_ids("incident"))


def p2_03(b, ctx, rep):
    days = ctx.cfg["rubric_rules"]["error_budget_review_max_age_days"]
    docs = b.documents(DocType.ERROR_BUDGET) + [d for d in b.documents(DocType.SLO) if re.search(r"error budget", d.text, re.I)]
    slo_rows = ctx.scoped(b.table(ctx.cfg["servicenow"]["slo_table"]))
    reviewed = [r for r in slo_rows if (r.get("_last_reviewed") or r.get("_u_last_reviewed"))
                and (r.get("_last_reviewed") or r.get("_u_last_reviewed")) >= ctx.days_ago(days)]
    fresh = [d for d in docs if d.reviewed_at and d.reviewed_at >= ctx.days_ago(days)]
    if not docs and not slo_rows:
        return _emit(rep, "P2-03", IE, "No error budget evidence provided")
    if not reviewed and not fresh:
        return _emit(rep, "P2-03", GAP, f"Error budget not reviewed in the last {days} days", [d.ref() for d in docs],
                     action="Adopt the Error Budget Policy and hold a monthly error budget review (SRE-PB-04)")
    _emit(rep, "P2-03", MET, f"Error budget reviewed within {days} days", [d.ref() for d in fresh] + [ref(r, "name") for r in reviewed])


def p2_04(b, ctx, rep):
    # Result produced by config_checks (resilience settings); here only the absence case.
    if not (b.k8s or b.of_kind(Kind.OTHER) or b.documents(DocType.ARCHITECTURE)):
        _emit(rep, "P2-04", IE, "No configuration or design evidence for timeouts/retries/circuit breakers",
              action="Provide service configuration (Kubernetes manifests, Helm values, client configs)")


def p2_05(b, ctx, rep):
    days = ctx.cfg["rubric_rules"]["perf_test_max_age_days"]
    docs = b.documents(DocType.LOAD_TEST)
    if not docs:
        return _emit(rep, "P2-05", IE if not b.documents() else GAP, "No load test results or performance benchmark on record",
                     action="Run a load test and record RPS, P95, P99 and error rate baselines (SRE-PB-06)")
    good = [d for d in docs if d.reviewed_at and d.reviewed_at >= ctx.days_ago(days)
            and sum(bool(re.search(p, d.text, re.I)) for p in (r"\brps\b|requests? per second|throughput", r"\bp95\b", r"\bp99\b", r"error rate")) >= 3]
    if not good:
        return _emit(rep, "P2-05", GAP, f"Load test older than {days} days or missing RPS/P95/P99/error-rate figures",
                     [d.ref() for d in docs], action="Re-run load test against the current release and record baselines")
    _emit(rep, "P2-05", MET, "Current load test with baseline metrics", [d.ref() for d in good], review=True)


def p2_06(b, ctx, rep):
    months = ctx.cfg["rubric_rules"]["capacity_plan_max_age_months"]
    docs, fresh = _fresh_docs(b, ctx, [DocType.CAPACITY], months)
    if not docs:
        return _emit(rep, "P2-06", IE if not b.documents() else GAP, "No capacity plan or forecast on record",
                     action="Produce a capacity forecast (SRE-PB-06)")
    if not fresh:
        return _emit(rep, "P2-06", GAP, f"Capacity plan not updated within {months} months", [d.ref() for d in docs],
                     action="Refresh the capacity forecast")
    _emit(rep, "P2-06", MET, f"Capacity plan updated within {months} months", [d.ref() for d in fresh])


def p2_07(b, ctx, rep):
    tier = ctx.tier or 2
    months = load_yaml("scoring_matrix")["tier_thresholds"]["dr_test_max_age_months"].get(tier, 24)
    tests = b.documents(DocType.DR_TEST)
    plans = b.documents(DocType.DR_PLAN)
    if not tests and not plans:
        return _emit(rep, "P2-07", IE if not b.documents() else GAP, "No DR plan or DR test evidence",
                     action="Create and exercise a DR plan (SRE-PB-10)")
    fresh = [d for d in tests if d.reviewed_at and d.reviewed_at >= ctx.months_ago(months)]
    if not fresh:
        return _emit(rep, "P2-07", GAP, f"DR plan not exercised within {months} months (Tier {tier})",
                     [d.ref() for d in tests + plans], action=f"Execute a DR test within the Tier {tier} interval and record results")
    _emit(rep, "P2-07", MET, f"DR test within {months} months (Tier {tier})", [d.ref() for d in fresh], review=True)


def p2_08(b, ctx, rep):
    if not b.has_table("change_request"):
        return _emit(rep, "P2-08", IE, "No change_request export provided")
    ch = sorted(_changes(b, ctx, ctx.cfg["rubric_rules"]["change_fallback_days"]),
                key=lambda r: r.get("_end_date") or r.get("_closed_at") or r.get("_start_date") or ctx.as_of)
    if not ch:
        return _emit(rep, "P2-08", IE, "No recent release (change) to validate")
    last = ch[-1]
    kws = ctx.cfg["rubric_rules"]["post_deploy_keywords"]
    notes = (last.get("close_notes") or "").lower()
    if (last.get("close_code") or "").lower().startswith("successful") or any(k in notes for k in kws):
        return _emit(rep, "P2-08", MET, f"Last release {last.get('number')} closed successful / validated", [ref(last)])
    _emit(rep, "P2-08", GAP, f"No post-deployment validation evidence for last release {last.get('number')}", [ref(last)],
          action="Run deployment health checks and SLO validation after each release (SRE-PB-13)")


def p2_09(b, ctx, rep):
    if not b.has_table("problem"):
        return _emit(rep, "P2-09", IE, "No problem export provided")
    probs = ctx.scoped(b.table("problem"))
    with_wa = [r for r in probs if truthy(r.get("workaround")) or truthy(r.get("u_workaround"))]
    not_ke = [r for r in with_wa if not truthy(r.get("known_error"))]
    open_no_wa = [r for r in probs if (r.get("state") or "").lower() not in ("closed", "resolved")
                  and not truthy(r.get("workaround")) and not truthy(r.get("u_workaround"))]
    if not_ke or open_no_wa:
        parts = []
        if not_ke:
            parts.append(f"{len(not_ke)} problem(s) with workarounds not flagged as Known Errors")
        if open_no_wa:
            parts.append(f"{len(open_no_wa)} open problem(s) without a documented workaround")
        return _emit(rep, "P2-09", GAP, "; ".join(parts), [ref(r) for r in (not_ke + open_no_wa)[:10]],
                     action="Record known errors and workarounds in the KEDB (ITSM-PB-07)")
    _emit(rep, "P2-09", MET, f"{len(with_wa)} known errors documented", [ref(r) for r in with_wa[:5]])


def p2_10(b, ctx, rep):
    tier = ctx.tier or 2
    threshold = load_yaml("scoring_matrix")["tier_thresholds"]["cmdb_completeness_pct"].get(tier, 85)
    cis = _app_ci(b, ctx)
    if not cis:
        return _emit(rep, "P2-10", IE, "No CMDB CI record to measure attribute completeness")
    fields = ctx.cfg["rubric_rules"]["required_ci_fields"]
    present = [f for f in fields if any(f in ci for ci in cis)]
    if not present:
        return _emit(rep, "P2-10", IE, "CMDB export does not include the required attribute columns")
    filled = sum(1 for ci in cis for f in present if truthy(ci.get(f)))
    pct = round(100 * filled / (len(cis) * len(present)), 1)
    # V2 CMDB-AT-04-C02 measures the same completeness against TH-01 (95/90/85%)
    th01 = cmdb_threshold("TH-01", tier)
    v2 = result("CMDB-AT-04-C02", MET if pct >= th01 else GAP,
                f"Required-attribute completeness {pct}% (TH-01 for Tier {tier}: {th01:.0f}%) over {', '.join(present)}",
                [ref(ci, "name") for ci in cis[:5]])
    v2.measured_value = f"{pct}% of {len(present)} required fields"
    v2.maturity_level = level_from_pct(pct, th01)
    rep.results["CMDB-AT-04-C02"] = v2
    # CMDB-AT-11-C04 (certification view) measures the same required-field completeness against TH-01
    rep.results["CMDB-AT-11-C04"] = v2.model_copy(update={"criterion_id": "CMDB-AT-11-C04"})
    absent = [f for f in fields if f not in present]
    note = f" (columns not in export: {', '.join(absent)})" if absent else ""
    if pct < threshold:
        empty = Counter(f for ci in cis for f in present if not truthy(ci.get(f)))
        return _emit(rep, "P2-10", GAP, f"CI attribute completeness {pct}% < {threshold}% (Tier {tier}); empty: "
                     f"{', '.join(f for f, _ in empty.most_common(5))}{note}", [ref(ci, "name") for ci in cis[:5]],
                     action="Populate the required CI attributes (CMDB-PB-02)")
    _emit(rep, "P2-10", MET, f"CI attribute completeness {pct}% ≥ {threshold}% (Tier {tier}){note}", [ref(ci, "name") for ci in cis[:5]])


RULES = [p1_01, p1_02, p1_03, p1_04, p1_05, p1_06, p1_07, p1_08,
         p2_01, p2_02, p2_03, p2_04, p2_05, p2_06, p2_07, p2_08, p2_09, p2_10]


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport("rubric_rules")
    for rule in RULES:
        rule(b, ctx, rep)
    return rep
