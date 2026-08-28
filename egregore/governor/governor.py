"""The Governor facade: when to generate, and whether it may cost money.

Ties the three pieces together (Architecture 2.5):

* ``SpendLedger`` -- the hard ceiling, which equals the configured budget.
* ``CadenceSolver`` -- when each zone is next eligible, recomputed from
  remaining budget and remaining time.
* per-zone last-generation timestamps -- the state the two need to meet.

The routing contract for callers: ``authorize`` returns ``None`` rather than
raising when the ceiling refuses, because budget exhaustion is not an error.
It is the normal end state of a good night (PRD B-4): the dream continues on
the local backend, just free. Callers that see ``None`` route to the free
backend; they never retry against the cloud.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from decimal import Decimal

from egregore.config.schema import EgregoreConfig
from egregore.types import BudgetExceeded, Reservation

from .cadence import DEFAULT_MIN_INTERVAL_S, CadenceSolver
from .ledger import SpendLedger

ZERO = Decimal("0")


class Governor:
    """Decides when to generate and enforces the budget ceiling."""

    def __init__(
        self,
        ceiling: Decimal,
        solver: CadenceSolver,
        zones: Iterable[str] = (),
        *,
        cost_per_clip: Decimal,
        clock: Callable[[], float] = time.monotonic,
        throughput_floor_s: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(cost_per_clip, Decimal):
            raise TypeError("cost_per_clip must be a Decimal (money is Decimal)")
        self._clock = clock
        self.ledger = SpendLedger(ceiling, clock=clock)
        self.solver = solver
        self.cost_per_clip = cost_per_clip
        self._last_generation: dict[str, float] = {}
        self.zones: list[str] = list(zones)
        self.throughput_floor_s = throughput_floor_s

    def _throughput_floor(self) -> float:
        """Seconds the slowest-plausible backend needs to deliver one clip.

        The cadence solver is purely economic: it answers "how often can we
        afford this?", never "how often can the hardware do it?". Asking a box
        for imagery faster than it can render only grows the queue — the extra
        requests are not more imagery, just more backlog. This floor is the
        physical half of the answer, and because it is a callable it tracks a
        backend's *learned* latency as the party runs.

        Returns 0.0 when unset, which preserves the pure-budget behaviour.
        """
        if self.throughput_floor_s is None:
            return 0.0
        try:
            floor = float(self.throughput_floor_s())
        except Exception:  # a broken probe must never stall generation
            return 0.0
        return floor if math.isfinite(floor) and floor > 0.0 else 0.0

    @classmethod
    def from_config(
        cls,
        config: EgregoreConfig,
        *,
        cost_per_clip: Decimal,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        throughput_floor_s: Callable[[], float] | None = None,
    ) -> Governor:
        """Build a Governor from a party config.

        ``demo_time_scale`` compresses both the party duration and the cadence
        floor, so a demo run exercises the same arithmetic at speed. It does
        *not* compress ``throughput_floor_s``: wall-clock render time is
        physical and does not speed up because the demo clock does.
        """
        scale = float(config.demo_time_scale)
        duration_s = (config.party.duration_hours * 3600.0) / scale
        zones = [z.id for z in config.zones]
        solver = CadenceSolver(
            total_budget=config.budget.total_usd,
            party_duration_s=duration_s,
            zone_count=max(1, len(zones)),
            curve_points=config.budget.spend_curve,
            clock=clock,
            min_interval_s=min_interval_s / scale,
        )
        return cls(
            config.budget.total_usd,
            solver,
            zones,
            cost_per_clip=cost_per_clip,
            clock=clock,
            throughput_floor_s=throughput_floor_s,
        )

    # -- cadence ------------------------------------------------------------

    def start(self, at: float | None = None) -> float:
        """Start the party clock (also started lazily on first use)."""
        return self.solver.start(self._clock() if at is None else at)

    def interval_for(
        self, zone: str, now: float | None = None, *, cost_per_clip: Decimal | None = None
    ) -> float:
        """Current cadence interval for a zone, in seconds.

        ``math.inf`` from the solver means "no budget left": generation is
        still allowed, but only on the free backend, paced on the cadence
        floor. That is the interval reported here.
        """
        now = self._now(now)
        interval = self.solver.interval_for(
            zone,
            remaining_budget=self.ledger.remaining,
            now_frac=self.solver.now_frac(now),
            cost_per_clip=self.cost_per_clip if cost_per_clip is None else cost_per_clip,
        )
        interval = self.solver.min_interval_s if math.isinf(interval) else interval
        return max(interval, self._throughput_floor())

    def continuity_interval_for(
        self,
        zone: str,
        now: float | None = None,
        *,
        movement_billed_seconds: float,
        movement_cost: Decimal,
    ) -> float:
        """Cadence between movement starts in continuity mode."""
        now = self._now(now)
        interval = self.solver.continuity_interval_for(
            zone,
            remaining_budget=self.ledger.remaining,
            now_frac=self.solver.now_frac(now),
            movement_billed_seconds=movement_billed_seconds,
            movement_cost=movement_cost,
        )
        if math.isinf(interval):
            return max(self.solver.min_interval_s, float(movement_billed_seconds))
        return interval

    def should_generate(self, zone: str, now: float | None = None) -> bool:
        """True if this zone is due for fresh imagery.

        Independent of whether the generation can be *paid* for: a zero-budget
        party still generates, on the local backend, paced by the cadence
        floor. ``authorize`` is what decides whether money may be spent.
        """
        now = self._now(now)
        last = self._last_generation.get(zone)
        if last is None:
            return True
        return (now - last) >= self.interval_for(zone, now)

    def next_eligible_at(self, zone: str, now: float | None = None) -> float:
        """Unix/monotonic time at which this zone is next due."""
        now = self._now(now)
        last = self._last_generation.get(zone)
        if last is None:
            return now
        return last + self.interval_for(zone, now)

    def record_generation(self, zone: str, now: float | None = None) -> float:
        """Note that a generation started for ``zone``. Resets its cadence."""
        stamp = self._now(now)
        self._last_generation[zone] = stamp
        if zone not in self.zones:
            self.zones.append(zone)
        return stamp

    def last_generation_at(self, zone: str) -> float | None:
        return self._last_generation.get(zone)

    # -- money --------------------------------------------------------------

    def authorize(self, zone: str, backend_name: str, max_plausible: Decimal) -> Reservation | None:
        """Hold ``max_plausible`` against the ceiling, or return ``None``.

        ``None`` means "not on the cloud's dime" -- route to the free backend.
        Never raises ``BudgetExceeded``: refusal is a routing signal, not an
        error. A zero ceiling refuses everything, which is how a zero-budget
        config makes cloud calls structurally impossible (PRD B-6).
        """
        if self.ledger.ceiling <= ZERO:
            return None
        try:
            return self.ledger.reserve(max_plausible, zone, backend_name)
        except BudgetExceeded:
            return None

    def settle(self, reservation: Reservation | str, actual: Decimal) -> None:
        """Reconcile a reservation to the actual spend."""
        self.ledger.settle(self._rid(reservation), actual)

    def release(self, reservation: Reservation | str) -> None:
        """Release a reservation whose generation failed or was abandoned."""
        self.ledger.release(self._rid(reservation))

    # -- reporting ----------------------------------------------------------

    def status(self, now: float | None = None) -> dict:
        """Operator-facing spend and cadence snapshot (PRD B-5).

        Money is stringified so the dict is JSON-safe without a Decimal
        encoder. ``overrun_detected`` is a real bool.
        """
        now = self._now(now)
        ledger = self.ledger
        zones = list(dict.fromkeys([*self.zones, *self._last_generation]))
        return {
            "ceiling": str(ledger.ceiling),
            "committed": str(ledger.committed),
            "reserved": str(ledger.reserved),
            "remaining": str(ledger.remaining),
            "projected_total": str(ledger.committed + ledger.reserved),
            "overrun_detected": ledger.overrun_detected,
            "active_reservations": len(ledger.active_reservations),
            "entry_count": len(ledger.entries),
            "party_frac": round(self.solver.now_frac(now), 6),
            "curve_rate": round(self.solver.rate(self.solver.now_frac(now)), 6),
            "zones": {
                zone: {
                    "last_generation_at": self._last_generation.get(zone),
                    "next_eligible_at": self.next_eligible_at(zone, now),
                    "interval_s": round(self.interval_for(zone, now), 3),
                }
                for zone in zones
            },
        }

    # -- internals ----------------------------------------------------------

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    @staticmethod
    def _rid(reservation: Reservation | str) -> str:
        return reservation if isinstance(reservation, str) else reservation.id

    def __repr__(self) -> str:  # pragma: no cover - operator convenience
        return (
            f"Governor(ceiling={self.ledger.ceiling}, committed={self.ledger.committed}, "
            f"zones={len(self.zones)})"
        )
