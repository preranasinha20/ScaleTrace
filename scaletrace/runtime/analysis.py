"""Growth analysis of runtime metrics across input sizes.

The growth exponent ``b`` is the least-squares slope of log(value) against log(size):
``value ~ size^b``. ``b ~ 0`` means constant, ``b ~ 1`` linear (e.g. one query per row),
``b > 1`` super-linear. Count metrics get +1 before the log so a constant 0 stays defined.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def growth_exponent(sizes: Sequence[float], values: Sequence[float | None], offset: float = 0.0) -> float | None:
    points = [
        (math.log(s), math.log(v + offset))
        for s, v in zip(sizes, values)
        if v is not None and s > 0 and v + offset > 0
    ]
    if len({x for x, _ in points}) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    var_x = sum((x - mean_x) ** 2 for x, _ in points)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return cov / var_x if var_x else None


def growth_class(exponent: float | None) -> str:
    if exponent is None:
        return "unknown"
    if exponent < 0.3:
        return "constant"
    if exponent < 0.8:
        return "sub-linear"
    if exponent < 1.5:
        return "linear"
    return "super-linear"


def describe_series(sizes: Sequence[int], values: Sequence[float | None], unit: str, digits: int = 1) -> str:
    parts = [f"{v:.{digits}f}{unit} @ {s}" for s, v in zip(sizes, values) if v is not None]
    return ", ".join(parts) if parts else "n/a"
