"""Runtime assessment models (what an assessment produces), separate from the catalog."""
from __future__ import annotations

import uuid
import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

from .catalog.models import Domain, Result, Risk


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Source(StrEnum):
    RULE = "rule"        # deterministic detector
    LLM = "llm"          # AI-assisted evaluation (verified)
    HUMAN = "human"      # assessor entry or override
    DERIVED = "derived"  # aggregated (e.g. rubric result from mapped criteria)


class FindingType(StrEnum):
    GAP = "gap"
    VULNERABILITY = "vulnerability"
    ANOMALY = "anomaly"
    OBSERVATION = "observation"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"     # assessor confirmed
    DISPUTED = "disputed"     # assessor rejected, with reason
    RESOLVED = "resolved"


class Recommendation(BaseModel):
    action: str
    playbooks: list[str] = Field(default_factory=list)
    playbook_steps: list[str] = Field(default_factory=list)
    owner: str = ""
    due_date: dt.date | None = None
    source: Source = Source.RULE


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: new_id("F"))
    type: FindingType
    severity: Risk
    title: str
    detail: str = ""
    criterion_ids: list[str] = Field(default_factory=list)   # template criteria it affects
    rubric_ids: list[str] = Field(default_factory=list)      # P1-xx / P2-xx it affects
    evidence_refs: list[str] = Field(default_factory=list)
    detector: str = ""                                       # rule id, "llm:<model>", "human"
    confidence: float = 1.0
    status: FindingStatus = FindingStatus.OPEN
    recommendation: Recommendation | None = None


class CriterionResult(BaseModel):
    criterion_id: str
    result: Result | None = None          # None = not yet evaluated
    risk: Risk | None = None              # risk classification when Gap (defaults to catalog risk)
    rationale: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    source: Source | None = None
    confidence: float | None = None
    needs_human_review: bool = False
    na_justification: str = ""
    na_approved: bool = False
    reviewer: str = ""
    recommendation: str = ""              # text for the template's Recommendation row
    validation: str = ""                  # Variant B "Validation" row
    measured_value: str = ""              # Variant C "Measured value" (e.g. "87% of 214 incidents")
    maturity_level: int | None = Field(None, ge=0, le=4)   # Variant C criterion maturity L0-L4
    # A rule Gap whose only reason is that keywords did not match (document checklists). When the run uses the LLM,
    # the model is asked too, because the wording may differ from the patterns; without the LLM it stays a Gap.
    second_opinion: bool = False


class ApplicationRef(BaseModel):
    name: str
    cmdb_id: str = ""


class SignOff(BaseModel):
    role: str
    name: str = ""
    date: dt.date | None = None
    decision: str = ""


class Assessment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("SRA"))
    application: ApplicationRef
    assessment_date: dt.date = Field(default_factory=dt.date.today)
    assessor: str = ""
    profile: dict = Field(default_factory=dict)
    templates: list[str] = Field(default_factory=list)
    results: dict[str, CriterionResult] = Field(default_factory=dict)          # by template criterion id
    rubric_direct: dict[str, CriterionResult] = Field(default_factory=dict)    # direct rubric evaluations
    findings: list[Finding] = Field(default_factory=list)
    signoffs: list[SignOff] = Field(default_factory=list)
    detector_runs: dict[str, str] = Field(default_factory=dict)  # detector -> "ran" | "skipped: reason"
    matrix_version: str = ""
    status: str = "draft"
    # criterion id -> {"key": hash of model + prompt + evidence sent, "result": CriterionResult}; lets a re-run reuse
    # an LLM answer when nothing it was shown has changed. Kept across runs; never shown as a result by itself.
    llm_cache: dict[str, dict] = Field(default_factory=dict)


class TemplateScore(BaseModel):
    template_id: str
    domain: Domain
    score: float | None          # None if nothing scorable (all N/A)
    weight: float
    met: int = 0
    gaps: int = 0
    insufficient: int = 0
    na: int = 0
    unevaluated: int = 0


class RubricOutcome(BaseModel):
    rubric_id: str
    priority: str
    domain: Domain
    name: str
    result: Result
    contributing: list[str] = Field(default_factory=list)


class CmdbDimension(BaseModel):
    name: str
    weight: float
    score: float | None          # 0-100: mean criterion level / 4; None when nothing assessed yet
    assessed: int
    total: int
    missing: list[str] = Field(default_factory=list)   # criteria without a maturity level yet


class CmdbGate(BaseModel):
    id: str                      # A-D
    name: str
    unlocks: str
    status: str                  # passed | not passed | undetermined
    failed: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class CmdbReadiness(BaseModel):
    """CMDB/CSDM V2 view (CMDB-AT-13): Readiness Index, indicative maturity L0-L4 and reporting gates."""
    cri: float | None
    complete: bool               # every in-scope CRI criterion has a level
    assessed: int
    total: int
    level: int | None
    level_name: str | None
    dimensions: list[CmdbDimension]
    gates: list[CmdbGate]
    thresholds_status: str = ""


class ScoreCard(BaseModel):
    matrix_version: str
    template_scores: list[TemplateScore]
    domain_scores: dict[Domain, float]
    index: float | None
    maturity_level: int | None
    maturity_name: str | None
    maturity_capped: bool = False
    rating: str
    rating_outcome: str
    remediation_days: int | None
    rubric: list[RubricOutcome]
    p1_gaps: list[str]
    p2_gaps: list[str]
    met: list[str]
    not_applicable: list[str]
    insufficient: list[str]
    evidence_coverage_pct: float
    conflicts: list[str] = Field(default_factory=list)
    cmdb: CmdbReadiness | None = None    # present when a V2 CMDB template is assessed
