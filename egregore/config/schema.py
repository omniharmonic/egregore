"""Party configuration schema (Architecture §4). One YAML file defines a party."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class PartyConfig(BaseModel):
    name: str = "Untitled"
    duration_hours: float = Field(4.0, gt=0, le=24 * 14)
    timezone: str = "UTC"


class AestheticConfig(BaseModel):
    grammar: str = (
        "Abstract, symbolic, non-representational. Deep saturated color, "
        "organic forms dissolving into geometric ones. Movement slow and "
        "liquid. Suggest meaning without depicting it."
    )
    drift: float = Field(0.4, ge=0.0, le=1.0)
    reference_images: list[str] = Field(default_factory=list)
    # How far from depiction to push: 1 renders the themes as pure abstract
    # imagery, 0 asks for them as recognisable subjects. Only affects how the
    # already-abstracted motifs are drawn — the validator still runs, so a
    # literal picture is not a literal prompt.
    abstraction: float = Field(1.0, ge=0.0, le=1.0)
    # How much the room's sound shapes the palette. At 1 a quiet room asks
    # for a dark, low-frequency palette and a loud one for bright; at 0.5
    # the palette bias is dropped and only energy and rhythm remain; at 0 the
    # room is not mentioned. Every prompt in the first soak came out dark
    # because a loopback rig is a quiet room.
    room_bias: float = Field(1.0, ge=0.0, le=1.0)



class WeaverLLMConfig(BaseModel):
    """Local LLM serving the Weaver's abstraction stage.

    Any OpenAI-compatible endpoint works: llama.cpp server, Ollama
    (http://localhost:11434/v1), vLLM. When unset (or unreachable at
    startup), the deterministic heuristic abstractor runs instead —
    the pipeline never depends on an LLM being present.
    """

    base_url: str | None = None  # e.g. "http://localhost:11434/v1"
    model: str = "qwen3:14b"
    api_key_env: str = "EGREGORE_LLM_API_KEY"  # most local servers need none


class SelectionConfig(BaseModel):
    """How the next theme is chosen from everything said since the last clip.

    Weights are normalised at scoring time; they need not sum to one. All
    fields are live-settable, per zone with a party default.
    """

    salience: float = Field(0.5, ge=0.0, le=1.0)   # what the room dwelt on
    novelty: float = Field(0.3, ge=0.0, le=1.0)    # distance from what was just shown
    recency: float = Field(0.2, ge=0.0, le=1.0)    # how fresh
    segment_gap_s: float = Field(6.0, ge=1.0, le=60.0)   # a pause this long ends a thought
    max_candidates: int = Field(6, ge=1, le=12)
    recency_tau_s: float | None = Field(None, ge=5.0)     # None = the last render's duration
    # How far back a thought may come from. What was said during the last
    # render is what the next clip should be about; the whole ring buffer is
    # six minutes, and in the first soak a thought from four minutes earlier
    # won. None = twice the last render, never under 90s. Older material is
    # used only if nothing newer exists.
    lookback_s: float | None = Field(None, ge=10.0)

    @model_validator(mode="after")
    def _some_weight(self) -> SelectionConfig:
        if self.salience + self.novelty + self.recency <= 0:
            raise ValueError("at least one selection weight must be above zero")
        return self


class WeaverConfig(BaseModel):
    engine: Literal["auto", "llm", "heuristic"] = "auto"  # auto = llm if configured
    llm: WeaverLLMConfig = Field(default_factory=WeaverLLMConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    # How long a room may be silent before a clip is rendered from mood and
    # memory alone. Below this the free lane covers the screen: a mood-only
    # prompt is the right thing for a long lull, not for the first two
    # minutes of a party while people are still arriving.
    fallback_after_s: float = Field(120.0, ge=0.0, le=3600.0)


class GenerationConfig(BaseModel):
    # "procedural" is the zero-cost ffmpeg renderer — a real backend, not
    # just a test double ("mock" is kept as an alias). "local" is diffusion
    # via ComfyUI/LTX-2 on operator hardware. Local paths are first-class
    # peers of the cloud, not fallbacks.
    backend: Literal["veo", "fal", "local", "procedural", "mock", "auto"] = "auto"
    model: str = "veo-3.1-lite"
    resolution: str = "1080p"
    aspect_ratio: str = "16:9"
    clip_duration_s: int = Field(8, ge=2, le=8)
    generate_audio: bool = False  # always false; validated below
    fallback: Literal["fal", "local", "procedural", "mock", "none"] = "procedural"
    comfyui_url: str = "http://127.0.0.1:8188"  # local diffusion server (ComfyUI/LTX-2)
    local_model: str = "ltx-2"
    # How hard the local GPU works per clip. These belong in config rather
    # than in the workflow file because they are properties of the machine,
    # not of the graph: the same graph should run on a laptop and on a DGX at
    # settings appropriate to each. None means "leave whatever the graph
    # already specifies alone", so an operator's own graph is never
    # second-guessed.
    # How long a free procedural fill runs. Separate from clip_duration_s,
    # which is sized for what the paid backend can render in time: a fill
    # costs nothing, and a composition worth lingering on should be long.
    fill_duration_s: int = Field(12, ge=2, le=16)
    local_steps: int | None = Field(None, ge=1, le=100)
    local_resolution: str | None = None  # e.g. "512x320"

    @field_validator("local_resolution")
    @classmethod
    def _wh(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            w, h = (int(part) for part in v.lower().split("x"))
        except ValueError:
            raise ValueError('local_resolution must look like "512x320"') from None
        # Latent-space models work in multiples of 32; a size that is not
        # would be silently rounded by the sampler, so the operator would be
        # tuning a number that is not the one being used.
        if w % 32 or h % 32:
            raise ValueError("local_resolution width and height must be multiples of 32")
        if not (128 <= w <= 4096 and 128 <= h <= 4096):
            raise ValueError("local_resolution must be between 128 and 4096 per side")
        return f"{w}x{h}"
    # fal.ai fronts many video models behind one queue protocol, so the model
    # is a catalogue key (egregore.forge.fal.FAL_MODELS) rather than a second
    # backend. Swapping vendors is a config edit, not a code change.
    fal_model: str = "minimax-h3-max"

    @field_validator("generate_audio")
    @classmethod
    def _no_audio(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "generate_audio must be false — the room has its own sound (V-3)"
            )
        return v


class SpendCurvePoint(BaseModel):
    at: str  # "0%".."100%"
    rate: float = Field(gt=0)

    @property
    def frac(self) -> float:
        return float(self.at.rstrip("%")) / 100.0


class BudgetConfig(BaseModel):
    total_usd: Decimal = Field(Decimal("0"), ge=0)
    spend_curve: list[SpendCurvePoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _curve_endpoints(self) -> BudgetConfig:
        if self.spend_curve:
            fracs = [p.frac for p in self.spend_curve]
            if fracs != sorted(fracs):
                raise ValueError("spend_curve points must be ordered by 'at'")
        return self


class ContinuityConfig(BaseModel):
    default_mode: Literal["mosaic", "continuity"] = "mosaic"
    loop_half_life_min: float = Field(45.0, gt=0)
    loop_floor_weight: float = Field(0.15, ge=0, le=1)
    active_pool_max: int = Field(200, gt=0)
    max_chain_length: int = Field(20, ge=1)  # Phase 0-validated provider ceiling
    # How zones relate to each other. "independent": each zone hears only its
    # own room and renders its own loop. "commons": every microphone in the
    # party feeds one transcript pool, but each zone still renders its own
    # loop, so rooms look different while dreaming the same conversation.
    # "mirror": one pool, one loop, every screen showing it at its own phase
    # offset — one generation stream no matter how many zones exist.
    topology: Literal["independent", "commons", "mirror"] = "independent"


class AsrConfig(BaseModel):
    engine: Literal["parakeet", "faster-whisper", "fixture"] = "fixture"
    language: str = "en"


class MicConfig(BaseModel):
    type: Literal["usb", "network", "fixture"] = "fixture"
    device: str | None = None
    host: str | None = None
    fixture_path: str | None = None  # demo mode: scripted conversation file


class ZoneConfig(BaseModel):
    id: str
    mic: MicConfig = Field(default_factory=MicConfig)
    lens_stack: list[str] = Field(default_factory=lambda: ["flow", "feedback", "bloom"])
    continuity_mode: Literal["mosaic", "continuity"] | None = None  # None = party default
    # Pacing. Below 1 the motion is languid and each clip holds the screen
    # longer; together with a generous crossfade this is most of the
    # difference between a loop that pulses and one that flickers past.
    playback_rate: float = Field(1.0, ge=0.25, le=2.0)
    crossfade_s: float | None = Field(None, gt=0.0, le=12.0)  # None = manifest default
    # Linger: the least wall time a clip holds the screen. A clip shorter
    # than this dissolves into itself rather than cutting to the next, so a
    # composition can be looked at. 0 = the clip's own length.
    hold_s: float = Field(0.0, ge=0.0, le=300.0)
    selection: SelectionConfig | None = None  # None = weaver.selection
    screens: list[str] = Field(default_factory=list)


class ScreenConfig(BaseModel):
    id: str
    lens_stack: list[str] | None = None  # None inherits zone default
    loop_phase_offset: float = Field(0.0, ge=0.0, le=1.0)
    audio_source: Literal["zone", "local_mic"] = "zone"


class PrivacyConfig(BaseModel):
    ring_buffer_minutes: float = Field(5.0, gt=0, le=10)
    ring_buffer_max_bytes: int = Field(8192, gt=0)
    export_dream: bool = False
    signage_required: bool = True


class ServingConfig(BaseModel):
    bind: str = "0.0.0.0:8420"
    public_tunnel: bool = False
    password_env: str = "EGREGORE_PARTY_PASSWORD"

    @property
    def host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])


class EgregoreConfig(BaseModel):
    party: PartyConfig = Field(default_factory=PartyConfig)
    aesthetic: AestheticConfig = Field(default_factory=AestheticConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    continuity: ContinuityConfig = Field(default_factory=ContinuityConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    weaver: WeaverConfig = Field(default_factory=WeaverConfig)
    zones: list[ZoneConfig] = Field(default_factory=lambda: [ZoneConfig(id="main")])
    screens: list[ScreenConfig] = Field(default_factory=list)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    serving: ServingConfig = Field(default_factory=ServingConfig)
    clip_store_dir: str = "var/clips"
    demo_time_scale: float = Field(1.0, gt=0)  # >1 speeds up demo cadence

    @model_validator(mode="after")
    def _zone_ids_unique(self) -> EgregoreConfig:
        ids = [z.id for z in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("zone ids must be unique")
        screen_ids = [s.id for s in self.screens]
        if len(screen_ids) != len(set(screen_ids)):
            raise ValueError("screen ids must be unique")
        return self

    def zone_mode(self, zone_id: str) -> Literal["mosaic", "continuity"]:
        for z in self.zones:
            if z.id == zone_id and z.continuity_mode is not None:
                return z.continuity_mode
        return self.continuity.default_mode

    def zone_selection(self, zone_id: str) -> SelectionConfig:
        for z in self.zones:
            if z.id == zone_id and z.selection is not None:
                return z.selection
        return self.weaver.selection


def load_config(path: str | Path) -> EgregoreConfig:
    """Load and validate a party YAML. Raises with a helpful message on error."""
    raw = yaml.safe_load(Path(path).read_text())
    return EgregoreConfig.model_validate(raw or {})
