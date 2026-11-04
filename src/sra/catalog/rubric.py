"""Parse the Reliability Readiness Score Rubric (.docx) and the Evidence Catalog (.xlsx)."""
from __future__ import annotations

import re
from pathlib import Path

from ..ooxml import Para, Table, docx_blocks, xlsx_sheets
from .models import Domain, RatingBand, Rubric, RubricCriterion

_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_rubric(rubric_docx: str | Path, evidence_xlsx: str | Path | None = None) -> Rubric:
    blocks = docx_blocks(rubric_docx)
    version = next((b.text for b in blocks if isinstance(b, Para) and b.text.startswith("Version:")), "")
    ratings: list[RatingBand] = []
    weights: dict[Domain, float] = {}
    criteria: list[RubricCriterion] = []
    outputs: list[str] = []
    for t in (b for b in blocks if isinstance(b, Table)):
        head = t.rows[0] if t.rows else []
        if head[:1] == ["Rating"]:
            ratings = [RatingBand(rating=r[0], meaning=r[1], p1=r[2], p2=r[3], outcome=r[4]) for r in t.rows[1:]]
        elif head[:2] == ["Domain", "Weight"]:
            for r in t.rows[1:]:
                m = _WEIGHT.search(r[1])
                if m:
                    weights[Domain(r[0])] = float(m.group(1)) / 100
        elif head[:2] == ["ID", "Criterion"]:
            for r in t.rows[1:]:
                criteria.append(RubricCriterion(id=r[0], priority=r[0][:2], name=r[1],
                                                domain=Domain(r[2]), condition=r[3]))
        elif head[:1] == ["Output Field"]:
            outputs = [r[0] for r in t.rows[1:]]

    if evidence_xlsx:
        sheets = xlsx_sheets(evidence_xlsx)
        rows = sheets.get("Evidence Catalog", [])
        if rows:
            cols = {name: i for i, name in enumerate(rows[0])}

            def g(r, name):
                i = cols.get(name)
                return r[i] if i is not None and i < len(r) else ""

            by_id = {c.id: c for c in criteria}
            for r in rows[1:]:
                c = by_id.get(g(r, "Criterion ID"))
                if not c:
                    continue
                c.tables = g(r, "ServiceNow Table(s)")
                c.fields = g(r, "Fields to Inspect")
                c.rule = g(r, "Evaluation Logic / Rule")
                c.pass_condition = g(r, "Pass Condition")
                c.gap_condition = g(r, "Gap Condition")
                c.method = g(r, "Evaluation Method")
                c.confirmation_needed = g(r, "CVS Confirmation Needed")
                c.notes = g(r, "Notes / Rationale")

    if not (ratings and weights and criteria):
        raise ValueError(f"rubric parse incomplete: ratings={len(ratings)} weights={len(weights)} criteria={len(criteria)}")
    return Rubric(version=version, ratings=ratings, domain_weights=weights, criteria=criteria, output_fields=outputs)
