"""Integration drills: the Implementation Plan §9 failure drills that can run
in CI, executed against the really-wired pipeline (no HTTP server).

Each drill builds a miniature party via the same classes run_party uses:
FixtureSource -> RingBuffer -> Weaver -> Governor -> Forge -> ZoneLoom ->
ConductorState.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest

from egregore.app import LiveSettings, PartyBus, ZonePipeline, make_control_handler
from egregore.conductor import ConductorState
from egregore.config.schema import EgregoreConfig
from egregore.forge import ClipStore, Forge, MockBackend
from egregore.governor import Governor
from egregore.scribe import RingBuffer

CHATTER = """\
00:01\tThe tide pools out past the point were glowing green last night.
00:03\tBioluminescence, the whole cove lit up every time a wave broke.
00:05\tWe stood there for an hour just watching the water breathe light.
00:07\tIt felt like the ocean was dreaming with its eyes open, honestly.
"""


def _cfg(tmp_path: Path, *, zones: list[dict], budget: str = "0",
         duration_s: int = 2, mode: str = "mosaic", max_chain: int = 2) -> EgregoreConfig:
    script = tmp_path / "script.txt"
    if not script.exists():
        script.write_text(CHATTER)
    for z in zones:
        if z.get("mic", {}).get("type") == "fixture" and "fixture_path" not in z["mic"]:
            z["mic"]["fixture_path"] = str(script)
    return EgregoreConfig.model_validate(
        {
            "party": {"name": "drill", "duration_hours": 0.5},
            "generation": {"backend": "mock", "clip_duration_s": duration_s},
            "budget": {"total_usd": budget},
            "continuity": {"default_mode": mode, "max_chain_length": max_chain},
            "zones": zones,
            "clip_store_dir": str(tmp_path / "clips"),
            "demo_time_scale": 30,
        }
    )


class Party:
    """Mini run_party with injectable ladder, for drills."""

    def __init__(self, cfg: EgregoreConfig, ladder=None, ceiling: Decimal | None = None):
        self.cfg = cfg
        self.store = ClipStore(Path(cfg.clip_store_dir))
        self.ladder = ladder or [MockBackend(self.store, name="procedural")]
        self.governor = Governor.from_config(
            cfg, cost_per_clip=Decimal("0"), min_interval_s=30.0
        )
        if ceiling is not None:
            self.governor = Governor.from_config(
                EgregoreConfig.model_validate(
                    {**cfg.model_dump(mode="json"), "budget": {"total_usd": str(ceiling)}}
                ),
                cost_per_clip=Decimal("0"),
                min_interval_s=30.0,
            )
        self.bus = PartyBus()
        self.pipelines: dict[str, ZonePipeline] = {}

        async def authorize(backend_name: str, amount: Decimal):
            return self.governor.authorize("party", backend_name, amount)

        async def settle(res, actual):
            self.governor.settle(res, actual)

        async def release(res):
            self.governor.release(res)

        async def on_clip(clip):
            pipe = self.pipelines.get(clip.zone)
            if pipe is not None:
                await pipe.on_clip(clip)

        self.forge = Forge(self.ladder, self.store, authorize=authorize,
                           settle=settle, release=release, on_clip=on_clip)
        self.state = ConductorState(clip_resolver=self.store.path_for)
        self.live = LiveSettings.from_config(cfg)
        self.shared_ring = (
            RingBuffer.from_config("party", cfg.privacy)
            if cfg.continuity.topology == "commons" else None
        )
        mirror_zone = (
            cfg.zones[0].id
            if cfg.continuity.topology == "mirror" and cfg.zones else None
        )
        for z in cfg.zones:
            self.pipelines[z.id] = ZonePipeline(
                z, cfg, forge=self.forge, governor=self.governor,
                state=self.state, bus=self.bus, live=self.live,
                ring=self.shared_ring,
                generates=(mirror_zone is None or z.id == mirror_zone),
            )
        if mirror_zone is not None:
            self.state.mirror_zone = mirror_zone
        self.state.control_handler = make_control_handler(
            self.bus, self.pipelines, self.state
        )
        self.state.settings_handler = lambda payload: {
            "applied": self.live.apply(payload)
        }

    async def __aenter__(self):
        if self.shared_ring is not None:
            await self.shared_ring.start()
        self.governor.start()
        self.forge.start()
        for p in self.pipelines.values():
            await p.resume(self.store.all())
            await p.run()
        return self

    async def __aexit__(self, *exc):
        for p in self.pipelines.values():
            await p.close()
        if self.shared_ring is not None:
            await self.shared_ring.close()
        await self.forge.close()

    async def wait_clips(self, n: int, zone: str | None = None, timeout: float = 90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clips = [c for c in self.store.all() if zone is None or c.zone == zone]
            if len(clips) >= n:
                return clips
            await asyncio.sleep(0.25)
        raise AssertionError(
            f"wanted {n} clips for zone={zone}, got {len(self.store.all())}"
        )


# ---------------------------------------------------------------------------


async def test_multi_zone_and_bleed(tmp_path):
    """Two zones; one has a dead mic. The dead zone dreams on the live
    zone's themes (degradation ladder + L-7)."""
    cfg = _cfg(tmp_path, zones=[
        {"id": "hearth", "mic": {"type": "fixture"}},
        {"id": "garden", "mic": {"type": "network", "host": "nope.local"}},
    ])
    async with Party(cfg) as party:
        await party.wait_clips(2, zone="hearth")
        await party.wait_clips(1, zone="garden")
        garden = party.pipelines["garden"]
        # The dead-mic zone only has weaver-fallback or borrowed themes;
        # after hearth shares a validated theme, garden must have borrowed.
        assert garden.bleeds >= 1
        assert party.state.get_manifest("hearth") is not None
        assert party.state.get_manifest("garden") is not None
        assert party.state.get_manifest("hearth").entries != (
            party.state.get_manifest("garden").entries
        )


async def test_backend_failover_drill(tmp_path):
    """First rung dies mid-party; clips keep arriving from the next rung."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    store = ClipStore(Path(cfg.clip_store_dir))
    primary = MockBackend(store, name="primary")
    backup = MockBackend(store, name="backup")
    party = Party(cfg, ladder=[primary, backup])
    party.store = store  # share the store the backends write to
    async with party:
        first = await party.wait_clips(1)
        assert first[0].backend in ("primary", "backup")
        primary.fail = True  # drill: kill the first rung
        before = len(party.store.all())
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            newer = [c for c in party.store.all()[before:]]
            if any(c.backend == "backup" for c in newer):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("no clip arrived from the backup rung")


async def test_budget_exhaustion_drill(tmp_path):
    """A priced rung with a tiny ceiling: the ceiling holds exactly and the
    dream continues on the free rung."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    store = ClipStore(Path(cfg.clip_store_dir))

    class PricedMock(MockBackend):
        def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
            return Decimal("1.00")

    priced = PricedMock(store, name="cloudish")
    free = MockBackend(store, name="procedural")
    party = Party(cfg, ladder=[priced, free], ceiling=Decimal("2.00"))
    party.store = store
    async with party:
        clips = await party.wait_clips(4)
        cloud_clips = [c for c in clips if c.backend == "cloudish"]
        free_clips = [c for c in clips if c.backend == "procedural"]
        # exactly $2 of $1 reservations can ever clear
        assert len(cloud_clips) <= 2
        assert free_clips, "free rung never took over after exhaustion"
        assert party.governor.ledger.committed <= Decimal("2.00")


async def test_mute_drill(tmp_path):
    """Mute zeroes the buffer and keeps it empty while speech continues."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        deadline = time.monotonic() + 30
        while pipe.ring.token_count() == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert pipe.ring.token_count() > 0
        handler = party.state.control_handler
        out = await handler("mute", {"zone": "main", "on": True})
        assert out["muted"] is True
        assert pipe.ring.token_count() == 0
        await asyncio.sleep(1.0)  # fixture keeps talking; buffer must stay empty
        assert pipe.ring.token_count() == 0
        await handler("mute", {"zone": "main", "on": False})
        deadline = time.monotonic() + 30
        while pipe.ring.token_count() == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert pipe.ring.token_count() > 0, "buffer did not refill after unmute"


async def test_freeze_drill(tmp_path):
    """Operator freeze halts generation; existing material remains served;
    unfreeze resumes."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        await party.wait_clips(1)
        handler = party.state.control_handler
        await handler("freeze", {"on": True})
        await party.forge.join()  # drain in-flight work started pre-freeze
        n_settled = len(party.store.all())
        await asyncio.sleep(3.5)  # several generation-loop ticks
        assert len(party.store.all()) == n_settled, "clips generated while frozen"
        assert party.state.get_manifest("main") is not None  # loop still served
        await handler("freeze", {"on": False})
        await party.wait_clips(n_settled + 1)


async def test_continuity_handoff_through_pipeline(tmp_path):
    """Continuity mode: chains grow to the ceiling, then a new movement
    starts seeded from the last frame."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}],
               mode="continuity", max_chain=2)
    async with Party(cfg) as party:
        await party.wait_clips(3)
        loom = party.pipelines["main"].loom
        st = loom.status()
        assert st["movement_count"] >= 1
        assert loom.last_frame is not None  # frames actually extracted
        plan = loom.plan_next()
        assert plan is not None


async def test_mode_switch_control(tmp_path):
    """C-4: live mode switch through the control API, without restart."""
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        await party.wait_clips(1)
        handler = party.state.control_handler
        rev_before = party.state.get_manifest("main").revision
        out = await handler("mode", {"zone": "main", "mode": "continuity"})
        assert out["mode"] == "continuity"
        assert party.pipelines["main"].loom.mode == "continuity"
        assert party.state.get_manifest("main").revision > rev_before
        with pytest.raises(ValueError):
            await handler("mode", {"zone": "main", "mode": "sideways"})
        with pytest.raises(ValueError):
            await handler("mute", {"zone": "nowhere"})


async def test_live_settings_change_the_next_clip_without_a_restart(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}], duration_s=2)
    async with Party(cfg) as party:
        await party.wait_clips(1)
        handler = party.state.settings_handler
        assert handler is not None, "the integration layer must bind a settings handler"
        assert party.pipelines["main"].live.clip_duration_s == 2

        result = handler({"generation": {"clip_duration_s": 6}, "aesthetic": {"drift": 0.9}})

        assert set(result["applied"]) == {"generation.clip_duration_s", "aesthetic.drift"}
        # The pipeline reads the same object, so the next cycle sees the change.
        assert party.pipelines["main"].live.clip_duration_s == 6
        assert party.pipelines["main"].live.drift == 0.9


async def test_live_settings_ignore_a_restart_only_key(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    ceiling_before = None
    async with Party(cfg, ceiling=Decimal("2.00")) as party:
        ceiling_before = party.governor.ledger.ceiling
        result = party.state.settings_handler(
            {"budget": {"total_usd": 999}, "generation": {"backend": "fal"}}
        )
    # Moving the ceiling under reservations already held against it, or
    # rebuilding the ladder mid-flight, is exactly what the restart group
    # exists to prevent — the handler must decline both.
    assert result["applied"] == []
    assert ceiling_before == Decimal("2.00")


async def test_cadence_floor_override_is_live(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        assert party.live.cadence_floor_s is None
        party.state.settings_handler({"cadence_floor_s": 45})
        assert party.live.cadence_floor_s == 45.0
        party.state.settings_handler({"cadence_floor_s": 0})
        assert party.live.cadence_floor_s is None, "0 means 'use the backend's own estimate'"


async def test_network_zone_transcribes_audio_that_arrived_over_the_wire(tmp_path):
    import math
    import struct

    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "network"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        assert pipe.network_source is not None, "a network zone needs a NetworkSource"

        # Half a second of a loud tone in 50ms blocks, as a phone would send.
        for _ in range(10):
            block = bytearray()
            for i in range(800):
                block += struct.pack(
                    "<h", int(0.6 * 32767 * math.sin(2 * math.pi * 220 * i / 16000)))
            await pipe.network_source.feed("n1", bytes(block), 16000)

        # Features reached the bus whether or not the gate heard speech in it.
        assert party.state.latest_frame("main") is not None


async def test_a_network_zone_with_no_phones_yet_is_simply_quiet(tmp_path):
    # Unlike a usb mic, this source opens no device, so an empty room must
    # start cleanly rather than falling back to thematic memory.
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "network"}}])
    async with Party(cfg) as party:
        assert party.pipelines["main"].network_source is not None
        assert party.state.latest_frame("main") is None


# ---------------------------------------------------------------------------
# Topologies — how zones relate to each other
# ---------------------------------------------------------------------------


def _two_zone_cfg(tmp_path, topology: str):
    cfg = _cfg(tmp_path, zones=[
        {"id": "kitchen", "mic": {"type": "fixture"}},
        {"id": "garden", "mic": {"type": "fixture"}},
    ])
    cfg.continuity.topology = topology
    return cfg


async def test_independent_topology_keeps_pools_separate(tmp_path):
    cfg = _two_zone_cfg(tmp_path, "independent")
    async with Party(cfg) as party:
        rings = {id(p.ring) for p in party.pipelines.values()}
        assert len(rings) == 2, "each room hears only itself"


async def test_commons_topology_shares_one_transcript_pool(tmp_path):
    cfg = _two_zone_cfg(tmp_path, "commons")
    async with Party(cfg) as party:
        rings = {id(p.ring) for p in party.pipelines.values()}
        assert len(rings) == 1, "commons means one pool for the whole party"
        # Both zones still generate: commons shares the ears, not the eyes.
        assert len(party.pipelines) == 2


async def test_commons_pool_carries_what_either_room_said(tmp_path):
    cfg = _two_zone_cfg(tmp_path, "commons")
    async with Party(cfg) as party:
        party.pipelines["kitchen"].ring.add("someone in the kitchen said this")
        assert "kitchen" in party.pipelines["garden"].ring.snapshot()


async def test_mirror_serves_one_zones_manifest_to_every_zone(tmp_path):
    cfg = _two_zone_cfg(tmp_path, "mirror")
    async with Party(cfg) as party:
        assert party.state.mirror_zone == "kitchen"
        # Only the mirror zone commissions video — that is what makes this
        # one generation stream for the whole venue rather than one per room.
        assert party.pipelines["kitchen"].generates is True
        assert party.pipelines["garden"].generates is False

        await party.wait_clips(1, zone="kitchen")
        kitchen = party.state.get_manifest("kitchen")
        garden = party.state.get_manifest("garden")
        assert kitchen is not None and garden is not None
        assert [e.clip_id for e in kitchen.entries] == [e.clip_id for e in garden.entries]
        assert all(c.zone == "kitchen" for c in party.store.all()), (
            "a follower zone must not commission clips of its own"
        )


async def test_default_topology_is_independent(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    assert cfg.continuity.topology == "independent"
    async with Party(cfg) as party:
        assert party.state.mirror_zone is None


async def test_music_shaped_noise_does_not_reach_the_ring(tmp_path, monkeypatch):
    """A room with music makes the VAD open on things that are not speech.

    A recogniser handed non-speech returns one or two confident-looking
    words, and in the buffer those are indistinguishable from real short
    replies — except that they carry no theme and crowd out the utterances
    that do. Measured in a live room: 17 "utterances" averaging 2.4 words,
    with music playing and nobody talking.
    """
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]

        class Fake:
            def __init__(self, text):
                self.text = text

            async def transcribe(self, pcm, rate):
                return self.text

        pipe._transcriber = Fake("the")
        await pipe._on_speech_audio(b"\x00\x01" * 8000, 16000)
        assert pipe.discarded_fragments == 1

        pipe._transcriber = Fake("yeah okay")
        await pipe._on_speech_audio(b"\x00\x01" * 8000, 16000)
        assert pipe.discarded_fragments == 2

        # A real sentence is kept.
        before = pipe.ring.token_count()
        pipe._transcriber = Fake("the tide pools were glowing green last night")
        await pipe._on_speech_audio(b"\x00\x01" * 8000, 16000)
        assert pipe.ring.token_count() > before
        assert pipe.discarded_fragments == 2


# ---------------------------------------------------------------------------
# Pull scheduling — one render in flight, never a backlog
# ---------------------------------------------------------------------------


class GatedMock(MockBackend):
    """A backend that renders only when told to, so a test can hold a job
    in flight and watch what the loop does meanwhile."""

    def __init__(self, store, **kw):
        super().__init__(store, **kw)
        self.gate = asyncio.Event()
        self.started = 0

    def estimated_latency(self, tier):
        # Behaves like local diffusion: free but slow, so the fill lane must
        # not route fills into it (FILL_MAX_LATENCY_S).
        from datetime import timedelta
        return timedelta(seconds=120)

    async def generate(self, *a, **kw):
        self.started += 1
        await self.gate.wait()
        self.gate.clear()
        return await super().generate(*a, **kw)


async def _wait_store(store: ClipStore, n: int, timeout: float = 30.0) -> list:
    """Party.wait_clips reads the Party's own store; a test that hands in a
    ladder built on its own store must poll that one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        clips = store.all()
        if len(clips) >= n:
            return clips
        await asyncio.sleep(0.1)
    raise AssertionError(f"wanted {n} clips, got {len(store.all())}")


async def test_no_new_request_while_a_render_is_in_flight(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    store = ClipStore(Path(cfg.clip_store_dir))
    slow = GatedMock(store, name="procedural")
    async with Party(cfg, ladder=[slow]) as party:
        pipe = party.pipelines["main"]
        pipe.live.fill_interval_s = None          # isolate the paid lane
        deadline = time.monotonic() + 20
        while slow.started == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert slow.started == 1
        await asyncio.sleep(3.0)                  # several loop ticks, still gated
        assert slow.started == 1, "must not enqueue behind the render"
        assert party.forge.queue_depth("main") == 1
        assert pipe.waited_for_slot > 0
        slow.gate.set()
        await _wait_store(store, 1)
        deadline = time.monotonic() + 20
        while slow.started < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert slow.started == 2, "the next request follows completion"
        slow.gate.set()


async def test_fill_lane_still_covers_a_thin_pool_during_a_render(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    store = ClipStore(Path(cfg.clip_store_dir))
    slow = GatedMock(store, name="local")
    free = MockBackend(store, name="procedural")
    async with Party(cfg, ladder=[slow, free]) as party:
        party.live.fill_interval_s = 0.5
        clips = await _wait_store(store, 2)
        assert all(c.backend == "procedural" for c in clips)
        slow.gate.set()


async def test_selection_is_recorded_and_lag_is_measured(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        await party.wait_clips(2, "main")
        deadline = time.monotonic() + 10
        while pipe.last_lag_s is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        st = pipe.status()
        assert st["last_selection"] is not None and st["last_selection"]["candidates"] >= 1
        assert st["lag_s"] is not None and st["lag_s"] >= 0
        assert "throttled" not in pipe.status()
        assert pipe.status()["in_flight"] in (0, 1)


async def test_selection_weights_are_live_per_zone(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        assert party.live.selection_for("main").novelty == 0.3
        party.state.settings_handler({"weaver": {"selection": {"novelty": 0.8}}})
        assert party.live.selection_for("main").novelty == 0.8
        party.live.apply_zone_selection("main", {"novelty": 0.1})
        assert party.live.selection_for("main").novelty == 0.1
        party.state.settings_handler({"weaver": {"selection": {"novelty": 0.6}}})
        assert party.live.selection_for("main").novelty == 0.1, "zone override wins"


async def test_lag_is_measured_on_the_paid_clip_not_a_fill(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    store = ClipStore(Path(cfg.clip_store_dir))
    slow = GatedMock(store, name="local")
    free = MockBackend(store, name="procedural")
    async with Party(cfg, ladder=[slow, free]) as party:
        pipe = party.pipelines["main"]
        party.live.fill_interval_s = 0.5
        await _wait_store(store, 2)               # fills landed while local is held
        deadline = time.monotonic() + 20
        while (pipe.last_selection is None) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert pipe.last_selection is not None
        assert pipe.last_lag_s is None, "a fill must not stamp the lag"
        held = time.monotonic()
        await asyncio.sleep(2.0)
        slow.gate.set()
        deadline = time.monotonic() + 20
        while pipe.last_lag_s is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        lag = pipe.last_lag_s
        assert lag is not None and lag >= (time.monotonic() - held) - 0.5
        slow.gate.set()


async def test_a_restart_picks_the_pool_back_up(tmp_path):
    # A party that restarts should not go dark: the clips on disk are
    # re-ingested so screens have material at once and, in continuity, the
    # chain can seed from the newest one.
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}], mode="continuity")
    async with Party(cfg) as party:
        await party.wait_clips(2, "main")
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        assert pipe.loom.playlist.size >= 2, "pool resumed before any new render"
        assert pipe.loom.last_frame is not None, "chain seeds from the newest clip"
        assert party.state.get_manifest("main") is not None
