"""Detector framework: every detector reads the EvidenceBundle and returns a DetectorReport."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..models import CriterionResult, Finding, FindingType, Recommendation, Source

APP_FIELDS = ("cmdb_ci_business_app", "business_service", "u_business_application", "cmdb_ci", "u_application",
              "application", "service_offering", "name", "assignment_group")


@dataclass
class Context:
    app_name: str
    as_of: datetime
    cfg: dict                       # config/detectors.yaml
    tier: int | None = None
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self):
        names = [self.app_name, *self.aliases]
        self._alias_re = re.compile("|".join(re.escape(n.strip().lower()) for n in names if n.strip()) or r"(?!)")

    def months_ago(self, months: float) -> datetime:
        return self.as_of - timedelta(days=30.44 * months)

    def days_ago(self, days: float) -> datetime:
        return self.as_of - timedelta(days=days)

    def matches(self, row: dict, fields=APP_FIELDS) -> bool:
        return any(self._alias_re.search(str(row.get(f, "")).lower()) for f in fields if row.get(f))

    def scoped(self, rows: list[dict], fields=APP_FIELDS) -> list[dict]:
        """Rows for this application. Exports without any app column are assumed pre-scoped."""
        if not any(any(r.get(f) for f in fields) for r in rows):
            return rows
        return [r for r in rows if self.matches(r, fields)]


@dataclass
class DetectorReport:
    name: str
    results: dict[str, CriterionResult] = field(default_factory=dict)   # template criterion id -> result
    rubric: dict[str, CriterionResult] = field(default_factory=dict)    # rubric id -> result
    findings: list[Finding] = field(default_factory=list)
    status: str = "ran"


def result(cid: str, res: Result, rationale: str, refs: list[str] | None = None, risk: Risk | None = None,
           review: bool = False, confidence: float = 1.0) -> CriterionResult:
    return CriterionResult(criterion_id=cid, result=res, rationale=rationale, evidence_refs=list(dict.fromkeys(refs or [])),
                           risk=risk, source=Source.RULE, needs_human_review=review, confidence=confidence)


def finding(ftype: FindingType, severity: Risk, title: str, detail: str = "", *, criteria=(), rubric=(),
            refs=(), detector: str, action: str = "", confidence: float = 1.0) -> Finding:
    return Finding(type=ftype, severity=severity, title=title, detail=detail, criterion_ids=list(criteria),
                   rubric_ids=list(rubric), evidence_refs=list(dict.fromkeys(refs)), detector=detector,
                   confidence=confidence,
                   recommendation=Recommendation(action=action, source=Source.RULE) if action else None)


def ref(row: dict, key: str = "number") -> str:
    return f"{row.get('_evidence', 'EV')}:{row.get(key) or row.get('sys_id') or row.get('name', '?')}"


def truthy(v) -> bool:
    return str(v).strip().lower() not in ("", "false", "0", "no", "none", "null", "n/a")
