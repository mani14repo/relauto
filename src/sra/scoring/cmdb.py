"""CMDB / CSDM V2 readiness: tier thresholds (TH-xx), maturity levels, the CMDB Readiness Index and Gates A-D.

Definitions come from CMDB-STD-00 and CMDB-AT-13 (V2.0 Draft), configured in config/scoring_matrix.yaml
under `cmdb_v2`. This sits beside the Reliability Readiness Index; it never changes the rubric rating.
"""
from __future__ import annotations

from ..catalog.models import Result
from ..config import load_yaml
from ..models import Assessment, CmdbDimension, CmdbGate, CmdbReadiness, RubricOutcome


def _cfg(matrix: dict | None = None) -> dict:
    return (matrix or load_yaml("scoring_matrix")).get("cmdb_v2", {})


def threshold(th_id: str, tier: int | None, matrix: dict | None = None) -> float | None:
    """TH-03, tier 2 -> 85. Unknown tier uses Tier 2 (the Standard tier)."""
    tiers = _cfg(matrix)["thresholds"][th_id]["tiers"]
    return tiers.get(tier or 2, tiers.get(2))


def level_from_pct(pct: float, target: float | None, matrix: dict | None = None) -> int:
    """Standard V2 percentage bands: L0 < 25, L1 25-49, L2 50-79, L3 80 to target, L4 at/above target."""
    b = _cfg(matrix).get("level_bands", [25, 50, 80])
    if target is not None and pct >= target:
        return 4
    return 3 if pct >= b[2] else 2 if pct >= b[1] else 1 if pct >= b[0] else 0


def _level(a: Assessment, cid: str) -> int | None | str:
    """Criterion level for the CRI: int, None (not assessed) or 'na' (excluded)."""
    r = a.results.get(cid)
    if r is None or r.result is None:
        return None
    if r.result == Result.NA and r.na_approved:
        return "na"
    if r.result == Result.INSUFFICIENT:
        return 0
    return r.maturity_level


def cmdb_readiness(a: Assessment, rubric: list[RubricOutcome], matrix: dict | None = None) -> CmdbReadiness | None:
    """None unless a V2 CMDB template is part of the assessment."""
    cfg = _cfg(matrix)
    if not cfg or not any(t.startswith("CMDB-") for t in a.templates):
        return None
    selected = _selected_ids(a)
    dims, total_w, score = [], 0.0, 0.0
    for d in cfg["cri_dimensions"]:
        # Every CRI criterion counts toward completeness; ones in templates not selected are reported as such,
        # so a CRI built from part of the model is labelled partial rather than presented as the full index.
        levels = [_level(a, c) if c in selected else None for c in d["criteria"]]
        known = [lv for lv in levels if isinstance(lv, int)]
        dim_score = round(100 * sum(known) / (4 * len(known)), 1) if known else None
        dims.append(CmdbDimension(name=d["name"], weight=d["weight"], score=dim_score, assessed=len(known),
                                  total=sum(1 for lv in levels if lv != "na"),
                                  missing=[c if c in selected else f"{c} (template not selected)"
                                           for c, lv in zip(d["criteria"], levels) if lv is None]))
        if dim_score is not None:
            total_w += d["weight"]
            score += d["weight"] * dim_score
    cri = round(score / total_w, 1) if total_w else None
    level = next((b for b in sorted(cfg["cri_levels"], key=lambda b: -b["min"]) if cri is not None and cri >= b["min"]), None)
    assessed = sum(d.assessed for d in dims)
    needed = sum(d.total for d in dims)
    return CmdbReadiness(
        cri=cri, complete=needed > 0 and assessed == needed, assessed=assessed, total=needed,
        level=level["level"] if level else None, level_name=level["name"] if level else None,
        dimensions=dims, gates=_gates(a, rubric, cfg),
        thresholds_status="V2.0 Draft — thresholds pending OD-2/OD-3 approval")


def _selected_ids(a: Assessment) -> set[str]:
    from ..catalog import load_templates
    templates = load_templates()
    return {c.id for t in a.templates if t in templates for c in templates[t].criteria}


def _gates(a: Assessment, rubric: list[RubricOutcome], cfg: dict) -> list[CmdbGate]:
    rubric_by_id = {o.rubric_id: o.result for o in rubric}
    selected = _selected_ids(a)
    out: dict[str, CmdbGate] = {}
    for g in cfg.get("gates", []):
        failed, pending = [], []
        for cid in g["criteria"]:
            if cid[:2] in ("P1", "P2"):
                res = rubric_by_id.get(cid)
            elif cid not in selected:
                pending.append(f"{cid} (template not selected)")
                continue
            else:
                r = a.results.get(cid)
                res = r.result if r else None
            if res is None:
                pending.append(cid)
            elif res != Result.MET:
                failed.append(f"{cid}: {res.value}")
        for req in g.get("requires", []):
            prev = out.get(req)
            if prev is None or prev.status == "not passed":
                failed.append(f"Gate {req} not passed")
            elif prev.status == "undetermined":
                pending.append(f"Gate {req}")
        status = "not passed" if failed else "undetermined" if pending else "passed"
        out[g["id"]] = CmdbGate(id=g["id"], name=g["name"], unlocks=g["unlocks"], status=status,
                                failed=failed, pending=pending)
    return list(out.values())
