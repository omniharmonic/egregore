"""Cadence solver -- how often a zone may generate (Architecture 2.5).

Base formula:

    interval_s = (party_duration_s * zone_count * cost_per_clip) / total_budget

Worked example from the architecture: a 4-hour party, 4 zones, $150, Veo 3.1
Lite 8s video-only clips at ~$0.24 gives (14400 * 4 * 0.24) / 150 = 92 s per
zone.

Two corrections on top of the base formula:

* **Under-spend redistribution.** The interval is recomputed from *remaining*
  budget and *remaining* time on every cycle, never from the initial plan.
  Validator skips, failovers and outages push actual spend below plan; feeding
  the leftovers back in is what keeps final spend inside the 10%-under band of
  PRD success criterion 4 instead of stranding budget.
* **Spend curve.** The normalized rate at the current point in the night
  divides the interval: a higher rate means imagery changes faster.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from decimal import Decimal

from egregore.config.schema import SpendCurvePoint

from .curve import Curve, normalize_curve, rate_at

ZERO = Decimal("0")

DEFAULT_MIN_INTERVAL_S = 30.0


class CadenceSolver:
    """Solves for the seconds between generations in one zone.

    ``min_interval_s`` is a floor, not a target: it stops a large budget (or a
    cheap backend) from turning the generation loop into a spin loop, and it is
    what paces free local generation when there is no budget left at all.
    """

    def __init__(
        self,
        total_budget: Decimal,
        party_duration_s: float,
        zone_count: int,
        curve_points: Sequence[SpendCurvePoint] | None = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    ) -> None:
        if not isinstance(total_budget, Decimal):
            raise TypeError("total_budget must be a Decimal (money is Decimal)")
        if total_budget < ZERO:
            raise ValueError("total_budget must not be negative")
        if party_duration_s <= 0:
            raise ValueError("party_duration_s must be positive")
        if zone_count < 1:
            raise ValueError("zone_count must be at least 1")
        if min_interval_s <= 0:
            raise ValueError("min_interval_s must be positive")
        self.total_budget = total_budget
        self.party_duration_s = float(party_duration_s)
        self.zone_count = int(zone_count)
        self.curve: Curve = normalize_curve(curve_points)
        self.min_interval_s = float(min_interval_s)
        self._clock = clock
        self._started_at: float | None = None

    # -- clock helpers ------------------------------------------------------

    def start(self, at: float | None = None) -> float:
        """Mark the start of the party. Idempotent-ish: last call wins."""
        self._started_at = self._clock() if at is None else float(at)
        return self._started_at

    @property
    def started_at(self) -> float | None:
        return self._started_at

    def now_frac(self, now: float | None = None) -> float:
        """Fraction of the party elapsed, clamped to [0, 1].

        The first call starts the clock if ``start()`` was never called.
        """
        current = self._clock() if now is None else float(now)
        if self._started_at is None:
            self.start(current)
        elapsed = current - (self._started_at or current)
        frac = elapsed / self.party_duration_s
        return 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)

    def remaining_time_s(self, now_frac: float) -> float:
        return max(0.0, self.party_duration_s * (1.0 - min(max(now_frac, 0.0), 1.0)))

    def rate(self, now_frac: float) -> float:
        """Normalized spend-curve multiplier at this point in the night."""
        return rate_at(self.curve, now_frac)

    # -- the solver ---------------------------------------------------------

    def interval_for(
        self,
        zone: str,
        *,
        remaining_budget: Decimal,
        now_frac: float,
        cost_per_clip: Decimal,
    ) -> float:
        """Seconds this zone should wait between clip generations.

        Returns ``math.inf`` when there is nothing left to spend or no time
        left to spend it in -- the signal to route to the free local backend,
        which the Governor then paces on ``min_interval_s``.
        """
        del zone  # per-zone policy is uniform today; kept for the signature
        return self._solve(
            remaining_budget=remaining_budget,
            now_frac=now_frac,
            line_item_cost=cost_per_clip,
            floor_s=self.min_interval_s,
            what="cost_per_clip",
        )

    def continuity_interval_for(
        self,
        zone: str,
        *,
        remaining_budget: Decimal,
        now_frac: float,
        movement_billed_seconds: float,
        movement_cost: Decimal,
    ) -> float:
        """Seconds between *movement starts* in continuity mode.

        The clip-denominated base formula does not apply here: a continuity
        movement is one budget line item worth ``movement_cost`` for
        ``movement_billed_seconds`` of billed video, assembled across ~21 calls
        (Architecture 3.2). So the same formula runs with the movement as the
        unit::

            interval_s = (remaining_time_s * zone_count * movement_cost)
                         / remaining_budget / rate(now_frac)

        floored at ``max(min_interval_s, movement_billed_seconds)``. The extra
        floor is physical, not economic: a movement occupies its own billed
        seconds of screen time, and chains grow serially at a fraction of real
        time, so starting movements closer together than that buys video the
        loop cannot consume.
        """
        del zone
        if movement_billed_seconds <= 0:
            raise ValueError("movement_billed_seconds must be positive")
        return self._solve(
            remaining_budget=remaining_budget,
            now_frac=now_frac,
            line_item_cost=movement_cost,
            floor_s=max(self.min_interval_s, float(movement_billed_seconds)),
            what="movement_cost",
        )

    # -- internals ----------------------------------------------------------

    def _solve(
        self,
        *,
        remaining_budget: Decimal,
        now_frac: float,
        line_item_cost: Decimal,
        floor_s: float,
        what: str,
    ) -> float:
        if not isinstance(remaining_budget, Decimal):
            raise TypeError("remaining_budget must be a Decimal (money is Decimal)")
        if not isinstance(line_item_cost, Decimal):
            raise TypeError(f"{what} must be a Decimal (money is Decimal)")
        if line_item_cost < ZERO:
            raise ValueError(f"{what} must not be negative")
        if remaining_budget <= ZERO:
            return math.inf
        remaining_time = self.remaining_time_s(now_frac)
        if remaining_time <= 0.0:
            return math.inf
        if line_item_cost == ZERO:
            # A free backend: nothing to meter, so pace on the floor alone.
            return min(floor_s, remaining_time)
        rate = self.rate(now_frac)
        if rate <= 0.0:  # pragma: no cover - normalize_curve rejects this
            return math.inf
        cost_ratio = float(line_item_cost / remaining_budget)
        interval = remaining_time * self.zone_count * cost_ratio / rate
        interval = max(floor_s, interval)
        return min(interval, remaining_time)
