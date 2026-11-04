"""Kubernetes / service configuration checks for resilience and capacity criteria.

Workloads: Deployment, StatefulSet, DaemonSet (and CronJob/Job pod templates).
Checks: replicas, resource requests/limits, liveness/readiness probes, PodDisruptionBudget,
HorizontalPodAutoscaler, and timeout/retry/circuit-breaker settings in manifests or config text.
Feeds SRE-AT-05-C01/C04, SRE-AT-06-C04/C05 and rubric P2-04.
"""
from __future__ import annotations

import re

from ..catalog.models import Result, Risk
from ..evidence.bundle import EvidenceBundle
from ..evidence.model import Kind
from ..models import FindingType
from .base import Context, DetectorReport, finding, result

NAME = "config_checks"
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}
TIMEOUT = re.compile(r"timeout|connect_?timeout|read_?timeout|deadline", re.I)
RETRY = re.compile(r"retr(y|ies)|backoff", re.I)
BREAKER = re.compile(r"circuit[_ -]?breaker|outlierDetection|resilience4j|hystrix|polly", re.I)


def _pod_spec(obj: dict) -> dict:
    spec = obj.get("spec", {}) or {}
    if obj.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return (spec.get("template", {}) or {}).get("spec", {}) or {}


def run(b: EvidenceBundle, ctx: Context) -> DetectorReport:
    rep = DetectorReport(NAME)
    objs = b.k8s
    configs = [it for it in b.items.values() if it.kind in (Kind.K8S, Kind.OTHER, Kind.CI_CONFIG)]
    if not objs and not configs:
        rep.status = "skipped: no configuration evidence"
        return rep

    min_rep = ctx.cfg["config_checks"]["min_replicas"]
    workloads = [(eid, o) for eid, o in objs if o.get("kind") in WORKLOADS]
    pdb_selectors = [o.get("spec", {}).get("selector", {}).get("matchLabels", {}) for _, o in objs if o.get("kind") == "PodDisruptionBudget"]
    hpa_targets = {o.get("spec", {}).get("scaleTargetRef", {}).get("name") for _, o in objs if o.get("kind") == "HorizontalPodAutoscaler"}

    single, no_limits, no_probes, no_pdb = [], [], [], []
    for eid, o in workloads:
        name = f"{o['kind']}/{o.get('metadata', {}).get('name', '?')}"
        refs = [f"{eid}:{name}"]
        spec = o.get("spec", {}) or {}
        replicas = spec.get("replicas", 1)   # Kubernetes default is 1
        if (o["kind"] in ("Deployment", "StatefulSet") and o.get("metadata", {}).get("name") not in hpa_targets
                and isinstance(replicas, int) and replicas < min_rep):
            single.append((name, refs, replicas))
        containers = _pod_spec(o).get("containers", []) or []
        for c in containers:
            res = c.get("resources", {}) or {}
            if not res.get("limits") or not res.get("requests"):
                no_limits.append((f"{name}/{c.get('name')}", refs))
            if not c.get("readinessProbe") or not c.get("livenessProbe"):
                no_probes.append((f"{name}/{c.get('name')}", refs))
        labels = (((spec.get("template") or {}).get("metadata") or {}).get("labels")) or {}
        if o["kind"] != "DaemonSet" and not any(sel and all(labels.get(k) == v for k, v in sel.items()) for sel in pdb_selectors):
            no_pdb.append((name, refs))

    for name, refs, n in single:
        rep.findings.append(finding(FindingType.VULNERABILITY, Risk.HIGH, f"Single point of failure: {name} runs {n} replica(s)",
                                    criteria=["SRE-AT-05-C04", "SRE-AT-02-C01"], refs=refs, detector=f"{NAME}:replicas",
                                    action=f"Run at least {min_rep} replicas across zones, or add an HPA with minReplicas ≥ {min_rep}"))
    for name, refs in no_limits:
        rep.findings.append(finding(FindingType.VULNERABILITY, Risk.MEDIUM, f"No resource requests/limits on {name}",
                                    criteria=["SRE-AT-06-C04"], refs=refs, detector=f"{NAME}:resources",
                                    action="Set CPU/memory requests and limits from load-test baselines (SRE-PB-06)"))
    for name, refs in no_probes:
        rep.findings.append(finding(FindingType.VULNERABILITY, Risk.MEDIUM, f"Missing liveness/readiness probe on {name}",
                                    criteria=["SRE-AT-05-C04", "SRE-AT-15-C01"], refs=refs, detector=f"{NAME}:probes",
                                    action="Add readiness and liveness probes against a health endpoint"))
    for name, refs in no_pdb:
        rep.findings.append(finding(FindingType.OBSERVATION, Risk.MEDIUM, f"No PodDisruptionBudget covers {name}",
                                    criteria=["SRE-AT-05-C04"], refs=refs, detector=f"{NAME}:pdb",
                                    action="Add a PodDisruptionBudget so node maintenance cannot take all replicas down"))

    if workloads:
        refs = [f"{eid}:{o['kind']}/{o.get('metadata', {}).get('name')}" for eid, o in workloads]
        rep.results["SRE-AT-05-C04"] = result("SRE-AT-05-C04", Result.GAP if single else Result.MET,
                                              f"{len(workloads)} workload(s); {len(single)} below {min_rep} replicas; "
                                              f"{len(no_pdb)} without PDB", refs)
        rep.results["SRE-AT-06-C04"] = result("SRE-AT-06-C04", Result.GAP if no_limits else Result.MET,
                                              f"{len(no_limits)} container(s) without requests/limits", refs)
        if hpa_targets:
            rep.results["SRE-AT-06-C05"] = result("SRE-AT-06-C05", Result.INSUFFICIENT,
                                                  f"HPA configured for {', '.join(sorted(filter(None, hpa_targets)))}; scaling test evidence required",
                                                  refs, review=True)

    # Resilience settings in any configuration text (timeouts, retries, circuit breakers)
    text = "\n".join(it.text for it in configs)
    if text.strip():
        refs = [it.ref() for it in configs]
        has_t, has_r, has_b = bool(TIMEOUT.search(text)), bool(RETRY.search(text)), bool(BREAKER.search(text))
        rep.results["SRE-AT-05-C01"] = result("SRE-AT-05-C01", Result.MET if has_t else Result.GAP,
                                              "Timeout settings " + ("found" if has_t else "not found") + " in configuration", refs,
                                              review=True, confidence=0.6)
        res = Result.MET if (has_t and has_r) else Result.GAP
        rep.rubric["P2-04"] = result("P2-04", res,
                                     f"Configuration: timeouts={'yes' if has_t else 'no'}, retries={'yes' if has_r else 'no'}, "
                                     f"circuit breaker={'yes' if has_b else 'no'}", refs, review=True, confidence=0.6)
        if res == Result.GAP:
            missing = [n for n, ok in (("timeouts", has_t), ("retries with backoff", has_r)) if not ok]
            rep.findings.append(finding(FindingType.GAP, Risk.HIGH, f"P2-04: no {' or '.join(missing)} configured for dependencies",
                                        rubric=["P2-04"], criteria=["SRE-AT-05-C01", "SRE-AT-05-C02"], refs=refs,
                                        detector=f"{NAME}:resilience",
                                        action="Configure timeouts and retries with exponential backoff on every critical dependency (SRE-PB-05)"))
    return rep
