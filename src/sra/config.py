"""Settings: environment first, then .env, then defaults (ported from RunbookIntelligence)."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("SRA_ROOT", Path(__file__).resolve().parents[2]))


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env reader; real environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'") and value[1:].find(value[0]) >= 0:
            value = value[1:1 + value[1:].find(value[0])]      # quoted: take what is inside the quotes
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()   # unquoted: drop an inline " # comment"
        os.environ.setdefault(key.strip(), value)


def setting(name: str, default: str | None = None) -> str | None:
    load_dotenv()
    return os.environ.get(name, default)


def path_setting(name: str, default: Path) -> Path:
    """A directory setting; empty means the default, relative paths are relative to the repo root."""
    raw = setting(name) or ""
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


DOCS_DIR = path_setting("SRA_DOCS_DIR", ROOT / "docs" / "OIPReqs")
# Flat layout: each folder holds its documents directly. File names carry the ID and, from V2 on, the version
# ("CMDB-AT-01 — … — V2.docx"); when two versions of one ID sit side by side, the catalog uses the highest.
TEMPLATES_DIR = DOCS_DIR / "Assessment Templates"
CMDB_CRITERIA_XLSX = DOCS_DIR / "CMDB-CSDM Readiness Criteria Catalog — V2.xlsx"
STANDARDS_DIR = DOCS_DIR / "Standards Policies Procedures"
PLAYBOOKS_DIR = DOCS_DIR / "Playbooks"
RUBRIC_DOCX = STANDARDS_DIR / "Reliability Readiness Score Rubric — V1 Draft.docx"
EVIDENCE_XLSX = STANDARDS_DIR / "Evidence Catalog — AI Reliability Assessment.xlsx"
CATALOG_DIR = path_setting("SRA_CATALOG_DIR", ROOT / "catalog")
CONFIG_DIR = path_setting("SRA_CONFIG_DIR", ROOT / "config")
DATA_DIR = path_setting("SRA_DATA_DIR", ROOT / "data")


@lru_cache
def load_yaml(name: str) -> dict:
    """Load config/<name>.yaml (cached)."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
