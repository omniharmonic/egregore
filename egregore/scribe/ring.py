"""The ring buffer — Egregore's privacy primitive (Architecture §2.3).

A fixed-size, in-memory, per-zone circular buffer of recent transcript text.
Everything else in this repository is an aesthetic decision; this file is an
ethical one, and it is written to be read carefully.

Hard guarantees implemented here:

* **Memory only.** There is no serialization path: no ``__getstate__``, no
  ``to_dict``, no file I/O. ``__reduce__`` raises so pickling fails loudly
  rather than quietly writing transcript text to a socket or a disk.
* **Two independent caps.** A time window and a byte cap, whichever binds
  first. The *time* cap is the guarantee the signage makes; the byte cap may
  only ever make retention shorter, never longer.
* **Eviction runs on a timer**, not only on write, so a zone that has gone
  quiet still empties on schedule. Eviction also runs lazily on every read and
  write as a backstop, so the class is correct even with no timer running.
* **Not cleared on read.** ``snapshot()`` is non-destructive: the weaver's
  stage 1 and the validator both read the same rolling window.
* **Zeroable on demand** — mute switch, shutdown, validator-triggered purge.
* **Never logged.** Log statements in this module emit occupancy and token
  counts only. ``__repr__``/``__str__`` emit counts, never content.

Clock choice: eviction is driven by ``time.monotonic`` by default rather than
wall time. A wall clock that steps backwards (NTP correction, DST-naive host)
would silently *extend* retention past the promised window; a monotonic clock
cannot. Tests inject their own clock to drive time by hand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from egregore.config.schema import PrivacyConfig
from egregore.types import TextFragment

logger = logging.getLogger(__name__)

__all__ = ["RingBuffer", "Segment"]


@dataclass(frozen=True)
class Segment:
    """One stretch of speech between pauses. Text lives here and in the
    weaver's stage 1 — nowhere else. ``repr`` is counts only."""

    text: str
    started_at: float
    ended_at: float
    tokens: int

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Segment(<{self.tokens} tokens redacted>, "
            f"{self.started_at:.1f}..{self.ended_at:.1f})"
        )

    __str__ = __repr__


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _tail_bytes(text: str, max_bytes: int) -> str:
    """Return the longest suffix of ``text`` that fits in ``max_bytes`` UTF-8 bytes.

    Used only for the pathological case of a single fragment larger than the
    whole buffer: the cap is honoured absolutely, so the oldest part of the
    fragment is what gets dropped.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    cut = raw[-max_bytes:]
    # Realign to a UTF-8 code point boundary (continuation bytes are 0b10xxxxxx).
    start = 0
    while start < len(cut) and (cut[start] & 0xC0) == 0x80:
        start += 1
    return cut[start:].decode("utf-8", errors="ignore")


class RingBuffer:
    """Per-zone rolling window of recent transcript text.

    Args:
        zone: zone id, used for logging only.
        window_s: retention window in seconds (the guarantee).
        max_bytes: byte cap on retained text, measured as UTF-8 length.
        clock: monotonic-ish time source, injectable for tests.
        eviction_interval_s: timer period for background age eviction. Defaults
            to a small fraction of the window, clamped to [0.05, 1.0] seconds.

    The buffer is not thread-safe; it is owned by one zone's asyncio task set.
    """

    def __init__(
        self,
        zone: str = "default",
        window_s: float = 300.0,
        max_bytes: int = 8192,
        *,
        clock: Callable[[], float] = time.monotonic,
        eviction_interval_s: float | None = None,
    ) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.zone = zone
        self.window_s = float(window_s)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._interval = (
            float(eviction_interval_s)
            if eviction_interval_s is not None
            else max(0.05, min(1.0, self.window_s / 20.0))
        )
        self._fragments: deque[TextFragment] = deque()
        self._bytes = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # -- construction -----------------------------------------------------

    @classmethod
    def from_config(
        cls,
        zone: str,
        privacy: PrivacyConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        eviction_interval_s: float | None = None,
    ) -> RingBuffer:
        """Build a buffer from the party config's ``privacy`` section."""
        return cls(
            zone=zone,
            window_s=privacy.ring_buffer_minutes * 60.0,
            max_bytes=privacy.ring_buffer_max_bytes,
            clock=clock,
            eviction_interval_s=eviction_interval_s,
        )

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Start the background age-eviction timer. Idempotent."""
        if self._closed:
            raise RuntimeError("ring buffer is closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._evict_loop(), name=f"ring-evict:{self.zone}")

    async def close(self) -> None:
        """Stop the timer and zero the buffer. Idempotent."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.zero()

    async def __aenter__(self) -> RingBuffer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _evict_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                dropped = self._evict()
                if dropped:
                    n, b = self.occupancy()
                    logger.debug(
                        "ring[%s]: timer evicted %d fragment(s); occupancy %d frags / %d bytes",
                        self.zone,
                        dropped,
                        n,
                        b,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive; never kill the timer
                logger.exception("ring[%s]: eviction timer error", self.zone)

    # -- writing ----------------------------------------------------------

    def add(self, text: str, t: float | None = None) -> None:
        """Append a transcript fragment.

        ``t`` is in the buffer's clock domain (``self._clock``); omit it and
        the buffer stamps the fragment itself. Empty/whitespace fragments are
        dropped — the ASR emits them constantly on a quiet mic.
        """
        if self._closed:
            raise RuntimeError("ring buffer is closed")
        if not text or not text.strip():
            return
        now = self._clock() if t is None else float(t)
        # A single fragment larger than the whole buffer keeps only its tail.
        if _utf8_len(text) > self.max_bytes:
            text = _tail_bytes(text, self.max_bytes)
            if not text:
                return
        self._fragments.append(TextFragment(text=text, t=now))
        self._bytes += _utf8_len(text)
        self._evict(now)
        logger.debug(
            "ring[%s]: wrote fragment; occupancy %d frags / %d bytes",
            self.zone,
            len(self._fragments),
            self._bytes,
        )

    def add_fragment(self, fragment: TextFragment) -> None:
        """Append an already-stamped fragment (same clock domain as ``add``)."""
        self.add(fragment.text, fragment.t)

    # -- eviction ---------------------------------------------------------

    def _evict(self, now: float | None = None) -> int:
        """Apply both caps. Returns the number of fragments dropped."""
        if now is None:
            now = self._clock()
        dropped = 0
        cutoff = now - self.window_s
        # Time cap: the guarantee.
        while self._fragments and self._fragments[0].t <= cutoff:
            dropped += self._drop_oldest()
        # Byte cap: may only shorten retention further, never extend it.
        while self._fragments and self._bytes > self.max_bytes:
            dropped += self._drop_oldest()
        return dropped

    def _drop_oldest(self) -> int:
        frag = self._fragments.popleft()
        self._bytes -= _utf8_len(frag.text)
        _destroy(frag)
        return 1

    def evict(self) -> int:
        """Public, idempotent eviction pass. Safe to call at any time."""
        return self._evict()

    # -- reading ----------------------------------------------------------

    def snapshot(self) -> str:
        """Non-destructive read of the current window, space-joined.

        Read by weaver stage 1 and, separately, by the validator as reference
        text. Clearing here would collapse the effective window to the
        generation interval and destroy the validator's reference — so it
        does not clear.
        """
        self._evict()
        return " ".join(f.text for f in self._fragments)

    def segments(self, gap_s: float) -> list[Segment]:
        """The window split at pauses of at least ``gap_s`` seconds.

        Same boundary as ``snapshot()``: evicts first, never clears, and the
        text goes to weaver stage 1 and nowhere else. A pause is measured
        between consecutive fragment timestamps, so a room that talks
        without a break yields one segment and a back-and-forth yields
        several — which is what lets the selector weigh them.
        """
        self._evict()
        out: list[Segment] = []
        parts: list[str] = []
        start = end = 0.0
        for frag in self._fragments:
            if parts and frag.t - end >= gap_s:
                text = " ".join(parts)
                out.append(Segment(text, start, end, len(text.split())))
                parts = []
            if not parts:
                start = frag.t
            parts.append(frag.text)
            end = frag.t
        if parts:
            text = " ".join(parts)
            out.append(Segment(text, start, end, len(text.split())))
        return out

    def occupancy(self) -> tuple[int, int]:
        """(fragment count, UTF-8 byte count) — safe to log."""
        self._evict()
        return len(self._fragments), self._bytes

    def token_count(self) -> int:
        """Whitespace token count of the current window — safe to log, and the
        value that goes into ``ZoneStatus.buffer_occupancy_tokens``."""
        self._evict()
        return sum(len(f.text.split()) for f in self._fragments)

    def __len__(self) -> int:
        self._evict()
        return len(self._fragments)

    def __bool__(self) -> bool:
        return len(self) > 0

    # -- destruction ------------------------------------------------------

    def zero(self) -> None:
        """Destroy everything now: mute switch, shutdown, validator purge.

        CPython gives no way to scrub an interned ``str`` in place, so this is
        best effort by construction: every stored fragment's text reference is
        overwritten and the container cleared, dropping the last references in
        this process immediately rather than at some later collection.
        """
        for frag in self._fragments:
            _destroy(frag)
        self._fragments.clear()
        self._bytes = 0
        logger.info("ring[%s]: zeroed", self.zone)

    # -- no serialization path -------------------------------------------

    def __repr__(self) -> str:
        return (
            f"RingBuffer(zone={self.zone!r}, fragments={len(self._fragments)}, "
            f"bytes={self._bytes}/{self.max_bytes}, window_s={self.window_s:g})"
        )

    __str__ = __repr__

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        raise TypeError(
            "RingBuffer is not serializable: transcript text must never leave memory "
            "(Architecture §2.3)"
        )

    def __copy__(self) -> RingBuffer:
        raise TypeError("RingBuffer must not be copied")

    def __deepcopy__(self, memo: dict) -> RingBuffer:
        raise TypeError("RingBuffer must not be copied")


def _destroy(fragment: TextFragment) -> None:
    """Best-effort overwrite of a frozen fragment's text reference."""
    try:
        object.__setattr__(fragment, "text", "")
    except Exception:  # pragma: no cover - frozen dataclass always permits this
        pass
