"""HTTP API (FastAPI) on :8080. Serves the React UI from web/dist when present.

Identity: IAP / ingress headers (X-Goog-Authenticated-User-Email, X-Forwarded-Email) or X-User for dev.
Long-running work (automated pass with the local LLM) is a job: Kafka topic sra.jobs when KAFKA_BOOTSTRAP_SERVERS
is set (consumed by `sra worker`), otherwise an in-process thread.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..catalog import load_playbooks, load_rubric, load_templates, load_titles
from ..events.bus import JOBS_TOPIC, LocalJobs
from ..scoring import load_matrix
from ..selection.profile import ApplicationProfile
from ..service import AssessmentService, Conflict
from ..store import store_from_settings

log = logging.getLogger("sra.api")
MAX_UPLOAD = int(config.setting("SRA_MAX_UPLOAD_MB", "50")) * 1024 * 1024


class CreateBody(BaseModel):
    profile: ApplicationProfile
    assessor: str = ""


def _evaluator_factory():
    from ..llm.evaluate import Evaluator
    return Evaluator.from_settings()


def create_app(store=None, run_jobs_inline: bool = False) -> FastAPI:
    app = FastAPI(title="Site Reliability Assessment Bot", version="0.1.0")
    store = store or store_from_settings()
    svc = AssessmentService(store, _evaluator_factory)
    jobs_state: dict[str, dict] = {}

    def handle_job(job: dict) -> None:
        aid = job["assessment_id"]
        jobs_state[aid] = {"state": "running", "job": job}
        try:
            out = svc.run(aid, use_llm=job.get("llm", False), actor=job.get("actor", "system"))
            jobs_state[aid] = {"state": "done", "rating": out["scorecard"]["rating"], "llm": out.get("llm")}
        except Exception as exc:  # noqa: BLE001
            log.exception("run failed")
            jobs_state[aid] = {"state": "failed", "error": str(exc)}

    kafka = bool(config.setting("KAFKA_BOOTSTRAP_SERVERS"))
    local_jobs = None if kafka else LocalJobs(handle_job)

    def actor(request: Request) -> str:
        for h in ("x-goog-authenticated-user-email", "x-forwarded-email", "x-user"):
            v = request.headers.get(h)
            if v:
                return v.split(":", 1)[-1]
        return "anonymous"

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

    # ---------------------------------------------------------------- health & catalog
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "templates": len(load_templates())}

    @app.get("/api/llm/health")
    def llm_health():
        from ..llm.client import from_settings
        c = from_settings()
        return {"model": c.model, "reachable": c.health()}

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
        return {"titles": load_titles(), "playbooks": {k: {"title": v.title, "steps": [s.model_dump() for s in v.steps]}
                                                      for k, v in load_playbooks().items()}}

    # ---------------------------------------------------------------- assessments
    def _cands(cands):
        return [{"template_id": c.template_id, "title": c.title, "selected": c.selected, "always": c.always,
                 "score": None if c.always else c.score, "reasons": c.reasons} for c in cands]

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
        return {"assessment": a.model_dump(mode="json"), "scorecard": store.scorecard(aid), "job": jobs_state.get(aid)}

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
    async def upload(aid: str, files: list[UploadFile] = File(...), hint: str | None = Form(None), who: str = Depends(actor)):
        get_or_404(aid)
        data = []
        for f in files:
            body = await f.read()
            if len(body) > MAX_UPLOAD:
                raise HTTPException(413, f"{f.filename} exceeds {MAX_UPLOAD // 1024 // 1024} MB")
            data.append((f.filename or "upload", body))
        return svc.add_evidence(aid, data, hint or None, who)

    @app.get("/api/assessments/{aid}/evidence")
    def evidence(aid: str):
        get_or_404(aid)
        rows = store.evidence(aid)
        b = store.bundle(aid)
        info = {it.id: {"kind": it.kind.value, "doc_type": it.doc_type.value if it.doc_type else None, "table": it.table,
                        "reviewed_at": it.reviewed_at.isoformat() if it.reviewed_at else None, "meta": {k: v for k, v in it.meta.items()
                                                                                                  if k != "raw_secret_scan"}}
                for it in b.items.values()}
        return {"files": [{**r, **info.get(r["id"], {})} for r in rows], "errors": b.errors, "summary": b.summary()}

    @app.post("/api/assessments/{aid}/run", status_code=202)
    def run(aid: str, llm: bool = Body(False, embed=True), who: str = Depends(actor)):
        get_or_404(aid)
        job = {"job": "run", "assessment_id": aid, "llm": llm, "actor": who}
        if run_jobs_inline:
            handle_job(job)
        elif kafka:
            store.bus.publish(JOBS_TOPIC, aid, job)
            jobs_state[aid] = {"state": "queued"}
        else:
            jobs_state[aid] = {"state": "queued"}
            local_jobs.submit(job)
        return jobs_state.get(aid, {"state": "queued"})

    @app.get("/api/assessments/{aid}/job")
    def job(aid: str):
        return jobs_state.get(aid) or {"state": "idle"}

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
        from ..llm.client import LLMError
        try:
            return {"answer": _evaluator_factory().chat(a, store.bundle(aid), cid, question)}
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

    dist = Path(config.setting("SRA_WEB_DIST", str(config.ROOT / "web" / "dist")))
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


app = None if config.setting("SRA_NO_APP") else create_app()
