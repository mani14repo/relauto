"""Evidence models: what was ingested for an assessment."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Kind(StrEnum):
    SN_TABLE = "servicenow_table"
    DOCUMENT = "document"
    SCAN = "scan"
    K8S = "k8s_manifest"
    CI_CONFIG = "ci_config"
    OTHER = "other"


class DocType(StrEnum):
    RUNBOOK = "runbook"
    ARCHITECTURE = "architecture"
    DR_TEST = "dr_test"
    DR_PLAN = "dr_plan"
    LOAD_TEST = "load_test"
    SLO = "slo"
    ERROR_BUDGET = "error_budget"
    CAPACITY = "capacity"
    POSTMORTEM = "postmortem"
    OBSERVABILITY = "observability"
    SECURITY = "security"
    OTHER = "other"


@dataclass
class EvidenceItem:
    id: str                       # stable: EV-<sha8>
    kind: Kind
    name: str                     # file name or source label
    sha256: str
    ingested_at: datetime
    doc_type: DocType | None = None
    table: str | None = None      # ServiceNow table name
    text: str = ""                # redacted text (documents, configs)
    reviewed_at: datetime | None = None   # last-reviewed date found in the document
    records: list[dict] = field(default_factory=list)   # table rows (redacted)
    meta: dict = field(default_factory=dict)

    def ref(self, locator: str = "") -> str:
        return f"{self.id}:{self.name}" + (f"#{locator}" if locator else "")


def evidence_id(data: bytes) -> tuple[str, str]:
    h = hashlib.sha256(data).hexdigest()
    return f"EV-{h[:8]}", h


@dataclass
class ScanFinding:
    """A normalized scanner finding (SARIF, CycloneDX VEX, Trivy, CSV)."""
    tool: str
    rule_id: str                  # CVE-..., rule name
    title: str
    severity: str                 # Critical/High/Medium/Low
    package: str = ""
    installed_version: str = ""
    fixed_version: str = ""
    location: str = ""
    evidence_id: str = ""
