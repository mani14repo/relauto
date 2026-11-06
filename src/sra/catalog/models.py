"""Catalog models: the structured form of the OIPReqs assessment templates and rubric."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Domain(StrEnum):
    SRE = "SRE"
    ITSM = "ITSM"
    CMDB = "CMDB"


class Method(StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"
    HUMAN = "human"


class Risk(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Result(StrEnum):
    MET = "Met"
    GAP = "Gap"
    INSUFFICIENT = "Insufficient Evidence"
    NA = "Not Applicable"


class Criterion(BaseModel):
    id: str                      # e.g. SRE-AT-01-C01 (synthesized for Variant A)
    number: int
    name: str
    standard_ref: str = ""
    applicability: str = ""
    required_evidence: str = ""
    evaluation_method: str = ""  # raw text from the template
    methods: list[Method] = Field(default_factory=list)
    acceptance_criteria: str = ""
    default_risk: Risk = Risk.HIGH
    default_risk_assumed: bool = False   # True when the template gives no risk (Variant B)
    accountable_owner: str = ""
    reassessment: str = ""
    # Variant A ships portfolio-specific example text; kept for few-shot use, never copied to output.
    example_risk: str = ""
    example_recommendation: str = ""
    references: list[str] = Field(default_factory=list)  # SRE-STD-03, SRE-PB-11 ...
    # Variant C (CMDB/CSDM V2) only: the template states these explicitly.
    rubric_refs: list[str] = Field(default_factory=list)  # rubric criteria this criterion feeds, e.g. P1-01
    rubric_enablers: list[str] = Field(default_factory=list)  # marked "(enabler)": supports, does not decide
    csdm_layer: str = ""
    data_source: str = ""
    formula: str = ""                    # "Measurement / formula"
    maturity_bands: str = ""             # L0-L4 band definitions
    look_for: str = ""                   # "What to look for"
    risk_if_gap: str = ""                # guidance text, not an example finding
    playbooks: list[str] = Field(default_factory=list)


class Template(BaseModel):
    id: str                      # SRE-AT-01
    title: str
    domain: Domain
    variant: str                 # "A", "B" (V1 layouts) or "C" (CMDB/CSDM V2)
    priority: str = ""           # P1/P2/P3 (Variant A only; B defaults from config)
    applicability: str = ""
    accountable_owner: str = ""
    purpose: str = ""
    relevance: str = ""
    reassessment_trigger: str = ""
    scope: list[str] = Field(default_factory=list)
    signoff_roles: list[str] = Field(default_factory=list)
    criteria: list[Criterion]
    source_file: str
    references: list[str] = Field(default_factory=list)
    version: str = "V1"                  # document version, from the file name or Document Control
    rubric_refs: list[str] = Field(default_factory=list)  # Variant C: "Readiness Rubric criteria fed"


class RubricCriterion(BaseModel):
    id: str                      # P1-01
    priority: str                # P1 / P2
    name: str
    domain: Domain
    condition: str               # blocking / gap condition text
    # From the Evidence Catalog (may be empty if not catalogued)
    tables: str = ""
    fields: str = ""
    rule: str = ""
    pass_condition: str = ""
    gap_condition: str = ""
    method: str = ""
    confirmation_needed: str = ""
    notes: str = ""


class RatingBand(BaseModel):
    rating: str
    meaning: str
    p1: str
    p2: str
    outcome: str


class Rubric(BaseModel):
    version: str
    ratings: list[RatingBand]
    domain_weights: dict[Domain, float]
    criteria: list[RubricCriterion]
    output_fields: list[str]
