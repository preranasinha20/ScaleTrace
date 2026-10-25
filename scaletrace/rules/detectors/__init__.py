"""Static detectors, one module per rule.

To add a rule: implement a ``Detector`` subclass with ``rule_id`` matching the rulebook and
add it to ``DETECTORS``. Rule metadata (name, severity, false positives, ...) always comes
from the rulebook, never from the detector.
"""

from __future__ import annotations

from scaletrace.rules.detectors.base import Detector, RawMatch
from scaletrace.rules.detectors.cmp_001 import ExpensiveOperationInLoopDetector
from scaletrace.rules.detectors.db_001 import DbQueryInLoopDetector
from scaletrace.rules.detectors.db_003 import OrmNPlusOneDetector
from scaletrace.rules.detectors.ext_001 import ExternalCallInLoopDetector
from scaletrace.rules.detectors.mem_001 import FullMaterializationDetector
from scaletrace.rules.detectors.mem_002 import UnboundedAccumulationDetector

DETECTORS: dict[str, type[Detector]] = {
    detector.rule_id: detector
    for detector in (
        DbQueryInLoopDetector,
        OrmNPlusOneDetector,
        ExpensiveOperationInLoopDetector,
        FullMaterializationDetector,
        UnboundedAccumulationDetector,
        ExternalCallInLoopDetector,
    )
}

__all__ = ["DETECTORS", "Detector", "RawMatch"]
