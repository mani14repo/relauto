"""AI-assisted evaluation of criteria the deterministic rules did not settle.

For each pending criterion: retrieve evidence chunks -> structured LLM call -> verify -> CriterionResult.
Verification failures never become Met/Gap: they become Insufficient Evidence flagged for human review.
Human-review criteria get a preliminary result, always flagged.

Efficiency (CPU inference is dominated by reading the prompt):
  * a re-run reuses the previous answer when the model, prompt version and evidence sent are unchanged (LLM_CACHE);
  * each call gets the top LLM_TOP_K chunks (default 4), each cut to the LLM_CHUNK_CHARS (default 800) characters
    that best match the criterion, and chunks are dropped if the prompt would not fit the context window;
  * the fixed system prompt comes first, so Ollama reuses it from its cache between calls;
  * the search index and embedding model are built once per evidence set / process (also for criterion chat);
  * the server's token counts and timings are logged for every call;
  * with LLM_CONCURRENCY > 1 or several LLM_BASE_URL servers, criteria run in parallel, one per free client.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from ..catalog import load_templates
from ..catalog.models import Criterion, Method, Result, Risk
from ..config import setting
from ..evidence.bundle import EvidenceBundle
from ..models import Assessment, CriterionResult, Source
from ..progress import NullProgress, Progress
from ..retrieval.chunks import Chunk, HybridIndex, cached_index, focus, shared_embedder
from .client import ChatClient, LLMError, clients_from_settings
from .prompts import CHAT_SYSTEM, PROMPT_VERSION, SCHEMA, SYSTEM, criterion_prompt
from .verify import verify

log = logging.getLogger(__name__)


def _wants_second_opinion(cur: CriterionResult) -> bool:
    """A checklist Gap caused only by keywords not matching: worth asking the model, which reads the wording."""
    return cur.second_opinion and cur.source == Source.RULE and cur.result == Result.GAP


def settle(prior: CriterionResult | None, new: CriterionResult, outcome: str) -> tuple[CriterionResult, str]:
    """Combine the model's answer with the result it was asked to double-check. Returns (result, outcome).

    Only for checklist Gaps from keyword misses: a verified Met or Gap from the model replaces the checklist's Gap,
    keeps its notes and evidence, and is always flagged for review (the two disagree, or the model wrote the
    recommendation). Any other answer (no evidence, rejected, error, Insufficient, N/A) keeps the checklist's Gap.
    Everything else passes through unchanged.
    """
    if prior is None or not _wants_second_opinion(prior):
        return new, outcome
    if outcome in ("verified", "reused") and new.result in (Result.MET, Result.GAP):
        r = new.model_copy(deep=True)
        verdict = "found the items the keywords missed" if new.result == Result.MET else "agrees it is a Gap"
        r.rationale = f"{new.rationale}\nAI second opinion on a keyword checklist Gap; the model {verdict}. Checklist: {prior.rationale}"
        r.evidence_refs = list(dict.fromkeys(new.evidence_refs + prior.evidence_refs))
        r.needs_human_review = True
        return r, outcome
    kept = prior.model_copy(deep=True)
    kept.second_opinion = False
    why = {"no_evidence": "found no matching evidence", "rejected": "gave an answer that failed verification",
           "error": "call failed"}.get(outcome, f"answered {new.result.value if new.result else 'nothing'}")
    kept.rationale = f"{prior.rationale}\nAI second opinion: the model {why}; the checklist's Gap stands."
    return kept, "kept"
CHARS_PER_TOKEN = 3.5          # conservative estimate for English prompts; real counts are logged per call


def query_for(c: Criterion) -> str:
    return f"{c.name} {c.required_evidence} {c.acceptance_criteria}"


def _new_stats() -> dict:
    return {"calls": 0, "verified": 0, "rejected": 0, "no_evidence": 0, "errors": 0, "reused": 0, "trimmed": 0,
            "at_limit": 0, "prompt_tokens": 0, "reply_tokens": 0, "prompt_s": 0.0, "reply_s": 0.0, "load_s": 0.0}


@dataclass
class Evaluator:
    client: ChatClient
    top_k: int = 4
    chunk_chars: int = 800
    min_confidence: float = 0.6
    embedder: object | None = None
    # On Postgres: store.retriever, i.e. (assessment id, embedder) -> PgHybridIndex over the knowledge store.
    # None = build the index in memory from the bundle (no database, CLI, tests).
    retriever: object | None = None
    use_cache: bool = True
    # One client per call that may be in flight (LLM_CONCURRENCY per server, across LLM_BASE_URL servers).
    # Empty means just `client`, one call at a time.
    clients: list = field(default_factory=list)
    stats: dict = field(default_factory=_new_stats)
    _fresh: bool = False           # set for an explicit re-evaluation: ask the model again
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_settings(cls) -> "Evaluator":
        pool = clients_from_settings()
        return cls(client=pool[0], clients=pool, embedder=shared_embedder(), top_k=int(setting("LLM_TOP_K", "4")),
                   chunk_chars=int(setting("LLM_CHUNK_CHARS", "800")),
                   use_cache=str(setting("LLM_CACHE", "true")).strip().lower() not in ("0", "false", "no"))

    @property
    def pool(self) -> list:
        return self.clients or [self.client]

    def _bump(self, key: str, n: float = 1) -> None:
        with self._lock:
            self.stats[key] += n

    def index(self, bundle: EvidenceBundle, a: Assessment | None = None) -> HybridIndex:
        """The hybrid index for this evidence: Postgres (tsvector + pgvector) when the store offers one, else in memory."""
        if self.retriever is not None and a is not None:
            try:
                return self.retriever(a.id, self.embedder)
            except Exception:  # noqa: BLE001 - retrieval must not stop an assessment; the in-memory index gives the same ranking idea
                log.exception("Postgres retrieval unavailable for %s; using the in-memory index", a.id)
        return cached_index(bundle, self.embedder)

    # ------------------------------------------------------------------------------------------ evidence for a call
    def evidence_for(self, query: str, idx: HybridIndex, reserved_chars: int) -> list[Chunk]:
        """Top chunks, each cut to its best-matching part, dropping the weakest until the prompt fits the window."""
        chunks = [Chunk(id=ch.id, evidence_id=ch.evidence_id, source=ch.source, kind=ch.kind,
                        text=focus(ch.text, query, self.chunk_chars)) for ch in idx.search(query, k=self.top_k)]
        num_ctx = getattr(self.client, "num_ctx", None)
        if num_ctx:
            budget = int(0.85 * num_ctx * CHARS_PER_TOKEN) - reserved_chars
            while len(chunks) > 1 and sum(len(ch.text) + 60 for ch in chunks) > budget:
                chunks.pop()
                self._bump("trimmed")
        return chunks

    def _key(self, system: str, prompt: str) -> str:
        model = getattr(self.client, "model", "")
        raw = json.dumps([PROMPT_VERSION, model, getattr(self.client, "think", False), system, prompt, SCHEMA], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------------------------------------ servers
    def _healthy_pool(self, progress: Progress) -> list:
        """Clients whose server is up with the model pulled; one health check per server."""
        ok: dict[str, bool] = {}
        for cl in self.pool:
            url = getattr(cl, "base_url", "")
            if url not in ok:
                health = getattr(cl, "health", None)
                ok[url] = health() if health is not None else True
        down = [u for u, good in ok.items() if not good]
        if down and len(down) < len(ok):
            progress.warn(f"Model server(s) not available, continuing without them: {', '.join(down)}")
        return [cl for cl in self.pool if ok[getattr(cl, "base_url", "")]]

    # ------------------------------------------------------------------------------------------ run
    def evaluate_pending(self, a: Assessment, bundle: EvidenceBundle, only: list[str] | None = None,
                         progress: Progress | None = None) -> None:
        progress = progress or NullProgress()
        self._fresh = bool(only)
        for cl in self.pool:
            if hasattr(cl, "cancel_check"):
                cl.cancel_check = progress.is_cancelled   # lets a cancel interrupt every call in flight
        templates = load_templates()
        pending = []
        for tid in a.templates:
            t = templates[tid]
            for c in t.criteria:
                if only and c.id not in only:
                    continue
                cur = a.results.get(c.id)
                if cur is not None and cur.result is not None and not only and not _wants_second_opinion(cur):
                    continue
                if cur is not None and cur.source == Source.HUMAN:
                    continue
                pending.append((t, c))
        model = getattr(self.client, "model", "model")
        servers = sorted({getattr(cl, "base_url", "") for cl in self.pool} - {""})
        where = ", ".join(servers)
        seconds = sum(1 for _, c in pending if a.results.get(c.id) and _wants_second_opinion(a.results[c.id]))
        progress.phase("AI-assisted criteria", total=len(pending),
                       msg=f"{len(pending)} criteria need the local model"
                       + (f" ({seconds} of them checklist Gaps where only keywords were missing, for a second opinion)" if seconds else "")
                       + f"; checking {model}" + (f" at {where}" if where else ""))
        if not pending:
            a.detector_runs["llm"] = f"ran: {model} (nothing left to evaluate)"
            progress.info("Every criterion was settled by the rules; no model calls needed")
            return
        pool = self._healthy_pool(progress)
        if not pool:
            # Fail fast: one check instead of a timeout per criterion. Pending criteria stay unevaluated
            # (they score as Insufficient Evidence) and the run records why.
            a.detector_runs["llm"] = f"skipped: model {model} unavailable"
            self.stats["errors"] += 1
            progress.warn(f"Model {model} is not available" + (f" at {where}" if where else "")
                          + (" (is ANTHROPIC_API_KEY set?)" if hasattr(self.client, "refusal_fallback")
                             else " (is Ollama running and the model pulled?)")
                          + f". {len(pending)} criteria left for the assessor.")
            return
        progress.info(f"Model {model} is reachable; indexing the evidence for retrieval")
        progress.busy("Indexing the evidence for retrieval" + (" (computing embeddings)" if self.embedder else ""))
        idx = self.index(bundle, a)
        n_servers = len({getattr(cl, "base_url", "") for cl in pool})
        how = ("one at a time" if len(pool) == 1 else
               f"{len(pool)} at a time" + (f" across {n_servers} servers" if n_servers > 1 else ""))
        progress.info(f"Evaluating {len(pending)} criteria {how}, {self.top_k} evidence passages each "
                      "(CPU models take roughly 20s–3min per criterion)")
        if len(pool) == 1:
            self._run_sequential(a, pending, idx, pool[0], progress)
        else:
            self._run_parallel(a, pending, idx, pool, progress)
        s = self.stats
        a.detector_runs["llm"] = f"ran: {model}; {s['calls']} calls, {s['reused']} reused" + (f", {how}" if len(pool) > 1 else "")
        progress.info(f"AI pass finished: {s['verified']} verified, {s['rejected']} rejected, {s['no_evidence']} without "
                      f"evidence, {s['errors']} errors, {s['reused']} reused from the last run" + self._totals_note())
        if s["at_limit"]:
            progress.warn(f"{s['at_limit']} answer(s) hit LLM_NUM_PREDICT and were cut off; raise it if this repeats")

    def _outcome(self, c: Criterion, r: CriterionResult, outcome: str, secs: float, usage: dict) -> tuple[str, str]:
        if outcome == "error":
            return f"{c.id}: model call failed after {secs:.0f}s — {r.rationale}", "warn"
        if outcome == "rejected":
            return f"{c.id}: answer rejected by verification after {secs:.0f}s — left for the assessor", "warn"
        if outcome == "no_evidence":
            return f"{c.id}: no matching evidence, marked Insufficient Evidence", "info"
        if outcome == "reused":
            return f"{c.id}: {r.result.value} (reused: same evidence, model and prompt as the last run)", "info"
        if outcome == "kept":
            return f"{c.id}: kept the checklist's Gap (the model gave no usable answer after {secs:.0f}s)", "info"
        return f"{c.id}: {r.result.value} ({secs:.0f}s{self._usage_note(usage)}, confidence {r.confidence:.0%})", "info"

    def _run_sequential(self, a, pending, idx, client, progress: Progress) -> None:
        for i, (t, c) in enumerate(pending, 1):
            progress.start_item(i, f"{c.id} {c.name}")
            t0 = time.monotonic()
            r, outcome, usage = self._evaluate(a, t, c, idx, client)
            r, outcome = settle(a.results.get(c.id), r, outcome)
            a.results[c.id] = r
            msg, level = self._outcome(c, r, outcome, time.monotonic() - t0, usage)
            progress.finish_item(i, msg, level)

    def _run_parallel(self, a, pending, idx, pool: list, progress: Progress) -> None:
        """Each worker takes a free client (so a busy or slow server simply gets fewer criteria) for one criterion.

        Results and progress are handled here on the calling thread; a cancel stops queued criteria and makes
        every call in flight stop within a second (each client checks the cancel flag while streaming).
        """
        free: queue.Queue = queue.Queue()
        for cl in pool:
            free.put(cl)
        in_flight: dict[str, float] = {}

        def work(t, c):
            cl = free.get()
            try:
                progress.check()          # never start a new criterion after a cancel
                with self._lock:
                    in_flight[c.id] = time.monotonic()
                t0 = time.monotonic()
                r, outcome, usage = self._evaluate(a, t, c, idx, cl)
                return c, r, outcome, usage, time.monotonic() - t0
            finally:
                with self._lock:
                    in_flight.pop(c.id, None)
                free.put(cl)

        def now_text() -> str:
            with self._lock:
                ids = sorted(in_flight, key=in_flight.get)
            return f"{len(ids)} in progress: {', '.join(ids)}" if ids else ""

        ex = ThreadPoolExecutor(max_workers=len(pool), thread_name_prefix="llm-criterion")
        futures = {ex.submit(work, t, c) for t, c in pending}
        done_n = 0
        try:
            while futures:
                finished, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                progress.check()
                for f in finished:
                    c, r, outcome, usage, secs = f.result()      # re-raises JobCancelled or an unexpected error
                    r, outcome = settle(a.results.get(c.id), r, outcome)
                    a.results[c.id] = r
                    done_n += 1
                    msg, level = self._outcome(c, r, outcome, secs, usage)
                    progress.items_done(done_n, now_text(), msg, level)
                if not finished:
                    progress.items_done(done_n, now_text())
        finally:
            ex.shutdown(wait=True, cancel_futures=True)

    def _usage_note(self, u: dict) -> str:
        if not u.get("prompt_tokens") and not u.get("reply_tokens"):
            return ""
        parts = [f"read {u.get('prompt_tokens', 0)} prompt tokens" + (f" in {u['prompt_s']:.0f}s" if u.get("prompt_s") else ""),
                 f"wrote {u.get('reply_tokens', 0)}" + (f" in {u['reply_s']:.0f}s" if u.get("reply_s") else "")]
        if u.get("load_s", 0) >= 1:
            parts.append(f"model load {u['load_s']:.0f}s")
        return ": " + ", ".join(parts)

    def _totals_note(self) -> str:
        s = self.stats
        if not s["calls"] or not (s["prompt_tokens"] or s["reply_tokens"]):
            return ""
        n = s["calls"]
        return (f". Per call on average: {s['prompt_tokens'] // n} prompt tokens read in {s['prompt_s'] / n:.0f}s, "
                f"{s['reply_tokens'] // n} written in {s['reply_s'] / n:.0f}s"
                + (f"; model loading took {s['load_s']:.0f}s in total" if s["load_s"] >= 1 else ""))

    def _record_usage(self, u: dict) -> None:
        with self._lock:
            for k in ("prompt_tokens", "reply_tokens", "prompt_s", "reply_s", "load_s"):
                self.stats[k] += u.get(k, 0) or 0
            self.stats["at_limit"] += 1 if u.get("stopped_at_limit") else 0

    def evaluate_one(self, a: Assessment, t, c: Criterion, idx: HybridIndex) -> CriterionResult:
        return self._evaluate(a, t, c, idx, self.client)[0]

    def _evaluate(self, a: Assessment, t, c: Criterion, idx: HybridIndex, client) -> tuple[CriterionResult, str, dict]:
        """(result, outcome, usage). outcome: verified | reused | no_evidence | rejected | error."""
        human = Method.HUMAN in c.methods
        empty_prompt = criterion_prompt(a.application.name, t, c, [], a.assessment_date.isoformat(), a.profile)
        chunks = self.evidence_for(query_for(c), idx, len(SYSTEM) + len(empty_prompt))
        if not chunks:
            self._bump("no_evidence")
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale=f"No evidence found for: {c.required_evidence}", needs_human_review=human,
                                   confidence=1.0), "no_evidence", {}
        prompt = criterion_prompt(a.application.name, t, c, chunks, a.assessment_date.isoformat(), a.profile)
        key = self._key(SYSTEM, prompt)
        with self._lock:
            cached = a.llm_cache.get(c.id) if self.use_cache and not self._fresh else None
        if cached and cached.get("key") == key:
            self._bump("reused")
            return CriterionResult.model_validate(cached["result"]), "reused", {}
        self._bump("calls")
        try:
            raw = client.json_chat(SYSTEM, prompt, SCHEMA)
        except LLMError as exc:
            usage = dict(getattr(client, "last_usage", None) or {})
            self._record_usage(usage)
            self._bump("errors")
            log.warning("LLM error on %s: %s", c.id, exc)
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale=f"Automated evaluation failed ({exc}); assessor must evaluate.",
                                   evidence_refs=[ch.id for ch in chunks], needs_human_review=True, confidence=0.0), "error", usage
        usage = dict(getattr(client, "last_usage", None) or {})
        self._record_usage(usage)
        ok, problems, out = verify(raw, {ch.id for ch in chunks})
        if not ok:
            self._bump("rejected")
            return CriterionResult(criterion_id=c.id, result=Result.INSUFFICIENT, source=Source.LLM,
                                   rationale="AI result rejected by verification: " + "; ".join(problems),
                                   evidence_refs=[ch.id for ch in chunks], needs_human_review=True, confidence=0.0), "rejected", usage
        self._bump("verified")
        conf = float(out["confidence"])
        rationale = out["rationale"] + (("\nUnmet: " + "; ".join(out["gaps"])) if out["gaps"] else "")
        src_names = {ch.id: f"{ch.id} ({ch.source})" for ch in chunks}
        r = CriterionResult(
            criterion_id=c.id, result=Result(out["result"]), rationale=rationale,
            evidence_refs=[src_names[i] for i in out["cited"]], source=Source.LLM, confidence=conf,
            risk=Risk(out["risk"]) if out["result"] != "Met" else None,
            recommendation=out["recommendation"] if out["result"] != "Met" else "",
            measured_value=out["measured_value"] if c.formula else "",
            maturity_level=out["maturity_level"] if c.formula else None,
            needs_human_review=human or conf < self.min_confidence)
        if self.use_cache:            # only verified answers are reused; failures are retried next run
            with self._lock:
                a.llm_cache[c.id] = {"key": key, "result": r.model_dump(mode="json")}
        return r, "verified", usage

    def chat(self, a: Assessment, bundle: EvidenceBundle, criterion_id: str, question: str) -> str:
        templates = load_templates()
        tid = criterion_id.rsplit("-", 1)[0]
        c = next(c for c in templates[tid].criteria if c.id == criterion_id)
        q = f"{question} {query_for(c)}"
        chunks = [Chunk(id=ch.id, evidence_id=ch.evidence_id, source=ch.source, kind=ch.kind, text=focus(ch.text, q, self.chunk_chars))
                  for ch in self.index(bundle, a).search(q, k=self.top_k)]
        cur = a.results.get(criterion_id)
        ev = "\n".join(f'<evidence id="{ch.id}" source="{ch.source}">\n{ch.text}\n</evidence>' for ch in chunks)
        user = (f"Criterion {c.id} {c.name}\nAcceptance criteria: {c.acceptance_criteria}\n"
                f"Current result: {cur.result.value if cur and cur.result else 'not evaluated'} — {cur.rationale if cur else ''}\n\n"
                f"{ev}\n\nQuestion: {question}")
        return self.client.chat(CHAT_SYSTEM, user)
