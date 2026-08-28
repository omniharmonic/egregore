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

from egregore.app import PartyBus, ZonePipeline, make_control_handler
from egregore.conductor import ConductorState
from egregore.config.schema import EgregoreConfig
from egregore.forge import ClipStore, Forge, MockBackend
from egregore.governor import Governor

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
        for z in cfg.zones:
            self.pipelines[z.id] = ZonePipeline(
                z, cfg, forge=self.forge, governor=self.governor,
                state=self.state, bus=self.bus,
            )
        self.state.control_handler = make_control_handler(
            self.bus, self.pipelines, self.state
        )

    async def __aenter__(self):
        self.governor.start()
        self.forge.start()
        for p in self.pipelines.values():
            await p.run()
        return self

    async def __aexit__(self, *exc):
        for p in self.pipelines.values():
            await p.close()
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
