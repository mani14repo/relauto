"""ServiceNow exports (CSV or Table API JSON) for incident, change_request, problem, kb_knowledge,
em_alert, cmdb_ci and cmdb_rel_ci. Ported and generalized from RunbookIntelligence incidents/source.py.

Rows are kept as flat dicts of strings (display values), redacted, with known date fields parsed
into `_<field>` datetime keys. Free-text journal fields are reduced to engineer-written notes.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from typing import Any

from .redact import clean_text, parse_datetime, split_notes

MONITORING_CALLERS = re.compile(r"event management|_alert\b|monitoring|appdynamics|splunk", re.I)
DATE_FIELDS = {"opened_at", "resolved_at", "closed_at", "sys_updated_on", "sys_created_on", "start_date",
               "end_date", "work_start", "work_end", "last_discovered", "valid_to", "published",
               "u_last_reviewed", "last_reviewed", "last_calculated", "u_last_calculated"}
FREE_TEXT = {"comments_and_work_notes", "work_notes", "comments"}
# Fields that identify people are dropped: assessments do not need them.
PEOPLE_FIELDS = {"caller_id", "assigned_to", "opened_by", "resolved_by", "closed_by"}

ID_PREFIX = {"INC": "incident", "CHG": "change_request", "PRB": "problem", "KB": "kb_knowledge",
             "ALERT": "em_alert", "PTASK": "problem_task", "CTASK": "change_task"}


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("display_value") or value.get("value") or ""
    return "" if value is None else str(value)


def guess_table(name: str, rows: list[dict]) -> str:
    low = name.lower()
    for t in ("cmdb_rel_ci", "cmdb_ci", "change_request", "kb_knowledge", "em_alert", "problem", "incident",
              "u_slo_definition", "u_business_application"):
        if t in low:
            return t
    if rows:
        cols = set(rows[0])
        if {"parent", "child"} <= cols and "type" in cols:
            return "cmdb_rel_ci"
        num = str(rows[0].get("number", "")).upper()
        for prefix, table in ID_PREFIX.items():
            if num.startswith(prefix):
                return table
        if "sys_class_name" in cols or "owned_by" in cols:
            return "cmdb_ci"
    return "unknown"


def normalize(record: Mapping[str, Any]) -> dict:
    row: dict = {}
    for k, v in record.items():
        if k in PEOPLE_FIELDS:
            if k == "caller_id":
                row["_from_monitoring"] = bool(MONITORING_CALLERS.search(_text(v)))
            continue
        s = _text(v).strip()
        if k in FREE_TEXT:
            notes = split_notes(s)
            row["_human_notes"] = [n.text for n in notes if n.by_person][-10:]
            continue
        row[k] = clean_text(s) if len(s) > 40 else s
        if k in DATE_FIELDS:
            row["_" + k] = parse_datetime(s)
    return row


def read_rows(data: bytes, name: str) -> list[dict]:
    if name.lower().endswith(".json"):
        obj = json.loads(data.decode("utf-8-sig"))
        if isinstance(obj, Mapping):
            obj = obj.get("result", obj.get("records", []))
        records = list(obj)
    else:
        csv.field_size_limit(2**31 - 1)
        records = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace"), newline="")))
    return [normalize(r) for r in records]
