"""Utterance assembly — the difference between a sentence and a syllable.

The bug these exist to prevent: handing a recogniser one 33ms block at a time,
which returns a word or nothing and fills the ring buffer with disconnected
tokens no theme can be found in.
"""

from __future__ import annotations

from egregore.listener.utterance import UtteranceAssembler

RATE = 16000


def block(ms: float) -> bytes:
    """A block of ``ms`` milliseconds of 16-bit mono PCM."""
    return b"\x00\x01" * int(RATE * ms / 1000.0)


def ms_of(pcm: bytes) -> float:
    return (len(pcm) / 2) / RATE * 1000.0


def test_speech_blocks_accumulate_rather_than_releasing_one_at_a_time():
    a = UtteranceAssembler(sample_rate=RATE, hangover_ms=300)
    for _ in range(30):                       # ~1s of speech in 33ms blocks
        assert a.add(block(33), True) is None, "must not release mid-sentence"
    assert a.buffered_ms > 900


def test_a_pause_releases_the_whole_utterance():
    a = UtteranceAssembler(sample_rate=RATE, hangover_ms=300, min_utterance_ms=100)
    for _ in range(30):
        a.add(block(33), True)
    out = None
    for _ in range(10):                       # silence
        out = a.add(block(33), False) or out
    assert out is not None
    # ~1s of speech plus the trailing silence that ended it.
    assert 1000 < ms_of(out) < 1500


def test_trailing_silence_is_kept_so_final_consonants_survive():
    a = UtteranceAssembler(sample_rate=RATE, hangover_ms=200, min_utterance_ms=100)
    for _ in range(20):
        a.add(block(33), True)
    speech_only = a.buffered_ms
    out = None
    for _ in range(8):
        out = a.add(block(33), False) or out
    assert out is not None and ms_of(out) > speech_only


def test_silence_before_any_speech_produces_nothing():
    a = UtteranceAssembler(sample_rate=RATE)
    for _ in range(50):
        assert a.add(block(33), False) is None
    assert a.buffered_ms == 0


def test_a_room_that_never_falls_silent_still_produces_utterances():
    # Music or a crowd: the gate never closes, so only the length ceiling
    # keeps transcripts flowing instead of one buffer growing forever.
    a = UtteranceAssembler(sample_rate=RATE, max_utterance_s=1.0, min_utterance_ms=100)
    released = []
    for _ in range(90):                       # ~3s, never silent
        out = a.add(block(33), True)
        if out:
            released.append(out)
    assert len(released) >= 2
    for utt in released:
        assert ms_of(utt) <= 1100


def test_a_short_burst_is_dropped_rather_than_transcribed():
    # A cough gated as speech would otherwise become a hallucinated word, and
    # one of those in the ring buffer is worse than a moment of silence.
    a = UtteranceAssembler(sample_rate=RATE, hangover_ms=100, min_utterance_ms=400)
    a.add(block(60), True)
    out = None
    for _ in range(5):
        out = a.add(block(33), False) or out
    assert out is None


def test_flush_releases_what_is_held():
    a = UtteranceAssembler(sample_rate=RATE, min_utterance_ms=100)
    for _ in range(20):
        a.add(block(33), True)
    out = a.flush()
    assert out is not None and ms_of(out) > 600
    assert a.flush() is None, "nothing is held after a flush"


def test_flush_of_a_short_burst_still_drops_it():
    a = UtteranceAssembler(sample_rate=RATE, min_utterance_ms=400)
    a.add(block(50), True)
    assert a.flush() is None


def test_state_resets_between_utterances():
    a = UtteranceAssembler(sample_rate=RATE, hangover_ms=200, min_utterance_ms=100)
    for _ in range(20):
        a.add(block(33), True)
    for _ in range(8):
        a.add(block(33), False)
    assert a.buffered_ms == 0
    for _ in range(20):
        assert a.add(block(33), True) is None, "a new utterance starts clean"


def test_empty_block_is_ignored():
    a = UtteranceAssembler(sample_rate=RATE)
    assert a.add(b"", True) is None
