"""The Forge — per-zone generation queue and backend selection ladder.

Architecture §2.6 assigns the generation queue to this module and nowhere
else: "The Governor decides *whether and when* a generation may start; the
Forge owns in-flight work, retries, backend selection at dispatch time, and
the depth metric surfaced on `/api/status`."

The ladder (Architecture §2.5) is:

1. cloud, if budget remains and the backend is healthy
2. local, if budget is exhausted, cloud is unhealthy, or the party is local-only
3. neither — the job is dropped and the loop keeps cycling existing material

Outcome 3 is **not an error**. It is the normal state most of the time and
guests cannot tell the difference (PRD C-5), so an exhausted ladder logs one
content-free line and moves on.

The Forge never imports the Governor. Budget authority arrives as three
callbacks that the integration layer binds; a Forge given none of them can
only ever use free rungs, which is the safe direction to fail in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from egregore.types import (
    BackendStatus,
    ClipRef,
    Reservation,
    ThemeObject,
    VideoBackend,
)

from .store import ClipStore

log = logging.getLogger(__name__)

ZERO = Decimal("0")

#: A fill has to arrive while the gap it covers is still open. Anything
#: slower than this is not a fill, whatever it costs.
FILL_MAX_LATENCY_S = 20.0

#: Reserve worst-case cost against the ceiling, then generate.
AuthorizeFn = Callable[[str, Decimal], Awaitable[Reservation | None]]
#: Reconcile a reservation to the actual cost once a clip lands.
SettleFn = Callable[[Reservation, Decimal], Awaitable[None]]
#: Give a reservation back untouched when the generation never happened.
ReleaseFn = Callable[[Reservation], Awaitable[None]]
#: Where finished clips go — the integration layer binds this to the Loom.
ClipSink = Callable[[ClipRef], Awaitable[None]]

#: Attempts per rung before dropping to the next one.
ATTEMPTS_PER_BACKEND = 2


@dataclass
class GenerationJob:
    """One queued request. Holds the outbound prompt only in memory, only
    until dispatch; it is never logged and never written to disk."""

    zone: str
    prompt: str
    duration_s: int
    tier: str
    theme_hint: ThemeObject | None = None
    seed_image: bytes | None = None
    extend_from: ClipRef | None = None
    movement_id: str | None = None
    #: Set for a fill: imagery asked for to cover the wait for a slow or
    #: expensive backend. A fill must be free — it should never spend the
    #: budget the cadence was pacing — and it must also be *fast*, which is
    #: not the same thing. Local diffusion is free and takes minutes; a fill
    #: routed there is not a fill, it is the same wait again.
    free_only: bool = False

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"GenerationJob(zone={self.zone!r}, duration_s={self.duration_s}, "
            f"tier={self.tier!r}, prompt=<{len(self.prompt)} chars redacted>)"
        )


@dataclass
class ForgeStats:
    """Content-blind counters, for `/api/status` and for tests."""

    requested: int = 0
    completed: int = 0
    dropped: int = 0
    refused: int = 0
    failures: int = 0
    by_backend: dict[str, int] = field(default_factory=dict)


class Forge:
    """Ordered backend ladder + one worker per zone."""

    def __init__(
        self,
        backends: list[VideoBackend],
        store: ClipStore,
        *,
        authorize: AuthorizeFn | None = None,
        settle: SettleFn | None = None,
        release: ReleaseFn | None = None,
        on_clip: ClipSink | None = None,
    ) -> None:
        if not backends:
            raise ValueError("Forge needs at least one backend")
        self.backends = list(backends)
        self.store = store
        self.authorize = authorize
        self.settle = settle
        self.release = release
        self.on_clip = on_clip
        self.stats = ForgeStats()

        # Two lanes per zone. A fill exists to cover the wait for a slow
        # backend, so putting it behind that backend in one queue defeats it
        # entirely: measured against local diffusion at 164s per clip, the
        # fills never ran at all and the loop sat on a single clip.
        self._queues: dict[str, asyncio.Queue[GenerationJob]] = {}
        self._fill_queues: dict[str, asyncio.Queue[GenerationJob]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._fill_workers: dict[str, asyncio.Task[None]] = {}
        self._inflight: dict[str, int] = {}
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spin up a worker per known zone. Idempotent."""
        self._running = True
        for zone in list(self._queues) + list(self._fill_queues):
            self._ensure_worker(zone)

    async def close(self) -> None:
        """Cancel workers and wait for them. In-flight generations are
        cancelled; the clip store is left alone (see `ClipStore.wipe`)."""
        self._running = False
        workers = list(self._workers.values()) + list(self._fill_workers.values())
        for task in workers:
            task.cancel()
        for task in workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._fill_workers.clear()
        self._inflight.clear()

    # -- public API --------------------------------------------------------

    async def request(
        self,
        *,
        zone: str,
        prompt: str,
        duration_s: int,
        tier: str,
        theme_hint: ThemeObject | None = None,
        seed_image: bytes | None = None,
        extend_from: ClipRef | None = None,
        movement_id: str | None = None,
        free_only: bool = False,
    ) -> None:
        """Enqueue a generation. Returns as soon as the job is queued.

        Nothing about the outcome comes back through this call: completed
        clips arrive on `on_clip`, and a job the ladder cannot place is
        dropped (Architecture §2.5, outcome 3).
        """
        job = GenerationJob(
            zone=zone,
            prompt=prompt,
            duration_s=duration_s,
            tier=tier,
            theme_hint=theme_hint,
            seed_image=seed_image,
            extend_from=extend_from,
            movement_id=movement_id,
            free_only=free_only,
        )
        queue = self._fill_queue_for(zone) if free_only else self._queue_for(zone)
        queue.put_nowait(job)
        self.stats.requested += 1
        if self._running:
            self._ensure_worker(zone)

    def queue_depth(self, zone: str) -> int:
        """Pending + in-flight paid jobs for one zone (PRD V-6).

        Fills are deliberately excluded: this number gates whether to ask for
        more imagery, and counting the free lane would let a slow backend's
        own gap-filling suppress the next real generation.
        """
        queue = self._queues.get(zone)
        pending = queue.qsize() if queue is not None else 0
        return pending + self._inflight.get(zone, 0)

    def in_flight(self, zone: str) -> int:
        """Paid jobs currently being rendered for ``zone`` — 0 or 1.

        The pull scheduler asks for a new clip only when this and the queue
        are both empty, which is what bounds lag at one render.
        """
        return self._inflight.get(zone, 0)

    def fill_queue_depth(self, zone: str) -> int:
        queue = self._fill_queues.get(zone)
        return queue.qsize() if queue is not None else 0

    def total_queue_depth(self) -> int:
        return sum(self.queue_depth(zone) for zone in self._queues)

    async def join(self, zone: str | None = None) -> None:
        """Wait until the named zone's queue (or every zone's) is drained.

        Test and shutdown affordance; the party loop never needs it.
        """
        zones = [zone] if zone is not None else list(
            set(self._queues) | set(self._fill_queues)
        )
        for name in zones:
            for pool in (self._queues, self._fill_queues):
                queue = pool.get(name)
                if queue is not None:
                    await queue.join()

    # -- workers -----------------------------------------------------------

    def _queue_for(self, zone: str) -> asyncio.Queue[GenerationJob]:
        queue = self._queues.get(zone)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[zone] = queue
            self._inflight.setdefault(zone, 0)
        return queue

    def _fill_queue_for(self, zone: str) -> asyncio.Queue[GenerationJob]:
        queue = self._fill_queues.get(zone)
        if queue is None:
            queue = asyncio.Queue()
            self._fill_queues[zone] = queue
        return queue

    def _ensure_worker(self, zone: str) -> None:
        task = self._workers.get(zone)
        if task is None or task.done():
            self._workers[zone] = asyncio.create_task(
                self._worker(zone), name=f"forge-{zone}"
            )
        fill = self._fill_workers.get(zone)
        if fill is None or fill.done():
            self._fill_workers[zone] = asyncio.create_task(
                self._worker(zone, fill=True), name=f"forge-fill-{zone}"
            )

    async def _worker(self, zone: str, *, fill: bool = False) -> None:
        queue = self._fill_queue_for(zone) if fill else self._queue_for(zone)
        while True:
            job = await queue.get()
            if not fill:
                self._inflight[zone] = self._inflight.get(zone, 0) + 1
            try:
                await self._dispatch(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A worker that dies takes the zone's imagery with it, so
                # nothing gets out of _dispatch. Content-blind by construction.
                self.stats.failures += 1
                log.exception("forge worker error zone=%s", zone)
            finally:
                if not fill:
                    self._inflight[zone] -= 1
                queue.task_done()

    # -- the ladder --------------------------------------------------------

    async def _dispatch(self, job: GenerationJob) -> None:
        for backend in self.backends:
            if not await self._is_healthy(backend):
                continue

            tier = self._pick_tier(backend, job.tier)
            duration_s = self._pick_duration(backend, job.duration_s)
            cost = backend.max_plausible_cost(duration_s, tier)

            if job.free_only:
                if cost > ZERO:
                    # Would spend the budget the cadence was spacing out.
                    continue
                try:
                    latency = backend.estimated_latency(tier).total_seconds()
                except Exception:
                    latency = 0.0
                if latency > FILL_MAX_LATENCY_S:
                    # Free but slow — local diffusion is both, and a fill sent
                    # there reproduces the wait it existed to cover. Measured:
                    # fills routed into a 164s render never ran at all.
                    continue

            authorized, reservation = await self._reserve(backend, cost, job.zone)
            if not authorized:
                continue

            clip = await self._attempt(backend, job, duration_s, tier)
            if clip is None:
                if reservation is not None and self.release is not None:
                    await self.release(reservation)
                continue

            if reservation is not None and self.settle is not None:
                # Actual cost is settled at the reserved amount for now: no
                # backend reports a billed figure, and settling low would let
                # a wrong price table under-count against the ceiling. When a
                # provider exposes real usage this is where it lands.
                await self.settle(reservation, cost)

            self.stats.completed += 1
            self.stats.by_backend[backend.name] = (
                self.stats.by_backend.get(backend.name, 0) + 1
            )
            await self._emit(clip)
            return

        # Outcome 3: not a failure. The loop continues on existing material.
        self.stats.dropped += 1
        log.info(
            "generation dropped zone=%s duration=%ds tier=%s reason=ladder-exhausted",
            job.zone,
            job.duration_s,
            job.tier,
        )

    async def _is_healthy(self, backend: VideoBackend) -> bool:
        try:
            health = await backend.health()
        except Exception as exc:
            log.warning("health check failed backend=%s error=%s", backend.name, type(exc).__name__)
            return False
        if health.status is BackendStatus.DOWN:
            log.info("backend down backend=%s detail=%s", backend.name, health.detail)
            return False
        return True

    async def _reserve(
        self, backend: VideoBackend, cost: Decimal, zone: str
    ) -> tuple[bool, Reservation | None]:
        """Authorize spend for one rung: `(may_proceed, reservation)`.

        Free rungs skip the Governor entirely — there is nothing to reserve
        and no reason to make the local backend wait on a budget decision.
        A priced rung with no `authorize` callback bound is skipped: the
        Forge will not spend money nobody authorised.
        """
        if cost <= 0:
            return True, None
        if self.authorize is None:
            log.warning(
                "skipping priced backend with no authorize callback backend=%s", backend.name
            )
            return False, None
        reservation = await self.authorize(backend.name, cost)
        if reservation is None:
            self.stats.refused += 1
            log.info("reservation refused backend=%s zone=%s", backend.name, zone)
            return False, None
        return True, reservation

    async def _attempt(
        self, backend: VideoBackend, job: GenerationJob, duration_s: int, tier: str
    ) -> ClipRef | None:
        """Generate on one rung, retrying once on a transient failure."""
        caps = backend.capabilities
        seed_image = job.seed_image if caps.supports_image_seed else None
        extend_from = job.extend_from if caps.supports_native_extend else None

        for attempt in range(ATTEMPTS_PER_BACKEND):
            try:
                return await backend.generate(
                    job.prompt,
                    duration_s,
                    tier,
                    seed_image=seed_image,
                    extend_from=extend_from,
                    theme_hint=job.theme_hint,
                    zone=job.zone,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.failures += 1
                # Exception text can carry provider detail but never prompt
                # text; log the type and the rung, nothing else.
                log.warning(
                    "generation failed backend=%s zone=%s attempt=%d/%d error=%s",
                    backend.name,
                    job.zone,
                    attempt + 1,
                    ATTEMPTS_PER_BACKEND,
                    type(exc).__name__,
                )
        return None

    async def _emit(self, clip: ClipRef) -> None:
        if self.on_clip is None:
            return
        try:
            await self.on_clip(clip)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("clip sink error clip=%s", clip.id)

    # -- negotiation -------------------------------------------------------

    @staticmethod
    def _pick_tier(backend: VideoBackend, requested: str) -> str:
        """Map the requested tier onto what this rung actually offers."""
        tiers = backend.capabilities.tiers
        if requested in tiers:
            return requested
        return sorted(tiers)[0]

    @staticmethod
    def _pick_duration(backend: VideoBackend, requested: int) -> int:
        """Snap to the nearest allowed duration, preferring the longer one.

        Architecture §2.6: "the Forge negotiates against [capabilities]
        rather than assuming" — Veo takes 4/6/8 s and other backends differ,
        and neither should be hardcoded upstream.
        """
        allowed = backend.capabilities.allowed_durations_s
        if requested in allowed:
            return requested
        return min(allowed, key=lambda value: (abs(value - requested), -value))


__all__ = ["ATTEMPTS_PER_BACKEND", "Forge", "ForgeStats", "GenerationJob"]
