"""Audio that arrives over a socket instead of an audio device.

``NetworkSource`` drives exactly the same :class:`ZoneEvents` callbacks
``MicSource`` does, so everything downstream — features, VAD gate, Scribe,
ring buffer — is unchanged and unaware. What differs is only where the PCM
came from.

It deliberately imports no web framework: the Conductor owns the socket and
hands this decoded bytes, which is what lets the merge and gating rules be
tested without a server.

Several phones may transmit into one zone. Their speech is transcribed
independently and lands in the same ring buffer, which is what makes a zone
hear a conversation rather than one person. Their *features* are merged into
a single stream by taking the per-field maximum across nodes heard from
recently: the energy of a room is the loudest thing in it, and a max degrades
correctly to the single-node case.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import fields

import numpy as np

from egregore.types import FeatureFrame

from .features import compute_features
from .sources import ZoneEvents
from .vad import make_gate

log = logging.getLogger(__name__)

__all__ = ["NetworkSource", "merge_frames"]

#: How long a node's last frame keeps contributing to the merge. A phone that
#: stops sending should stop holding the room at its last peak.
DEFAULT_MERGE_WINDOW_S = 2.0


def merge_frames(frames: list[FeatureFrame]) -> FeatureFrame:
    """One frame representing several nodes: per-field max, newest timestamp.

    Raises ``ValueError`` on an empty list — a zone with no active node has
    nothing to publish, and inventing a zeroed frame would read as a silence
    the room did not actually produce.
    """
    if not frames:
        raise ValueError("cannot merge an empty frame list")
    if len(frames) == 1:
        return frames[0]
    merged: dict[str, float] = {}
    for f in fields(FeatureFrame):
        merged[f.name] = max(getattr(fr, f.name) for fr in frames)
    merged["t"] = max(fr.t for fr in frames)
    return FeatureFrame(**merged)


class NetworkSource:
    """A zone's microphone, assembled from whichever browsers are transmitting."""

    def __init__(
        self,
        events: ZoneEvents,
        *,
        zone: str = "main",
        sample_rate: int = 16000,
        gate: object | None = None,
        merge_window_s: float = DEFAULT_MERGE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.zone = zone
        self.sample_rate = sample_rate
        self.gate = gate if gate is not None else make_gate()
        self.merge_window_s = float(merge_window_s)
        self._clock = clock
        self._stopped = False
        #: node id -> (heard_at, frame)
        self._recent: dict[str, tuple[float, FeatureFrame]] = {}
        #: node id -> previous rms, so onset stays a per-node delta
        self._prev_rms: dict[str, float] = {}

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Nothing to poll: this source is driven by :meth:`feed`.

        Present so the integration layer can start every source the same way.
        """
        return None

    def active_nodes(self) -> list[str]:
        now = self._clock()
        return sorted(
            node for node, (heard, _) in self._recent.items()
            if now - heard <= self.merge_window_s
        )

    async def feed(self, node_id: str, pcm: bytes, sample_rate: int) -> float | None:
        """Handle one block of PCM from one node.

        ``pcm`` is 16-bit signed little-endian mono. Publishes a merged
        feature frame for the zone always — the feature path is never gated
        (Architecture §2.1) — and forwards this node's audio to the Scribe
        only when the gate hears speech in it.

        Returns this node's own level, so the operator's per-device meter
        shows what that phone is hearing rather than the room's merged
        maximum. ``None`` when nothing was processed.
        """
        if self._stopped or not pcm:
            return None
        if len(pcm) % 2:
            # Decoding a truncated frame would shift every sample one byte
            # out of phase and produce plausible-looking garbage.
            raise ValueError("payload is not 16-bit mono PCM (odd byte count)")

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        frame = compute_features(
            samples, sample_rate, prev_rms=self._prev_rms.get(node_id, 0.0)
        )
        self._prev_rms[node_id] = frame.rms

        now = self._clock()
        self._recent[node_id] = (now, frame)
        live = [
            f for _node, (heard, f) in self._recent.items()
            if now - heard <= self.merge_window_s
        ]
        await self.events.on_features(merge_frames(live))

        if self.events.on_speech_audio is not None and self.gate.is_speech(
            pcm, sample_rate
        ):
            await self.events.on_speech_audio(pcm, sample_rate)
        return frame.rms
