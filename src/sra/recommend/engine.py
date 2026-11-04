"""Attach recommendations to findings: playbook references and steps, owner, due date.

Playbook choice: the finding's own recommendation, else the crosswalk for its first criterion / rubric id.
Step choice: lexical overlap between the finding and each playbook step (deterministic, explainable);
the LLM layer may rewrite the action text later, but the cited steps stay grounded in the playbook.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..catalog import load_playbooks, load_templates
from ..crosswalk import playbooks_for
from ..models import Finding, Recommendation, Source

WORD = re.compile(r"[a-z][a-z0-9]{2,}")
STOP = set("the and for with that this from into each all are not any has have been was were its their them "
           "must should will can per via use using record records application applications".split())
DOMAIN_OWNER = {"SRE": "SRE / Application Engineering", "ITSM": "ITSM Process Owner", "CMDB": "CMDB Standards Owner"}


def _tokens(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def best_steps(playbook_id: str, text: str, k: int = 2) -> list[str]:
    pb = load_playbooks().get(playbook_id)
    if not pb or not pb.steps:
        return []
    want = _tokens(text)
    scored = sorted(((len(want & _tokens(s.action)), s.n) for s in pb.steps), key=lambda x: (-x[0], x[1]))
    return [f"{playbook_id} step {n}" for score, n in scored[:k] if score > 0]


def owner_for(f: Finding) -> str:
    ts = load_templates()
    for cid in f.criterion_ids:
        tid = cid.rsplit("-", 1)[0]
        if tid in ts:
            c = next((c for c in ts[tid].criteria if c.id == cid), None)
            if c and c.accountable_owner:
                return c.accountable_owner
    if f.rubric_ids:
        from ..catalog import load_rubric
        rc = next((r for r in load_rubric().criteria if r.id == f.rubric_ids[0]), None)
        if rc:
            return DOMAIN_OWNER[rc.domain.value]
    return "SRE"


def attach(findings: list[Finding], as_of: date, matrix: dict, remediation_days: int | None = None) -> None:
    sev_days = matrix["severity_due_days"]
    for f in findings:
        rec = f.recommendation or Recommendation(action="", source=Source.RULE)
        if not rec.playbooks:
            keys = f.criterion_ids + f.rubric_ids
            for key in keys:
                pbs = playbooks_for(key)
                if pbs:
                    rec.playbooks = pbs[:2]
                    break
        # playbook ids mentioned in the action text
        for pid in re.findall(r"\b(?:SRE|ITSM|CMDB)-PB-\d{2}\b", rec.action):
            if pid not in rec.playbooks:
                rec.playbooks.append(pid)
        if not rec.playbook_steps:
            text = f"{f.title} {f.detail} {rec.action}"
            for pid in rec.playbooks[:2]:
                rec.playbook_steps += best_steps(pid, text)
        if not rec.action:
            rec.action = f"Close the gap: {f.title}"
        if not rec.owner:
            rec.owner = owner_for(f)
        days = sev_days[f.severity.value]
        if remediation_days is not None and remediation_days > 0:
            days = min(days, remediation_days)
        if remediation_days == 0 and any(r.startswith("P1") for r in f.rubric_ids):
            days = min(days, 7)   # Not Ready: P1 blockers are immediate
        rec.due_date = as_of + timedelta(days=days)
        f.recommendation = rec
