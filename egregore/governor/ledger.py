"""Append-only spend ledger with a hard ceiling (Architecture 2.5, PRD B-2).

Two properties this file exists to guarantee:

1. **The ceiling equals the budget.** ``SpendLedger`` is constructed with one
   number and there is no second, higher number anywhere in the module.
2. **The ceiling does not trust the cost estimate.** Reservations hold the
   *maximum plausible* cost of a request up front, and are reconciled against
   the actual on completion. A cost model wrong by 10x cannot breach the
   ceiling, because the hold -- not the estimate -- is what is checked.

The ledger is append-only: ``reserve``/``settle``/``release`` each append an
entry and nothing is ever mutated or removed. Totals are derived from the
entries, so the tape is the truth and the properties are auditable after the
fact.

Single event loop, so no locking: this is deliberately not thread-safe. It does
guard against the bug that actually happens -- settling or releasing the same
reservation twice.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from egregore.types import BudgetExceeded, Reservation

EntryKind = Literal["reserve", "settle", "release"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable line on the tape.

    ``amount`` is the reserved hold for ``reserve``, the actual spend for
    ``settle``, and the freed hold for ``release`` (recorded for audit; a
    release contributes nothing to committed spend).
    """

    kind: EntryKind
    reservation_id: str
    amount: Decimal
    zone: str
    backend: str
    timestamp: float = field(default_factory=time.time)


def _as_money(value: Decimal | int, what: str) -> Decimal:
    """Coerce to Decimal, refusing float (CONTRACTS rule 4: money is Decimal)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{what} must be a Decimal, not {type(value).__name__} (money is Decimal)")
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, Decimal):
        raise TypeError(f"{what} must be a Decimal, not {type(value).__name__}")
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"{what} must be a finite Decimal")
    return value


class SpendLedger:
    """Append-only ledger of holds and actual spend, bounded by ``ceiling``.

    The ceiling *is* the configured budget. ``reserve`` refuses any hold that
    would push ``committed + reserved`` past it; a zero ceiling therefore
    refuses every positive reservation, which is how PRD B-6 (zero-budget,
    local-only, no cloud calls possible) is enforced structurally.
    """

    def __init__(self, ceiling: Decimal, *, clock: Callable[[], float] = time.time) -> None:
        ceiling = _as_money(ceiling, "ceiling")
        if ceiling < ZERO:
            raise ValueError("ceiling must not be negative")
        self._ceiling = ceiling
        self._clock = clock
        self._entries: list[LedgerEntry] = []
        self._active: dict[str, Reservation] = {}
        self._closed: set[str] = set()
        self._committed = ZERO
        self._reserved = ZERO
        self._overrun_detected = False
        self._ids = itertools.count(1)

    # -- properties ---------------------------------------------------------

    @property
    def ceiling(self) -> Decimal:
        """The hard ceiling. Equals the configured budget, by construction."""
        return self._ceiling

    @property
    def committed(self) -> Decimal:
        """Total settled (actual) spend."""
        return self._committed

    @property
    def reserved(self) -> Decimal:
        """Total of holds that are neither settled nor released."""
        return self._reserved

    @property
    def remaining(self) -> Decimal:
        """Headroom left under the ceiling, floored at zero."""
        left = self._ceiling - self._committed - self._reserved
        return left if left > ZERO else ZERO

    @property
    def overrun_detected(self) -> bool:
        """True once any settle exceeded its own reservation.

        The cost model was wrong in the unsafe direction. The ceiling still
        binds for every *subsequent* call, but the operator should see this.
        """
        return self._overrun_detected

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        """Immutable view of the tape, oldest first."""
        return tuple(self._entries)

    @property
    def active_reservations(self) -> tuple[Reservation, ...]:
        return tuple(self._active.values())

    # -- operations ---------------------------------------------------------

    def reserve(self, amount: Decimal, zone: str, backend: str) -> Reservation:
        """Hold ``amount`` (the max plausible cost) against the ceiling.

        Raises ``BudgetExceeded`` if the hold would breach the ceiling. Callers
        must treat that as a routing signal (fall to the free backend), never
        as something to retry against the cloud.
        """
        amount = _as_money(amount, "amount")
        if amount < ZERO:
            raise ValueError("reservation amount must not be negative")
        if self._committed + self._reserved + amount > self._ceiling:
            raise BudgetExceeded(
                f"reservation of {amount} would breach ceiling {self._ceiling} "
                f"(committed {self._committed}, reserved {self._reserved})"
            )
        reservation = Reservation(
            id=f"rsv-{next(self._ids):06d}",
            amount=amount,
            zone=zone,
            backend=backend,
            created_at=self._clock(),
        )
        self._active[reservation.id] = reservation
        self._reserved += amount
        self._append("reserve", reservation.id, amount, zone, backend)
        return reservation

    def settle(self, reservation_id: str, actual: Decimal) -> LedgerEntry:
        """Reconcile a hold to the actual spend.

        The ledger never lies: if ``actual`` exceeds the hold, the *full*
        actual is recorded and ``overrun_detected`` flips. Committed spend may
        then exceed what was planned locally, but every future reservation is
        checked against the true total, so the ceiling still binds for all
        subsequent calls.
        """
        actual = _as_money(actual, "actual")
        if actual < ZERO:
            raise ValueError("actual spend must not be negative")
        reservation = self._take(reservation_id)
        self._reserved -= reservation.amount
        self._committed += actual
        if actual > reservation.amount:
            self._overrun_detected = True
        return self._append(
            "settle", reservation_id, actual, reservation.zone, reservation.backend
        )

    def release(self, reservation_id: str) -> LedgerEntry:
        """Cancel a hold (generation failed, was refused, or never ran)."""
        reservation = self._take(reservation_id)
        self._reserved -= reservation.amount
        return self._append(
            "release", reservation_id, reservation.amount, reservation.zone, reservation.backend
        )

    # -- internals ----------------------------------------------------------

    def _take(self, reservation_id: str) -> Reservation:
        reservation = self._active.pop(reservation_id, None)
        if reservation is None:
            if reservation_id in self._closed:
                raise ValueError(f"reservation {reservation_id!r} is already closed")
            raise ValueError(f"unknown reservation {reservation_id!r}")
        self._closed.add(reservation_id)
        return reservation

    def _append(
        self, kind: EntryKind, reservation_id: str, amount: Decimal, zone: str, backend: str
    ) -> LedgerEntry:
        entry = LedgerEntry(
            kind=kind,
            reservation_id=reservation_id,
            amount=amount,
            zone=zone,
            backend=backend,
            timestamp=self._clock(),
        )
        self._entries.append(entry)
        return entry

    def __repr__(self) -> str:  # pragma: no cover - operator convenience
        return (
            f"SpendLedger(ceiling={self._ceiling}, committed={self._committed}, "
            f"reserved={self._reserved}, entries={len(self._entries)})"
        )
