"""Minimal readers for .docx and .xlsx (zip + XML), no python-docx/openpyxl needed.

The OIPReqs templates are generated documents with direct formatting only, so a
plain walk over the body (paragraphs and tables in order) is all we need.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
S = "{%s}" % S_NS


def text_of(el) -> str:
    """Visible text of an element: w:t runs, tabs and breaks as spaces."""
    parts = []
    for node in el.iter(W + "t", W + "tab", W + "br"):
        parts.append(node.text or "" if node.tag == W + "t" else " ")
    return "".join(parts)


@dataclass
class Para:
    text: str
    el: object = field(repr=False, default=None)


@dataclass
class Table:
    rows: list[list[str]]
    el: object = field(repr=False, default=None)

    def kv(self) -> dict[str, str]:
        """First column as key, the rest joined as value (2-column tables)."""
        out = {}
        for row in self.rows:
            if row and row[0].strip():
                out.setdefault(row[0].strip(), " ".join(c.strip() for c in row[1:]).strip())
        return out

    @property
    def labels(self) -> list[str]:
        return [r[0].strip() for r in self.rows if r]


def read_document_xml(path: str | Path) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml")


def body_blocks(xml: bytes | etree._Element) -> list[Para | Table]:
    root = etree.fromstring(xml) if isinstance(xml, bytes) else xml
    body = root.find(W + "body")
    blocks: list[Para | Table] = []
    for el in body:
        if el.tag == W + "p":
            blocks.append(Para(text_of(el).strip(), el))
        elif el.tag == W + "tbl":
            rows = [[text_of(tc).strip() for tc in tr.findall(W + "tc")] for tr in el.findall(W + "tr")]
            blocks.append(Table(rows, el))
    return blocks


def docx_blocks(path: str | Path) -> list[Para | Table]:
    return body_blocks(read_document_xml(path))


def docx_text(path: str | Path) -> str:
    """Flatten a docx to plain text; tables as ' | '-joined rows."""
    lines = []
    for b in docx_blocks(path):
        if isinstance(b, Para):
            if b.text:
                lines.append(b.text)
        else:
            lines.extend(" | ".join(r) for r in b.rows)
    return "\n".join(lines)


_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def xlsx_sheets(path: str | Path) -> dict[str, list[list[str]]]:
    """Return {sheet name: rows} with shared and inline strings resolved."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = etree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(S + "t")) for si in sst.findall(S + "si")]
        wb = etree.fromstring(z.read("xl/workbook.xml"))
        rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        out: dict[str, list[list[str]]] = {}
        for sh in wb.find(S + "sheets"):
            tgt = target[sh.get(r_ns)].lstrip("/")
            part = tgt if tgt.startswith("xl/") else "xl/" + tgt
            ws = etree.fromstring(z.read(part))
            rows: list[list[str]] = []
            for row in ws.iter(S + "row"):
                cells: dict[int, str] = {}
                for c in row.findall(S + "c"):
                    m = _CELL_REF.match(c.get("r", ""))
                    idx = _col_index(m.group(1)) if m else len(cells)
                    t = c.get("t")
                    if t == "s":
                        v = c.find(S + "v")
                        val = shared[int(v.text)] if v is not None else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(S + "t"))
                    else:
                        v = c.find(S + "v")
                        val = v.text if v is not None and v.text else ""
                    cells[idx] = val.strip()
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            out[sh.get("name")] = rows
        return out
