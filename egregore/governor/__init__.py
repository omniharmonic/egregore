"""GOVERNOR -- budget ceiling and generation cadence (Architecture 2.5).

The ceiling equals the configured budget, and it holds even when the cost
model is wrong by 10x, because reservations hold the maximum plausible cost
and are reconciled to actuals.
"""

from .cadence import DEFAULT_MIN_INTERVAL_S, CadenceSolver
from .curve import Curve, curve_integral, normalize_curve, rate_at
from .governor import Governor
from .ledger import LedgerEntry, SpendLedger

__all__ = [
    "DEFAULT_MIN_INTERVAL_S",
    "CadenceSolver",
    "Curve",
    "Governor",
    "LedgerEntry",
    "SpendLedger",
    "curve_integral",
    "normalize_curve",
    "rate_at",
]
