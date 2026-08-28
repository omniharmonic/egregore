"""Scribe tests — the ring buffer's privacy guarantees, engines, excepthook.

The ring buffer tests drive time with an injected clock so they are fast and
deterministic; the one test that must prove the *timer* works (a quiet zone's
buffer emptying with nobody reading or writing) still uses a real short sleep.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import pickle
import sys

import pytest

from egregore.config.schema import PrivacyConfig
from egregore.scribe import (
    REDACTED,
    FixtureTranscriber,
    RingBuffer,
    format_exception_redacted,
    install_privacy_excepthook,
    make_transcriber,
    privacy_asyncio_handler,
    scrub_value,
)
from egregore.types import TextFragment, Transcriber

# Every token is an invented proper noun, so a leak is unambiguous: none of
# these can appear in a traceback, a log line or a repr by coincidence.
SENTINEL = "Quillanthorpe Vestibrance Marrowlight Ondrasec Threnhollow Bekvarrin Calyxine"


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_ring(**kw) -> tuple[RingBuffer, FakeClock]:
    clock = FakeClock()
    kw.setdefault("window_s", 60.0)
    kw.setdefault("max_bytes", 8192)
    ring = RingBuffer(zone="test", clock=clock, **kw)
    return ring, clock


# ---------------------------------------------------------------------------
# Time cap — the guarantee
# ---------------------------------------------------------------------------


def test_time_cap_evicts_on_schedule():
    ring, clock = make_ring(window_s=60.0)
    ring.add("first fragment")
    clock.advance(30)
    ring.add("second fragment")

    assert "first" in ring.snapshot() and "second" in ring.snapshot()

    clock.advance(31)  # first is now 61 s old, second 31 s
    snap = ring.snapshot()
    assert "first" not in snap
    assert "second" in snap

    clock.advance(30)  # second is now 61 s old
    assert ring.snapshot() == ""
    assert ring.occupancy() == (0, 0)


def test_time_cap_boundary_is_exclusive_of_the_window_edge():
    ring, clock = make_ring(window_s=10.0)
    ring.add("edge")
    clock.advance(10.0)
    # Exactly at the window edge the fragment is gone: the signage promise is
    # "destroyed within N minutes", so ties resolve toward destruction.
    assert ring.snapshot() == ""


def test_eviction_happens_without_any_read_or_write():
    """Lazy eviction is a backstop; the class must be correct without the timer."""
    ring, clock = make_ring(window_s=5.0)
    ring.add("quiet zone content")
    clock.advance(6)
    assert ring.evict() == 1
    assert len(ring) == 0


async def test_timer_evicts_a_quiet_buffer():
    """A zone that has gone silent still empties on schedule, untouched."""
    ring, clock = make_ring(window_s=5.0, eviction_interval_s=0.01)
    await ring.start()
    try:
        ring.add("nobody will read this")
        assert ring._bytes > 0  # noqa: SLF001 - white-box: no read allowed here
        clock.advance(6)
        # No read, no write: only the timer can empty this.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not ring._fragments:  # noqa: SLF001
                break
        assert not ring._fragments  # noqa: SLF001
        assert ring._bytes == 0  # noqa: SLF001
    finally:
        await ring.close()


async def test_start_is_idempotent_and_close_stops_the_timer():
    ring, _clock = make_ring(eviction_interval_s=0.01)
    await ring.start()
    task = ring._task  # noqa: SLF001
    await ring.start()
    assert ring._task is task  # noqa: SLF001
    await ring.close()
    assert task.done()
    with pytest.raises(RuntimeError):
        ring.add("after close")


async def test_async_context_manager():
    ring, clock = make_ring(window_s=5.0, eviction_interval_s=0.01)
    async with ring:
        ring.add("hello there")
        assert len(ring) == 1
    assert len(ring._fragments) == 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# Byte cap — may only shorten retention
# ---------------------------------------------------------------------------


def test_byte_cap_binds_before_time_cap():
    ring, clock = make_ring(window_s=3600.0, max_bytes=32)
    ring.add("aaaaaaaaaa")  # 10 bytes
    clock.advance(1)
    ring.add("bbbbbbbbbb")  # 20
    clock.advance(1)
    ring.add("cccccccccc")  # 30
    assert ring.occupancy() == (3, 30)

    clock.advance(1)
    ring.add("dddddddddd")  # would be 40 > 32, so the oldest goes
    n, b = ring.occupancy()
    assert b <= 32
    snap = ring.snapshot()
    assert "aaaaaaaaaa" not in snap
    assert "dddddddddd" in snap
    assert n == 3
    # ...and nothing aged out: the byte cap alone did this.
    assert "bbbbbbbbbb" in snap


def test_byte_cap_counts_utf8_not_characters():
    ring, _clock = make_ring(max_bytes=16)
    ring.add("é" * 8)  # 16 UTF-8 bytes, 8 characters
    assert ring.occupancy() == (1, 16)
    ring.add("x")
    n, b = ring.occupancy()
    assert b <= 16 and n == 1


def test_oversized_single_fragment_is_truncated_to_the_cap():
    ring, _clock = make_ring(max_bytes=20)
    ring.add("z" * 100)
    n, b = ring.occupancy()
    assert (n, b) == (1, 20)
    assert ring.snapshot() == "z" * 20


def test_from_config_uses_privacy_section():
    privacy = PrivacyConfig(ring_buffer_minutes=2.0, ring_buffer_max_bytes=64)
    clock = FakeClock()
    ring = RingBuffer.from_config("main", privacy, clock=clock)
    assert ring.window_s == 120.0
    assert ring.max_bytes == 64
    ring.add("still here")
    clock.advance(119)
    assert ring.snapshot() != ""
    clock.advance(2)
    assert ring.snapshot() == ""


def test_rejects_nonsense_limits():
    with pytest.raises(ValueError):
        RingBuffer(window_s=0)
    with pytest.raises(ValueError):
        RingBuffer(max_bytes=0)


# ---------------------------------------------------------------------------
# Reads, occupancy, zeroing
# ---------------------------------------------------------------------------


def test_snapshot_is_non_destructive_and_space_joined():
    ring, _clock = make_ring()
    ring.add("one")
    ring.add("two")
    assert ring.snapshot() == "one two"
    assert ring.snapshot() == "one two"  # weaver stage 1 AND the validator read it
    assert ring.occupancy() == (2, 6)


def test_blank_fragments_are_dropped():
    ring, _clock = make_ring()
    ring.add("")
    ring.add("   \n ")
    assert ring.occupancy() == (0, 0)
    assert not ring


def test_add_fragment_preserves_timestamp():
    ring, clock = make_ring(window_s=10.0)
    ring.add_fragment(TextFragment(text="old news", t=clock.t - 9))
    assert len(ring) == 1
    clock.advance(2)  # fragment is now 11 s old
    assert ring.snapshot() == ""


def test_occupancy_and_token_count():
    ring, _clock = make_ring()
    ring.add("the room is listening")  # 4 tokens, 21 bytes
    ring.add("and then it forgets")  # 4 tokens, 19 bytes
    assert ring.token_count() == 8
    assert ring.occupancy() == (2, 40)
    assert len(ring) == 2


def test_token_count_follows_eviction():
    ring, clock = make_ring(window_s=10.0)
    ring.add("four tokens right here")
    assert ring.token_count() == 4
    clock.advance(11)
    assert ring.token_count() == 0


def test_zero_empties_immediately_and_drops_references():
    ring, _clock = make_ring()
    ring.add(SENTINEL)
    held = ring._fragments[0]  # noqa: SLF001 - white-box: prove the text is overwritten
    assert len(ring) == 1

    ring.zero()

    assert ring.occupancy() == (0, 0)
    assert ring.snapshot() == ""
    assert ring.token_count() == 0
    assert held.text == ""  # the stored fragment's text reference was overwritten
    assert not ring


def test_evicted_fragments_are_overwritten_too():
    ring, clock = make_ring(window_s=5.0)
    ring.add(SENTINEL)
    held = ring._fragments[0]  # noqa: SLF001
    clock.advance(6)
    ring.evict()
    assert held.text == ""


def test_buffer_is_reusable_after_zero():
    ring, _clock = make_ring()
    ring.add("before")
    ring.zero()
    ring.add("after")
    assert ring.snapshot() == "after"


# ---------------------------------------------------------------------------
# No serialization path, no content in repr
# ---------------------------------------------------------------------------


def test_repr_and_str_contain_no_content():
    ring, _clock = make_ring()
    ring.add(SENTINEL)
    for text in (repr(ring), str(ring), f"{ring}", format(ring)):
        for word in SENTINEL.split():
            assert word not in text
        assert "fragments=1" in text
        assert "bytes=" in text


def test_pickling_raises():
    ring, _clock = make_ring()
    ring.add(SENTINEL)
    with pytest.raises(TypeError):
        pickle.dumps(ring)
    with pytest.raises(TypeError):
        copy.copy(ring)
    with pytest.raises(TypeError):
        copy.deepcopy(ring)


def test_no_serialization_helpers_exist():
    ring, _clock = make_ring()
    for attr in ("to_dict", "to_json", "as_wire", "save", "dump", "write"):
        assert not hasattr(ring, attr), f"RingBuffer must not expose {attr}"
    # object.__getstate__ exists on 3.11+, but the class must not define one of
    # its own — the only pickle entry point is __reduce__, and it raises.
    assert "__getstate__" not in RingBuffer.__dict__
    with pytest.raises(TypeError):
        json.dumps(ring)


def test_fragment_repr_is_redacted():
    frag = TextFragment(text=SENTINEL, t=1.0)
    assert SENTINEL not in repr(frag)
    assert SENTINEL not in str(frag)


def test_logging_emits_no_content(caplog):
    ring, clock = make_ring(window_s=5.0)
    with caplog.at_level(logging.DEBUG, logger="egregore.scribe.ring"):
        ring.add(SENTINEL)
        ring.snapshot()
        clock.advance(6)
        ring.evict()
        ring.zero()
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob  # we did log *something*
    for word in SENTINEL.split():
        assert word not in blob


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


async def test_fixture_transcriber_decodes_utf8():
    engine = FixtureTranscriber()
    assert isinstance(engine, Transcriber)
    assert await engine.transcribe(b"hello room", 16000) == "hello room"
    assert await engine.transcribe(b"  spaced  ", 16000) == "spaced"
    assert await engine.transcribe(b"", 16000) is None
    assert await engine.transcribe(b"   ", 16000) is None


async def test_fixture_transcriber_ignores_sample_rate_and_survives_bad_bytes():
    engine = FixtureTranscriber()
    assert await engine.transcribe("café".encode(), 48000) == "café"
    assert await engine.transcribe(b"\xff\xfe", 16000) is not None  # replaced, not raised


def test_factory_returns_fixture_engine():
    engine = make_transcriber("fixture", "en")
    assert isinstance(engine, FixtureTranscriber)
    assert isinstance(engine, Transcriber)


def test_factory_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unknown asr engine"):
        make_transcriber("wav2vec", "en")


def test_factory_parakeet_raises_helpfully_without_nemo():
    if "nemo" in sys.modules or _importable("nemo"):  # pragma: no cover - dev box with nemo
        pytest.skip("nemo is installed; the missing-dependency path cannot be exercised")
    with pytest.raises(RuntimeError) as exc:
        make_transcriber("parakeet", "en")
    msg = str(exc.value)
    assert "nemo_toolkit" in msg
    assert "fixture" in msg


def test_factory_faster_whisper_raises_helpfully_when_absent():
    if _importable("faster_whisper"):  # pragma: no cover - optional extra installed
        pytest.skip("faster-whisper is installed; missing-dependency path not exercisable")
    with pytest.raises(RuntimeError) as exc:
        make_transcriber("faster-whisper", "en")
    assert "faster-whisper" in str(exc.value)


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Privacy excepthook
# ---------------------------------------------------------------------------


def _boom_with_buffer_locals() -> None:
    ring = RingBuffer(zone="test", clock=FakeClock())
    ring.add(SENTINEL)
    transcript = f"{SENTINEL} {SENTINEL}"  # noqa: F841 - a long str local, redacted on length
    fragment = TextFragment(text=SENTINEL, t=0.0)
    fragments = [fragment]  # noqa: F841 - a container of fragments
    short = "ok"  # noqa: F841 - harmless, must survive
    count = 3  # noqa: F841 - harmless, must survive
    raise ValueError("synthesis failed")


def test_scrub_value_redacts_sensitive_things():
    ring, _clock = make_ring()
    assert scrub_value(ring) == REDACTED
    assert scrub_value(TextFragment(text="x", t=0.0)) == REDACTED
    assert scrub_value("y" * 65) == REDACTED
    assert scrub_value("short") == repr("short")
    assert scrub_value(7) == "7"
    assert "redacted" in scrub_value([TextFragment(text="x", t=0.0)])
    assert "redacted" in scrub_value({"a": "y" * 200})


def test_formatted_traceback_leaks_no_content():
    try:
        _boom_with_buffer_locals()
    except ValueError:
        text = format_exception_redacted(*sys.exc_info())

    assert "ValueError: synthesis failed" in text
    assert "_boom_with_buffer_locals" in text
    assert REDACTED in text
    for word in SENTINEL.split():
        assert word not in text
    # Harmless locals still survive, so the traceback stays useful.
    assert "count = 3" in text
    assert "'ok'" in text


def test_formatted_traceback_scrubs_long_exception_messages():
    try:
        raise RuntimeError(f"prompt rejected: {SENTINEL} {SENTINEL}")
    except RuntimeError:
        text = format_exception_redacted(*sys.exc_info())
    for word in SENTINEL.split():
        assert word not in text
    assert REDACTED in text


def test_formatted_traceback_follows_causes():
    try:
        try:
            raise ValueError("inner cause")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError:
        text = format_exception_redacted(*sys.exc_info())
    assert "inner cause" in text
    assert "RuntimeError: outer" in text


def test_format_is_robust_against_hostile_reprs():
    class Nasty:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    assert scrub_value(Nasty()) == "<unreprable>"
    assert format_exception_redacted(None, None, None) == "<no exception>\n"


def test_install_privacy_excepthook_writes_scrubbed_output(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)

    install_privacy_excepthook()
    assert sys.excepthook is not sys.__excepthook__

    try:
        _boom_with_buffer_locals()
    except ValueError:
        sys.excepthook(*sys.exc_info())

    out = buf.getvalue()
    assert "ValueError: synthesis failed" in out
    for word in SENTINEL.split():
        assert word not in out


def test_privacy_asyncio_handler_logs_without_content(caplog):
    try:
        _boom_with_buffer_locals()
    except ValueError as exc:
        with caplog.at_level(logging.ERROR, logger="egregore.scribe.excepthook"):
            privacy_asyncio_handler(None, {"message": "task failed", "exception": exc})
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "task failed" in blob
    for word in SENTINEL.split():
        assert word not in blob


def test_privacy_asyncio_handler_never_raises():
    privacy_asyncio_handler(None, {})
    privacy_asyncio_handler(None, {"exception": "not an exception"})
