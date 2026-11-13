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


def flag(name: str, default: bool) -> bool:
    """A true/false setting; empty means the default."""
    raw = str(setting(name, "") or "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


def llm_enabled() -> bool:
    """LLM_ENABLED=false turns every generative-model feature off: AI-assisted criteria and criterion chat."""
    return flag("LLM_ENABLED", True)


CAP_JDBC = re.compile(r"^CAP_POSTGRES_(?:.+_)?JDBC_URI$")


def database_dsn() -> str | None:
    """The Postgres connection string, or None to use local files.

    DATABASE_URL wins. Otherwise the settings CAP injects for a provisioned database are used, the same ones COG
    reads: CAP_POSTGRES_<APP>_<DB>_JDBC_URI and CAP_POSTGRES_USER (IAM login through the cloud-sql-proxy sidecar, so
    no password). When CAP injects more than one database, SRA_CAP_POSTGRES_VAR names the variable to use.
    """
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    wanted = os.environ.get("SRA_CAP_POSTGRES_VAR")
    names = [wanted] if wanted else sorted(k for k in os.environ if CAP_JDBC.match(k))
    if len(names) > 1:
        raise ValueError(f"several CAP Postgres settings found ({', '.join(names)}); set SRA_CAP_POSTGRES_VAR to one of them")
    jdbc = os.environ.get(names[0]) if names else None
    return jdbc_to_conninfo(jdbc, os.environ.get("CAP_POSTGRES_USER"), os.environ.get("CAP_POSTGRES_PASSWORD")) if jdbc else None


def jdbc_to_conninfo(jdbc: str, user: str | None = None, password: str | None = None) -> str:
    """'jdbc:postgresql://host:5432/db?sslmode=disable' -> a libpq connection string for psycopg.

    A JDBC URI that connects through the Cloud SQL socket factory has no usable host; the sidecar proxy on
    127.0.0.1:5432 is used then. user/password in the URI are honoured when CAP_POSTGRES_USER is not set.
    """
    from urllib.parse import parse_qs, unquote, urlsplit
    if not jdbc.startswith("jdbc:postgresql:"):
        raise ValueError("expected a jdbc:postgresql:// URI")
    u = urlsplit(jdbc[len("jdbc:"):])
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    via_proxy = "socketFactory" in q or "cloudSqlInstance" in q or not u.hostname
    parts = {"host": "127.0.0.1" if via_proxy else u.hostname, "port": str(5432 if via_proxy else (u.port or 5432)),
             "dbname": unquote(u.path.lstrip("/")) or "postgres",
             "user": user or q.get("user") or (unquote(u.username) if u.username else None),
             "password": password or q.get("password") or (unquote(u.password) if u.password else None),
             "sslmode": q.get("sslmode") or ("require" if q.get("ssl", "").lower() == "true" else "disable" if via_proxy else None)}

    def quote(v: str) -> str:
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return " ".join(f"{k}={quote(v)}" for k, v in parts.items() if v)


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
