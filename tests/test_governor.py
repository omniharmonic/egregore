"""Governor tests -- the ceiling, the curve, the cadence.

The load-bearing ones are the adversarial ceiling tests: PRD B-2 says the
system must be structurally incapable of exceeding the budget, and that this
must not depend on the accuracy of its own cost estimates.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest

from egregore.config.schema import (
    BudgetConfig,
    EgregoreConfig,
    PartyConfig,
    SpendCurvePoint,
    ZoneConfig,
)
from egregore.governor import (
    CadenceSolver,
    Governor,
    SpendLedger,
    curve_integral,
    normalize_curve,
    rate_at,
)
from egregore.types import BudgetExceeded, Reservation


class FakeClock:
    """Monotonic-ish clock the tests drive by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


def points(*pairs: tuple[str, float]) -> list[SpendCurvePoint]:
    return [SpendCurvePoint(at=at, rate=rate) for at, rate in pairs]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_reserve_then_settle_happy_path() -> None:
    ledger = SpendLedger(Decimal("10.00"))
    reservation = ledger.reserve(Decimal("1.60"), "main", "veo")

    assert isinstance(reservation, Reservation)
    assert ledger.reserved == Decimal("1.60")
    assert ledger.committed == Decimal("0")
    assert ledger.remaining == Decimal("8.40")

    ledger.settle(reservation.id, Decimal("1.20"))

    assert ledger.reserved == Decimal("0")
    assert ledger.committed == Decimal("1.20")
    assert ledger.remaining == Decimal("8.80")
    assert ledger.overrun_detected is False
    assert [e.kind for e in ledger.entries] == ["reserve", "settle"]
    assert ledger.entries[0].zone == "main"
    assert ledger.entries[0].backend == "veo"
    assert isinstance(ledger.entries, tuple)


def test_ceiling_refuses_at_the_exact_boundary() -> None:
    ledger = SpendLedger(Decimal("10.00"))

    # Reserving up to exactly the ceiling is fine.
    ledger.reserve(Decimal("9.99"), "main", "veo")
    ledger.reserve(Decimal("0.01"), "main", "veo")
    assert ledger.remaining == Decimal("0")

    # One cent more is not.
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("0.01"), "main", "veo")


def test_ceiling_binds_against_committed_spend_too() -> None:
    ledger = SpendLedger(Decimal("5.00"))
    reservation = ledger.reserve(Decimal("5.00"), "main", "veo")
    ledger.settle(reservation.id, Decimal("5.00"))

    assert ledger.reserved == Decimal("0")
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("0.01"), "main", "veo")


def test_release_frees_headroom() -> None:
    ledger = SpendLedger(Decimal("2.00"))
    first = ledger.reserve(Decimal("1.60"), "main", "veo")
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("1.60"), "main", "veo")

    ledger.release(first.id)

    assert ledger.reserved == Decimal("0")
    assert ledger.committed == Decimal("0")
    assert ledger.remaining == Decimal("2.00")
    second = ledger.reserve(Decimal("1.60"), "main", "veo")
    assert second.id != first.id
    assert [e.kind for e in ledger.entries] == ["reserve", "release", "reserve"]


def test_double_settle_and_unknown_ids_raise() -> None:
    ledger = SpendLedger(Decimal("10.00"))
    reservation = ledger.reserve(Decimal("1.00"), "main", "veo")
    ledger.settle(reservation.id, Decimal("1.00"))

    with pytest.raises(ValueError):
        ledger.settle(reservation.id, Decimal("1.00"))
    with pytest.raises(ValueError):
        ledger.release(reservation.id)
    with pytest.raises(ValueError):
        ledger.settle("rsv-999999", Decimal("1.00"))
    with pytest.raises(ValueError):
        ledger.release("nope")

    # The double-settle attempt did not move the totals.
    assert ledger.committed == Decimal("1.00")


def test_release_then_settle_raises() -> None:
    ledger = SpendLedger(Decimal("10.00"))
    reservation = ledger.reserve(Decimal("1.00"), "main", "veo")
    ledger.release(reservation.id)
    with pytest.raises(ValueError):
        ledger.settle(reservation.id, Decimal("1.00"))
    assert ledger.committed == Decimal("0")


def test_remaining_floors_at_zero_after_an_overrun() -> None:
    ledger = SpendLedger(Decimal("5.00"))
    reservation = ledger.reserve(Decimal("5.00"), "main", "veo")
    ledger.settle(reservation.id, Decimal("8.00"))  # the ledger never lies

    assert ledger.committed == Decimal("8.00")
    assert ledger.remaining == Decimal("0")
    assert ledger.overrun_detected is True


def test_zero_budget_ledger_refuses_every_positive_reservation() -> None:
    ledger = SpendLedger(Decimal("0"))
    for amount in (Decimal("0.01"), Decimal("0.24"), Decimal("1.60")):
        with pytest.raises(BudgetExceeded):
            ledger.reserve(amount, "main", "veo")
    assert ledger.entries == ()
    assert ledger.remaining == Decimal("0")


def test_money_must_be_decimal() -> None:
    with pytest.raises(TypeError):
        SpendLedger(150.0)  # type: ignore[arg-type]
    ledger = SpendLedger(Decimal("10.00"))
    with pytest.raises(TypeError):
        ledger.reserve(0.24, "main", "veo")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ledger.reserve(Decimal("-1"), "main", "veo")


# ---------------------------------------------------------------------------
# The adversarial ceiling tests (Implementation Plan section 7)
# ---------------------------------------------------------------------------


def test_ceiling_holds_when_the_cost_model_is_wrong_by_10x() -> None:
    """Estimate says $0.10/clip; every clip actually bills $1.00.

    Reservations hold the max *plausible* cost -- 10x the estimate -- so the
    ceiling is checked against a number that is right even though the estimate
    is not. Committed spend must never pass the ceiling.
    """
    ceiling = Decimal("20.00")
    estimate = Decimal("0.10")
    max_plausible = estimate * 10  # what the backend reports as worst case
    actual = Decimal("1.00")  # what it really bills: 10x the estimate

    ledger = SpendLedger(ceiling)
    settled = 0
    refused = 0

    for _ in range(500):
        try:
            reservation = ledger.reserve(max_plausible, "main", "veo")
        except BudgetExceeded:
            refused += 1
            continue
        ledger.settle(reservation.id, actual)
        settled += 1
        assert ledger.committed <= ceiling
        assert ledger.remaining >= Decimal("0")

    assert settled == 20  # $20 ceiling / $1.00 actual
    assert refused > 0
    assert ledger.committed == ceiling
    assert ledger.overrun_detected is False  # holds were never exceeded
    assert ledger.remaining == Decimal("0")


def test_underestimated_reservations_flip_overrun_and_the_ceiling_still_binds() -> None:
    """The nastier variant: the max plausible cost is itself too low.

    One bad settle can push committed spend past the ceiling -- the ledger
    records the truth rather than the plan. What must survive is the property
    that matters: overrun is surfaced, and every *subsequent* reservation is
    refused, so the breach is bounded by a single generation and never
    compounds.
    """
    ceiling = Decimal("20.00")
    max_plausible = Decimal("0.20")  # underestimated worst case
    actual = Decimal("1.30")  # what it really bills

    ledger = SpendLedger(ceiling)
    settled = 0
    for _ in range(200):
        try:
            reservation = ledger.reserve(max_plausible, "main", "veo")
        except BudgetExceeded:
            break
        ledger.settle(reservation.id, actual)
        settled += 1

    assert ledger.overrun_detected is True
    assert ledger.committed > ceiling  # the ledger reports the real damage
    # The breach is bounded by one generation's actual cost.
    assert ledger.committed <= ceiling + actual
    assert ledger.remaining == Decimal("0")

    # The ceiling holds for all future calls, even after the bad settle.
    for _ in range(5):
        with pytest.raises(BudgetExceeded):
            ledger.reserve(Decimal("0.01"), "main", "veo")
    assert settled == 16


def test_governor_routes_to_free_backend_under_a_10x_wrong_model() -> None:
    """Same adversary, seen through the facade: authorize never raises."""
    solver = CadenceSolver(Decimal("20.00"), 14400, 1, clock=FakeClock())
    gov = Governor(Decimal("20.00"), solver, ["main"], cost_per_clip=Decimal("0.10"))

    authorized = 0
    routed_free = 0
    for _ in range(100):
        reservation = gov.authorize("main", "veo", Decimal("1.00"))
        if reservation is None:
            routed_free += 1
            continue
        gov.settle(reservation, Decimal("1.00"))
        authorized += 1

    assert authorized == 20
    assert routed_free == 80
    assert gov.ledger.committed == Decimal("20.00")


# ---------------------------------------------------------------------------
# Spend curve
# ---------------------------------------------------------------------------


def test_unnormalized_curve_is_normalized_to_mean_one() -> None:
    curve = normalize_curve(points(("0%", 2.0), ("100%", 2.0)))
    assert curve == [(0.0, 1.0), (1.0, 1.0)]
    assert curve_integral(curve) == pytest.approx(1.0)


def test_architecture_example_curve_normalizes_to_mean_one() -> None:
    raw = points(("0%", 0.5), ("30%", 1.2), ("60%", 1.5), ("85%", 0.8), ("100%", 0.3))
    assert curve_integral([(p.frac, p.rate) for p in raw]) != pytest.approx(1.0)

    curve = normalize_curve(raw)
    assert curve_integral(curve) == pytest.approx(1.0, abs=1e-12)

    # Only the distribution is the operator's; the shape is preserved exactly.
    peak = rate_at(curve, 0.6)
    arrival = rate_at(curve, 0.0)
    assert peak / arrival == pytest.approx(1.5 / 0.5)
    assert peak > 1.0 > arrival


def test_curve_endpoints_are_extended_flat() -> None:
    # A single interior point means a flat curve, which normalizes to 1.0.
    curve = normalize_curve(points(("50%", 7.0)))
    assert curve == [(0.5, 1.0)]
    assert rate_at(curve, 0.0) == pytest.approx(1.0)
    assert rate_at(curve, 1.0) == pytest.approx(1.0)

    # Held flat before the first and after the last point.
    curve = normalize_curve(points(("25%", 1.0), ("75%", 3.0)))
    assert rate_at(curve, 0.0) == pytest.approx(curve[0][1])
    assert rate_at(curve, 1.0) == pytest.approx(curve[-1][1])
    assert curve_integral(curve) == pytest.approx(1.0)


def test_rate_at_interpolates_linearly() -> None:
    curve = [(0.0, 1.0), (1.0, 3.0)]
    assert rate_at(curve, 0.0) == pytest.approx(1.0)
    assert rate_at(curve, 0.5) == pytest.approx(2.0)
    assert rate_at(curve, 0.25) == pytest.approx(1.5)
    assert rate_at(curve, 1.0) == pytest.approx(3.0)
    # Out of range clamps rather than extrapolating.
    assert rate_at(curve, -5.0) == pytest.approx(1.0)
    assert rate_at(curve, 5.0) == pytest.approx(3.0)


def test_empty_curve_is_flat_one() -> None:
    assert normalize_curve([]) == []
    assert normalize_curve(None) == []
    assert rate_at([], 0.0) == 1.0
    assert rate_at([], 0.73) == 1.0
    assert curve_integral([]) == 1.0


def test_curve_points_are_sorted_and_deduplicated() -> None:
    curve = normalize_curve(points(("100%", 1.0), ("0%", 1.0), ("50%", 1.0), ("50%", 1.0)))
    assert [frac for frac, _ in curve] == [0.0, 0.5, 1.0]


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def solver(budget: str = "150", **kw) -> CadenceSolver:
    kw.setdefault("party_duration_s", 14400)
    kw.setdefault("zone_count", 4)
    return CadenceSolver(total_budget=Decimal(budget), clock=FakeClock(), **kw)


def test_worked_example_from_architecture() -> None:
    # 4-hour party, 4 zones, $150, Veo 3.1 Lite 8s video-only at ~$0.24/clip.
    s = solver()
    interval = s.interval_for(
        "main",
        remaining_budget=Decimal("150"),
        now_frac=0.0,
        cost_per_clip=Decimal("0.24"),
    )
    assert interval == pytest.approx(92.16, abs=0.01)


def test_under_spend_is_redistributed_forward() -> None:
    s = solver()
    start = s.interval_for(
        "main", remaining_budget=Decimal("150"), now_frac=0.0, cost_per_clip=Decimal("0.24")
    )
    # Halfway through the night with the whole budget still unspent: half the
    # time remains for the same money, so the cadence roughly halves.
    halfway = s.interval_for(
        "main", remaining_budget=Decimal("150"), now_frac=0.5, cost_per_clip=Decimal("0.24")
    )
    assert halfway == pytest.approx(start / 2, rel=1e-9)
    assert halfway == pytest.approx(46.08, abs=0.01)

    # On plan (half the budget spent at halfway) the cadence is unchanged.
    on_plan = s.interval_for(
        "main", remaining_budget=Decimal("75"), now_frac=0.5, cost_per_clip=Decimal("0.24")
    )
    assert on_plan == pytest.approx(start, rel=1e-9)


def test_exhausted_budget_signals_infinite_interval() -> None:
    s = solver()
    for remaining in (Decimal("0"), Decimal("-1.00")):
        assert math.isinf(
            s.interval_for(
                "main", remaining_budget=remaining, now_frac=0.5, cost_per_clip=Decimal("0.24")
            )
        )
    # Out of time is the same signal.
    assert math.isinf(
        s.interval_for(
            "main", remaining_budget=Decimal("150"), now_frac=1.0, cost_per_clip=Decimal("0.24")
        )
    )


def test_min_interval_floor_stops_a_spin_loop() -> None:
    s = solver(budget="100000")
    interval = s.interval_for(
        "main",
        remaining_budget=Decimal("100000"),
        now_frac=0.0,
        cost_per_clip=Decimal("0.24"),
    )
    assert interval == pytest.approx(30.0)  # default floor, not 0.14s

    s = solver(budget="100000", min_interval_s=5.0)
    assert s.interval_for(
        "main",
        remaining_budget=Decimal("100000"),
        now_frac=0.0,
        cost_per_clip=Decimal("0.24"),
    ) == pytest.approx(5.0)


def test_interval_is_capped_at_remaining_time() -> None:
    s = solver(budget="0.50")
    # Tiny budget late in the night: the raw interval exceeds what is left.
    interval = s.interval_for(
        "main", remaining_budget=Decimal("0.50"), now_frac=0.999, cost_per_clip=Decimal("0.24")
    )
    assert interval == pytest.approx(14.4, abs=0.01)  # 0.1% of 14400s


def test_curve_rate_shortens_the_interval_at_peak() -> None:
    curve = points(("0%", 0.5), ("60%", 1.5), ("100%", 0.5))
    # A low floor, so the comparison is about the curve and not the clamp.
    s = solver(curve_points=curve, min_interval_s=1.0)
    flat = solver(min_interval_s=1.0)

    kwargs = {"remaining_budget": Decimal("150"), "cost_per_clip": Decimal("0.24")}
    peak = s.interval_for("main", now_frac=0.6, **kwargs)
    flat_peak = flat.interval_for("main", now_frac=0.6, **kwargs)
    arrival = s.interval_for("main", now_frac=0.0, **kwargs)
    flat_arrival = flat.interval_for("main", now_frac=0.0, **kwargs)

    assert peak < flat_peak  # higher rate -> shorter interval
    assert arrival > flat_arrival
    assert peak / flat_peak == pytest.approx(1.0 / s.rate(0.6))


def test_free_backend_paces_on_the_floor() -> None:
    s = solver()
    assert s.interval_for(
        "main", remaining_budget=Decimal("150"), now_frac=0.0, cost_per_clip=Decimal("0")
    ) == pytest.approx(30.0)


def test_now_frac_tracks_the_clock() -> None:
    clock = FakeClock()
    s = CadenceSolver(Decimal("150"), 14400, 4, clock=clock)
    s.start()
    assert s.now_frac() == pytest.approx(0.0)
    clock.advance(7200)
    assert s.now_frac() == pytest.approx(0.5)
    assert s.remaining_time_s(s.now_frac()) == pytest.approx(7200)
    clock.advance(14400)
    assert s.now_frac() == pytest.approx(1.0)  # clamped, never past the end


def test_continuity_meters_on_movements_not_clips() -> None:
    s = solver()
    # A movement: ~148s of billed video, ~21 Veo Lite calls at $0.03/s.
    movement_cost = Decimal("4.44")
    interval = s.continuity_interval_for(
        "main",
        remaining_budget=Decimal("150"),
        now_frac=0.0,
        movement_billed_seconds=148.0,
        movement_cost=movement_cost,
    )
    expected = 14400 * 4 * float(movement_cost / Decimal("150"))
    assert interval == pytest.approx(expected, rel=1e-9)
    assert interval == pytest.approx(1705.0, abs=1.0)

    # A large budget cannot start movements faster than they play.
    rich = s.continuity_interval_for(
        "main",
        remaining_budget=Decimal("100000"),
        now_frac=0.0,
        movement_billed_seconds=148.0,
        movement_cost=movement_cost,
    )
    assert rich == pytest.approx(148.0)

    with pytest.raises(ValueError):
        s.continuity_interval_for(
            "main",
            remaining_budget=Decimal("150"),
            now_frac=0.0,
            movement_billed_seconds=0.0,
            movement_cost=movement_cost,
        )


def test_solver_rejects_nonsense_construction() -> None:
    with pytest.raises(TypeError):
        CadenceSolver(150.0, 14400, 4)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CadenceSolver(Decimal("-1"), 14400, 4)
    with pytest.raises(ValueError):
        CadenceSolver(Decimal("150"), 0, 4)
    with pytest.raises(ValueError):
        CadenceSolver(Decimal("150"), 14400, 0)
    with pytest.raises(ValueError):
        CadenceSolver(Decimal("150"), 14400, 4, min_interval_s=0)


# ---------------------------------------------------------------------------
# Governor facade
# ---------------------------------------------------------------------------


def build_governor(budget: str = "150", **kw) -> tuple[Governor, FakeClock]:
    clock = FakeClock()
    cost = Decimal(kw.pop("cost_per_clip", "0.24"))
    s = CadenceSolver(
        total_budget=Decimal(budget),
        party_duration_s=kw.pop("party_duration_s", 14400),
        zone_count=kw.pop("zone_count", 4),
        curve_points=kw.pop("curve_points", None),
        clock=clock,
        min_interval_s=kw.pop("min_interval_s", 30.0),
    )
    gov = Governor(Decimal(budget), s, ["main"], cost_per_clip=cost, clock=clock)
    gov.start()
    return gov, clock


def test_authorize_returns_none_past_the_ceiling() -> None:
    gov, _ = build_governor(budget="1.00")
    first = gov.authorize("main", "veo", Decimal("0.80"))
    assert first is not None

    assert gov.authorize("main", "veo", Decimal("0.80")) is None  # would breach

    gov.settle(first, Decimal("0.20"))  # cheaper than feared: headroom returns
    second = gov.authorize("main", "veo", Decimal("0.80"))
    assert second is not None
    gov.release(second)
    assert gov.ledger.reserved == Decimal("0")
    assert gov.ledger.committed == Decimal("0.20")


def test_zero_budget_governor_never_authorizes_but_still_paces() -> None:
    gov, clock = build_governor(budget="0")

    assert gov.authorize("main", "veo", Decimal("0.24")) is None
    assert gov.authorize("main", "veo", Decimal("0")) is None
    assert gov.ledger.entries == ()

    # Local generation is free, and paced by the cadence floor.
    assert gov.should_generate("main") is True
    gov.record_generation("main")
    assert gov.should_generate("main") is False
    clock.advance(29.0)
    assert gov.should_generate("main") is False
    clock.advance(2.0)
    assert gov.should_generate("main") is True
    assert gov.interval_for("main") == pytest.approx(30.0)


def test_should_generate_follows_the_worked_example_cadence() -> None:
    gov, clock = build_governor()
    assert gov.should_generate("main") is True  # nothing generated yet
    gov.record_generation("main")

    clock.advance(90.0)
    assert gov.should_generate("main") is False
    clock.advance(3.0)  # past 92.16s
    assert gov.should_generate("main") is True
    # Next eligibility is recomputed live, so it tracks the current interval
    # (a hair under 92.16s now that 93s of the night have gone by).
    last = gov.last_generation_at("main")
    assert last is not None
    assert gov.next_eligible_at("main") == pytest.approx(last + gov.interval_for("main"))
    assert gov.interval_for("main") == pytest.approx(92.16, abs=1.0)


def test_exhausted_budget_still_paces_local_generation() -> None:
    gov, clock = build_governor(budget="1.00")
    reservation = gov.authorize("main", "veo", Decimal("1.00"))
    assert reservation is not None
    gov.settle(reservation, Decimal("1.00"))
    assert gov.ledger.remaining == Decimal("0")

    gov.record_generation("main")
    assert gov.interval_for("main") == pytest.approx(30.0)  # floor, not infinity
    clock.advance(31.0)
    assert gov.should_generate("main") is True
    assert gov.authorize("main", "veo", Decimal("0.01")) is None


def test_status_is_json_safe_and_reports_the_ceiling() -> None:
    gov, _ = build_governor(budget="150")
    reservation = gov.authorize("main", "veo", Decimal("1.60"))
    assert reservation is not None
    gov.settle(reservation, Decimal("2.00"))  # overrun on this one line item
    gov.record_generation("main")

    status = gov.status()
    assert status["ceiling"] == "150"
    assert status["committed"] == "2.00"
    assert Decimal(status["reserved"]) == Decimal("0")
    assert Decimal(status["remaining"]) == Decimal("148.00")
    assert status["overrun_detected"] is True
    assert status["zones"]["main"]["interval_s"] > 0
    assert status["zones"]["main"]["last_generation_at"] is not None
    json.dumps(status)  # no Decimal escapes into the wire format


def test_governor_from_config_uses_the_budget_as_the_ceiling() -> None:
    config = EgregoreConfig(
        party=PartyConfig(name="test", duration_hours=4.0),
        budget=BudgetConfig(
            total_usd=Decimal("150"),
            spend_curve=points(("0%", 1.0), ("60%", 3.0), ("100%", 1.0)),
        ),
        zones=[ZoneConfig(id=f"z{i}") for i in range(4)],
    )
    gov = Governor.from_config(config, cost_per_clip=Decimal("0.24"), clock=FakeClock())
    gov.start()

    assert gov.ledger.ceiling == config.budget.total_usd
    assert gov.solver.zone_count == 4
    assert gov.solver.party_duration_s == pytest.approx(14400.0)
    assert curve_integral(gov.solver.curve) == pytest.approx(1.0)
    assert gov.interval_for("z0") == pytest.approx(92.16 / gov.solver.rate(0.0), abs=0.05)


def test_zero_budget_config_makes_cloud_calls_impossible() -> None:
    config = EgregoreConfig(budget=BudgetConfig(total_usd=Decimal("0")))
    gov = Governor.from_config(config, cost_per_clip=Decimal("0.24"), clock=FakeClock())
    assert gov.ledger.ceiling == Decimal("0")
    assert all(
        gov.authorize("main", backend, Decimal("0.24")) is None
        for backend in ("veo", "veo-quality", "anything")
    )
    assert gov.should_generate("main") is True
