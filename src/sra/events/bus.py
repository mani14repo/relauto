"""Event bus abstraction. Topics:

    sra.assessment.events   domain events (created, templates_selected, evidence_ingested, evaluated,
                            criterion_overridden, finding_updated, scored, report_generated, signed_off)
    sra.jobs                work queue: {"job": "run" | "report", "assessment_id": ..., ...}

Production: events are written to the Postgres outbox and relayed to Kafka (events/relay.py).
Development/tests: InMemoryBus records them; LocalJobs runs jobs in a thread pool.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Protocol

log = logging.getLogger(__name__)

EVENTS_TOPIC = "sra.assessment.events"
JOBS_TOPIC = "sra.jobs"


def envelope(event: str, assessment_id: str, data: dict | None = None, actor: str = "system") -> dict:
    return {"event": event, "assessment_id": assessment_id, "actor": actor,
            "at": datetime.now(timezone.utc).isoformat(), "data": data or {}}


class Bus(Protocol):
    def publish(self, topic: str, key: str, payload: dict) -> None: ...


class InMemoryBus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def publish(self, topic: str, key: str, payload: dict) -> None:
        with self._lock:
            self.messages.append((topic, key, json.loads(json.dumps(payload, default=str))))


class LocalJobs:
    """Runs jobs in-process when no Kafka is configured (dev / single replica)."""

    def __init__(self, handler: Callable[[dict], None], workers: int = 1) -> None:
        self.handler = handler
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sra-job")

    def submit(self, job: dict) -> None:
        def run():
            try:
                self.handler(job)
            except Exception:  # noqa: BLE001 - logged, job state is recorded by the handler
                log.exception("job failed: %s", job)
        self.pool.submit(run)
