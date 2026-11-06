"""Verify an LLM criterion result before it is trusted (pattern from RunbookIntelligence drafting/check.py).

Rejects: invalid result values, Met/Gap without citations, citations of evidence that was not provided,
empty rationale, out-of-range confidence, and 'Met' while listing unmet conditions.
"""
from __future__ import annotations

VALID = {"Met", "Gap", "Insufficient Evidence"}
RISKS = {"Critical", "High", "Medium", "Low"}


def verify(raw: dict, provided_ids: set[str]) -> tuple[bool, list[str], dict]:
    problems: list[str] = []
    out = {
        "result": str(raw.get("result", "")).strip(),
        "rationale": str(raw.get("rationale", "")).strip(),
        "cited": [str(x).strip() for x in (raw.get("cited") or []) if str(x).strip()],
        "gaps": [str(x).strip() for x in (raw.get("gaps") or []) if str(x).strip()],
        "recommendation": str(raw.get("recommendation", "") or "").strip(),
        "risk": str(raw.get("risk", "High")).strip().capitalize(),
        "confidence": raw.get("confidence", 0),
        "measured_value": str(raw.get("measured_value") or "").strip()[:300],
        "maturity_level": raw.get("maturity_level"),
    }
    lv = out["maturity_level"]
    out["maturity_level"] = lv if isinstance(lv, int) and not isinstance(lv, bool) and 0 <= lv <= 4 else None
    if out["result"] not in VALID:
        problems.append(f"invalid result {out['result']!r}")
    if not out["rationale"]:
        problems.append("empty rationale")
    invented = [c for c in out["cited"] if c not in provided_ids]
    if invented:
        problems.append(f"cited evidence not provided: {', '.join(invented[:5])}")
    if out["result"] in ("Met", "Gap") and not [c for c in out["cited"] if c in provided_ids]:
        problems.append(f"{out['result']} without a valid citation")
    if out["result"] == "Met" and out["gaps"]:
        problems.append("Met while listing unmet conditions")
    if out["risk"] not in RISKS:
        out["risk"] = "High"
    try:
        out["confidence"] = float(out["confidence"])
        if not 0 <= out["confidence"] <= 1:
            problems.append("confidence out of range")
    except (TypeError, ValueError):
        problems.append("confidence not a number")
    return not problems, problems, out
