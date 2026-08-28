"""Assemble audio blocks into utterances worth transcribing.

An audio source hands us tens of milliseconds at a time — small enough that
the VAD can react quickly and the feature bus stays at ~30 Hz. Handing those
blocks to a speech recogniser one at a time does not work: 33 ms is roughly
half a syllable, so the recogniser sees a fragment of a fragment and returns a
word or nothing, and the ring buffer fills with disconnected tokens that no
theme can be found in.

This accumulates gated speech until the room pauses, then releases the whole
utterance at once. A recogniser given a sentence returns a sentence.

Two bounds keep it honest:

* ``hangover_ms`` — how much silence ends an utterance. Too short and a
  sentence is chopped at its commas, which is what a room with music in it
  does to a gate that reacts quickly; too long and imagery lags the room.
* ``max_utterance_s`` — a hard ceiling, so a room that never falls silent
  (music, a crowd) still produces transcripts at a steady rate rather than
  growing one buffer forever.

Very short bursts are dropped rather than transcribed: a cough or a chair
scrape gated as speech produces a hallucinated word, and a hallucinated word
in the ring buffer is worse than a moment of silence.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__all__ = ["UtteranceAssembler"]

DEFAULT_HANGOVER_MS = 900.0
DEFAULT_MAX_UTTERANCE_S = 12.0
DEFAULT_MIN_UTTERANCE_MS = 500.0


class UtteranceAssembler:
    """Turn a stream of (block, is_speech) into whole utterances.

    Bytes in and bytes out are 16-bit mono PCM at ``sample_rate``. The
    assembler holds no text and never sees any.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        hangover_ms: float = DEFAULT_HANGOVER_MS,
        max_utterance_s: float = DEFAULT_MAX_UTTERANCE_S,
        min_utterance_ms: float = DEFAULT_MIN_UTTERANCE_MS,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.hangover_ms = float(hangover_ms)
        self.max_utterance_s = float(max_utterance_s)
        self.min_utterance_ms = float(min_utterance_ms)
        self._buf = bytearray()
        self._silence_ms = 0.0

    @property
    def buffered_ms(self) -> float:
        # 2 bytes per sample, mono.
        return (len(self._buf) / 2) / self.sample_rate * 1000.0

    def _block_ms(self, pcm: bytes) -> float:
        return (len(pcm) / 2) / self.sample_rate * 1000.0

    def add(self, pcm: bytes, is_speech: bool) -> bytes | None:
        """Feed one block. Returns a finished utterance, or ``None``.

        Blocks are kept once speech has started, silence included, so a
        transcriber sees natural pauses inside a sentence rather than a
        stitched-together version with the gaps removed.
        """
        if not pcm:
            return None

        if is_speech:
            self._buf.extend(pcm)
            self._silence_ms = 0.0
        elif self._buf:
            # Trailing silence is kept: it is what tells the recogniser the
            # sentence ended, and trimming it clips final consonants.
            self._buf.extend(pcm)
            self._silence_ms += self._block_ms(pcm)
        else:
            return None  # silence before any speech is simply silence

        if self.buffered_ms >= self.max_utterance_s * 1000.0:
            return self._take(reason="length")
        if self._silence_ms >= self.hangover_ms:
            return self._take(reason="pause")
        return None

    def flush(self) -> bytes | None:
        """Release whatever is buffered, e.g. when a source is shutting down."""
        return self._take(reason="flush") if self._buf else None

    def _take(self, *, reason: str) -> bytes | None:
        utterance = bytes(self._buf)
        held_ms = self.buffered_ms
        self._buf.clear()
        self._silence_ms = 0.0
        if held_ms < self.min_utterance_ms:
            # A cough or a chair scrape. Asking a recogniser about it invites
            # a hallucinated word, and one of those in the ring buffer is
            # worse than a moment of silence.
            log.debug("dropping %.0fms burst (%s)", held_ms, reason)
            return None
        log.debug("utterance of %.0fms released (%s)", held_ms, reason)
        return utterance
