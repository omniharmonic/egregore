"""SCRIBE — local speech recognition and the ring buffer (Architecture §2.2–2.3).

Public API:

* :class:`RingBuffer` — the privacy primitive: a per-zone, in-memory, rolling
  window of transcript text with independent time and byte caps, timer-driven
  eviction, non-destructive reads and no serialization path.
* Transcriber engines implementing ``egregore.types.Transcriber``:
  :class:`FixtureTranscriber` (demo mode), :class:`ParakeetTranscriber`,
  :class:`FasterWhisperTranscriber`, built by :func:`make_transcriber`.
* :func:`install_privacy_excepthook` / :func:`privacy_asyncio_handler` — keep
  transcript text out of tracebacks and asyncio error logs.
"""

from egregore.scribe.engines import (
    FasterWhisperTranscriber,
    FixtureTranscriber,
    ParakeetTranscriber,
    make_transcriber,
)
from egregore.scribe.excepthook import (
    REDACTED,
    format_exception_redacted,
    install_privacy_excepthook,
    privacy_asyncio_handler,
    scrub_value,
)
from egregore.scribe.ring import RingBuffer, Segment

__all__ = [
    "Segment",
    "RingBuffer",
    "FixtureTranscriber",
    "ParakeetTranscriber",
    "FasterWhisperTranscriber",
    "make_transcriber",
    "install_privacy_excepthook",
    "privacy_asyncio_handler",
    "format_exception_redacted",
    "scrub_value",
    "REDACTED",
]
