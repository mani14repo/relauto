"""AI-assisted evaluation of criteria the deterministic rules did not settle.

For each pending criterion: retrieve evidence chunks -> structured LLM call -> verify -> CriterionResult.
Verification failures never become Met/Gap: they become Insufficient Evidence flagged for human review.
Human-review criteria get a preliminary result, always flagged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..catalog import load_templates
from ..catalog.models import Criterion, Method, Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..models import Assessment, CriterionResult, Source
from ..retrieval.chunks import HybridIndex, chunk_bundle
from .client import ChatClient, LLMError, from_settings
from .prompts import CHAT_SYSTEM, SCHEMA, SYSTEM, criterion_prompt
from .verify import verify

log = logging.getLogger(__name__)


def query_for(c: Criterion) -> str:
    return f"{c.name} {c.required_evidence} {c.acceptance_criteria}"


@dataclass
class Evaluator:
    client: ChatClient
    top_k: int = 6
    min_confidence: float = 0.6
    embedder: object | None = None
    stats: dict = field(default_factory=lambda: {"calls": 0, "verified": 0, "rejected": 0, "no_evidence": 0, "errors": 0})

    @classmethod
    def from_settings(cls) -> "Evaluator":
        embedder = None
        try:
            from ..retrieval.chunks import FastEmbedder
            embedder = FastEmbedder()
        except Exception:  # fastembed optional; BM25 alone still works
            embedder = None
        return cls(client=from_settings(), embedder=embedder)

    def index(self, bundle: EvidenceBundle) -> HybridIndex:
        return HybridIndex(chunk_bundle(bundle), embedder=self.embedder)

    def evaluate_pending(self, a: Assessment, bundle: EvidenceBundle, only: list[str] | None = None) -> None:
        templates = load_templates()
        health = getattr(self.client, "health", None)
        if health is not None and not health():
            # Fail fast: one check instead of a timeout per criterion. Pending criteria stay unevaluated
            # (they score as Insufficient Evidence) and the run records why.
            a.detector_runs["llm"] = f"skipped: model {self.client.model} unavailable"
            self.stats["errors"] += 1
            return
        a.detector_runs["llm"] = f"ran: {self.client.model}"
        idx = self.index(bundle)
        for tid in a.templates:
            t = templates[tid]
            for c in t.criteria:
                if only and c.id not in only:
                    continue
                cur = a.results.get(c.id)
                if cur is not None and cur.result is not None and not only:
                    continue
                if cur is not None and cur.source == Source.HUMAN:
                    continue
                a.results[c.id] = self.evaluate_one(a, t, c, idx)

    def evaluate_one(self, a: Assessment, t, c: Criterion, idx: HybridIndex) -> CriterionResult:
        human = Method.HUMAN in c.methods
        chunks = idx.search(query_for(c), k=self.top_k)
        if not chunks:
            self.stats["no_evidence"] += 1
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale=f"No evidence found for: {c.required_evidence}", needs_human_review=human,
                                   confidence=1.0)
        prompt = criterion_prompt(a.application.name, t, c, chunks, a.assessment_date.isoformat(), a.profile)
        self.stats["calls"] += 1
        try:
            raw = self.client.json_chat(SYSTEM, prompt, SCHEMA)
        except LLMError as exc:
            self.stats["errors"] += 1
            log.warning("LLM error on %s: %s", c.id, exc)
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale=f"Automated evaluation failed ({exc}); assessor must evaluate.",
                                   evidence_refs=[ch.id for ch in chunks], needs_human_review=True, confidence=0.0)
        ok, problems, out = verify(raw, {ch.id for ch in chunks})
        if not ok:
            self.stats["rejected"] += 1
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale="AI result rejected by verification: " + "; ".join(problems),
                                   evidence_refs=[ch.id for ch in chunks], needs_human_review=True, confidence=0.0)
        self.stats["verified"] += 1
        conf = float(out["confidence"])
        rationale = out["rationale"] + (("\nUnmet: " + "; ".join(out["gaps"])) if out["gaps"] else "")
        src_names = {ch.id: f"{ch.id} ({ch.source})" for ch in chunks}
        return CriterionResult(
            criterion_id=c.id, result=Result(out["result"]), rationale=rationale,
            evidence_refs=[src_names[i] for i in out["cited"]], source=Source.LLM, confidence=conf,
            risk=Risk(out["risk"]) if out["result"] != "Met" else None,
            recommendation=out["recommendation"] if out["result"] != "Met" else "",
            needs_human_review=human or conf < self.min_confidence)

    def chat(self, a: Assessment, bundle: EvidenceBundle, criterion_id: str, question: str) -> str:
        templates = load_templates()
        tid = criterion_id.rsplit("-", 1)[0]
        c = next(c for c in templates[tid].criteria if c.id == criterion_id)
        chunks = self.index(bundle).search(f"{question} {query_for(c)}", k=self.top_k)
        cur = a.results.get(criterion_id)
        ev = "\n".join(f'<evidence id="{ch.id}" source="{ch.source}">\n{ch.text}\n</evidence>' for ch in chunks)
        user = (f"Criterion {c.id} {c.name}\nAcceptance criteria: {c.acceptance_criteria}\n"
                f"Current result: {cur.result.value if cur and cur.result else 'not evaluated'} — {cur.rationale if cur else ''}\n\n"
                f"{ev}\n\nQuestion: {question}")
        return self.client.chat(CHAT_SYSTEM, user)
