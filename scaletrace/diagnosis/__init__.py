"""Evidence correlation and backtracking diagnosis (deterministic and transparent)."""

from scaletrace.diagnosis.backtrack import Backtracker, Ranker
from scaletrace.diagnosis.config import DiagnosisConfig
from scaletrace.diagnosis.correlate import correlate_findings
from scaletrace.diagnosis.engine import diagnose_repository, user_symptom
from scaletrace.diagnosis.symptoms import detect_symptoms

__all__ = [
    "Backtracker",
    "DiagnosisConfig",
    "Ranker",
    "correlate_findings",
    "detect_symptoms",
    "diagnose_repository",
    "user_symptom",
]
