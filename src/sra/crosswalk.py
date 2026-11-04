"""Lookups over config/crosswalk.yaml (template criterion <-> rubric <-> standards <-> playbooks)."""
from __future__ import annotations

from functools import lru_cache

from .config import load_yaml


def _split(cid: str) -> tuple[str, str]:
    """'SRE-AT-01-C03' -> ('SRE-AT-01', 'C03')."""
    tid, _, c = cid.rpartition("-")
    return tid, c


@lru_cache
def rubric_criteria_map() -> dict[str, list[str]]:
    return {rid: list(v.get("criteria", [])) for rid, v in load_yaml("crosswalk")["rubric"].items()}


@lru_cache
def criterion_rubric_ids() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for rid, cids in rubric_criteria_map().items():
        for cid in cids:
            out.setdefault(cid, []).append(rid)
    return out


def rubric_ids_for(cid: str) -> list[str]:
    return criterion_rubric_ids().get(cid, [])


def _lookup(cid: str, key: str) -> list[str]:
    cw = load_yaml("crosswalk")
    tid, c = _split(cid)
    t = cw.get("templates", {}).get(tid, {}) or {}
    crit = (t.get("criteria") or {}).get(c, {}) or {}
    if crit.get(key):
        return list(crit[key])
    if t.get(key):
        return list(t[key])
    for rid in rubric_ids_for(cid):
        if cw["rubric"][rid].get(key):
            return list(cw["rubric"][rid][key])
    return []


def playbooks_for(cid: str) -> list[str]:
    """Playbooks for a template criterion ID or a rubric ID (P1-xx)."""
    if cid[:2] in ("P1", "P2"):
        return list(load_yaml("crosswalk")["rubric"].get(cid, {}).get("playbooks", []))
    return _lookup(cid, "playbooks")


def standards_for(cid: str) -> list[str]:
    if cid[:2] in ("P1", "P2"):
        return list(load_yaml("crosswalk")["rubric"].get(cid, {}).get("standards", []))
    return _lookup(cid, "standards")
