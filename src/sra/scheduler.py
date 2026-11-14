"""In-process scheduler, as OIP runs its jobs (OIP docs: CAP_DEPLOYMENT_ARCHITECTURE.md section 4.1).

One daemon thread inside `sra serve` runs the knowledge-store ingestion (refresh the OIPReqs library baked into the
image, sync ServiceNow when it is configured, then ingest whatever is waiting) at the hours in SRA_INGEST_HOURS.
Jobs run one after another, never in parallel: a run that overruns makes the next one start late rather than overlap.
A failure is logged and never stops the scheduler or the web server. Hours are in the container clock (usually UTC).

    SRA_INGEST_HOURS       e.g. "2" or "2,14"; empty = no schedule (POST /api/knowledge/sync still works)
    SRA_RUN_ON_START       true: run the job once at start, before the schedule (a missed slot is not made up otherwise)
    SRA_INGEST_STANDARDS   false: do not refresh the OIPReqs library on schedule (default true)

Schedule state is in memory, so run ONE replica: with two, each would run every job against the same database.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from . import config

log = logging.getLogger("sra.scheduler")


def parse_hours(raw: str | None) -> list[int]:
    hours = sorted({int(h) for h in str(raw or "").replace(";", ",").split(",") if h.strip()})
    bad = [h for h in hours if not 0 <= h <= 23]
    if bad:
        raise ValueError(f"SRA_INGEST_HOURS must be hours 0-23, not {bad}")
    return hours


def next_run(now: datetime, hours: list[int]) -> datetime:
    """The next whole hour in `hours` strictly after `now`."""
    base = now.replace(minute=0, second=0, microsecond=0)
    for day in (0, 1):
        for h in hours:
            t = base.replace(hour=h) + timedelta(days=day)
            if t > now:
                return t
    raise ValueError("no hours to schedule")


def ingest_job() -> dict:
    return {"job": "sync", "standards": config.flag("SRA_INGEST_STANDARDS", True),
            "servicenow": bool(config.setting("SERVICENOW_INSTANCE_URL")), "tables": None}


class Scheduler:
    def __init__(self, run, hours: list[int], run_on_start: bool = False, clock=datetime.now) -> None:
        self.run, self.hours, self.run_on_start, self.clock = run, hours, run_on_start, clock
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def _run_once(self) -> None:
        job = ingest_job()
        log.info("scheduled job starting: %s", job)
        try:
            self.run(job)
            log.info("scheduled job finished")
        except Exception:  # noqa: BLE001 - logged; the scheduler and the web server keep running
            log.exception("scheduled job failed")

    def loop(self) -> None:
        if self.run_on_start:
            self._run_once()
        while self.hours and not self.stop.is_set():
            due = next_run(self.clock(), self.hours)
            log.info("next scheduled ingestion at %s", due.isoformat(timespec="minutes"))
            while not self.stop.is_set() and self.clock() < due:
                self.stop.wait(min(60.0, max(1.0, (due - self.clock()).total_seconds())))
            if not self.stop.is_set():
                self._run_once()

    def start(self) -> "Scheduler":
        self.thread = threading.Thread(target=self.loop, name="sra-scheduler", daemon=True)
        self.thread.start()
        return self


def start_from_settings(store, kafka: bool = False) -> Scheduler | None:
    """Start the scheduler for `sra serve`, or return None when nothing is scheduled."""
    hours = parse_hours(config.setting("SRA_INGEST_HOURS", ""))
    on_start = config.flag("SRA_RUN_ON_START", False)
    if not hours and not on_start:
        return None
    if not hasattr(store, "knowledge"):
        log.warning("SRA_INGEST_HOURS / SRA_RUN_ON_START are set, but the knowledge store needs PostgreSQL: not scheduling")
        return None
    if kafka:
        from .events.bus import JOBS_TOPIC

        def run(job: dict) -> None:            # the Kafka worker runs it
            store.bus.publish(JOBS_TOPIC, "knowledge", job)
    else:
        from .knowledge.jobs import run_job

        def run(job: dict) -> None:
            if not store.ready():
                raise RuntimeError(f"database unavailable: {store.init_error}")
            run_job(store, job)
    log.info("scheduler: ingestion at hours %s%s", hours or "-", " and once at start" if on_start else "")
    return Scheduler(run, hours, on_start).start()
