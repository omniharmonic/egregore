"""Voice activity detection — the speech path's gate (Architecture §2.1, PRD L-2, L-5).

Only audio a gate judges to contain speech is forwarded to the Scribe;
everything else (music, room noise, silence) is dropped at the edge and
never transcribed. Gates here take raw 16-bit PCM bytes and a sample rate
and return a plain bool — they hold no transcript text and never will.

Two implementations:

* :class:`WebRtcSpeechGate` — the real thing, backed by the optional
  ``webrtcvad`` package (a lazy import, per Cross-module rule 3). Robust to
  music/room noise because it reasons about spectral shape, not just level.
* :class:`EnergySpeechGate` — a zero-dependency fallback so the core install
  always has *some* gate. It is a plain amplitude gate against an adaptive
  noise floor and, being spectrum-blind, cannot tell speech from any other
  broadband loud sound (loud music will pass it). Document this limit to
  callers; it exists for CI/dev boxes without ``webrtcvad`` installed, not
  as a recommended production gate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

__all__ = ["SpeechGate", "WebRtcSpeechGate", "EnergySpeechGate", "make_gate"]


@runtime_checkable
class SpeechGate(Protocol):
    """Narrow VAD interface. Implementations hold no history of what they saw."""

    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool: ...


class WebRtcSpeechGate:
    """WebRTC's VAD, chunked into the 10/20/30 ms frames it requires.

    Args:
        aggressiveness: 0 (least aggressive, most false positives) to 3
            (most aggressive, most false negatives) — webrtcvad's own scale.
        frame_ms: internal chunk size; must be one of 10/20/30 (webrtcvad's
            supported set).
        speech_ratio: fraction of internal frames that must be flagged
            speech for the whole block to count as speech. A single 10-30 ms
            frame is a noisy unit; requiring a majority smooths that out
            without adding latency worth mentioning at block sizes of tens
            of ms.

    Raises ``RuntimeError`` at construction time if ``webrtcvad`` is not
    installed, with an install hint — never at call time, so a missing
    optional dependency fails fast and close to the mistake.
    """

    name = "webrtcvad"

    def __init__(
        self,
        aggressiveness: int = 2,
        *,
        frame_ms: int = 30,
        speech_ratio: float = 0.5,
    ) -> None:
        try:
            import webrtcvad  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "WebRTC VAD requires the webrtcvad package, which is not installed. "
                "Install it with: pip install 'egregore[mic]' "
                "(or use EnergySpeechGate / make_gate() for the no-dep fallback)"
            ) from exc
        if not 0 <= aggressiveness <= 3:
            raise ValueError("aggressiveness must be 0..3")
        if frame_ms not in (10, 20, 30):
            raise ValueError("frame_ms must be 10, 20, or 30 (webrtcvad requirement)")
        self.aggressiveness = aggressiveness
        self.frame_ms = frame_ms
        self.speech_ratio = speech_ratio
        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool:
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(
                f"webrtcvad requires 8000/16000/32000/48000 Hz, got {sample_rate}"
            )
        frame_bytes = int(sample_rate * self.frame_ms / 1000.0) * 2  # 16-bit mono
        if frame_bytes <= 0 or len(pcm_bytes) < frame_bytes:
            return False
        n_frames = 0
        n_speech = 0
        for i in range(0, len(pcm_bytes) - frame_bytes + 1, frame_bytes):
            chunk = pcm_bytes[i : i + frame_bytes]
            n_frames += 1
            if self._vad.is_speech(chunk, sample_rate):
                n_speech += 1
        if n_frames == 0:
            return False
        return (n_speech / n_frames) >= self.speech_ratio


class EnergySpeechGate:
    """No-dependency fallback: RMS above an adaptive noise floor.

    Limits — read before relying on this in a real room: it is a plain
    amplitude gate with no spectral reasoning, so it cannot distinguish
    speech from any other loud broadband sound (music, clapping, a blender).
    It exists so the core install always has *a* gate; prefer
    :class:`WebRtcSpeechGate` (via :func:`make_gate`) whenever the optional
    dependency is available.

    The noise floor only ever adapts downward toward quiet blocks — never
    upward from a loud one — so a sustained loud passage cannot drag the
    floor up and mask itself out.

    Args:
        threshold_ratio: a block counts as speech once its RMS exceeds
            ``floor * threshold_ratio``.
        floor_alpha: EMA weight applied when a block is quieter than the
            current floor estimate (0..1; higher adapts faster).
        min_floor: floor never adapts below this, so a silent room doesn't
            let the gate fire on floating-point dust.
    """

    name = "energy"

    def __init__(
        self,
        threshold_ratio: float = 2.5,
        *,
        floor_alpha: float = 0.1,
        min_floor: float = 0.003,
    ) -> None:
        if threshold_ratio <= 1.0:
            raise ValueError("threshold_ratio must be > 1.0")
        if not 0.0 < floor_alpha <= 1.0:
            raise ValueError("floor_alpha must be in (0, 1]")
        self.threshold_ratio = threshold_ratio
        self.floor_alpha = floor_alpha
        self.min_floor = min_floor
        self._floor = min_floor

    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(samples))))
        if rms < self._floor:
            self._floor = (1.0 - self.floor_alpha) * self._floor + self.floor_alpha * rms
        self._floor = max(self._floor, self.min_floor)
        return rms > self._floor * self.threshold_ratio


def make_gate(aggressiveness: int = 2) -> SpeechGate:
    """Prefer :class:`WebRtcSpeechGate`; fall back to :class:`EnergySpeechGate`.

    This is the factory the rest of the system should call rather than
    constructing a gate class directly — it is what keeps the core install
    (no ``webrtcvad``) working out of the box.
    """
    try:
        return WebRtcSpeechGate(aggressiveness=aggressiveness)
    except RuntimeError:
        return EnergySpeechGate()
