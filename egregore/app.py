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
import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from egregore.conductor import ConductorState, create_app
from egregore.config import store as config_store
from egregore.config.schema import EgregoreConfig, ZoneConfig
from egregore.forge import (
    ClipStore,
    ComfyUIBackend,
    FalBackend,
    Forge,
    MockBackend,
    VeoBackend,
)
from egregore.governor import Governor
from egregore.listener import FixtureSource, MoodIntegrator, ZoneEvents
from egregore.loom import ZoneLoom
from egregore.scribe import RingBuffer, install_privacy_excepthook, make_transcriber
from egregore.types import ClipRef, FeatureFrame, ThemeObject, VideoBackend
from egregore.weaver import Weaver, build_abstractor, synthesize_prompt

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
    want_fal = choice in ("fal", "auto") or cfg.generation.fallback == "fal"
    if want_fal and cfg.budget.total_usd > 0 and os.environ.get("FAL_KEY"):
        rungs.append(
            FalBackend(
                store,
                model=cfg.generation.fal_model,
                resolution=cfg.generation.resolution,
                aspect_ratio=cfg.generation.aspect_ratio,
            )
        )
    elif want_fal and cfg.budget.total_usd > 0:
        log.warning("fal backend requested but FAL_KEY is not set; skipping")
    if choice in ("local", "auto") or cfg.generation.fallback == "local":
        rungs.append(
            ComfyUIBackend(
                store,
                base_url=cfg.generation.comfyui_url,
                workflow=_comfy_workflow(),
            )
        )
    # The procedural renderer ("mock") is a real zero-cost backend, always last.
    rungs.append(
        MockBackend(
            store,
            name="procedural",
            codec=os.environ.get("EGREGORE_PROCEDURAL_CODEC", "h264"),
        )
    )
    return rungs


#: Operator-supplied ComfyUI graph in API format. ``ComfyUIBackend``'s built-in
#: default is a plausible LTX-2 graph, not a contract (see forge/local.py), and
#: every real install has its own node versions and checkpoint filenames — so
#: prefer a graph exported from the actual box when one is present.
_COMFY_WORKFLOW_ENV = "EGREGORE_COMFY_WORKFLOW"
_DEFAULT_COMFY_WORKFLOW = Path(__file__).resolve().parent.parent / "presets" / "comfyui"


def _comfy_workflow() -> dict | None:
    """Load the operator's ComfyUI graph, or ``None`` to use the built-in default.

    Keys beginning with ``_`` are stripped: they carry human notes, and ComfyUI
    would otherwise reject them as nodes with no ``class_type``.
    """
    raw = os.environ.get(_COMFY_WORKFLOW_ENV)
    path = Path(raw) if raw else _DEFAULT_COMFY_WORKFLOW / "ltxv-2b-gguf.json"
    if not path.is_file():
        if raw:
            log.warning("%s points at %s, which does not exist; using the built-in graph",
                        _COMFY_WORKFLOW_ENV, path)
        return None
    try:
        graph = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("could not read ComfyUI graph %s (%s); using the built-in graph",
                    path, type(exc).__name__)
        return None
    log.info("comfyui graph loaded from %s", path)
    return {k: v for k, v in graph.items() if not k.startswith("_")}


#: Force a cadence floor in seconds, overriding what the ladder reports. Set to
#: 0 to disable throughput pacing entirely and go back to pure budget cadence.
_PACING_ENV = "EGREGORE_MIN_CLIP_INTERVAL_S"

#: Pending clips per zone above which the loop stops asking for more.
_MAX_QUEUE_DEPTH = int(os.environ.get("EGREGORE_MAX_QUEUE_DEPTH", "3"))


def _throughput_floor(
    cfg: EgregoreConfig, ladder: list[VideoBackend], live: LiveSettings | None = None
) -> Callable[[], float] | None:
    """A probe the Governor calls to learn how fast the hardware can go.

    The same party config has to work on a datacentre GPU, a laptop, and a
    cloud API, and those differ by two orders of magnitude in render time. So
    the cadence floor is not a constant in a preset — it is read back from the
    ladder's first (preferred) rung, which updates it as real timings arrive.
    An operator who wants a fixed cadence sets ``EGREGORE_MIN_CLIP_INTERVAL_S``.
    """
    override = os.environ.get(_PACING_ENV)
    if override is not None:
        try:
            fixed = float(override)
        except ValueError:
            log.warning("%s=%r is not a number; ignoring", _PACING_ENV, override)
        else:
            log.info("cadence floor pinned to %.1fs by %s", fixed, _PACING_ENV)
            return (lambda: fixed) if fixed > 0 else None
    if not ladder:
        return None
    preferred, tier = ladder[0], cfg.generation.model

    def probe() -> float:
        # An operator-set floor wins over what the backend reports, so a
        # cadence can be pinned from the settings page without a restart.
        if live is not None and live.cadence_floor_s:
            return live.cadence_floor_s
        try:
            return preferred.estimated_latency(tier).total_seconds()
        except Exception:
            return 0.0

    return probe


def cost_per_clip(cfg: EgregoreConfig, ladder: list[VideoBackend]) -> Decimal:
    """Expected cadence cost: the *preferred* metered rung's price, else 0.

    This feeds the cadence formula, not the ceiling — reservations always use
    each backend's own ``max_plausible_cost``. It reads the first metered rung
    in ladder order rather than any Veo rung anywhere, so a fal-first ladder
    paces on fal's price instead of a cloud price it will never pay.
    """
    for backend in ladder:
        if isinstance(backend, FalBackend):
            model = backend.catalogue.get(cfg.generation.fal_model, backend.model)
            per_sec = model.price_per_second.get(
                backend.resolution, model.worst_price_per_second
            )
            return per_sec * cfg.generation.clip_duration_s
        if isinstance(backend, VeoBackend):
            per_sec = _CLOUD_PER_SEC.get(cfg.generation.model, Decimal("0.20"))
            return per_sec * cfg.generation.clip_duration_s
    return Decimal("0")


@dataclass
class LiveSettings:
    """The subset of configuration a running party re-reads each cycle.

    Everything here is read per generation, so changing it is an assignment
    rather than a reconstruction. Anything that would rebuild the backend
    ladder, or move a ceiling that reservations are already held against,
    belongs in ``store.RESTART_KEYS`` instead — see the configuration spec.
    """

    clip_duration_s: int
    resolution: str
    drift: float
    cadence_floor_s: float | None = None

    @classmethod
    def from_config(cls, cfg: EgregoreConfig) -> LiveSettings:
        return cls(
            clip_duration_s=cfg.generation.clip_duration_s,
            resolution=cfg.generation.resolution,
            drift=cfg.aesthetic.drift,
        )

    def apply(self, overrides: dict) -> list[str]:
        """Apply only the live keys present in ``overrides``; return what changed.

        Restart-only keys in the same payload are ignored here on purpose:
        the endpoint has already persisted them for the next run, and acting
        on them now is exactly what the live/restart split exists to prevent.
        """
        changed: list[str] = []
        gen = overrides.get("generation") or {}
        if "clip_duration_s" in gen:
            self.clip_duration_s = int(gen["clip_duration_s"])
            changed.append("generation.clip_duration_s")
        if "resolution" in gen:
            self.resolution = str(gen["resolution"])
            changed.append("generation.resolution")
        aes = overrides.get("aesthetic") or {}
        if "drift" in aes:
            self.drift = float(aes["drift"])
            changed.append("aesthetic.drift")
        if "cadence_floor_s" in overrides:
            raw = overrides["cadence_floor_s"]
            self.cadence_floor_s = float(raw) if raw else None
            changed.append("cadence_floor_s")
        return changed


class PartyBus:
    """Party-wide shared state: the operator freeze flag (R-7) and the
    cross-zone thematic pool that powers zone-to-zone bleed (L-7)."""

    def __init__(self) -> None:
        self.frozen = False
        self._themes: list[tuple[str, ThemeObject]] = []  # (zone, theme)

    def share_theme(self, zone: str, theme: ThemeObject) -> None:
        self._themes.append((zone, theme))
        del self._themes[:-30]

    def borrow_theme(self, for_zone: str) -> ThemeObject | None:
        """Most recent validated theme from any *other* zone."""
        for zone, theme in reversed(self._themes):
            if zone != for_zone:
                return theme
        return None


class ZonePipeline:
    """Everything one zone owns. Construction wires; run() animates."""

    def __init__(self, zcfg: ZoneConfig, cfg: EgregoreConfig, *, forge: Forge,
                 governor: Governor, state: ConductorState,
                 bus: PartyBus | None = None,
                 live: LiveSettings | None = None,
                 ring: RingBuffer | None = None,
                 generates: bool = True) -> None:
        self.bus = bus or PartyBus()
        self.cfg = cfg
        # Read per cycle, so a settings change lands on the next clip.
        self.live = live if live is not None else LiveSettings.from_config(cfg)
        self.zcfg = zcfg
        self.zone = zcfg.id
        self.forge = forge
        self.governor = governor
        self.state = state
        self.muted = False
        #: Set when this zone's audio comes from enrolled browsers.
        self.network_source = None
        #: False for a follower zone under the "mirror" topology. It still
        #: listens and contributes transcripts; it just does not commission
        #: video of its own, which is what makes mirror one stream for a
        #: whole venue rather than one per room.
        self.generates = generates

        # Under the "commons" topology every zone is handed the same buffer,
        # so the whole party is one conversation. The pipeline neither knows
        # nor cares which case it is in.
        self.shares_ring = ring is not None
        self.ring = ring if ring is not None else RingBuffer.from_config(
            self.zone, cfg.privacy
        )
        self.weaver = Weaver(build_abstractor(cfg.weaver))
        self.mood = MoodIntegrator()
        self.loom = ZoneLoom.from_config(
            self.zone, cfg.zone_mode(self.zone), cfg.continuity
        )
        self._frame_n = 0
        self.bleeds = 0
        self.throttled = 0
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
        if mic.type == "network":
            # Browsers enrolled as transmitters feed this over /ws/ingest. It
            # owns no audio device, so unlike a usb mic it cannot fail to open
            # — a zone with no phones in it yet is simply a quiet zone.
            from egregore.listener import NetworkSource

            try:
                self._transcriber = make_transcriber(cfg.asr.engine, cfg.asr.language)
            except (RuntimeError, ValueError) as e:
                log.warning(
                    "zone %s: no transcriber (%s); network audio will drive "
                    "features only", self.zone, e,
                )
            self.network_source = NetworkSource(events, zone=self.zone)
            return self.network_source
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
        if not self.shares_ring:
            await self.ring.start()
        if self._source is not None:
            self._tasks.append(asyncio.create_task(self._source.run()))
        self._tasks.append(asyncio.create_task(self._generation_loop()))

    async def _generation_loop(self) -> None:
        cfg = self.cfg
        if not self.generates:
            log.info("zone %s: mirroring another zone; not generating", self.zone)
            return
        while True:
            await asyncio.sleep(1.0)
            try:
                if self.bus.frozen:
                    continue  # operator freeze: loop keeps playing, nothing new
                if self.forge.queue_depth(self.zone) >= _MAX_QUEUE_DEPTH:
                    # Backpressure. The cadence floor should already keep us at
                    # the backend's pace, but a backend that suddenly slows (a
                    # bigger model, a busy GPU, a degraded API) would otherwise
                    # build a backlog of prompts describing a room that has
                    # since moved on. Stale imagery is worse than less imagery.
                    self.throttled += 1
                    continue
                if not self.governor.should_generate(self.zone):
                    continue
                plan = self.loom.plan_next()
                window = self.ring.snapshot()
                borrowed: ThemeObject | None = None
                if len(window.split()) < self.weaver.min_window_tokens:
                    # Zone-to-zone bleed (L-7): a quiet or dead zone dreams
                    # on a neighbouring zone's most recent validated theme.
                    borrowed = self.bus.borrow_theme(self.zone)
                if borrowed is not None:
                    prompt = synthesize_prompt(
                        borrowed,
                        cfg.aesthetic.grammar,
                        self.loom.continuity_context(),
                        self.live.drift,
                        self.mood.state(),
                    )
                    self.bleeds += 1
                    self.governor.record_generation(self.zone)
                    await self.forge.request(
                        zone=self.zone,
                        prompt=prompt,
                        duration_s=self.live.clip_duration_s,
                        tier=cfg.generation.model,
                        theme_hint=borrowed,
                        seed_image=plan.seed_image,
                        extend_from=plan.use_extend,
                    )
                    continue
                result = await self.weaver.weave(
                    window,
                    grammar=cfg.aesthetic.grammar,
                    drift=self.live.drift,
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
                    duration_s=self.live.clip_duration_s,
                    tier=cfg.generation.model,
                    theme_hint=result.theme,
                    seed_image=plan.seed_image,
                    extend_from=plan.use_extend,
                )
                if result.theme is not None and not result.fallback:
                    self.mood.absorb_theme(result.theme)
                    self.loom.remember_theme(result.theme)
                    self.bus.share_theme(self.zone, result.theme)
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
        if not self.shares_ring:
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
            "bleeds": self.bleeds,
            "throttled": self.throttled,
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


def make_control_handler(
    bus: PartyBus, pipelines: dict[str, ZonePipeline], state: ConductorState
):
    """Operator controls: freeze (R-7), per-zone mute (P-6), live mode
    switch (C-4). Raises ValueError on bad input; the route 400s it."""

    async def control_handler(action: str, payload: dict) -> dict:
        if action == "freeze":
            bus.frozen = bool(payload.get("on", True))
            log.warning("operator control: freeze=%s", bus.frozen)
            return {"frozen": bus.frozen}
        zone = payload.get("zone")
        pipe = pipelines.get(zone or "")
        if pipe is None:
            raise ValueError(f"unknown zone {zone!r}")
        if action == "mute":
            pipe.set_muted(bool(payload.get("on", True)))
            return {"zone": zone, "muted": pipe.muted}
        if action == "mode":
            mode = payload.get("mode")
            if mode not in ("mosaic", "continuity"):
                raise ValueError(f"mode must be mosaic|continuity, got {mode!r}")
            pipe.loom.set_mode(mode)
            state.set_manifest(zone, pipe.loom.manifest())
            return {"zone": zone, "mode": mode}
        raise ValueError(f"unknown action {action!r}")

    return control_handler


async def run_party(cfg: EgregoreConfig) -> None:
    install_privacy_excepthook()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    # Secrets first, so a key saved by `egregore setup` is visible to
    # build_ladder; then the settings overlay, so the ladder is built from
    # what the operator last chose rather than only from the preset.
    config_store.load_env_file()
    overrides = config_store.load_settings()
    if overrides:
        try:
            cfg = config_store.apply_overlay(cfg, overrides)
            log.info("settings overlay applied from %s", config_store.settings_path())
        except (ValueError, TypeError) as exc:
            log.warning("ignoring invalid settings overlay (%s); using the preset", exc)
    live = LiveSettings.from_config(cfg)

    store = ClipStore(Path(cfg.clip_store_dir))
    ladder = build_ladder(cfg, store)
    governor = Governor.from_config(
        cfg,
        cost_per_clip=cost_per_clip(cfg, ladder),
        min_interval_s=60.0,
        throughput_floor_s=_throughput_floor(cfg, ladder, live),
    )

    # Under "mirror" exactly one zone commissions video and every screen
    # plays it; the others still listen. None for the other topologies.
    mirror_zone = (
        cfg.zones[0].id
        if cfg.continuity.topology == "mirror" and cfg.zones else None
    )

    # One buffer for the whole party under "commons"; None means each zone
    # keeps its own, which is the independent and mirror cases.
    shared_ring = (
        RingBuffer.from_config("party", cfg.privacy)
        if cfg.continuity.topology == "commons" else None
    )

    pipelines: dict[str, ZonePipeline] = {}
    bus = PartyBus()

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

    health_cache: dict = {"at": 0.0, "rows": []}

    async def backend_rows() -> list[dict]:
        # Backend health for the dashboard, cached so a 2s status poll
        # doesn't hammer remote health endpoints.
        import time as _time

        now = _time.monotonic()
        if now - health_cache["at"] > 15.0:
            rows = []
            for b in ladder:
                try:
                    h = await b.health()
                    rows.append({"name": b.name, "state": h.status.value})
                except Exception:
                    rows.append({"name": b.name, "state": "down"})
            health_cache["rows"] = rows
            health_cache["at"] = now
        return health_cache["rows"]

    async def status_provider() -> dict:
        return {
            "party": cfg.party.name,
            "frozen": bus.frozen,
            "governor": governor.status(),
            "zones": {z: p.status() for z, p in pipelines.items()},
            "backends": await backend_rows(),
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
    state.control_handler = make_control_handler(bus, pipelines, state)

    password = os.environ.get(cfg.serving.password_env) or None
    if password is None and cfg.serving.public_tunnel:
        password = secrets.token_urlsafe(9)

    def _apply_settings(payload: dict) -> dict:
        changed = live.apply(payload)
        log.info("live settings changed: %s", ", ".join(changed) or "nothing")
        return {"applied": changed}

    async def _ingest(
        zone: str, node_id: str, pcm: bytes, sample_rate: int
    ) -> float | None:
        pipe = pipelines.get(zone)
        if pipe is None or pipe.network_source is None:
            return None
        try:
            return await pipe.network_source.feed(node_id, pcm, sample_rate)
        except ValueError as exc:      # a malformed frame is one node's bug
            log.warning("zone %s: bad ingest frame from a node (%s)", zone, exc)
            return None

    state.ingest_handler = _ingest
    state.settings_handler = _apply_settings
    state.effective_config = cfg.model_dump(mode="json")

    app = create_app(state, lens_dir=_LENS_DIR, password=password)

    for z in cfg.zones:
        pipelines[z.id] = ZonePipeline(
            z, cfg, forge=forge, governor=governor, state=state, bus=bus,
            live=live, ring=shared_ring,
            generates=(mirror_zone is None or z.id == mirror_zone)
        )

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.serving.host, port=cfg.serving.port, log_level="warning")
    )

    if mirror_zone is not None:
        state.mirror_zone = mirror_zone
        log.info("topology mirror: every screen follows zone %s", mirror_zone)
    if shared_ring is not None:
        await shared_ring.start()
        log.info("topology commons: all zones share one transcript pool")

    governor.start()
    forge.start()
    for p in pipelines.values():
        await p.run()

    from egregore.banner import print_banner

    print_banner(cfg, password=password, backends=[b.name for b in ladder])

    try:
        await server.serve()
    finally:
        for p in pipelines.values():
            await p.close()
        if shared_ring is not None:
            await shared_ring.close()   # zeroes the shared buffer exactly once
        await forge.close()
        if not cfg.privacy.export_dream:
            log.info("export_dream is false; run `egregore wipe` to delete %d clips",
                     len(store.all()))


__all__ = ["run_party", "build_ladder", "cost_per_clip", "ZonePipeline"]
