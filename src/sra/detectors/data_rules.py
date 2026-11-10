"""Rules for criteria defined on ServiceNow tables and team registers beyond the core incident/change/CMDB exports.

Inputs (each optional; a rule runs only when its data is in the evidence, otherwise the criterion is left for the
assessor or the LLM):
  ServiceNow: cmdb_ci_service_auto, svc_ci_assoc, service_offering, cmdb_rel_ci, cmdb_ci (hosting classes too),
    reconcile_duplicate_task, discovery_schedule/status/device_history/credentials, cmdb_identifier, sys_audit,
    task_ci, task_cmdb_ci_service, alm_hardware, cmdb_sam_sw_install, cmdb_software_product_model,
    contract_sla, task_sla, sc_cat_item, problem (RCA dates)
  Registers (config/registers.yaml): risk, exception, improvement, vendor, contract, debt, corrective_action,
    postmortem_action, cmdb_remediation, audit_finding, sla_breach, knowledge_migration, roster, access_review,
    automation, toil, deployment

Field names that vary between ServiceNow instances are under `data_rules.fields` in config/detectors.yaml (CONFIRM).
Thresholds are the templates' own (TH-xx from CMDB-STD-00 where the template refers to one). Where the data shows
only part of a criterion (an approval, a report, a test), the result is flagged for human review.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from ..catalog.models import Result
from ..evidence.bundle import EvidenceBundle
from ..evidence.redact import parse_datetime
from ..scoring.cmdb import level_from_pct
from ..scoring.cmdb import threshold as th
from .base import Context, DetectorReport, ref, result, truthy

NAME = "data_rules"
MET, GAP = Result.MET, Result.GAP

# Standard ServiceNow field names; override per instance under data_rules.fields (CONFIRM).
FIELDS = {
    "svc_assoc_service": "service_id", "svc_assoc_ci": "ci_id",
    "offering_class": "service_classification",
    "service_tier": "busines_criticality",          # sic: the out-of-box field on service tables is misspelt
    "tier_approved_by": "u_tier_approved_by",
    "attested_date": "attested_date",
    "duplicate_of": "duplicate_of",
    "discovery_source": "discovery_source",
    "used_for": "used_for",
    "status_schedule": "dscheduler", "status_state": "state",
    "device_status": "status",
    "credential_vault": "credential_storage_id", "credential_owner": "u_owner",
    "identifier_class": "applies_to",
    "audit_table": "tablename", "audit_field": "fieldname", "audit_ci": "documentkey",
    "task_ci_task": "task", "task_ci_ci": "ci_item",
    "impacted_task": "task", "change_approved": "approval_set",
    "change_conflict": "conflict_status",
    "hardware_ci": "ci",
    "install_normalized": "norm_product", "install_host": "installed_on", "install_model": "software_model",
    "model_eol": "end_of_life_date",
    "problem_rca_date": "u_root_cause_date", "problem_fix_date": "resolved_at",
    "sla_task": "task", "sla_priority": "task.priority", "sla_breached": "has_breached",
    "sla_collection": "collection", "sla_target": "target",
    "cat_group": "group", "cat_owner": "owner", "cat_sla": "u_sla_target",
    "cat_ci": ["cmdb_ci", "u_cmdb_ci", "service_offering", "u_business_service"],
    "naming": r"(?i)\b(prod|production|uat|qa|test|dev|development|stag(e|ing)|dr|pre-?prod|perf)\b",
}
PROD = re.compile(r"^\s*(prod|production)\b", re.I)
APP_CLASS = re.compile(r"business_app|service|appl|offering", re.I)
NO = re.compile(r"^\s*(no|n|false|0|none|-|n/?a)?\s*$", re.I)


# ------------------------------------------------------------------------------------------------ helpers
def f(ctx: Context, key: str):
    return ((ctx.cfg.get("data_rules") or {}).get("fields") or {}).get(key, FIELDS[key])


def when(row: dict, field: str) -> datetime | None:
    return row.get("_" + field) or (parse_datetime(str(row.get(field))) if row.get(field) else None)


def yes(v) -> bool:
    return v is not None and not NO.match(str(v))


def pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def table(b, name):
    return b.table(name)


def reg(b, ctx, t: str, scoped: bool = True) -> list[dict]:
    rows = b.table("register:" + t)
    return ctx.scoped(rows, ("application",)) if scoped else rows


def name(r: dict) -> str:
    return str(r.get("name") or r.get("number") or r.get("id") or r.get("title") or "").strip()


def emit(rep, cid, ok, why, refs=(), *, p=None, target=None, measured="", review=False, level=None):
    r = result(cid, MET if ok else GAP, why, list(refs)[:10], review=review)
    if cid.startswith("CMDB-"):
        r.measured_value = measured or (f"{p}%" if p is not None else "")
        r.maturity_level = level if level is not None else (4 if ok else level_from_pct(p, target) if p is not None else 1)
    rep.results[cid] = r


def quarter_bounds(d: datetime, back: int = 0) -> tuple[datetime, datetime]:
    """Start and end of the calendar quarter `back` quarters before the one containing d."""
    q = (d.month - 1) // 3 - back
    y = d.year + q // 4
    q %= 4
    start = datetime(y, q * 3 + 1, 1)
    end = datetime(y + (q == 3), (q * 3 + 3) % 12 + 1, 1)
    return start, end


# ------------------------------------------------------------------------------------------------ CMDB model
def services(b, ctx) -> list[dict]:
    """This application's Application Services: named after it, or consumed by its Business Application."""
    rows = table(b, "cmdb_ci_service_auto")
    by_rel = {str(r.get("child")) for r in b.table("cmdb_rel_ci") if ctx.matches(r, ("parent",))}
    return [r for r in rows if ctx.matches(r, ("name",)) or name(r) in by_rel]


def assoc(b, ctx) -> dict[str, set[str]]:
    """service name -> CI names (svc_ci_assoc)."""
    out = defaultdict(set)
    for r in table(b, "svc_ci_assoc"):
        out[str(r.get(f(ctx, "svc_assoc_service"), "")).strip()].add(str(r.get(f(ctx, "svc_assoc_ci"), "")).strip())
    return out


def hosting(b, ctx) -> list[dict]:
    """This application's infrastructure CIs: named after it, or members of its Application Services."""
    svc_names = {name(s) for s in services(b, ctx)}
    members = {ci for s, cis in assoc(b, ctx).items() if s in svc_names for ci in cis}
    return [r for r in b.table("cmdb_ci") if r.get("sys_class_name") and not APP_CLASS.search(str(r["sys_class_name"]))
            and (ctx.matches(r, ("name",)) or name(r) in members)]


def is_prod(r: dict) -> bool:
    env = str(r.get("environment", "")).strip()
    return not env or bool(PROD.match(env))


def cmdb_service_model(b, ctx, rep):
    svcs = services(b, ctx)
    if not table(b, "cmdb_ci_service_auto"):
        return
    ba = ctx.app_name.strip().lower()
    prod = [s for s in svcs if PROD.match(str(s.get("environment", "")))]
    naming = re.compile(f(ctx, "naming"))
    named_ok = [s for s in svcs if naming.search(name(s)) and name(s).lower() != ba]
    same = [s for s in svcs if name(s).lower() == ba]
    blank = [s for s in svcs if not truthy(s.get("environment"))]
    p = pct(len(named_ok), len(svcs))
    ok = bool(prod) and p >= 95 and not same and not blank
    emit(rep, "CMDB-AT-01-C04", ok,
         f"{len(svcs)} Application Service(s) for {ctx.app_name}; Production service: {'yes' if prod else 'none'}; naming compliant "
         f"{p}% (target 95%); named the same as the Business Application: {len(same)}; blank environment: {len(blank)}. "
         "Naming pattern is configurable (data_rules.fields.naming, CONFIRM).", [ref(s, "name") for s in svcs],
         p=p, target=95, measured=f"Production service {'present' if prod else 'missing'}; naming {p}%",
         level=None if prod else 0)


def cmdb_offerings(b, ctx, rep):
    offs = table(b, "service_offering")
    if not offs:
        return
    prod = [s for s in services(b, ctx) if PROD.match(str(s.get("environment", "")))]
    if not prod:
        return
    kind = {name(o): str(o.get(f(ctx, "offering_class"), "")).lower() for o in offs}
    links = defaultdict(set)                             # service -> offering classes that depend on it
    for r in b.table("cmdb_rel_ci"):
        if str(r.get("parent")) in kind:
            links[str(r.get("child"))].add(kind[str(r.get("parent"))])
    bus = [s for s in prod if any("business" in k for k in links[name(s)])]
    tech = [s for s in prod if any("technical" in k for k in links[name(s)])]
    pb, pt = pct(len(bus), len(prod)), pct(len(tech), len(prod))
    emit(rep, "CMDB-AT-01-C05", pb == 100 and pt >= 90,
         f"Business offering linkage {pb}% (target 100%), technical {pt}% (target 90%) over {len(prod)} Production Application Service(s)",
         [ref(s, "name") for s in prod], p=min(pb, pt / 0.9), target=100, measured=f"business {pb}%, technical {pt}%")


def cmdb_discovery_coverage(b, ctx, rep):
    status = table(b, "discovery_status")
    if not status:
        return
    sched = [r for r in table(b, "discovery_schedule") if yes(r.get("active", "true"))]
    inventory = {name(r) for r in sched} or {str(r.get(f(ctx, "status_schedule"))) for r in status}
    good = {str(r.get(f(ctx, "status_schedule"))) for r in status
            if re.search(r"complete", str(r.get(f(ctx, "status_state"), "")), re.I)
            and (when(r, "sys_created_on") or datetime.min) >= ctx.days_ago(7)}
    covered = inventory & good
    p, target = pct(len(covered), len(inventory)), th("TH-13", ctx.tier) or 95
    missing = sorted(inventory - covered)
    emit(rep, "CMDB-AT-02-C01", p >= target,
         f"{len(covered)} of {len(inventory)} discovery source(s) completed a run in the last 7 days ({p}%; TH-13 {target:g}%)"
         + (f"; not run: {', '.join(missing[:5])}" if missing else ""), b.table_ids("discovery_status"), p=p, target=target)


def cmdb_ire(b, ctx, rep):
    ids = table(b, "cmdb_identifier")
    if not ids:
        return
    ruled = {str(r.get(f(ctx, "identifier_class"), "")).strip() for r in ids if yes(r.get("active", "true"))}
    classes = {str(r["sys_class_name"]).strip() for r in hosting(b, ctx)} or \
              {str(r.get("sys_class_name", "")).strip() for r in b.table("cmdb_ci") if r.get("sys_class_name")}
    if not classes:
        return
    p = pct(len(classes & ruled), len(classes))
    emit(rep, "CMDB-AT-02-C02", p == 100,
         f"Identification rules exist for {len(classes & ruled)} of {len(classes)} CI class(es) in scope ({p}%)"
         + (f"; none for {', '.join(sorted(classes - ruled))}" if classes - ruled else "")
         + ". Discovery overwrites of owner-maintained fields need a sys_audit sample.", b.table_ids("cmdb_identifier"),
         p=p, target=100, review=True)


def cmdb_duplicates(b, ctx, rep):
    tasks = table(b, "reconcile_duplicate_task")
    cis = hosting(b, ctx)
    dup_field = f(ctx, "duplicate_of")
    if not tasks and not any(dup_field in r for r in cis):
        return
    limit = th("TH-11", ctx.tier) or 1
    by_class = defaultdict(list)
    for r in cis:
        by_class[r["sys_class_name"]].append(r)
    rates = {c: pct(sum(1 for r in rows if truthy(r.get(dup_field))), len(rows)) for c, rows in by_class.items()}
    worst = max(rates.items(), key=lambda kv: kv[1], default=("—", 0.0))
    old = [t for t in tasks if not re.search(r"closed|complete|cancel", str(t.get("state", "")), re.I)
           and (when(t, "sys_created_on") or ctx.as_of) < ctx.days_ago(30)]
    emit(rep, "CMDB-AT-02-C03", worst[1] <= limit and not old,
         f"Worst duplicate rate {worst[1]}% ({worst[0]}; TH-11 ≤ {limit:g}%); open duplicate tasks older than 30 days: {len(old)}",
         [ref(t) for t in old] or b.table_ids("reconcile_duplicate_task"), measured=f"worst class {worst[1]}%; {len(old)} aged tasks",
         level=4 if worst[1] <= limit and not old else 2 if worst[1] <= 2 * limit else 1)


def cmdb_enrichment(b, ctx, rep):
    src = f(ctx, "discovery_source")
    cis = [r for r in hosting(b, ctx) if is_prod(r) and truthy(r.get(src)) and not re.match(r"manual", str(r.get(src)), re.I)]
    if not cis or not table(b, "svc_ci_assoc"):
        return
    members = {ci for cis_ in assoc(b, ctx).values() for ci in cis_}
    ok = [r for r in cis if truthy(r.get("support_group")) and truthy(r.get(f(ctx, "used_for"))) and name(r) in members]
    p = pct(len(ok), len(cis))
    emit(rep, "CMDB-AT-02-C05", p >= 90, f"{len(ok)} of {len(cis)} discovered production CIs have support group, used for and "
         f"service membership ({p}%; target 90%)", [ref(r, "name") for r in cis if r not in ok] or [ref(r, "name") for r in cis],
         p=p, target=90)


def cmdb_discovery_health(b, ctx, rep):
    dev, creds = table(b, "discovery_device_history"), table(b, "discovery_credentials")
    if not dev and not creds:
        return
    errs = [r for r in dev if re.search(r"error|fail", str(r.get(f(ctx, "device_status"), "")), re.I)]
    rate = pct(len(errs), len(dev))
    active = [c for c in creds if yes(c.get("active", "true"))]
    good = [c for c in active if truthy(c.get(f(ctx, "credential_vault"))) and truthy(c.get(f(ctx, "credential_owner")))
            and (when(c, "sys_updated_on") or datetime.min) >= ctx.days_ago(90)]
    cp = pct(len(good), len(active))
    ok = (not dev or rate < 5) and (not active or cp == 100)
    emit(rep, "CMDB-AT-02-C06", ok,
         (f"Device probe error rate {rate}% ({len(errs)} of {len(dev)}; target < 5%)" if dev else "No device history exported")
         + (f"; {len(good)} of {len(active)} credentials vault-backed, owned and rotated within 90 days ({cp}%)" if active else "")
         + ". Least-privilege documentation needs checking.", b.table_ids("discovery_device_history") + b.table_ids("discovery_credentials"),
         measured=f"error rate {rate}%; credentials {cp}%", review=True,
         level=4 if ok else 2 if rate < 10 else 1)


def cmdb_lifecycle_governance(b, ctx, rep):
    audit, links = table(b, "sys_audit"), table(b, "task_ci")
    if not audit or not links:
        return
    mine = {name(r) for r in hosting(b, ctx)} | {name(s) for s in services(b, ctx)}
    trans = [r for r in audit if str(r.get(f(ctx, "audit_field"))) in ("install_status", "operational_status")
             and str(r.get(f(ctx, "audit_table"), "cmdb_ci")).startswith("cmdb_ci")
             and str(r.get(f(ctx, "audit_ci"))) in mine and when(r, "sys_created_on")]
    if not trans:
        return
    changes = {str(c.get("number")): c for c in b.table("change_request")}
    windows = defaultdict(list)                         # CI -> [(start, end)]
    for t in links:
        c = changes.get(str(t.get(f(ctx, "task_ci_task"))))
        if c and (c.get("_start_date") or c.get("_end_date")):
            s = c.get("_start_date") or c["_end_date"]
            e = (c.get("_end_date") or s) + timedelta(hours=24)
            windows[str(t.get(f(ctx, "task_ci_ci")))].append((s, e))
    gov = [r for r in trans if any(s <= when(r, "sys_created_on") <= e for s, e in windows[str(r.get(f(ctx, "audit_ci")))])]
    p = pct(len(gov), len(trans))
    emit(rep, "CMDB-AT-03-C02", p >= 95, f"{len(gov)} of {len(trans)} life cycle state changes happened under a change linked to the CI "
         f"({p}%; target 95%)", [ref(r, f(ctx, "audit_ci")) for r in trans if r not in gov][:8] or b.table_ids("sys_audit"),
         p=p, target=95)


def cmdb_hosting_linked(b, ctx, rep):
    if not table(b, "svc_ci_assoc"):
        return
    cis = [r for r in hosting(b, ctx) if is_prod(r)]
    if not cis:
        return
    members = {ci for cis_ in assoc(b, ctx).values() for ci in cis_}
    ok = [r for r in cis if name(r) in members]
    p, target = pct(len(ok), len(cis)), 98 if ctx.tier == 1 else 90
    emit(rep, "CMDB-AT-03-C03", p >= target, f"{len(ok)} of {len(cis)} production hosting CIs belong to an Application Service "
         f"({p}%; target {target}%)", [ref(r, "name") for r in cis if r not in ok] or [ref(r, "name") for r in cis], p=p, target=target)


def cmdb_asset_link(b, ctx, rep):
    assets = table(b, "alm_hardware")
    if not assets:
        return
    hw = [r for r in hosting(b, ctx) if re.search(r"server|hardware|computer|netgear|storage", r["sys_class_name"], re.I)
          and not re.search(r"vm|cloud|virtual|kubernetes|container", r["sys_class_name"], re.I)]
    if not hw:
        return
    linked = {str(a.get(f(ctx, "hardware_ci"))).strip() for a in assets}
    ok = [r for r in hw if name(r) in linked]
    p = pct(len(ok), len(hw))
    emit(rep, "CMDB-AT-03-C04", p >= 95, f"{len(ok)} of {len(hw)} hardware CIs have a linked asset record ({p}%; target 95%)",
         [ref(r, "name") for r in hw if r not in ok] or [ref(r, "name") for r in hw], p=p, target=95)


def cmdb_sw_normalized(b, ctx, rep):
    installs = table(b, "cmdb_sam_sw_install")
    if not installs:
        return
    hosts = {name(r) for r in hosting(b, ctx)}
    mine = [r for r in installs if str(r.get(f(ctx, "install_host"))) in hosts] or installs
    ok = [r for r in mine if truthy(r.get(f(ctx, "install_normalized")))]
    p = pct(len(ok), len(mine))
    emit(rep, "CMDB-AT-03-C06", p >= 90, f"{len(ok)} of {len(mine)} software installs are normalized to a product ({p}%; target 90%)",
         b.table_ids("cmdb_sam_sw_install"), p=p, target=90)


def cmdb_service_population(b, ctx, rep):
    if not table(b, "svc_ci_assoc"):
        return
    prod = [s for s in services(b, ctx) if PROD.match(str(s.get("environment", "")))]
    if not prod:
        return
    a = assoc(b, ctx)
    ok = [s for s in prod if a.get(name(s))]
    p, target = pct(len(ok), len(prod)), 90 if ctx.tier == 3 else 100
    emit(rep, "CMDB-AT-06-C02", p >= target, f"{len(ok)} of {len(prod)} Production Application Services have member CIs ({p}%; "
         f"target {target}%)", [ref(s, "name") for s in prod], p=p, target=target)


def cmdb_k8s_namespaces(b, ctx, rep):
    ns = [r for r in hosting(b, ctx) if re.search(r"namespace", r["sys_class_name"], re.I)]
    if not ns or not table(b, "svc_ci_assoc"):
        return
    owners = Counter(ci for cis in assoc(b, ctx).values() for ci in cis)
    ok = [r for r in ns if owners[name(r)] == 1]
    p, target = pct(len(ok), len(ns)), 100 if ctx.tier == 1 else 95
    emit(rep, "CMDB-AT-07-C02", p >= target, f"{len(ok)} of {len(ns)} namespaces map to exactly one Application Service ({p}%; "
         f"target {target}%)", [ref(r, "name") for r in ns], p=p, target=target)


def cmdb_cloud_stale(b, ctx, rep):
    cloud = [r for r in hosting(b, ctx) if re.search(r"cloud|vm_instance|aws|azure|gcp|gce", r["sys_class_name"], re.I)
             and "last_discovered" in r]
    if not cloud:
        return
    installed = [r for r in cloud if not re.search(r"retired|absent|decommission", f"{r.get('install_status', '')} "
                                                   f"{r.get('operational_status', '')}", re.I)]
    stale = [r for r in installed if (when(r, "last_discovered") or datetime.min) < ctx.days_ago(7)]
    rate = pct(len(stale), len(cloud))
    emit(rep, "CMDB-AT-07-C04", rate <= 2, f"{len(stale)} of {len(cloud)} cloud CIs unseen for more than 7 days but still installed "
         f"({rate}%; target ≤ 2%)", [ref(r, "name") for r in stale] or [ref(r, "name") for r in cloud],
         measured=f"{rate}% stale", level=4 if rate <= 2 else 3 if rate <= 5 else 2 if rate <= 10 else 1)


def _changes(b, ctx) -> list[dict]:
    rows = ctx.scoped(b.table("change_request"), ("cmdb_ci", "business_service", "u_business_application", "short_description"))
    days = ctx.cfg["rubric_rules"]["change_window_days"]
    return [r for r in rows if (r.get("_start_date") or r.get("_opened_at") or datetime.min) >= ctx.days_ago(days)]


def cmdb_impacted_services(b, ctx, rep):
    imp = table(b, "task_cmdb_ci_service")
    target = th("TH-06", ctx.tier)
    if not imp or target is None:
        return
    ch = [c for c in _changes(b, ctx) if not re.search(r"standard", str(c.get("type", "")), re.I)]
    if not ch:
        return
    first = {}
    for r in imp:
        t, w = str(r.get(f(ctx, "impacted_task"))), when(r, "sys_created_on")
        if w and (t not in first or w < first[t]):
            first[t] = w
    ok = [c for c in ch if c.get("number") in first and first[c["number"]] <= (
        when(c, f(ctx, "change_approved")) or c.get("_start_date") or ctx.as_of)]
    p = pct(len(ok), len(ch))
    emit(rep, "CMDB-AT-08-C02", p >= target, f"{len(ok)} of {len(ch)} changes had Impacted Services calculated before approval "
         f"({p}%; TH-06 {target:g}%)", [ref(c) for c in ch if c not in ok] or [ref(c) for c in ch], p=p, target=target)


def cmdb_conflict_check(b, ctx, rep):
    field = f(ctx, "change_conflict")
    ch = [c for c in _changes(b, ctx) if field in c and not re.search(r"standard|emergency", str(c.get("type", "")), re.I)]
    if not ch:
        return
    ok = [c for c in ch if truthy(c.get(field)) and not re.search(r"not run", str(c[field]), re.I)]
    p = pct(len(ok), len(ch))
    emit(rep, "CMDB-AT-08-C06", p == 100, f"Conflict detection ran on {len(ok)} of {len(ch)} normal changes ({p}%; target 100%)",
         [ref(c) for c in ch if c not in ok] or [ref(c) for c in ch], p=p, target=100)


def cmdb_eol_dates(b, ctx, rep):
    models = table(b, "cmdb_software_product_model")
    if not models:
        return
    used = {str(r.get(f(ctx, "install_model"))) for r in table(b, "cmdb_sam_sw_install")}
    inuse = [m for m in models if name(m) in used] or models
    ok = [m for m in inuse if truthy(m.get(f(ctx, "model_eol")))]
    p = pct(len(ok), len(inuse))
    emit(rep, "CMDB-AT-09-C01", p >= 95, f"{len(ok)} of {len(inuse)} product models in use have an end-of-life date ({p}%; target 95%)",
         [ref(m, "name") for m in inuse if m not in ok] or b.table_ids("cmdb_software_product_model"), p=p, target=95)


def cmdb_tier_approved(b, ctx, rep):
    svcs = services(b, ctx)
    if not svcs:
        return
    tier_f, appr_f = f(ctx, "service_tier"), f(ctx, "tier_approved_by")
    tiered = [s for s in svcs if truthy(s.get(tier_f) or s.get("business_criticality"))]
    has_appr = any(appr_f in s for s in svcs)
    ok = [s for s in tiered if not has_appr or truthy(s.get(appr_f))]
    p = pct(len(ok), len(svcs))
    emit(rep, "CMDB-AT-10-C01", p == 100, f"{len(ok)} of {len(svcs)} Application Services have a tier"
         + (" with a recorded approver" if has_appr else " (no approval field exported; approval needs confirming)") + f" ({p}%)",
         [ref(s, "name") for s in svcs], p=p, target=100, review=not has_appr)


def cmdb_attestation(b, ctx, rep):
    fld = f(ctx, "attested_date")
    from .rubric_rules import _app_ci
    recs = [r for r in services(b, ctx) + _app_ci(b, ctx) if fld in r]
    if not recs:
        return
    months = th("TH-09", ctx.tier) or 12
    cur = [r for r in recs if (when(r, fld) or datetime.min) >= ctx.months_ago(months)]
    p, target = pct(len(cur), len(recs)), 100 if ctx.tier == 1 else 95
    emit(rep, "CMDB-AT-11-C01", p >= target, f"{len(cur)} of {len(recs)} records attested within {months:g} months (TH-09; "
         f"{p}%, target {target}%)", [ref(r, "name") for r in recs if r not in cur] or [ref(r, "name") for r in recs],
         p=p, target=target)


# ------------------------------------------------------------------------------------------------ registers (CMDB)
def _on_time(rows: list[dict], ctx) -> tuple[list, list, list]:
    """(due, on_time, overdue_open) for action-style registers."""
    due = [r for r in rows if r.get("_due_date") and (r["_due_date"] <= ctx.as_of or r.get("_completed_date"))]
    on = [r for r in due if r.get("_completed_date") and r["_completed_date"] <= r["_due_date"] + timedelta(days=1)]
    late_open = [r for r in due if not r.get("_completed_date") and r["_due_date"] < ctx.as_of]
    return due, on, late_open


def cmdb_remediation(b, ctx, rep):
    rows = reg(b, ctx, "cmdb_remediation")
    if not rows:
        return
    due, on, _ = _on_time(rows, ctx)
    open_no_owner = [r for r in rows if not r.get("_completed_date") and not truthy(r.get("owner"))]
    p = pct(len(on), len(due))
    emit(rep, "CMDB-AT-04-C05", (not due or p >= 90) and not open_no_owner,
         f"{len(on)} of {len(due)} remediation tasks due were closed within SLA ({p}%; target 90%); open without owner: {len(open_no_owner)}",
         b.table_ids("register:cmdb_remediation"), p=p if due else 100.0, target=90)


def cmdb_deferrals(b, ctx, rep):
    rows = [r for r in reg(b, ctx, "risk") if re.search(r"defer|accept", str(r.get("status", "")), re.I) or truthy(r.get("accepted_by"))]
    if not rows:
        return
    ok = [r for r in rows if truthy(r.get("accepted_by")) and (r.get("_acceptance_expiry") or datetime.min) >= ctx.as_of]
    p = pct(len(ok), len(rows))
    emit(rep, "CMDB-AT-09-C04", p == 100, f"{len(ok)} of {len(rows)} deferred items have an unexpired risk acceptance ({p}%)",
         b.table_ids("register:risk"), p=p, target=100)


def cmdb_debt(b, ctx, rep):
    rows = reg(b, ctx, "debt", scoped=False)
    if not rows:
        return
    linked = [r for r in rows if truthy(r.get("application"))]
    p = pct(len(linked), len(rows))
    emit(rep, "CMDB-AT-09-C05", p == 100, f"{len(linked)} of {len(rows)} technical debt items are linked to a Business Application ({p}%)",
         b.table_ids("register:debt"), p=p, target=100)
    hi = [r for r in rows if re.search(r"critical|high|^[12]\b", str(r.get("severity", "")), re.I)]
    qs, qe = quarter_bounds(ctx.as_of, back=1)
    open_at = [r for r in hi if (r.get("_opened_date") or datetime.min) < qs and (not r.get("_completed_date") or r["_completed_date"] >= qs)]
    if open_at:
        closed = [r for r in open_at if r.get("_completed_date") and qs <= r["_completed_date"] < qe]
        v = pct(len(closed), len(open_at))
        emit(rep, "CMDB-AT-09-C06", v >= 20, f"{len(closed)} of {len(open_at)} Critical/High debt items open at the start of "
             f"{qs.date()}–{(qe - timedelta(days=1)).date()} were closed in that quarter ({v}%; target ≥ 20%). Reporting to REO needs confirming.",
             b.table_ids("register:debt"), measured=f"{v}% per quarter", level=4 if v >= 20 else 3 if v >= 15 else 2 if v >= 10 else 1,
             review=True)


SLA_DAYS = {"critical": 14, "high": 30, "medium": 60, "low": 90}      # CMDB-AT-11-C03 (Critical 10 business days)


def cmdb_audit_findings(b, ctx, rep):
    rows = reg(b, ctx, "audit_finding")
    if not rows:
        return
    owned = [r for r in rows if truthy(r.get("owner"))]

    def in_sla(r):
        days = next((d for k, d in SLA_DAYS.items() if k in str(r.get("severity", "")).lower()), 90)
        start = r.get("_opened_date")
        if not start:
            return bool(r.get("_completed_date"))
        end = r.get("_completed_date") or ctx.as_of
        return end <= start + timedelta(days=days)
    ok = [r for r in rows if in_sla(r)]
    po, ps = pct(len(owned), len(rows)), pct(len(ok), len(rows))
    emit(rep, "CMDB-AT-11-C03", po == 100 and ps >= 90, f"{len(owned)} of {len(rows)} audit findings owned ({po}%); {len(ok)} within "
         f"SLA by severity ({ps}%; target 90%)", b.table_ids("register:audit_finding"), p=min(po, ps), target=90,
         measured=f"owned {po}%, within SLA {ps}%")


def cmdb_exceptions(b, ctx, rep):
    rows = reg(b, ctx, "exception")
    if not rows:
        return
    valid = [r for r in rows if truthy(r.get("approved_by")) and (r.get("_expiry_date") or datetime.min) >= ctx.as_of]
    p = pct(len(valid), len(rows))
    emit(rep, "CMDB-AT-11-C05", p == 100, f"{len(valid)} of {len(rows)} exceptions are approved and unexpired ({p}%)",
         b.table_ids("register:exception"), p=p, target=100)


# ------------------------------------------------------------------------------------------------ ITSM
def itsm_rca_cycle(b, ctx, rep):
    rca_f, fix_f = f(ctx, "problem_rca_date"), f(ctx, "problem_fix_date")
    probs = [p for p in ctx.scoped(b.table("problem"), ("cmdb_ci", "business_service", "short_description", "assignment_group"))
             if re.match(r"\s*[12]", str(p.get("priority", ""))) and when(p, rca_f) and when(p, fix_f)]
    if not probs:
        return
    days = [(when(p, fix_f) - when(p, rca_f)).total_seconds() / 86400 for p in probs]
    med = statistics.median(days)
    emit(rep, "ITSM-AT-03-C04", med < 14, f"Median RCA-to-fix {med:.1f} days over {len(probs)} P1/P2 Problem(s) (target < 14). "
         "Monthly reporting needs confirming.", [ref(p) for p in probs], review=True)


def itsm_roster(b, ctx, rep):
    rows = reg(b, ctx, "roster")
    if not rows:
        return
    crit = [r for r in rows if yes(r.get("critical", "yes"))]
    agents = defaultdict(set)
    for r in crit:
        if yes(r.get("trained", "yes")):
            agents[str(r.get("queue", "")).strip()].add(str(r.get("agent", "")).strip().lower())
    queues = {str(r.get("queue", "")).strip() for r in crit}
    thin = sorted(q for q in queues if len(agents[q]) < 2)
    emit(rep, "ITSM-AT-04-C05", not thin, f"{len(queues) - len(thin)} of {len(queues)} critical queue(s) have at least 2 trained agents"
         + (f"; fewer for: {', '.join(thin)}" if thin else "") + ". Shift and holiday coverage needs checking.",
         b.table_ids("register:roster"), review=True)


def _cat_items(b, ctx):
    rows = table(b, "sc_cat_item")
    return rows, [r for r in rows if ctx.matches(r, ("name", "short_description", "category", "u_application"))]


def itsm_catalog(b, ctx, rep):
    rows, mine = _cat_items(b, ctx)
    if not rows:
        return
    active = [r for r in mine if yes(r.get("active", "true"))]
    emit(rep, "ITSM-AT-05-C01", bool(active), f"{len(active)} active catalog item(s) for {ctx.app_name}"
         + ("" if active else " in the exported Service Catalog"), b.table_ids("sc_cat_item"))
    if not active:
        return
    need = (f(ctx, "cat_group"), f(ctx, "cat_owner"), f(ctx, "cat_sla"))
    full = [r for r in active if all(truthy(r.get(k)) for k in need)]
    emit(rep, "ITSM-AT-05-C02", len(full) == len(active), f"{len(full)} of {len(active)} active item(s) have a fulfillment group, "
         f"service owner and SLA target ({', '.join(need)})", [ref(r, "name") for r in active])
    cis = f(ctx, "cat_ci")
    linked = [r for r in active if any(truthy(r.get(k)) for k in cis)]
    emit(rep, "ITSM-AT-05-C05", len(linked) == len(active), f"{len(linked)} of {len(active)} active item(s) are linked to a CMDB "
         "record", [ref(r, "name") for r in active])


PRIO = re.compile(r"(?:\bP|priority\s*(?:=|is)?\s*)([1-4])\b", re.I)


def itsm_sla_definitions(b, ctx, rep):
    rows = [r for r in table(b, "contract_sla") if yes(r.get("active", "true"))
            and re.search(r"incident", str(r.get(f(ctx, "sla_collection"), "incident")), re.I)
            and not re.search(r"\bOLA\b|underpinning", str(r.get("type", "")), re.I)]
    if not rows:
        return
    have = defaultdict(set)
    for r in rows:
        text = f"{r.get('name', '')} {r.get('start_condition', '')} {r.get('condition', '')}"
        m = PRIO.search(text)
        kind = str(r.get(f(ctx, "sla_target")) or r.get("name", "")).lower()
        if m:
            have[m.group(1)] |= {k for k in ("response", "resolution") if k in kind}
    missing = [f"P{p} {k}" for p in "1234" for k in ("response", "resolution") if k not in have[p]]
    emit(rep, "ITSM-AT-06-C01", not missing, "Response and resolution SLAs defined for "
         + ("P1–P4" if not missing else f"some priorities; missing: {', '.join(missing)}")
         + ". Auto-attachment on incident creation needs confirming.", b.table_ids("contract_sla"), review=True)


def itsm_sla_attainment(b, ctx, rep):
    rows = table(b, "task_sla")
    if not rows:
        return
    mine = {str(i.get("number")) for i in ctx.scoped(b.table("incident"))}
    rows = [r for r in rows if not mine or str(r.get(f(ctx, "sla_task"))) in mine]
    p1 = [r for r in rows if str(r.get(f(ctx, "sla_priority"), "")).strip().startswith("1")]
    use = p1 or rows                                   # no P1 in the period: judge on overall attainment
    met = [r for r in use if not yes(r.get(f(ctx, "sla_breached"), "false"))]
    p = pct(len(met), len(use))
    emit(rep, "ITSM-AT-06-C02", p > 95, (f"P1 SLA attainment {p}% ({len(met)} of {len(use)}; target > 95%)" if p1 else
         f"No P1 incidents with SLA records in the period; overall attainment {p}% ({len(met)} of {len(use)}; target > 95%)")
         + ". Monthly reporting and breach root causes need checking.", b.table_ids("task_sla"), review=True)


def itsm_sla_breaches(b, ctx, rep):
    rows = reg(b, ctx, "sla_breach")
    if not rows:
        return
    uncategorised = [r for r in rows if not truthy(r.get("cause_category"))]
    causes = Counter(str(r["cause_category"]).strip() for r in rows if truthy(r.get("cause_category")))
    top = [c for c, _ in causes.most_common(3)]
    no_action = [c for c in top if not any(truthy(r.get("improvement_action")) for r in rows if str(r.get("cause_category")).strip() == c)]
    emit(rep, "ITSM-AT-06-C04", not uncategorised and not no_action,
         f"{len(rows) - len(uncategorised)} of {len(rows)} breaches have a root-cause category; top causes: {', '.join(top) or '—'}"
         + (f"; without a tracked action: {', '.join(no_action)}" if no_action else ""), b.table_ids("register:sla_breach"))


def _actions(b, ctx, rep, t, cid, target, label):
    rows = reg(b, ctx, t)
    if not rows:
        return
    due, on, late = _on_time(rows, ctx)
    unesc = [r for r in late if not yes(r.get("escalated"))]
    p = pct(len(on), len(due))
    emit(rep, cid, (not due or p > target) and not unesc,
         f"{len(on)} of {len(due)} {label} due were completed on time ({p}%; target > {target}%); {len(late)} overdue, "
         f"{len(unesc)} of them not escalated", b.table_ids("register:" + t))


def itsm_corrective(b, ctx, rep):
    _actions(b, ctx, rep, "corrective_action", "ITSM-AT-06-C05", 90, "corrective actions")


def itsm_migration(b, ctx, rep):
    rows = reg(b, ctx, "knowledge_migration")
    if not rows:
        return
    judged, ok = [], []
    for r in rows:
        start, done = r.get("_identified_date"), r.get("_migrated_date")
        if not start:
            continue
        if done:
            judged.append(r)
            if done <= start + timedelta(days=60):
                ok.append(r)
        elif ctx.as_of > start + timedelta(days=60):
            judged.append(r)                               # past the 60-day window and still not migrated
    if not judged:
        return
    p = pct(len(ok), len(judged))
    emit(rep, "ITSM-AT-07-C03", p == 100, f"{len(ok)} of {len(judged)} procedures were migrated to ServiceNow within 60 days of "
         f"being identified ({p}%); {len(rows) - len(judged)} still inside their window", b.table_ids("register:knowledge_migration"))


def _num(v) -> float | None:
    m = re.search(r"-?\d+(\.\d+)?", str(v or ""))
    return float(m.group()) if m else None


def itsm_improvements(b, ctx, rep):
    rows = reg(b, ctx, "improvement")
    if not rows:
        return
    ids = b.table_ids("register:improvement")
    full = [r for r in rows if truthy(r.get("owner")) and _num(r.get("score")) is not None
            and (r.get("_planned_date") or r.get("_due_date")) and truthy(r.get("status"))]
    emit(rep, "ITSM-AT-08-C01", len(full) == len(rows), f"{len(full)} of {len(rows)} improvement opportunities have owner, priority "
         "score, target date and status", ids)
    open_ = [r for r in rows if not r.get("_completed_date") and not re.search(r"done|closed|complete|cancel", str(r.get("status", "")), re.I)]
    top = sorted((r for r in open_ if _num(r.get("score")) is not None), key=lambda r: -_num(r["score"]))[:5]
    if top:
        _, qend = quarter_bounds(ctx.as_of)
        bad = [r for r in top if not ((r.get("_planned_date") or r.get("_due_date") or datetime.max) < qend or truthy(r.get("override_reason")))]
        emit(rep, "ITSM-AT-08-C02", not bad, f"{len(top) - len(bad)} of the top {len(top)} open opportunities by score are scheduled "
             "this quarter or have a documented override" + (f"; not: {', '.join(name(r) for r in bad)}" if bad else ""), ids)
    qs, qe = quarter_bounds(ctx.as_of, back=1)
    committed = [r for r in rows if (p := r.get("_planned_date") or r.get("_due_date")) and qs <= p < qe]
    if committed:
        on = [r for r in committed if r.get("_completed_date") and r["_completed_date"] <= (r.get("_planned_date") or r["_due_date"])]
        p = pct(len(on), len(committed))
        emit(rep, "ITSM-AT-08-C03", p > 80, f"{len(on)} of {len(committed)} opportunities committed for last quarter were delivered on "
             f"schedule ({p}%; target > 80%)", ids)
    closed = [r for r in rows if r.get("_completed_date") or re.search(r"done|closed|complete", str(r.get("status", "")), re.I)]
    if closed:
        measured = [r for r in closed if truthy(r.get("baseline_metric")) and truthy(r.get("result_metric"))]
        emit(rep, "ITSM-AT-08-C04", len(measured) == len(closed), f"{len(measured)} of {len(closed)} closed opportunities have a "
             "before/after metric pair", ids)


def itsm_risk_register(b, ctx, rep):
    rows = reg(b, ctx, "risk")
    if not rows:
        return
    full = [r for r in rows if all(truthy(r.get(k)) for k in ("likelihood", "impact", "mitigation", "owner"))]
    emit(rep, "ITSM-AT-09-C01", len(full) == len(rows), f"{len(full)} of {len(rows)} risks have likelihood, impact, mitigation and a "
         "named owner. Coverage of every identified gap needs checking.", b.table_ids("register:risk"), review=True)


def itsm_exceptions(b, ctx, rep):
    rows = reg(b, ctx, "exception")
    if not rows:
        return
    full = [r for r in rows if truthy(r.get("approved_by")) and r.get("_expiry_date") and truthy(r.get("compensating_control"))]
    emit(rep, "ITSM-AT-09-C05", len(full) == len(rows), f"{len(full)} of {len(rows)} policy exceptions have an approval, an expiry "
         "date and a compensating control", b.table_ids("register:exception"))


def _apps(v) -> list[str]:
    return [a.strip() for a in re.split(r"[,;|\n]", str(v or "")) if a.strip()]


def itsm_vendors(b, ctx, rep):
    rows = reg(b, ctx, "vendor", scoped=False)
    if not rows:
        return
    ids = b.table_ids("register:vendor")
    full = [r for r in rows if truthy(r.get("services")) and _apps(r.get("applications"))]
    emit(rep, "ITSM-AT-10-C01", len(full) == len(rows), f"{len(full)} of {len(rows)} vendors list the services they provide and the "
         "applications that depend on them. Links in the CMDB need checking.", ids, review=True)
    big = [r for r in rows if len(_apps(r.get("applications"))) > 5]
    if big:
        bad = [r for r in big if not (yes(r.get("concentration_assessed")) and truthy(r.get("mitigation")))]
        emit(rep, "ITSM-AT-10-C05", not bad, f"{len(big) - len(bad)} of {len(big)} vendors supporting more than 5 applications have a "
             "documented concentration risk and mitigation" + (f"; not: {', '.join(str(r.get('vendor')) for r in bad)}" if bad else ""), ids)


def itsm_contracts(b, ctx, rep):
    rows = reg(b, ctx, "contract", scoped=False)
    crit = [r for r in rows if yes(r.get("critical", "yes"))]
    if not crit:
        return
    ok = [r for r in crit if yes(r.get("sla")) and yes(r.get("penalty")) and (r.get("_review_date") or datetime.min) >= ctx.months_ago(12)
          and (r.get("_contract_end") or datetime.max) >= ctx.as_of]
    bad = [str(r.get("vendor") or name(r)) for r in crit if r not in ok]
    emit(rep, "ITSM-AT-10-C02", not bad, f"{len(ok)} of {len(crit)} critical vendor contracts are current, with SLA, penalty clause and a "
         "review in the last 12 months" + (f"; not: {', '.join(bad[:5])}" if bad else ""), b.table_ids("register:contract"))


# ------------------------------------------------------------------------------------------------ SRE
def sre_risk_mitigation(b, ctx, rep):
    rows = [r for r in reg(b, ctx, "risk") if re.search(r"critical|high", f"{r.get('rating', '')} {r.get('impact', '')} "
                                                       f"{r.get('severity', '')}", re.I)]
    if not rows:
        return
    full = [r for r in rows if truthy(r.get("mitigation")) and truthy(r.get("owner")) and (r.get("_target_date") or r.get("_due_date"))]
    emit(rep, "SRE-AT-02-C05", len(full) == len(rows), f"{len(full)} of {len(rows)} High/Critical risks have a mitigation plan, named "
         "owner and target date", b.table_ids("register:risk"))


def sre_postmortem_actions(b, ctx, rep):
    _actions(b, ctx, rep, "postmortem_action", "SRE-AT-09-C03", 80, "post-mortem action items")


def _quarter_key(r) -> tuple[int, int] | None:
    s = str(r.get("quarter", ""))
    m = re.search(r"(20\d\d)\D{0,3}Q([1-4])|Q([1-4])\D{0,3}(20\d\d)", s, re.I)
    if m:
        return (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
    d = r.get("_measured_date")
    return (d.year, (d.month - 1) // 3 + 1) if d else None


def sre_toil(b, ctx, rep):
    rows = [(k, _num(r.get("toil_pct"))) for r in reg(b, ctx, "toil") if (k := _quarter_key(r)) and _num(r.get("toil_pct")) is not None]
    if not rows:
        return
    series = [v for _, v in sorted(dict(rows).items())]
    (ly, lq), latest = max(dict(rows).items())
    ny, nq = ctx.as_of.year, (ctx.as_of.month - 1) // 3 + 1
    recent = (ny * 4 + nq) - (ly * 4 + lq) <= 2               # measured this quarter or one of the last two
    emit(rep, "SRE-AT-10-C02", recent and latest < 50, f"Latest toil measurement {latest:g}% ({ly} Q{lq}; target < 50%)"
         + ("" if recent else "; not measured in the last two quarters"), b.table_ids("register:toil"))
    if len(series) >= 3:
        falling = all(b2 <= a2 for a2, b2 in zip(series, series[1:]))
        yoy = series[-1] <= series[-5] - 10 if len(series) >= 5 else None
        emit(rep, "SRE-AT-10-C05", falling and yoy is not False, f"Toil over {len(series)} quarters: {', '.join(f'{v:g}%' for v in series)}; "
             + ("decreasing every quarter" if falling else "not decreasing every quarter")
             + ("" if yoy is None else f"; year-over-year {'down more than' if yoy else 'not down'} 10 points"),
             b.table_ids("register:toil"), review=yoy is None)


def sre_automation(b, ctx, rep):
    rows = reg(b, ctx, "automation")
    if not rows:
        return
    ids = b.table_ids("register:automation")
    live = [r for r in rows if not re.search(r"retired|decommission|cancel", str(r.get("status", "")), re.I)]
    if live:
        ok = [r for r in live if truthy(r.get("owner")) and (r.get("_last_activity") or datetime.min) >= ctx.days_ago(30)]
        emit(rep, "SRE-AT-14-C04", len(ok) == len(live), f"{len(ok)} of {len(live)} active automations have a named owner with "
             "activity in the last 30 days", ids)
    done = [r for r in rows if r.get("_deployed_date") or re.search(r"deployed|live|done", str(r.get("status", "")), re.I)]
    if done:
        measured = [r for r in done if _num(r.get("before_hours")) is not None and _num(r.get("after_hours")) is not None]
        saved = sum(_num(r["before_hours"]) - _num(r["after_hours"]) for r in measured)
        emit(rep, "SRE-AT-10-C04", len(measured) == len(done), f"{len(measured)} of {len(done)} delivered automations have a before/after "
             f"measurement ({saved:g} hours/week saved in total). Quarterly reporting to REO needs confirming.", ids, review=True)


def sre_access_review(b, ctx, rep):
    rows = sorted(reg(b, ctx, "access_review"), key=lambda r: r.get("_review_date") or datetime.min)
    if not rows:
        return
    last = rows[-1]
    recent = (last.get("_review_date") or datetime.min) >= ctx.days_ago(92)
    lp = yes(last.get("least_privilege"))
    shared = _num(last.get("shared_accounts"))
    no_shared = (shared == 0) if shared is not None else not yes(last.get("shared_accounts"))
    emit(rep, "SRE-AT-11-C05", recent and lp and no_shared, f"Latest production access review {last.get('review_date') or 'undated'}"
         f" ({'within' if recent else 'older than'} a quarter); least privilege {'confirmed' if lp else 'not confirmed'}; shared accounts: "
         f"{last.get('shared_accounts') or '—'}", b.table_ids("register:access_review"))


def sre_deployments(b, ctx, rep):
    rows = [r for r in reg(b, ctx, "deployment") if r.get("_deployed_at") and r["_deployed_at"] >= ctx.days_ago(90)
            and is_prod(r)]
    if not rows:
        return
    leads = [(r["_deployed_at"] - r["_commit_at"]).total_seconds() / 3600 for r in rows if r.get("_commit_at")]
    per_month = len(rows) / 3
    lead = f"median lead time {statistics.median(leads):.1f} h" if leads else "lead time not measurable (no commit times)"
    emit(rep, "SRE-AT-12-C04", bool(leads), f"{len(rows)} production deployments in 90 days ({per_month:.1f} per month); {lead}. "
         "Baseline and improvement targets need confirming.", b.table_ids("register:deployment"), review=True)


RULES = [cmdb_service_model, cmdb_offerings, cmdb_discovery_coverage, cmdb_ire, cmdb_duplicates, cmdb_enrichment,
         cmdb_discovery_health, cmdb_lifecycle_governance, cmdb_hosting_linked, cmdb_asset_link, cmdb_sw_normalized,
         cmdb_service_population, cmdb_k8s_namespaces, cmdb_cloud_stale, cmdb_impacted_services, cmdb_conflict_check,
         cmdb_eol_dates, cmdb_tier_approved, cmdb_attestation, cmdb_remediation, cmdb_deferrals, cmdb_debt,
         cmdb_audit_findings, cmdb_exceptions,
         itsm_rca_cycle, itsm_roster, itsm_catalog, itsm_sla_definitions, itsm_sla_attainment, itsm_sla_breaches,
         itsm_corrective, itsm_migration, itsm_improvements, itsm_risk_register, itsm_exceptions, itsm_vendors, itsm_contracts,
         sre_risk_mitigation, sre_postmortem_actions, sre_toil, sre_automation, sre_access_review, sre_deployments]


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    for rule in RULES:
        rule(b, ctx, rep)
    rep.status = f"ran: {len(rep.results)} criteria settled from ServiceNow tables and registers"
    return rep
