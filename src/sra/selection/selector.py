"""Rule-based template selection over config/selection_rules.yaml.

Expressions are parsed, never eval()'d: "<field> <op> <value>" or a bare "<field>" (truthy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from ..catalog.models import Template
from ..config import load_yaml
from .profile import ApplicationProfile

EXPR = re.compile(r"^\s*(\w+)\s*(==|!=|<=|>=|<|>|not_in|in)\s*(.+?)\s*$")


def evaluate(expr: str, profile: dict) -> bool:
    m = EXPR.match(expr)
    if not m:
        return bool(profile.get(expr.strip()))
    fld, op, raw = m.groups()
    value = yaml.safe_load(raw)
    actual = profile.get(fld)
    if hasattr(actual, "value"):
        actual = actual.value
    if op in ("in", "not_in"):
        items = value if isinstance(value, list) else [value]
        hit = actual in items
        return hit if op == "in" else not hit
    if actual is None:
        return False
    try:
        return {"==": actual == value, "!=": actual != value, "<": actual < value, "<=": actual <= value,
                ">": actual > value, ">=": actual >= value}[op]
    except TypeError:
        return False


@dataclass
class Candidate:
    template_id: str
    title: str
    score: float
    selected: bool
    always: bool = False
    reasons: list[str] = field(default_factory=list)


def select_templates(profile: ApplicationProfile, templates: dict[str, Template]) -> list[Candidate]:
    cfg = load_yaml("selection_rules")
    threshold = cfg["threshold"]
    p = profile.model_dump()
    out = []
    for tid, t in templates.items():
        rule = cfg["templates"].get(tid, {})
        if rule.get("always"):
            out.append(Candidate(tid, t.title, float("inf"), True, True, [rule.get("reason", "always applies")]))
            continue
        score, reasons = 0.0, []
        for r in rule.get("rules", []):
            if evaluate(r["if"], p):
                score += r["weight"]
                reasons.append(r["reason"])
        out.append(Candidate(tid, t.title, score, score >= threshold, False, reasons))
    out.sort(key=lambda c: (not c.selected, -c.score if c.score != float("inf") else -1e9, c.template_id))
    return out
