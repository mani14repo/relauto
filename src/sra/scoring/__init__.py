"""Scoring matrix engine."""
from ..config import load_yaml
from .engine import effective_result, rate, score_assessment  # noqa: F401


def load_matrix() -> dict:
    return load_yaml("scoring_matrix")
