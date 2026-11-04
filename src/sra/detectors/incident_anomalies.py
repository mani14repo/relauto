"""Anomaly detection over incident, alert and change history (deterministic, explainable statistics).

Detects: weekly volume spikes (robust z-score over median/MAD), recurring patterns without Problems,
false-positive / noisy alerting, MTTR outliers, change-induced incidents, poor CI linkage, the share of
monitoring- vs human-opened incidents, and close notes that point outside ServiceNow (OneNote, email).
Also sets results for the ITSM/CMDB template criteria these measurements answer directly.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..evidence.redact import Resolution, classify_resolution
from ..models import FindingType
from .base import Context, DetectorReport, finding, ref, result, truthy

NAME = "incident_anomalies"


def _mad_z(values: list[float], x: float) -> float:
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) or 1.0
    return 0.6745 * (x - med) / mad


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    if not b.has_table("incident"):
        rep.status = "skipped: no incident export"
        return rep
    a = ctx.cfg["anomalies"]
    window = ctx.cfg["rubric_rules"]["incident_window_days"]
    all_inc = [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at")]
    inc = [r for r in all_inc if r["_opened_at"] >= ctx.days_ago(window)]
    refs = b.table_ids("incident")
    if not all_inc:
        rep.status = "ran: no incidents for application"
        return rep

    # ---- weekly volume spikes
    weeks = Counter()
    start = ctx.as_of - timedelta(weeks=a["spike_window_weeks"])
    for r in all_inc:
        if r["_opened_at"] >= start:
            weeks[(ctx.as_of - r["_opened_at"]).days // 7] += 1
    series = [weeks.get(w, 0) for w in range(a["spike_window_weeks"])]
    if len(series) >= 4 and max(series) >= a["min_weekly_incidents"]:
        for w, n in enumerate(series):
            z = _mad_z(series, n)
            if z >= a["spike_mad_threshold"] and n >= a["min_weekly_incidents"]:
                rep.findings.append(finding(
                    FindingType.ANOMALY, Risk.HIGH, f"Incident volume spike: {n} incidents in week -{w} (robust z={z:.1f})",
                    f"Weekly counts (newest first): {series}", criteria=["ITSM-AT-02-C01", "SRE-AT-09-C01"],
                    refs=refs, detector=f"{NAME}:volume_spike",
                    action="Investigate the spike week for a common cause (change, dependency, capacity) and open a Problem if recurring"))

    # ---- resolution quality / alert noise
    labels = Counter(classify_resolution(r.get("close_notes", "")) for r in inc)
    if inc:
        fp = labels[Resolution.FALSE_POSITIVE] / len(inc)
        if fp > a["false_positive_share_max"]:
            rep.findings.append(finding(
                FindingType.ANOMALY, Risk.MEDIUM, f"Alert noise: {fp:.0%} of incidents closed as false positive",
                criteria=["SRE-AT-03-C03"], rubric=["P1-03"], refs=refs, detector=f"{NAME}:alert_noise",
                action="Tune or retire noisy alerts (ITSM-PB-11 Alert Noise Management)"))
        pointer = labels[Resolution.POINTER]
        if pointer:
            rep.findings.append(finding(
                FindingType.GAP, Risk.HIGH, f"{pointer} incident resolutions only point to instructions outside ServiceNow (e.g. OneNote)",
                criteria=["ITSM-AT-07-C03", "SRE-AT-07-C01"], rubric=["P1-04"], refs=refs, detector=f"{NAME}:pointer_resolutions",
                action="Migrate the referenced OneNote/email instructions into ServiceNow KB runbooks (ITSM-PB-08)"))
        thin = labels[Resolution.THIN] + labels[Resolution.EMPTY] + labels[Resolution.AI_FILLER]
        if thin / len(inc) > 0.5:
            rep.findings.append(finding(
                FindingType.OBSERVATION, Risk.MEDIUM, f"{thin}/{len(inc)} close notes are empty, generic or AI filler",
                criteria=["ITSM-AT-02-C01"], refs=refs, detector=f"{NAME}:close_note_quality",
                action="Require resolution steps in close notes; add a close-note quality check to incident closure"))

    # ---- MTTR outliers and measurement integrity (ITSM-AT-02-C02)
    durations = [((r["_resolved_at"] - r["_opened_at"]).total_seconds() / 3600, r) for r in inc
                 if r.get("_resolved_at") and r["_resolved_at"] >= r["_opened_at"]]
    negative = [r for r in inc if r.get("_resolved_at") and r["_resolved_at"] < r["_opened_at"]]
    if durations:
        med = statistics.median(d for d, _ in durations) or 0.01
        outliers = [(d, r) for d, r in durations if d > a["mttr_outlier_factor"] * med and d > 4]
        if outliers:
            top = sorted(outliers, key=lambda x: -x[0])[:5]
            rep.findings.append(finding(
                FindingType.ANOMALY, Risk.MEDIUM,
                f"{len(outliers)} incidents with restore time > {a['mttr_outlier_factor']}× median ({med:.1f}h)",
                "Longest: " + ", ".join(f"{r.get('number')} {d:.0f}h" for d, r in top),
                criteria=["ITSM-AT-02-C02"], refs=[ref(r) for _, r in top], detector=f"{NAME}:mttr_outliers",
                action="Review long-running incidents for escalation delays and missing runbook coverage"))
        rep.results["ITSM-AT-02-C02"] = result(
            "ITSM-AT-02-C02", Result.GAP if negative else Result.MET,
            f"{len(durations)} incidents with resolved_at; median restore {med:.1f}h"
            + (f"; {len(negative)} with resolved_at before opened_at" if negative else ""), refs, review=True)

    # ---- monitoring vs human (ITSM-AT-02-C04)
    if inc:
        mon = sum(1 for r in inc if r.get("_from_monitoring"))
        share = mon / len(inc)
        rep.results["ITSM-AT-02-C04"] = result(
            "ITSM-AT-02-C04", Result.MET,
            f"{mon}/{len(inc)} ({share:.0%}) incidents in {window}d were opened by monitoring; caller-based segmentation available",
            refs, review=True, confidence=0.7)

    # ---- classification completeness (ITSM-AT-02-C01)
    if inc:
        unclassified = [r for r in inc if not (truthy(r.get("category")) and truthy(r.get("subcategory")))]
        pct = 100 * (1 - len(unclassified) / len(inc))
        rep.results["ITSM-AT-02-C01"] = result(
            "ITSM-AT-02-C01", Result.MET if pct >= 95 else Result.GAP,
            f"Category+subcategory populated on {pct:.1f}% of {len(inc)} incidents (target 95%)", refs)

    # ---- CI linkage (CMDB-AT-01-C04)
    ci_field_present = any("cmdb_ci_business_app" in r or "cmdb_ci" in r for r in inc)
    if inc and ci_field_present:
        linked = sum(1 for r in inc if truthy(r.get("cmdb_ci_business_app")) or truthy(r.get("cmdb_ci")))
        pct = 100 * linked / len(inc)
        res = Result.MET if pct >= a["ci_linkage_min_pct"] else Result.GAP
        rep.results["CMDB-AT-01-C04"] = result("CMDB-AT-01-C04", res,
                                               f"{pct:.1f}% of {len(inc)} incidents linked to a CI (target {a['ci_linkage_min_pct']}%)", refs)
        if res == Result.GAP:
            rep.findings.append(finding(
                FindingType.GAP, Risk.CRITICAL, f"Only {pct:.1f}% of incidents are linked to a CMDB CI",
                criteria=["CMDB-AT-01-C04"], refs=refs, detector=f"{NAME}:ci_linkage",
                action="Make cmdb_ci_business_app mandatory on incident and backfill open incidents (CMDB-PB-06)"))

    # ---- recurrence without problems (ITSM-AT-03-C01) — complements rubric P2-02
    groups = defaultdict(list)
    for r in inc:
        key = (r.get("short_description") or "")[:60].lower().strip()
        if key:
            groups[key].append(r)
    recurring = {k: v for k, v in groups.items() if len(v) >= ctx.cfg["rubric_rules"]["recurrence_min_incidents"]}
    if recurring:
        unlinked = {k: v for k, v in recurring.items() if not any(truthy(r.get("problem_id")) for r in v)}
        linked_pct = 100 * (1 - len(unlinked) / len(recurring))
        rep.results["ITSM-AT-03-C01"] = result(
            "ITSM-AT-03-C01", Result.MET if not unlinked else Result.GAP,
            f"{len(recurring)} recurring patterns (≥3 in {window}d); {linked_pct:.0f}% linked to a Problem", refs)
        for k, v in sorted(unlinked.items(), key=lambda kv: -len(kv[1]))[:5]:
            rep.findings.append(finding(
                FindingType.ANOMALY, Risk.HIGH, f"Recurring incident ×{len(v)} with no Problem record: '{k}'",
                criteria=["ITSM-AT-03-C01"], rubric=["P2-02"], refs=[ref(r) for r in v[:5]],
                detector=f"{NAME}:recurrence", action="Open a Problem record, link the incidents and perform RCA (ITSM-PB-03)"))

    # ---- change-induced incidents (CMDB-AT-08-C02)
    changes = [c for c in b.table("change_request") if c.get("_end_date") or c.get("_start_date")]
    if changes and inc:
        h = timedelta(hours=a["change_induced_window_hours"])
        induced = []
        for r in inc:
            for c in changes:
                end = c.get("_end_date") or c.get("_start_date")
                if end <= r["_opened_at"] <= end + h and (ctx.matches(c) or not any(c.get(f) for f in ("cmdb_ci", "business_service"))):
                    induced.append((r, c))
                    break
        if induced:
            untraced = [(r, c) for r, c in induced if not truthy(r.get("caused_by"))]
            rep.findings.append(finding(
                FindingType.ANOMALY, Risk.HIGH,
                f"{len(induced)} incidents opened within {a['change_induced_window_hours']}h after a change"
                f" ({len(untraced)} without caused_by link)",
                ", ".join(f"{r.get('number')}←{c.get('number')}" for r, c in induced[:8]),
                criteria=["CMDB-AT-08-C02", "ITSM-AT-01-C04"], refs=[ref(r) for r, _ in induced[:8]] + [ref(c) for _, c in induced[:8]],
                detector=f"{NAME}:change_induced",
                action="Link change-caused incidents via caused_by and review the change's test/backout plan (CMDB-PB-07)"))
            rep.results["CMDB-AT-08-C02"] = result(
                "CMDB-AT-08-C02", Result.GAP if untraced else Result.MET,
                f"{len(induced)} change-adjacent incidents; {len(induced) - len(untraced)} traced via caused_by", refs, review=True)
    return rep
