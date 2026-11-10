"""Generate the synthetic scenario once per test session (it is not committed: it contains fake secrets
that exist to exercise secret detection, and would trip repository secret scanning)."""
import os
import runpy
from pathlib import Path

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"

# Checklist line suggestions use an NLI model only if it is in the local cache; keep tests the same on every machine
# (and fast). Tests of the suggestions turn them on explicitly.
os.environ["SRA_SUGGEST"] = "off"


def pytest_sessionstart(session):
    if not (SCENARIO / "expected.yaml").exists():
        runpy.run_path(str(SCENARIO.parents[1] / "synthetic" / "make_scenario.py"), run_name="__main__")
