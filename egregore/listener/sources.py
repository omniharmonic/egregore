"""Audio/text sources feeding a zone's Listener pipeline (Architecture §2.1).

A source's only job is to produce two independent streams for one zone and
hand them to whatever the integration layer wired up via a
:class:`ZoneEvents` callback bundle: fast content-blind
:class:`~egregore.types.FeatureFrame`\\ s, and gated speech — either text
(fixture demo mode, since there is no ASR hardware in this environment) or
raw PCM (a real mic, forwarded to the Scribe). The two streams never rejoin
inside this module.

:class:`FixtureSource` is the primary deliverable here: with no audio
hardware available, it is what drives the whole demo end to end (Demo mode,
CONTRACTS.md). :class:`MicSource` is the real-hardware counterpart and is
not exercised in CI.

Network/Pi Opus streaming (Architecture §2.1 deployment option B/C) is out
of scope for v1. A future network source would decode Opus to the same
16 kHz mono PCM shape :class:`MicSource` already produces and reuse the same
gate + feature pipeline unchanged — the seam is ``ZoneEvents``, not this
module's internals.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from egregore.types import FeatureFrame

from .features import compute_features
from .utterance import UtteranceAssembler
from .vad import SpeechGate, make_gate

logger = logging.getLogger(__name__)

__all__ = [
    "ZoneEvents",
    "ScriptLine",
    "parse_script",
    "FixtureSource",
    "MicSource",
]


# ---------------------------------------------------------------------------
# The callback bundle the integration layer passes in (Architecture §2.1)
# ---------------------------------------------------------------------------


@dataclass
class ZoneEvents:
    """Per-zone callbacks a source drives. Owned and wired by the integration
    layer (``egregore/app.py`` — CONTRACTS.md), never by a module.

    Attributes:
        on_features: called for every ~30 Hz feature frame, always — the
            fast path is never gated.
        on_speech_text: called with already-transcribed text. Fixture mode
            delivers text directly here (there is no ASR hardware to
            exercise), keeping the demo path structurally simple while
            staying content-blind *within this module* — the text is only
            ever handed onward, never stored or inspected here.
        on_speech_audio: called with gated raw PCM + its sample rate for a
            real mic, so the Scribe can transcribe it. ``None`` for sources
            (like the fixture) that have no audio to give the Scribe.
    """

    on_features: Callable[[FeatureFrame], Awaitable[None]]
    on_speech_text: Callable[[str], Awaitable[None]]
    on_speech_audio: Callable[[bytes, int], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Script parsing — shared by FixtureSource and the fixture-file test
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptLine:
    """One parsed line of a fixture conversation script."""

    t: float  # seconds from script start
    text: str


_TIMESTAMP_RE = re.compile(r"^(\d+):(\d+(?:\.\d+)?)$")


def _parse_timestamp(raw: str) -> float:
    m = _TIMESTAMP_RE.match(raw)
    if not m:
        raise ValueError(f"bad timestamp {raw!r}; expected MM:SS or MM:SS.s")
    minutes = int(m.group(1))
    seconds = float(m.group(2))
    if seconds >= 60:
        raise ValueError(f"bad timestamp {raw!r}: seconds field must be < 60")
    return minutes * 60.0 + seconds


def parse_script(path: str | Path) -> list[ScriptLine]:
    """Parse a fixture conversation script.

    Format: one utterance per line, ``MM:SS<TAB>utterance text`` (also
    accepts ``MM:SS.s``). Blank lines and lines starting with ``#`` (after
    stripping leading whitespace) are ignored. Raises ``ValueError`` naming
    the file and line number on any malformed line.
    """
    p = Path(path)
    lines: list[ScriptLine] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"{p}:{lineno}: expected 'MM:SS<TAB>text', got {raw!r}")
        ts_raw, _, text = line.partition("\t")
        text = text.strip()
        if not text:
            raise ValueError(f"{p}:{lineno}: empty utterance text")
        try:
            t = _parse_timestamp(ts_raw.strip())
        except ValueError as exc:
            raise ValueError(f"{p}:{lineno}: {exc}") from exc
        lines.append(ScriptLine(t=t, text=text))
    return lines


# ---------------------------------------------------------------------------
# FixtureSource — the demo driver
# ---------------------------------------------------------------------------


class FixtureSource:
    """Replays a scripted conversation as if it were live zone audio.

    The primary deliverable of this module: with no audio hardware in this
    environment, this is what drives the whole demo (CONTRACTS.md, Demo
    mode). Between utterances it synthesizes plausible 30 Hz
    :class:`~egregore.types.FeatureFrame`\\ s procedurally — a slow
    sinusoidal "music" bed plus a burst of higher rms/centroid around each
    utterance's timestamp, so the visuals visibly react around speech — and
    at each utterance's timestamp it calls ``events.on_speech_text`` with
    the line's text directly (no ASR: see :class:`ZoneEvents`).

    Args:
        path: fixture script file (see :func:`parse_script` for format).
        events: the zone's callback bundle.
        time_scale: playback speed multiplier; > 1 plays faster than real
            time (e.g. 60 = one script-second per real-second/60).
        loop: restart the script when it finishes. Defaults to True so a
            long-running demo keeps talking; set False to run one pass.
        feature_hz: synthetic feature frame rate.
        seed: seed for the small amount of noise mixed into the synthetic
            bed, for reproducible-looking demos.
        tail_s: how many script-seconds of feature frames to keep emitting
            after the last utterance before looping/ending.

    ``run()`` is a long-lived coroutine; cancel it or call :meth:`stop` to
    end it (stop takes effect at the next scheduled event, not immediately,
    same as a cooperative cancellation).
    """

    def __init__(
        self,
        path: str | Path,
        events: ZoneEvents,
        *,
        time_scale: float = 1.0,
        loop: bool = True,
        feature_hz: float = 30.0,
        seed: int = 0,
        tail_s: float = 2.0,
    ) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be positive")
        if feature_hz <= 0:
            raise ValueError("feature_hz must be positive")
        self.path = Path(path)
        self.events = events
        self.time_scale = float(time_scale)
        self.loop = loop
        self.feature_hz = float(feature_hz)
        self.tail_s = float(tail_s)
        self.script = parse_script(self.path)
        if not self.script:
            raise ValueError(f"{self.path}: script contains no utterances")
        self._rng = np.random.default_rng(seed)
        self._stopped = False
        self._prev_synth_rms = 0.0

    def stop(self) -> None:
        """Signal the run loop to end at its next scheduled event."""
        self._stopped = True

    async def run(self) -> None:
        """Play the script, looping per ``self.loop``, until stopped."""
        self._stopped = False
        while not self._stopped:
            await self._run_once()
            if not self.loop:
                return

    async def _run_once(self) -> None:
        end_t = self.script[-1].t + self.tail_s
        frame_period = 1.0 / self.feature_hz

        # Build one merged, time-ordered timeline of feature ticks and
        # utterances. Feature frames sort before an utterance that lands on
        # the exact same instant, so speech text always arrives alongside
        # (not strictly before) the feature burst it's meant to accompany.
        n_ticks = int(math.floor(end_t / frame_period)) + 1
        events: list[tuple[float, int, ScriptLine | None]] = [
            (i * frame_period, 0, None) for i in range(n_ticks)
        ]
        events.extend((line.t, 1, line) for line in self.script)
        events.sort(key=lambda e: (e[0], e[1]))

        last_t = 0.0
        self._prev_synth_rms = 0.0
        for virtual_t, _kind, line in events:
            if self._stopped:
                return
            wait_s = (virtual_t - last_t) / self.time_scale
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            last_t = virtual_t
            if line is None:
                frame = self._synthetic_feature(virtual_t)
                await self.events.on_features(frame)
            else:
                await self.events.on_speech_text(line.text)

    def _synthetic_feature(self, virtual_t: float) -> FeatureFrame:
        """Procedural stand-in feature frame: music bed + speech-proximity burst."""
        bed = 0.12 + 0.06 * math.sin(2 * math.pi * virtual_t / 19.0)
        bed += 0.03 * math.sin(2 * math.pi * virtual_t / 4.7 + 1.0)

        nearest = min((abs(virtual_t - line.t) for line in self.script), default=1e9)
        sigma = 1.2
        burst = 0.55 * math.exp(-(nearest**2) / (2 * sigma**2))

        noise = float(self._rng.normal(0.0, 0.015))
        rms = float(np.clip(bed + burst + noise, 0.0, 1.0))

        # Music skews low; the closer we are to an utterance, the more the
        # spectral shape skews toward mid/high, like a voice cutting through.
        speech_frac = float(np.clip(burst / (burst + 0.2), 0.0, 1.0))
        low = float(
            np.clip(
                0.4 + 0.1 * math.sin(virtual_t / 5.3) - 0.2 * speech_frac
                + self._rng.normal(0.0, 0.02),
                0.0,
                1.0,
            )
        )
        mid = float(
            np.clip(0.15 + 0.55 * speech_frac + self._rng.normal(0.0, 0.02), 0.0, 1.0)
        )
        high = float(
            np.clip(0.08 + 0.3 * speech_frac + self._rng.normal(0.0, 0.015), 0.0, 1.0)
        )
        centroid = float(
            np.clip(0.22 + 0.45 * speech_frac + self._rng.normal(0.0, 0.02), 0.0, 1.0)
        )

        onset = float(np.clip(max(0.0, rms - self._prev_synth_rms) * 6.0, 0.0, 1.0))
        self._prev_synth_rms = rms

        return FeatureFrame(
            t=time.time(),
            rms=rms,
            low=low,
            mid=mid,
            high=high,
            centroid=centroid,
            onset=onset,
        )


# ---------------------------------------------------------------------------
# MicSource — real USB mic (Architecture §2.1, deployment option A)
# ---------------------------------------------------------------------------


class MicSource:
    """Direct USB microphone capture (deployment option A).

    Lazily imports ``sounddevice`` (Cross-module rule 3); construction never
    imports it, only ``run()`` does, so building/holding an instance is safe
    on a box with no audio device. Will not run in CI — tests reach this
    class only via ``pytest.importorskip("sounddevice")``.

    Captures 16 kHz mono int16 blocks. Every block is turned into a
    :class:`~egregore.types.FeatureFrame` via
    :func:`~egregore.listener.features.compute_features` and forwarded to
    ``events.on_features`` unconditionally — the feature path is always on
    and never VAD-gated (Architecture §2.1). Each block is also run through
    a :class:`~egregore.listener.vad.SpeechGate`; only blocks judged speech
    are forwarded (as raw PCM) to ``events.on_speech_audio``, for the
    Scribe.
    """

    def __init__(
        self,
        events: ZoneEvents,
        *,
        device: str | int | None = None,
        sample_rate: int = 16000,
        block_ms: float = 33.0,
        gate: SpeechGate | None = None,
    ) -> None:
        self.events = events
        self.device = device
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.gate = gate if gate is not None else make_gate()
        self._utterances = UtteranceAssembler(sample_rate=sample_rate)
        self._stopped = False
        self._prev_rms = 0.0

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "MicSource requires the sounddevice package, which is not installed. "
                "Install it with: pip install 'egregore[mic]'"
            ) from exc

        block_frames = max(1, int(self.sample_rate * self.block_ms / 1000.0))
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _callback(indata: object, frames: int, time_info: object, status: object) -> None:
            pcm = bytes(indata)  # type: ignore[call-overload]
            loop.call_soon_threadsafe(queue.put_nowait, pcm)

        self._stopped = False
        with sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=block_frames,
            device=self.device,
            callback=_callback,
        ):
            while not self._stopped:
                pcm_bytes = await queue.get()
                await self._process_block(pcm_bytes)

    async def _process_block(self, pcm_bytes: bytes) -> None:
        samples_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        samples_f32 = samples_i16.astype(np.float32) / 32768.0
        frame = compute_features(samples_f32, self.sample_rate, self._prev_rms)
        self._prev_rms = frame.rms
        await self.events.on_features(frame)

        if self.events.on_speech_audio is not None:
            # Accumulate into whole utterances. A recogniser handed one 33ms
            # block returns half a syllable; handed a sentence it returns a
            # sentence, which is the difference between a ring buffer full of
            # disconnected tokens and one a theme can be found in.
            utterance = self._utterances.add(
                pcm_bytes, self.gate.is_speech(pcm_bytes, self.sample_rate)
            )
            if utterance is not None:
                await self.events.on_speech_audio(utterance, self.sample_rate)
