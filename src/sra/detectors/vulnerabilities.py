"""Vulnerability detection: scanner findings, secrets in evidence, EOL components, CI pipeline gates.

Every scanner finding at or above `vulnerabilities.report_min_severity` becomes its own Finding, so
nothing a scanner reported is dropped. Answers SRE-AT-11 C01-C04 and CMDB-AT-03-C02 / CMDB-AT-09-C01.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..evidence.model import Kind
from ..evidence.redact import find_secrets
from ..models import FindingType
from .base import Context, DetectorReport, finding, result

NAME = "vulnerabilities"
SEV_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

GATES = {
    "sca": r"dependency[- ]check|snyk|trivy|grype|owasp|\bsca\b|blackduck|mend|whitesource|dependabot|pip-audit|npm audit",
    "sast": r"codeql|sonar|semgrep|checkmarx|fortify|bandit|\bsast\b",
    "secrets": r"gitleaks|trufflehog|detect-secrets|secret[- ]scan",
    "tests": r"pytest|unittest|npm (run )?test|mvn (-\w+ )*test|gradle test|go test|jest",
    "image_scan": r"trivy image|grype|docker scout|prisma|twistlock|aqua",
}


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    vc = ctx.cfg["vulnerabilities"]
    min_rank = SEV_RANK[vc["report_min_severity"]]
    scans = b.of_kind(Kind.SCAN)

    # ---- scanner findings (one finding each, deduplicated by rule+package+version)
    seen = set()
    vulns = [f for f in b.scan_findings if not f.rule_id.startswith("secret:")]
    for f in sorted(vulns, key=lambda f: SEV_RANK[f.severity]):
        key = (f.rule_id, f.package, f.installed_version, f.location)
        if key in seen or SEV_RANK[f.severity] > min_rank:
            continue
        seen.add(key)
        fix = f"Upgrade {f.package} {f.installed_version} → {f.fixed_version}" if f.fixed_version and f.package else \
            f"Remediate {f.rule_id}" + (f" in {f.package}" if f.package else "") + " (no fixed version published: mitigate or accept risk)"
        crit = ["SRE-AT-11-C01"] + (["SRE-AT-11-C03"] if f.package else [])
        rep.findings.append(finding(
            FindingType.VULNERABILITY, Risk(f.severity), f"{f.rule_id}: {f.title[:120] or f.package}",
            f"{f.tool}; package {f.package} {f.installed_version}; fixed {f.fixed_version or 'n/a'}; {f.location}",
            criteria=crit, refs=[f"{f.evidence_id}:{f.rule_id}"], detector=f"{NAME}:{f.tool}", action=fix))
    counts = Counter(f.severity for f in vulns)
    if scans:
        blocking = sum(counts[s] for s in vc["block_severities"])
        summary = ", ".join(f"{counts[s]} {s}" for s in ("Critical", "High", "Medium", "Low") if counts[s]) or "no findings"
        refs = [s.ref() for s in scans]
        rep.results["SRE-AT-11-C01"] = result(
            "SRE-AT-11-C01", Result.GAP if blocking else Result.MET,
            f"Scanner results ({', '.join(s.meta['format'] for s in scans)}): {summary}",
            refs, risk=Risk.CRITICAL if counts["Critical"] else Risk.HIGH if blocking else None)
        dep = [f for f in vulns if f.package]
        dep_block = sum(1 for f in dep if f.severity in vc["block_severities"])
        rep.results["SRE-AT-11-C03"] = result(
            "SRE-AT-11-C03", Result.GAP if dep_block else Result.MET,
            f"{len(dep)} third-party library findings, {dep_block} at {'/'.join(vc['block_severities'])}", refs)
    else:
        rep.findings.append(finding(
            FindingType.GAP, Risk.HIGH, "No vulnerability scan results provided",
            criteria=["SRE-AT-11-C01", "SRE-AT-11-C03"], detector=f"{NAME}:no_scan",
            action="Provide current SCA and container image scan results (e.g. Trivy/Grype JSON, SARIF)"))

    # ---- secrets: scanner-reported and found in ingested text
    secret_hits = [f for f in b.scan_findings if f.rule_id.startswith("secret:")]
    for f in secret_hits:
        rep.findings.append(finding(FindingType.VULNERABILITY, Risk.CRITICAL, f"Secret detected by {f.tool}: {f.title}",
                                    f.location, criteria=["SRE-AT-11-C02"], refs=[f"{f.evidence_id}:{f.location}"],
                                    detector=f"{NAME}:secrets",
                                    action="Rotate the credential and move it to the secret manager; purge it from history"))
    text_hits = []
    for it in b.items.values():
        raw_text = it.meta.get("raw_secret_scan")  # set by the ingest pipeline before redaction, if available
        for kind, line in (raw_text or []):
            text_hits.append((it, kind, line))
    for it, kind, line in text_hits:
        rep.findings.append(finding(FindingType.VULNERABILITY, Risk.CRITICAL, f"Plain-text secret ({kind}) in {it.name}",
                                    f"line {line}; value redacted on ingest", criteria=["SRE-AT-11-C02"],
                                    refs=[it.ref(f"L{line}")], detector=f"{NAME}:secrets",
                                    action="Rotate the credential and reference it from the secret manager instead"))
    if secret_hits or text_hits:
        rep.results["SRE-AT-11-C02"] = result("SRE-AT-11-C02", Result.GAP,
                                              f"{len(secret_hits) + len(text_hits)} plain-text secret(s) found in evidence",
                                              [f.evidence_id for f in secret_hits] + [it.id for it, _, _ in text_hits],
                                              risk=Risk.CRITICAL)

    # ---- EOL components
    eol = {k.lower(): datetime.fromisoformat(v) for k, v in vc["eol_products"].items()}
    names = [(c["name"], c.get("version", "")) for c in b.components]
    for ci in b.of_kind(Kind.CI_CONFIG):
        names.append((ci.meta.get("tool", ""), ""))
    for row in b.table("cmdb_ci"):
        if row.get("version") or row.get("model_id"):
            names.append((str(row.get("name", "")), str(row.get("version", ""))))
    eol_hits = []
    for name, ver in names:
        label = f"{name} {ver}".lower().strip()
        for prod, when in eol.items():
            if when <= ctx.as_of and (label == prod or label.startswith(prod + " ") or label.startswith(prod + ".")
                                      or (" " not in prod and name.lower() == prod)):
                eol_hits.append((name, ver, prod, when))
    for name, ver, prod, when in {(h[0], h[1], h[2], h[3]) for h in eol_hits}:
        rep.findings.append(finding(
            FindingType.VULNERABILITY, Risk.CRITICAL if prod == "jenkins" else Risk.HIGH,
            f"End-of-life component: {name} {ver}".strip() + f" (EOL {when.date().isoformat()})",
            criteria=["CMDB-AT-03-C02", "CMDB-AT-09-C01", "SRE-AT-11-C04" if prod == "jenkins" else "SRE-AT-11-C03"],
            detector=f"{NAME}:eol", action=f"Plan migration off {name} {ver}".strip() + " and register the EOL risk (CMDB-PB-04)"))

    # ---- CI/CD pipeline security gates (SRE-AT-11-C04)
    cis = b.of_kind(Kind.CI_CONFIG)
    if cis:
        text = "\n".join(c.text for c in cis).lower()
        missing = [g for g, pat in GATES.items() if not re.search(pat, text)]
        jenkins = any(c.meta.get("tool") == "jenkins" for c in cis)
        res = Result.GAP if (missing and ({"sca", "secrets"} & set(missing))) or jenkins else Result.MET
        why = f"Pipeline gates present: {', '.join(g for g in GATES if g not in missing) or 'none'}; missing: {', '.join(missing) or 'none'}"
        if jenkins:
            why += "; pipeline runs on Jenkins (past EOL deadline)"
        rep.results["SRE-AT-11-C04"] = result("SRE-AT-11-C04", res, why, [c.ref() for c in cis], risk=Risk.CRITICAL if jenkins else None)
        for g in missing:
            rep.findings.append(finding(FindingType.VULNERABILITY, Risk.HIGH if g in ("sca", "secrets", "sast") else Risk.MEDIUM,
                                        f"CI/CD pipeline has no {g.replace('_', ' ')} gate", criteria=["SRE-AT-11-C04", "SRE-AT-12-C02"],
                                        refs=[c.ref() for c in cis], detector=f"{NAME}:pipeline_gates",
                                        action=f"Add a blocking {g.replace('_', ' ')} step to the pipeline (ITSM-PB-05)"))
    return rep


def pre_redaction_secret_scan(text: str) -> list[tuple[str, int]]:
    """Call on raw text before redaction; store result in EvidenceItem.meta['raw_secret_scan']."""
    return find_secrets(text)
