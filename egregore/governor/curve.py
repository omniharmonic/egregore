"""Spend curve normalization and interpolation (Architecture 2.5).

An operator authors a piecewise-linear multiplier over the night:

    spend_curve:
      - { at: "0%",   rate: 0.5 }   # arrival -- sparse
      - { at: "60%",  rate: 1.5 }   # peak
      - { at: "100%", rate: 0.3 }   # wind-down

A curve whose time-weighted mean is not 1.0 silently over- or under-spends the
budget. So the curve is normalized at load time: its integral over [0, 1] is
computed by trapezoid rule (with the first and last points extended flat to 0%
and 100% when the endpoints are missing) and every rate divided through by that
mean. The operator controls the *distribution* of spend over the night; the
*total* is the budget, always.
"""

from __future__ import annotations

from collections.abc import Sequence

from egregore.config.schema import SpendCurvePoint

Curve = list[tuple[float, float]]

FLAT_RATE = 1.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def curve_integral(curve: Sequence[tuple[float, float]]) -> float:
    """Time-weighted mean of the curve over [0, 1] (trapezoid rule).

    The domain is exactly one unit wide, so the integral *is* the mean. The
    first and last points are extended flat to 0.0 and 1.0 if the curve does
    not already reach the endpoints.
    """
    if not curve:
        return FLAT_RATE
    pts = list(curve)
    if pts[0][0] > 0.0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < 1.0:
        pts.append((1.0, pts[-1][1]))
    total = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        total += (x1 - x0) * (y0 + y1) / 2.0
    return total


def normalize_curve(points: Sequence[SpendCurvePoint] | None) -> Curve:
    """Normalize an operator-authored curve so its time-weighted mean is 1.0.

    Returns ``(frac, rate)`` pairs sorted by ``frac``, with fractions clamped
    into [0, 1] and duplicate fractions collapsed (last wins). An empty or
    missing curve returns ``[]``, which ``rate_at`` reads as a flat 1.0.
    """
    if not points:
        return []
    collapsed: dict[float, float] = {}
    for point in points:
        rate = float(point.rate)
        if rate <= 0.0:
            raise ValueError("spend curve rates must be positive")
        collapsed[_clamp01(point.frac)] = rate
    raw: Curve = sorted(collapsed.items())
    mean = curve_integral(raw)
    if mean <= 0.0:  # pragma: no cover - unreachable while rates are positive
        raise ValueError("spend curve integral must be positive")
    return [(frac, rate / mean) for frac, rate in raw]


def rate_at(curve: Sequence[tuple[float, float]], frac: float) -> float:
    """Linear interpolation of the curve at ``frac``, clamped to [0, 1].

    An empty curve is flat 1.0. Outside the curve's own span the nearest
    endpoint's rate is held flat, matching how the integral is computed.
    """
    if not curve:
        return FLAT_RATE
    frac = _clamp01(frac)
    if frac <= curve[0][0]:
        return curve[0][1]
    if frac >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:], strict=False):
        if x0 <= frac <= x1:
            span = x1 - x0
            if span <= 0.0:  # pragma: no cover - collapsed upstream
                return y1
            return y0 + (y1 - y0) * (frac - x0) / span
    return curve[-1][1]  # pragma: no cover - unreachable
