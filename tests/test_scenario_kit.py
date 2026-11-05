"""The scenario kit's answer keys must match what the bot actually finds (no LLM, no network)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "eval"), str(ROOT / "eval" / "synthetic")]
from run_eval import evaluate  # noqa: E402
from scenario_kit import DEFECTS, EXCLUSIVE, Spec, render  # noqa: E402

FULL = {"containerized": True, "has_cicd": True, "has_sla": True}


def _run(tmp_path, defects=(), tier=2, flags=FULL, name="case"):
    spec = Spec.load({"slug": name, "name": "Payments Hub", "tier": tier, "flags": dict(flags), "defects": list(defects)})
    return evaluate(render(spec, tmp_path / name), False, None)


def _assert_matches(r):
    assert r["rating_ok"], r
    assert not r["missed"], r["missed"]
    assert not r["unexpected_gaps"], r["unexpected_gaps"]
    assert r["p1_gap_recall"] == 1 and r["p2_gap_recall"] == 1, r


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_healthy_baseline_is_ready(tmp_path, tier):
    r = _run(tmp_path, tier=tier)
    _assert_matches(r)
    assert r["rating_ok"]


def test_baseline_without_kubernetes_or_cicd_is_ready(tmp_path):
    _assert_matches(_run(tmp_path, flags={"has_sla": True}))


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_each_defect_matches_its_answer_key(tmp_path, defect):
    _assert_matches(_run(tmp_path, [defect], tier=1 if defect == "cmdb_missing_support_group" else 2, name=defect))


def test_all_defects_together(tmp_path):
    second = {b for _, b in EXCLUSIVE}
    _assert_matches(_run(tmp_path, [d for d in DEFECTS if d not in second], tier=1))


def test_invalid_specs_are_rejected():
    assert Spec.load({"name": "X", "defects": ["single_replica", "made_up"]}).validate() == [
        "unknown defect: made_up", "defect single_replica needs containerized: true"]
    assert Spec.load({"name": "X", "defects": ["dependencies_unmapped", "dependencies_stale"]}).validate() == [
        "choose one of dependencies_unmapped / dependencies_stale"]
