"""Procedural video backend — the demo and CI rendering path.

This is not a stub. `MockBackend` renders real, watchable, abstract MP4s with
ffmpeg `lavfi` sources, parameterised by the `ThemeObject` so that different
themes genuinely look different: palette from elemental words plus valence,
motion speed and colour swing from intensity, and the filtergraph itself
chosen deterministically from a hash of the theme.

Why it matters: the whole demo path (CONTRACTS.md "Demo mode") and every
end-to-end test render through here, and PRD success criterion 5 asks the
imagery to be *genuinely beautiful*, not merely present. A backend that
emitted colour bars would let the rest of the system pass its tests while
the product failed its actual bar.

Privacy: `prompt` is accepted to satisfy the `VideoBackend` protocol and is
then deliberately never read, never logged, and never written to disk. The
only thing that influences the picture is the already-validated, abstract
`ThemeObject`.
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from egregore.types import (
    BackendCapabilities,
    BackendHealth,
    BackendStatus,
    ClipRef,
    ThemeObject,
)

from .store import ClipStore

log = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720
FPS = 24

#: Everything upstream of the final upscale is composited at this size. Video
#: this soft loses nothing to it, and it is what keeps an 8 s clip inside the
#: ~5 s encode budget the queue needs in order to stay ahead.
_WORK_W = 512
_WORK_H = 288

_ALLOWED_DURATIONS = frozenset({2, 4, 6, 8})
_TIERS = frozenset({"mock"})

#: Seconds of held still frame before the dissolve, and the dissolve itself.
_SEED_HOLD_S = 0.5
_SEED_FADE_S = 1.5


# ---------------------------------------------------------------------------
# Palette derivation
# ---------------------------------------------------------------------------

#: Elemental / motif words to a base hue in degrees. Deliberately coarse: the
#: point is that "water" and "fire" are unmistakably different rooms, not that
#: every noun gets its own colour.
_ELEMENT_HUES: dict[str, float] = {
    # water
    "water": 205.0, "ocean": 200.0, "sea": 198.0, "river": 200.0, "rain": 210.0,
    "tide": 196.0, "wave": 202.0, "flood": 206.0, "liquid": 203.0, "depth": 214.0,
    # ice / mist / air
    "ice": 188.0, "frost": 186.0, "mist": 192.0, "fog": 194.0, "cloud": 196.0,
    "air": 195.0, "wind": 190.0, "breath": 193.0, "sky": 208.0, "vapour": 191.0,
    # fire
    "fire": 24.0, "flame": 18.0, "ember": 20.0, "heat": 30.0, "lava": 8.0,
    "spark": 34.0, "burn": 16.0, "forge": 26.0, "furnace": 22.0,
    # sun / gold
    "sun": 44.0, "gold": 46.0, "honey": 42.0, "amber": 40.0, "dawn": 36.0,
    # earth / stone
    "earth": 32.0, "soil": 30.0, "clay": 28.0, "stone": 38.0, "sand": 42.0,
    "dust": 36.0, "rust": 18.0,
    # growth
    "forest": 128.0, "moss": 120.0, "leaf": 118.0, "root": 108.0, "wood": 96.0,
    "green": 130.0, "vine": 124.0, "garden": 122.0, "seed": 110.0, "grow": 126.0,
    # metal
    "metal": 214.0, "steel": 216.0, "iron": 218.0, "silver": 210.0,
    "mercury": 212.0, "machine": 220.0,
    # night / void
    "night": 262.0, "shadow": 268.0, "void": 272.0, "dark": 266.0,
    "smoke": 258.0, "ash": 256.0, "dream": 280.0, "deep": 250.0,
    # blood / rose
    "blood": 352.0, "rose": 344.0, "crimson": 350.0, "wound": 348.0,
}

#: No elemental match: violet. Neutral, saturated, and reads as "abstract"
#: rather than as a depiction of anything.
_DEFAULT_HUE = 276.0

_WORD_RE = re.compile(r"[^a-z]+")


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _hex(hue_deg: float, lightness: float, saturation: float) -> str:
    hue = (hue_deg % 360.0) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, _clamp(lightness, 0.0, 1.0), _clamp(saturation, 0.0, 1.0))
    return f"0x{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


@dataclass(frozen=True)
class Palette:
    """Four ramp stops fed to the `gradients` source, darkest to brightest."""

    c0: str
    c1: str
    c2: str
    c3: str
    hue: float

    def as_args(self) -> str:
        return f"c0={self.c0}:c1={self.c1}:c2={self.c2}:c3={self.c3}"


def base_hue_for(theme: ThemeObject) -> float:
    """First elemental (then motif, then movement/register) word that maps to
    a hue. Falls back to violet."""
    for phrase in (*theme.elemental, *theme.motifs, theme.movement, theme.register):
        for token in _WORD_RE.split(phrase.lower()):
            hue = _ELEMENT_HUES.get(token)
            if hue is not None:
                return hue
    return _DEFAULT_HUE


def palette_for(theme: ThemeObject) -> Palette:
    """Palette from elemental hue + valence (depth) + intensity (spread)."""
    hue = base_hue_for(theme)
    valence = _clamp(theme.valence, 0.0, 1.0)
    intensity = _clamp(theme.intensity, 0.0, 1.0)

    span = 18.0 + 34.0 * intensity  # how far the ramp travels around the wheel
    sat = _clamp(0.46 + 0.30 * intensity, 0.36, 0.86)
    # Dark themes stay deep, light themes open up — but the floor is high
    # enough that a valence-0 theme is still visible on a dim projector.
    lit = 0.15 + 0.28 * valence

    return Palette(
        c0=_hex(hue - span * 0.35, lit * 0.40, sat * 0.90),
        c1=_hex(hue, lit, sat),
        c2=_hex(hue + span * 0.50, lit * 0.72, sat),
        c3=_hex(hue + span, min(0.88, lit * 2.4 + 0.18), sat * 0.70),
        hue=hue,
    )


# ---------------------------------------------------------------------------
# Motion model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Motion:
    gradient_speed: float
    rotate_rate: float
    hue_swing: float
    hue_rate: float
    smear_frames: int
    saturation: float


def motion_for(theme: ThemeObject) -> Motion:
    """Speed, colour swing and temporal smear, all from intensity."""
    intensity = _clamp(theme.intensity, 0.0, 1.0)
    return Motion(
        gradient_speed=round(0.004 + 0.020 * intensity, 5),
        rotate_rate=round(0.012 + 0.055 * intensity, 4),
        hue_swing=round(8.0 + 22.0 * intensity, 2),
        hue_rate=round(0.09 + 0.16 * intensity, 3),
        # Stiller themes smear across more frames, which reads as slower.
        smear_frames=int(round(6 - 3 * intensity)),
        saturation=round(1.25 + 0.25 * intensity, 3),
    )


def theme_digest(theme: ThemeObject) -> int:
    """Stable integer digest of a theme's abstract fields.

    Used only to pick a filtergraph variant, to seed the procedural sources
    and to place the gradient axis, so that the same theme always looks the
    same and two different themes reliably do not. Never stored, never
    logged, never written to disk.
    """
    key = "|".join(
        [
            *sorted(theme.motifs),
            theme.register,
            theme.movement,
            *sorted(theme.elemental),
            f"{theme.valence:.2f}",
            f"{theme.intensity:.2f}",
        ]
    )
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16)


# ---------------------------------------------------------------------------
# Filtergraph construction
# ---------------------------------------------------------------------------

#: Human-readable names for the four looks, in selection order.
VARIANTS = ("liquid", "bloom", "nebula", "organism")

#: Upscale to delivery resolution, then a whisper of grain. The grain is not
#: decoration: these are very smooth deep-colour fields, and without dither
#: they band visibly on a projector at crf 28. It costs ~0.1 s and roughly
#: doubles the bitrate, which is a good trade for a wall-sized image.
_FINISH = f"scale={WIDTH}:{HEIGHT}:flags=bilinear,noise=alls=10:allf=t+u,format=yuv420p"


def variant_for(theme: ThemeObject) -> str:
    return VARIANTS[theme_digest(theme) % len(VARIANTS)]


def _graph(
    palette: Palette, motion: Motion, digest: int, source_len: float
) -> tuple[list[str], str]:
    """Return (lavfi source descriptions, unterminated filter chain).

    The chain ends at working resolution and without an output label, so the
    caller can either finish it straight into `_FINISH` or splice a seed
    dissolve in first — the dissolve is much cheaper here than at 720p.

    Every variant composites two procedural sources: a moving colour field,
    and a structural field that both warps it (`displace`) and lights it
    (`blend`). That two-sources-with-depth shape is what stops the result
    reading as a screensaver gradient.
    """
    variant = VARIANTS[digest % len(VARIANTS)]
    seed = digest % 2_000_000_000
    w, h = _WORK_W, _WORK_H
    pal = palette.as_args()

    # Point the gradient's axis somewhere theme-dependent so two themes that
    # happen to share a variant still do not share a composition.
    x0 = 40 + digest % (w - 120)
    y0 = 30 + (digest // 7) % (h - 80)
    x1 = w - 40 - (digest // 13) % (w - 120)
    y1 = h - 30 - (digest // 17) % (h - 80)

    def gradient(gtype: str) -> str:
        return (
            f"gradients=s={w}x{h}:{pal}:x0={x0}:y0={y0}:x1={x1}:y1={y1}"
            f":type={gtype}:speed={motion.gradient_speed:.5f}"
            f":rate={FPS}:d={source_len:.2f}"
        )

    # Common tail, all still at working resolution: temporal smear, then a
    # slow hue drift, then the vignette. `_FINISH` does the upscale.
    tail = (
        f"tmix=frames={motion.smear_frames}"
        f",hue=h={motion.hue_swing}*sin({motion.hue_rate}*t):s={motion.saturation}"
        f",vignette=PI/4"
    )

    if variant == "liquid":
        # Ink diffusing in water: a coarse, long-lived cellular field blurred
        # into soft masses that both push the colour around and glow through.
        sources = [
            gradient("radial"),
            f"life=s=64x36:mold=24:r={FPS}:ratio=0.18"
            f":death_color=0x101010:life_color=0xf0f0f0:seed={seed}",
        ]
        chain = (
            f"[1:v]format=gray,scale={w}:{h}:flags=bicubic,gblur=sigma=12[n];"
            f"[n]split=3[nx][ny0][nl];"
            f"[ny0]rotate=1.5708:c=none:ow={w}:oh={h}[ny];"
            f"[0:v][nx][ny]displace=edge=smear[d];"
            f"[d][nl]blend=all_mode=screen:all_opacity=0.22,{tail}"
        )
    elif variant == "bloom":
        # Luminous forms adrift in a dark field: a very coarse cellular grid
        # blurred into large soft masses that lighten the colour from within.
        sources = [
            gradient("linear"),
            f"life=s=44x25:mold=30:r={FPS}:ratio=0.42"
            f":death_color=0x080808:life_color=0xffffff:seed={seed}",
        ]
        chain = (
            f"[1:v]format=gray,scale={w}:{h}:flags=bicubic,gblur=sigma=16[n];"
            f"[n]split=3[nx][ny0][nl];"
            f"[ny0]vflip[ny];"
            f"[0:v][nx][ny]displace=edge=smear[d];"
            f"[d][nl]blend=all_mode=lighten:all_opacity=0.30,"
            f"colorbalance=bs=0.06,{tail}"
        )
    elif variant == "nebula":
        # Deep-zoomed fractal, blurred well past recognition into smoke.
        sources = [
            gradient("radial"),
            f"mandelbrot=s=320x180:rate={FPS}:maxiter=50"
            f":start_scale=0.004:end_scale=0.0008"
            f":inner=period:outer=iteration_count",
        ]
        chain = (
            # The rotate canvas is oversized so the crop never reaches a
            # corner: a 512x288 window turned 15 degrees needs 569x411 of
            # cover, and the rotation rate is damped to keep it inside that.
            f"[1:v]format=gray,scale=600:430:flags=fast_bilinear,"
            f"rotate=a={motion.rotate_rate * 0.35:.4f}*t:c=none:ow=600:oh=430,"
            f"crop={w}:{h},gblur=sigma=10[n];"
            f"[n]split=3[nx][ny][nl];"
            f"[0:v][nx][ny]displace=edge=smear[d];"
            f"[d][nl]blend=all_mode=softlight:all_opacity=0.55,{tail}"
        )
    else:  # organism
        # Fine plankton-scale cellular churn over a slowly winding spiral.
        sources = [
            gradient("spiral"),
            f"life=s=110x62:mold=8:r={FPS}:ratio=0.32"
            f":death_color=0x000000:life_color=0xffffff:seed={seed}",
        ]
        chain = (
            f"[1:v]format=gray,scale={w}:{h}:flags=bicubic,gblur=sigma=9[n];"
            f"[n]split=3[nx][ny0][nl];"
            f"[ny0]hflip[ny];"
            f"[0:v][nx][ny]displace=edge=smear[d];"
            f"[nl]eq=contrast=1.45[nm];"
            f"[d][nm]blend=all_mode=overlay:all_opacity=0.42,{tail}"
        )

    return sources, chain


def build_command(
    theme: ThemeObject,
    duration_s: int,
    out_path: Path,
    seed_image_path: Path | None = None,
    *,
    ffmpeg: str = "ffmpeg",
    codec: str = "h264",
) -> list[str]:
    """Assemble the full ffmpeg argv for one clip.

    Exposed separately from `generate` so tests and operators can inspect —
    or reproduce by hand — exactly what will be rendered.
    """
    palette = palette_for(theme)
    motion = motion_for(theme)
    digest = theme_digest(theme)

    # Sources run long: the seed dissolve eats head time and `-t` is what
    # actually fixes the output duration.
    source_len = duration_s + _SEED_HOLD_S + _SEED_FADE_S + 1.0
    sources, chain = _graph(palette, motion, digest, source_len)

    args: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]

    if seed_image_path is None:
        chain = f"{chain},{_FINISH}[v]"
    else:
        # The still becomes input 0, so the two procedural sources shift up by
        # one and the chain's stream labels have to move with them. The
        # dissolve happens at working resolution, before the upscale — doing
        # it at 720p costs roughly a second more per clip for no visible gain.
        still_len = _SEED_HOLD_S + _SEED_FADE_S + 0.5
        args += [
            "-loop", "1",
            "-framerate", str(FPS),
            "-t", f"{still_len:.2f}",
            "-i", str(seed_image_path),
        ]
        chain = chain.replace("[0:v]", "\x00").replace("[1:v]", "[2:v]")
        chain = chain.replace("\x00", "[1:v]")
        chain = (
            f"[0:v]scale={_WORK_W}:{_WORK_H}:force_original_aspect_ratio=increase,"
            f"crop={_WORK_W}:{_WORK_H},setsar=1,fps={FPS},format=rgb24[still];"
            f"{chain},format=rgb24[anim];"
            f"[still][anim]xfade=transition=fade:duration={_SEED_FADE_S}"
            f":offset={_SEED_HOLD_S},{_FINISH}[v]"
        )

    for src in sources:
        args += ["-f", "lavfi", "-i", src]

    args += [
        "-filter_complex", chain,
        "-map", "[v]",
        "-an",
        "-t", str(duration_s),
        "-r", str(FPS),
        *(
            # VP9-in-mp4 for browsers without H.264 (headless test Chromium
            # ships none); h264 stays the default for real screens.
            ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
             "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1"]
            if codec == "vp9"
            else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
        ),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return args


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class MockBackend:
    """Zero-cost procedural `VideoBackend`. Always available, never billed."""

    def __init__(
        self,
        store: ClipStore,
        *,
        name: str = "mock",
        latency_s: float = 0.0,
        ffmpeg: str = "ffmpeg",
        codec: str = "h264",
    ) -> None:
        self.name = name
        self.store = store
        #: Simulated provider latency, awaited before rendering. Tests use it
        #: to make a rung slow; operators can use it to rehearse failover.
        self.latency_s = latency_s
        self.ffmpeg = ffmpeg
        self.codec = codec
        #: Test/operator switch: force `generate` to raise, exercising the
        #: ladder's failure path without needing a broken ffmpeg.
        self.fail = False
        #: Test/operator switch: report DOWN from `health`.
        self.healthy = True

    # -- protocol ----------------------------------------------------------

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            allowed_durations_s=_ALLOWED_DURATIONS,
            supports_native_extend=False,
            supports_image_seed=True,
            tiers=_TIERS,
            max_chain_length=0,
        )

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
        return Decimal("0")

    def estimated_latency(self, tier: str) -> timedelta:
        # Measured: ~2.5-4 s of ffmpeg for an 8 s clip on a modest CPU.
        return timedelta(seconds=self.latency_s + 4.0)

    async def health(self) -> BackendHealth:
        if not self.healthy:
            return BackendHealth(BackendStatus.DOWN, "disabled")
        if shutil.which(self.ffmpeg) is None:
            return BackendHealth(BackendStatus.DOWN, "ffmpeg not on PATH")
        return BackendHealth(BackendStatus.HEALTHY, "procedural")

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        tier: str,
        seed_image: bytes | None = None,
        extend_from: ClipRef | None = None,
        theme_hint: ThemeObject | None = None,
        zone: str = "default",
    ) -> ClipRef:
        """Render one clip and land it in the store.

        `prompt` is never read — see the module docstring. `extend_from` is
        accepted and ignored: this backend advertises no native extension,
        so continuity reaches it as `seed_image` instead (Architecture §3.2).
        """
        if self.fail:
            raise RuntimeError(f"{self.name}: forced failure")
        if duration_s not in _ALLOWED_DURATIONS:
            raise ValueError(
                f"{self.name}: duration {duration_s}s not in {sorted(_ALLOWED_DURATIONS)}"
            )

        if self.latency_s > 0:
            await asyncio.sleep(self.latency_s)

        theme = theme_hint or ThemeObject()
        out_path = self.store.temp_path()
        seed_path: Path | None = None
        if seed_image is not None:
            seed_path = self.store.temp_path(".png")
            await asyncio.to_thread(seed_path.write_bytes, seed_image)

        started = time.monotonic()
        try:
            await self._run(
                build_command(theme, duration_s, out_path, seed_path, ffmpeg=self.ffmpeg, codec=self.codec)
            )
            log.info(
                "mock render backend=%s zone=%s variant=%s duration=%ds encode=%.2fs",
                self.name,
                zone,
                variant_for(theme),
                duration_s,
                time.monotonic() - started,
            )
            return await self.store.put(
                out_path,
                duration_s=float(duration_s),
                zone=zone,
                backend=self.name,
                tier=tier if tier in _TIERS else "mock",
            )
        except BaseException:
            if out_path.exists():
                out_path.unlink()
            raise
        finally:
            if seed_path is not None and seed_path.exists():
                seed_path.unlink()

    # -- internals ---------------------------------------------------------

    async def _run(self, args: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            lines = stderr.decode("utf-8", "replace").strip().splitlines()
            tail = " | ".join(lines[-4:]) if lines else "no stderr"
            raise RuntimeError(f"{self.name}: ffmpeg exited {proc.returncode}: {tail}")
        if not Path(args[-1]).exists():
            raise RuntimeError(f"{self.name}: ffmpeg produced no output file")


__all__ = [
    "VARIANTS",
    "MockBackend",
    "Motion",
    "Palette",
    "base_hue_for",
    "build_command",
    "motion_for",
    "palette_for",
    "theme_digest",
    "variant_for",
]
