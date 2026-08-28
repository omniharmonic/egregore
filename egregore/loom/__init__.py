"""LOOM — continuity state machine, weighted playlist, and manifests
(Architecture §2.7 / §3)."""

from __future__ import annotations

from egregore.loom.frames import FrameExtractionError, extract_last_frame
from egregore.loom.loom import GenerationPlan, Movement, ZoneLoom
from egregore.loom.playlist import WeightedPlaylist

__all__ = [
    "FrameExtractionError",
    "GenerationPlan",
    "Movement",
    "WeightedPlaylist",
    "ZoneLoom",
    "extract_last_frame",
]
