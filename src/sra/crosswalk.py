"""Lookups linking template criteria <-> rubric <-> standards <-> playbooks.

Two sources:
  * config/crosswalk.yaml, curated by hand, for the V1 SRE and ITSM templates (they state no rubric link);
  * the V2 CMDB/CSDM templates themselves (Variant C), which state each criterion's rubric criteria,
    standard requirement and remediation playbooks. These are authoritative for CMDB.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .config import load_yaml

DOC_RE = re.compile(r"\b(?:SRE|ITSM|CMDB)-(?:STD|POL|PRC)-\d{2}\b")


def _split(cid: str) -> tuple[str, str]:
    """'SRE-AT-01-C03' -> ('SRE-AT-01', 'C03')."""
    tid, _, c = cid.rpartition("-")
    return tid, c


@lru_cache
def v2_criteria() -> dict:
    """Criterion ID -> catalog Criterion, for templates whose documents define their own links (Variant C)."""
    try:
        from .catalog import load_templates
        templates = load_templates()
    except FileNotFoundError:
        return {}
    return {c.id: c for t in templates.values() if t.variant == "C" for c in t.criteria}


def is_self_described(cid: str) -> bool:
    """True when the template states this criterion's rubric link itself (no curated equivalence)."""
    return cid in v2_criteria()


@lru_cache
def rubric_criteria_map() -> dict[str, list[str]]:
    out = {rid: [c for c in v.get("criteria", []) if not is_self_described(c)]
           for rid, v in load_yaml("crosswalk")["rubric"].items()}
    for cid, c in v2_criteria().items():
        for rid in c.rubric_refs:
            if cid not in out.setdefault(rid, []):
                out[rid].append(cid)
    return out


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
    c2 = v2_criteria().get(cid)
    if c2 is not None:
        own = c2.playbooks if key == "playbooks" else list(dict.fromkeys(DOC_RE.findall(c2.standard_ref)))
        if own:
            return list(own)
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
