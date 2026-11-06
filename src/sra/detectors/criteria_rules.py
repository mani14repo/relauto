"""Deterministic rules for template criteria that the evidence the app already ingests can settle.

Before these rules, every criterion here went to the local LLM (or the assessor). Each rule sets a result only
when the data it needs is in the evidence; otherwise the criterion is left alone, so it still reaches the LLM or
the assessor. A rule whose criterion also needs something the data cannot show (a test record, a confirmation)
sets its result for human review. V2 CMDB criteria also get a measured value and a maturity level.
Settings are under `criteria_rules` in config/detectors.yaml; field names marked CONFIRM there need checking
against the ServiceNow instance.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..evidence.model import Kind
from ..models import CriterionResult
from ..scoring.cmdb import level_from_pct
from ..scoring.cmdb import threshold as cmdb_threshold
from .base import Context, DetectorReport, ref, result, truthy
from .config_checks import RETRY, TIMEOUT, WORKLOADS
from .vulnerabilities import GATES

NAME = "criteria_rules"
MET, GAP = Result.MET, Result.GAP
CLOSED = re.compile(r"resolved|closed|cancel", re.I)
OUTAGE = re.compile(r"\b(down|outage|unavailable|not available|offline|unreachable)\b", re.I)
ZERO_TIME = re.compile(r"^(0+|0+[:.]0+([:.]0+)?|0+ ?(s|sec|seconds|min|minutes|h|hours)?)$", re.I)


# ------------------------------------------------------------------------------------------------ helpers
def _cfg(ctx: Context) -> dict:
    return ctx.cfg.get("criteria_rules") or {}


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def _v2(cid: str, ok: bool, why: str, refs, pct: float, target: float, measured: str, review=False) -> CriterionResult:
    """A V2 CMDB criterion result with its measured value and L0-L4 maturity level."""
    r = result(cid, MET if ok else GAP, why, list(refs), review=review)
    r.measured_value = measured
    r.maturity_level = level_from_pct(pct, target)
    return r


def _window(rows: list[dict], ctx: Context, days: int, key: str = "_opened_at") -> list[dict]:
    since = ctx.days_ago(days)
    return [r for r in rows if r.get(key) and r[key] >= since]


def _incidents(b, ctx) -> list[dict]:
    return _window(ctx.scoped(b.table("incident")), ctx, ctx.cfg["rubric_rules"]["incident_window_days"])


def _changes(b, ctx) -> list[dict]:
    rows = ctx.scoped(b.table("change_request"), ("cmdb_ci", "business_service", "u_business_application",
                                                  "cmdb_ci_business_app", "short_description", "assignment_group"))
    days = ctx.cfg["rubric_rules"]["change_window_days"]
    return [r for r in rows if (r.get("_start_date") or r.get("_opened_at") or r.get("_sys_created_on") or datetime.min)
            >= ctx.days_ago(days)]


def _has_col(rows: list[dict], col: str) -> bool:
    return any(col in r for r in rows)


def _ci_configs(b) -> list:
    return b.of_kind(Kind.CI_CONFIG)


def _prio(r: dict) -> int | None:
    m = re.match(r"\s*(\d)", str(r.get("priority", "")))
    return int(m.group(1)) if m else None


def _labels(obj: dict) -> dict:
    meta = obj.get("metadata", {}) or {}
    tmpl = ((obj.get("spec", {}) or {}).get("template", {}) or {}).get("metadata", {}) or {}
    return {**(meta.get("labels") or {}), **(meta.get("annotations") or {}), **(tmpl.get("labels") or {})}


def _otel_keys(obj: dict) -> set[str]:
    keys = set()
    pod = ((obj.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {}
    for c in pod.get("containers", []) or []:
        for e in c.get("env", []) or []:
            if e.get("name") == "OTEL_RESOURCE_ATTRIBUTES":
                keys |= {kv.split("=", 1)[0].strip() for kv in str(e.get("value", "")).split(",") if "=" in kv}
    return keys


# ------------------------------------------------------------------------------------------------ CMDB (V2)
def cmdb_placement(b, ctx, rep):
    """CMDB-AT-01-C03: BAs with every placement attribute valid; Region = None count."""
    from .rubric_rules import _app_ci
    bas = _app_ci(b, ctx)
    want = _cfg(ctx).get("placement_fields", [])
    present = [f for f in want if _has_col(bas, f)]
    if not bas or not present:
        return
    valid = lambda v: truthy(v) and str(v).strip().lower() != "none"  # noqa: E731
    ok = [ci for ci in bas if all(valid(ci.get(f)) for f in present)]
    region = next((f for f in present if "region" in f), None)
    none_region = sum(1 for ci in bas if region and str(ci.get(region, "")).strip().lower() in ("", "none"))
    pct = _pct(len(ok), len(bas))
    missing = [f for f in want if f not in present]
    rep.results["CMDB-AT-01-C03"] = _v2(
        "CMDB-AT-01-C03", pct == 100 and none_region == 0,
        f"{len(ok)} of {len(bas)} Business Application record(s) have valid {', '.join(present)}; Region = None: {none_region}"
        + (f" (not in export: {', '.join(missing)})" if missing else "") + ". Tier approval per CMDB-PB-09 needs confirming.",
        [ref(ci, "name") for ci in bas[:5]], pct, 100, f"{pct}% placement complete; Region = None {none_region}", review=True)


def cmdb_stale(b, ctx, rep):
    """CMDB-AT-02-C04: Installed discoverable CIs not discovered in 30 days, against TH-10."""
    rows = [r for r in b.table("cmdb_ci") if "last_discovered" in r
            and not re.search(r"business_app|service", str(r.get("sys_class_name", "")))
            and not re.search(r"retired|decommission|absent", str(r.get("install_status", "")) + str(r.get("operational_status", "")), re.I)]
    if not rows:
        return
    stale = [r for r in rows if not r.get("_last_discovered") or r["_last_discovered"] < ctx.days_ago(30)]
    rate = _pct(len(stale), len(rows))
    limit = cmdb_threshold("TH-10", ctx.tier) or 5
    r = result("CMDB-AT-02-C04", MET if rate <= limit else GAP,
               f"{len(stale)} of {len(rows)} installed discoverable CIs not discovered in 30 days ({rate}%; TH-10 for Tier "
               f"{ctx.tier or 2}: ≤ {limit:g}%). Whether an automated retire policy is active needs confirming.",
               [ref(x, "name") for x in (stale or rows)[:5]], review=True)
    r.measured_value = f"{rate}% stale ({len(stale)} of {len(rows)})"
    r.maturity_level = 4 if rate <= 2 else 3 if rate <= 5 else 2 if rate <= 10 else 1 if rate <= 20 else 0
    rep.results["CMDB-AT-02-C04"] = r


def _workloads(b):
    return [(eid, o) for eid, o in b.k8s if o.get("kind") in WORKLOADS]


def cmdb_labels(b, ctx, rep):
    """CMDB-AT-07-C03 (required labels) and CMDB-AT-12-C04 (workload metadata standard) from the manifests."""
    wl = _workloads(b)
    if not wl:
        return
    c = _cfg(ctx)
    for cid, keys, target in (("CMDB-AT-07-C03", c.get("required_labels", []), 95.0),
                              ("CMDB-AT-12-C04", c.get("workload_metadata_keys", []), 100.0 if ctx.tier == 1 else 95.0)):
        if not keys:
            continue
        ok, bad = [], []
        for eid, o in wl:
            have = set(_labels(o)) | _otel_keys(o)
            (ok if all(k in have for k in keys) else bad).append(f"{eid}:{o['kind']}/{o.get('metadata', {}).get('name')}")
        pct = _pct(len(ok), len(wl))
        rep.results[cid] = _v2(cid, pct >= target,
                               f"{len(ok)} of {len(wl)} workload(s) carry all of {', '.join(keys)} (target {target:g}%)"
                               + (f"; missing on {', '.join(bad[:3])}" if bad else "")
                               + ". Admission/policy enforcement for new deployments needs confirming.",
                               ok + bad, pct, target, f"{pct}% of workloads ({len(ok)} of {len(wl)})", review=True)


def cmdb_change_ci(b, ctx, rep):
    """CMDB-AT-08-C01: changes with the Affected CI populated, against TH-05 (100%)."""
    ch = _changes(b, ctx)
    if not ch or not _has_col(ch, "cmdb_ci"):
        return
    ok = [r for r in ch if truthy(r.get("cmdb_ci"))]
    pct, target = _pct(len(ok), len(ch)), cmdb_threshold("TH-05", ctx.tier) or 100
    rep.results["CMDB-AT-08-C01"] = _v2(
        "CMDB-AT-08-C01", pct >= target, f"{len(ok)} of {len(ch)} changes have an Affected CI ({pct}%; TH-05 {target:g}%). "
        "Whether it is the Production Application Service rather than the Business Application needs checking.",
        [ref(r) for r in ch[:8]], pct, target, f"{pct}% ({len(ok)} of {len(ch)} changes)", review=True)


def cmdb_tier_priority(b, ctx, rep):
    """CMDB-AT-10-C04: outages logged at a priority the service tier allows."""
    worst = (_cfg(ctx).get("outage_max_priority") or {}).get(ctx.tier or 2)
    outages = [r for r in _incidents(b, ctx) if _prio(r) and OUTAGE.search(f"{r.get('short_description', '')} {r.get('subcategory', '')}")]
    if not outages or worst is None:
        return
    ok = [r for r in outages if _prio(r) <= worst]
    pct = _pct(len(ok), len(outages))
    low = [r for r in outages if r not in ok]
    rep.results["CMDB-AT-10-C04"] = _v2(
        "CMDB-AT-10-C04", pct >= 95, f"{len(ok)} of {len(outages)} outage incidents logged at P{worst} or higher for a "
        f"Tier {ctx.tier or 2} service (target 95%)" + (f"; lower: {', '.join(f'{r.get('number')} P{_prio(r)}' for r in low[:5])}" if low else ""),
        [ref(r) for r in (low or outages)[:8]], pct, 95, f"{pct}% aligned ({len(ok)} of {len(outages)} outages)", review=True)


def cmdb_alert_binding(b, ctx, rep):
    """CMDB-AT-12-C05: alerts bound to a CI (≥ 90%)."""
    alerts = b.table("em_alert")
    if not alerts or not _has_col(alerts, "cmdb_ci"):
        return
    ok = [r for r in alerts if truthy(r.get("cmdb_ci"))]
    pct = _pct(len(ok), len(alerts))
    generic = Counter(r.get("cmdb_ci") for r in ok).most_common(1)
    rep.results["CMDB-AT-12-C05"] = _v2(
        "CMDB-AT-12-C05", pct >= 90, f"{len(ok)} of {len(alerts)} alerts bound to a CI ({pct}%; target 90%, 100% for alerts "
        f"that open P1/P2 incidents). Most common CI: {generic[0][0] if generic else '—'} (check it is the service, not a tool or shared host).",
        b.table_ids("em_alert"), pct, 90, f"{pct}% bound ({len(ok)} of {len(alerts)})", review=True)


# ------------------------------------------------------------------------------------------------ ITSM
def _jenkins(b) -> list:
    return [c for c in _ci_configs(b) if c.meta.get("tool") == "jenkins"]


def itsm_jenkins(b, ctx, rep):
    """ITSM-AT-01-C05: no production deployments through Jenkins (deadline end of Oct 2026)."""
    cis = _ci_configs(b)
    if not cis:
        return
    j = _jenkins(b)
    late = ctx.as_of.date() > datetime(2026, 10, 31).date()
    rep.results["ITSM-AT-01-C05"] = result(
        "ITSM-AT-01-C05", GAP if j else MET,
        (f"Jenkins pipeline in use: {', '.join(c.name for c in j)}" + (" after the end-Oct 2026 deadline" if late else ""))
        if j else f"No Jenkins pipeline among {len(cis)} CI configuration(s); deployment records should confirm",
        [c.ref() for c in cis], risk=Risk.CRITICAL if j and late else None, review=not j)


def itsm_time_worked(b, ctx, rep):
    """ITSM-AT-02-C03: time worked captured on > 80% of human-handled incidents."""
    inc = [r for r in _incidents(b, ctx) if not r.get("_from_monitoring") or r.get("_human_notes")]
    if not inc or not _has_col(inc, "time_worked"):
        return
    ok = [r for r in inc if truthy(r.get("time_worked")) and not ZERO_TIME.match(str(r["time_worked"]).strip())]
    pct = _pct(len(ok), len(inc))
    rep.results["ITSM-AT-02-C03"] = result("ITSM-AT-02-C03", MET if pct > 80 else GAP,
                                           f"Time worked captured on {len(ok)} of {len(inc)} human-handled incidents ({pct}%; target > 80%)",
                                           b.table_ids("incident"))


def itsm_problem_change(b, ctx, rep):
    """ITSM-AT-03-C03: Problems recommending a permanent fix have a linked Change (rfc)."""
    pr = ctx.scoped(b.table("problem"), ("cmdb_ci", "business_service", "u_business_application", "short_description", "assignment_group"))
    if not pr or not _has_col(pr, "rfc"):
        return
    fix = [r for r in pr if truthy(r.get("rfc")) or truthy(r.get("fix_notes")) or re.search(r"fix|resolv|closed", str(r.get("state", "")), re.I)]
    if not fix:
        return
    ok = [r for r in fix if truthy(r.get("rfc"))]
    rep.results["ITSM-AT-03-C03"] = result(
        "ITSM-AT-03-C03", MET if len(ok) == len(fix) else GAP,
        f"{len(ok)} of {len(fix)} Problems with a fix in progress or done have a linked Change"
        + (f"; without: {', '.join([str(r.get('number')) for r in fix if r not in ok][:5])}" if len(ok) < len(fix) else ""),
        [ref(r) for r in fix[:8]])


def itsm_fcr(b, ctx, rep):
    """ITSM-AT-04-C01: first-call resolution (no reassignment) > 40% for PSE-handled incidents."""
    inc = _incidents(b, ctx)
    if not inc or not _has_col(inc, "reassignment_count"):
        return
    groups = [g.lower() for g in _cfg(ctx).get("pse_groups", [])]
    if groups:
        inc = [r for r in inc if str(r.get("assignment_group", "")).lower() in groups]
    nums = [(r, int(m.group())) for r in inc if (m := re.match(r"\s*\d+", str(r.get("reassignment_count", ""))))]
    if not nums:
        return
    first = sum(1 for _, n in nums if n == 0)
    pct = _pct(first, len(nums))
    rep.results["ITSM-AT-04-C01"] = result(
        "ITSM-AT-04-C01", MET if pct > 40 else GAP,
        f"{first} of {len(nums)} incidents resolved without reassignment ({pct}%; target > 40%)"
        + ("" if groups else "; PSE queues not configured (criteria_rules.pse_groups), so all incidents were counted"),
        b.table_ids("incident"), review=not groups)


def itsm_backlog(b, ctx, rep):
    """ITSM-AT-04-C03: no open P1/P2; P3/P4 mean age < 5 business days (snapshot at export time)."""
    rows = ctx.scoped(b.table("incident"))
    open_ = [r for r in rows if r.get("state") and not CLOSED.search(str(r["state"])) and r.get("_opened_at")]
    if not open_:
        return                  # an export of closed incidents says nothing about the backlog
    hi = [r for r in open_ if (_prio(r) or 9) <= 2]
    lo = [r for r in open_ if (_prio(r) or 9) > 2]
    age = statistics.mean((ctx.as_of - r["_opened_at"]).days * 5 / 7 for r in lo) if lo else 0.0
    rep.results["ITSM-AT-04-C03"] = result(
        "ITSM-AT-04-C03", MET if not hi and age < 5 else GAP,
        f"At export: {len(hi)} open P1/P2 incident(s); {len(lo)} open P3/P4 with mean age {age:.1f} business days (target < 5). "
        "One snapshot; the criterion asks for end of every shift.", [ref(r) for r in (hi + lo)[:8]], review=True)


def _kb(b, ctx) -> list[dict]:
    kb = b.table("kb_knowledge")
    mine = ctx.scoped(kb, ("u_application", "kb_category", "short_description", "cmdb_ci", "ownership_group"))
    return [r for r in mine if not re.search(r"retired|draft|outdated", str(r.get("workflow_state", "")), re.I)]


def itsm_kb_coverage(b, ctx, rep):
    """ITSM-AT-07-C01: a KB article for each of the top-20 incident categories."""
    inc, kb = _incidents(b, ctx), _kb(b, ctx)
    if not inc or not b.has_table("kb_knowledge"):
        return
    cats = Counter((r.get("subcategory") or r.get("category") or "").strip() for r in inc)
    top = [c for c, _ in cats.most_common(20) if c]
    if not top:
        return
    text = [f"{r.get('kb_category', '')} {r.get('short_description', '')}".lower() for r in kb]
    words = lambda c: [w for w in re.findall(r"[a-z]{4,}", c.lower())]  # noqa: E731
    covered = [c for c in top if any(any(w in t for w in words(c)) for t in text)]
    missing = [c for c in top if c not in covered]
    rep.results["ITSM-AT-07-C01"] = result(
        "ITSM-AT-07-C01", MET if not missing else GAP,
        f"KB articles found for {len(covered)} of {len(top)} top incident categories (matched on category and title words)"
        + (f"; none for: {', '.join(missing[:8])}" if missing else ""),
        b.table_ids("kb_knowledge") + b.table_ids("incident"), review=bool(missing))


def itsm_kb_currency(b, ctx, rep):
    """ITSM-AT-07-C02: published articles reviewed within 6 months, each with a review owner."""
    kb = _kb(b, ctx)
    if not kb:
        return
    owners = [f for f in _cfg(ctx).get("kb_owner_fields", []) if _has_col(kb, f)]
    when = lambda r: r.get("_u_last_reviewed") or r.get("_last_reviewed") or r.get("_sys_updated_on")  # noqa: E731
    dated = [r for r in kb if when(r)]
    if not dated:
        return
    fresh = [r for r in dated if when(r) >= ctx.months_ago(6)]
    owned = [r for r in kb if any(truthy(r.get(f)) for f in owners)] if owners else []
    ok = len(fresh) == len(kb) and (not owners or len(owned) == len(kb))   # no owner column: dates decide, reviewer checks owners
    rep.results["ITSM-AT-07-C02"] = result(
        "ITSM-AT-07-C02", MET if ok else GAP,
        f"{len(fresh)} of {len(kb)} article(s) reviewed or updated within 6 months; "
        + (f"{len(owned)} of {len(kb)} have a review owner ({', '.join(owners)})" if owners else "no review-owner column in the export"),
        [ref(r) for r in kb[:8]], review=not owners)


def itsm_kb_contribution(b, ctx, rep):
    """ITSM-AT-07-C04: at least 2 new or updated KB articles per month (last 90 days)."""
    kb = _kb(b, ctx)
    if not kb or not any(r.get("_sys_updated_on") or r.get("_sys_created_on") for r in kb):
        return
    recent = [r for r in kb if max(filter(None, (r.get("_sys_updated_on"), r.get("_sys_created_on"))), default=datetime.min)
              >= ctx.days_ago(90)]
    rep.results["ITSM-AT-07-C04"] = result(
        "ITSM-AT-07-C04", MET if len(recent) >= 6 else GAP,
        f"{len(recent)} article(s) created or updated in the last 90 days (target ≥ 6: 2 per month for the team)",
        [ref(r) for r in (recent or kb)[:8]])


def itsm_policy(b, ctx, rep):
    """ITSM-AT-09-C02: change control 100% compliant (backout and test plan) and Jenkins migration complete."""
    ch = _changes(b, ctx)
    if not ch and not _ci_configs(b):
        return
    bad = [r for r in ch if not (truthy(r.get("backout_plan")) and truthy(r.get("test_plan")))]
    j = _jenkins(b)
    parts = [f"{len(ch) - len(bad)} of {len(ch)} changes have backout and test plans" if ch else "no change records",
             f"Jenkins still in use ({', '.join(c.name for c in j)})" if j else
             ("no Jenkins pipeline" if _ci_configs(b) else "no CI configuration to check the Jenkins migration")]
    rep.results["ITSM-AT-09-C02"] = result("ITSM-AT-09-C02", GAP if bad or j else MET, "; ".join(parts),
                                           [ref(r) for r in (bad or ch)[:8]] + [c.ref() for c in j],
                                           review=not (ch and _ci_configs(b)))


def _complete(r: dict, fields: list[str]) -> bool:
    closed = bool(CLOSED.search(str(r.get("state", ""))))
    need = [f for f in fields if f not in ("resolved_at", "close_code", "close_notes") or closed]
    return all(truthy(r.get(f)) for f in need)


def itsm_audit(b, ctx, rep):
    """ITSM-AT-09-C03: > 95% of the latest 10 changes, 10 problems and 20 incidents are complete."""
    spec = _cfg(ctx).get("audit_fields", {})
    sample, used = [], []
    for table, n in (("change_request", 10), ("problem", 10), ("incident", 20)):
        rows = ctx.scoped(b.table(table), ("cmdb_ci", "business_service", "u_business_application", "cmdb_ci_business_app",
                                           "short_description", "assignment_group"))
        key = lambda r: r.get("_opened_at") or r.get("_start_date") or r.get("_sys_created_on") or datetime.min  # noqa: E731
        rows = sorted(rows, key=key, reverse=True)[:n]
        fields = [f for f in spec.get(table, []) if _has_col(rows, f)]
        if rows and fields:
            sample += [(r, fields) for r in rows]
            used.append(f"{len(rows)} {table} ({', '.join(fields)})")
    if not sample:
        return
    bad = [r for r, f in sample if not _complete(r, f)]
    pct = _pct(len(sample) - len(bad), len(sample))
    rep.results["ITSM-AT-09-C03"] = result(
        "ITSM-AT-09-C03", MET if pct > 95 else GAP,
        f"{len(sample) - len(bad)} of {len(sample)} sampled records complete ({pct}%; target > 95%). Sample: {'; '.join(used)}"
        + (f". Incomplete: {', '.join(str(r.get('number')) for r in bad[:6])}" if bad else "")
        + ". Retrieval of the evidence trail within 24 h needs confirming.",
        [ref(r) for r in bad[:8]] or [ref(r) for r, _ in sample[:8]], review=True)


def itsm_reporting_quality(b, ctx, rep):
    """ITSM-AT-11-C02: required reporting fields on ≥ 95% of incidents; no impossible durations."""
    inc = _incidents(b, ctx)
    fields = [f for f in _cfg(ctx).get("reporting_fields", []) if _has_col(inc, f)]
    if not inc or not fields:
        return
    ok = [r for r in inc if _complete(r, fields)]
    neg = [r for r in inc if r.get("_resolved_at") and r["_resolved_at"] < r["_opened_at"]]
    pct = _pct(len(ok), len(inc))
    rep.results["ITSM-AT-11-C02"] = result(
        "ITSM-AT-11-C02", MET if pct >= 95 and not neg else GAP,
        f"Reporting fields ({', '.join(fields)}) populated on {len(ok)} of {len(inc)} incidents ({pct}%; target 95%)"
        + (f"; {len(neg)} resolved before opened" if neg else ""), b.table_ids("incident"))


# ------------------------------------------------------------------------------------------------ SRE
def sre_autoscaling(b, ctx, rep):
    """SRE-AT-06-C05: HPA CPU target at (or below) the standard 70%. A scaling test is still needed for Met."""
    wl = _workloads(b)
    if not wl:
        return
    target = _cfg(ctx).get("hpa_cpu_target_pct", 70)
    hpas = [o for _, o in b.k8s if o.get("kind") == "HorizontalPodAutoscaler"]
    cpu = []
    for h in hpas:
        spec = h.get("spec", {}) or {}
        if spec.get("targetCPUUtilizationPercentage") is not None:
            cpu.append(spec["targetCPUUtilizationPercentage"])
        for m in spec.get("metrics", []) or []:
            res = (m or {}).get("resource", {}) or {}
            if res.get("name") == "cpu":
                cpu.append((res.get("target", {}) or {}).get("averageUtilization"))
    cpu = [c for c in cpu if isinstance(c, (int, float))]
    refs = [f"{eid}:{o['kind']}/{o.get('metadata', {}).get('name')}" for eid, o in wl]
    if not hpas:
        rep.results["SRE-AT-06-C05"] = result("SRE-AT-06-C05", GAP, "No HorizontalPodAutoscaler for the workloads in the manifests", refs)
    elif cpu and max(cpu) > target:
        rep.results["SRE-AT-06-C05"] = result("SRE-AT-06-C05", GAP, f"HPA CPU target {max(cpu)}% is above the {target}% standard", refs)
    # otherwise config_checks leaves it Insufficient Evidence until a scaling test record is reviewed


def sre_postmortems(b, ctx, rep):
    """SRE-AT-09-C01: every P1 incident has a post-mortem (Problem); within 5 business days when dates are exported."""
    p1 = [r for r in _incidents(b, ctx) if _prio(r) == 1]
    if not p1:
        return
    probs = {str(r.get("number")): r for r in b.table("problem")}
    linked = [r for r in p1 if truthy(r.get("problem_id"))]
    late = []
    for r in linked:
        p = probs.get(str(r["problem_id"]))
        end = r.get("_resolved_at") or r["_opened_at"]
        if p and p.get("_opened_at") and (p["_opened_at"] - end).days * 5 / 7 > 5:
            late.append(r)
    ok = len(linked) == len(p1) and not late
    rep.results["SRE-AT-09-C01"] = result(
        "SRE-AT-09-C01", MET if ok else GAP,
        f"{len(linked)} of {len(p1)} P1 incident(s) linked to a Problem / post-mortem"
        + (f"; {len(late)} opened more than 5 business days after resolution" if late else "")
        + ". Significant P2s (10 days) need the assessor's selection.", [ref(r) for r in p1[:8]], review=True)


def sre_pipeline(b, ctx, rep):
    """SRE-AT-12-C01 (automated, no Jenkins, no manual deploy step), C02 (quality gates), C05 (rollback stage)."""
    cis = _ci_configs(b)
    if not cis:
        return
    text = "\n".join(c.text for c in cis)
    refs = [c.ref() for c in cis]
    j = _jenkins(b)
    manual = re.search(r"when:\s*manual|^\s*input\s*\{|manual[ _-]?(deploy|step|approval)", text, re.I | re.M)
    rep.results["SRE-AT-12-C01"] = result(
        "SRE-AT-12-C01", GAP if j or manual else MET,
        "; ".join(filter(None, [f"Jenkins in use ({', '.join(c.name for c in j)})" if j else "",
                                "manual deployment step in the pipeline" if manual else "",
                                "" if j or manual else f"Automated pipeline found ({', '.join(c.name for c in cis)})"]))
        + ". Deployment logs should confirm no manual production deploys.", refs, review=True)
    gates = {"SAST": GATES["sast"], "unit tests": GATES["tests"], "smoke tests": r"smoke"}
    missing = [g for g, pat in gates.items() if not re.search(pat, text, re.I)]
    rep.results["SRE-AT-12-C02"] = result(
        "SRE-AT-12-C02", GAP if missing else MET,
        f"Pipeline stages: {', '.join(g for g in gates if g not in missing) or 'none'} present"
        + (f"; missing: {', '.join(missing)}" if missing else "") + ". Whether a bypass path exists needs checking.",
        refs, review=not missing)
    rollback = re.search(r"rollback|rollout undo|helm rollback|revert|previous (image|release)", text, re.I)
    rep.results["SRE-AT-12-C05"] = result(
        "SRE-AT-12-C05", MET if rollback else GAP,
        ("Rollback stage defined in the pipeline" if rollback else "No rollback stage in the pipeline configuration")
        + ("; a rollback test within 6 months needs a record" if rollback else ""), refs, review=bool(rollback))


def sre_integration_errors(b, ctx, rep):
    """SRE-AT-13-C03: timeouts and retries configured for integrations (code review stays with the assessor)."""
    cfgs = [it for it in b.items.values() if it.kind in (Kind.K8S, Kind.OTHER)]
    text = "\n".join(it.text for it in cfgs)
    if not text.strip():
        return
    has_t, has_r = bool(TIMEOUT.search(text)), bool(RETRY.search(text))
    rep.results["SRE-AT-13-C03"] = result(
        "SRE-AT-13-C03", MET if has_t and has_r else GAP,
        f"Timeouts {'found' if has_t else 'not found'} and retries {'found' if has_r else 'not found'} in configuration. "
        "Error handling per endpoint (permanent errors logged and alerted) needs a code or design review.",
        [it.ref() for it in cfgs], review=True)


RULES = [cmdb_placement, cmdb_stale, cmdb_labels, cmdb_change_ci, cmdb_tier_priority, cmdb_alert_binding,
         itsm_jenkins, itsm_time_worked, itsm_problem_change, itsm_fcr, itsm_backlog, itsm_kb_coverage, itsm_kb_currency,
         itsm_kb_contribution, itsm_policy, itsm_audit, itsm_reporting_quality,
         sre_autoscaling, sre_postmortems, sre_pipeline, sre_integration_errors]


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    for rule in RULES:
        rule(b, ctx, rep)
    rep.status = f"ran: {len(rep.results)} criteria settled by {len(RULES)} rules (the others lacked the data they need)"
    return rep
