"""Document checklists: criteria that ask "does the evidence contain X, Y and Z", answered without an LLM.

Each criterion in config/checklists.yaml lists the documents to look in and the items that must be there:

    SRE-AT-16-C01:
      find: [toil]                       # file name or title (first two lines) matches -> candidate
      doc_types: [runbook]               # ...or the document was classified as one of these
      fresh_months: 6                    # newest candidate document reviewed within N months
      monthly: 3                         # dated evidence in each of the last N months (reports, minutes)
      review_met: true                   # a Met still needs a person (approval, test, count the data cannot show)
      items:
        - name: weekly effort per item
          any: ['\\d+\\s*h(ours|rs)?\\s*(/|per)\\s*week']   # regexes, case-insensitive
          min_count: 3                   # pattern must occur at least N times
          means: hours of effort per week for each toil item   # optional: matched by meaning (embeddings)
          none: ['if it looks bad']      # optional: the item fails if any of these appear
          fresh_months: 12               # optional: only documents reviewed within N months count for this item

Record checklists (`source: p1_incidents` or `kb_articles`, with `per_record: true` and `min_pct`) apply the items
to each record instead of to documents.

How a result is decided:
  * no candidate document (or record): nothing is set; the criterion goes to the assessor (or the LLM);
  * every item found (and fresh/monthly conditions met): Met;
  * otherwise Gap, naming the missing items. Gaps are always flagged for human review: wording the patterns do not
    expect can hide an item that is really there.
Items found only by meaning (embeddings) are reported as such and the result is flagged for review.
"""
from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache

from ..catalog.models import Result
from ..config import load_yaml
from ..evidence.bundle import EvidenceBundle
from ..evidence.model import DocType
from .base import Context, DetectorReport, ref, result, truthy

NAME = "checklists"
MET, GAP = Result.MET, Result.GAP
MONTHS = "january february march april may june july august september october november december".split()
_ISO = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])(?:-\d{2})?\b")
_MON = re.compile(r"\b(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.?\s+(20\d{2})\b", re.I)


# ------------------------------------------------------------------------------------------------ evidence
class Source:
    """A document or record the checklist reads: text, a display name, an evidence ref and a date."""
    def __init__(self, name: str, text: str, ref_: str, when: datetime | None):
        self.name, self.text, self.ref, self.when = name, text, ref_, when


def _title(text: str) -> str:
    """The first two non-empty lines: what the document says it is."""
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][:2])[:300]


def documents(b: EvidenceBundle, spec: dict) -> list[Source]:
    find = [re.compile(p, re.I) for p in spec.get("find", [])]
    types = {DocType(t) for t in spec.get("doc_types", [])}
    out = []
    for d in b.documents():
        hit = d.doc_type in types or any(p.search(d.name.replace("_", " ").replace("-", " ")) or p.search(_title(d.text)) for p in find)
        if hit:
            out.append(Source(d.name, d.text, d.ref(), d.reviewed_at))
    return out


def records(b: EvidenceBundle, ctx: Context, spec: dict) -> list[Source]:
    src = spec.get("source")
    if src == "p1_incidents":
        days = ctx.cfg["rubric_rules"]["incident_window_days"]
        rows = [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= ctx.days_ago(days)
                and str(r.get("priority", "")).strip().startswith("1")]
        return [Source(str(r.get("number")), "\n".join([str(r.get("close_notes", "")), *map(str, r.get("_human_notes", []) or []),
                                                        str(r.get("work_notes", "")), str(r.get("description", ""))]),
                       ref(r), r.get("_resolved_at") or r["_opened_at"]) for r in rows]
    if src == "kb_articles":
        rows = [r for r in ctx.scoped(b.table("kb_knowledge"), ("u_application", "kb_category", "short_description", "cmdb_ci"))
                if truthy(r.get("text") or r.get("u_body") or r.get("wiki"))]
        return [Source(str(r.get("number")), str(r.get("text") or r.get("u_body") or r.get("wiki")), ref(r), r.get("_sys_updated_on"))
                for r in rows]
    return []


# ------------------------------------------------------------------------------------------------ matching
@lru_cache(maxsize=1)
def _embedder():
    from ..retrieval.chunks import FastEmbedder
    return FastEmbedder()


def semantic_enabled(cfg: dict) -> bool:
    mode = cfg.get("semantic", "auto")
    mode = {False: "off", True: "on"}.get(mode, mode)       # YAML reads bare off/on as booleans
    if mode == "off":
        return False
    try:
        _embedder()
        return True
    except Exception:  # noqa: BLE001 - fastembed missing or model not available
        if mode == "on":
            raise
        return False


def _lines(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if 12 <= len(p.strip()) <= 400]


def _by_meaning(means: str, sources: list[Source], threshold: float) -> tuple[float, str]:
    import numpy as np
    lines = [ln for s in sources for ln in _lines(s.text)][:2000]
    if not lines:
        return 0.0, ""
    e = _embedder()
    q = np.asarray(e.embed([means])[0])
    m = np.asarray(e.embed(lines))
    sims = m @ q / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-9)
    i = int(sims.argmax())
    return float(sims[i]), lines[i]


def check_item(item: dict, sources: list[Source], ctx: Context, semantic: bool, threshold: float) -> tuple[bool, str]:
    """(found, how). `how` is a short note: the matched text, or why it failed."""
    if item.get("fresh_months"):
        sources = [s for s in sources if s.when and s.when >= ctx.months_ago(item["fresh_months"])]
        if not sources:
            return False, f"no document reviewed within {item['fresh_months']} months"
    text = "\n".join(s.text for s in sources)
    for bad in item.get("none", []):
        m = re.search(bad, text, re.I | re.M)
        if m:
            return False, f"found '{m.group(0)[:60]}'"
    need = item.get("min_count", 1)
    hits = [m for p in item.get("any", []) for m in re.finditer(p, text, re.I | re.M)]
    if len(hits) >= need:
        return True, f"'{hits[0].group(0).strip()[:60]}'" + (f" ×{len(hits)}" if need > 1 else "")
    if hits:
        return False, f"{len(hits)} of {need} needed"
    if semantic and item.get("means") and need == 1:
        score, line = _by_meaning(item["means"], sources, threshold)
        if score >= threshold:
            return True, f"by meaning ({score:.2f}): '{line[:60]}'"
    return False, "not found"


def months_covered(sources: list[Source], ctx: Context, n: int) -> list[str]:
    """Which of the last n full months (before the assessment month) have dated evidence."""
    seen = set()
    for s in sources:
        if s.when:
            seen.add((s.when.year, s.when.month))
        seen |= {(int(y), int(m)) for y, m in _ISO.findall(s.text)}
        seen |= {(int(y), MONTHS.index(next(x for x in MONTHS if x.startswith(mo.lower()[:3]))) + 1) for mo, y in _MON.findall(s.text)}
    want, y, m = [], ctx.as_of.year, ctx.as_of.month
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        want.append((y, m))
    return [f"{y}-{m:02d}" for y, m in want if (y, m) in seen]


# ------------------------------------------------------------------------------------------------ evaluation
def evaluate(cid: str, spec: dict, b: EvidenceBundle, ctx: Context, semantic: bool, threshold: float):
    if spec.get("source"):
        return _per_record(cid, spec, records(b, ctx, spec), ctx, semantic, threshold)
    docs = documents(b, spec)
    if not docs:
        return None
    found, missing, by_meaning = [], [], False
    for item in spec.get("items", []):
        ok, how = check_item(item, docs, ctx, semantic, threshold)
        (found if ok else missing).append(f"{item['name']} ({how})")
        by_meaning |= ok and how.startswith("by meaning")
    if spec.get("fresh_months"):
        newest = max((d.when for d in docs if d.when), default=None)
        if not newest or newest < ctx.months_ago(spec["fresh_months"]):
            missing.append(f"reviewed within {spec['fresh_months']} months (newest: {newest.date() if newest else 'no date found'})")
    if spec.get("monthly"):
        got = months_covered(docs, ctx, spec["monthly"])
        if len(got) < spec["monthly"]:
            missing.append(f"evidence for each of the last {spec['monthly']} months (found: {', '.join(got) or 'none'})")
    res = GAP if missing else MET
    why = (f"Checked {len(docs)} document(s): {', '.join(d.name for d in docs[:4])}{' …' if len(docs) > 4 else ''}. "
           + (f"Found: {'; '.join(found)}. " if found else "") + (f"Missing: {'; '.join(missing)}." if missing else "All checklist items present."))
    r = result(cid, res, why, [d.ref for d in docs], review=bool(missing) or by_meaning or spec.get("review_met", False),
               confidence=round(len(found) / max(1, len(found) + len(missing)), 2))
    return r, len(found), len(found) + len(missing)


def _per_record(cid, spec, recs: list[Source], ctx, semantic, threshold):
    if not recs:
        return None
    items, good, bad = spec.get("items", []), [], []
    for s in recs:
        miss = [it["name"] for it in items if not check_item(it, [s], ctx, semantic, threshold)[0]]
        (bad if miss else good).append((s, miss))
    pct = round(100 * len(good) / len(recs), 1)
    need = spec.get("min_pct", 100)
    why = (f"{len(good)} of {len(recs)} record(s) ({pct}%) contain every item ({', '.join(it['name'] for it in items)}); "
           f"target {need:g}%." + (" Missing: " + "; ".join(f"{s.name}: {', '.join(m)}" for s, m in bad[:5]) if bad else ""))
    r = result(cid, MET if pct >= need else GAP, why, [s.ref for s in recs[:10]],
               review=pct < need or spec.get("review_met", False), confidence=round(pct / 100, 2))
    return r, len(good), len(recs)


# ------------------------------------------------------------------------------------------------ special checks
_DUR = r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|h|days?)\b"


def _hours(num: str, unit: str) -> float:
    u = unit.lower()
    return float(num) / 60 if u.startswith("m") else float(num) * 24 if u.startswith("d") else float(num)


def recovery_values(text: str) -> dict[str, float]:
    out = {}
    for key in ("RTO", "RPO"):
        m = re.search(rf"\b{key}\b[^\n\d]{{0,30}}{_DUR}", text, re.I)
        if m:
            out[key] = _hours(m.group(1), m.group(2))
    return out


def rto_rpo_consistent(b, ctx, rep, cfg):
    """CMDB-AT-10-C03: RTO/RPO in the CMDB equal the continuity/DR documents and are within the tier's limits."""
    docs = documents(b, {"doc_types": ["dr_plan", "dr_test"], "find": ["continuity", "recovery objective", "disaster recovery"]})
    vals = {}
    for d in docs:
        for k, v in recovery_values(d.text).items():
            vals.setdefault(k, (v, d))
    if not vals:
        return
    limits = (cfg.get("rto_rpo_max_hours") or {}).get(ctx.tier or 2, {})
    from .rubric_rules import _app_ci
    cis = _app_ci(b, ctx)
    problems = []
    for key in ("RTO", "RPO"):
        if key not in vals:
            problems.append(f"no {key} value in the continuity or DR documents")
            continue
        v = vals[key][0]
        if limits.get(key) is not None and v > limits[key]:
            problems.append(f"{key} {v:g} h exceeds the Tier {ctx.tier or 2} maximum of {limits[key]:g} h")
        field = f"u_{key.lower()}"
        cmdb = [c for c in cis if truthy(c.get(field))]
        if cmdb:
            m = re.search(_DUR, str(cmdb[0][field]), re.I)
            cv = _hours(m.group(1), m.group(2)) if m else None
            if cv is None or abs(cv - v) > 1e-6:
                problems.append(f"CMDB {field} = {cmdb[0][field]} differs from the documents ({v:g} h)")
    seen = ", ".join(f"{k} {v:g} h ({d.name})" for k, (v, d) in vals.items())
    note = "" if cis and any(truthy(c.get("u_rto")) for c in cis) else " CMDB has no u_rto/u_rpo to compare."
    r = result("CMDB-AT-10-C03", GAP if problems else MET,
               f"Documents state {seen}." + (f" Problems: {'; '.join(problems)}." if problems else " Within tier limits.") + note,
               [d.ref for d in docs], review=bool(problems) or bool(note))
    r.measured_value = seen
    r.maturity_level = 4 if not problems and not note else 2 if not problems else 1
    rep.results["CMDB-AT-10-C03"] = r


def continuity_after_incidents(b, ctx, rep, cfg):
    """ITSM-AT-12-C04: a continuity plan review dated after the latest P1/P2 incident."""
    days = ctx.cfg["rubric_rules"]["incident_window_days"]
    major = [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= ctx.days_ago(days)
             and str(r.get("priority", "")).strip()[:1] in ("1", "2")]
    docs = documents(b, {"doc_types": ["dr_plan"], "find": ["continuity"]})
    if not major or not docs:
        return
    last = max(r.get("_resolved_at") or r["_opened_at"] for r in major)
    newest = max((d.when for d in docs if d.when), default=None)
    no_change = any(re.search(r"no (change|update)s? (required|needed)", d.text, re.I) for d in docs)
    ok = newest is not None and newest >= last
    rep.results["ITSM-AT-12-C04"] = result(
        "ITSM-AT-12-C04", MET if ok else GAP,
        f"Latest P1/P2 incident resolved {last.date()}; newest continuity plan review {newest.date() if newest else 'not dated'}"
        + ("; a 'no change required' decision is recorded" if no_change else "")
        + ". Architecture changes as triggers need the assessor.", [d.ref for d in docs] + [ref(r) for r in major[:5]],
        review=True)


def kb_findable(b, ctx, rep, cfg):
    """ITSM-AT-04-C04: KB articles can be found for the top-10 incident categories (search test)."""
    days = ctx.cfg["rubric_rules"]["incident_window_days"]
    inc = [r for r in ctx.scoped(b.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= ctx.days_ago(days)]
    kb = b.table("kb_knowledge")
    if not inc or not kb:
        return
    from collections import Counter

    from ..retrieval.chunks import HybridIndex, tokens
    cats = [c for c, _ in Counter((r.get("subcategory") or r.get("category") or "").strip() for r in inc).most_common(10) if c]
    if not cats:
        return
    from ..retrieval.chunks import Chunk
    chunks = [Chunk(id=str(r.get("number")), evidence_id=r["_evidence"], source="kb", kind="kb",
                    text=" ".join(str(r.get(f, "")) for f in ("short_description", "kb_category", "text"))) for r in kb]
    idx = HybridIndex(chunks)
    found = {}
    for c in cats:
        # a category counts as findable when a search for "<category> <app>" returns an article sharing its words
        hits = idx.search(f"{c} {ctx.app_name}", k=3)
        words = {w for w in tokens(c) if len(w) > 3}
        found[c] = next((h.id for h in hits if words & set(tokens(h.text))), None)
    missing = [c for c, h in found.items() if not h]
    rep.results["ITSM-AT-04-C04"] = result(
        "ITSM-AT-04-C04", GAP if missing else MET,
        f"Search found a KB article for {len(cats) - len(missing)} of {len(cats)} top incident categories"
        + (f"; nothing for: {', '.join(missing)}" if missing else "") + ". PSE agents should confirm the articles are usable.",
        b.table_ids("kb_knowledge"), review=True)


SPECIAL = [rto_rpo_consistent, continuity_after_incidents, kb_findable]


# ------------------------------------------------------------------------------------------------ detector
def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    cfg = load_yaml("checklists")
    settings = cfg.get("settings", {})
    semantic = semantic_enabled(settings)
    threshold = float(settings.get("semantic_threshold", 0.8))
    for cid, spec in (cfg.get("criteria") or {}).items():
        out = evaluate(cid, spec, b, ctx, semantic, threshold)
        if out is None:
            continue
        r, got, total = out
        if cid.startswith("CMDB-"):        # V2 criteria carry a measured value and level
            pct = 100 * got / max(1, total)
            r.measured_value = f"{got} of {total} checklist items present"
            r.maturity_level = 4 if r.result == MET else 3 if pct >= 80 else 2 if pct >= 50 else 1 if pct >= 25 else 0
        rep.results[cid] = r
    for fn in SPECIAL:
        fn(b, ctx, rep, settings)
    rep.status = (f"ran: {len(rep.results)} criteria checked against documents and records; matching by "
                  + ("keywords and meaning (embeddings)" if semantic else "keywords only"))
    return rep
