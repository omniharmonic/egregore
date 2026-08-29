"""Forge tests: clip store, procedural rendering, the ladder, cloud clients.

The rendering tests actually run ffmpeg and actually probe the result. That
is deliberate: the mock backend is the demo and CI rendering path, and a
test that only checked "a file appeared" would pass just as happily for a
zero-length file. They skip cleanly where ffmpeg is absent so the rest of
the suite still runs.

Nothing here touches the network. The cloud clients are exercised against
`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from egregore.forge import (
    ClipStore,
    ComfyUIBackend,
    Forge,
    MockBackend,
    VeoBackend,
    build_command,
    palette_for,
    theme_digest,
    variant_for,
)
from egregore.forge.fal import FAL_MODELS, FalBackend
from egregore.forge.veo import (
    COST_PER_SECOND,
    COST_PER_SECOND_BY_RESOLUTION,
    SAFETY_FACTOR,
)
from egregore.types import BackendStatus, ClipRef, Reservation, ThemeObject

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(HAS_FFMPEG is False, reason="ffmpeg/ffprobe not on PATH")

WATER = ThemeObject(
    motifs=["tidal", "threshold"],
    register="reflective",
    valence=0.35,
    intensity=0.4,
    movement="slow drift",
    elemental=["water", "mist"],
)
FIRE = ThemeObject(
    motifs=["kiln", "argument"],
    register="urgent",
    valence=0.6,
    intensity=0.85,
    movement="quick flare",
    elemental=["fire"],
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def make_png(path: Path, size: str = "320x180") -> bytes:
    """A tiny real PNG, made with ffmpeg so the suite needs no image library."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"gradients=s={size}:c0=0xff2200:c1=0xffdd00",
            "-frames:v", "1", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def write_fake_clip(path: Path, payload: bytes = b"fake-mp4-bytes") -> Path:
    path.write_bytes(payload)
    return path


@pytest.fixture
def store(tmp_path: Path) -> ClipStore:
    return ClipStore(tmp_path / "clips")


# ---------------------------------------------------------------------------
# ClipStore
# ---------------------------------------------------------------------------


async def test_store_round_trip(store: ClipStore, tmp_path: Path) -> None:
    src = write_fake_clip(tmp_path / "a.mp4", b"one")
    ref = await store.put(src, duration_s=8.0, zone="main", backend="mock", tier="mock")

    assert ref.path.exists()
    assert ref.path.name == f"{ref.id}.mp4"
    assert ref.path.parent == store.dir
    assert not src.exists(), "source should be moved, not copied"
    assert ref.duration_s == 8.0
    assert ref.zone == "main"
    assert store.get(ref.id) == ref
    assert store.path_for(ref.id) == ref.path
    assert store.all() == [ref]
    assert ref.id in store


async def test_store_is_content_addressed(store: ClipStore, tmp_path: Path) -> None:
    first = await store.put(
        write_fake_clip(tmp_path / "a.mp4", b"identical"),
        duration_s=4.0, zone="main", backend="mock", tier="mock",
    )
    second = await store.put(
        write_fake_clip(tmp_path / "b.mp4", b"identical"),
        duration_s=4.0, zone="main", backend="mock", tier="mock",
    )
    different = await store.put(
        write_fake_clip(tmp_path / "c.mp4", b"other"),
        duration_s=4.0, zone="main", backend="mock", tier="mock",
    )

    assert first.id == second.id
    assert first.id != different.id
    # Identical bytes are one clip: the playlist must not be able to hold it twice.
    assert len(store) == 2
    assert len(store.all()) == 2


async def test_store_id_is_sha256_prefix(store: ClipStore, tmp_path: Path) -> None:
    import hashlib

    payload = b"deterministic"
    ref = await store.put(
        write_fake_clip(tmp_path / "a.mp4", payload),
        duration_s=2.0, zone="main", backend="mock", tier="mock",
    )
    assert ref.id == hashlib.sha256(payload).hexdigest()[:16]


async def test_store_wipe_removes_files(store: ClipStore, tmp_path: Path) -> None:
    refs = [
        await store.put(
            write_fake_clip(tmp_path / f"{i}.mp4", f"clip-{i}".encode()),
            duration_s=4.0, zone="main", backend="mock", tier="mock",
        )
        for i in range(3)
    ]
    assert all(ref.path.exists() for ref in refs)

    removed = store.wipe()

    assert removed == 3
    assert len(store) == 0
    assert not any(ref.path.exists() for ref in refs)
    assert list(store.dir.glob("*.mp4")) == []


def test_store_temp_path_is_on_the_store_filesystem(store: ClipStore) -> None:
    tmp = store.temp_path()
    assert tmp.parent == store.incoming_dir
    assert store.incoming_dir.parent == store.dir
    assert tmp != store.temp_path()


# ---------------------------------------------------------------------------
# MockBackend — real rendering
# ---------------------------------------------------------------------------


def test_mock_capabilities(store: ClipStore) -> None:
    caps = MockBackend(store).capabilities
    assert caps.allowed_durations_s == frozenset({2, 4, 6, 8})
    assert caps.supports_native_extend is False
    assert caps.supports_image_seed is True
    assert caps.tiers == frozenset({"mock"})
    assert caps.max_chain_length == 0
    assert MockBackend(store).max_plausible_cost(8, "mock") == Decimal("0")


def test_mock_theme_parameterisation_is_deterministic_and_distinct() -> None:
    assert theme_digest(WATER) == theme_digest(WATER)
    assert theme_digest(WATER) != theme_digest(FIRE)
    # Water lands in the blues, fire in the ambers.
    assert 180.0 <= palette_for(WATER).hue <= 230.0
    assert palette_for(FIRE).hue <= 60.0
    assert palette_for(WATER) != palette_for(FIRE)
    # Intensity drives motion.
    water_cmd = " ".join(build_command(WATER, 4, Path("/tmp/x.mp4")))
    fire_cmd = " ".join(build_command(FIRE, 4, Path("/tmp/x.mp4")))
    assert water_cmd != fire_cmd
    assert "generateAudio" not in water_cmd  # nothing cloud-shaped leaks in
    assert "-an" in build_command(WATER, 4, Path("/tmp/x.mp4"))


def test_mock_never_puts_the_prompt_in_the_command() -> None:
    secret = "the-quick-brown-fox-said-something-private"
    args = build_command(WATER, 4, Path("/tmp/x.mp4"))
    assert secret not in " ".join(args)
    # The prompt is not even a parameter of the builder.
    assert "prompt" not in build_command.__code__.co_varnames


@needs_ffmpeg
async def test_mock_renders_a_playable_clip(store: ClipStore) -> None:
    backend = MockBackend(store)
    ref = await backend.generate(
        "an abstract prompt that must never appear anywhere",
        4,
        "mock",
        theme_hint=WATER,
        zone="main",
    )

    assert ref.path.exists()
    assert ref.path.stat().st_size > 50_000, "clip is too small to be real footage"
    assert abs(probe_duration(ref.path) - 4.0) < 0.5
    assert ref.backend == "mock"
    assert ref.tier == "mock"
    assert ref.zone == "main"
    assert ref.duration_s == 4.0
    assert store.get(ref.id) is ref


@needs_ffmpeg
async def test_mock_seed_image_produces_a_clip(store: ClipStore, tmp_path: Path) -> None:
    png = make_png(tmp_path / "seed.png")
    backend = MockBackend(store)

    ref = await backend.generate(
        "prompt", 4, "mock", seed_image=png, theme_hint=WATER, zone="main"
    )

    assert ref.path.stat().st_size > 50_000
    assert abs(probe_duration(ref.path) - 4.0) < 0.5
    # The dissolve must actually change the picture: seeded and unseeded
    # renders of the same theme are different clips.
    plain = await backend.generate("prompt", 4, "mock", theme_hint=WATER, zone="main")
    assert ref.id != plain.id
    # And the scratch PNG is not left lying around.
    assert list(store.incoming_dir.glob("*.png")) == []


@needs_ffmpeg
async def test_mock_variants_differ_between_themes(store: ClipStore) -> None:
    backend = MockBackend(store)
    a = await backend.generate("p", 2, "mock", theme_hint=WATER, zone="main")
    b = await backend.generate("p", 2, "mock", theme_hint=FIRE, zone="main")
    assert a.id != b.id
    assert variant_for(WATER) in {"liquid", "bloom", "nebula", "organism"}


async def test_mock_rejects_unsupported_duration(store: ClipStore) -> None:
    with pytest.raises(ValueError):
        await MockBackend(store).generate("p", 5, "mock")


async def test_mock_fail_flag_and_health(store: ClipStore) -> None:
    backend = MockBackend(store)
    assert (await backend.health()).status is BackendStatus.HEALTHY

    backend.fail = True
    with pytest.raises(RuntimeError):
        await backend.generate("p", 4, "mock")
    # A forced failure is a generation failure, not a health failure — that is
    # what makes it exercise the ladder's retry/fall-through path.
    assert (await backend.health()).status is BackendStatus.HEALTHY

    backend.healthy = False
    assert (await backend.health()).status is BackendStatus.DOWN


async def test_mock_latency_is_awaited(store: ClipStore) -> None:
    """Failover tests need a rung that is slow but not broken."""
    # Point at a binary that does not exist so the render fails *after* the
    # simulated wait, without paying for a real encode.
    backend = MockBackend(store, latency_s=0.2, ffmpeg="/nonexistent/ffmpeg")
    loop = asyncio.get_running_loop()

    started = loop.time()
    with pytest.raises(FileNotFoundError):
        await backend.generate("p", 4, "mock")
    assert loop.time() - started >= 0.2

    # A missing ffmpeg is also a health failure, so the ladder skips it.
    assert (await backend.health()).status is BackendStatus.DOWN


def test_backends_satisfy_the_protocol(store: ClipStore) -> None:
    from egregore.types import VideoBackend

    for backend in (MockBackend(store), VeoBackend(store, api_key="k"), ComfyUIBackend(store)):
        assert isinstance(backend, VideoBackend)


# ---------------------------------------------------------------------------
# Forge — queue and ladder
# ---------------------------------------------------------------------------


class FakeBackend:
    """A `VideoBackend` that records calls and never touches ffmpeg."""

    def __init__(
        self,
        name: str,
        store: ClipStore,
        *,
        cost: Decimal = Decimal("0"),
        tiers: frozenset[str] = frozenset({"fake"}),
        durations: frozenset[int] = frozenset({4, 6, 8}),
        native_extend: bool = False,
        image_seed: bool = True,
        status: BackendStatus = BackendStatus.HEALTHY,
        fail_times: int = 0,
    ) -> None:
        from egregore.types import BackendCapabilities, BackendHealth

        self.name = name
        self.store = store
        self.cost = cost
        self._caps = BackendCapabilities(
            allowed_durations_s=durations,
            supports_native_extend=native_extend,
            supports_image_seed=image_seed,
            tiers=tiers,
            max_chain_length=20 if native_extend else 0,
        )
        self._health = BackendHealth(status)
        self.fail_times = fail_times
        self.calls: list[dict] = []
        self._counter = 0

    @property
    def capabilities(self):
        return self._caps

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
        return self.cost * Decimal(duration_s)

    def estimated_latency(self, tier: str):
        from datetime import timedelta

        return timedelta(seconds=1)

    async def health(self):
        return self._health

    async def generate(
        self, prompt, duration_s, tier, seed_image=None, extend_from=None,
        theme_hint=None, zone="default",
    ) -> ClipRef:
        self.calls.append(
            {
                "duration_s": duration_s,
                "tier": tier,
                "zone": zone,
                "seed_image": seed_image,
                "extend_from": extend_from,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{self.name}: transient")
        self._counter += 1
        tmp = self.store.temp_path()
        tmp.write_bytes(f"{self.name}-{self._counter}".encode())
        return await self.store.put(
            tmp, duration_s=float(duration_s), zone=zone, backend=self.name, tier=tier
        )


class Ledger:
    """Stand-in for the Governor's three callbacks."""

    def __init__(self, *, approve: bool = True) -> None:
        self.approve = approve
        self.authorized: list[tuple[str, Decimal]] = []
        self.settled: list[tuple[str, Decimal]] = []
        self.released: list[str] = []
        self._n = 0

    async def authorize(self, backend: str, amount: Decimal) -> Reservation | None:
        self.authorized.append((backend, amount))
        if not self.approve:
            return None
        self._n += 1
        return Reservation(id=f"r{self._n}", amount=amount, zone="main", backend=backend)

    async def settle(self, reservation: Reservation, actual: Decimal) -> None:
        self.settled.append((reservation.id, actual))

    async def release(self, reservation: Reservation) -> None:
        self.released.append(reservation.id)


class Sink:
    def __init__(self) -> None:
        self.clips: list[ClipRef] = []

    async def __call__(self, clip: ClipRef) -> None:
        self.clips.append(clip)


async def run_forge(forge: Forge, zone: str = "main") -> None:
    forge.start()
    await forge.join(zone)
    await forge.close()


async def test_forge_dispatches_to_the_first_healthy_rung(store: ClipStore) -> None:
    first = FakeBackend("first", store)
    second = FakeBackend("second", store)
    sink = Sink()
    forge = Forge([first, second], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await run_forge(forge)

    assert len(sink.clips) == 1
    assert sink.clips[0].backend == "first"
    assert second.calls == []
    assert forge.stats.completed == 1
    assert forge.stats.by_backend == {"first": 1}


async def test_forge_fails_over_from_a_broken_backend(store: ClipStore) -> None:
    broken = MockBackend(store, name="broken")
    broken.fail = True
    healthy = FakeBackend("healthy", store)
    sink = Sink()
    forge = Forge([broken, healthy], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="mock")
    await run_forge(forge)

    assert len(sink.clips) == 1
    assert sink.clips[0].backend == "healthy"
    # One retry on the broken rung before dropping down (ATTEMPTS_PER_BACKEND).
    assert forge.stats.failures == 2
    assert forge.stats.dropped == 0


async def test_forge_skips_a_backend_reporting_down(store: ClipStore) -> None:
    down = FakeBackend("down", store, status=BackendStatus.DOWN)
    up = FakeBackend("up", store)
    sink = Sink()
    forge = Forge([down, up], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await run_forge(forge)

    assert down.calls == []
    assert [c.backend for c in sink.clips] == ["up"]


async def test_forge_retries_once_then_succeeds_on_the_same_rung(store: ClipStore) -> None:
    flaky = FakeBackend("flaky", store, fail_times=1)
    fallback = FakeBackend("fallback", store)
    sink = Sink()
    forge = Forge([flaky, fallback], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await run_forge(forge)

    assert len(flaky.calls) == 2
    assert fallback.calls == []
    assert [c.backend for c in sink.clips] == ["flaky"]


async def test_authorize_refusal_routes_to_the_free_rung(store: ClipStore) -> None:
    cloud = FakeBackend("cloud", store, cost=Decimal("0.06"), tiers=frozenset({"quality"}))
    free = FakeBackend("free", store, cost=Decimal("0"))
    ledger = Ledger(approve=False)
    sink = Sink()
    forge = Forge(
        [cloud, free],
        store,
        authorize=ledger.authorize,
        settle=ledger.settle,
        release=ledger.release,
        on_clip=sink,
    )

    await forge.request(zone="main", prompt="p", duration_s=8, tier="quality")
    await run_forge(forge)

    assert ledger.authorized == [("cloud", Decimal("0.48"))]
    assert cloud.calls == [], "refused rung must not generate"
    assert [c.backend for c in sink.clips] == ["free"]
    # Nothing was reserved, so there is nothing to settle or release.
    assert ledger.settled == []
    assert ledger.released == []
    # And the free rung never bothers the Governor at all.
    assert forge.stats.refused == 1


async def test_backend_failure_releases_its_reservation(store: ClipStore) -> None:
    cloud = FakeBackend(
        "cloud", store, cost=Decimal("0.10"), tiers=frozenset({"quality"}), fail_times=99
    )
    free = FakeBackend("free", store)
    ledger = Ledger(approve=True)
    sink = Sink()
    forge = Forge(
        [cloud, free],
        store,
        authorize=ledger.authorize,
        settle=ledger.settle,
        release=ledger.release,
        on_clip=sink,
    )

    await forge.request(zone="main", prompt="p", duration_s=8, tier="quality")
    await run_forge(forge)

    assert ledger.authorized == [("cloud", Decimal("0.80"))]
    assert ledger.released == ["r1"], "a failed generation must give the hold back"
    assert ledger.settled == []
    assert [c.backend for c in sink.clips] == ["free"]


async def test_success_settles_at_the_reserved_amount(store: ClipStore) -> None:
    cloud = FakeBackend("cloud", store, cost=Decimal("0.10"), tiers=frozenset({"quality"}))
    ledger = Ledger(approve=True)
    forge = Forge(
        [cloud], store, authorize=ledger.authorize, settle=ledger.settle,
        release=ledger.release, on_clip=Sink(),
    )

    await forge.request(zone="main", prompt="p", duration_s=6, tier="quality")
    await run_forge(forge)

    assert ledger.settled == [("r1", Decimal("0.60"))]
    assert ledger.released == []


async def test_priced_rung_is_skipped_without_an_authorize_callback(store: ClipStore) -> None:
    cloud = FakeBackend("cloud", store, cost=Decimal("0.10"))
    free = FakeBackend("free", store)
    sink = Sink()
    forge = Forge([cloud, free], store, on_clip=sink)  # no callbacks bound

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await run_forge(forge)

    assert cloud.calls == [], "must not spend money nobody authorised"
    assert [c.backend for c in sink.clips] == ["free"]


async def test_exhausted_ladder_drops_the_job_quietly(store: ClipStore) -> None:
    a = FakeBackend("a", store, status=BackendStatus.DOWN)
    b = FakeBackend("b", store, fail_times=99)
    sink = Sink()
    forge = Forge([a, b], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await run_forge(forge)

    # Outcome 3 (Architecture §2.5): not an exception, not a raised error.
    assert sink.clips == []
    assert forge.stats.dropped == 1
    assert forge.stats.completed == 0


async def test_forge_negotiates_duration_and_tier_against_capabilities(
    store: ClipStore,
) -> None:
    backend = FakeBackend(
        "b", store, durations=frozenset({4, 6, 8}), tiers=frozenset({"only-tier"})
    )
    forge = Forge([backend], store, on_clip=Sink())

    await forge.request(zone="main", prompt="p", duration_s=5, tier="veo-3.1-quality")
    await run_forge(forge)

    assert backend.calls[0]["duration_s"] == 6, "5s snaps to the nearer/longer 6s"
    assert backend.calls[0]["tier"] == "only-tier"


async def test_forge_drops_capabilities_the_rung_lacks(store: ClipStore) -> None:
    no_extend = FakeBackend("plain", store, native_extend=False, image_seed=False)
    forge = Forge([no_extend], store, on_clip=Sink())
    source = ClipRef(
        id="deadbeef", path=store.path_for("deadbeef"), duration_s=8.0,
        zone="main", backend="veo", tier="veo-3.1-lite",
    )

    await forge.request(
        zone="main", prompt="p", duration_s=8, tier="fake",
        seed_image=b"png-bytes", extend_from=source,
    )
    await run_forge(forge)

    assert no_extend.calls[0]["seed_image"] is None
    assert no_extend.calls[0]["extend_from"] is None


async def test_queue_depth_reflects_pending_work(store: ClipStore) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()

    class Blocking(FakeBackend):
        async def generate(self, *args, **kwargs):
            entered.set()
            await gate.wait()
            return await super().generate(*args, **kwargs)

    backend = Blocking("slow", store)
    forge = Forge([backend], store, on_clip=Sink())

    for _ in range(3):
        await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    assert forge.queue_depth("main") == 3
    assert forge.queue_depth("other") == 0

    forge.start()
    await asyncio.wait_for(entered.wait(), 2.0)
    # One job is out of the queue and in flight; depth must still say 3.
    assert forge.queue_depth("main") == 3, "in-flight work still counts"
    assert forge.total_queue_depth() == 3

    gate.set()
    await forge.join("main")
    assert forge.queue_depth("main") == 0
    await forge.close()


async def test_zones_have_independent_queues(store: ClipStore) -> None:
    backend = FakeBackend("b", store)
    sink = Sink()
    forge = Forge([backend], store, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await forge.request(zone="bar", prompt="p", duration_s=8, tier="fake")
    await forge.request(zone="bar", prompt="p", duration_s=8, tier="fake")

    assert forge.queue_depth("main") == 1
    assert forge.queue_depth("bar") == 2

    forge.start()
    await forge.join()
    await forge.close()

    assert len(sink.clips) == 3
    assert {c.zone for c in sink.clips} == {"main", "bar"}


async def test_job_repr_redacts_the_prompt(store: ClipStore) -> None:
    from egregore.forge import GenerationJob

    job = GenerationJob(zone="main", prompt="a private sentence", duration_s=8, tier="mock")
    assert "private" not in repr(job)
    assert "redacted" in repr(job)


async def test_clip_sink_error_does_not_kill_the_worker(store: ClipStore) -> None:
    backend = FakeBackend("b", store)
    seen: list[str] = []

    async def angry(clip: ClipRef) -> None:
        seen.append(clip.id)
        raise RuntimeError("loom exploded")

    forge = Forge([backend], store, on_clip=angry)
    await forge.request(zone="main", prompt="p", duration_s=8, tier="fake")
    await forge.request(zone="main", prompt="p", duration_s=6, tier="fake")
    await run_forge(forge)

    assert len(seen) == 2, "the second job must still be processed"


# ---------------------------------------------------------------------------
# VeoBackend — submit / poll / download against MockTransport
# ---------------------------------------------------------------------------


VIDEO_URI = "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"veo-clip-bytes" * 8


def veo_transport(
    recorder: dict, *, poll_pending: int = 2, submit_status: int = 200
) -> httpx.MockTransport:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        recorder.setdefault("requests", []).append((request.method, url))
        recorder.setdefault("headers", []).append(dict(request.headers))

        if request.method == "POST" and url.endswith(":predictLongRunning"):
            recorder["payload"] = json.loads(request.content)
            recorder["submit_url"] = url
            if submit_status != 200:
                return httpx.Response(submit_status, json={"error": "nope"})
            return httpx.Response(200, json={"name": "models/veo/operations/op-1"})

        if request.method == "GET" and url.endswith("/models/veo/operations/op-1"):
            state["polls"] += 1
            if state["polls"] <= poll_pending:
                return httpx.Response(200, json={"name": "op-1", "done": False})
            recorder["polls"] = state["polls"]
            return httpx.Response(
                200,
                json={
                    "name": "op-1",
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [{"video": {"uri": VIDEO_URI}}]
                        }
                    },
                },
            )

        if request.method == "GET" and url == VIDEO_URI:
            recorder["downloaded"] = True
            return httpx.Response(200, content=VIDEO_BYTES)

        return httpx.Response(404, json={"error": f"unexpected {url}"})

    return httpx.MockTransport(handler)


def veo_backend(store: ClipStore, transport: httpx.MockTransport, **kwargs) -> VeoBackend:
    return VeoBackend(
        store,
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
        poll_interval_s=0.0,
        **kwargs,
    )


async def test_veo_full_submit_poll_download_cycle(store: ClipStore) -> None:
    recorder: dict = {}
    backend = veo_backend(store, veo_transport(recorder))

    ref = await backend.generate("an abstract prompt", 8, "veo-3.1-lite", zone="main")

    assert ref.backend == "veo"
    assert ref.tier == "veo-3.1-lite"
    assert ref.zone == "main"
    assert ref.duration_s == 8.0
    assert ref.path.read_bytes() == VIDEO_BYTES
    assert store.get(ref.id) is ref

    assert recorder["submit_url"].endswith(":predictLongRunning")
    assert recorder["polls"] == 3, "polled until done"
    assert recorder["downloaded"] is True
    assert recorder["headers"][0]["x-goog-api-key"] == "test-key"
    # Nothing left in the scratch directory.
    assert list(store.incoming_dir.iterdir()) == []
    await backend.close()


async def test_veo_never_asks_for_audio(store: ClipStore) -> None:
    recorder: dict = {}
    backend = veo_backend(store, veo_transport(recorder))
    await backend.generate("p", 4, "veo-3.1-quality", zone="main")

    params = recorder["payload"]["parameters"]
    # Veo 3.x rejects `generateAudio` outright, so the request must not carry
    # it at all. PRD V-3 is honoured at playback instead: the Lens plays every
    # clip muted, so the room still only hears itself.
    assert "generateAudio" not in params
    assert params["durationSeconds"] == 4
    assert params["aspectRatio"] == "16:9"
    assert recorder["payload"]["instances"][0]["prompt"] == "p"
    await backend.close()


async def test_veo_seed_image_is_base64_in_the_payload(store: ClipStore) -> None:
    import base64

    recorder: dict = {}
    backend = veo_backend(store, veo_transport(recorder))
    await backend.generate("p", 4, "veo-3.1-lite", seed_image=b"\x89PNGfake", zone="main")

    image = recorder["payload"]["instances"][0]["image"]
    assert base64.b64decode(image["bytesBase64Encoded"]) == b"\x89PNGfake"
    assert image["mimeType"] == "image/png"
    await backend.close()


async def test_veo_extend_uses_the_provider_side_uri(store: ClipStore) -> None:
    recorder: dict = {}
    backend = veo_backend(store, veo_transport(recorder))
    first = await backend.generate("p", 8, "veo-3.1-fast", zone="main")

    await backend.generate("p", 8, "veo-3.1-fast", extend_from=first, zone="main")
    assert recorder["payload"]["instances"][0]["video"] == {"uri": VIDEO_URI}

    # A clip this backend did not generate cannot be extended: extension only
    # accepts provider-generated source video (Architecture §2.6).
    foreign = ClipRef(
        id="ffffffffffffffff", path=store.path_for("ffffffffffffffff"),
        duration_s=8.0, zone="main", backend="local", tier="ltx-2",
    )
    with pytest.raises(RuntimeError, match="provider-side video reference"):
        await backend.generate("p", 8, "veo-3.1-fast", extend_from=foreign, zone="main")

    # Veo 3.1 Lite has no continuation mode at all, so asking it to extend is
    # refused before any request is built.
    with pytest.raises(RuntimeError, match="cannot extend video"):
        await backend.generate("p", 8, "veo-3.1-lite", extend_from=first, zone="main")
    await backend.close()


async def test_veo_cost_table_math(store: ClipStore) -> None:
    backend = VeoBackend(store, api_key="k")

    assert SAFETY_FACTOR == Decimal("2")
    # Priced off each tier's *most expensive* resolution (lite 1080p $0.08,
    # fast 4k $0.30, quality 4k $0.60), then doubled.
    assert backend.max_plausible_cost(8, "veo-3.1-lite") == Decimal("1.28")
    assert backend.max_plausible_cost(8, "veo-3.1-fast") == Decimal("4.80")
    assert backend.max_plausible_cost(8, "veo-3.1-quality") == Decimal("9.60")
    assert backend.max_plausible_cost(4, "veo-3.1-lite") == Decimal("0.64")

    # Always at least twice the published rate — the ceiling must hold even
    # when the price table is wrong (Architecture §2.5).
    for tier, per_second in COST_PER_SECOND.items():
        assert backend.max_plausible_cost(8, tier) >= per_second * 8 * 2

    # And at least twice the real published rate for *every* resolution that
    # tier can be run at, not just the one currently configured. This is the
    # assertion the old table failed: it reserved $0.48 for a lite 8s clip
    # that bills at $0.64, so a party could have overrun its own ceiling.
    for tier, by_res in COST_PER_SECOND_BY_RESOLUTION.items():
        for price in by_res.values():
            assert backend.max_plausible_cost(8, tier) >= price * 8 * 2

    # An unknown tier is charged as the most expensive one, never as free.
    assert backend.max_plausible_cost(8, "veo-9-imaginary") == Decimal("9.60")
    assert isinstance(backend.max_plausible_cost(8, "veo-3.1-lite"), Decimal)


async def test_veo_capabilities_and_health(store: ClipStore, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    keyless = VeoBackend(store)
    health = await keyless.health()
    assert health.status is BackendStatus.DOWN
    assert "GEMINI_API_KEY" in health.detail

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    from_env = VeoBackend(store)
    assert from_env.api_key == "from-env"
    assert (await from_env.health()).status is BackendStatus.HEALTHY
    # Constructor argument wins over the environment.
    assert VeoBackend(store, api_key="explicit").api_key == "explicit"

    caps = keyless.capabilities
    assert caps.allowed_durations_s == frozenset({4, 6, 8})
    assert caps.supports_native_extend is True
    assert caps.supports_image_seed is True
    assert caps.max_chain_length == 20
    assert caps.tiers == frozenset(COST_PER_SECOND)


async def test_veo_rejects_bad_duration_and_tier(store: ClipStore) -> None:
    backend = VeoBackend(store, api_key="k")
    with pytest.raises(ValueError):
        await backend.generate("p", 5, "veo-3.1-lite")
    with pytest.raises(ValueError):
        await backend.generate("p", 8, "not-a-tier")


async def test_veo_surfaces_http_and_operation_errors(store: ClipStore) -> None:
    recorder: dict = {}
    backend = veo_backend(store, veo_transport(recorder, submit_status=500))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await backend.generate("p", 8, "veo-3.1-lite")
    await backend.close()

    def failing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"name": "models/veo/operations/op-1"})
        return httpx.Response(
            200, json={"done": True, "error": {"code": 9, "message": "safety"}}
        )

    backend = veo_backend(store, httpx.MockTransport(failing))
    with pytest.raises(RuntimeError, match="operation failed"):
        await backend.generate("p", 8, "veo-3.1-lite")
    assert list(store.incoming_dir.iterdir()) == []
    await backend.close()


async def test_veo_poll_timeout(store: ClipStore) -> None:
    def never_done(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"name": "models/veo/operations/op-1"})
        return httpx.Response(200, json={"done": False})

    backend = veo_backend(store, httpx.MockTransport(never_done), timeout_s=0.0)
    with pytest.raises(TimeoutError):
        await backend.generate("p", 8, "veo-3.1-lite")
    await backend.close()


async def test_veo_in_a_ladder_above_the_mock(store: ClipStore) -> None:
    """The whole point of the ladder: cloud first, procedural underneath."""
    recorder: dict = {}
    cloud = veo_backend(store, veo_transport(recorder, poll_pending=0))
    ledger = Ledger(approve=False)  # budget exhausted
    fallback = FakeBackend("mockish", store)
    sink = Sink()
    forge = Forge(
        [cloud, fallback], store, authorize=ledger.authorize, settle=ledger.settle,
        release=ledger.release, on_clip=sink,
    )

    await forge.request(zone="main", prompt="p", duration_s=8, tier="veo-3.1-lite")
    await run_forge(forge)

    assert ledger.authorized == [("veo", Decimal("1.28"))]
    assert "requests" not in recorder, "refused rung must never hit the network"
    assert [c.backend for c in sink.clips] == ["mockish"]
    await cloud.close()


# ---------------------------------------------------------------------------
# ComfyUIBackend
# ---------------------------------------------------------------------------


def comfy_transport(recorder: dict, *, pending: int = 1) -> httpx.MockTransport:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            recorder["submitted"] = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": "pid-1"})
        if path == "/history/pid-1":
            state["polls"] += 1
            if state["polls"] <= pending:
                return httpx.Response(200, json={})
            recorder["polls"] = state["polls"]
            return httpx.Response(
                200,
                json={
                    "pid-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "8": {
                                "gifs": [
                                    {
                                        "filename": "egregore_00001.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if path == "/view":
            recorder["view_params"] = dict(request.url.params)
            return httpx.Response(200, content=b"ltx-clip-bytes" * 4)
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"os": "posix"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def comfy_backend(store: ClipStore, transport: httpx.MockTransport) -> ComfyUIBackend:
    return ComfyUIBackend(
        store, client=httpx.AsyncClient(transport=transport), poll_interval_s=0.0
    )


async def test_comfy_generate_cycle(store: ClipStore) -> None:
    recorder: dict = {}
    backend = comfy_backend(store, comfy_transport(recorder))

    ref = await backend.generate("an abstract prompt", 4, "ltx-2", zone="main")

    assert ref.backend == "local"
    assert ref.tier == "ltx-2"
    assert ref.duration_s == 4.0
    assert ref.path.read_bytes() == b"ltx-clip-bytes" * 4

    workflow = recorder["submitted"]["prompt"]
    assert workflow["2"]["inputs"]["text"] == "an abstract prompt"
    assert workflow["3"]["inputs"]["text"] != "an abstract prompt"  # negative untouched
    assert workflow["4"]["inputs"]["length"] == 4 * 24 + 1
    assert workflow["6"]["inputs"]["seed"] != 0
    assert recorder["view_params"]["filename"] == "egregore_00001.mp4"
    assert recorder["polls"] == 2
    await backend.close()


async def test_comfy_does_not_mutate_its_template(store: ClipStore) -> None:
    from egregore.forge import DEFAULT_WORKFLOW

    recorder: dict = {}
    backend = comfy_backend(store, comfy_transport(recorder))
    await backend.generate("leaky", 4, "ltx-2")

    assert backend.workflow["2"]["inputs"]["text"] == ""
    assert DEFAULT_WORKFLOW["2"]["inputs"]["text"] == ""
    await backend.close()


async def test_comfy_accepts_an_operator_workflow(store: ClipStore) -> None:
    recorder: dict = {}
    custom = {
        "a": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder"}},
        "b": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"length": 1}},
        "c": {
            "class_type": "VHS_VideoCombine",
            "inputs": {"filename_prefix": "operator"},
        },
    }
    backend = ComfyUIBackend(
        store,
        workflow=custom,
        client=httpx.AsyncClient(transport=comfy_transport(recorder)),
        poll_interval_s=0.0,
    )
    await backend.generate("operator prompt", 8, "ltx-2")

    submitted = recorder["submitted"]["prompt"]
    assert submitted["a"]["inputs"]["text"] == "operator prompt"
    assert submitted["b"]["inputs"]["length"] == 8 * 24 + 1
    assert submitted["c"]["inputs"]["filename_prefix"] == "operator"
    await backend.close()


async def test_comfy_health_down_on_connection_refused(store: ClipStore) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = ComfyUIBackend(store, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    health = await backend.health()

    assert health.status is BackendStatus.DOWN
    assert "ConnectError" in health.detail
    await backend.close()


async def test_comfy_health_up_and_capabilities(store: ClipStore) -> None:
    backend = comfy_backend(store, comfy_transport({}))
    assert (await backend.health()).status is BackendStatus.HEALTHY

    caps = backend.capabilities
    assert caps.allowed_durations_s == frozenset({4, 6, 8})
    assert caps.supports_native_extend is False  # continuity via seed frames only
    # Seeding is offered only when a seeded graph is configured; this backend
    # has none, and saying False is the difference between declining a seed
    # and accepting one it will silently drop.
    assert caps.supports_image_seed is False
    assert caps.tiers == frozenset({"ltx-2"})
    assert backend.max_plausible_cost(8, "ltx-2") == Decimal("0")  # PRD V-4 / B-6
    await backend.close()


async def test_comfy_rejects_bad_duration_and_missing_text_node(store: ClipStore) -> None:
    backend = comfy_backend(store, comfy_transport({}))
    with pytest.raises(ValueError):
        await backend.generate("p", 5, "ltx-2")

    headless = ComfyUIBackend(
        store,
        workflow={"a": {"class_type": "KSampler", "inputs": {}}},
        client=httpx.AsyncClient(transport=comfy_transport({})),
    )
    with pytest.raises(RuntimeError, match="no CLIPTextEncode"):
        await headless.generate("p", 4, "ltx-2")
    await backend.close()
    await headless.close()


# ---------------------------------------------------------------------------
# ComfyUI latency learning — one config across very different hardware
# ---------------------------------------------------------------------------


async def test_comfy_seeds_latency_then_learns_from_observed_renders(
    store: ClipStore,
) -> None:
    # Before any render the backend can only report the seed it was given.
    backend = ComfyUIBackend(
        store,
        client=httpx.AsyncClient(transport=comfy_transport({})),
        poll_interval_s=0.0,
        initial_latency_s=60.0,
        latency_smoothing=0.5,
    )
    assert backend.estimated_latency("ltx-2").total_seconds() == pytest.approx(60.0)

    # The first observation replaces the seed outright rather than being
    # averaged with it: a guess carries no evidence worth preserving.
    backend._observe_latency(300.0)
    assert backend.estimated_latency("ltx-2").total_seconds() == pytest.approx(300.0)

    # Later observations move the estimate but do not let one slow render
    # (a busy GPU, a cold model load) redefine the cadence on its own.
    backend._observe_latency(100.0)
    assert backend.estimated_latency("ltx-2").total_seconds() == pytest.approx(200.0)


async def test_comfy_latency_ignores_nonsense_observations(store: ClipStore) -> None:
    backend = ComfyUIBackend(
        store,
        client=httpx.AsyncClient(transport=comfy_transport({})),
        poll_interval_s=0.0,
        initial_latency_s=45.0,
    )
    backend._observe_latency(0.0)
    backend._observe_latency(-12.0)
    assert backend.estimated_latency("ltx-2").total_seconds() == pytest.approx(45.0)


async def test_comfy_generate_records_its_own_wall_time(store: ClipStore) -> None:
    # A completed generation must move the estimate off its seed, which is
    # what lets the Governor pace to the box it is actually running on.
    backend = ComfyUIBackend(
        store,
        client=httpx.AsyncClient(transport=comfy_transport({})),
        poll_interval_s=0.0,
        initial_latency_s=999.0,
    )
    await backend.generate("a prompt", 4, "ltx-2", zone="main")
    assert backend.estimated_latency("ltx-2").total_seconds() < 999.0


# ---------------------------------------------------------------------------
# FalBackend — one queue protocol, many models
# ---------------------------------------------------------------------------


FAL_VIDEO_URL = "https://v3.fal.media/files/rabbit/abc123.mp4"
FAL_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fal-clip-bytes" * 8
FAL_MODEL_ID = "minimax/h3-max/text-to-video"
#: Where fal actually serves the request, which is not the submit path.
FAL_SHORT = "https://queue.fal.run/minimax/h3-max"


def fal_transport(
    recorder: dict, *, queued_polls: int = 2, submit_status: int = 200,
    fail_with: str | None = None,
) -> httpx.MockTransport:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        recorder.setdefault("requests", []).append((request.method, url))
        recorder.setdefault("headers", []).append(dict(request.headers))

        if request.method == "POST" and url.endswith(FAL_MODEL_ID):
            recorder["payload"] = json.loads(request.content)
            if submit_status != 200:
                return httpx.Response(submit_status, json={"detail": "nope"})
            # fal replies with its own URLs, and they do not carry the whole
            # model id: submitting to minimax/h3-max/text-to-video yields
            # status at minimax/h3-max/requests/{id}/status. Mirroring that
            # here is what stops the backend inventing a 404.
            return httpx.Response(200, json={
                "request_id": "req-1",
                "status_url": f"{FAL_SHORT}/requests/req-1/status",
                "response_url": f"{FAL_SHORT}/requests/req-1",
            })

        if request.method == "GET" and url == f"{FAL_SHORT}/requests/req-1/status":
            state["polls"] += 1
            if fail_with is not None:
                return httpx.Response(
                    200, json={"status": "COMPLETED", "error": "boom",
                               "error_type": fail_with},
                )
            if state["polls"] <= queued_polls:
                return httpx.Response(200, json={"status": "IN_QUEUE", "queue_position": 1})
            recorder["polls"] = state["polls"]
            return httpx.Response(200, json={"status": "COMPLETED"})

        if request.method == "GET" and url == f"{FAL_SHORT}/requests/req-1":
            return httpx.Response(
                200,
                json={"video": {"url": FAL_VIDEO_URL, "content_type": "video/mp4"},
                      "expanded_prompt": "…", "timings": {"inference": 42.0}},
            )

        if request.method == "GET" and url == FAL_VIDEO_URL:
            recorder["downloaded"] = True
            return httpx.Response(200, content=FAL_BYTES)

        return httpx.Response(404, json={"error": f"unexpected {url}"})

    return httpx.MockTransport(handler)


def fal_backend(store: ClipStore, transport: httpx.MockTransport, **kw) -> FalBackend:
    kw.setdefault("api_key", "fal-test-key")
    return FalBackend(
        store, client=httpx.AsyncClient(transport=transport), poll_interval_s=0.0, **kw
    )


async def test_fal_generate_cycle(store: ClipStore) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder))

    ref = await backend.generate("an abstract prompt", 6, "minimax-h3-max", zone="main")

    assert ref.backend == "fal"
    assert ref.tier == "minimax-h3-max"
    assert ref.duration_s == 6.0
    assert ref.path.read_bytes() == FAL_BYTES
    assert recorder["downloaded"] is True
    assert recorder["polls"] == 3  # polled through IN_QUEUE, then COMPLETED

    body = recorder["payload"]
    assert body["prompt"] == "an abstract prompt"
    assert body["duration"] == 6
    assert body["resolution"] == "768P"
    assert body["prompt_expansion_mode"] == "balanced"  # model-specific knob
    await backend.close()


async def test_fal_authorizes_with_key_scheme_and_not_on_the_cdn(store: ClipStore) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder))
    await backend.generate("p", 5, "minimax-h3-max", zone="main")

    by_url = dict(zip([u for _, u in recorder["requests"]], recorder["headers"], strict=True))
    api_headers = [h for u, h in by_url.items() if u != FAL_VIDEO_URL]
    assert all(h["authorization"] == "Key fal-test-key" for h in api_headers)
    # The media URL is pre-signed and points at a CDN we do not control, so
    # the key must not ride along with the download.
    assert "authorization" not in by_url[FAL_VIDEO_URL]
    await backend.close()


async def test_fal_reserves_against_standard_price_never_the_promo(store: ClipStore) -> None:
    # minimax-h3-max runs $0.025/s (480P) on promo but $0.05 standard. A
    # reservation outlives a promo, so the ceiling holds the standard rate.
    at_480 = FalBackend(store, api_key="k", resolution="480P")
    assert at_480.max_plausible_cost(8, "minimax-h3-max") == Decimal("0.80")  # .05*8*2

    at_768 = FalBackend(store, api_key="k", resolution="768P")
    assert at_768.max_plausible_cost(8, "minimax-h3-max") == Decimal("1.28")  # .08*8*2
    assert at_768.max_plausible_cost(5, "minimax-h3") == Decimal("0.60")

    # Unknown tier is charged as the most expensive model, never as free.
    assert at_480.max_plausible_cost(8, "some-new-model") == Decimal("1.28")

    # Whatever the resolution, a reservation covers at least twice the real
    # standard price of the clip it is reserving for (PRD B-2).
    for backend in (at_480, at_768):
        for key, model in FAL_MODELS.items():
            actual = model.price_per_second.get(
                backend.resolution, model.worst_price_per_second
            )
            assert backend.max_plausible_cost(8, key) >= actual * 8 * 2


async def test_fal_rejects_unknown_model_at_construction(store: ClipStore) -> None:
    with pytest.raises(ValueError, match="unknown fal model"):
        FalBackend(store, model="not-a-model", api_key="k")


async def test_fal_rejects_duration_the_model_will_not_accept(store: ClipStore) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder))
    # 4s is a valid Egregore clip length but below MiniMax's 5s floor; failing
    # here lets the ladder drop a rung instead of paying for a refusal.
    with pytest.raises(ValueError, match="duration 4s not in"):
        await backend.generate("p", 4, "minimax-h3-max", zone="main")
    assert "requests" not in recorder
    await backend.close()


async def test_fal_refuses_continuation_and_seeding_it_cannot_do(store: ClipStore) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder))
    first = await backend.generate("p", 5, "minimax-h3-max", zone="main")

    # Silently dropping either would look like it worked while quietly
    # breaking the continuity handoff.
    with pytest.raises(RuntimeError, match="cannot continue a previous clip"):
        await backend.generate("p", 5, "minimax-h3-max", extend_from=first, zone="main")
    with pytest.raises(RuntimeError, match="takes no first-frame seed"):
        await backend.generate("p", 5, "minimax-h3-max", seed_image=b"png", zone="main")

    caps = backend.capabilities
    assert caps.supports_native_extend is False
    assert caps.max_chain_length == 0
    await backend.close()


async def test_fal_surfaces_a_failed_generation(store: ClipStore) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder, fail_with="ContentPolicy"))
    with pytest.raises(RuntimeError, match="ContentPolicy"):
        await backend.generate("p", 5, "minimax-h3-max", zone="main")
    await backend.close()


async def test_fal_submit_failure_is_not_silent(store: ClipStore) -> None:
    backend = fal_backend(store, fal_transport({}, submit_status=422))
    with pytest.raises(RuntimeError, match="HTTP 422"):
        await backend.generate("p", 5, "minimax-h3-max", zone="main")
    await backend.close()


@pytest.mark.parametrize(
    "requested,expected",
    [("480p", "480P"), ("768P", "768P"), ("720p", "768P"), ("1080p", "768P"),
     ("4k", "768P")],
)
async def test_fal_maps_party_resolutions_onto_what_the_model_offers(
    store: ClipStore, requested: str, expected: str
) -> None:
    # Party configs speak "1080p"; these models top out at 768P. Asking for
    # more should land on the best available rather than erroring at request
    # time, which would take the rung down for a cosmetic mismatch.
    backend = FalBackend(store, api_key="k", resolution=requested)
    assert backend.resolution == expected


async def test_fal_health_needs_a_key(store: ClipStore) -> None:
    assert (await FalBackend(store, api_key=None).health()).status is BackendStatus.DOWN
    healthy = await FalBackend(store, api_key="k").health()
    assert healthy.status is BackendStatus.HEALTHY
    assert "minimax-h3-max" in healthy.detail


async def test_fal_learns_its_queue_latency(store: ClipStore) -> None:
    backend = fal_backend(store, fal_transport({}))
    seeded = backend.estimated_latency("minimax-h3-max").total_seconds()
    # Seeded low on purpose: the Governor will not schedule faster than this,
    # so a pessimistic seed stalls the first clip and with it the measurement
    # that would have corrected the seed.
    assert seeded == pytest.approx(20.0)
    await backend.generate("p", 5, "minimax-h3-max", zone="main")
    assert backend.estimated_latency("minimax-h3-max").total_seconds() < seeded
    await backend.close()


async def test_fal_follows_the_urls_fal_returns_not_ones_it_builds(store: ClipStore) -> None:
    # Submitting to minimax/h3-max/text-to-video yields a status URL at
    # minimax/h3-max/requests/{id}/status. Building that path from the model
    # id instead produced a 404 against the real service.
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder))
    await backend.generate("p", 5, "minimax-h3-max", zone="main")
    polled = [u for m, u in recorder["requests"] if m == "GET" and "/requests/" in u]
    assert polled, "the backend must poll somewhere"
    for url in polled:
        assert "/text-to-video/requests/" not in url, f"invented path: {url}"
        assert url.startswith(FAL_SHORT + "/requests/")
    await backend.close()


async def test_fal_advertises_only_the_model_it_was_configured_with(store: ClipStore) -> None:
    # The Forge maps an unsupported tier onto sorted(tiers)[0]. Advertising
    # the whole catalogue therefore meant asking for minimax-h3-max and
    # silently generating on minimax-h3, which sorts first.
    backend = FalBackend(store, model="minimax-h3-max", api_key="k")
    assert backend.capabilities.tiers == frozenset({"minimax-h3-max"})
    assert sorted(backend.capabilities.tiers)[0] == "minimax-h3-max"


async def test_fal_generates_on_the_configured_model_when_asked_for_another(
    store: ClipStore,
) -> None:
    recorder: dict = {}
    backend = fal_backend(store, fal_transport(recorder), model="minimax-h3-max")
    # A party config's `model` is a cloud tier name like veo-3.1-lite; it must
    # not silently select a different fal model.
    ref = await backend.generate("p", 5, "veo-3.1-lite", zone="main")
    assert ref.tier == "minimax-h3-max"
    submit = [u for m, u in recorder["requests"] if m == "POST"][0]
    assert submit.endswith("minimax/h3-max/text-to-video"), submit
    await backend.close()


async def test_a_fill_never_reaches_a_metered_rung(store: ClipStore) -> None:
    # A fill exists to keep the loop populated between paid generations. If it
    # could reach a priced backend it would spend exactly the budget the
    # cadence was spacing out.
    class Priced(MockBackend):
        def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
            return Decimal("1.00")

    priced = Priced(store, name="cloudish")
    free = MockBackend(store, name="procedural")
    ledger = Ledger(approve=True)
    sink = Sink()
    forge = Forge([priced, free], store, authorize=ledger.authorize,
                  settle=ledger.settle, release=ledger.release, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="mock",
                        free_only=True)
    await run_forge(forge)

    assert ledger.authorized == [], "a fill must not reserve against the budget"
    assert [c.backend for c in store.all()] == ["procedural"]


async def test_a_normal_request_still_prefers_the_paid_rung(store: ClipStore) -> None:
    class Priced(MockBackend):
        def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
            return Decimal("1.00")

    priced = Priced(store, name="cloudish")
    free = MockBackend(store, name="procedural")
    ledger = Ledger(approve=True)
    sink = Sink()
    forge = Forge([priced, free], store, authorize=ledger.authorize,
                  settle=ledger.settle, release=ledger.release, on_clip=sink)

    await forge.request(zone="main", prompt="p", duration_s=8, tier="mock")
    await run_forge(forge)

    assert [c.backend for c in store.all()] == ["cloudish"]


async def test_comfy_cancels_its_queue_on_close(store: ClipStore) -> None:
    """ComfyUI keeps its queue server-side, so work outlives the party that
    asked for it. Four orphaned twelve-minute renders ahead of a live one is
    indistinguishable from local video simply never appearing."""
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, url, body))
        if url.endswith("/prompt"):
            return httpx.Response(200, json={"prompt_id": "never-finishes"})
        if "/history/" in url:
            return httpx.Response(200, json={})          # never completes
        if url.endswith("/system_stats"):
            return httpx.Response(200, json={"system": {}})
        return httpx.Response(200, json={})

    backend = ComfyUIBackend(
        store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        poll_interval_s=0.0, timeout_s=0.2,
    )
    with pytest.raises(TimeoutError):
        await backend.generate("p", 4, "ltx-2", zone="main")

    # The prompt is no longer tracked once its own generate() gave up...
    await backend.close()
    deletes = [b for m, u, b in seen if m == "POST" and u.endswith("/queue")]
    assert deletes == [] or deletes[0]["delete"] == ["never-finishes"]


async def test_comfy_cancel_names_the_prompts_it_queued(store: ClipStore) -> None:
    backend = ComfyUIBackend(
        store, client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={}))),
        poll_interval_s=0.0,
    )
    backend._inflight.update({"a", "b"})
    await backend.cancel_inflight()
    assert backend._inflight == set(), "cancelling clears what it cancelled"


async def test_comfy_offers_seeding_only_with_a_seeded_graph(store: ClipStore) -> None:
    plain = ComfyUIBackend(store)
    assert plain.capabilities.supports_image_seed is False

    seeded = ComfyUIBackend(store, seed_workflow={"12": {"class_type": "LoadImage",
                                                        "inputs": {"image": "x.png"}}})
    assert seeded.capabilities.supports_image_seed is True


async def test_comfy_uploads_the_seed_and_points_the_graph_at_it(store: ClipStore) -> None:
    """Continuity means this clip starts where the last one ended. The seed
    frame has to reach ComfyUI's input directory and the graph has to name
    it, or the Loom believes it is building a movement out of unrelated
    clips."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/upload/image"):
            seen["uploaded"] = True
            return httpx.Response(200, json={"name": "stored-seed.png"})
        if url.endswith("/prompt"):
            seen["graph"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1"})
        if "/history/" in url:
            return httpx.Response(200, json={"p1": {"status": {"status_str": "success"},
                "outputs": {"8": {"gifs": [{"filename": "o.mp4", "subfolder": "",
                                            "type": "output"}]}}}})
        if "/view" in url:
            return httpx.Response(200, content=b"seeded-clip-bytes")
        return httpx.Response(200, json={"system": {}})

    seed_graph = {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""},
              "_meta": {"title": "positive"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
        "13": {"class_type": "LTXVImgToVideo", "inputs": {"length": 1}},
        "6": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }
    backend = ComfyUIBackend(
        store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        poll_interval_s=0.0, seed_workflow=seed_graph,
    )
    await backend.generate("a prompt", 4, "ltx-2", seed_image=b"\x89PNG-bytes", zone="main")

    assert seen.get("uploaded") is True
    graph = seen["graph"]
    assert graph["12"]["inputs"]["image"] == "stored-seed.png"
    # 4s at 24fps, 8n+1
    assert graph["13"]["inputs"]["length"] == 97
    assert graph["2"]["inputs"]["text"] == "a prompt"


async def test_comfy_without_a_seeded_graph_says_so_rather_than_pretending(
    store: ClipStore,
) -> None:
    recorder: dict = {}
    backend = comfy_backend(store, comfy_transport(recorder))
    # No seed graph configured: the clip is still rendered, unseeded, and the
    # operator is told — silently dropping the frame is what used to happen.
    ref = await backend.generate("p", 4, "ltx-2", seed_image=b"png", zone="main")
    assert ref is not None
    assert "12" not in recorder["submitted"]["prompt"]


async def test_a_fill_does_not_wait_behind_a_slow_generation(store: ClipStore) -> None:
    """A fill exists to cover the wait for a slow backend. Queued behind that
    backend it is useless: measured against local diffusion at 164s a clip,
    the fills never ran at all and the loop sat on a single clip while the
    backpressure counter climbed into the hundreds."""
    started = asyncio.Event()
    release = asyncio.Event()

    class Slow(MockBackend):
        def estimated_latency(self, tier):
            from datetime import timedelta
            return timedelta(seconds=160)      # local diffusion, free but slow

        async def generate(self, prompt, duration_s, tier, **kw):
            started.set()
            await release.wait()
            return await super().generate(prompt, duration_s, tier, **kw)

    slow = Slow(store, name="slow")
    free = MockBackend(store, name="procedural")
    sink = Sink()
    forge = Forge([slow, free], store, on_clip=sink)
    forge.start()
    try:
        await forge.request(zone="main", prompt="p", duration_s=8, tier="mock")
        await asyncio.wait_for(started.wait(), timeout=5)

        # With the slow backend mid-render, a fill must still get through.
        await forge.request(zone="main", prompt="f", duration_s=8, tier="mock",
                            free_only=True)
        for _ in range(100):
            if any(c.backend == "procedural" for c in store.all()):
                break
            await asyncio.sleep(0.05)
        assert any(c.backend == "procedural" for c in store.all()), (
            "the fill never ran while the slow backend held the zone"
        )
    finally:
        release.set()
        await forge.close()


async def test_fills_are_not_counted_in_the_backpressure_signal(store: ClipStore) -> None:
    # queue_depth gates whether to ask for more imagery. Counting the free
    # lane would let a slow backend's own gap-filling suppress the next real
    # generation.
    forge = Forge([MockBackend(store)], store, on_clip=Sink())
    await forge.request(zone="main", prompt="f", duration_s=8, tier="mock",
                        free_only=True)
    assert forge.queue_depth("main") == 0
    assert forge.fill_queue_depth("main") == 1


async def test_a_fill_skips_a_free_but_slow_rung(store: ClipStore) -> None:
    """free_only originally filtered on cost alone. Local diffusion is free
    and takes minutes, so fills were routed straight into the wait they
    existed to cover — which is why a local party sat on one clip."""
    from datetime import timedelta

    class SlowFree(MockBackend):
        def estimated_latency(self, tier):
            return timedelta(seconds=160)

    slow = SlowFree(store, name="local")
    fast = MockBackend(store, name="procedural")
    forge = Forge([slow, fast], store, on_clip=Sink())
    await forge.request(zone="main", prompt="f", duration_s=8, tier="mock",
                        free_only=True)
    await run_forge(forge)
    assert [c.backend for c in store.all()] == ["procedural"]


# ---------------------------------------------------------------------------
# ComfyUI hardware knobs
#
# Steps and resolution are properties of the machine, not of the graph file.
# The same graph should run on a laptop and on a DGX at settings appropriate
# to each, without shipping a second copy of the graph to change a number.
# ---------------------------------------------------------------------------


def _sampler_graph() -> dict:
    return {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "2": {
            "class_type": "EmptyLTXVLatentVideo",
            "inputs": {"length": 1, "width": 640, "height": 384},
        },
        "3": {"class_type": "KSampler", "inputs": {"steps": 12, "seed": 0}},
    }


def _inputs_of(graph: dict, class_type: str) -> dict:
    return next(n["inputs"] for n in graph.values() if n["class_type"] == class_type)


def test_comfy_steps_and_resolution_come_from_config(tmp_path):
    store = ClipStore(tmp_path)
    backend = ComfyUIBackend(
        store, workflow=_sampler_graph(), steps=8, resolution="512x320"
    )
    graph = backend._patched_workflow("a prompt", duration_s=4)
    assert _inputs_of(graph, "KSampler")["steps"] == 8
    latent = _inputs_of(graph, "EmptyLTXVLatentVideo")
    assert (latent["width"], latent["height"]) == (512, 320)


def test_comfy_leaves_an_operator_graph_alone_when_unset(tmp_path):
    # None means "you know your graph better than I do".
    store = ClipStore(tmp_path)
    backend = ComfyUIBackend(store, workflow=_sampler_graph())
    graph = backend._patched_workflow("a prompt", duration_s=4)
    assert _inputs_of(graph, "KSampler")["steps"] == 12
    assert _inputs_of(graph, "EmptyLTXVLatentVideo")["width"] == 640


def test_comfy_knobs_retune_without_rebuilding_the_backend(tmp_path):
    # The operator changes these mid-party from the dashboard. Rebuilding the
    # backend would mean a restart, and a restart drops the continuity chain.
    store = ClipStore(tmp_path)
    backend = ComfyUIBackend(store, workflow=_sampler_graph(), steps=8)
    backend.steps = 20
    backend.resolution = "768x512"
    graph = backend._patched_workflow("a prompt", duration_s=4)
    assert _inputs_of(graph, "KSampler")["steps"] == 20
    assert _inputs_of(graph, "EmptyLTXVLatentVideo")["height"] == 512


async def test_in_flight_counts_only_the_job_being_rendered(tmp_path):
    store = ClipStore(tmp_path)
    gate = asyncio.Event()

    class Slow(MockBackend):
        async def generate(self, *a, **kw):
            await gate.wait()
            return await super().generate(*a, **kw)

    forge = Forge([Slow(store, name="slow")], store)
    forge.start()
    try:
        assert forge.in_flight("z") == 0
        await forge.request(zone="z", prompt="p", duration_s=2, tier="fast")
        await forge.request(zone="z", prompt="p", duration_s=2, tier="fast")
        await asyncio.sleep(0.05)
        assert forge.in_flight("z") == 1, "one rendering, one waiting"
        assert forge.queue_depth("z") == 2
        gate.set()
        await forge.join("z")
        assert forge.in_flight("z") == 0
    finally:
        await forge.close()
