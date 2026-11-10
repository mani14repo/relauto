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


def _flags_for(*defects):
    return {**FULL, **{n: True for d in defects for n in DEFECTS[d].needs}}


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_each_defect_matches_its_answer_key(tmp_path, defect):
    _assert_matches(_run(tmp_path, [defect], tier=1 if defect == "cmdb_missing_support_group" else 2,
                         flags=_flags_for(defect), name=defect))


def test_all_defects_together(tmp_path):
    second = {b for _, b in EXCLUSIVE}
    chosen = [d for d in DEFECTS if d not in second]
    _assert_matches(_run(tmp_path, chosen, tier=1, flags=_flags_for(*chosen)))


def test_healthy_operational_data_has_no_gaps(tmp_path):
    """Every rule on the generated ServiceNow tables and registers is Met when no defect is planted."""
    from datetime import datetime

    from scenario_kit import PROFILE_FLAGS
    from sra.cli import _bundle, _load_profile
    from sra.detectors import data_rules
    from sra.pipeline import context_for
    spec = Spec.load({"slug": "ops", "name": "Payments Hub", "flags": {f: True for f in PROFILE_FLAGS}})
    sc = render(spec, tmp_path / "ops")
    as_of = datetime.fromisoformat(spec.as_of)
    b = _bundle(sc, as_of)
    assert not b.errors and not [it.name for it in b.items.values() if it.table == "unknown"]
    rep = data_rules.run(b, context_for(_load_profile(sc, None), as_of))
    assert len(rep.results) >= 50
    assert {cid: r.rationale for cid, r in rep.results.items() if r.result.value != "Met"} == {}


def test_invalid_specs_are_rejected():
    assert Spec.load({"name": "X", "defects": ["single_replica", "made_up"]}).validate() == [
        "unknown defect: made_up", "defect single_replica needs containerized: true"]
    assert Spec.load({"name": "X", "trigger": "deployment", "hosting": "on_prem",
                      "defects": ["toil_rising", "stale_cloud_cis"], "flags": {"sre_toil_work": True}}).validate() == [
        "defect toil_rising needs trigger: periodic", "defect stale_cloud_cis needs hosting: cloud or hybrid"]
    assert Spec.load({"name": "X", "defects": ["dependencies_unmapped", "dependencies_stale"]}).validate() == [
        "choose one of dependencies_unmapped / dependencies_stale"]
