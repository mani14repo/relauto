"""Detectors. Each module exposes run(bundle, ctx) -> DetectorReport.

All are deterministic; incident_clusters uses unsupervised machine learning (text vectors + clustering) and only
raises advisory findings and review flags.
"""
from . import checklists, config_checks, criteria_rules, data_rules, incident_anomalies, incident_clusters, rubric_rules, vulnerabilities
from .base import Context, DetectorReport  # noqa: F401

ALL = {
    "rubric_rules": rubric_rules.run,
    "incident_anomalies": incident_anomalies.run,
    "incident_clusters": incident_clusters.run,
    "vulnerabilities": vulnerabilities.run,
    "config_checks": config_checks.run,
    "criteria_rules": criteria_rules.run,
    "checklists": checklists.run,
    "data_rules": data_rules.run,
}
