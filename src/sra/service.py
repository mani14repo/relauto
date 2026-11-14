"""Application service: the operations behind the API and the Kafka worker.

Every state change is saved together with its domain events and an audit row. Re-running the automated
pass keeps human decisions (overrides, N/A approvals, finding dispositions) intact.
"""
from __future__ import annotations

import datetime as dt
import logging
import io
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from . import config
from .catalog import load_rubric, load_templates
from .catalog.compiler import template_path
from .catalog.models import Result, Risk
from .events.bus import envelope
from .models import ApplicationRef, Assessment, CriterionResult, FindingStatus, SignOff, Source
from .progress import JobCancelled, NullProgress, Progress
from .pipeline import completeness_gate, propose_templates, rescore, run_assessment
from .report.docx_fill import fill_template
from .report.summary import build_summary
from .scoring import load_matrix
from .selection.profile import ApplicationProfile
from .store import Store

log = logging.getLogger("sra.service")


class Conflict(Exception):
    pass


class AssessmentService:
    def __init__(self, store: Store, evaluator_factory=None, submit_job=None) -> None:
        self.store = store
        self.evaluator_factory = evaluator_factory
        # How knowledge jobs ("ingest", "sync") reach the ingestion service: set by the API (Kafka or a background
        # thread). None = nothing is queued; ingestion then happens when an assessment's evidence is first needed.
        self.submit_job = submit_job

    @property
    def knowledge(self):
        """The standing knowledge store, or None without PostgreSQL."""
        return getattr(self.store, "knowledge", None)

    def request_ingestion(self, job: dict | None = None) -> bool:
        if self.knowledge is None or self.submit_job is None:
            return False
        self.submit_job(job or {"job": "ingest"})
        return True

    # ---------------------------------------------------------------- lifecycle
    def create(self, profile: ApplicationProfile, assessor: str = "", actor: str = "system") -> tuple[Assessment, list]:
        _, cands = propose_templates(profile)
        templates = load_templates()
        a = Assessment(application=ApplicationRef(name=profile.name, cmdb_id=profile.cmdb_id), assessor=assessor,
                       profile=profile.model_dump(mode="json"), templates=[c.template_id for c in cands if c.selected])
        a.signoffs = [SignOff(role=r) for r in templates["SRE-AT-01"].signoff_roles]
        self.store.save(a, events=[envelope("created", a.id, {"application": profile.name}, actor)],
                        audit=[{"actor": actor, "action": "create", "after": {"templates": a.templates}}])
        return a, cands

    def profile(self, a: Assessment) -> ApplicationProfile:
        return ApplicationProfile.model_validate(a.profile)

    def candidates(self, aid: str):
        a = self.store.get(aid)
        profile, cands = propose_templates(self.profile(a), self.store.bundle(aid), datetime.combine(a.assessment_date, dt.time(12)))
        return profile, cands

    def update_profile(self, aid: str, patch: dict, actor: str) -> Assessment:
        a = self.store.get(aid)
        before = dict(a.profile)
        a.profile = ApplicationProfile.model_validate({**a.profile, **patch}).model_dump(mode="json")
        self.store.save(a, events=[envelope("profile_updated", aid, patch, actor)],
                        audit=[{"actor": actor, "action": "update_profile", "before": before, "after": a.profile}])
        return a

    def update_details(self, aid: str, patch: dict, actor: str) -> tuple[Assessment, bool]:
        """Edit the application profile (name, CMDB id, tier, characteristics...), assessment date and assessor.

        Returns (assessment, rerun_needed). Results are kept as they are; when the profile or date changed and the
        assessment already has automated results, rerun_needed tells the UI they no longer reflect the details.
        """
        a = self.store.get(aid)
        if a.status == "signed_off":
            raise Conflict("this assessment is signed off; its details can no longer be changed")
        unknown = set(patch) - {"profile", "assessment_date", "assessor"}
        if unknown:
            raise ValueError(f"cannot edit: {', '.join(sorted(unknown))}")
        before = {"profile": dict(a.profile), "assessment_date": a.assessment_date.isoformat(), "assessor": a.assessor}
        if "profile" in patch:
            p = ApplicationProfile.model_validate({**a.profile, **(patch["profile"] or {})})
            if not p.name.strip():
                raise ValueError("the application name cannot be empty")
            a.profile = p.model_dump(mode="json")
            a.application = ApplicationRef(name=p.name, cmdb_id=p.cmdb_id)
        if "assessment_date" in patch:
            a.assessment_date = dt.date.fromisoformat(str(patch["assessment_date"]))
        if "assessor" in patch:
            a.assessor = str(patch["assessor"] or "").strip()
        after = {"profile": a.profile, "assessment_date": a.assessment_date.isoformat(), "assessor": a.assessor}
        changed = [k for k in after if after[k] != before[k]]
        if not changed:
            return a, False
        automated = any(r.source != Source.HUMAN for r in a.results.values()) or bool(a.findings)
        rerun = automated and bool({"profile", "assessment_date"} & set(changed))
        self.store.save(a, events=[envelope("updated", aid, {"changed": changed}, actor)],
                        audit=[{"actor": actor, "action": "update_details", "target": ", ".join(changed),
                                "before": {k: before[k] for k in changed}, "after": {k: after[k] for k in changed}}])
        return a, rerun

    def delete(self, aid: str, actor: str) -> dict:
        """Permanently delete the assessment and everything stored for it (see Store.delete).

        SRE decisions on criteria are kept as training data unless SRA_KEEP_FEEDBACK=false. Each keeps the evidence
        passages a model would have been shown for that criterion, since the evidence itself is deleted.
        """
        rows = None
        if config.flag("SRA_KEEP_FEEDBACK", True):
            rows = self.store.feedback_rows(aid)
            if rows:
                rows = self._with_excerpts(aid, rows)
        return self.store.delete(aid, actor, keep_feedback=rows)

    def _with_excerpts(self, aid: str, rows: list[dict]) -> list[dict]:
        from .llm.evaluate import query_for
        from .retrieval.chunks import cached_index, focus
        a = self.store.get(aid)
        by_id = {c.id: (t, c) for t, tpl in load_templates().items() for c in tpl.criteria}
        idx = cached_index(self.store.bundle(aid, datetime.combine(a.assessment_date, dt.time(12))))
        out = []
        for r in rows:
            t, c = by_id.get(r["criterion_id"], (None, None))
            excerpts = []
            if c is not None:
                q = query_for(c)
                excerpts = [{"evidence": ch.source, "id": ch.id, "text": focus(ch.text, q, 800)} for ch in idx.search(q, k=4)]
            out.append({**r, "application": a.application.name, "template": t, "excerpts": excerpts})
        return out

    def set_templates(self, aid: str, template_ids: list[str], actor: str) -> Assessment:
        known = load_templates()
        bad = [t for t in template_ids if t not in known]
        if bad:
            raise ValueError(f"unknown templates: {bad}")
        a = self.store.get(aid)
        before = a.templates
        a.templates = list(dict.fromkeys(template_ids))
        self.store.save(a, events=[envelope("templates_selected", aid, {"templates": a.templates}, actor)],
                        audit=[{"actor": actor, "action": "set_templates", "before": before, "after": a.templates}])
        return a

    def add_evidence(self, aid: str, files: list[tuple[str, bytes]], hint: str | None, actor: str,
                     acl: list[str] | None = None) -> list[dict]:
        a = self.store.get(aid)
        rows = [self.store.add_evidence(aid, name, data, hint, acl) for name, data in files]
        self.request_ingestion()            # chunking and embedding happen in the ingestion service, not here
        self.store.save(a, events=[envelope("evidence_ingested", aid, {"files": [r["name"] for r in rows]}, actor)],
                        audit=[{"actor": actor, "action": "add_evidence", "after": [r["name"] for r in rows]}])
        return rows

    def scan_github(self, aid: str, repo: str, ref: str | None, host: str | None, actor: str,
                    alerts: bool = True, signals: bool = True, code: bool = True, acl: list[str] | None = None,
                    progress: Progress | None = None) -> dict:
        """Scan a GitHub repository with the gh CLI and add what it found as evidence, stored like uploaded files
        (connectors/github.py). Raises connectors.github.GitHubError (bad input, gh failures) or GhUnavailable."""
        from .connectors.github import scan
        a = self.store.get(aid)
        progress = progress or NullProgress()
        res = scan(repo, ref, host, alerts=alerts, signals=signals, code=code, progress=progress)
        progress.phase("Storing evidence", total=len(res.files), msg=f"Adding {len(res.files)} evidence file(s)")
        rows = []
        for n, f in enumerate(res.files, 1):     # nothing is stored before this point, so a cancel adds nothing
            rows.append(self.store.add_evidence(aid, f.name, f.data, None, acl))
            progress.items_done(n, f.name.rsplit("/", 1)[-1])
        if rows:
            self.request_ingestion()
        summary = res.summary()
        self.store.save(a, events=[envelope("evidence_ingested", aid, {"files": [r["name"] for r in rows],
                                                                        "source": "github", "repository": res.repository,
                                                                        "sha": res.sha}, actor)],
                        audit=[{"actor": actor, "action": "github_scan", "target": f"{res.host}/{res.repository}@{res.sha}",
                                "after": {**summary, "evidence": [r["id"] for r in rows]}}])
        return {**summary, "evidence": rows}

    def search(self, query: str, aid: str | None = None, k: int = 6, principals: list[str] | None = None,
               application: str | None = None, kinds: list[str] | None = None, doc_types: list[str] | None = None,
               source_systems: list[str] | None = None, related: bool = True) -> dict:
        """Hybrid retrieval: keyword + vector search fused with RRF, optional rerank, then the access check; every
        result carries its source reference and, from the knowledge store, the documents it is linked to.

        With `aid`: that assessment's uploaded files and the standing records about its application.
        Without: the whole knowledge store (PostgreSQL only): uploads, synced ServiceNow records, the OIPReqs
        standards and playbooks; narrow it with application / kinds / doc_types / source_systems.
        `principals` are the caller's user name, groups and roles; None skips the access check (trusted internal use)."""
        from .retrieval.chunks import cached_index, shared_embedder
        embedder = shared_embedder()
        links: dict = {}
        if hasattr(self.store, "retriever"):
            idx = self.store.retriever(aid, embedder, principals, application=application, kinds=kinds, doc_types=doc_types,
                                       source_systems=source_systems)
            chunks = idx.search(query, k=k)
            info = idx.last
            if related:
                links = self.knowledge.related(list(dict.fromkeys(c.document_id for c in chunks)), principals)
        elif aid:
            # no database: the in-memory index over this assessment's files, with the same filters applied here
            a = self.store.get(aid)
            acl = {r["id"]: r.get("acl") or [] for r in self.store.evidence(aid)}
            idx = cached_index(self.store.bundle(aid, datetime.combine(a.assessment_date, dt.time(12))), embedder)

            def allowed(c) -> bool:
                return ((principals is None or not acl.get(c.evidence_id) or set(acl[c.evidence_id]) & set(principals))
                        and (not kinds or c.kind in kinds) and (not doc_types or c.doc_type in doc_types))
            chunks = [c for c in idx.search(query, k=max(k * 5, 50)) if allowed(c)][:k]
            for c in chunks:
                c.assessment_id, c.application = aid, a.application.name
            info = {"mode": "hybrid (keyword + vector, RRF)" if idx.vectors is not None else "keyword only",
                    "reranked": idx.reranker is not None, "keyword_rank": {}, "vector_rank": {}}
        else:
            raise ValueError("searching the knowledge store needs PostgreSQL (set DATABASE_URL or CAP_POSTGRES_*)")
        return {"query": query, "mode": info.get("mode"), "reranked": bool(info.get("reranked")),
                "results": [{"rank": n, "chunk_id": c.id, "document_id": c.document_id or None, "source_system": c.source_system,
                             "assessment_id": c.assessment_id or None, "application": c.application,
                             "evidence_id": c.evidence_id, "source": c.source, "kind": c.kind, "doc_type": c.doc_type,
                             "keyword_rank": info["keyword_rank"].get((c.document_id, c.id)),
                             "vector_rank": info["vector_rank"].get((c.document_id, c.id)), "text": c.text,
                             "related": links.get(c.document_id, [])}
                            for n, c in enumerate(chunks, 1)]}

    def run(self, aid: str, use_llm: bool = False, actor: str = "system", progress: Progress | None = None) -> dict:
        progress = progress or NullProgress()
        a = self.store.get(aid)
        as_of = datetime.combine(a.assessment_date, dt.time(12))
        progress.phase("Loading evidence", msg="Loading and redacting the uploaded evidence")
        bundle = self.store.bundle(aid, as_of)
        progress.info(f"{len(bundle.items)} evidence files loaded")
        keep_results = {cid: r for cid, r in a.results.items() if r.source == Source.HUMAN}
        keep_status = {(f.detector, f.title): (f.status, f.recommendation) for f in a.findings if f.status != FindingStatus.OPEN}
        a.results, a.rubric_direct, a.findings, a.detector_runs = dict(keep_results), {}, [], {}
        a.status = "evaluating"
        evaluator = None
        if use_llm and not config.llm_enabled():
            progress.info("The local LLM is turned off (LLM_ENABLED=false); running rules only")
            use_llm = False
        if use_llm and self.evaluator_factory:
            progress.busy("Starting the local LLM client and loading the embedding model")
            evaluator = self.evaluator_factory()
            evaluator.retriever = getattr(self.store, "retriever", None)   # Postgres hybrid retrieval when available
        sc, gate = run_assessment(a, bundle, self.profile(a), as_of, evaluator, progress)
        progress.phase("Saving", msg="Saving results")
        for f in a.findings:
            prev = keep_status.get((f.detector, f.title))
            if prev:
                f.status = prev[0]
                if prev[1] and prev[1].source == Source.HUMAN:
                    f.recommendation = prev[1]
        a.status = "in_review"
        stats = getattr(evaluator, "stats", None)
        self.store.save(a, sc, events=[envelope("evaluated", aid, {"rating": sc.rating, "index": sc.index, "llm": stats}, actor),
                                       envelope("scored", aid, {"rating": sc.rating, "p1": sc.p1_gaps, "p2": sc.p2_gaps}, actor)]
                        + [envelope("finding_raised", aid, {"id": f.id, "type": f.type.value, "severity": f.severity.value,
                                                            "title": f.title}, actor) for f in a.findings],
                        audit=[{"actor": actor, "action": "run", "after": {"rating": sc.rating, "llm": bool(evaluator)}}])
        return {"scorecard": sc.model_dump(mode="json"), "gate": gate.__dict__, "llm": stats}

    def run_job(self, job: dict) -> dict | None:
        """Run a queued 'run' job, recording live progress where the UI can read it (store.set_job)."""
        aid, llm, who = job["assessment_id"], job.get("llm", False), job.get("actor", "system")
        queued = self.store.get_job(aid) or {}
        current_id = (queued.get("job") or {}).get("id")
        if job.get("id") and current_id and current_id != job["id"]:
            log.info("skipping superseded job %s for %s (current run is %s)", job["id"], aid, current_id)
            return None
        progress = Progress(sink=lambda s: self.store.set_job(aid, s), job=job, label=aid, history=queued.get("log"),
                            cancelled=lambda: self.store.cancel_requested(aid))
        try:
            progress.check()             # cancelled while still queued
            progress.info(f"Automated pass started by {who} " + ("with the local LLM" if llm else "(rules only, no LLM)"))
            out = self.run(aid, use_llm=llm, actor=who, progress=progress)
        except JobCancelled:
            progress.cancelled("Cancelled. Nothing from this run was saved; the assessment keeps its previous results.")
            self.store.clear_cancel(aid)
            return None
        except Exception as exc:  # noqa: BLE001 - reported to the user through the job record
            log.exception("run failed for %s", aid)
            progress.failed(f"{type(exc).__name__}: {exc}")
            return None
        sc, stats = out["scorecard"], out.get("llm")
        progress.done({"rating": sc["rating"], "index": sc["index"], "llm": stats},
                      f"Finished: rated {sc['rating']} (index {sc['index']})")
        return out

    def scan_github_job(self, job: dict) -> dict | None:
        """Run a queued 'github_scan' job with live progress in the assessment's job record (as runs have)."""
        from .connectors.github import GitHubError
        aid, who = job["assessment_id"], job.get("actor", "system")
        queued = self.store.get_job(aid) or {}
        current_id = (queued.get("job") or {}).get("id")
        if job.get("id") and current_id and current_id != job["id"]:
            log.info("skipping superseded job %s for %s (current job is %s)", job["id"], aid, current_id)
            return None
        progress = Progress(sink=lambda s: self.store.set_job(aid, s), job=job, label=aid, history=queued.get("log"),
                            cancelled=lambda: self.store.cancel_requested(aid))
        try:
            progress.check()
            progress.info(f"Repository scan of {job['repository']} started by {who}")
            out = self.scan_github(aid, job["repository"], job.get("ref"), job.get("host"), who, alerts=job.get("alerts", True),
                                   signals=job.get("signals", True), code=job.get("code", True), acl=job.get("acl"),
                                   progress=progress)
        except JobCancelled:
            progress.cancelled("Cancelled. No evidence from this scan was added.")
            self.store.clear_cancel(aid)
            return None
        except GitHubError as exc:
            progress.failed(str(exc), what="Scan")
            return None
        except Exception as exc:  # noqa: BLE001 - reported to the user through the job record
            log.exception("repository scan failed for %s", aid)
            progress.failed(f"{type(exc).__name__}: {exc}", what="Scan")
            return None
        scan = {k: v for k, v in out.items() if k != "evidence"} | {"evidence_added": len(out["evidence"])}
        for n in out.get("notes") or []:
            progress.info(n)
        progress.done({"scan": scan}, f"Finished: {len(out['evidence'])} evidence file(s) from {out['repository']}@{out['sha'][:12]}")
        return out

    # ---------------------------------------------------------------- review
    def override_criterion(self, aid: str, cid: str, patch: dict, actor: str) -> dict:
        a = self.store.get(aid)
        ids = {c.id for t in a.templates for c in load_templates()[t].criteria}
        if cid not in ids:
            raise KeyError(cid)
        cur = a.results.get(cid) or CriterionResult(criterion_id=cid)
        before = cur.model_dump(mode="json")
        new = cur.model_copy(deep=True)
        for k in ("rationale", "recommendation", "na_justification", "validation", "measured_value"):
            if k in patch:
                setattr(new, k, patch[k])
        if "maturity_level" in patch:
            lv = patch["maturity_level"]
            if lv not in (None, "", 0, 1, 2, 3, 4):
                raise ValueError("maturity_level must be 0-4 or empty")
            new.maturity_level = None if lv in (None, "") else int(lv)
        if "result" in patch:
            new.result = Result(patch["result"]) if patch["result"] else None
        if "risk" in patch:
            new.risk = Risk(patch["risk"]) if patch["risk"] else None
        if "na_approved" in patch:
            new.na_approved = bool(patch["na_approved"])
        if new.result == Result.NA and not new.na_justification:
            raise ValueError("Not Applicable requires a documented justification")
        new.source, new.reviewer, new.needs_human_review = Source.HUMAN, actor, False
        if patch.get("evidence_refs"):
            new.evidence_refs = list(dict.fromkeys(new.evidence_refs + list(patch["evidence_refs"])))
        a.results[cid] = new
        self._record_decision(aid, cid, before, new)
        sc = rescore(a)
        self.store.save(a, sc, events=[envelope("criterion_overridden", aid, {"criterion": cid, "result": new.result}, actor)],
                        audit=[{"actor": actor, "action": "override_criterion", "target": cid, "before": before,
                                "after": new.model_dump(mode="json")}])
        return {"result": new.model_dump(mode="json"), "scorecard": sc.model_dump(mode="json")}

    def _record_decision(self, aid: str, cid: str, before: dict, new: CriterionResult) -> None:
        """Every SRE decision is a training label: agreement with the automated result counts as much as a correction."""
        new_result = new.result.value if new.result else None
        if not new_result:
            return
        automated = before.get("source") in ("llm", "rule", "derived") and before.get("result")
        if automated:
            kind, ai = ("confirm" if before["result"] == new_result else "override"), before
        elif before.get("source") == "human":
            if before.get("result") == new_result:
                return                    # wording edits to one's own decision are not a new label
            kind, ai = "revise", None
        else:
            kind, ai = "fill", None
        self.store.feedback(aid, cid, ai, new.model_dump(mode="json"), kind)

    def confirm_criterion(self, aid: str, cid: str, actor: str) -> dict:
        """Reviewer accepts the automated result as-is."""
        a = self.store.get(aid)
        r = a.results.get(cid)
        if not r or r.result is None:
            raise ValueError(f"{cid} has no result to confirm")
        return self.override_criterion(aid, cid, {"result": r.result.value}, actor)

    def update_finding(self, aid: str, fid: str, patch: dict, actor: str) -> dict:
        a = self.store.get(aid)
        f = next((f for f in a.findings if f.id == fid), None)
        if f is None:
            raise KeyError(fid)
        before = f.model_dump(mode="json")
        if "status" in patch:
            f.status = FindingStatus(patch["status"])
            if f.status == FindingStatus.DISPUTED and not patch.get("reason"):
                raise ValueError("disputing a finding requires a reason")
            if patch.get("reason"):
                f.detail = (f.detail + f"\nReviewer note ({actor}): {patch['reason']}").strip()
        if "severity" in patch:
            f.severity = Risk(patch["severity"])
        rec = patch.get("recommendation")
        if rec and f.recommendation:
            for k in ("action", "owner"):
                if k in rec:
                    setattr(f.recommendation, k, rec[k])
            if rec.get("due_date"):
                f.recommendation.due_date = dt.date.fromisoformat(rec["due_date"])
            f.recommendation.source = Source.HUMAN
        self.store.save(a, events=[envelope("finding_updated", aid, {"id": fid, **{k: v for k, v in patch.items() if k != "recommendation"}}, actor)],
                        audit=[{"actor": actor, "action": "update_finding", "target": fid, "before": before,
                                "after": f.model_dump(mode="json")}])
        return f.model_dump(mode="json")

    def sign_off(self, aid: str, role: str, name: str, decision: str, actor: str) -> Assessment:
        a = self.store.get(aid)
        gate = completeness_gate(a, None, final=False)
        if gate.blockers:
            raise Conflict(f"cannot sign off: {len(gate.blockers)} blockers, e.g. {gate.blockers[0]}")
        s = next((s for s in a.signoffs if s.role == role), None)
        if s is None:
            s = SignOff(role=role)
            a.signoffs.append(s)
        s.name, s.decision, s.date = name, decision, dt.date.today()
        if all(x.name for x in a.signoffs):
            a.status = "signed_off"
        self.store.save(a, events=[envelope("signed_off", aid, {"role": role, "decision": decision}, actor)],
                        audit=[{"actor": actor, "action": "sign_off", "target": role, "after": {"name": name, "decision": decision}}])
        return a

    def gate(self, aid: str, final: bool = False):
        a = self.store.get(aid)
        return completeness_gate(a, self.store.bundle(aid) if not final else None, final=final)

    # ---------------------------------------------------------------- reports
    def report_zip(self, aid: str, actor: str = "system") -> tuple[str, bytes]:
        a = self.store.get(aid)
        templates = load_templates()
        sc = rescore(a)
        gate = completeness_gate(a, None)
        buf = io.BytesIO()
        with TemporaryDirectory() as tmp, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            out = Path(tmp)
            for tid in a.templates:
                t = templates[tid]
                p = fill_template(t, template_path(t), a, out / f"{tid} — {a.application.name}.docx")
                z.write(p, p.name)
            p = build_summary(a, sc, templates, load_rubric(), load_matrix(), out / f"Assessment Summary — {a.application.name}.docx")
            z.write(p, p.name)
            z.writestr("assessment.json", a.model_dump_json(indent=1))
            z.writestr("scorecard.json", sc.model_dump_json(indent=1))
            if gate.blockers:
                z.writestr("DRAFT-NOT-COMPLETE.txt", "Report generated before the completeness gate passed:\n" + "\n".join(gate.blockers))
        self.store.save(a, sc, events=[envelope("report_generated", aid, {"blockers": len(gate.blockers)}, actor)],
                        audit=[{"actor": actor, "action": "report"}])
        name = f"SRA {a.application.name} {a.assessment_date.isoformat()}.zip"
        return name, buf.getvalue()
