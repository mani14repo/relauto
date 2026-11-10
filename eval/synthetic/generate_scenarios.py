"""Draft new synthetic scenarios with a Foundry model, render them, and check the bot against each answer key.

    python eval/synthetic/generate_scenarios.py                       # the four built-in briefs
    python eval/synthetic/generate_scenarios.py --brief "Tier 2 cloud-only prescription refill API, mostly healthy"
    python eval/synthetic/generate_scenarios.py --from-spec eval/scenarios/<slug>/spec.yaml   # re-render, no model call

The model only writes the spec: names, profile and which defects to plant, chosen from the fixed list in
scenario_kit.DEFECTS. The evidence files and the answer key are produced from the spec by code, so the
key is right whatever the model picks. Each scenario lands in eval/scenarios/<slug>/ (gitignored) with
its spec.yaml, so it can be re-rendered later without calling the model again.
Settings: FOUNDRY_ENDPOINT, FOUNDRY_API_KEY, APP_MODEL, APP_EFFORT (see foundry.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import yaml

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[1])]  # eval/synthetic, eval, repo root
from foundry import FoundryError, chat_json, setting  # noqa: E402
from scenario_kit import DEFECTS, PROFILE_FLAGS, Spec, render  # noqa: E402

BRIEFS = [
    "A healthy, well-run Tier 2 cloud vaccine appointment scheduling service on Kubernetes with CI/CD and seasonal peaks. It should come "
    "out Ready: plant no defects.",
    "A Tier 1 hybrid pharmacy claims adjudication service (NCPDP claims to benefit managers) with good engineering but weak ITSM hygiene ("
    "problem and change records) and a CVE backlog.",
    "A Tier 3 on-premises internal drug recall lookup tool, not containerized, no CI/CD, with neglected documentation.",
    "A Tier 2 cloud specialty-pharmacy shipment tracking API that recently had a bad release; fine otherwise apart from one or two "
    "Kubernetes configuration issues.",
]

SYSTEM = f"""You design synthetic applications for testing a Site Reliability Assessment tool at a US healthcare company
that runs retail pharmacies, mail-order and specialty pharmacy, retail health clinics and pharmacy benefits.
Applications should be the kind such a company runs (e-prescribing, claims, eligibility, dispensing, immunizations,
clinic scheduling, patient messaging, supply chain), and the invented names and incident descriptions should read
like that domain. Use real healthcare standards where they fit (NCPDP, X12, HL7, FHIR, DSCSA), but never real
patient data.
Given a brief, return one application spec as JSON. Rules:
- Invent plausible names: application name, owner team, support group, Kubernetes service name (lowercase-dashes),
  database name, one upstream dependency (another internal application), four distinct routine incident short
  descriptions, and one recurring issue (a short description different from the four).
- Choose defects only from this list, and only ones that fit the brief (an empty list means a healthy application):
{chr(10).join(f'  {k}: {d.summary}' + (f' (requires {", ".join(d.needs)})' if d.needs else '') for k, d in DEFECTS.items())}
- Kubernetes defects need containerized=true; pipeline defects need has_cicd=true.
- Never combine dependencies_unmapped with dependencies_stale, or recovery_untested with dr_test_overdue.
- Do not invent real company, vendor or person names. Keep everything fictional."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["slug", "name", "description", "tier", "priority_class", "trigger", "hosting", "flags", "owner_team",
                 "support_group", "service", "database", "dependency", "incident_themes", "recurring_issue", "defects"],
    "properties": {
        "slug": {"type": "string", "description": "lowercase_with_underscores"},
        "name": {"type": "string"}, "description": {"type": "string"},
        "tier": {"type": "integer", "enum": [1, 2, 3]},
        "priority_class": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "trigger": {"type": "string",
                    "enum": ["new_production", "major_change", "architecture_change", "deployment", "post_incident", "periodic"]},
        "hosting": {"type": "string", "enum": ["cloud", "on_prem", "hybrid"]},
        "flags": {"type": "object", "additionalProperties": False, "required": list(PROFILE_FLAGS),
                  "properties": {f: {"type": "boolean"} for f in PROFILE_FLAGS}},
        "owner_team": {"type": "string"}, "support_group": {"type": "string"}, "service": {"type": "string"},
        "database": {"type": "string"}, "dependency": {"type": "string"},
        "incident_themes": {"type": "array", "items": {"type": "string"}},
        "recurring_issue": {"type": "string"},
        "defects": {"type": "array", "items": {"type": "string", "enum": list(DEFECTS)}},
    },
}


def say(msg: str = "", end: str = "\n") -> None:
    print(msg, end=end, flush=True)


@contextmanager
def waiting(label: str, every: float = 10.0):
    """Show that a slow step is still running: a live timer on a terminal, a line every `every` seconds otherwise."""
    done, start = threading.Event(), time.monotonic()
    tty = sys.stdout.isatty()

    def tick():
        while not done.wait(1.0 if tty else every):
            secs = int(time.monotonic() - start)
            if tty:
                say(f"\r    {label}... {secs}s", end="")
            else:
                say(f"    still {label} ({secs}s)")

    t = threading.Thread(target=tick, daemon=True)
    say(f"    {label}...", end="" if tty else "\n")
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join()
        if tty:
            say("\r" + " " * 70 + "\r", end="")


def draft(brief: str, attempts: int = 2) -> Spec:
    """Ask the model for a spec; retry once with the validation errors if it breaks a rule."""
    prompt, errs = f"Brief: {brief}", []
    for attempt in range(1, attempts + 1):
        t0 = time.monotonic()
        with waiting(f"waiting for {setting('APP_MODEL', 'the model')}" + (f" (attempt {attempt}/{attempts})" if attempt > 1 else "")):
            data = chat_json(SYSTEM, prompt, SCHEMA, name="scenario_spec")
        spec = Spec.load(data)
        errs = spec.validate()
        if not errs:
            say(f"    spec received in {time.monotonic() - t0:.0f}s: {spec.name} (Tier {spec.tier}, {spec.hosting}, "
                f"{len(spec.defects)} defect{'s' if len(spec.defects) != 1 else ''})")
            return spec
        say(f"    spec had problems ({'; '.join(errs)}); asking the model to correct it")
        prompt = f"Brief: {brief}\n\nYour previous spec was invalid: {'; '.join(errs)}. Return a corrected spec."
    raise ValueError(f"model could not produce a valid spec: {'; '.join(errs)}")


def check(out: Path) -> dict:
    from eval.run_eval import evaluate
    r = evaluate(out, use_llm=False, model=None)
    r["ok"] = bool(r["rating_ok"] and not r["missed"] and not r["unexpected_gaps"]
                   and r["p1_gap_recall"] == 1 and r["p2_gap_recall"] == 1)
    return r


def build(spec: Spec) -> dict:
    """Render the evidence, then run the bot against the answer key, reporting each step."""
    say("    writing evidence files...", end="")
    out = render(spec)
    say(f" {sum(1 for _ in (out / 'evidence').iterdir())} files in {out.relative_to(HERE.parents[1])}")
    expected = yaml.safe_load((out / "expected.yaml").read_text(encoding="utf-8"))["rating"]
    say("    running the assessment against the answer key...", end="")
    r = check(out)
    if r["ok"]:
        say(f" OK: rated {expected}, as expected")
    else:
        say(" MISMATCH")
        if not r["rating_ok"]:
            say(f"      rating differs from the expected {expected}")
        for m in r["missed"]:
            say(f"      missed planted problem: {m[0]} '{m[1]}'")
        for g in r["unexpected_gaps"]:
            say(f"      unexpected rubric gap: {g}")
    return {"scenario": spec.slug, "name": spec.name, "tier": spec.tier, "defects": spec.defects, "expected_rating": expected,
            "bot_rating_matches": r["rating_ok"], "missed": r["missed"], "unexpected_gaps": r["unexpected_gaps"],
            "ok": r["ok"], "folder": str(out)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", action="append", help="describe the application to generate (repeatable)")
    ap.add_argument("--from-spec", action="append", type=Path, help="re-render an existing spec.yaml without calling the model")
    ap.add_argument("--json", action="store_true", help="also print the full result of each scenario as JSON")
    args = ap.parse_args()

    started = time.monotonic()
    jobs = [("spec", p) for p in (args.from_spec or [])] or [("brief", b) for b in (args.brief or BRIEFS)]
    if not args.from_spec:
        host = urlparse(setting("FOUNDRY_ENDPOINT", "")).hostname or "Foundry (FOUNDRY_ENDPOINT not set)"
        effort = setting("APP_EFFORT", "")
        say(f"Generating {len(jobs)} scenario(s) with {setting('APP_MODEL', '?')}"
            + (f", effort {effort}" if effort else "") + f", on {host}")
        say("Each model call usually takes from several seconds to a few minutes.\n")
    results, skipped = [], 0
    for i, (kind, item) in enumerate(jobs, 1):
        if kind == "spec":
            say(f"[{i}/{len(jobs)}] Re-rendering {item}")
            spec = Spec.load(yaml.safe_load(Path(item).read_text(encoding="utf-8")))
        else:
            say(f"[{i}/{len(jobs)}] {item[:90]}{'...' if len(item) > 90 else ''}")
            try:
                spec = draft(item)
            except (FoundryError, ValueError) as e:
                skipped += 1
                say(f"    SKIPPED: {e}")
                continue
        results.append(build(spec))
        say()

    ok = sum(r["ok"] for r in results)
    say(f"Done in {time.monotonic() - started:.0f}s: {ok} of {len(results)} scenario(s) match their answer key"
        + (f", {skipped} skipped" if skipped else "") + ".")
    for r in results:
        say(f"  {'ok      ' if r['ok'] else 'MISMATCH'}  {r['scenario']:<32} {r['expected_rating']:<12} "
            f"{', '.join(r['defects']) or 'no defects'}")
    if args.json:
        say(json.dumps(results, indent=1))
    sys.exit(1 if skipped or ok < len(results) else 0)


if __name__ == "__main__":
    main()
