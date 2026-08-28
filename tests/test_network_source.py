"""NetworkSource — audio that arrives over a socket instead of a device."""

from __future__ import annotations

import math
import struct

import pytest

from egregore.listener.network import NetworkSource, merge_frames
from egregore.listener.sources import ZoneEvents
from egregore.types import FeatureFrame


def pcm(amplitude: float, samples: int = 800, rate: int = 16000, freq: float = 220.0) -> bytes:
    """Mono 16-bit PCM of a sine at ``amplitude`` (0..1)."""
    out = bytearray()
    for i in range(samples):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<h", v)
    return bytes(out)


class Collect:
    def __init__(self):
        self.features: list[FeatureFrame] = []
        self.audio: list[tuple[bytes, int]] = []
        self.text: list[str] = []

    def events(self) -> ZoneEvents:
        async def on_features(f):
            self.features.append(f)

        async def on_text(t):
            self.text.append(t)

        async def on_audio(p, sr):
            self.audio.append((p, sr))

        return ZoneEvents(
            on_features=on_features, on_speech_text=on_text, on_speech_audio=on_audio
        )


class AlwaysSpeech:
    name = "always"

    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool:
        return True


class NeverSpeech:
    name = "never"

    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool:
        return False


async def test_pcm_in_produces_a_feature_frame_out():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", pcm(0.5), 16000)
    assert len(c.features) == 1
    assert c.features[0].rms > 0


async def test_silence_still_produces_features_but_no_speech():
    # The feature path is never gated (Architecture 2.1); the speech path is.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", pcm(0.0), 16000)
    assert len(c.features) == 1
    assert c.audio == []


async def test_speech_is_assembled_into_an_utterance_before_the_scribe_sees_it():
    # One 50ms frame is half a syllable. The Scribe must be handed a whole
    # utterance, released when the speaker pauses, or the ring buffer fills
    # with disconnected tokens no theme can be found in.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=AlwaysSpeech())
    for _ in range(20):                       # ~1s of speech
        await src.feed("n1", pcm(0.6), 16000)
    assert c.audio == [], "must not forward mid-sentence"

    src.gate = NeverSpeech()                  # the room falls quiet
    for _ in range(20):
        await src.feed("n1", pcm(0.0), 16000)
    assert len(c.audio) == 1
    utterance, rate = c.audio[0]
    assert rate == 16000
    assert len(utterance) > len(pcm(0.6)) * 15, "the whole utterance, not one frame"


async def test_two_nodes_keep_their_utterances_separate():
    # Two people talking at once must not have their sentences interleaved
    # into one another before the recogniser sees them.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=AlwaysSpeech())
    for _ in range(20):
        await src.feed("n1", pcm(0.4), 16000)
        await src.feed("n2", pcm(0.5), 16000)
    assert c.audio == []
    assert set(src.active_nodes()) == {"n1", "n2"}

    src.gate = NeverSpeech()
    for _ in range(20):
        await src.feed("n1", pcm(0.0), 16000)
        await src.feed("n2", pcm(0.0), 16000)
    assert len(c.audio) == 2, "one utterance each, not one merged stream"





def test_merge_takes_the_per_field_max():
    # The energy of a room is the loudest thing in it.
    a = FeatureFrame(t=1.0, rms=0.2, low=0.9, mid=0.1, high=0.1, centroid=0.3, onset=0.0)
    b = FeatureFrame(t=2.0, rms=0.8, low=0.1, mid=0.4, high=0.2, centroid=0.7, onset=0.5)
    m = merge_frames([a, b])
    assert m.rms == pytest.approx(0.8)
    assert m.low == pytest.approx(0.9)
    assert m.onset == pytest.approx(0.5)
    assert m.t == pytest.approx(2.0), "timestamp comes from the newest frame"


def test_merge_of_one_frame_is_that_frame():
    a = FeatureFrame(t=1.0, rms=0.2, low=0.9, mid=0.1, high=0.1, centroid=0.3, onset=0.0)
    assert merge_frames([a]).rms == pytest.approx(0.2)


def test_merge_of_nothing_is_an_error_not_a_fake_silence():
    with pytest.raises(ValueError, match="empty"):
        merge_frames([])


async def test_a_node_that_goes_quiet_ages_out_of_the_merge():
    clock = {"t": 1000.0}
    c = Collect()
    src = NetworkSource(
        c.events(), zone="k", gate=NeverSpeech(),
        merge_window_s=2.0, clock=lambda: clock["t"],
    )
    await src.feed("loud", pcm(0.9), 16000)
    clock["t"] += 5                      # 'loud' is now stale
    await src.feed("quiet", pcm(0.02), 16000)
    assert src.active_nodes() == ["quiet"]
    # The merged frame must reflect the quiet node alone, not a stale peak.
    assert c.features[-1].rms < c.features[0].rms


async def test_odd_length_payload_is_refused_rather_than_misread():
    # A truncated frame would otherwise be decoded one byte out of phase and
    # produce plausible-looking garbage.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    with pytest.raises(ValueError, match="16-bit"):
        await src.feed("n1", b"\x01\x02\x03", 16000)


async def test_empty_payload_is_ignored():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", b"", 16000)
    assert c.features == []


async def test_a_stopped_source_accepts_nothing_further():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=AlwaysSpeech())
    src.stop()
    await src.feed("n1", pcm(0.8), 16000)
    assert c.features == [] and c.audio == []
