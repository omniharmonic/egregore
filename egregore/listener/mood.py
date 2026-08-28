"""The mood integrator — the middle temporal layer (Architecture §2.1, PRD §3).

PRD §3 names three temporal layers (fast/mood/slow); the fast feature path
and the slow theme-extraction cadence are the outer two, and this is the
middle one — a rolling 1-10 second summary derived purely from audio
features, plus a slow decay of the last validated theme's ``valence`` and
``intensity`` back toward neutral (0.5). It is content-blind by
construction: :class:`MoodIntegrator` only ever consumes
:class:`~egregore.types.FeatureFrame` (numbers) and
:class:`~egregore.types.ThemeObject` (already-validated, already abstract),
never raw transcript text.

It does two jobs (Architecture §2.1): it gives the shader layer a
slow-moving envelope to modulate against, and it biases prompt synthesis so
a quiet, low-energy room produces different imagery from a room at peak even
when the words are similar.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from egregore.types import FeatureFrame, MoodState, ThemeObject

__all__ = ["MoodIntegrator"]

# Default attack/release time constant for the fast-feature EMAs, per
# Architecture §2.1 ("rolling 1-10 second summary"). Symmetric attack and
# release: this is a plain envelope follower, not asymmetric attack/decay.
_DEFAULT_TAU_S = 3.0

# Default time constant for the theme valence/intensity decay back to 0.5.
# Minutes, not seconds — a theme should color the mood for a while after the
# conversation has moved on, not vanish within one integration window.
_DEFAULT_THEME_DECAY_MIN = 5.0


def _ema_alpha(dt: float, tau_s: float) -> float:
    """Exponential-moving-average weight for a step of ``dt`` seconds."""
    if dt <= 0.0 or tau_s <= 0.0:
        return 0.0
    return 1.0 - math.exp(-dt / tau_s)


def _ema_step(current: float, target: float, alpha: float) -> float:
    return current + alpha * (target - current)


class MoodIntegrator:
    """Rolling content-blind mood summary for one zone.

    Args:
        tau_s: time constant (seconds) for the fast-feature EMAs (energy,
            variability, onset_density, brightness). ~1-10 s per
            Architecture §2.1; defaults to 3 s.
        theme_decay_min: time constant (minutes) for the slow decay of
            ``valence``/``intensity`` back toward 0.5 after a theme is
            absorbed.
        clock: wall-clock source used only as a fallback when a caller does
            not supply an explicit ``t``/``dt``. Injectable for tests.

    ``update()`` and ``absorb_theme()`` both accept an explicit ``dt`` (or
    ``t``) so tests can drive the integrator through simulated seconds or
    minutes without a real sleep.
    """

    def __init__(
        self,
        *,
        tau_s: float = _DEFAULT_TAU_S,
        theme_decay_min: float = _DEFAULT_THEME_DECAY_MIN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if tau_s <= 0:
            raise ValueError("tau_s must be positive")
        if theme_decay_min <= 0:
            raise ValueError("theme_decay_min must be positive")
        self.tau_s = float(tau_s)
        self.theme_tau_s = float(theme_decay_min) * 60.0
        self._clock = clock

        self._energy = 0.0
        self._variability = 0.0
        self._onset_density = 0.0
        self._brightness = 0.0
        self._valence = 0.5
        self._intensity = 0.5

        self._last_feature_t: float | None = None
        self._last_theme_t: float | None = None
        self._feature_initialized = False
        self._updated_at = self._clock()

    # -- fast-feature accumulation ----------------------------------------

    def update(self, frame: FeatureFrame, dt: float | None = None) -> None:
        """Fold one feature frame into the rolling EMAs.

        ``dt`` (seconds since the previous ``update``) is inferred from
        ``frame.t`` deltas when omitted; pass it explicitly to drive the
        integrator deterministically in tests. Also advances the theme decay
        clock so mood state read via ``state()`` reflects both processes as
        of the same instant.
        """
        if dt is None:
            dt = 0.0 if self._last_feature_t is None else max(0.0, frame.t - self._last_feature_t)
        self._last_feature_t = frame.t

        if not self._feature_initialized:
            # First-ever sample: initialize directly rather than EMA-ing from
            # zero, so a single loud frame doesn't read as near-silence.
            self._energy = frame.rms
            self._variability = 0.0
            self._onset_density = frame.onset
            self._brightness = frame.centroid
            self._feature_initialized = True
        else:
            alpha = _ema_alpha(dt, self.tau_s)
            prev_energy = self._energy
            self._energy = _ema_step(self._energy, frame.rms, alpha)
            self._variability = _ema_step(self._variability, abs(frame.rms - prev_energy), alpha)
            self._onset_density = _ema_step(self._onset_density, frame.onset, alpha)
            self._brightness = _ema_step(self._brightness, frame.centroid, alpha)

        self._decay_theme(frame.t)
        self._updated_at = frame.t

    # -- slow theme decay ---------------------------------------------------

    def absorb_theme(self, theme: ThemeObject, t: float | None = None) -> None:
        """Fold a freshly validated theme's valence/intensity in.

        Snaps ``valence``/``intensity`` to the theme's values, then lets them
        decay back toward 0.5 over ``theme_decay_min`` minutes as later calls
        (to ``update`` or ``absorb_theme``) advance the clock. Content-blind:
        ``ThemeObject`` here is already the validated, abstract output of the
        Weaver — no transcript text reaches this class.
        """
        now = self._clock() if t is None else float(t)
        self._decay_theme(now)
        self._valence = float(theme.valence)
        self._intensity = float(theme.intensity)
        self._last_theme_t = now
        self._updated_at = now

    def _decay_theme(self, now: float) -> None:
        if self._last_theme_t is None:
            self._last_theme_t = now
            return
        dt = max(0.0, now - self._last_theme_t)
        if dt <= 0.0:
            return
        alpha = _ema_alpha(dt, self.theme_tau_s)
        self._valence = _ema_step(self._valence, 0.5, alpha)
        self._intensity = _ema_step(self._intensity, 0.5, alpha)
        self._last_theme_t = now

    # -- read -----------------------------------------------------------

    def state(self) -> MoodState:
        """Snapshot the current mood. Cheap; safe to call every tick."""
        return MoodState(
            energy=self._energy,
            variability=self._variability,
            onset_density=self._onset_density,
            brightness=self._brightness,
            valence=self._valence,
            intensity=self._intensity,
            updated_at=self._updated_at,
        )
