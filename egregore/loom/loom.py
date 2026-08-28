"""ZoneLoom — the continuity state machine (Architecture §3.3).

Turns discrete generated clips into one continuous dream: it owns the
zone's ``WeightedPlaylist`` (mosaic), the movement/chain bookkeeping and
last-frame handoff (continuity), and the night-long thematic memory that
biases both prompt seeding and motif recall. Content-blind by construction
— it only ever handles ``ClipRef``, ``ThemeObject`` (already validated by
the Weaver by the time it reaches here) and media bytes, never transcript
text.

Mode is switchable mid-party per PRD C-4 / Architecture §3.3: switching
never restarts anything or disturbs the playlist.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from egregore.config.schema import ContinuityConfig
from egregore.loom.frames import extract_last_frame
from egregore.loom.playlist import WeightedPlaylist
from egregore.types import ClipRef, Manifest, ThemeObject

logger = logging.getLogger(__name__)

__all__ = ["GenerationPlan", "Movement", "ZoneLoom"]

_THEMATIC_MEMORY_MAX = 50


@dataclass
class Movement:
    """One continuity chain: a seed clip plus its extensions/handoffs."""

    id: str
    clip_ids: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    movement_descriptor: str = ""  # carried motion descriptor, from the theme that seeded it


@dataclass
class GenerationPlan:
    """Tells the caller (Forge integration) how to generate the next clip.

    The Loom does not know which backend is in play or what it supports —
    it only exposes state. ``use_extend`` set means "chain not at ceiling,
    a native extension is *possible* if the backend supports one"; the
    integration layer is what actually checks
    ``BackendCapabilities.supports_native_extend`` and decides. When the
    chain *is* at its ceiling, ``use_extend`` is None and ``new_movement``
    is True instead — that transition is the ceiling signal.
    """

    use_extend: ClipRef | None = None
    seed_image: bytes | None = None
    movement_descriptor: str | None = None
    new_movement: bool = False


def _join_natural(items: list[str]) -> str:
    """Join like 'a, b and c' — used only for already-validated theme fields
    (never raw transcript content)."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


class ZoneLoom:
    """Per-zone continuity engine.

    Args:
        zone: zone id.
        mode: initial mode, "mosaic" or "continuity".
        half_life_min, floor_weight, active_pool_max: passed straight to
            the zone's ``WeightedPlaylist`` (Architecture §3.4).
        archive_rate: playlist archive-tier thinning factor. Not part of
            ``ContinuityConfig`` (see ``from_config``'s docstring) — a
            loom-local default.
        max_chain_length: provider-validated extension ceiling per movement
            (Architecture §3.2).
        crossfade_s: clip crossfade seconds, carried into every manifest.
        clock: wall-clock source, injectable for tests. Shared with the
            playlist so ages are computed consistently.
    """

    def __init__(
        self,
        zone: str,
        mode: Literal["mosaic", "continuity"] = "mosaic",
        *,
        half_life_min: float = 45.0,
        floor_weight: float = 0.15,
        active_pool_max: int = 200,
        archive_rate: float = 0.05,
        max_chain_length: int = 20,
        crossfade_s: float = 2.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_chain_length < 1:
            raise ValueError("max_chain_length must be >= 1")
        self.zone = zone
        self.mode: Literal["mosaic", "continuity"] = mode
        self.max_chain_length = int(max_chain_length)
        self.crossfade_s = float(crossfade_s)
        self._clock = clock

        self.playlist = WeightedPlaylist(
            half_life_min=half_life_min,
            floor_weight=floor_weight,
            active_pool_max=active_pool_max,
            archive_rate=archive_rate,
            clock=clock,
        )
        self.movements: list[Movement] = []
        self.current_chain_length = 0
        self.last_frame: bytes | None = None
        self.thematic_memory: list[ThemeObject] = []

        self._last_clip: ClipRef | None = None
        self._movement_seq = 0
        # True whenever the *next* continuity ingest should start a fresh
        # Movement rather than extend the current one: at construction (no
        # movement exists yet), after a chain hits its ceiling, and right
        # after a mosaic->continuity mode switch.
        self._pending_handoff = True

    @classmethod
    def from_config(
        cls,
        zone: str,
        mode: Literal["mosaic", "continuity"],
        continuity: ContinuityConfig,
        *,
        archive_rate: float = 0.05,
        crossfade_s: float = 2.0,
        clock: Callable[[], float] = time.time,
    ) -> ZoneLoom:
        """Build from the party config's ``continuity`` section.

        Note: ``ContinuityConfig`` (frozen contract) has no ``archive_rate``
        field, so that knob stays a loom-local default rather than a
        config-driven one — see the final report for this as contract
        friction rather than a silent gap.
        """
        return cls(
            zone,
            mode,
            half_life_min=continuity.loop_half_life_min,
            floor_weight=continuity.loop_floor_weight,
            active_pool_max=continuity.active_pool_max,
            archive_rate=archive_rate,
            max_chain_length=continuity.max_chain_length,
            crossfade_s=crossfade_s,
            clock=clock,
        )

    # -- ingest ---------------------------------------------------------------

    async def ingest(self, clip: ClipRef, clip_path: Path) -> None:
        """Absorb one newly generated clip.

        Always adds to the playlist. In continuity mode, also advances the
        chain: starts a new ``Movement`` if a handoff is pending, tracks the
        clip in it, and extracts the new last frame — tolerating extraction
        failure by keeping whatever frame was already stored, so a flaky
        ffmpeg call never breaks the chain.
        """
        self.playlist.add(clip)
        self._last_clip = clip

        if self.mode != "continuity":
            return

        if self._pending_handoff or not self.movements:
            self._start_movement()
            self._pending_handoff = False
            self.current_chain_length = 0

        self.movements[-1].clip_ids.append(clip.id)
        self.current_chain_length += 1

        try:
            self.last_frame = await extract_last_frame(clip_path)
        except Exception:
            logger.warning(
                "loom[%s]: last-frame extraction failed for clip %s; keeping previous frame",
                self.zone,
                clip.id,
            )

    def _start_movement(self) -> None:
        self._movement_seq += 1
        self.movements.append(
            Movement(
                id=f"{self.zone}-m{self._movement_seq}",
                started_at=self._clock(),
                movement_descriptor=self._last_theme_movement() or "",
            )
        )

    # -- planning ---------------------------------------------------------------

    def plan_next(self) -> GenerationPlan:
        """Decide how the next clip for this zone should be generated."""
        if self.mode == "mosaic":
            return GenerationPlan()

        descriptor = self._last_theme_movement()

        if self._pending_handoff or self.current_chain_length >= self.max_chain_length:
            self._pending_handoff = True
            return GenerationPlan(
                use_extend=None,
                seed_image=self.last_frame,
                movement_descriptor=descriptor,
                new_movement=True,
            )

        # Both ways of continuing the movement are offered, and the caller
        # keeps whichever its backend supports (see this class's note on
        # capability-blindness). A backend that can natively extend uses the
        # clip; one that can only continue from a picture — local diffusion —
        # uses the frame. Offering the clip alone left seed-only backends
        # rendering an unrelated clip into every slot of what the chain
        # counter was calling one continuous movement.
        return GenerationPlan(
            use_extend=self._last_clip,
            seed_image=self.last_frame,
            movement_descriptor=descriptor,
            new_movement=False,
        )

    def _last_theme_movement(self) -> str | None:
        if not self.thematic_memory:
            return None
        return self.thematic_memory[-1].movement

    # -- mode ---------------------------------------------------------------

    def set_mode(self, mode: Literal["mosaic", "continuity"]) -> None:
        """Live mode switch (PRD C-4). Never restarts, never touches the playlist.

        mosaic -> continuity: begins seeding from whatever last frame is
        already on hand (from an earlier continuity run), as a fresh
        movement. If there is no last frame yet, the next clip is simply
        generated fresh — there is nothing to seed from.

        continuity -> mosaic: just stops seeding; the last frame and chain
        state are left in place in case the zone switches back later.
        """
        if mode not in ("mosaic", "continuity"):
            raise ValueError(f"unknown mode: {mode!r}")
        if mode == self.mode:
            return
        if mode == "continuity":
            self._pending_handoff = True
            self.current_chain_length = 0
        self.mode = mode

    # -- thematic memory ---------------------------------------------------------------

    def remember_theme(self, theme: ThemeObject) -> None:
        """Record a validated theme object. Capped; oldest dropped past the cap."""
        self.thematic_memory.append(theme)
        if len(self.thematic_memory) > _THEMATIC_MEMORY_MAX:
            del self.thematic_memory[0]

    def recall_motifs(self, k: int = 3, *, rng: random.Random | None = None) -> list[str]:
        """Weighted-random sample of ``k`` motifs from thematic memory, favoring
        recency (T-5: imagery returns to earlier motifs, but more recent
        ones surface more often). Deterministic given a seeded ``rng``.
        """
        if rng is None:
            rng = random.Random()
        pool: list[str] = []
        weights: list[float] = []
        for i, theme in enumerate(self.thematic_memory):
            recency_weight = float(i + 1)  # later entries (more recent) weigh more
            for motif in theme.motifs:
                pool.append(motif)
                weights.append(recency_weight)

        if not pool:
            return []

        k = min(k, len(pool))
        indices = list(range(len(pool)))
        chosen: list[str] = []
        for _ in range(k):
            total = sum(weights[i] for i in indices)
            r = rng.uniform(0.0, total)
            upto = 0.0
            pick_pos = len(indices) - 1  # fallback for float rounding at the edge
            for pos, i in enumerate(indices):
                upto += weights[i]
                if upto >= r:
                    pick_pos = pos
                    break
            chosen.append(pool[indices.pop(pick_pos)])
        return chosen

    def continuity_context(self) -> str | None:
        """Short content-safe descriptor for the Weaver's seeding prompt.

        Built only from the last remembered theme's register/movement/
        elemental fields — never motifs, and never anything that failed
        the Weaver's validator, since only validated themes ever reach
        ``remember_theme``.
        """
        if not self.thematic_memory:
            return None
        theme = self.thematic_memory[-1]
        segments = [f"{theme.register} register", f"{theme.movement} movement"]
        if theme.elemental:
            segments.append(_join_natural(list(theme.elemental)))
        return ", ".join(segments)

    # -- output ---------------------------------------------------------------

    def manifest(self, revision: int = 0) -> Manifest:
        """The current playlist as a Manifest for the Conductor to serve."""
        return Manifest(
            zone=self.zone,
            entries=self.playlist.entries(),
            mode=self.mode,
            crossfade_s=self.crossfade_s,
            revision=revision,
        )

    def status(self) -> dict:
        """Operator dashboard data. Counts only — content-blind."""
        return {
            "zone": self.zone,
            "mode": self.mode,
            "clip_count": self.playlist.size,
            "active_clip_count": self.playlist.active_size,
            "archived_clip_count": self.playlist.archive_size,
            "movement_count": len(self.movements),
            "current_chain_length": self.current_chain_length,
            "max_chain_length": self.max_chain_length,
            "has_last_frame": self.last_frame is not None,
            "thematic_memory_count": len(self.thematic_memory),
        }
