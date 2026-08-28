"""Shared types and contracts for Egregore.

Every module builds against these. This file is the architecture's stable
surface: modules may depend on it, never on each other's internals.

Privacy invariant (Architecture §5): nothing in this module may ever hold
raw transcript text except ThemeObject *inputs* upstream of the validator.
ClipRef, Manifest, FeatureFrame and MoodState are all content-blind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Theme objects — the only thing that crosses the Weaver's stage boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThemeObject:
    """Abstract distillation of a zone's recent conversation.

    Produced by Weaver stage 1, checked by the validator, consumed by
    stage 2. Frozen so nothing downstream can smuggle content into it.
    """

    motifs: list[str] = field(default_factory=list)
    register: str = "ambient"
    valence: float = 0.5  # 0 = dark .. 1 = light
    intensity: float = 0.5  # 0 = still .. 1 = peak
    movement: str = "slow drift"
    elemental: list[str] = field(default_factory=list)

    # Schema caps enforced by the validator (chars per field / items)
    MAX_MOTIFS = 5
    MAX_ELEMENTAL = 5
    MAX_FIELD_CHARS = 80

    def all_text(self) -> list[str]:
        """Every free-form string in the object, for validation sweeps."""
        return [*self.motifs, self.register, self.movement, *self.elemental]


# ---------------------------------------------------------------------------
# Clips and manifests — content-blind media references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipRef:
    """A generated clip in the store. Content-addressed, immutable."""

    id: str  # content hash, also the URL path segment
    path: Path  # local file
    duration_s: float
    zone: str
    backend: str  # "veo" | "local" | "mock"
    tier: str  # backend model tier used
    created_at: float = field(default_factory=time.time)
    movement_id: str | None = None  # continuity movement this belongs to
    chain_index: int = 0  # position within its extension chain
    # NOTE: no prompt text here. Prompts are not persisted alongside clips.


@dataclass
class ManifestEntry:
    clip_id: str
    duration_s: float
    weight: float  # sampling weight, already normalized upstream
    movement_id: str | None = None


@dataclass
class Manifest:
    """What the Conductor serves to Lens clients for one zone."""

    zone: str
    entries: list[ManifestEntry]
    mode: Literal["mosaic", "continuity"]
    crossfade_s: float = 2.0
    generated_at: float = field(default_factory=time.time)
    revision: int = 0


# ---------------------------------------------------------------------------
# Audio features and mood — the fast, content-blind path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureFrame:
    """One ~30 Hz frame of content-blind audio features."""

    t: float  # unix time
    rms: float  # 0..1
    low: float  # band energies, 0..1
    mid: float
    high: float
    centroid: float  # normalized spectral centroid 0..1
    onset: float  # onset strength 0..1

    def as_wire(self) -> dict:
        return {
            "t": self.t,
            "rms": round(self.rms, 4),
            "low": round(self.low, 4),
            "mid": round(self.mid, 4),
            "high": round(self.high, 4),
            "centroid": round(self.centroid, 4),
            "onset": round(self.onset, 4),
        }


@dataclass
class MoodState:
    """The 1–10 s middle temporal layer (Architecture §2.1).

    Content-blind: derived from audio features plus a slow decay of the
    last theme object's valence/intensity. Feeds shaders and prompt bias.
    """

    energy: float = 0.0
    variability: float = 0.0
    onset_density: float = 0.0
    brightness: float = 0.0
    valence: float = 0.5
    intensity: float = 0.5
    updated_at: float = field(default_factory=time.time)

    def as_wire(self) -> dict:
        return {
            "energy": round(self.energy, 4),
            "variability": round(self.variability, 4),
            "onset_density": round(self.onset_density, 4),
            "brightness": round(self.brightness, 4),
            "valence": round(self.valence, 4),
            "intensity": round(self.intensity, 4),
        }


# ---------------------------------------------------------------------------
# Generation backends — the Forge protocol (Architecture §2.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    allowed_durations_s: frozenset[int]
    supports_native_extend: bool
    supports_image_seed: bool
    tiers: frozenset[str]
    max_chain_length: int = 0  # native extensions per chain; 0 = none


class BackendStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass
class BackendHealth:
    status: BackendStatus
    detail: str = ""
    checked_at: float = field(default_factory=time.time)


@runtime_checkable
class VideoBackend(Protocol):
    """Narrow generation interface. Governor reserves against
    max_plausible_cost, never the expected cost (Architecture §2.5)."""

    name: str

    @property
    def capabilities(self) -> BackendCapabilities: ...

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        tier: str,
        seed_image: bytes | None = None,
        extend_from: ClipRef | None = None,
        theme_hint: ThemeObject | None = None,  # mock backend only; cloud ignores
        zone: str = "default",
    ) -> ClipRef: ...

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal: ...

    def estimated_latency(self, tier: str) -> timedelta: ...

    async def health(self) -> BackendHealth: ...


# ---------------------------------------------------------------------------
# Governor — reservations against the hard ceiling
# ---------------------------------------------------------------------------


@dataclass
class Reservation:
    """A hold against the budget. Reconciled to actual cost on completion."""

    id: str
    amount: Decimal  # max plausible cost, reserved up front
    zone: str
    backend: str
    created_at: float = field(default_factory=time.time)


class BudgetExceeded(Exception):
    """Raised when a reservation would breach the hard ceiling.

    Callers must treat this as a routing signal (fall to local backend),
    never as an error to retry against the cloud.
    """


# ---------------------------------------------------------------------------
# Transcription — the Scribe output contract (Architecture §2.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextFragment:
    """Timestamped transcript fragment. Lives ONLY in the ring buffer.

    Never logged, never serialized, never persisted. repr is redacted.
    """

    text: str
    t: float

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"TextFragment(<{len(self.text)} chars redacted>, t={self.t:.1f})"

    __str__ = __repr__


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text engine. Emits fragments; holds no history."""

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None: ...


# ---------------------------------------------------------------------------
# Zone wiring
# ---------------------------------------------------------------------------


@dataclass
class ZoneStatus:
    """Operator-facing status for one zone. Content-blind by construction."""

    zone: str
    mode: Literal["mosaic", "continuity"]
    clip_count: int = 0
    queue_depth: int = 0
    buffer_occupancy_tokens: int = 0  # token count only, never content
    prompts_sent: int = 0
    last_generation_at: float | None = None
    validator_rejections: int = 0
    muted: bool = False
