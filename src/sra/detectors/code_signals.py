"""Reliability signals in source code (from a repository scan: evidence/code_signals.py).

One finding per kind of signal (with up to `max_refs` locations), and rule results, all marked for human review because
the analysis is pattern-based:
- SRE-AT-05-C01 timeouts: Gap when outbound calls have no timeout; Met when timeouts are set and none is missing;
- SRE-AT-05-C02 retries with backoff and SRE-AT-05-C03 circuit breakers: Met when a library or setting is found;
- SRE-AT-06-C04 throttling: Met when rate limiting is found, Gap when thread pools or queues are unbounded;
- SRE-AT-03-C01 structured logs: Met with a structured logger, Gap when the code logs with print/console only;
- SRE-AT-03-C02 metrics: Met when a metrics library is used;
- SRE-AT-11-C02 secrets: Gap for credentials hard-coded in source;
- SRE-AT-13-C03 integration error handling: Gap for swallowed exceptions;
- SRE-AT-11-C01 and SRE-AT-12-C02: review notes (insecure code patterns; no automated tests).
Latency, performance and scalability signals that no criterion measures directly are reported as findings.
"""
from __future__ import annotations

from collections import defaultdict

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..models import FindingType
from .base import Context, DetectorReport, finding, result

NAME = "code_signals"
SEV_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
TIMEOUT_RULES = {"py-requests-no-timeout", "py-urlopen-no-timeout", "go-default-http-client", "go-client-no-timeout",
                 "java-urlconnection-no-timeout", "java-httpclient-no-timeout", "java-resttemplate-no-timeout",
                 "js-axios-no-timeout", "js-fetch-no-timeout"}
UNBOUNDED_RULES = {"java-unbounded-pool", "py-unbounded-queue"}
SWALLOW_RULES = {"swallowed-exception", "bare-except"}


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    items = b.code_analyses()
    if not items:
        rep.status = "skipped: no source code analysis (scan a GitHub repository)"
        return rep
    cfg = ctx.cfg.get("code_signals") or {}
    min_rank = SEV_RANK[cfg.get("report_min_severity", "Low")]
    max_refs = int(cfg.get("max_refs", 10))
    min_prints = int(cfg.get("print_logging_min", 20))

    # combine every analysed repository (and snapshot): one entry per rule, locations de-duplicated
    rules: dict[str, dict] = {}
    seen: dict[str, set] = defaultdict(set)
    caps: dict[str, list[str]] = defaultdict(list)
    files = tests = prints = 0
    for it in items:
        doc = it.meta.get("doc") or {}
        practice = doc.get("practice") or {}
        files += int(practice.get("source_files", 0) or it.meta.get("files", 0))
        tests += int(practice.get("test_files", 0))
        prints += int(practice.get("print_statements", 0))
        for cap, paths in (doc.get("capabilities") or {}).items():
            caps[cap] += [f"{it.id}:{p}" for p in paths if f"{it.id}:{p}" not in caps[cap]]
        for s in it.records:
            r = rules.setdefault(s["rule"], {**s, "count": 0, "refs": [], "where": []})
            for o in s.get("occurrences") or []:
                key = (o.get("path"), o.get("line"))
                if key in seen[s["rule"]]:
                    continue
                seen[s["rule"]].add(key)
                r["refs"].append(f"{it.id}:{o.get('path')}:{o.get('line')}")
                r["where"].append(f"{o.get('path')}:{o.get('line')}" + (f" `{o['snippet']}`" if o.get("snippet") else ""))
            r["count"] = max(r["count"], int(s.get("count", 0)))
    item_refs = [it.ref() for it in items]

    for rid, r in sorted(rules.items(), key=lambda kv: (SEV_RANK.get(kv[1]["severity"], 3), kv[0])):
        sev = r["severity"] if r["severity"] in SEV_RANK else "Low"
        if SEV_RANK[sev] > min_rank:
            continue
        ftype = (FindingType.VULNERABILITY if r["category"] == "security"
                 else FindingType.OBSERVATION if sev == "Low" else FindingType.GAP)
        more = r["count"] - len(r["where"][:5])
        rep.findings.append(finding(
            ftype, Risk(sev), f"{r['title']} ({r['count']} place{'s' if r['count'] != 1 else ''})",
            f"{r['category']}: " + "; ".join(r["where"][:5]) + (f"; and {more} more" if more > 0 else ""),
            criteria=r.get("criteria") or [], refs=r["refs"][:max_refs] or item_refs, detector=f"{NAME}:{rid}",
            action=r.get("action", ""), confidence=0.8 if rid == "hardcoded-secret" else 0.7))

    def count(ids) -> int:
        return sum(rules[x]["count"] for x in ids if x in rules)

    def refs(ids) -> list[str]:
        return [ref for x in ids if x in rules for ref in rules[x]["refs"][:max_refs]] or item_refs

    no_timeout = count(TIMEOUT_RULES)
    if no_timeout:
        rep.results["SRE-AT-05-C01"] = result(
            "SRE-AT-05-C01", Result.GAP, f"Source code: {no_timeout} outbound call(s) without a timeout "
            f"({', '.join(sorted(rules[x]['title'] for x in TIMEOUT_RULES if x in rules))})",
            refs(TIMEOUT_RULES), risk=Risk.HIGH, review=True, confidence=0.7)
    elif caps.get("timeouts"):
        rep.results["SRE-AT-05-C01"] = result("SRE-AT-05-C01", Result.MET, "Source code sets timeouts on outbound calls "
                                              "and no call without one was found", caps["timeouts"][:max_refs], review=True,
                                              confidence=0.6)
    for cid, cap, label in (("SRE-AT-05-C02", "retries_with_backoff", "Retry with backoff"),
                            ("SRE-AT-05-C03", "circuit_breaker", "Circuit breaker"),
                            ("SRE-AT-03-C02", "metrics", "Application metrics library")):
        if caps.get(cap):
            rep.results[cid] = result(cid, Result.MET, f"Source code: {label.lower()} found in "
                                      f"{', '.join(p.split(':', 1)[1] for p in caps[cap][:3])}; confirm it covers the "
                                      "critical dependencies", caps[cap][:max_refs], review=True, confidence=0.6)

    unbounded = count(UNBOUNDED_RULES)
    if unbounded:
        rep.results["SRE-AT-06-C04"] = result("SRE-AT-06-C04", Result.GAP, f"Source code: {unbounded} unbounded thread "
                                              "pool(s) or queue(s); load is not shed", refs(UNBOUNDED_RULES), review=True,
                                              confidence=0.6)
    elif caps.get("rate_limiting"):
        rep.results["SRE-AT-06-C04"] = result("SRE-AT-06-C04", Result.MET, "Source code applies rate limiting / "
                                              "throttling", caps["rate_limiting"][:max_refs], review=True, confidence=0.6)

    if caps.get("structured_logging"):
        rep.results["SRE-AT-03-C01"] = result("SRE-AT-03-C01", Result.MET, "Source code uses a structured logger",
                                              caps["structured_logging"][:max_refs], review=True, confidence=0.6)
    elif prints >= min_prints:
        rep.results["SRE-AT-03-C01"] = result("SRE-AT-03-C01", Result.GAP, f"Source code logs with print/console "
                                              f"({prints} statements) and no structured logger was found", item_refs,
                                              review=True, confidence=0.6)
        rep.findings.append(finding(FindingType.GAP, Risk.MEDIUM, f"Unstructured logging ({prints} print/console statements)",
                                    criteria=["SRE-AT-03-C01"], refs=item_refs, detector=f"{NAME}:print-logging",
                                    action="Log through a structured (JSON) logger with request/trace ids", confidence=0.7))

    if "hardcoded-secret" in rules:
        rep.results["SRE-AT-11-C02"] = result("SRE-AT-11-C02", Result.GAP, f"{rules['hardcoded-secret']['count']} "
                                              "credential(s) hard-coded in source", refs({"hardcoded-secret"}),
                                              risk=Risk.CRITICAL, confidence=0.8)
    swallowed = count(SWALLOW_RULES)
    if swallowed:
        rep.results["SRE-AT-13-C03"] = result("SRE-AT-13-C03", Result.GAP, f"Source code: {swallowed} exception "
                                              "handler(s) swallow errors or catch everything", refs(SWALLOW_RULES),
                                              review=True, confidence=0.6)

    insecure = [r for r in rules.values() if r["category"] == "security" and r["rule"] != "hardcoded-secret"
                and SEV_RANK.get(r["severity"], 3) <= 1]
    if insecure:
        rep.review["SRE-AT-11-C01"] = ("Source code has high-risk patterns not covered by scanner results: "
                                       + ", ".join(f"{r['title']} ({r['count']})" for r in insecure))
    if files >= 5 and tests == 0:
        rep.findings.append(finding(FindingType.GAP, Risk.MEDIUM, f"No automated tests found ({files} source files)",
                                    criteria=["SRE-AT-12-C02"], refs=item_refs, detector=f"{NAME}:no-tests",
                                    action="Add unit and integration tests and make them a blocking pipeline gate",
                                    confidence=0.7))
        rep.review["SRE-AT-12-C02"] = "The scanned repository has no test files"
    missing = [c for c in ("timeouts", "retries_with_backoff", "circuit_breaker", "health_endpoint", "graceful_shutdown",
                           "metrics", "structured_logging") if not caps.get(c)]
    if missing and files:
        rep.findings.append(finding(FindingType.OBSERVATION, Risk.LOW, "Reliability capabilities not found in the code: "
                                    + ", ".join(m.replace("_", " ") for m in missing),
                                    "They may live in a framework, sidecar or service mesh; confirm with the team.",
                                    criteria=["SRE-AT-05-C02", "SRE-AT-05-C03", "SRE-AT-05-C05"], refs=item_refs,
                                    detector=f"{NAME}:capabilities", confidence=0.5))
    rep.status = f"ran: {files} source files, {sum(r['count'] for r in rules.values())} signals"
    return rep
