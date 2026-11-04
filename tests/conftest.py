"""Generate the synthetic scenario once per test session (it is not committed: it contains fake secrets
that exist to exercise secret detection, and would trip repository secret scanning)."""
import runpy
from pathlib import Path

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"


def pytest_sessionstart(session):
    if not (SCENARIO / "expected.yaml").exists():
        runpy.run_path(str(SCENARIO.parents[1] / "synthetic" / "make_scenario.py"), run_name="__main__")
