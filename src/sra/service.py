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
    def __init__(self, store: Store, evaluator_factory=None) -> None:
        self.store = store
        self.evaluator_factory = evaluator_factory

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

    def add_evidence(self, aid: str, files: list[tuple[str, bytes]], hint: str | None, actor: str) -> list[dict]:
        a = self.store.get(aid)
        rows = [self.store.add_evidence(aid, name, data, hint) for name, data in files]
        self.store.save(a, events=[envelope("evidence_ingested", aid, {"files": [r["name"] for r in rows]}, actor)],
                        audit=[{"actor": actor, "action": "add_evidence", "after": [r["name"] for r in rows]}])
        return rows

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
        if use_llm and self.evaluator_factory:
            progress.busy("Starting the local LLM client and loading the embedding model")
            evaluator = self.evaluator_factory()
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
        if before.get("source") in ("llm", "rule") and before.get("result") != (new.result.value if new.result else None):
            self.store.feedback(aid, cid, before, new.model_dump(mode="json"))
        sc = rescore(a)
        self.store.save(a, sc, events=[envelope("criterion_overridden", aid, {"criterion": cid, "result": new.result}, actor)],
                        audit=[{"actor": actor, "action": "override_criterion", "target": cid, "before": before,
                                "after": new.model_dump(mode="json")}])
        return {"result": new.model_dump(mode="json"), "scorecard": sc.model_dump(mode="json")}

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
