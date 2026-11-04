"""Golden evaluation: run a scenario end to end and score the bot against expected.yaml.

    python eval/run_eval.py                         # deterministic layer only
    python eval/run_eval.py --llm                   # include the local LLM (LLM_MODEL / LLM_BASE_URL)
    python eval/run_eval.py --llm --model qwen2.5:3b-instruct --model llama3.1:8b   # compare models

Metrics:
  selection_recall      expected templates selected
  seeded_recall         seeded defects detected (target 1.0)
  p1/p2 gap recall      expected rubric gaps found
  rating_ok             overall rating matches
  llm_*                 calls, verified share, rejected (invented citations / schema), errors, seconds per call
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sra.cli import _bundle, _load_profile  # noqa: E402
from sra.models import ApplicationRef, Assessment  # noqa: E402
from sra.pipeline import propose_templates, run_assessment  # noqa: E402


def evaluate(scenario: Path, use_llm: bool, model: str | None) -> dict:
    exp = yaml.safe_load((scenario / "expected.yaml").read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(exp["as_of"])
    bundle = _bundle(scenario, as_of)
    profile, cands = propose_templates(_load_profile(scenario, None), bundle, as_of)
    selected = [c.template_id for c in cands if c.selected]
    a = Assessment(application=ApplicationRef(name=profile.name), templates=selected, assessment_date=as_of.date())
    evaluator = None
    if use_llm:
        if model:
            os.environ["LLM_MODEL"] = model
        from sra.llm.evaluate import Evaluator
        evaluator = Evaluator.from_settings()
    t0 = time.perf_counter()
    sc, gate = run_assessment(a, bundle, profile, as_of, evaluator)
    elapsed = time.perf_counter() - t0

    seeded = exp["seeded_findings"]
    hit = [s for s in seeded if any(f.detector.startswith(s[0]) and s[1].lower() in f"{f.title} {f.detail}".lower() for f in a.findings)]
    out = {
        "scenario": scenario.name, "model": model or (os.environ.get("LLM_MODEL") if use_llm else None),
        "selection_recall": round(len(set(exp["templates_include"]) & set(selected)) / len(exp["templates_include"]), 3),
        "seeded_recall": round(len(hit) / len(seeded), 3),
        "missed": [s for s in seeded if s not in hit],
        "p1_gap_recall": round(len(set(exp["p1_gaps_include"]) & set(sc.p1_gaps)) / len(exp["p1_gaps_include"]), 3),
        "p2_gap_recall": round(len(set(exp["p2_gaps_include"]) & set(sc.p2_gaps)) / len(exp["p2_gaps_include"]), 3),
        "rating_ok": sc.rating == exp["rating"],
        "index": sc.index, "maturity": sc.maturity_name, "findings": len(a.findings),
        "unevaluated": sum(1 for b in gate.blockers if "not evaluated" in b),
        "seconds": round(elapsed, 1),
    }
    if evaluator:
        s = evaluator.stats
        out.update({f"llm_{k}": v for k, v in s.items()})
        out["llm_verified_share"] = round(s["verified"] / s["calls"], 3) if s["calls"] else None
        out["llm_seconds_per_call"] = round(elapsed / s["calls"], 1) if s["calls"] else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=str(ROOT / "eval" / "scenarios" / "orders_batch"))
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", action="append")
    args = ap.parse_args()
    for m in (args.model or [None]):
        print(json.dumps(evaluate(Path(args.scenario), args.llm, m), indent=1))


if __name__ == "__main__":
    main()
