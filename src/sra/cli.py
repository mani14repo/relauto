"""Command line: compile the catalog, propose templates, run an assessment offline.

    sra catalog                                   # compile docs/OIPReqs -> catalog/
    sra select  <evidence-dir> [--profile p.yaml]  # show proposed templates with reasons
    sra assess  <evidence-dir> [--profile p.yaml] [--out out/<app>] [--as-of 2026-11-04] [--llm]
    sra serve [--build]                            # UI, API and scheduler on :8080 (one process, as on CAP); --build runs npm first
    sra worker                                     # Kafka worker (optional: only when KAFKA_BOOTSTRAP_SERVERS is set)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml


def _load_profile(evidence_dir: Path, profile: str | None):
    from .selection.profile import ApplicationProfile
    p = Path(profile) if profile else evidence_dir / "profile.yaml"
    if not p.exists():
        sys.exit(f"profile not found: {p} (pass --profile)")
    return ApplicationProfile.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))


def _evidence_root(d: Path) -> Path:
    """A scenario folder keeps its evidence in evidence/, next to profile.yaml; a plain folder is the evidence."""
    return d / "evidence" if (d / "evidence").is_dir() else d


def _bundle(evidence_dir: Path, as_of: datetime):
    from .evidence.bundle import EvidenceBundle
    b = EvidenceBundle()
    root = _evidence_root(evidence_dir)
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.name not in ("profile.yaml", "expected.yaml"):
            b.ingest(f.relative_to(root).as_posix(), f.read_bytes(), as_of=as_of)
    return b


def cmd_select(args):
    from .pipeline import propose_templates
    d = Path(args.evidence)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    profile, cands = propose_templates(_load_profile(d, args.profile), _bundle(d, as_of), as_of)
    for c in cands:
        mark = "✔" if c.selected else " "
        score = "always" if c.always else f"{c.score:g}"
        print(f"{mark} {c.template_id:11} {score:>6}  {c.title}  — {'; '.join(c.reasons) or 'no matching rule'}")


def cmd_assess(args):
    from .catalog import load_rubric, load_templates
    from .catalog.compiler import template_path
    from .models import ApplicationRef, Assessment, SignOff
    from .pipeline import propose_templates, run_assessment
    from .report.docx_fill import fill_template
    from .report.summary import build_summary
    from .scoring import load_matrix

    d = Path(args.evidence)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()
    bundle = _bundle(d, as_of)
    profile, cands = propose_templates(_load_profile(d, args.profile), bundle, as_of)
    selected = args.templates.split(",") if args.templates else [c.template_id for c in cands if c.selected]
    templates = load_templates()
    a = Assessment(application=ApplicationRef(name=profile.name, cmdb_id=profile.cmdb_id), assessor=args.assessor,
                   templates=selected, assessment_date=as_of.date())
    a.signoffs = [SignOff(role=r) for r in templates["SRE-AT-01"].signoff_roles]
    evaluator = None
    if args.llm:
        from .llm.evaluate import Evaluator
        evaluator = Evaluator.from_settings()
    sc, gate = run_assessment(a, bundle, profile, as_of, evaluator)

    out = Path(args.out or f"out/{profile.name.replace(' ', '_')}")
    out.mkdir(parents=True, exist_ok=True)
    for tid in selected:
        t = templates[tid]
        fill_template(t, template_path(t), a, out / f"{tid} — {a.application.name}.docx")
    build_summary(a, sc, templates, load_rubric(), load_matrix(), out / f"Assessment Summary — {a.application.name}.docx")
    (out / "assessment.json").write_text(a.model_dump_json(indent=1), encoding="utf-8")
    (out / "scorecard.json").write_text(sc.model_dump_json(indent=1), encoding="utf-8")

    print(f"Application   {profile.name}  (tier {profile.tier}, trigger {profile.trigger.value})")
    print(f"Templates     {', '.join(selected)}")
    print(f"Rating        {sc.rating} — {sc.rating_outcome}")
    print(f"Index         {sc.index}  maturity L{sc.maturity_level} {sc.maturity_name}{' (capped)' if sc.maturity_capped else ''}")
    print(f"Domains       {', '.join(f'{k.value} {v}' for k, v in sc.domain_scores.items())}")
    print(f"P1 gaps       {', '.join(sc.p1_gaps) or '-'}")
    print(f"P2 gaps       {', '.join(sc.p2_gaps) or '-'}")
    by_type = {}
    for f in a.findings:
        by_type[f.type.value] = by_type.get(f.type.value, 0) + 1
    print(f"Findings      {len(a.findings)} ({', '.join(f'{v} {k}' for k, v in sorted(by_type.items()))})")
    print(f"Coverage      {sc.evidence_coverage_pct}% of scorable criteria have direct evidence")
    print(f"Gate          {'OK' if gate.ok else 'BLOCKED'}: {len(gate.blockers)} blockers, {len(gate.warnings)} warnings")
    for b in gate.blockers[:15]:
        print(f"  - {b}")
    if len(gate.blockers) > 15:
        print(f"  … {len(gate.blockers) - 15} more")
    print(f"Output        {out.resolve()}")


def build_web() -> None:
    """npm install (first time only) + npm run build in web/, so the API serves a fresh web/dist."""
    import shutil
    import subprocess

    from .config import ROOT
    web = ROOT / "web"
    npm = shutil.which("npm")          # resolves npm.cmd on Windows
    if not npm:
        sys.exit("npm not found on PATH; install Node.js or run without --build")
    steps = ([] if (web / "node_modules").exists() else [[npm, "install", "--no-audit", "--no-fund"]]) + [[npm, "run", "build"]]
    for cmd in steps:
        print(f"[web] {' '.join(cmd[1:])}", flush=True)
        if subprocess.run(cmd, cwd=web).returncode != 0:
            sys.exit(f"web build failed: npm {' '.join(cmd[1:])}; API not started")


def cmd_ingest(args) -> None:
    """The ingestion service as its own process: refresh the chosen sources, then ingest everything waiting."""
    import json
    import logging
    import time

    from .knowledge.jobs import run_job
    from .store import store_from_settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = store_from_settings()
    if not hasattr(store, "knowledge"):
        sys.exit("the knowledge store needs PostgreSQL: set DATABASE_URL (or the CAP_POSTGRES_* settings)")
    if store.init_error:
        sys.exit(f"database unavailable: {store.init_error}")
    job = {"job": "sync" if (args.standards or args.servicenow) else "ingest", "standards": args.standards,
           "servicenow": args.servicenow, "tables": [t.strip() for t in args.tables.split(",")] if args.tables else None}
    while True:
        print(json.dumps(run_job(store, job), indent=1, default=str), flush=True)
        if not args.every:
            break
        time.sleep(args.every)
    store.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    for name in ("select", "assess"):
        p = sub.add_parser(name)
        p.add_argument("evidence")
        p.add_argument("--profile")
        p.add_argument("--as-of")
        if name == "assess":
            p.add_argument("--out")
            p.add_argument("--assessor", default="")
            p.add_argument("--templates", help="comma-separated override of the proposed templates")
            p.add_argument("--llm", action="store_true", help="evaluate remaining criteria with the local LLM")
    sp = sub.add_parser("serve")
    sp.add_argument("--build", action="store_true", help="run the web UI build (npm) before starting the API")
    sub.add_parser("worker")
    ip = sub.add_parser("ingest", help="run the ingestion service: refresh sources and ingest waiting documents (needs PostgreSQL)")
    ip.add_argument("--standards", action="store_true", help="register the OIPReqs standards, playbooks and templates")
    ip.add_argument("--servicenow", action="store_true", help="sync ServiceNow (SERVICENOW_INSTANCE_URL and credentials)")
    ip.add_argument("--tables", help="comma-separated ServiceNow tables (default: all in config/sources.yaml)")
    ip.add_argument("--every", type=int, metavar="SECONDS", help="keep running: repeat after this many seconds")
    args = ap.parse_args(argv)
    if args.cmd == "catalog":
        from .catalog import compile_catalog
        ts = compile_catalog()
        print(f"compiled {len(ts)} templates, {sum(len(t.criteria) for t in ts)} criteria")
    elif args.cmd == "select":
        cmd_select(args)
    elif args.cmd == "assess":
        cmd_assess(args)
    elif args.cmd == "serve":
        # One process, as OIP runs on CAP: UI, API and /healthz on :8080, jobs in-process (unless Kafka is set),
        # and the scheduler thread (sra/scheduler.py). Run a single replica.
        import uvicorn
        from .config import setting
        if args.build:
            build_web()
        from .api.app import app
        from .scheduler import start_from_settings
        start_from_settings(app.state.store, kafka=app.state.kafka)
        uvicorn.run(app, host="0.0.0.0", port=int(setting("PORT", "8080")))
    elif args.cmd == "worker":
        import asyncio
        from .worker import main as worker_main
        asyncio.run(worker_main())
    elif args.cmd == "ingest":
        cmd_ingest(args)


if __name__ == "__main__":
    main()
