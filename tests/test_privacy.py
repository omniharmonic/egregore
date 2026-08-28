"""The test that must never fail.

Implementation Plan §3: feed a fixture transcript containing rare sentinel
tokens through the full Weaver path, then assert that no artifact written
during the run — files, logs, the clip store, the manifest, the outbound
prompt — contains any word-level 3-gram from the fixture, any 12-character
contiguous run from it, or any sentinel token. Then assert the ring buffer
empties on schedule and that a forced exception mid-synthesis leaks nothing
into the traceback.

The overlap checks reuse the validator's own normalization helpers, so the
test and the runtime gate cannot drift apart.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from decimal import Decimal

import pytest

from egregore.app import ZonePipeline
from egregore.conductor import ConductorState
from egregore.config.schema import EgregoreConfig
from egregore.forge import ClipStore, Forge, MockBackend
from egregore.governor import Governor
from egregore.scribe import RingBuffer
from egregore.scribe.excepthook import format_exception_redacted
from egregore.weaver.validator import char_runs, normalize_chars, normalize_words, word_ngrams

# Sentinels: invented proper nouns, a distinctive 8-word phrase, a fake
# phone number. None of these may survive into any artifact.
SENTINELS = [
    "Zorblatt",
    "Quindlemere",
    "Vexatrine",
    "the copper heron flew backwards through my window",
    "555-0173-8842",
    "quindlemere.zorblatt@example-sentinel.net",
]

FIXTURE_SCRIPT = """\
00:01\tSo Zorblatt finally called me back about the apartment in Quindlemere.
00:03\tNo way. What did he say about the deposit money situation?
00:05\tHe said the copper heron flew backwards through my window, I swear.
00:07\tThat is the strangest excuse for water damage I have ever heard anyone give.
00:09\tCall the office directly, the number is 555-0173-8842, ask for Vexatrine.
00:11\tShe handles all the Quindlemere leases and she actually answers her phone.
00:13\tMy grandmother used to describe the ocean like it was a person she missed.
00:15\tVast and blue and patient, she said, holding every ship that ever sank.
00:17\tI think about that every time we drive along the coast highway at night.
00:19\tEmail quindlemere.zorblatt@example-sentinel.net if the phone does not work.
"""


@dataclasses.dataclass
class RunArtifacts:
    prompts: list[str]
    log_text: str
    manifest_text: str
    status_text: str
    clip_paths: list


async def _run_fixture_party(tmp_path) -> RunArtifacts:
    """Drive the real pipeline (source -> ring -> weaver -> forge -> loom ->
    conductor state) exactly as app.run_party wires it, minus the HTTP server."""
    script = tmp_path / "script.txt"
    script.write_text(FIXTURE_SCRIPT)
    clip_dir = tmp_path / "clips"

    cfg = EgregoreConfig.model_validate(
        {
            "party": {"name": "privacy-test", "duration_hours": 0.5},
            "generation": {"backend": "mock", "clip_duration_s": 2},
            "budget": {"total_usd": 0},
            "zones": [
                {"id": "main", "mic": {"type": "fixture", "fixture_path": str(script)}}
            ],
            "clip_store_dir": str(clip_dir),
            "demo_time_scale": 30,
        }
    )

    # Capture every log record emitted during the run, at DEBUG and up.
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    store = ClipStore(clip_dir)
    backend = MockBackend(store, name="procedural")

    # Record the only strings that ever reach a generation backend.
    prompts: list[str] = []
    real_generate = backend.generate

    async def spy_generate(prompt: str, *a, **kw):
        prompts.append(prompt)
        return await real_generate(prompt, *a, **kw)

    backend.generate = spy_generate  # type: ignore[method-assign]

    governor = Governor.from_config(
        cfg, cost_per_clip=Decimal("0"), min_interval_s=45.0
    )
    pipelines: dict[str, ZonePipeline] = {}

    async def on_clip(clip):
        pipe = pipelines.get(clip.zone)
        if pipe is not None:
            await pipe.on_clip(clip)

    forge = Forge([backend], store, on_clip=on_clip)
    state = ConductorState(clip_resolver=store.path_for)
    pipe = ZonePipeline(cfg.zones[0], cfg, forge=forge, governor=governor, state=state)
    pipelines["main"] = pipe

    governor.start()
    forge.start()
    await pipe.run()
    try:
        deadline = time.monotonic() + 60
        while len(store.all()) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
    finally:
        await pipe.close()
        await forge.close()
        root.removeHandler(handler)
        root.setLevel(old_level)

    assert len(store.all()) >= 2, "pipeline produced no clips; nothing was tested"
    assert pipe.weaver.prompts_synthesized >= 1

    manifest = state.get_manifest("main")
    manifest_text = json.dumps(dataclasses.asdict(manifest), default=str) if manifest else ""
    status_text = json.dumps(pipe.status(), default=str)
    return RunArtifacts(
        prompts=prompts,
        log_text="\n".join(records),
        manifest_text=manifest_text,
        status_text=status_text,
        clip_paths=[c.path for c in store.all()],
    )


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory) -> RunArtifacts:
    return asyncio.run(_run_fixture_party(tmp_path_factory.mktemp("privacy")))


def _assert_clean(artifact_text: str, label: str) -> None:
    fixture_words = normalize_words(FIXTURE_SCRIPT)
    fixture_grams = word_ngrams(fixture_words)
    fixture_runs = char_runs(normalize_chars(FIXTURE_SCRIPT))

    for sentinel in SENTINELS:
        assert sentinel.lower() not in artifact_text.lower(), (
            f"sentinel leaked into {label}"
        )
    grams = word_ngrams(normalize_words(artifact_text))
    shared = grams & fixture_grams
    assert not shared, f"word 3-gram from transcript leaked into {label}"
    runs = char_runs(normalize_chars(artifact_text))
    shared_runs = runs & fixture_runs
    assert not shared_runs, f"12-char run from transcript leaked into {label}"


def test_outbound_prompts_are_clean(artifacts: RunArtifacts) -> None:
    """The outbound prompt is the only thing that can ever leave (§2.4)."""
    assert artifacts.prompts
    for prompt in artifacts.prompts:
        _assert_clean(prompt, "outbound prompt")


def test_logs_are_clean(artifacts: RunArtifacts) -> None:
    _assert_clean(artifacts.log_text, "log output")


def test_manifest_and_status_are_clean(artifacts: RunArtifacts) -> None:
    _assert_clean(artifacts.manifest_text, "manifest")
    _assert_clean(artifacts.status_text, "status")


def test_no_transcript_bytes_in_any_written_file(artifacts: RunArtifacts, tmp_path_factory) -> None:
    """Sweep every file the run wrote (the clip store) for sentinel bytes."""
    assert artifacts.clip_paths
    for path in artifacts.clip_paths:
        blob = path.read_bytes()
        for sentinel in SENTINELS:
            assert sentinel.encode() not in blob, f"sentinel bytes in clip {path.name}"
            assert sentinel.lower().encode() not in blob


def test_ring_buffer_empties_on_schedule() -> None:
    """Text older than the window is destroyed, not archived (§2.3)."""
    now = [0.0]
    ring = RingBuffer("z", window_s=3.0, max_bytes=8192, clock=lambda: now[0])
    ring.add("Zorblatt spoke of Quindlemere at length tonight")
    assert ring.token_count() > 0
    now[0] = 3.1
    ring.evict()
    assert ring.snapshot() == ""
    assert ring.occupancy() == (0, 0)


def test_forced_exception_leaks_nothing() -> None:
    """A crash mid-synthesis must not carry buffer content in its traceback."""
    window_text = FIXTURE_SCRIPT.replace("\t", " ")

    def synthesize_and_fail() -> None:
        local_copy = window_text  # noqa: F841 — the leak vector under test
        fragments = [window_text[i : i + 40] for i in range(0, 200, 40)]  # noqa: F841
        raise RuntimeError("synthesis failed at stage " + str(len(window_text)))

    try:
        synthesize_and_fail()
    except RuntimeError as e:
        formatted = format_exception_redacted(type(e), e, e.__traceback__)
    _assert_clean(formatted, "formatted traceback")


def test_shutdown_zeroes_buffer() -> None:
    ring = RingBuffer("z", window_s=300.0, max_bytes=8192)
    ring.add("Vexatrine answered on the second ring as promised")
    ring.zero()
    assert ring.snapshot() == ""
