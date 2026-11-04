"""Parse the OIPReqs playbooks (Playbook Template layout) into remediation steps."""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..ooxml import Table, docx_blocks

ID_RE = re.compile(r"\b((?:SRE|ITSM|CMDB)-(?:PB|STD|POL|PRC)-\d{2})\b")


class Step(BaseModel):
    n: int
    action: str


class Playbook(BaseModel):
    id: str
    title: str
    objective: str = ""
    triggers: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    decisions: list[tuple[str, str]] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    source_file: str


def parse_playbook(path: Path) -> Playbook:
    m = ID_RE.search(path.name)
    pid = m.group(1)
    rows = [r for b in docx_blocks(path) if isinstance(b, Table) for r in b.rows]
    title, objective = path.stem.split(" — ", 1)[-1], ""
    triggers, steps, decisions, validation = [], [], [], []
    section = ""
    for r in rows:
        first = r[0].strip()
        if re.match(r"^\d{1,2}\.\s+[A-Z]", first) and len(r) == 1:
            section = first.split(".", 1)[1].strip().lower()
            continue
        if len(r) >= 2 and first == "Title":
            title = r[1]
        elif len(r) >= 2 and first == "Objective":
            objective = r[1]
        elif section.startswith("trigger") and len(r) == 1:
            triggers.append(first.lstrip("•").strip())
        elif section.startswith("procedure") and len(r) >= 2 and re.match(r"^\d+\.?$", first):
            steps.append(Step(n=int(first.rstrip(".")), action=r[1]))
        elif section.startswith(("process", "procedure")) and len(r) == 1:
            # Alternate layout: "Step 1 — Title" rows followed by bullet rows.
            sm = re.match(r"^Step\s+(\d+)\s*[—–-]\s*(.*)$", first)
            if sm:
                steps.append(Step(n=int(sm.group(1)), action=sm.group(2).strip()))
            elif steps and first.startswith("•"):
                steps[-1].action += ("; " if ":" in steps[-1].action or ";" in steps[-1].action else ": ") + first.lstrip("• ").strip()
        elif section.startswith("decision") and len(r) >= 2 and first != "Condition":
            decisions.append((first, r[1]))
        elif section.startswith("validation") and len(r) == 1:
            validation.append(first.lstrip("✓").strip())
    return Playbook(id=pid, title=title, objective=objective, triggers=triggers, steps=steps, decisions=decisions,
                    validation=validation, source_file=path.name)


def parse_titles(folder: Path) -> dict[str, str]:
    """ID -> title for every standard/policy/process/playbook file in a folder."""
    out = {}
    for p in folder.glob("*.docx"):
        m = ID_RE.search(p.name)
        if m:
            out[m.group(1)] = p.stem.split(" — ", 1)[-1]
    return out
