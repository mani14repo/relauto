"""Settings: environment first, then .env, then defaults (ported from RunbookIntelligence)."""
from __future__ import annotations

import os
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def setting(name: str, default: str | None = None) -> str | None:
    load_dotenv()
    return os.environ.get(name, default)


DOCS_DIR = Path(setting("SRA_DOCS_DIR", str(ROOT / "docs" / "OIPReqs")))
TEMPLATES_DIR = DOCS_DIR / "Assessment Templates" / "Assessment Templates" / "V1"
STANDARDS_DIR = DOCS_DIR / "Standards Policies Procedures"
PLAYBOOKS_DIR = DOCS_DIR / "Playbooks"
RUBRIC_DOCX = STANDARDS_DIR / "Reliability Readiness Score Rubric — V1 Draft.docx"
EVIDENCE_XLSX = STANDARDS_DIR / "Evidence Catalog — AI Reliability Assessment.xlsx"
CATALOG_DIR = Path(setting("SRA_CATALOG_DIR", str(ROOT / "catalog")))
CONFIG_DIR = Path(setting("SRA_CONFIG_DIR", str(ROOT / "config")))
DATA_DIR = Path(setting("SRA_DATA_DIR", str(ROOT / "data")))


@lru_cache
def load_yaml(name: str) -> dict:
    """Load config/<name>.yaml (cached)."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
