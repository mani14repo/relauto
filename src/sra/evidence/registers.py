"""Registers: team spreadsheets (risk, exception, improvement, vendor, ... registers) in CSV or XLSX.

`register_type(name)` recognises a file by its name (config/registers.yaml); `normalize(rows, type)` maps its
headers onto canonical column names and parses date columns into `_<column>` datetimes, the same convention as
ServiceNow exports, so rules can treat a register like a table named `register:<type>`.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from functools import lru_cache

from ..config import load_yaml
from .redact import clean_text, parse_datetime, redact_pii

PREFIX = "register:"


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_yaml("registers")


def _key(header: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-/]+", " ", str(header))).strip().lower().rstrip(" ?:")


def types() -> list[str]:
    return list(_cfg()["types"])


def register_type(name: str) -> str | None:
    """The register type a file name points to, e.g. 'Orders_Risk_Register.xlsx' -> 'risk'."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for t, spec in _cfg()["types"].items():
        if any(re.search(p, base, re.I) for p in spec.get("files", [])):
            return t
    return None


def _aliases(t: str) -> dict[str, str]:
    """alias (normalised) -> canonical column, type-specific aliases first."""
    out: dict[str, str] = {}
    spec = _cfg()["types"][t]
    for canon, names in list((spec.get("columns") or {}).items()) + list(_cfg()["common"].items()):
        for n in [canon, *names]:
            out.setdefault(_key(n), canon)
    return out


def normalize(rows: list[dict], t: str) -> list[dict]:
    aliases, spec = _aliases(t), _cfg()["types"][t]
    dates = set(spec.get("dates", [])) | {"due_date", "completed_date", "opened_date"}
    out = []
    for r in rows:
        row: dict = {}
        for k, v in r.items():
            if k is None:
                continue
            canon = aliases.get(_key(k), _key(k).replace(" ", "_"))
            if canon in row and row[canon] not in ("", None):
                continue                      # first matching column wins
            if isinstance(v, (datetime, date)):
                s = v.isoformat()
            else:
                s = "" if v is None else str(v).strip()
            row[canon] = clean_text(s) if len(s) > 40 else redact_pii(s)
        for d in dates:
            if row.get(d):
                row["_" + d] = parse_datetime(row[d])
        if any(str(v).strip() for k, v in row.items() if not k.startswith("_")):
            out.append(row)
    return out


def read_xlsx(data: bytes) -> list[dict]:
    """Rows of the first sheet that has a header row, as dicts keyed by the header (openpyxl, values only)."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            header = None
            for raw in it:                        # skip title rows above the table: the header is the first row
                cells = [c for c in raw if c not in (None, "")]   # with at least two text cells
                if len(cells) >= 2 and all(isinstance(c, str) for c in cells):
                    header = [str(c).strip() if c is not None else "" for c in raw]
                    break
            if not header:
                continue
            rows = []
            for raw in it:
                if not any(c not in (None, "") for c in raw):
                    continue
                rows.append({h: v for h, v in zip(header, raw) if h})
            return rows
        return []
    finally:
        wb.close()
