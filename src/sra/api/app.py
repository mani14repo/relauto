"""HTTP API (FastAPI) on :8080. Serves the React UI from web/dist when present.

Identity: IAP / ingress headers (X-Goog-Authenticated-User-Email, X-Forwarded-Email) or X-User for dev.
Long-running work (automated pass with the local LLM) is a job: Kafka topic sra.jobs when KAFKA_BOOTSTRAP_SERVERS
is set (consumed by `sra worker`), otherwise an in-process thread.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config
from ..catalog import load_playbooks, load_rubric, load_templates, load_titles
from ..events.bus import JOBS_TOPIC, LocalJobs
from ..knowledge.jobs import KNOWLEDGE_JOBS, run_job as run_knowledge_job
from ..scoring import load_matrix
from ..selection.profile import ApplicationProfile
from ..service import AssessmentService, Conflict
from ..store import store_from_settings

log = logging.getLogger("sra.api")
MAX_UPLOAD = int(config.setting("SRA_MAX_UPLOAD_MB", "50")) * 1024 * 1024


class CreateBody(BaseModel):
    profile: ApplicationProfile
    assessor: str = ""


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(6, ge=1, le=50)
    application: str | None = None          # only with /api/search
    kinds: list[str] | None = None          # document, servicenow_table, scan, k8s_manifest, ci_config, other
    doc_types: list[str] | None = None      # runbook, architecture, standard, playbook, policy, procedure, ...
    source_systems: list[str] | None = None  # upload, servicenow, oipreqs (only with /api/search)
    related: bool = True                    # include each result's linked documents


class SyncBody(BaseModel):
    standards: bool = True                  # the OIPReqs standards, policies, procedures, playbooks and templates
    servicenow: bool = True                 # needs SERVICENOW_INSTANCE_URL and credentials in the environment
    tables: list[str] | None = None         # ServiceNow tables to sync; default: all in config/sources.yaml


def _evaluator_factory():
    from ..llm.evaluate import Evaluator
    return Evaluator.from_settings()


def create_app(store=None, run_jobs_inline: bool = False) -> FastAPI:
    app = FastAPI(title="Site Reliability Assessment Bot", version="1.0")
    store = store or store_from_settings()
    svc = AssessmentService(store, _evaluator_factory)
    if not logging.getLogger().handlers:   # show job activity in the server console (uvicorn only configures its own loggers)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # A run with no progress for this long has probably died (server restart, worker crash): one model call can take
    # up to LLM_TIMEOUT seconds, so allow that plus a margin.
    stale_after = int(config.setting("LLM_TIMEOUT", "300")) + 120

    def handle_job(job: dict) -> None:
        if job.get("job") in KNOWLEDGE_JOBS:
            run_knowledge_job(store, job)
        else:
            svc.run_job(job)

    def job_view(aid: str) -> dict:
        j = store.get_job(aid)
        if not j:
            return {"state": "idle"}
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(j.get("updated_at"))).total_seconds()
        except (TypeError, ValueError):
            age = 0
        active = j.get("state") in ("queued", "running")
        return {**j, "seconds_since_update": int(age), "stale_after_s": stale_after,
                "stale": active and age > stale_after,
                "cancel_requested": active and store.cancel_requested(aid)}

    def _close_job(aid: str, j: dict, msg: str) -> None:
        """Mark a job cancelled from the API (used when no worker is running it)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store.set_job(aid, {**j, "state": "cancelled", "current": "", "eta_s": None, "finished_at": now, "updated_at": now,
                            "log": (j.get("log") or []) + [{"t": now, "level": "warn", "msg": msg}]})

    kafka = bool(config.setting("KAFKA_BOOTSTRAP_SERVERS"))
    local_jobs = None if kafka else LocalJobs(handle_job)
    # The ingestion service: with Kafka the worker runs it; otherwise its own background thread here, so a long
    # assessment run never holds up ingestion and an upload request never waits for chunking and embedding.
    local_ingest = None if kafka else LocalJobs(handle_job)

    def submit_knowledge_job(job: dict) -> None:
        if run_jobs_inline:
            handle_job(job)
        elif kafka:
            store.bus.publish(JOBS_TOPIC, "knowledge", job)
        else:
            local_ingest.submit(job)
    svc.submit_job = submit_knowledge_job

    def actor(request: Request) -> str:
        for h in ("x-goog-authenticated-user-email", "x-forwarded-email", "x-user"):
            v = request.headers.get(h)
            if v:
                return v.split(":", 1)[-1]
        return "anonymous"

    def principals(request: Request) -> list[str]:
        """Who is searching: the user and their groups, as passed on by the ingress (X-Groups: comma-separated).
        Retrieval returns a chunk only if its access list is empty or names one of these."""
        groups = [g.strip() for g in (request.headers.get("x-groups") or "").split(",") if g.strip()]
        return [actor(request), *groups]

    def get_or_404(aid: str):
        try:
            return store.get(aid)
        except KeyError:
            raise HTTPException(404, f"assessment {aid} not found")

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "templates": len(load_templates())}

    @app.get("/api/llm/health")
    def llm_health():
        if not config.llm_enabled():
            return {"enabled": False, "model": None, "reachable": False}
        from ..llm.client import from_settings
        c = from_settings()
        return {"enabled": True, "model": c.model, "reachable": c.health()}

    @app.get("/api/catalog/templates")
    def catalog_templates():
        return [{"id": t.id, "title": t.title, "domain": t.domain, "variant": t.variant, "priority": t.priority,
                 "applicability": t.applicability, "criteria": len(t.criteria)} for t in load_templates().values()]

    @app.get("/api/catalog/templates/{tid}")
    def catalog_template(tid: str):
        t = load_templates().get(tid)
        if not t:
            raise HTTPException(404)
        return t.model_dump(mode="json")

    @app.get("/api/catalog/rubric")
    def catalog_rubric():
        return {"rubric": load_rubric().model_dump(mode="json"), "matrix": load_matrix()}

    @app.get("/api/catalog/documents")
    def catalog_documents():
        return {
            "titles": load_titles(),
            "playbooks": {k: {"title": v.title, "steps": [s.model_dump() for s in v.steps]} for k, v in load_playbooks().items()}
        }

    def _cands(cands):
        return [{"template_id": c.template_id, "title": c.title, "selected": c.selected, "always": c.always,
                 "score": None if c.always else c.score, "reasons": c.reasons} for c in cands]

    @app.post("/api/profile/parse")
    async def parse_profile(file: UploadFile = File(...)):
        """Read an uploaded profile.yaml so the UI can prefill the form; nothing is stored."""
        body = await file.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise HTTPException(413, "a profile file should be a few KB; this one is over 256 KB")
        profile, ignored = ApplicationProfile.from_file(body)
        return {"profile": profile, "ignored": ignored, "filename": file.filename}

    @app.post("/api/assessments", status_code=201)
    def create(body: CreateBody, who: str = Depends(actor)):
        a, cands = svc.create(body.profile, body.assessor or who, who)
        return {"assessment": a.model_dump(mode="json"), "candidates": _cands(cands)}

    @app.get("/api/assessments")
    def list_assessments():
        return store.list()

    @app.get("/api/assessments/{aid}")
    def get_assessment(aid: str):
        a = get_or_404(aid)
        return {"assessment": a.model_dump(mode="json"), "scorecard": store.scorecard(aid), "job": job_view(aid)}

    def _refuse_while_running(aid: str, what: str) -> None:
        cur = job_view(aid)
        if cur["state"] in ("queued", "running") and not cur["stale"]:
            raise HTTPException(409, f"An automated pass is running; cancel it or wait for it to finish before you {what}")

    @app.patch("/api/assessments/{aid}")
    def edit(aid: str, patch: dict = Body(...), who: str = Depends(actor)):
        """Edit details: {"profile": {...partial ApplicationProfile...}, "assessment_date": "YYYY-MM-DD", "assessor": "..."}."""
        get_or_404(aid)
        _refuse_while_running(aid, "edit it")   # the run saves the whole assessment when it ends and would undo the edit
        a, rerun = svc.update_details(aid, patch, who)
        return {"assessment": a.model_dump(mode="json"), "rerun_needed": rerun}

    @app.delete("/api/assessments/{aid}")
    def delete(aid: str, who: str = Depends(actor)):
        """Permanently delete the assessment, its evidence files, findings, job, audit trail and feedback."""
        get_or_404(aid)
        _refuse_while_running(aid, "delete it")
        log.info("assessment %s deleted by %s", aid, who)
        return svc.delete(aid, who)

    @app.patch("/api/assessments/{aid}/profile")
    def patch_profile(aid: str, patch: dict = Body(...), who: str = Depends(actor)):
        get_or_404(aid)
        return svc.update_profile(aid, patch, who).model_dump(mode="json")

    @app.get("/api/assessments/{aid}/candidates")
    def candidates(aid: str):
        get_or_404(aid)
        profile, cands = svc.candidates(aid)
        return {"profile": profile.model_dump(mode="json"), "candidates": _cands(cands)}

    @app.put("/api/assessments/{aid}/templates")
    def put_templates(aid: str, templates: list[str] = Body(..., embed=True), who: str = Depends(actor)):
        get_or_404(aid)
        return svc.set_templates(aid, templates, who).model_dump(mode="json")

    @app.post("/api/assessments/{aid}/evidence", status_code=201)
    async def upload(aid: str, files: list[UploadFile] = File(...), hint: str | None = Form(None),
                     acl: str | None = Form(None), who: str = Depends(actor)):
        """acl: optional comma-separated users/groups allowed to read these files through search (default: everyone
        who can see the assessment)."""
        get_or_404(aid)
        data = []
        for f in files:
            body = await f.read()
            if len(body) > MAX_UPLOAD:
                raise HTTPException(413, f"{f.filename} exceeds {MAX_UPLOAD // 1024 // 1024} MB")
            data.append((f.filename or "upload", body))
        readers = [x.strip() for x in (acl or "").split(",") if x.strip()]
        return svc.add_evidence(aid, data, hint or None, who, readers or None)

    @app.post("/api/assessments/{aid}/search")
    def search_assessment(aid: str, body: SearchBody, who: list[str] = Depends(principals)):
        """Hybrid retrieval inside one assessment's evidence (keyword + vector + RRF, optional rerank)."""
        get_or_404(aid)
        return svc.search(body.query, aid, body.k, who, kinds=body.kinds, doc_types=body.doc_types, related=body.related)

    @app.post("/api/search")
    def search_all(body: SearchBody, who: list[str] = Depends(principals)):
        """Hybrid retrieval over the standing knowledge store, for other applications and agents (PostgreSQL only):
        uploaded evidence, synced ServiceNow records, and the OIPReqs standards and playbooks."""
        if svc.knowledge is None:
            raise HTTPException(501, "The knowledge store needs PostgreSQL")
        return svc.search(body.query, None, body.k, who, application=body.application, kinds=body.kinds,
                          doc_types=body.doc_types, source_systems=body.source_systems, related=body.related)

    def knowledge_or_501():
        if svc.knowledge is None:
            raise HTTPException(501, "The knowledge store needs PostgreSQL")
        return svc.knowledge

    @app.get("/api/knowledge/status")
    def knowledge_status():
        """What is in the knowledge store, what is waiting for ingestion, and where each connector stopped."""
        return knowledge_or_501().status()

    @app.post("/api/knowledge/sync", status_code=202)
    def knowledge_sync(body: SyncBody | None = None, who: str = Depends(actor)):
        """Refresh the sources (OIPReqs library, ServiceNow) and ingest what changed. Runs in the ingestion service."""
        knowledge_or_501()
        body = body or SyncBody()
        job = {"job": "sync", "standards": body.standards, "servicenow": body.servicenow, "tables": body.tables, "actor": who}
        svc.request_ingestion(job)
        return {"queued": True, "job": job}

    @app.get("/api/knowledge/documents/{did}")
    def knowledge_document(did: str, who: list[str] = Depends(principals)):
        """One document's metadata and the documents it is linked to (incident -> problem -> change, document -> service)."""
        k = knowledge_or_501()
        doc = k.get(did)
        if not doc or (doc["acl"] and not set(doc["acl"]) & set(who)):
            raise HTTPException(404, "document not found")
        return {**doc, "related": k.related([did], who, per_doc=50).get(did, [])}

    @app.get("/api/assessments/{aid}/evidence")
    def evidence(aid: str):
        get_or_404(aid)
        rows = store.evidence(aid)
        b = store.bundle(aid)
        info = {it.id: {"kind": it.kind.value, "doc_type": it.doc_type.value if it.doc_type else None, "table": it.table,
                        "reviewed_at": it.reviewed_at.isoformat() if it.reviewed_at else None, "meta": {k: v for k, v in it.meta.items()
                                                                                                        if k != "raw_secret_scan"}}
                for it in b.items.values()}
        mine = {r["id"] for r in rows}
        synced = [{"table": it.table, "rows": len(it.records), "source": it.name} for it in b.items.values() if it.id not in mine]
        return {"files": [{**r, **info.get(r["id"], {})} for r in rows], "errors": b.errors, "summary": b.summary(),
                "synced": synced, "notes": b.notes}

    @app.post("/api/assessments/{aid}/run", status_code=202)
    def run(aid: str, llm: bool = Body(False, embed=True), who: str = Depends(actor)):
        get_or_404(aid)
        cur = job_view(aid)
        if cur["state"] in ("queued", "running") and not cur["stale"]:
            raise HTTPException(409, "An automated pass is already running for this assessment")
        llm = llm and config.llm_enabled()   # with the LLM turned off every run is rules only
        job = {"job": "run", "assessment_id": aid, "llm": llm, "actor": who, "id": uuid.uuid4().hex[:12]}
        store.clear_cancel(aid)           # a cancel aimed at an earlier run must not stop this one
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        where = "the Kafka worker" if kafka else "this server"
        store.set_job(aid, {"state": "queued", "phase": "Queued", "step": 0, "total": 0, "current": "", "started_at": now,
                            "updated_at": now, "finished_at": None, "eta_s": None, "warnings": 0, "error": None,
                            "summary": None, "job": job,
                            "log": [{"t": now, "level": "info", "msg": f"Queued by {who}; waiting for {where} to pick it up"}]})
        if run_jobs_inline:
            handle_job(job)
        elif kafka:
            store.bus.publish(JOBS_TOPIC, aid, job)
        else:
            local_jobs.submit(job)
        return job_view(aid)

    @app.post("/api/assessments/{aid}/cancel", status_code=202)
    def cancel(aid: str, who: str = Depends(actor)):
        """Stop the automated pass. The worker stops at its next step or model token; nothing from the run is saved."""
        get_or_404(aid)
        cur = job_view(aid)
        if cur["state"] not in ("queued", "running"):
            raise HTTPException(409, "No automated pass is running for this assessment")
        log.info("cancel requested for %s by %s", aid, who)
        store.request_cancel(aid)
        raw = store.get_job(aid) or {}
        if cur["stale"]:
            # Nothing is running it any more (e.g. the server restarted); close the record now.
            _close_job(aid, raw, f"Cancelled by {who}. The run had stopped reporting; nothing from it was saved.")
            store.clear_cancel(aid)
        elif cur["state"] == "queued":
            # Not started yet: close it now; the flag stays so the worker skips it when it gets there.
            _close_job(aid, raw, f"Cancelled by {who} before it started.")
        return job_view(aid)

    @app.get("/api/assessments/{aid}/job")
    def job(aid: str):
        get_or_404(aid)
        return job_view(aid)

    @app.patch("/api/assessments/{aid}/criteria/{cid}")
    def patch_criterion(aid: str, cid: str, patch: dict = Body(...), who: str = Depends(actor)):
        get_or_404(aid)
        try:
            return svc.override_criterion(aid, cid, patch, who)
        except KeyError:
            raise HTTPException(404, f"criterion {cid} not in this assessment")

    @app.post("/api/assessments/{aid}/criteria/{cid}/confirm")
    def confirm(aid: str, cid: str, who: str = Depends(actor)):
        get_or_404(aid)
        return svc.confirm_criterion(aid, cid, who)

    @app.post("/api/assessments/{aid}/criteria/{cid}/chat")
    def chat(aid: str, cid: str, question: str = Body(..., embed=True)):
        a = get_or_404(aid)
        if not config.llm_enabled():
            raise HTTPException(409, "Criterion chat needs the local LLM, which is turned off (LLM_ENABLED=false)")
        from ..llm.client import LLMError
        try:
            ev = _evaluator_factory()
            ev.retriever = getattr(store, "retriever", None)
            return {"answer": ev.chat(a, store.bundle(aid), cid, question)}
        except LLMError as exc:
            raise HTTPException(503, f"local LLM unavailable: {exc}")

    @app.patch("/api/assessments/{aid}/findings/{fid}")
    def patch_finding(aid: str, fid: str, patch: dict = Body(...), who: str = Depends(actor)):
        get_or_404(aid)
        try:
            return svc.update_finding(aid, fid, patch, who)
        except KeyError:
            raise HTTPException(404, f"finding {fid} not found")

    @app.get("/api/assessments/{aid}/gate")
    def gate(aid: str, final: bool = False):
        get_or_404(aid)
        g = svc.gate(aid, final)
        return {"ok": g.ok, "blockers": g.blockers, "warnings": g.warnings}

    @app.post("/api/assessments/{aid}/signoffs")
    def signoff(aid: str, role: str = Body(...), name: str = Body(...), decision: str = Body(...), who: str = Depends(actor)):
        get_or_404(aid)
        return svc.sign_off(aid, role, name, decision, who).model_dump(mode="json")

    @app.get("/api/assessments/{aid}/audit")
    def audit(aid: str):
        get_or_404(aid)
        return store.audit_log(aid)

    @app.get("/api/assessments/{aid}/report")
    def report(aid: str, who: str = Depends(actor)):
        get_or_404(aid)
        name, data = svc.report_zip(aid, who)
        ascii_name = name.encode("ascii", "ignore").decode() or "report.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"'})

    dist = config.path_setting("SRA_WEB_DIST", config.ROOT / "web" / "dist")
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


app = create_app()
