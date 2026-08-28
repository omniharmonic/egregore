"""Integration layer: wires every module into a running party.

This is the only place modules meet (CONTRACTS.md). Per zone:

    source -> features -> mood -----------------------------\\
    source -> speech -> ring buffer -> weaver -> validator ->+-> forge -> loom -> conductor -> lens
    governor (cadence + hard ceiling) ----------------------/

Privacy invariants enforced here: transcript text flows only
source -> ring -> weaver stage 1; nothing here logs or persists it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from decimal import Decimal
from pathlib import Path

from egregore.conductor import ConductorState, create_app
from egregore.config.schema import EgregoreConfig, ZoneConfig
from egregore.forge import ClipStore, ComfyUIBackend, Forge, MockBackend, VeoBackend
from egregore.governor import Governor
from egregore.listener import FixtureSource, MoodIntegrator, ZoneEvents
from egregore.loom import ZoneLoom
from egregore.scribe import RingBuffer, install_privacy_excepthook, make_transcriber
from egregore.types import ClipRef, FeatureFrame, VideoBackend
from egregore.weaver import Weaver, build_abstractor

log = logging.getLogger("egregore.app")

# Cloud price table ($/sec, video-only) used for cadence math. The ceiling
# never depends on these being right — reservations use max_plausible_cost.
_CLOUD_PER_SEC = {
    "veo-3.1-lite": Decimal("0.03"),
    "veo-3.1-fast": Decimal("0.10"),
    "veo-3.1-quality": Decimal("0.20"),
}

_DEFAULT_FIXTURE = Path(__file__).parent / "listener" / "fixtures" / "demo_conversation.txt"
_LENS_DIR = Path(__file__).parent.parent / "lens"


def build_ladder(cfg: EgregoreConfig, store: ClipStore) -> list[VideoBackend]:
    """Backend selection ladder (Architecture §2.5). Local paths are
    first-class: the procedural renderer is always the final rung, so the
    dream never starves regardless of GPUs, networks, or budgets."""
    rungs: list[VideoBackend] = []
    choice = cfg.generation.backend
    want_cloud = choice in ("veo", "auto") and cfg.budget.total_usd > 0
    if want_cloud and os.environ.get("GEMINI_API_KEY"):
        rungs.append(
            VeoBackend(
                store,
                resolution=cfg.generation.resolution,
                aspect_ratio=cfg.generation.aspect_ratio,
            )
        )
    elif want_cloud:
        log.warning("cloud backend requested but GEMINI_API_KEY is not set; skipping")
    if choice in ("local", "auto") or cfg.generation.fallback == "local":
        rungs.append(ComfyUIBackend(store, base_url=cfg.generation.comfyui_url))
    # The procedural renderer ("mock") is a real zero-cost backend, always last.
    rungs.append(MockBackend(store, name="procedural"))
    return rungs


def cost_per_clip(cfg: EgregoreConfig, ladder: list[VideoBackend]) -> Decimal:
    """Expected cadence cost: cloud tier price if a cloud rung exists, else 0."""
    if any(isinstance(b, VeoBackend) for b in ladder):
        per_sec = _CLOUD_PER_SEC.get(cfg.generation.model, Decimal("0.20"))
        return per_sec * cfg.generation.clip_duration_s
    return Decimal("0")


class ZonePipeline:
    """Everything one zone owns. Construction wires; run() animates."""

    def __init__(self, zcfg: ZoneConfig, cfg: EgregoreConfig, *, forge: Forge,
                 governor: Governor, state: ConductorState) -> None:
        self.cfg = cfg
        self.zcfg = zcfg
        self.zone = zcfg.id
        self.forge = forge
        self.governor = governor
        self.state = state
        self.muted = False

        self.ring = RingBuffer.from_config(self.zone, cfg.privacy)
        self.weaver = Weaver(build_abstractor(cfg.weaver))
        self.mood = MoodIntegrator()
        self.loom = ZoneLoom.from_config(
            self.zone, cfg.zone_mode(self.zone), cfg.continuity
        )
        self._frame_n = 0
        self._tasks: list[asyncio.Task] = []
        self._source = self._build_source(cfg)

    # -- input wiring -------------------------------------------------------

    def _build_source(self, cfg: EgregoreConfig):
        mic = self.zcfg.mic
        events = ZoneEvents(
            on_features=self._on_features,
            on_speech_text=self._on_speech_text,
            on_speech_audio=self._on_speech_audio,
        )
        if mic.type == "fixture":
            path = Path(mic.fixture_path) if mic.fixture_path else _DEFAULT_FIXTURE
            return FixtureSource(path, events, time_scale=cfg.demo_time_scale)
        if mic.type == "usb":
            try:
                from egregore.listener import MicSource

                self._transcriber = make_transcriber(cfg.asr.engine, cfg.asr.language)
                return MicSource(events, device=mic.device)
            except (RuntimeError, TypeError, ImportError) as e:
                log.warning(
                    "zone %s: mic unavailable (%s); running on thematic memory",
                    self.zone, e,
                )
                return None
        log.warning(
            "zone %s: mic type %r not wired in v1; running on thematic memory",
            self.zone, mic.type,
        )
        return None

    async def _on_features(self, frame: FeatureFrame) -> None:
        self.mood.update(frame)
        await self.state.publish_features(self.zone, frame)
        self._frame_n += 1
        if self._frame_n % 15 == 0:  # mood at ~2 Hz
            await self.state.publish_mood(self.zone, self.mood.state())

    async def _on_speech_text(self, text: str) -> None:
        if not self.muted:
            self.ring.add(text)

    async def _on_speech_audio(self, pcm: bytes, sample_rate: int) -> None:
        if self.muted:
            return
        text = await self._transcriber.transcribe(pcm, sample_rate)
        if text:
            self.ring.add(text)

    # -- output wiring ------------------------------------------------------

    async def on_clip(self, clip: ClipRef) -> None:
        await self.loom.ingest(clip, clip.path)
        self.state.set_manifest(self.zone, self.loom.manifest())

    # -- the generation loop ------------------------------------------------

    async def run(self) -> None:
        await self.ring.start()
        if self._source is not None:
            self._tasks.append(asyncio.create_task(self._source.run()))
        self._tasks.append(asyncio.create_task(self._generation_loop()))

    async def _generation_loop(self) -> None:
        cfg = self.cfg
        while True:
            await asyncio.sleep(1.0)
            try:
                if not self.governor.should_generate(self.zone):
                    continue
                plan = self.loom.plan_next()
                result = await self.weaver.weave(
                    self.ring.snapshot(),
                    grammar=cfg.aesthetic.grammar,
                    drift=cfg.aesthetic.drift,
                    mood=self.mood.state(),
                    continuity=self.loom.continuity_context(),
                )
                if result.purge_requested:
                    self.ring.zero()
                    log.warning("zone %s: cycle skipped, buffer purged", self.zone)
                    continue
                if result.prompt is None:
                    continue
                self.governor.record_generation(self.zone)
                await self.forge.request(
                    zone=self.zone,
                    prompt=result.prompt,
                    duration_s=cfg.generation.clip_duration_s,
                    tier=cfg.generation.model,
                    theme_hint=result.theme,
                    seed_image=plan.seed_image,
                    extend_from=plan.use_extend,
                )
                if result.theme is not None and not result.fallback:
                    self.mood.absorb_theme(result.theme)
                    self.loom.remember_theme(result.theme)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Content-free by the scrubbing excepthook contract; the
                # loop must survive anything (degradation, not death).
                log.exception("zone %s: generation cycle failed", self.zone)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.ring.zero()

    async def close(self) -> None:
        if self._source is not None:
            self._source.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.ring.close()  # zeroes the buffer
        self._tasks.clear()

    def status(self) -> dict:
        frags, occupied = self.ring.occupancy()
        return {
            "zone": self.zone,
            "mode": self.loom.mode,
            "muted": self.muted,
            "queue_depth": self.forge.queue_depth(self.zone),
            "buffer_fragments": frags,
            "buffer_tokens": self.ring.token_count(),
            "prompts_sent": self.weaver.prompts_synthesized,
            "validator_rejections": self.weaver.rejections,
            "purges": self.weaver.purges_requested,
            **self.loom.status(),
        }


def _zone_config_map(cfg: EgregoreConfig) -> dict[str, dict]:
    screens = {s.id: s for s in cfg.screens}
    out: dict[str, dict] = {}
    for z in cfg.zones:
        out[z.id] = {
            "lens_stack": z.lens_stack,
            "crossfade_s": 2.0,
            "screens": {
                sid: {
                    "lens_stack": screens[sid].lens_stack if sid in screens else None,
                    "loop_phase_offset": screens[sid].loop_phase_offset if sid in screens else 0.0,
                    "audio_source": screens[sid].audio_source if sid in screens else "zone",
                }
                for sid in z.screens
            },
        }
    return out


async def run_party(cfg: EgregoreConfig) -> None:
    install_privacy_excepthook()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    store = ClipStore(Path(cfg.clip_store_dir))
    ladder = build_ladder(cfg, store)
    governor = Governor.from_config(
        cfg, cost_per_clip=cost_per_clip(cfg, ladder), min_interval_s=60.0
    )

    pipelines: dict[str, ZonePipeline] = {}

    async def authorize(backend_name: str, amount: Decimal):
        return governor.authorize("party", backend_name, amount)

    async def settle(reservation, actual: Decimal) -> None:
        governor.settle(reservation, actual)

    async def release(reservation) -> None:
        governor.release(reservation)

    async def on_clip(clip: ClipRef) -> None:
        pipe = pipelines.get(clip.zone)
        if pipe is not None:
            await pipe.on_clip(clip)

    forge = Forge(ladder, store, authorize=authorize, settle=settle,
                  release=release, on_clip=on_clip)

    async def status_provider() -> dict:
        return {
            "party": cfg.party.name,
            "governor": governor.status(),
            "zones": {z: p.status() for z, p in pipelines.items()},
            "backends": [b.name for b in ladder],
            "privacy": {
                "retained": "nothing",
                "transcripts_on_disk": 0,
                "prompts_sent": sum(p.weaver.prompts_synthesized for p in pipelines.values()),
                "ring_buffer_minutes": cfg.privacy.ring_buffer_minutes,
            },
        }

    state = ConductorState(
        clip_resolver=store.path_for,
        zone_config=_zone_config_map(cfg),
        status_provider=status_provider,
    )

    password = os.environ.get(cfg.serving.password_env) or None
    if password is None and cfg.serving.public_tunnel:
        password = secrets.token_urlsafe(9)

    app = create_app(state, lens_dir=_LENS_DIR, password=password)

    for z in cfg.zones:
        pipelines[z.id] = ZonePipeline(z, cfg, forge=forge, governor=governor, state=state)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.serving.host, port=cfg.serving.port, log_level="warning")
    )

    governor.start()
    forge.start()
    for p in pipelines.values():
        await p.run()

    zones_qs = cfg.zones[0].id if cfg.zones else "main"
    print(f"\n  EGREGORE — {cfg.party.name}")
    print(f"  join:     http://<this-host>:{cfg.serving.port}/?zone={zones_qs}")
    print(f"  status:   http://<this-host>:{cfg.serving.port}/api/status")
    print(f"  password: {password if password else '(auth disabled — trusted LAN)'}")
    mode = ("LOCAL — nothing derived from speech leaves this machine"
            if cfg.budget.total_usd == 0 else
            f"cloud-capable, hard ceiling ${cfg.budget.total_usd}")
    print(f"  privacy:  {mode}\n")

    try:
        await server.serve()
    finally:
        for p in pipelines.values():
            await p.close()
        await forge.close()
        if not cfg.privacy.export_dream:
            log.info("export_dream is false; run `egregore wipe` to delete %d clips",
                     len(store.all()))


__all__ = ["run_party", "build_ladder", "cost_per_clip", "ZonePipeline"]
