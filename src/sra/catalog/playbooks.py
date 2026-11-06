"""Parse the OIPReqs playbooks (Playbook Template layout) into remediation steps.

V1 playbooks keep their section headings as single-cell table rows. V2 (CMDB/CSDM) playbooks use
paragraph headings and a 'Step | Action | Expected result | Owner' procedure table with phase rows.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..ooxml import Para, Table, docx_blocks

ID_RE = re.compile(r"\b((?:SRE|ITSM|CMDB)-(?:PB|STD|POL|PRC)-\d{2})\b")
VERSION_RE = re.compile(r"\s*[—-]\s*(V\d+)\s*$", re.I)


class Step(BaseModel):
    n: int
    action: str
    expected: str = ""           # V2: expected result of the step
    owner: str = ""              # V2: role that performs it
    phase: str = ""              # V2: e.g. "PHASE A — Gather"


class Playbook(BaseModel):
    id: str
    title: str
    objective: str = ""
    triggers: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    decisions: list[tuple[str, str]] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    source_file: str
    version: str = "V1"


def title_from_name(stem: str) -> str:
    """'CMDB-PB-05 — Service Dependency Mapping Playbook — V2' -> 'Service Dependency Mapping Playbook'."""
    return VERSION_RE.sub("", stem.split(" — ", 1)[-1]).strip()


def version_of(stem: str) -> str:
    m = VERSION_RE.search(stem)
    return m.group(1).upper() if m else "V1"


def parse_playbook(path: Path) -> Playbook:
    path = Path(path)
    pid = ID_RE.search(path.name).group(1)
    blocks = docx_blocks(path)
    if any(isinstance(b, Table) and b.rows and b.rows[0][:2] == ["Step", "Action"] for b in blocks):
        return _parse_v2(path, pid, blocks)
    rows = [r for b in blocks if isinstance(b, Table) for r in b.rows]
    title, objective = title_from_name(path.stem), ""
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
                    validation=validation, source_file=path.name, version=version_of(path.stem))


def _parse_v2(path: Path, pid: str, blocks: list) -> Playbook:
    title, objective = title_from_name(path.stem), ""
    triggers, steps, decisions, validation = [], [], [], []
    for b in blocks:
        if isinstance(b, Para) or not b.rows:
            continue
        head = [c.strip() for c in b.rows[0]]
        kv = b.kv()
        if "Objective" in kv and not objective:
            objective = kv["Objective"]
        if head[:2] == ["Step", "Action"]:
            phase = ""
            for r in b.rows[1:]:
                first = r[0].strip()
                if re.match(r"^\d+\.?$", first) and len(r) >= 2:
                    n = int(first.rstrip('.'))
                    n = n if n not in {st.n for st in steps} else max(st.n for st in steps) + 1
                    steps.append(Step(n=n, action=r[1].strip(), expected=r[2].strip() if len(r) > 2 else "",
                                      owner=r[3].strip() if len(r) > 3 else "", phase=phase))
                elif first and len(set(c.strip() for c in r)) == 1:     # merged phase row
                    phase = first
        elif head[:2] == ["#", "Trigger"]:
            triggers += [r[1].strip() for r in b.rows[1:] if len(r) > 1 and r[1].strip()]
        elif head[:1] == ["Condition"]:
            decisions += [(r[0].strip(), " / ".join(c.strip() for c in r[1:] if c.strip())) for r in b.rows[1:] if r[0].strip()]
        elif head[:1] == ["Validation check"]:
            validation += [" — ".join(c.strip() for c in r[:2] if c.strip()) for r in b.rows[1:] if r[0].strip()]
    return Playbook(id=pid, title=title, objective=objective, triggers=triggers, steps=steps, decisions=decisions,
                    validation=validation, source_file=path.name, version=version_of(path.stem))


def parse_titles(folder: Path) -> dict[str, str]:
    """ID -> title for every standard/policy/process/playbook file in a folder (latest version wins)."""
    out, ver = {}, {}
    for p in folder.glob("*.docx"):
        m = ID_RE.search(p.name)
        if not m:
            continue
        v = int(version_of(p.stem)[1:])
        if v >= ver.get(m.group(1), 0):
            out[m.group(1)], ver[m.group(1)] = title_from_name(p.stem), v
    return out
