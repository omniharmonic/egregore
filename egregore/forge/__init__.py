"""FORGE — generation backends, the per-zone queue, the failover ladder and
the clip store (Architecture §2.6).

Public surface::

    from egregore.forge import ClipStore, Forge, MockBackend, VeoBackend, ComfyUIBackend

    store = ClipStore("var/clips")
    forge = Forge(
        [VeoBackend(store), ComfyUIBackend(store), MockBackend(store)],
        store,
        authorize=governor.authorize,   # bound by the integration layer
        settle=governor.settle,
        release=governor.release,
        on_clip=loom.add_clip,
    )
    forge.start()
    await forge.request(zone="main", prompt=validated, duration_s=8, tier="veo-3.1-lite")

The ladder is the list order: first rung that is healthy, affordable and
working wins. An exhausted ladder is a normal outcome, not an error.
"""

from .fal import FAL_MODELS, FalBackend, FalModel
from .forge import ATTEMPTS_PER_BACKEND, Forge, ForgeStats, GenerationJob
from .local import DEFAULT_WORKFLOW, TIER_LTX2, ComfyUIBackend
from .mock import (
    VARIANTS,
    MockBackend,
    Motion,
    Palette,
    base_hue_for,
    build_command,
    motion_for,
    palette_for,
    theme_digest,
    variant_for,
)
from .store import ClipStore
from .veo import (
    COST_PER_SECOND,
    SAFETY_FACTOR,
    TIER_FAST,
    TIER_LITE,
    TIER_QUALITY,
    VeoBackend,
)

__all__ = [
    "ATTEMPTS_PER_BACKEND",
    "COST_PER_SECOND",
    "DEFAULT_WORKFLOW",
    "FAL_MODELS",
    "SAFETY_FACTOR",
    "TIER_FAST",
    "TIER_LITE",
    "TIER_LTX2",
    "TIER_QUALITY",
    "VARIANTS",
    "ClipStore",
    "ComfyUIBackend",
    "FalBackend",
    "FalModel",
    "Forge",
    "ForgeStats",
    "GenerationJob",
    "MockBackend",
    "Motion",
    "Palette",
    "VeoBackend",
    "base_hue_for",
    "build_command",
    "motion_for",
    "palette_for",
    "theme_digest",
    "variant_for",
]
