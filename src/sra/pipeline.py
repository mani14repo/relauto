"""Assessment pipeline: select -> detect -> merge -> (LLM evaluate) -> recommend -> score -> gate.

The same functions back the CLI, the API and the Kafka workers. Nothing here talks to a database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .catalog import load_rubric, load_templates
from .catalog.models import Method, Result, Risk
from .config import load_yaml
from .crosswalk import rubric_criteria_map, rubric_ids_for
from .detectors import ALL as DETECTORS
from .detectors.base import Context, DetectorReport
from .evidence.bundle import EvidenceBundle
from .models import Assessment, CriterionResult, Finding, FindingType, ScoreCard, Source
from .recommend.engine import attach
from .scoring import load_matrix, score_assessment
from .selection.profile import ApplicationProfile
from .selection.selector import Candidate, select_templates

SEVERITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def _merge(existing: CriterionResult | None, new: CriterionResult) -> CriterionResult:
    """Prefer evaluated over Insufficient; between two evaluations keep the worse (Gap wins)."""
    if existing is None or existing.result is None:
        return new
    if existing.source == Source.HUMAN:          # never overwrite an assessor's decision
        return existing
    if existing.result == Result.INSUFFICIENT and new.result != Result.INSUFFICIENT:
        return new
    if new.result == Result.GAP and existing.result != Result.GAP:
        new.evidence_refs = list(dict.fromkeys(existing.evidence_refs + new.evidence_refs))
        new.rationale = f"{new.rationale}\n(also: {existing.rationale})"
        return new
    if new.result == existing.result and new is not existing:
        existing.evidence_refs = list(dict.fromkeys(existing.evidence_refs + new.evidence_refs))
        if new.rationale not in existing.rationale:
            existing.rationale = f"{existing.rationale}\n{new.rationale}"
        existing.needs_human_review = existing.needs_human_review or new.needs_human_review
    return existing


@dataclass
class GateResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def context_for(profile: ApplicationProfile, as_of: datetime) -> Context:
    return Context(app_name=profile.name, aliases=profile.aliases, as_of=as_of, cfg=load_yaml("detectors"), tier=profile.tier)


def propose_templates(profile: ApplicationProfile, bundle: EvidenceBundle | None = None,
                      as_of: datetime | None = None) -> tuple[ApplicationProfile, list[Candidate]]:
    if bundle is not None:
        profile = profile.enrich_from_evidence(bundle, as_of or datetime.now())
    return profile, select_templates(profile, load_templates())


def run_detectors(a: Assessment, bundle: EvidenceBundle, ctx: Context) -> list[DetectorReport]:
    templates = load_templates()
    selected_ids = {c.id for tid in a.templates for c in templates[tid].criteria}
    reports = []
    for name, fn in DETECTORS.items():
        rep = fn(bundle, ctx)
        reports.append(rep)
        a.detector_runs[name] = rep.status
        for rid, r in rep.rubric.items():
            a.rubric_direct[rid] = _merge(a.rubric_direct.get(rid), r)
        for cid, r in rep.results.items():
            if cid in selected_ids:
                a.results[cid] = _merge(a.results.get(cid), r)
        a.findings.extend(rep.findings)
    if bundle.errors:
        a.detector_runs["ingest_errors"] = "; ".join(bundle.errors)[:500]

    # P2-04 'Insufficient' from rubric_rules is superseded by config_checks when it ran
    # (handled by _merge). Now propagate rubric results to mapped template criteria.
    cmap = rubric_criteria_map()
    by_id = {c.id: c for tid in a.templates for c in templates[tid].criteria}
    no_inherit = {cid for c in load_yaml("crosswalk").get("conflicts", []) for cid in c.get("criteria", [])}
    for rid, direct in a.rubric_direct.items():
        for cid in cmap.get(rid, []):
            if cid not in by_id or cid in no_inherit or (a.results.get(cid) and a.results[cid].result is not None):
                continue
            c = by_id[cid]
            pure_rule = c.methods == [Method.DETERMINISTIC]
            a.results[cid] = CriterionResult(
                criterion_id=cid, result=direct.result, rationale=f"From rubric rule {rid}: {direct.rationale}",
                evidence_refs=list(direct.evidence_refs), source=Source.RULE,
                needs_human_review=direct.needs_human_review or not pure_rule,
                confidence=(direct.confidence or 1.0) * (1.0 if pure_rule else 0.7))

    # Link rubric findings to the selected template criteria they affect
    for f in a.findings:
        for rid in f.rubric_ids:
            for cid in cmap.get(rid, []):
                if cid in by_id and cid not in f.criterion_ids:
                    f.criterion_ids.append(cid)
        for cid in list(f.criterion_ids):
            for rid in rubric_ids_for(cid):
                if rid not in f.rubric_ids and f.type == FindingType.GAP:
                    f.rubric_ids.append(rid)
    return reports


def ensure_gap_findings(a: Assessment) -> None:
    """Every Gap / Insufficient criterion gets at least one finding (so it gets a recommendation)."""
    templates = load_templates()
    covered = {cid for f in a.findings for cid in f.criterion_ids}
    for tid in a.templates:
        for c in templates[tid].criteria:
            r = a.results.get(c.id)
            if r and r.result in (Result.GAP, Result.INSUFFICIENT) and c.id not in covered:
                ie = r.result == Result.INSUFFICIENT
                a.findings.append(Finding(
                    type=FindingType.GAP, severity=r.risk or c.default_risk,
                    title=f"{c.id} {c.name}: " + ("insufficient evidence" if ie else "acceptance criteria not met"),
                    detail=r.rationale, criterion_ids=[c.id], rubric_ids=rubric_ids_for(c.id), evidence_refs=r.evidence_refs,
                    detector=r.source.value if r.source else "assessment",
                    ))
                if ie:
                    a.findings[-1].detail = (a.findings[-1].detail + "\nProvide: " + c.required_evidence).strip()
    # Rubric criteria with no direct and no mapped evaluation
    rubric_covered = {rid for f in a.findings for rid in f.rubric_ids}
    for rc in load_rubric().criteria:
        if rc.id not in a.rubric_direct and rc.id not in rubric_covered:
            mapped = [a.results.get(cid) for cid in rubric_criteria_map().get(rc.id, [])]
            if not any(m and m.result in (Result.MET, Result.NA) for m in mapped):
                a.findings.append(Finding(type=FindingType.GAP, severity=Risk.CRITICAL if rc.priority == "P1" else Risk.HIGH,
                                          title=f"{rc.id}: insufficient evidence — {rc.name}", detail=rc.condition,
                                          rubric_ids=[rc.id], detector="assessment"))


def completeness_gate(a: Assessment, bundle: EvidenceBundle | None, final: bool = False) -> GateResult:
    templates = load_templates()
    g = GateResult(ok=True)
    for tid in a.templates:
        for c in templates[tid].criteria:
            r = a.results.get(c.id)
            if r is None or r.result is None:
                g.blockers.append(f"{c.id} not evaluated")
            elif r.result == Result.NA and not r.na_approved:
                (g.blockers if final else g.warnings).append(f"{c.id} Not Applicable awaiting approval")
            elif r.needs_human_review and not r.reviewer:
                (g.blockers if final else g.warnings).append(f"{c.id} needs human review")
    with_rec = {cid for f in a.findings if f.recommendation and f.recommendation.action for cid in f.criterion_ids}
    for tid in a.templates:
        for c in templates[tid].criteria:
            r = a.results.get(c.id)
            if r and r.result in (Result.GAP, Result.INSUFFICIENT) and c.id not in with_rec:
                g.blockers.append(f"{c.id} is {r.result.value} but has no finding with a recommendation")
    if bundle is not None:
        min_rank = SEVERITY_RANK[load_yaml("detectors")["vulnerabilities"]["report_min_severity"]]
        reported = {ref.split(":", 1)[1] for f in a.findings if f.type == FindingType.VULNERABILITY for ref in f.evidence_refs if ":" in ref}
        for sf in bundle.scan_findings:
            if SEVERITY_RANK[sf.severity] <= min_rank and not sf.rule_id.startswith("secret:") and sf.rule_id not in reported:
                g.blockers.append(f"scanner finding {sf.rule_id} ({sf.severity}) not reported")
    for name in DETECTORS:
        if name not in a.detector_runs:
            g.blockers.append(f"detector {name} did not run")
    if final:
        missing = [s.role for s in a.signoffs if not s.name]
        if missing or not a.signoffs:
            g.blockers.append("sign-offs incomplete: " + (", ".join(missing) or "none recorded"))
    g.ok = not g.blockers
    return g


def run_assessment(a: Assessment, bundle: EvidenceBundle, profile: ApplicationProfile, as_of: datetime,
                   evaluator=None) -> tuple[ScoreCard, GateResult]:
    """Full automated pass. `evaluator` (llm.evaluate.Evaluator) handles criteria rules could not settle."""
    templates, rubric, matrix = load_templates(), load_rubric(), load_matrix()
    a.profile = profile.model_dump(mode="json")
    a.matrix_version = matrix["version"]
    ctx = context_for(profile, as_of)
    run_detectors(a, bundle, ctx)
    if evaluator is not None:
        evaluator.evaluate_pending(a, bundle)
    ensure_gap_findings(a)
    sc = score_assessment(a, templates, rubric, matrix)
    attach(a.findings, as_of.date(), matrix, sc.remediation_days)
    sc.conflicts = threshold_conflicts(a)
    return sc, completeness_gate(a, bundle)


def rescore(a: Assessment) -> ScoreCard:
    sc = score_assessment(a, load_templates(), load_rubric(), load_matrix())
    sc.conflicts = threshold_conflicts(a)
    return sc


def threshold_conflicts(a: Assessment) -> list[str]:
    out = []
    for c in load_yaml("crosswalk").get("conflicts", []):
        if set(c["templates"]) & set(a.templates):
            out.append(c["text"])
    return out
