"""ServiceNow connector: copies records into the standing knowledge store through the Table API, incrementally.

    GET /api/now/table/<table>?sysparm_query=...sys_updated_on>=<watermark>^ORDERBYsys_updated_on
                              &sysparm_display_value=all&sysparm_limit=..&sysparm_offset=..

Each record becomes one document (source id "<table>/<sys_id>") holding the redacted record. The ingestion service
then makes it searchable, and the assessment rules read the records of their application straight from the store, so
an assessment no longer needs exported files for the tables that are synced.

Configuration: config/sources.yaml. Connection and credentials: environment only (see that file). Nothing here has
been run against a real instance yet; field names and query scope are marked CONFIRM in the configuration.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import load_yaml, setting
from ..detectors.base import APP_FIELDS
from ..evidence.servicenow import DATE_FIELDS, normalize
from ..knowledge.store import SERVICENOW, KnowledgeStore, content_hash

log = logging.getLogger(__name__)

STAMP = "%Y-%m-%d %H:%M:%S"


class ConnectorError(RuntimeError):
    pass


class ServiceNowClient:
    def __init__(self, base_url: str, auth=None, headers: dict | None = None, timeout: float = 60, transport=None,
                 retries: int = 3, sleep=time.sleep):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), auth=auth, timeout=timeout, transport=transport,
                                 headers={"Accept": "application/json", **(headers or {})})
        self.retries, self.sleep = retries, sleep

    @classmethod
    def from_settings(cls, timeout: float = 60) -> "ServiceNowClient | None":
        """A client from the environment, or None when no instance is configured."""
        url = setting("SERVICENOW_INSTANCE_URL")
        if not url:
            return None
        token = setting("SERVICENOW_TOKEN")
        if token:
            return cls(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        user, password = setting("SERVICENOW_USER"), setting("SERVICENOW_PASSWORD")
        if not (user and password):
            raise ConnectorError("SERVICENOW_INSTANCE_URL is set but no credentials: set SERVICENOW_TOKEN, or "
                                 "SERVICENOW_USER and SERVICENOW_PASSWORD")
        return cls(url, auth=(user, password), timeout=timeout)

    def _get(self, path: str, params: dict) -> list[dict]:
        for attempt in range(self.retries + 1):
            try:
                r = self.http.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == self.retries:
                    raise ConnectorError(f"could not reach ServiceNow ({type(exc).__name__})") from exc
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.retries:
                    raise ConnectorError(f"ServiceNow answered {r.status_code} for {path} after {self.retries} retries")
                wait = r.headers.get("Retry-After", "")
                self.sleep(min(float(wait), 60) if wait.replace(".", "", 1).isdigit() else min(2 ** attempt, 30))
                continue
            if r.status_code in (401, 403):
                raise ConnectorError(f"ServiceNow refused the credentials or the table ({r.status_code} for {path})")
            if r.status_code == 404:
                raise ConnectorError(f"table not found or not readable: {path}")
            if r.status_code >= 400:
                raise ConnectorError(f"ServiceNow answered {r.status_code} for {path}: {r.text[:200]}")
            body = r.json()
            return body.get("result", []) if isinstance(body, dict) else []
        return []

    def fetch(self, table: str, query: str = "", since: str | None = None, page_size: int = 500, max_records: int = 20000):
        """Records updated at or after `since` (UTC 'YYYY-MM-DD HH:MM:SS'), oldest first, page by page."""
        parts = [p for p in (query, f"sys_updated_on>={since}" if since else "") if p]
        encoded = "^".join(parts) + "^ORDERBYsys_updated_on" if parts else "ORDERBYsys_updated_on"
        offset = 0
        while offset < max_records:
            limit = min(page_size, max_records - offset)
            rows = self._get(f"/api/now/table/{table}", {
                "sysparm_query": encoded, "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true",
                "sysparm_limit": limit, "sysparm_offset": offset})
            yield from rows[:limit]              # never more than asked for, whatever the server returns
            if len(rows) < limit:
                return
            offset += len(rows)

    def close(self) -> None:
        self.http.close()


# ------------------------------------------------------------------------------------------------ record -> document
def _both(value) -> tuple[str, str]:
    """(display value, stored value) of a Table API field fetched with sysparm_display_value=all."""
    if isinstance(value, dict):
        return str(value.get("display_value") or ""), str(value.get("value") or "")
    return ("" if value is None else str(value)),  ("" if value is None else str(value))


def flatten(raw: dict) -> tuple[dict, dict]:
    """(readable record, stored values). Dates keep their stored value (UTC); everything else its display value, which
    is what an export shows: 'Closed', '1 - Critical', a group's name."""
    flat, stored = {}, {}
    for k, v in raw.items():
        display, value = _both(v)
        flat[k] = value if k in DATE_FIELDS else (display or value)
        stored[k] = value
    return flat, stored


def storable(flat: dict) -> dict:
    """The record as it is kept: redacted and normalised exactly as an uploaded export would be (people fields
    dropped, secrets and personal data blanked), in a form JSON can hold."""
    row = normalize(flat)
    return {k: v for k, v in row.items() if not isinstance(v, datetime) and v not in (None, "")}


def restore(record: dict) -> dict:
    """A stored record back as a rule-ready row (parsed dates and the derived underscore fields)."""
    row = normalize({k: v for k, v in record.items() if not k.startswith("_")})
    row.update({k: v for k, v in record.items() if k.startswith("_")})
    return row


def _settings(cfg: dict, table: str) -> dict:
    return {**(cfg.get("defaults") or {}), **((cfg.get("tables") or {}).get(table) or {})}


def to_document(table: str, raw: dict, tcfg: dict) -> dict | None:
    flat, stored = flatten(raw)
    sys_id = stored.get("sys_id")
    if not sys_id:
        return None
    record = storable(flat)
    record["sys_id"] = sys_id
    app = next((record[f] for f in tcfg.get("application_fields") or [] if record.get(f)), "")
    scope = " | ".join(str(record.get(f, "")) for f in dict.fromkeys([*APP_FIELDS, *(tcfg.get("application_fields") or [])]) if record.get(f))
    acl = sorted({p.strip() for f in tcfg.get("acl_fields") or [] for p in str(flat.get(f) or "").split(",") if p.strip()})
    links = [[field, f"{target}/{stored[field]}"] for field, target in (tcfg.get("references") or {}).items() if stored.get(field)]
    link = tcfg.get("link")
    source_id = f"{table}/{sys_id}"
    ci_link = None
    if link and stored.get(link["from"]) and stored.get(link["to"]):
        # A relationship row (cmdb_rel_ci): it points at both items, and the ingestion service also records the
        # link itself between the two configuration items (parent -> child, typed "Depends on::Used by" etc.).
        links += [["parent", f"{link['table']}/{stored[link['from']]}"], ["child", f"{link['table']}/{stored[link['to']]}"]]
        ci_link = {"from": f"{link['table']}/{stored[link['from']]}", "to": f"{link['table']}/{stored[link['to']]}",
                   "type": flat.get(link.get("type_field", "type")) or "related"}
    updated = None
    if stored.get("sys_updated_on"):
        try:
            updated = datetime.strptime(stored["sys_updated_on"], STAMP).replace(tzinfo=timezone.utc)
        except ValueError:
            updated = None
    return {"source_id": source_id, "title": str(record.get("number") or record.get("name") or sys_id), "record": record,
            "application": str(app), "scope_text": scope, "acl": acl, "links": links, "updated": updated,
            "watermark": stored.get("sys_updated_on") or "", "ci_link": ci_link}


# ------------------------------------------------------------------------------------------------ sync
def sync(knowledge: KnowledgeStore, client: ServiceNowClient, tables: list[str] | None = None, cfg: dict | None = None,
         now: datetime | None = None) -> dict:
    """Copy new and changed records of each configured table into the store. Returns {table: {...}}.

    A table that fails is reported and the others continue; its watermark does not move, so the next run retries.
    """
    cfg = cfg or (load_yaml("sources").get("servicenow") or {})
    now = now or datetime.now(timezone.utc)
    report = {}
    for table in tables or list(cfg.get("tables") or {}):
        tcfg = _settings(cfg, table)
        since = knowledge.watermark(SERVICENOW, table)
        if since is None:
            days = tcfg.get("lookback_days", cfg.get("lookback_days", 365))
            since = (now - timedelta(days=days)).strftime(STAMP) if days else None
        seen = queued = 0
        newest = since or ""
        try:
            batch: list[dict] = []
            for raw in client.fetch(table, tcfg.get("query") or "", since, int(cfg.get("page_size", 500)),
                                    int(cfg.get("max_records_per_table", 20000))):
                doc = to_document(table, raw, tcfg)
                if doc:
                    batch.append(doc)
                if len(batch) >= 200:
                    queued += _store(knowledge, table, batch)
                    seen += len(batch)
                    newest = max(newest, max(d["watermark"] for d in batch))
                    batch = []
            if batch:
                queued += _store(knowledge, table, batch)
                seen += len(batch)
                newest = max(newest, max(d["watermark"] for d in batch))
            knowledge.set_sync(SERVICENOW, table, watermark=newest or None, status="ok", records=seen)
            report[table] = {"status": "ok", "records": seen, "queued": queued, "watermark": newest or None}
        except ConnectorError as exc:
            log.warning("ServiceNow sync of %s failed: %s", table, exc)
            knowledge.set_sync(SERVICENOW, table, watermark=None, status="failed", records=seen, error=str(exc))
            report[table] = {"status": "failed", "records": seen, "queued": queued, "error": str(exc)}
    return report


def _store(knowledge: KnowledgeStore, table: str, docs: list[dict]) -> int:
    queued = 0
    with knowledge.pool.connection() as c, c.transaction():
        for d in docs:
            links = d["links"]
            _, changed = knowledge.upsert(
                c, source_system=SERVICENOW, source_id=d["source_id"], title=d["title"], kind="record", table_name=table,
                content_hash=content_hash([d["record"], links]), application=d["application"], scope_text=d["scope_text"],
                acl=d["acl"], record=d["record"], meta={"links": links, "ci_link": d["ci_link"]},
                source_updated_at=d["updated"], tags=[table])
            queued += changed
    return queued
