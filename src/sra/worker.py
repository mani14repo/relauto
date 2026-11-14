"""Kafka worker: relays the Postgres outbox to Kafka and consumes jobs from sra.jobs.

    sra worker      (KAFKA_BOOTSTRAP_SERVERS and DATABASE_URL required)

Jobs are idempotent: re-running an assessment recomputes automated results and keeps human decisions,
so at-least-once delivery is safe. Offsets are committed after the job completes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from .config import setting
from .events.bus import JOBS_TOPIC
from .knowledge.jobs import KNOWLEDGE_JOBS, run_job as run_knowledge_job
from .service import AssessmentService
from .store import store_from_settings

log = logging.getLogger("sra.worker")


def _evaluator_factory():
    from .llm.evaluate import Evaluator
    return Evaluator.from_settings()


async def relay_outbox(store, producer, stop: asyncio.Event, batch: int = 100, idle: float = 1.0) -> None:
    """Publish unpublished outbox rows in id order, then mark them (at-least-once)."""
    while not stop.is_set():
        with store.pool.connection() as c, c.transaction():
            rows = c.execute("SELECT id, topic, key, payload FROM sra_outbox WHERE published_at IS NULL ORDER BY id "
                             "LIMIT %s FOR UPDATE SKIP LOCKED", (batch,)).fetchall()
            for rid, topic, key, payload in rows:
                await producer.send_and_wait(topic, json.dumps(payload).encode(), key=key.encode())
                c.execute("UPDATE sra_outbox SET published_at = now() WHERE id = %s", (rid,))
        if not rows:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle)
            except asyncio.TimeoutError:
                pass


async def consume_jobs(svc: AssessmentService, consumer, stop: asyncio.Event) -> None:
    while not stop.is_set():
        batch = await consumer.getmany(timeout_ms=1000, max_records=1)
        for _, msgs in batch.items():
            for m in msgs:
                job = json.loads(m.value)
                log.info("job %s for %s", job.get("job"), job.get("assessment_id"))
                try:
                    if job.get("job") == "run":
                        # CPU-bound LLM work runs in a thread so the relay keeps flowing
                        await asyncio.to_thread(svc.run_job, job)
                    elif job.get("job") in KNOWLEDGE_JOBS:
                        # the ingestion service: extract, chunk, embed, store (and source sync)
                        await asyncio.to_thread(run_knowledge_job, svc.store, job)
                    elif job.get("job") == "report":
                        await asyncio.to_thread(svc.report_zip, job["assessment_id"], job.get("actor", "worker"))
                except Exception:  # noqa: BLE001
                    log.exception("job failed: %s", job)
                await consumer.commit()


async def main() -> None:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    logging.basicConfig(level=logging.INFO)
    bootstrap = setting("KAFKA_BOOTSTRAP_SERVERS")
    from .config import database_dsn
    if not bootstrap or not database_dsn():
        raise SystemExit("worker needs KAFKA_BOOTSTRAP_SERVERS and a database (DATABASE_URL or CAP_POSTGRES_*)")
    store = store_from_settings()
    if store.init_error:
        raise SystemExit(f"database unavailable: {store.init_error}")
    svc = AssessmentService(store, _evaluator_factory)
    security = {}
    if setting("KAFKA_SECURITY_PROTOCOL"):
        security = {"security_protocol": setting("KAFKA_SECURITY_PROTOCOL"), "sasl_mechanism": setting("KAFKA_SASL_MECHANISM"),
                    "sasl_plain_username": setting("KAFKA_USERNAME"), "sasl_plain_password": setting("KAFKA_PASSWORD")}
        security = {k: v for k, v in security.items() if v}
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True, **security)
    consumer = AIOKafkaConsumer(JOBS_TOPIC, bootstrap_servers=bootstrap, group_id=setting("KAFKA_GROUP", "sra-worker"),
                                enable_auto_commit=False, auto_offset_reset="earliest", max_poll_interval_ms=3_600_000, **security)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:   # Windows
            pass
    await producer.start()
    await consumer.start()
    try:
        await asyncio.gather(relay_outbox(store, producer, stop), consume_jobs(svc, consumer, stop))
    finally:
        await consumer.stop()
        await producer.stop()
