"""Content-blind audio feature extraction (Architecture §2.1, PRD L-6).

Pure numpy: RMS, three-band spectral energy (low/mid/high), a normalized
spectral centroid, and an onset strength — all from a single rFFT per
frame. No librosa, no external audio dependency. This is the "fast path":
it runs at ~30 Hz per zone, is published straight to the Conductor's
feature bus, and never touches speech recognition — it carries loudness
and texture, never words (PRD §6.1 L-6, Architecture §2.1).
"""

from __future__ import annotations

import time

import numpy as np

from egregore.types import FeatureFrame

__all__ = ["compute_features", "BAND_LOW", "BAND_MID", "BAND_HIGH"]

# Band edges in Hz (Architecture §2.1: "three-band spectral energy").
BAND_LOW = (20.0, 250.0)
BAND_MID = (250.0, 2000.0)
BAND_HIGH = (2000.0, 8000.0)

# Soft-companding gain for band energies: tanh(mean_magnitude * gain).
# Tuned so ordinary speech/music levels land in the visually useful middle
# of 0..1 instead of pinned near the rails.
_BAND_GAIN = 40.0

# Onset scaling: a positive RMS delta of this size or more saturates to 1.0.
_ONSET_GAIN = 8.0


def compute_features(
    pcm: np.ndarray,
    sample_rate: int,
    prev_rms: float = 0.0,
) -> FeatureFrame:
    """Compute one content-blind feature frame from a mono PCM block.

    Args:
        pcm: float32 mono samples in [-1, 1]. Any non-empty length; callers
            typically pass ~``sample_rate / 30`` samples for a ~30 Hz frame.
        sample_rate: samples/sec of ``pcm``.
        prev_rms: the previous frame's ``rms``, used to derive ``onset`` as a
            positive delta. Callers own this bit of state across frames —
            this function is otherwise pure and holds none itself.

    Returns:
        A :class:`~egregore.types.FeatureFrame` stamped with ``time.time()``.

    Deterministic and cheap: one windowed rFFT, a handful of numpy
    reductions. This function only ever looks at energy and spectral shape —
    it is content-blind by construction and must stay that way.
    """
    samples = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n = samples.size
    if n == 0 or sample_rate <= 0:
        return FeatureFrame(
            t=time.time(), rms=0.0, low=0.0, mid=0.0, high=0.0, centroid=0.0, onset=0.0
        )

    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    rms = float(np.clip(rms, 0.0, 1.0))

    window = np.hanning(n).astype(np.float32) if n > 1 else np.ones(n, dtype=np.float32)
    spec = np.fft.rfft(samples * window)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    nyquist = sample_rate / 2.0

    low = _band_energy(mag, freqs, *BAND_LOW)
    mid = _band_energy(mag, freqs, *BAND_MID)
    high = _band_energy(mag, freqs, *BAND_HIGH)

    mag_sum = float(np.sum(mag))
    if mag_sum > 1e-12 and nyquist > 0:
        centroid_hz = float(np.sum(freqs * mag) / mag_sum)
        centroid = float(np.clip(centroid_hz / nyquist, 0.0, 1.0))
    else:
        centroid = 0.0

    delta = rms - float(prev_rms)
    onset = float(np.clip(max(0.0, delta) * _ONSET_GAIN, 0.0, 1.0))

    return FeatureFrame(
        t=time.time(),
        rms=rms,
        low=low,
        mid=mid,
        high=high,
        centroid=centroid,
        onset=onset,
    )


def _band_energy(mag: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    """Mean FFT magnitude in ``[lo, hi)`` Hz, soft-companded to 0..1 via tanh."""
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    energy = float(np.mean(mag[mask]))
    return float(np.tanh(energy * _BAND_GAIN))
