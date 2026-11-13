"""Knowledge-store jobs, run by whichever process hosts the ingestion service: the Kafka worker, the background
thread of a single-process deployment, or `sra ingest`.

    {"job": "ingest"}                                   work through the documents waiting for ingestion
    {"job": "sync", "standards": true, "servicenow": true, "tables": [...]}
                                                        refresh the sources, then ingest what changed
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

KNOWLEDGE_JOBS = ("ingest", "sync")
_one_at_a_time = threading.Lock()      # in one process, ingestion runs serially; extra triggers just wait their turn


def run_job(store, job: dict) -> dict:
    """Run one knowledge job against a PgStore. Safe to repeat: registering and syncing are idempotent, and the
    ingestion queue is simply whatever is still waiting."""
    from ..retrieval.chunks import shared_embedder
    from .ingest import IngestionService
    knowledge = store.knowledge
    out: dict = {}
    with _one_at_a_time:
        if job.get("job") == "sync":
            if job.get("standards", True):
                from .standards import register_standards
                out["standards"] = register_standards(knowledge)
            if job.get("servicenow", True):
                from ..connectors.servicenow import ConnectorError, ServiceNowClient, sync
                try:
                    client = ServiceNowClient.from_settings()
                except ConnectorError as exc:
                    client, out["servicenow"] = None, {"status": "not configured", "detail": str(exc)}
                else:
                    if client is None:
                        out["servicenow"] = {"status": "not configured", "detail": "SERVICENOW_INSTANCE_URL is not set"}
                if client is not None:
                    try:
                        out["servicenow"] = sync(knowledge, client, job.get("tables"))
                    finally:
                        client.close()
        out["ingested"] = IngestionService(knowledge, shared_embedder()).run_pending()
    return out
