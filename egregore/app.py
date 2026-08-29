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
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from pathlib import Path

from egregore.conductor import ConductorState, create_app
from egregore.config import store as config_store
from egregore.config.schema import (
    LOCAL_QUALITY,
    EgregoreConfig,
    SelectionConfig,
    ZoneConfig,
    resolve_local_effort,
)
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
from egregore.weaver import Weaver, Weights, build_abstractor, select, synthesize_prompt

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
                seed_workflow=_comfy_workflow("ltxv-2b-seeded.json",
                                              env="EGREGORE_COMFY_SEED_WORKFLOW"),
                steps=resolve_local_effort(cfg.generation)[0],
                resolution=resolve_local_effort(cfg.generation)[1],
                stretch=cfg.generation.local_stretch,
                boomerang=cfg.generation.local_boomerang,
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


def _comfy_workflow(
    name: str = "ltxv-2b-gguf.json", env: str = _COMFY_WORKFLOW_ENV
) -> dict | None:
    """Load the operator's ComfyUI graph, or ``None`` to use the built-in default.

    Keys beginning with ``_`` are stripped: they carry human notes, and ComfyUI
    would otherwise reject them as nodes with no ``class_type``.
    """
    raw = os.environ.get(env)
    path = Path(raw) if raw else _DEFAULT_COMFY_WORKFLOW / name
    if not path.is_file():
        if raw:
            log.warning("%s points at %s, which does not exist; using the built-in graph",
                        env, path)
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

#: A transcription shorter than this is treated as noise rather than speech.
#: Overridable because a quiet gallery and a loud party want different floors.
_MIN_TRANSCRIPT_WORDS = int(os.environ.get("EGREGORE_MIN_TRANSCRIPT_WORDS", "3"))

#: Pending clips per zone above which the loop stops asking for more.


def _operator_floor(live: LiveSettings) -> Callable[[], float] | None:
    """A minimum spacing the operator may pin, from the environment or the
    settings page. The Governor no longer paces on measured render latency:
    the loop asks for a clip only when the previous one has finished, which
    is what bounds lag at one render. This floor is for the other case — an
    operator who wants a *slower* room than the hardware would give.
    """
    override = os.environ.get(_PACING_ENV)
    fixed: float | None = None
    if override is not None:
        try:
            fixed = float(override)
        except ValueError:
            log.warning("%s=%r is not a number; ignoring", _PACING_ENV, override)
        else:
            log.info("cadence floor pinned to %.1fs by %s", fixed, _PACING_ENV)

    def probe() -> float:
        if live.cadence_floor_s:
            return live.cadence_floor_s
        return fixed if fixed and fixed > 0 else 0.0

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
    #: The prompt preamble every generation is built on — the single
    #: strongest lever over how a party looks, and therefore the one most
    #: worth being able to change without restarting it.
    grammar: str = ""
    #: 1 = pure abstraction, 0 = recognisable depiction.
    abstraction: float = 1.0
    #: How much the room's sound shapes the palette (0 = not at all).
    room_bias: float = 1.0
    cadence_floor_s: float | None = None
    #: Longest the loop may go without a new clip while the pool is still
    #: thin. Filling is not a steady drip: the free renderer exists to get a
    #: loop off the ground and to cover a gap, not to become the material.
    fill_interval_s: float | None = 45.0
    #: Stop filling once the pool holds this many clips. Past it the loop has
    #: enough to work with, and every further fill would only dilute the
    #: diffusion clips the party is paying for.
    fill_pool_floor: int = 6
    #: How long a free fill runs — free, so long enough to linger on.
    fill_duration_s: int = 12
    #: Local diffusion effort. Properties of the machine rather than of the
    #: workflow file, so the same graph serves a laptop and a DGX, and so an
    #: operator can trade fidelity for responsiveness while the room is
    #: running — a restart would drop the clip pool and the chain with it.
    #: None on either leaves the graph's own value alone.
    local_quality: str = "balanced"
    local_stretch: int = 2
    local_boomerang: bool = True
    local_steps: int | None = None
    local_resolution: str | None = None
    #: The local rungs these knobs are pushed to. Populated by
    #: :meth:`bind_backends`; empty when no local backend is in the ladder.
    _local_backends: list = dc_field(default_factory=list, repr=False)
    #: Party-default selection knobs and per-zone overrides, as plain dicts
    #: so a live change can touch one field without rebuilding the model.
    selection: dict = dc_field(default_factory=dict)
    selection_by_zone: dict[str, dict] = dc_field(default_factory=dict)
    #: Silence a room may sit in before a clip is rendered from mood alone.
    fallback_after_s: float = 120.0

    def selection_for(self, zone: str) -> SelectionConfig:
        """Zone override on top of the party default, validated."""
        merged = {**self.selection, **self.selection_by_zone.get(zone, {})}
        return SelectionConfig.model_validate(merged)

    def apply_zone_selection(self, zone: str, patch: dict) -> None:
        cur = dict(self.selection_by_zone.get(zone, {}))
        for k, v in patch.items():
            if v is None or v == "":
                cur.pop(k, None)          # clear an override: back to party default
            else:
                cur[k] = v
        SelectionConfig.model_validate({**self.selection, **cur})   # reject before storing
        self.selection_by_zone[zone] = cur

    def bind_backends(self, ladder: list[VideoBackend]) -> None:
        """Remember which rungs the hardware knobs apply to."""
        self._local_backends = [b for b in ladder if isinstance(b, ComfyUIBackend)]
        self._push_hardware()

    def effective_local_effort(self) -> tuple[int, str]:
        """The level's numbers, each overridden by an explicit one if set."""
        steps, size = LOCAL_QUALITY.get(self.local_quality, LOCAL_QUALITY["balanced"])
        return (self.local_steps if self.local_steps is not None else steps,
                self.local_resolution if self.local_resolution is not None else size)

    def _push_hardware(self) -> None:
        steps, size = self.effective_local_effort()
        for backend in self._local_backends:
            backend.steps = steps
            backend.resolution = size
            backend.stretch = self.local_stretch
            backend.boomerang = self.local_boomerang

    @classmethod
    def from_config(cls, cfg: EgregoreConfig) -> LiveSettings:
        return cls(
            clip_duration_s=cfg.generation.clip_duration_s,
            resolution=cfg.generation.resolution,
            drift=cfg.aesthetic.drift,
            grammar=cfg.aesthetic.grammar,
            abstraction=cfg.aesthetic.abstraction,
            room_bias=cfg.aesthetic.room_bias,
            local_quality=cfg.generation.local_quality,
            local_stretch=cfg.generation.local_stretch,
            local_boomerang=cfg.generation.local_boomerang,
            local_steps=cfg.generation.local_steps,
            local_resolution=cfg.generation.local_resolution,
            fallback_after_s=cfg.weaver.fallback_after_s,
            fill_duration_s=cfg.generation.fill_duration_s,
            selection=cfg.weaver.selection.model_dump(),
            selection_by_zone={
                z.id: z.selection.model_dump(exclude_unset=True)
                for z in cfg.zones if z.selection is not None
            },
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
        if "fill_duration_s" in gen:
            self.fill_duration_s = int(gen["fill_duration_s"])
            changed.append("generation.fill_duration_s")
        if "local_stretch" in gen:
            self.local_stretch = max(1, min(4, int(gen["local_stretch"] or 1)))
            changed.append("generation.local_stretch")
        if "local_boomerang" in gen:
            self.local_boomerang = bool(gen["local_boomerang"])
            changed.append("generation.local_boomerang")
        if "local_quality" in gen:
            level = str(gen["local_quality"])
            if level in LOCAL_QUALITY:
                self.local_quality = level
                changed.append("generation.local_quality")
        if "local_steps" in gen:
            raw = gen["local_steps"]
            self.local_steps = int(raw) if raw not in (None, "") else None
            changed.append("generation.local_steps")
        if "local_resolution" in gen:
            raw = gen["local_resolution"]
            self.local_resolution = str(raw) if raw not in (None, "") else None
            changed.append("generation.local_resolution")
        if any(k in gen for k in ("local_quality", "local_steps", "local_resolution",
                                  "local_stretch", "local_boomerang")):
            self._push_hardware()
        weaver_over = overrides.get("weaver") or {}
        if "fallback_after_s" in weaver_over:
            self.fallback_after_s = float(weaver_over["fallback_after_s"] or 0.0)
            changed.append("weaver.fallback_after_s")
        sel = (overrides.get("weaver") or {}).get("selection") or {}
        for k in ("salience", "novelty", "recency", "segment_gap_s",
                  "max_candidates", "recency_tau_s", "lookback_s", "standin_penalty"):
            if k in sel:
                raw = sel[k]
                self.selection[k] = None if raw in (None, "") else raw
                changed.append(f"weaver.selection.{k}")
        if any(c.startswith("weaver.selection.") for c in changed):
            SelectionConfig.model_validate(self.selection)   # a bad blend must not be stored
        aes = overrides.get("aesthetic") or {}
        if "abstraction" in aes:
            self.abstraction = float(aes["abstraction"])
            changed.append("aesthetic.abstraction")
        if "room_bias" in aes:
            self.room_bias = float(aes["room_bias"])
            changed.append("aesthetic.room_bias")
        if "grammar" in aes:
            self.grammar = str(aes["grammar"])
            changed.append("aesthetic.grammar")
        if "drift" in aes:
            self.drift = float(aes["drift"])
            changed.append("aesthetic.drift")
        if "fill_pool_floor" in overrides:
            self.fill_pool_floor = int(overrides["fill_pool_floor"])
            changed.append("fill_pool_floor")
        if "fill_interval_s" in overrides:
            raw = overrides["fill_interval_s"]
            self.fill_interval_s = float(raw) if raw else None
            changed.append("fill_interval_s")
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
        #: Set when this zone opens a local audio device.
        self.mic_source = None
        #: The most recent validated prompt, and when it was made. Operator
        #: visibility only — never a transcript, only the abstraction of one.
        self.last_prompt: str | None = None
        self.last_prompt_at: float = 0.0
        #: When any clip was last asked for, paid or filled.
        self._last_clip_request: float = 0.0
        #: Transcriptions dropped as too short to be speech. Visible to the
        #: operator, because a high count means the room is noisy rather than
        #: the microphone being broken.
        self.discarded_fragments = 0
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
        self.weaver = Weaver(
            build_abstractor(cfg.weaver), stage1_budget_s=cfg.weaver.stage1_budget_s,
            max_slow_calls=cfg.weaver.max_slow_calls,
        )
        self.mood = MoodIntegrator()
        self.loom = ZoneLoom.from_config(
            self.zone, cfg.zone_mode(self.zone), cfg.continuity
        )
        self._frame_n = 0
        self.bleeds = 0
        #: Loop ticks where spacing was satisfied but the worker was busy —
        #: the number that says "the GPU is the bottleneck".
        self.waited_for_slot = 0
        #: How the last clip was chosen. Counts and scores only.
        self.last_selection: dict | None = None
        #: When the winning segment ended, so the landing clip can report
        #: how far behind the room it is.
        self._lag_anchor: float | None = None
        #: Forge.paid_completed at the moment of the last paid request, so
        #: on_clip can tell that clip from a fill that lands first.
        self._paid_done_at_request = 0
        #: Lag of the most recent paid clip to land: last word of the winning
        #: thought to clip on disk. Kept apart from ``last_selection`` because
        #: under pull scheduling the next cycle starts the second a clip
        #: lands and would overwrite the record before anyone read it.
        self.last_lag_s: float | None = None
        #: When this zone last rendered from speech (or started), so a lull
        #: is measured from something real rather than from the epoch.
        self._last_speech_render_at: float = time.monotonic()
        #: Paid cycles skipped because the room had said nothing yet.
        self.held_for_speech = 0
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
                self.mic_source = MicSource(events, device=mic.device)
                return self.mic_source
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
            self._prime()

    def _prime(self) -> None:
        """Work out the themes of closed thoughts while the GPU is busy, so
        the next render slot finds them ready instead of waiting on stage 1."""
        try:
            self.weaver.abstraction = self.live.abstraction
            gap = self.live.selection_for(self.zone).segment_gap_s
            self.weaver.prime(
                self.ring.segments(gap), self.mood.state(),
                now=time.monotonic(), gap_s=gap,
            )
        except Exception:  # noqa: BLE001 - priming is an optimisation, never a failure
            log.exception("zone %s: prime failed", self.zone)

    async def _on_speech_audio(self, pcm: bytes, sample_rate: int) -> None:
        if self.muted:
            return
        text = await self._transcriber.transcribe(pcm, sample_rate)
        if not text:
            return
        if len(text.split()) < _MIN_TRANSCRIPT_WORDS:
            # A room with music in it makes the VAD open on things that are
            # not speech, and a recogniser handed non-speech returns one or
            # two confident-looking words. Those are indistinguishable from
            # real short replies in the buffer, but they carry no theme and
            # they crowd out the utterances that do. Dropping them is the
            # difference between a prompt about the room and a prompt about
            # nothing (measured: 17 "utterances" averaging 2.4 words, with
            # music playing and nobody talking).
            self.discarded_fragments += 1
            return
        self.ring.add(text)
        self._prime()

    def _publish_input_device(self) -> str | None:
        """The device this zone actually opened, once it has one.

        Read here rather than snapshotted at start-up: the stream opens
        asynchronously, so at wiring time the name is always still None —
        which is what the dashboard was showing.
        """
        name = getattr(self.mic_source, "device_name", None)
        if name:
            self.state.input_devices[self.zone] = name
        return name

    # -- output wiring ------------------------------------------------------

    async def on_clip(self, clip: ClipRef) -> None:
        await self.loom.ingest(clip, clip.path, fill=self.forge.landed_as_fill(clip.id))
        self.state.set_manifest(self.zone, self.loom.manifest())
        landed_paid = self.forge.paid_completed(self.zone) > self._paid_done_at_request
        if landed_paid and self._lag_anchor is not None and self.last_selection is not None:
            # Only the clip this selection asked for — a fill landing first
            # would otherwise report a four-second lag on an eighty-second
            # render. Monotonic domain, like the ring's fragment stamps.
            self.last_lag_s = round(time.monotonic() - self._lag_anchor, 1)
            self.last_selection["lag_s"] = self.last_lag_s
            self._lag_anchor = None

    # -- the generation loop ------------------------------------------------

    async def resume(self, clips: list[ClipRef]) -> int:
        """Pick up clips a previous run left in the store.

        Oldest first, so the playlist's half-life weighting and the
        continuity chain both end on the newest clip. Only the newest clip
        pays for a last-frame extraction: the chain seeds from that one.
        """
        mine = sorted((c for c in clips if c.zone == self.zone), key=lambda c: c.created_at)
        for c in mine[:-1]:
            self.loom.playlist.add(c)
        if mine:
            await self.loom.ingest(mine[-1], mine[-1].path)
            self.state.set_manifest(self.zone, self.loom.manifest())
            log.info("zone %s: resumed %d clip(s) from the store", self.zone, len(mine))
        return len(mine)

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
                self._prime()
                spaced = self.governor.should_generate(self.zone)
                busy = (
                    self.forge.in_flight(self.zone) > 0
                    or self.forge.queue_depth(self.zone) > 0
                )
                # Pull, not push. A clip is asked for when the previous one has
                # finished, never before: the prompt is then written from what
                # the room said *during* that render, and lag is one render —
                # not a queue of prompts describing a room that has moved on.
                due = spaced and not busy
                if spaced and busy:
                    self.waited_for_slot += 1
                sel_cfg = self.live.selection_for(self.zone)
                self.weaver.abstraction = self.live.abstraction
                segments = self.ring.segments(sel_cfg.segment_gap_s)
                window_tokens = sum(s.tokens for s in segments)
                borrowed: ThemeObject | None = None
                if window_tokens < self.weaver.min_window_tokens:
                    # Zone-to-zone bleed (L-7): a quiet or dead zone dreams
                    # on a neighbouring zone's most recent validated theme.
                    borrowed = self.bus.borrow_theme(self.zone)
                if due and borrowed is None and window_tokens < self.weaver.min_window_tokens:
                    lull = time.monotonic() - self._last_speech_render_at
                    if lull < self.live.fallback_after_s:
                        # Nothing said yet, or not for long. A mood-only
                        # render is for a real lull; spending a render slot
                        # on it while people are still arriving wastes the
                        # one thing this backend is short of. Demoted to
                        # "not due" so the free lane still covers the screen.
                        self.held_for_speech += 1
                        due = False
                fill = False
                if not due:
                    gap = self.live.fill_interval_s
                    thin = self.loom.playlist.active_size < self.live.fill_pool_floor
                    if gap and thin and (
                        time.monotonic() - self._last_clip_request
                    ) >= gap:
                        # The free lane covers an empty pool at party start
                        # and gaps between renders — only while the pool is
                        # thin, or it buries the diffusion clips under
                        # connective tissue.
                        fill = True
                if not due and not fill:
                    continue
                self._last_clip_request = time.monotonic()
                plan = self.loom.plan_next()
                if borrowed is not None:
                    prompt = synthesize_prompt(
                        borrowed,
                        self.live.grammar,
                        self.loom.continuity_context(),
                        self.live.drift,
                        self.mood.state(),
                        abstraction=self.live.abstraction,
                        room_bias=self.live.room_bias,
                    )
                    self.bleeds += 1
                    self.governor.record_generation(self.zone)
                    await self.forge.request(
                        zone=self.zone,
                        prompt=prompt,
                        duration_s=self.live.fill_duration_s if fill else self.live.clip_duration_s,
                        tier=cfg.generation.model,
                        theme_hint=borrowed,
                        seed_image=plan.seed_image,
                        extend_from=plan.use_extend,
                        free_only=fill,
                    )
                    continue

                theme: ThemeObject | None = None
                prompt = ""
                fallback = False
                if window_tokens >= self.weaver.min_window_tokens:
                    # A fill chooses its theme the same way, but only a paid
                    # cycle writes the record: the lag and the candidate list
                    # describe the clip the room is waiting for.
                    chosen = await self._choose_theme(segments, sel_cfg, record=not fill)
                    if chosen is not None:
                        theme = chosen
                        prompt = synthesize_prompt(
                            theme, self.live.grammar, self.loom.continuity_context(),
                            self.live.drift, self.mood.state(),
                            abstraction=self.live.abstraction,
                            room_bias=self.live.room_bias,
                        )
                        self.weaver.prompts_synthesized += 1
                if theme is None:
                    # Nothing survived per-segment validation, or the window
                    # is thin: the whole-window path, which may purge,
                    # exactly as before.
                    result = await self.weaver.weave(
                        self.ring.snapshot(),
                        grammar=self.live.grammar,
                        drift=self.live.drift,
                        mood=self.mood.state(),
                        continuity=self.loom.continuity_context(),
                        abstraction=self.live.abstraction,
                        room_bias=self.live.room_bias,
                    )
                    if result.purge_requested:
                        self.ring.zero()
                        log.warning("zone %s: cycle skipped, buffer purged", self.zone)
                        continue
                    if result.prompt is None:
                        continue
                    prompt = result.prompt
                    theme = result.theme
                    fallback = result.fallback
                    if not fill:
                        self.last_selection = None
                        self._lag_anchor = None
                # The outbound prompt is the one string this system is
                # willing to send to a third party, so showing it to the
                # operator is strictly safer than what already happens to it.
                # It is also the only way to see, from outside, that the
                # video on screen came from what the room actually said.
                self.last_prompt = prompt
                self.last_prompt_at = time.time()
                if not fill:
                    # A fill must not reset the paid cadence, or the budget
                    # would never be spent at all once filling starts.
                    self.governor.record_generation(self.zone)
                    self._paid_done_at_request = self.forge.paid_completed(self.zone)
                    if not fallback:
                        self._last_speech_render_at = time.monotonic()
                await self.forge.request(
                    zone=self.zone,
                    prompt=prompt,
                    duration_s=self.live.fill_duration_s if fill else self.live.clip_duration_s,
                    tier=cfg.generation.model,
                    theme_hint=theme,
                    seed_image=plan.seed_image,
                    extend_from=plan.use_extend,
                    free_only=fill,
                )
                if theme is not None and not fallback:
                    self.mood.absorb_theme(theme)
                    self.loom.remember_theme(theme)
                    self.bus.share_theme(self.zone, theme)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Content-free by the scrubbing excepthook contract; the
                # loop must survive anything (degradation, not death).
                log.exception("zone %s: generation cycle failed", self.zone)

    async def _choose_theme(
        self, segments, sel_cfg: SelectionConfig, *, record: bool = True
    ) -> ThemeObject | None:
        """Abstract each stretch of speech, score them, keep the best.

        Records ``last_selection`` (counts and scores — motifs are validated
        abstractions, never text) and the lag anchor. Returns None when no
        candidate survived validation, so the caller can fall back to the
        whole-window path.
        """
        now = time.monotonic()
        # What was said during the last render is what the next clip should
        # be about. Older material competes only when nothing newer exists.
        lookback = sel_cfg.lookback_s or max(2.0 * self._last_render_s(), 90.0)
        recent = [s for s in segments if now - s.ended_at <= lookback] or list(segments)
        candidates = await self.weaver.weave_candidates(
            recent, mood=self.mood.state(), max_candidates=sel_cfg.max_candidates,
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            winner = candidates[0]
            scored_out = [{
                "motifs": list(winner.theme.motifs),
                "elemental": list(winner.theme.elemental),
                "salience": 1.0, "novelty": 1.0, "recency": 1.0,
                "score": 1.0, "winner": True,
            }]
            listened = now - winner.started_at
            winner_score = 1.0
        else:
            selection = select(
                candidates,
                memory=self.loom.thematic_memory,
                weights=Weights(sel_cfg.salience, sel_cfg.novelty, sel_cfg.recency),
                now=now,
                tau_s=sel_cfg.recency_tau_s or self._last_render_s(),
                standin_penalty=sel_cfg.standin_penalty,
            )
            winner = selection.winner
            listened = selection.listened_s
            winner_score = selection.scored[0].score
            scored_out = [{
                "motifs": list(sc.candidate.theme.motifs),
                "elemental": list(sc.candidate.theme.elemental),
                "salience": round(sc.salience, 3),
                "novelty": round(sc.novelty, 3),
                "recency": round(sc.recency, 3),
                "score": round(sc.score, 3),
                "winner": sc.candidate is winner,
            } for sc in selection.scored]
        if record:
            self.last_selection = {
                "candidates": len(candidates),
                "winner_score": round(winner_score, 3),
                "listened_s": round(listened, 1),
                # How old the winning thought already was when the render
                # slot opened. Lag on landing is this plus the render, so a
                # quiet room reads as a quiet room, not as a slow pipeline.
                "age_s": round(max(0.0, now - winner.ended_at), 1),
                "lag_s": None,
                "scored": scored_out,
            }
            self._lag_anchor = winner.ended_at
        return winner.theme

    def _last_render_s(self) -> float:
        """The preferred backend's learned render time, for the recency tau."""
        try:
            return self.forge.backends[0].estimated_latency(
                self.cfg.generation.model
            ).total_seconds()
        except Exception:
            return 0.0

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.ring.zero()

    async def close(self) -> None:
        await self.weaver.close()
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
            "last_prompt": self.last_prompt,
            "last_prompt_at": self.last_prompt_at,
            "validator_rejections": self.weaver.rejections,
            "purges": self.weaver.purges_requested,
            "bleeds": self.bleeds,
            "in_flight": self.forge.in_flight(self.zone),
            # Which brain writes the themes. The heuristic has a fixed
            # vocabulary; an LLM is what makes the wall feel like the room.
            "weaver_engine": self.weaver.engine_name,
            "weaver_model": getattr(self.weaver.abstractor, "model", None),
            "lag_s": self.last_lag_s,
            "waited_for_slot": self.waited_for_slot,
            "held_for_speech": self.held_for_speech,
            "last_selection": (
                {k: v for k, v in self.last_selection.items() if k != "scored"}
                if self.last_selection else None
            ),
            "discarded_fragments": self.discarded_fragments,
            "input_device": self._publish_input_device(),
            **self.loom.status(),
        }


def _zone_config_map(cfg: EgregoreConfig) -> dict[str, dict]:
    screens = {s.id: s for s in cfg.screens}
    out: dict[str, dict] = {}
    for z in cfg.zones:
        out[z.id] = {
            "lens_stack": z.lens_stack,
            "lens_params": {},
            "crossfade_s": 2.0,
            "playback_rate": z.playback_rate,
            "hold_s": z.hold_s,
            "crossfade_override": z.crossfade_s,
            "screens": {
                sid: {
                    "lens_stack": screens[sid].lens_stack if sid in screens else None,
                    "lens_params": None,
                    "playback_rate": None,
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


async def run_party(cfg: EgregoreConfig, *, ignore_settings: bool = False) -> None:
    install_privacy_excepthook()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    # Secrets first, so a key saved by `egregore setup` is visible to
    # build_ladder; then the settings overlay, so the ladder is built from
    # what the operator last chose rather than only from the preset.
    config_store.load_env_file()
    overrides = {} if ignore_settings else config_store.load_settings()
    applied_overrides: list[tuple[str, object, object]] = []
    if overrides:
        try:
            merged = config_store.apply_overlay(cfg, overrides)
        except (ValueError, TypeError) as exc:
            log.warning("ignoring invalid settings overlay (%s); using the preset", exc)
        else:
            # Record what the overlay actually changed so the banner can say
            # so. A saved setting quietly overruling the preset someone just
            # typed is the most confusing thing this system can do.
            preset_json = cfg.model_dump(mode="json")
            merged_json = merged.model_dump(mode="json")
            for dotted in config_store.dotted_keys(overrides):
                was = config_store.value_at(preset_json, dotted)
                now = config_store.value_at(merged_json, dotted)
                if was != now:
                    applied_overrides.append((dotted, was, now))
            cfg = merged
            if applied_overrides:
                log.warning(
                    "settings overlay from %s overrode the preset: %s",
                    config_store.settings_path(),
                    "; ".join(f"{k} {w!r} -> {n!r}" for k, w, n in applied_overrides),
                )
    live = LiveSettings.from_config(cfg)

    store = ClipStore(Path(cfg.clip_store_dir))
    ladder = build_ladder(cfg, store)
    live.bind_backends(ladder)
    governor = Governor.from_config(
        cfg,
        cost_per_clip=cost_per_clip(cfg, ladder),
        min_interval_s=60.0,
        throughput_floor_s=_operator_floor(live),
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

    def _apply_zone_settings(zone: str, patch: dict) -> None:
        live.apply_zone_selection(zone, patch)
        log.info("zone %s: selection changed: %s", zone, ", ".join(sorted(patch)))

    state.zone_settings_handler = _apply_zone_settings

    if os.environ.get("EGREGORE_MONITOR") == "1":
        def _monitor() -> dict:
            return {
                "zones": {
                    zone: {
                        "transcript": pipe.ring.snapshot(),
                        "last_prompt": pipe.last_prompt,
                        "last_prompt_at": pipe.last_prompt_at,
                        "fragments": pipe.ring.occupancy()[0],
                        "tokens": pipe.ring.token_count(),
                        "candidates": (pipe.last_selection or {}).get("scored", []),
                        "listened_s": (pipe.last_selection or {}).get("listened_s"),
                    }
                    for zone, pipe in pipelines.items()
                }
            }

        state.monitor_provider = _monitor
        log.warning(
            "EGREGORE_MONITOR=1: live transcripts are readable from this machine "
            "at /api/monitor for as long as this party runs"
        )
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
        await p.resume(store.all())
        await p.run()

    from egregore.banner import print_banner

    print_banner(
        cfg, password=password, backends=[b.name for b in ladder],
        overrides=applied_overrides,
    )

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
