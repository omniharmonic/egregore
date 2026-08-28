"""The weighted playlist — Egregore's recency-weighted clip pool (Architecture §3.4).

Old material never fully retires (PRD C-6): weight decays toward a floor
rather than to zero, and clips that age out of the active working set move
to a low-rate archive tier instead of being dropped. The playlist itself is
content-blind — it only ever handles ``ClipRef``/``ManifestEntry``, never
theme or transcript data.

Weighting is base-2 (a true half-life), not ``exp`` — see Architecture §3.4's
note on why an earlier exponential draft lied about its own config key.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from egregore.types import ClipRef, ManifestEntry

__all__ = ["WeightedPlaylist"]


class WeightedPlaylist:
    """Recency-weighted pool of clips for one zone's mosaic playlist.

    Args:
        half_life_min: minutes for the recency weight to halve.
        floor_weight: weight floor — old material thins but never vanishes.
        active_pool_max: size of the "hot" working set. Beyond this, the
            oldest active clips move to the archive tier rather than being
            dropped, bounding both server sampling cost and client cache
            size (Architecture §2.9) while keeping the night's full record
            occasionally visible.
        archive_rate: multiplier applied to an archived clip's recency
            weight — a further thinning on top of the floor, not a
            replacement for it, so archived clips stay nonzero.
        clock: wall-clock time source, injectable for tests. Must share a
            domain with ``ClipRef.created_at`` (both default to
            ``time.time``).
    """

    def __init__(
        self,
        *,
        half_life_min: float = 45.0,
        floor_weight: float = 0.15,
        active_pool_max: int = 200,
        archive_rate: float = 0.05,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if half_life_min <= 0:
            raise ValueError("half_life_min must be positive")
        if not (0.0 <= floor_weight <= 1.0):
            raise ValueError("floor_weight must be in [0, 1]")
        if active_pool_max <= 0:
            raise ValueError("active_pool_max must be positive")
        if not (0.0 <= archive_rate <= 1.0):
            raise ValueError("archive_rate must be in [0, 1]")
        self.half_life_min = float(half_life_min)
        self.floor_weight = float(floor_weight)
        self.active_pool_max = int(active_pool_max)
        self.archive_rate = float(archive_rate)
        self._clock = clock
        # Plain dicts preserve insertion order (oldest first), which is what
        # we need to evict the oldest active clip into the archive tier.
        self._active: dict[str, ClipRef] = {}
        self._archive: dict[str, ClipRef] = {}

    # -- writing ------------------------------------------------------------

    def add(self, clip: ClipRef) -> None:
        """Add (or re-add) a clip to the active pool.

        Re-adding a clip that is currently archived promotes it back to
        active — content-addressed clips are immutable, so this only
        matters if a caller ever legitimately re-ingests the same id.
        """
        self._archive.pop(clip.id, None)
        self._active[clip.id] = clip
        self._rebalance()

    def _rebalance(self) -> None:
        while len(self._active) > self.active_pool_max:
            oldest_id = next(iter(self._active))
            self._archive[oldest_id] = self._active.pop(oldest_id)

    # -- weighting ------------------------------------------------------------

    def weight(self, clip: ClipRef) -> float:
        """Base recency weight: ``max(floor, 2 ** (-age_minutes / half_life))``.

        This is the pure recency term — it does not know or care whether
        ``clip`` is currently in the active pool or the archive tier. The
        archive's further thinning is applied separately by
        ``_effective_weight``, so this method's values match the formula in
        Architecture §3.4 exactly and are what tests check.
        """
        age_minutes = max(0.0, (self._clock() - clip.created_at) / 60.0)
        raw = 2.0 ** (-age_minutes / self.half_life_min)
        return max(self.floor_weight, raw)

    def _effective_weight(self, clip: ClipRef) -> float:
        w = self.weight(clip)
        if clip.id in self._archive:
            w *= self.archive_rate
        return w

    # -- reading ------------------------------------------------------------

    def _all_clips(self) -> list[ClipRef]:
        return [*self._active.values(), *self._archive.values()]

    def entries(self) -> list[ManifestEntry]:
        """Current playlist as manifest entries, weights normalized to sum 1.0."""
        clips = self._all_clips()
        if not clips:
            return []
        raw = [self._effective_weight(c) for c in clips]
        total = sum(raw)
        if total <= 0:
            # Degenerate (shouldn't happen: weight() always returns >= floor
            # > 0 for a positive floor, and archive_rate > 0 in practice) —
            # fall back to uniform rather than dividing by zero.
            n = len(clips)
            return [
                ManifestEntry(clip_id=c.id, duration_s=c.duration_s, weight=1.0 / n, movement_id=c.movement_id)
                for c in clips
            ]
        return [
            ManifestEntry(
                clip_id=c.id,
                duration_s=c.duration_s,
                weight=w / total,
                movement_id=c.movement_id,
            )
            for c, w in zip(clips, raw, strict=True)
        ]

    def sample(self, rng) -> ClipRef | None:
        """Weighted-random choice of one clip, or ``None`` if empty.

        ``rng`` is any object exposing ``random.Random``'s ``choices``
        (typically a seeded ``random.Random`` instance, injected by the
        caller for determinism).
        """
        clips = self._all_clips()
        if not clips:
            return None
        weights = [self._effective_weight(c) for c in clips]
        return rng.choices(clips, weights=weights, k=1)[0]

    @property
    def size(self) -> int:
        return len(self._active) + len(self._archive)

    @property
    def active_size(self) -> int:
        return len(self._active)

    @property
    def archive_size(self) -> int:
        return len(self._archive)

    def __len__(self) -> int:
        return self.size
