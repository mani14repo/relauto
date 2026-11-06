"""Recurring incidents found by meaning rather than by exact wording (unsupervised machine learning).

The rule-based checks group incidents by category/subcategory (rubric P2-02) and by the exact start of the
short description (ITSM-AT-03-C01). The same root cause is often filed under different categories with
different wording ("orders queue backlog", "messages stuck in the orders queue"), so neither rule sees it.

This detector turns each incident's text into a vector and groups incidents whose vectors are close:
  * text: short description (and description), with numbers, URLs and the application's own name removed.
    Every incident mentions the application, and leaving its name in makes unrelated incidents look alike;
  * vectors: the local embedding model (BAAI/bge-small-en-v1.5 through fastembed) when it is installed,
    otherwise TF-IDF over the words, so the detector always runs (the method used is recorded). TF-IDF only
    groups incidents that share words; the embeddings also group rewordings ("cert expired" / "TLS expiry");
  * grouping: leader clustering (Butina). The incident with the most close neighbours starts a cluster and
    takes all of its unassigned neighbours, then the next one, and so on. It is deterministic, needs no
    number of clusters up front, and every member is directly similar to the cluster's representative,
    so clusters cannot drift through chains of loosely related incidents.

Two incidents count as similar only above a fixed floor AND clearly above how alike this application's incidents
usually are (its median pairwise similarity plus a margin): one application's incidents all read somewhat alike.
Only groups the rules cannot already see are reported: at least `min_size` incidents on more than one day, none
linked to a Problem, and at least `min_descriptions` different descriptions (one wording is the exact-match rule's
job; two wordings are most often two routine issues that each happened twice). The findings are advisory: they never set a
criterion result or change the rating; they ask the assessor to review ITSM-AT-03-C01 and P2-02.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from ..catalog.models import Risk
from ..evidence.bundle import EvidenceBundle
from ..models import FindingType
from .base import Context, DetectorReport, finding, ref, truthy

NAME = "incident_clusters"
CRITERION = "ITSM-AT-03-C01"

_WORD = re.compile(r"[a-z][a-z0-9]+")
_NOISE = re.compile(r"https?://\S+|\b[A-Z]{2,5}\d{5,}\b|\b\d[\d:.\-/]*\b")   # URLs, record numbers, numbers and dates
# words of the application name that are kept because they carry meaning in incident text
_GENERIC = frozenset("""support team service services platform api app application engineering group system batch data
portal web mobile online the and of""".split())
_SUFFIXES = "api|svc|services?|app|web|ui|backend|frontend|server|platform|portal|systems?|hub"
_STOP = frozenset("""a an and are as at be by for from has have in into is it its of on or that the this to was were
with after before again not no than then there their they we our users user""".split())


@dataclass
class Cluster:
    seed: int
    members: list[int]
    cohesion: float          # mean similarity of the members to the representative


# ------------------------------------------------------------------------------------------------ vectors
def name_pattern(names: list[str]) -> re.Pattern | None:
    """The distinctive words of the application's name and aliases ("Orders Batch" -> orders), plural-tolerant."""
    words = {re.sub(r"s$", "", w.lower()) for n in names for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", n or "")}
    words = sorted((w for w in words if len(w) >= 3 and w not in _GENERIC and w + "s" not in _GENERIC), key=len, reverse=True)
    if not words:
        return None
    # "orders-api" and "orders_service" go entirely; "orders-api-db" keeps "-db", which says what failed
    return re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")s?(?:[-_](?:" + _SUFFIXES + r"))?\b", re.I)


def incident_text(r: dict, fields: list[str], names: re.Pattern | None = None) -> str:
    text = _NOISE.sub(" ", " ".join(str(r.get(f) or "") for f in fields if r.get(f)))
    if names is not None:
        text = names.sub(" ", text)
    return re.sub(r"\s+", " ", text)[:600].strip(" -:,")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tfidf_vectors(texts: list[str]) -> np.ndarray:
    docs = [Counter(_stem(w) for w in _WORD.findall(t.lower()) if w not in _STOP) for t in texts]
    vocab = {w: i for i, w in enumerate(sorted({w for d in docs for w in d}))}
    m = np.zeros((len(docs), max(1, len(vocab))), dtype=np.float32)
    df = Counter(w for d in docs for w in d)
    n = len(docs)
    for row, d in enumerate(docs):
        for w, tf in d.items():
            m[row, vocab[w]] = (1 + math.log(tf)) * (math.log((1 + n) / (1 + df[w])) + 1)
    return _normalise(m)


def _normalise(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


def _embedder():
    from ..retrieval.chunks import shared_embedder
    e = shared_embedder()
    if e is None:
        raise RuntimeError("fastembed or the bge-small model is not available")
    return e


def vectors(texts: list[str], method: str) -> tuple[np.ndarray, str]:
    """Returns (unit vectors, method used). `method` is auto | embedding | tfidf."""
    if method in ("auto", "embedding"):
        try:
            return _normalise(np.asarray(_embedder().embed(texts), dtype=np.float32)), "embedding"
        except Exception as exc:  # noqa: BLE001 - fastembed missing or model not available offline
            if method == "embedding":
                raise RuntimeError(f"embedding model unavailable: {exc}") from exc
    return tfidf_vectors(texts), "tfidf"


# ------------------------------------------------------------------------------------------------ clustering
def background(sim: np.ndarray) -> float:
    """This application's typical similarity between two different incident texts (median of all pairs)."""
    n = len(sim)
    if n < 5:
        return 0.0
    return float(np.median(sim[np.triu_indices(n, k=1)]))


def leader_clusters(v: np.ndarray, threshold: float, weights: list[int] | None = None) -> list[Cluster]:
    """Butina clustering over cosine similarity of distinct texts.

    `weights` is how many incidents share each text; the text whose neighbours cover the most incidents seeds first.
    Deterministic: ties go to the earlier text.
    """
    w = np.asarray(weights or [1] * len(v))
    sim = v @ v.T
    close = sim >= threshold
    np.fill_diagonal(close, False)
    order = sorted(range(len(v)), key=lambda i: (-int(w[close[i]].sum() + w[i]), i))
    taken = np.zeros(len(v), dtype=bool)
    out = []
    for i in order:
        if taken[i]:
            continue
        members = [i] + [int(j) for j in np.flatnonzero(close[i] & ~taken)]
        taken[members] = True
        others = [sim[i, j] for j in members if j != i]
        out.append(Cluster(seed=i, members=sorted(members), cohesion=float(np.mean(others)) if others else 1.0))
    return out


# ------------------------------------------------------------------------------------------------ detector
def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    cfg = ctx.cfg.get("incident_clusters") or {}
    if not cfg.get("enabled", True):
        rep.status = "skipped: disabled in config/detectors.yaml"
        return rep
    if not b.has_table("incident"):
        rep.status = "skipped: no incident export"
        return rep
    window = ctx.cfg["rubric_rules"]["incident_window_days"]
    fields = cfg.get("text_fields", ["short_description", "description"])
    min_size = cfg.get("min_size", ctx.cfg["rubric_rules"]["recurrence_min_incidents"])
    min_desc = cfg.get("min_descriptions", 3)
    inc = [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= ctx.days_ago(window)]
    inc = sorted(inc, key=lambda r: r["_opened_at"], reverse=True)[: cfg.get("max_incidents", 3000)]

    # Cluster distinct texts (after removing numbers and the application's name); incidents follow their text.
    names = name_pattern([ctx.app_name, *ctx.aliases])
    by_text: dict[str, list[dict]] = {}
    for r in inc:
        t = incident_text(r, fields, names)
        if t:
            by_text.setdefault(t.lower(), []).append(r)
    texts = list(by_text)
    if len(texts) < min_desc:
        rep.status = f"ran: {len(texts)} different incident descriptions in {window}d, too few to cluster"
        return rep
    try:
        v, used = vectors(texts, cfg.get("method", "auto"))
    except RuntimeError as exc:
        rep.status = f"skipped: {exc}"
        return rep

    # A pair is close when it is above a fixed floor AND clearly above this application's usual similarity:
    # incidents of one application all read somewhat alike, more so when they are long and written in one style.
    floor = float(cfg.get("similarity", {}).get(used, 0.72 if used == "embedding" else 0.35))
    bg = background(v @ v.T)
    threshold = max(floor, bg + float(cfg.get("margin_over_typical", 0.12)))
    clusters = leader_clusters(v, threshold, [len(by_text[t]) for t in texts])
    label = "embeddings (bge-small)" if used == "embedding" else "TF-IDF (embedding model not available)"

    candidates = reported = 0
    for c in sorted(clusters, key=lambda c: (-sum(len(by_text[texts[i]]) for i in c.members), c.seed)):
        rows = sorted((r for i in c.members for r in by_text[texts[i]]), key=lambda r: r["_opened_at"], reverse=True)
        # One wording is the rule-based recurrence check's job; two wordings are usually two routine issues.
        if len(rows) < min_size or len(c.members) < min_desc:
            continue
        candidates += 1
        if any(truthy(r.get("problem_id")) for r in rows):
            continue                                       # already under problem management
        if len({r["_opened_at"].date() for r in rows}) < 2:
            continue                                       # one burst on one day is an outage, not recurrence
        cats = sorted({"/".join(filter(None, (r.get("category"), r.get("subcategory")))) or "uncategorised" for r in rows})
        seed = by_text[texts[c.seed]][0]
        reported += 1
        rep.findings.append(finding(
            FindingType.ANOMALY, Risk.MEDIUM,
            f"Possible recurring issue: {len(rows)} similar incidents with no Problem record, "
            f"{len(c.members)} different descriptions across {len(cats)} categor{'y' if len(cats) == 1 else 'ies'}",
            f"Representative: {seed.get('number')} '{seed.get('short_description')}'. "
            f"Grouped by text similarity using {label}: mean similarity {c.cohesion:.2f} to the representative "
            f"(threshold {threshold:.2f}; typical for this application {bg:.2f}). Categories: {', '.join(cats)}. "
            "Advisory: confirm before acting.\n"
            + "\n".join(f"{r.get('number')} {r['_opened_at'].date()} [{r.get('category') or '-'}] {r.get('short_description')}"
                        for r in rows[:10]),
            criteria=[CRITERION], rubric=["P2-02"], refs=[ref(r) for r in rows[:10]], detector=f"{NAME}:recurrence",
            confidence=round(min(0.9, c.cohesion), 2),
            action="Confirm whether these incidents share a root cause; if so open a Problem record, link them and "
                   "perform RCA (ITSM-PB-03). Fix the categorisation so the pattern is visible in reporting."))
        if reported >= cfg.get("max_findings", 5):
            break
    if reported:
        rep.review[CRITERION] = (f"Text clustering found {reported} possible recurring issue(s) that the category and "
                                 "description rules do not see; review before accepting this result.")
    rep.status = (f"ran: {label}; {len(rows_total(by_text))} incidents, {len(texts)} different descriptions in {window}d; "
                  f"threshold {threshold:.2f}; {candidates} candidate group(s), {reported} reported")
    return rep


def rows_total(by_text: dict[str, list[dict]]) -> list[dict]:
    return [r for rows in by_text.values() for r in rows]
