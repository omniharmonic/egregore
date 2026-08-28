"""LISTENER — per-zone audio capture split into two streams that never
rejoin, plus the mood integrator (Architecture §2.1, PRD §6.1).

* ``features``: the fast, always-on, content-blind feature path (~30 Hz).
* ``vad``: the gate that decides which audio reaches the Scribe.
* ``mood``: the 1-10 s middle temporal layer, content-blind by construction.
* ``sources``: the sources that drive a zone pipeline — ``FixtureSource``
  (the primary deliverable; drives the whole demo with no audio hardware)
  and ``MicSource`` (real USB mic, not exercised in CI).
"""

from __future__ import annotations

from .features import BAND_HIGH, BAND_LOW, BAND_MID, compute_features
from .mood import MoodIntegrator
from .network import NetworkSource, merge_frames
from .sources import FixtureSource, MicSource, ScriptLine, ZoneEvents, parse_script
from .vad import EnergySpeechGate, SpeechGate, WebRtcSpeechGate, make_gate

__all__ = [
    "NetworkSource",
    "merge_frames",
    "compute_features",
    "BAND_LOW",
    "BAND_MID",
    "BAND_HIGH",
    "MoodIntegrator",
    "SpeechGate",
    "WebRtcSpeechGate",
    "EnergySpeechGate",
    "make_gate",
    "ZoneEvents",
    "ScriptLine",
    "parse_script",
    "FixtureSource",
    "MicSource",
]
