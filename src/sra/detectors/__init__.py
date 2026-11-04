"""Deterministic detectors. Each module exposes run(bundle, ctx) -> DetectorReport."""
from . import config_checks, incident_anomalies, rubric_rules, vulnerabilities
from .base import Context, DetectorReport  # noqa: F401

ALL = {
    "rubric_rules": rubric_rules.run,
    "incident_anomalies": incident_anomalies.run,
    "vulnerabilities": vulnerabilities.run,
    "config_checks": config_checks.run,
}
