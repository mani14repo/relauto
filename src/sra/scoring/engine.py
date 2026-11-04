"""Scoring engine: criterion results -> template/domain scores, Readiness Index, maturity, rating.

Pure functions over the catalog, the scoring matrix and the crosswalk. See config/scoring_matrix.yaml.

Rules (rubric V1 Draft + proposed matrix):
  * Insufficient Evidence scores as a Gap.
  * Not Applicable is excluded only when approved; unapproved N/A scores as Insufficient Evidence.
  * Unevaluated criteria score as Insufficient Evidence (nothing is silently dropped).
  * Rubric P1/P2 result = worst of its direct evaluation and every mapped template criterion
    in the assessment; with neither, it is Insufficient Evidence (= Gap).
  * Rating from P1/P2 gap counts; maturity capped by rating.
"""
from __future__ import annotations

from ..catalog.models import Domain, Result, Rubric, Template
from ..crosswalk import rubric_criteria_map
from ..models import Assessment, CriterionResult, RubricOutcome, ScoreCard, TemplateScore

GAPLIKE = {Result.GAP, Result.INSUFFICIENT}
# Precedence when combining several results into one (worst first).
_PRECEDENCE = [Result.GAP, Result.INSUFFICIENT, Result.MET, Result.NA]


def effective_result(r: CriterionResult | None) -> Result:
    """The result used for scoring."""
    if r is None or r.result is None:
        return Result.INSUFFICIENT
    if r.result == Result.NA and not r.na_approved:
        return Result.INSUFFICIENT
    return r.result


def worst(results: list[Result]) -> Result:
    for r in _PRECEDENCE:
        if r in results:
            return r
    return Result.INSUFFICIENT


def score_template(t: Template, a: Assessment, matrix: dict) -> TemplateScore:
    pts = matrix["result_points"]
    weights = matrix["risk_weights"]
    num = den = 0.0
    counts = {Result.MET: 0, Result.GAP: 0, Result.INSUFFICIENT: 0, Result.NA: 0}
    unevaluated = 0
    for c in t.criteria:
        r = a.results.get(c.id)
        if r is None or r.result is None:
            unevaluated += 1
        eff = effective_result(r)
        counts[eff] += 1
        if eff == Result.NA:
            continue
        risk = (r.risk if r and r.risk else c.default_risk).value
        w = float(weights[risk])
        num += w * float(pts[eff.value])
        den += w
    prio = t.priority or matrix["default_template_priority"]
    return TemplateScore(
        template_id=t.id, domain=t.domain,
        score=round(100 * num / den, 1) if den else None,
        weight=float(matrix["template_priority_weights"][prio]),
        met=counts[Result.MET], gaps=counts[Result.GAP], insufficient=counts[Result.INSUFFICIENT],
        na=counts[Result.NA], unevaluated=unevaluated,
    )


def domain_scores(scores: list[TemplateScore]) -> dict[Domain, float]:
    out: dict[Domain, float] = {}
    for d in Domain:
        items = [s for s in scores if s.domain == d and s.score is not None]
        tw = sum(s.weight for s in items)
        if tw:
            out[d] = round(sum(s.score * s.weight for s in items) / tw, 1)
    return out


def readiness_index(domains: dict[Domain, float], matrix: dict) -> float | None:
    w = {Domain(k): float(v) for k, v in matrix["domain_weights"].items()}
    tw = sum(w[d] for d in domains)
    if not tw:
        return None
    return round(sum(domains[d] * w[d] for d in domains) / tw, 1)


def maturity(index: float | None, matrix: dict) -> tuple[int | None, str | None]:
    if index is None:
        return None, None
    level = None
    for m in sorted(matrix["maturity_levels"], key=lambda m: m["min"]):
        if index >= m["min"]:
            level = m
    return level["level"], level["name"]


def rate(p1: int, p2: int, matrix: dict) -> dict:
    for band in matrix["ratings"]:
        if p1 < band.get("p1_min", 0) or p1 > band.get("p1_max", 10**9):
            continue
        if p2 < band.get("p2_min", 0) or p2 > band.get("p2_max", 10**9):
            continue
        return band
    raise ValueError(f"no rating band for p1={p1} p2={p2}")


def rubric_outcomes(a: Assessment, rubric: Rubric) -> list[RubricOutcome]:
    cmap = rubric_criteria_map()
    out = []
    for rc in rubric.criteria:
        contributing, results = [], []
        direct = a.rubric_direct.get(rc.id)
        if direct is not None and direct.result is not None:
            results.append(effective_result(direct))
            contributing.append(rc.id)
        for cid in cmap.get(rc.id, []):
            r = a.results.get(cid)
            if r is not None and r.result is not None:
                results.append(effective_result(r))
                contributing.append(cid)
        out.append(RubricOutcome(rubric_id=rc.id, priority=rc.priority, domain=rc.domain, name=rc.name,
                                 result=worst(results) if results else Result.INSUFFICIENT,
                                 contributing=contributing))
    return out


def score_assessment(a: Assessment, templates: dict[str, Template], rubric: Rubric, matrix: dict) -> ScoreCard:
    selected = [templates[tid] for tid in a.templates]
    tscores = [score_template(t, a, matrix) for t in selected]
    domains = domain_scores(tscores)
    index = readiness_index(domains, matrix)
    level, name = maturity(index, matrix)

    outcomes = rubric_outcomes(a, rubric)
    p1 = [o.rubric_id for o in outcomes if o.priority == "P1" and o.result in GAPLIKE]
    p2 = [o.rubric_id for o in outcomes if o.priority == "P2" and o.result in GAPLIKE]
    band = rate(len(p1), len(p2), matrix)

    capped = False
    cap = matrix.get("maturity_caps", {}).get(band["rating"])
    if level is not None and cap is not None and level > cap:
        level = cap
        name = next(m["name"] for m in matrix["maturity_levels"] if m["level"] == cap)
        capped = True

    all_ids = [c.id for t in selected for c in t.criteria]
    eff = {cid: effective_result(a.results.get(cid)) for cid in all_ids}
    with_evidence = [cid for cid in all_ids if a.results.get(cid) and a.results[cid].evidence_refs]
    scorable = [cid for cid in all_ids if eff[cid] != Result.NA]
    coverage = round(100 * len([c for c in with_evidence if c in scorable]) / len(scorable), 1) if scorable else 100.0

    return ScoreCard(
        matrix_version=matrix["version"],
        template_scores=tscores,
        domain_scores=domains,
        index=index,
        maturity_level=level,
        maturity_name=name,
        maturity_capped=capped,
        rating=band["rating"],
        rating_outcome=band["outcome"],
        remediation_days=band.get("remediation_days"),
        rubric=outcomes,
        p1_gaps=p1,
        p2_gaps=p2,
        met=[cid for cid in all_ids if eff[cid] == Result.MET] + [o.rubric_id for o in outcomes if o.result == Result.MET],
        not_applicable=[cid for cid in all_ids if eff[cid] == Result.NA] + [o.rubric_id for o in outcomes if o.result == Result.NA],
        insufficient=[cid for cid in all_ids if eff[cid] == Result.INSUFFICIENT] + [o.rubric_id for o in outcomes if o.result == Result.INSUFFICIENT],
        evidence_coverage_pct=coverage,
    )
