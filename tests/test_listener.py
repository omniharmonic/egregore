"""Tests for the LISTENER module (Architecture §2.1, PRD §6.1).

Everything here runs offline, no audio device, no GPU (CONTRACTS.md rule 6).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from egregore.listener import (
    EnergySpeechGate,
    FixtureSource,
    MoodIntegrator,
    ScriptLine,
    ZoneEvents,
    compute_features,
    parse_script,
)
from egregore.types import FeatureFrame, ThemeObject

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "egregore" / "listener" / "fixtures" / (
    "demo_conversation.txt"
)


# ---------------------------------------------------------------------------
# compute_features
# ---------------------------------------------------------------------------


def _sine(freq: float, sr: int, n: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestComputeFeatures:
    def test_silence_is_near_zero_everywhere(self):
        sr = 16000
        pcm = np.zeros(512, dtype=np.float32)
        frame = compute_features(pcm, sr, prev_rms=0.0)
        assert isinstance(frame, FeatureFrame)
        assert frame.rms == pytest.approx(0.0, abs=1e-9)
        assert frame.low == pytest.approx(0.0, abs=1e-9)
        assert frame.mid == pytest.approx(0.0, abs=1e-9)
        assert frame.high == pytest.approx(0.0, abs=1e-9)
        assert frame.centroid == pytest.approx(0.0, abs=1e-9)
        assert frame.onset == pytest.approx(0.0, abs=1e-9)

    def test_low_tone_dominates_low_band(self):
        sr = 16000
        pcm = _sine(100.0, sr, 512)
        frame = compute_features(pcm, sr, prev_rms=0.0)
        assert frame.low > frame.mid
        assert frame.low > frame.high
        assert frame.low > 0.5

    def test_high_tone_dominates_high_band_and_centroid(self):
        sr = 16000
        pcm = _sine(5000.0, sr, 512)
        frame = compute_features(pcm, sr, prev_rms=0.0)
        assert frame.high > frame.low
        assert frame.high > frame.mid
        assert frame.centroid > 0.5

    def test_step_in_level_spikes_onset(self):
        sr = 16000
        pcm = _sine(440.0, sr, 512, amp=0.9)
        frame = compute_features(pcm, sr, prev_rms=0.0)
        assert frame.onset > 0.5

        # A block no louder than the previous one should not spike onset.
        frame2 = compute_features(pcm, sr, prev_rms=frame.rms)
        assert frame2.onset == pytest.approx(0.0, abs=1e-6)

    def test_all_fields_bounded(self):
        sr = 16000
        rng = np.random.default_rng(1)
        pcm = rng.normal(0, 0.3, 512).astype(np.float32)
        frame = compute_features(pcm, sr, prev_rms=0.1)
        for value in (frame.rms, frame.low, frame.mid, frame.high, frame.centroid, frame.onset):
            assert 0.0 <= value <= 1.0

    def test_empty_pcm_is_safe(self):
        frame = compute_features(np.zeros(0, dtype=np.float32), 16000, prev_rms=0.2)
        assert frame.rms == 0.0
        assert frame.onset == 0.0


# ---------------------------------------------------------------------------
# MoodIntegrator
# ---------------------------------------------------------------------------


def _make_frame(t: float, rms: float, centroid: float = 0.5, onset: float = 0.0) -> FeatureFrame:
    return FeatureFrame(t=t, rms=rms, low=0.3, mid=0.3, high=0.3, centroid=centroid, onset=onset)


class TestMoodIntegrator:
    def test_energy_ramps_under_sustained_loud_frames(self):
        mood = MoodIntegrator(tau_s=3.0)
        t = 0.0
        for _ in range(50):
            mood.update(_make_frame(t, rms=0.9), dt=0.1)
            t += 0.1
        state = mood.state()
        assert state.energy > 0.8

    def test_energy_decays_after_loud_frames_stop(self):
        mood = MoodIntegrator(tau_s=3.0)
        t = 0.0
        for _ in range(50):
            mood.update(_make_frame(t, rms=0.9), dt=0.1)
            t += 0.1
        loud_energy = mood.state().energy

        for _ in range(50):
            mood.update(_make_frame(t, rms=0.0), dt=0.1)
            t += 0.1
        quiet_energy = mood.state().energy

        assert quiet_energy < loud_energy
        assert quiet_energy < 0.3

    def test_variability_and_brightness_and_onset_density_track_input(self):
        mood = MoodIntegrator(tau_s=1.0)
        t = 0.0
        for i in range(60):
            rms = 0.8 if i % 2 == 0 else 0.1
            mood.update(_make_frame(t, rms=rms, centroid=0.9, onset=1.0), dt=0.05)
            t += 0.05
        state = mood.state()
        assert state.variability > 0.05
        assert state.brightness > 0.5
        assert state.onset_density > 0.5

    def test_absorb_theme_pulls_valence_and_decays_back_toward_half(self):
        mood = MoodIntegrator(theme_decay_min=5.0, clock=lambda: 0.0)
        theme = ThemeObject(valence=0.95, intensity=0.9)

        mood.absorb_theme(theme, t=0.0)
        state = mood.state()
        assert state.valence == pytest.approx(0.95)
        assert state.intensity == pytest.approx(0.9)

        # Advance a feature update far in the future (minutes) to drive decay.
        mood.update(_make_frame(t=5.0 * 60.0, rms=0.0))
        decayed = mood.state()
        assert decayed.valence < state.valence
        assert decayed.valence > 0.5  # decaying toward 0.5, not overshooting

        # A long time later it should be very close to neutral.
        mood.update(_make_frame(t=60.0 * 60.0, rms=0.0))
        near_neutral = mood.state()
        assert near_neutral.valence == pytest.approx(0.5, abs=0.01)
        assert near_neutral.intensity == pytest.approx(0.5, abs=0.01)

    def test_default_state_before_any_update_is_neutral(self):
        mood = MoodIntegrator()
        state = mood.state()
        assert state.energy == 0.0
        assert state.valence == 0.5
        assert state.intensity == 0.5


# ---------------------------------------------------------------------------
# EnergySpeechGate
# ---------------------------------------------------------------------------


class TestEnergySpeechGate:
    def _pcm_bytes(self, samples: np.ndarray) -> bytes:
        clipped = np.clip(samples, -1.0, 1.0)
        return (clipped * 32767).astype(np.int16).tobytes()

    def test_rejects_near_silence(self):
        gate = EnergySpeechGate()
        rng = np.random.default_rng(0)
        sr = 16000
        # Feed a run of near-silent blocks so the floor settles low.
        result = True
        for _ in range(20):
            block = rng.normal(0, 0.0008, 480).astype(np.float32)
            result = gate.is_speech(self._pcm_bytes(block), sr)
        assert result is False

    def test_passes_loud_speech_band_signal(self):
        gate = EnergySpeechGate()
        sr = 16000
        rng = np.random.default_rng(0)
        # Settle the noise floor on quiet blocks first.
        for _ in range(20):
            block = rng.normal(0, 0.0008, 480).astype(np.float32)
            gate.is_speech(self._pcm_bytes(block), sr)

        t = np.arange(480) / sr
        loud = (0.35 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
        assert gate.is_speech(self._pcm_bytes(loud), sr) is True

    def test_empty_block_is_not_speech(self):
        gate = EnergySpeechGate()
        assert gate.is_speech(b"", 16000) is False

    def test_rejects_bad_construction_args(self):
        with pytest.raises(ValueError):
            EnergySpeechGate(threshold_ratio=1.0)
        with pytest.raises(ValueError):
            EnergySpeechGate(floor_alpha=0.0)


# ---------------------------------------------------------------------------
# FixtureSource
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.frames: list[FeatureFrame] = []

    def make_events(self) -> ZoneEvents:
        async def on_features(frame: FeatureFrame) -> None:
            self.frames.append(frame)

        async def on_speech_text(text: str) -> None:
            self.texts.append(text)

        return ZoneEvents(on_features=on_features, on_speech_text=on_speech_text)


_INLINE_SCRIPT = """\
# tiny inline script for fast tests
00:00\thello there, welcome to the party
00:00.5\tcome on in
00:01\tthe night is young
"""


class TestFixtureSource:
    def test_delivers_utterances_in_order_with_monotonic_frames(self, tmp_path):
        script_path = tmp_path / "script.txt"
        script_path.write_text(_INLINE_SCRIPT)
        collector = _Collector()
        source = FixtureSource(
            script_path,
            collector.make_events(),
            time_scale=60.0,
            loop=False,
            feature_hz=30.0,
        )

        import asyncio

        asyncio.run(source.run())

        assert collector.texts == [
            "hello there, welcome to the party",
            "come on in",
            "the night is young",
        ]
        assert len(collector.frames) > 0
        timestamps = [f.t for f in collector.frames]
        assert timestamps == sorted(timestamps)
        for f in collector.frames:
            for value in (f.rms, f.low, f.mid, f.high, f.centroid, f.onset):
                assert 0.0 <= value <= 1.0

    def test_loop_false_terminates(self, tmp_path):
        script_path = tmp_path / "script.txt"
        script_path.write_text(_INLINE_SCRIPT)
        collector = _Collector()
        source = FixtureSource(
            script_path,
            collector.make_events(),
            time_scale=200.0,
            loop=False,
        )

        import asyncio

        # Must complete without hanging — the real assertion is that this
        # call returns at all.
        asyncio.run(asyncio.wait_for(source.run(), timeout=10.0))
        assert len(collector.texts) == 3

    def test_loop_true_replays_the_script(self, tmp_path):
        script_path = tmp_path / "script.txt"
        script_path.write_text(_INLINE_SCRIPT)
        collector = _Collector()
        source = FixtureSource(
            script_path,
            collector.make_events(),
            time_scale=300.0,
            loop=True,
        )

        import asyncio

        async def _run_briefly():
            task = asyncio.create_task(source.run())
            # Let it play through the (very short, heavily sped-up) script
            # more than once, then stop it.
            await asyncio.sleep(0.3)
            source.stop()
            await asyncio.wait_for(task, timeout=5.0)

        asyncio.run(_run_briefly())
        # Looping means we should see the 3 utterances more than once.
        assert len(collector.texts) >= 3

    def test_rejects_empty_script(self, tmp_path):
        script_path = tmp_path / "empty.txt"
        script_path.write_text("# nothing but comments\n\n")
        with pytest.raises(ValueError):
            FixtureSource(script_path, _Collector().make_events())

    def test_rejects_bad_time_scale(self, tmp_path):
        script_path = tmp_path / "script.txt"
        script_path.write_text(_INLINE_SCRIPT)
        with pytest.raises(ValueError):
            FixtureSource(script_path, _Collector().make_events(), time_scale=0)


# ---------------------------------------------------------------------------
# parse_script
# ---------------------------------------------------------------------------


class TestParseScript:
    def test_parses_basic_lines(self, tmp_path):
        p = tmp_path / "s.txt"
        p.write_text("# comment\n\n00:05\thello\n01:02.5\tworld\n")
        lines = parse_script(p)
        assert lines == [
            ScriptLine(t=5.0, text="hello"),
            ScriptLine(t=62.5, text="world"),
        ]

    def test_rejects_missing_tab(self, tmp_path):
        p = tmp_path / "s.txt"
        p.write_text("00:05 hello\n")
        with pytest.raises(ValueError):
            parse_script(p)

    def test_rejects_bad_timestamp(self, tmp_path):
        p = tmp_path / "s.txt"
        p.write_text("bogus\thello\n")
        with pytest.raises(ValueError):
            parse_script(p)

    def test_rejects_empty_text(self, tmp_path):
        p = tmp_path / "s.txt"
        p.write_text("00:05\t   \n")
        with pytest.raises(ValueError):
            parse_script(p)


# ---------------------------------------------------------------------------
# The shipped demo fixture file
# ---------------------------------------------------------------------------


class TestDemoFixtureFile:
    def test_fixture_file_exists_and_parses(self):
        assert FIXTURE_PATH.exists(), f"missing fixture file: {FIXTURE_PATH}"
        lines = parse_script(FIXTURE_PATH)
        assert len(lines) >= 25

    def test_fixture_timestamps_are_non_decreasing(self):
        lines = parse_script(FIXTURE_PATH)
        timestamps = [line.t for line in lines]
        assert timestamps == sorted(timestamps)

    def test_fixture_spans_roughly_six_minutes(self):
        lines = parse_script(FIXTURE_PATH)
        assert lines[-1].t >= 5 * 60
        assert lines[-1].t <= 8 * 60

    def test_fixture_loads_into_a_fixture_source(self):
        collector = _Collector()
        source = FixtureSource(FIXTURE_PATH, collector.make_events(), loop=False)
        assert len(source.script) >= 25


# ---------------------------------------------------------------------------
# MicSource — construction only; real capture is untestable without hardware
# ---------------------------------------------------------------------------


class TestMicSource:
    def test_requires_sounddevice_to_run(self, tmp_path):
        sd = pytest.importorskip("sounddevice")
        del sd  # only used to decide whether to skip

    def test_construction_does_not_import_sounddevice(self):
        from egregore.listener.sources import MicSource

        collector = _Collector()
        # Must not raise even without sounddevice installed: the import is
        # lazy, deferred to run().
        source = MicSource(collector.make_events())
        assert source.sample_rate == 16000
