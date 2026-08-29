"""Tests for the LOOM module: weighted playlist, last-frame extraction, and
the ZoneLoom continuity state machine (Architecture §2.7 / §3)."""

from __future__ import annotations

import math
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from egregore.loom import (
    FrameExtractionError,
    GenerationPlan,
    WeightedPlaylist,
    ZoneLoom,
    extract_last_frame,
)
from egregore.loom.frames import PNG_MAGIC
from egregore.types import ClipRef, ThemeObject

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_clip(clip_id: str, created_at: float, *, movement_id: str | None = None, duration_s: float = 8.0) -> ClipRef:
    return ClipRef(
        id=clip_id,
        path=Path(f"/nonexistent/{clip_id}.mp4"),
        duration_s=duration_s,
        zone="main",
        backend="mock",
        tier="fast",
        created_at=created_at,
        movement_id=movement_id,
    )


def make_theme(register: str, movement: str, elemental: list[str], motifs: list[str]) -> ThemeObject:
    return ThemeObject(
        motifs=motifs,
        register=register,
        valence=0.5,
        intensity=0.5,
        movement=movement,
        elemental=elemental,
    )


# ---------------------------------------------------------------------------
# WeightedPlaylist — weight formula
# ---------------------------------------------------------------------------


def test_weight_at_half_life_is_one_half_before_floor_binds():
    now = 10_000.0
    playlist = WeightedPlaylist(half_life_min=45.0, floor_weight=0.15, clock=lambda: now)
    clip = make_clip("c1", created_at=now - 45.0 * 60.0)
    assert math.isclose(playlist.weight(clip), 0.5, rel_tol=1e-9)


def test_weight_at_zero_age_is_one():
    now = 10_000.0
    playlist = WeightedPlaylist(half_life_min=45.0, floor_weight=0.15, clock=lambda: now)
    clip = make_clip("c1", created_at=now)
    assert math.isclose(playlist.weight(clip), 1.0, rel_tol=1e-9)


def test_weight_floor_binds_past_threshold():
    half_life = 45.0
    floor = 0.15
    now = 100_000.0
    playlist = WeightedPlaylist(half_life_min=half_life, floor_weight=floor, clock=lambda: now)
    # Architecture §3.4: floor binds at half_life * log2(1/floor) minutes.
    threshold_min = half_life * math.log2(1.0 / floor)
    clip_at_threshold = make_clip("c-at", created_at=now - threshold_min * 60.0)
    clip_past_threshold = make_clip("c-past", created_at=now - (threshold_min + 30.0) * 60.0)
    assert math.isclose(playlist.weight(clip_at_threshold), floor, rel_tol=1e-6)
    assert playlist.weight(clip_past_threshold) == floor


def test_weight_never_drops_below_floor_no_matter_how_old():
    now = 10_000_000.0
    playlist = WeightedPlaylist(half_life_min=45.0, floor_weight=0.15, clock=lambda: now)
    ancient = make_clip("ancient", created_at=0.0)
    assert playlist.weight(ancient) == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# WeightedPlaylist — archive tier
# ---------------------------------------------------------------------------


def test_archive_tier_kicks_in_past_pool_max():
    now = 10_000.0
    playlist = WeightedPlaylist(active_pool_max=3, archive_rate=0.05, clock=lambda: now)
    for i in range(5):
        playlist.add(make_clip(f"c{i}", created_at=now - i))
    assert playlist.active_size == 3
    assert playlist.archive_size == 2
    assert playlist.size == 5


def test_archived_weight_is_reduced_but_nonzero():
    now = 10_000.0
    playlist = WeightedPlaylist(
        half_life_min=45.0, floor_weight=0.15, active_pool_max=2, archive_rate=0.05, clock=lambda: now
    )
    # All clips the same age, so any weight difference in entries() comes
    # purely from the archive-tier thinning, not recency.
    for i in range(4):
        playlist.add(make_clip(f"c{i}", created_at=now))

    entries = {e.clip_id: e.weight for e in playlist.entries()}
    active_weight = entries["c2"]  # last two added stay active
    archived_weight = entries["c0"]  # first two added get archived
    assert archived_weight > 0.0
    assert archived_weight < active_weight
    assert math.isclose(archived_weight / active_weight, 0.05, rel_tol=1e-6)


def test_normalized_entries_sum_to_one():
    now = 10_000.0
    playlist = WeightedPlaylist(active_pool_max=3, clock=lambda: now)
    for i in range(7):
        playlist.add(make_clip(f"c{i}", created_at=now - i * 60.0))
    entries = playlist.entries()
    assert len(entries) == 7
    assert math.isclose(sum(e.weight for e in entries), 1.0, rel_tol=1e-9)


def test_entries_empty_playlist():
    playlist = WeightedPlaylist()
    assert playlist.entries() == []
    assert playlist.sample(random.Random(0)) is None


def test_sample_returns_the_only_clip():
    playlist = WeightedPlaylist(clock=lambda: 0.0)
    clip = make_clip("only", created_at=0.0)
    playlist.add(clip)
    assert playlist.sample(random.Random(1)) == clip


# ---------------------------------------------------------------------------
# frames.extract_last_frame
# ---------------------------------------------------------------------------


def _make_fixture_clip(tmp_path: Path, name: str = "clip.mp4", duration_s: float = 1.0) -> Path:
    out = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=64x64:d={duration_s}",
            "-r",
            "5",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
async def test_extract_last_frame_returns_valid_png(tmp_path: Path):
    clip_path = _make_fixture_clip(tmp_path)
    frame = await extract_last_frame(clip_path)
    assert isinstance(frame, bytes)
    assert frame.startswith(PNG_MAGIC)
    assert len(frame) > len(PNG_MAGIC)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
async def test_extract_last_frame_very_short_clip_falls_back(tmp_path: Path):
    clip_path = _make_fixture_clip(tmp_path, name="tiny.mp4", duration_s=0.1)
    frame = await extract_last_frame(clip_path)
    assert frame.startswith(PNG_MAGIC)


async def test_extract_last_frame_missing_file_raises():
    with pytest.raises(FrameExtractionError):
        await extract_last_frame(Path("/nonexistent/does-not-exist.mp4"))


# ---------------------------------------------------------------------------
# ZoneLoom — mosaic mode
# ---------------------------------------------------------------------------


async def test_mosaic_ingest_grows_playlist_and_plans_plain_generation():
    loom = ZoneLoom("main", "mosaic")
    clip = make_clip("c1", created_at=0.0)
    await loom.ingest(clip, Path("/does/not/need/to/exist.mp4"))
    assert loom.playlist.size == 1

    plan = loom.plan_next()
    assert plan == GenerationPlan(use_extend=None, seed_image=None, movement_descriptor=None, new_movement=False)
    # Mosaic mode never touches movements or last_frame.
    assert loom.movements == []
    assert loom.last_frame is None


# ---------------------------------------------------------------------------
# ZoneLoom — continuity mode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
async def test_continuity_chain_reaches_ceiling_and_hands_off(tmp_path: Path):
    clip_path = _make_fixture_clip(tmp_path)
    loom = ZoneLoom("main", "continuity", max_chain_length=3, clock=lambda: 0.0)

    for i in range(3):
        plan = loom.plan_next()
        # i == 0: nothing ingested yet, so this starts the first movement.
        # i == 1, 2: mid-chain, not yet at the ceiling.
        assert plan.new_movement is (i == 0)
        clip = make_clip(f"c{i}", created_at=0.0)
        await loom.ingest(clip, clip_path)

    assert loom.current_chain_length == 3
    assert len(loom.movements) == 1
    assert loom.movements[0].clip_ids == ["c0", "c1", "c2"]

    plan = loom.plan_next()
    assert plan.new_movement is True
    assert plan.use_extend is None
    assert plan.seed_image is not None
    assert plan.seed_image.startswith(PNG_MAGIC)

    # The handoff clip starts a fresh movement and resets the chain counter.
    handoff_clip = make_clip("c3", created_at=0.0)
    await loom.ingest(handoff_clip, clip_path)
    assert loom.current_chain_length == 1
    assert len(loom.movements) == 2
    assert loom.movements[1].clip_ids == ["c3"]


async def test_continuity_mid_chain_offers_both_ways_to_continue():
    # The Loom is capability-blind by construction: it offers every way of
    # continuing the movement it has, and the integration layer picks the one
    # the backend actually supports. Offering only ``use_extend`` here meant a
    # backend that can continue *only* from an image — local diffusion, which
    # cannot natively extend — rendered every mid-chain clip fresh while the
    # chain counter kept climbing. The movement was bookkeeping, not picture.
    loom = ZoneLoom("main", "continuity", max_chain_length=5, clock=lambda: 0.0)
    loom._start_movement()  # simulate a movement already underway
    loom._pending_handoff = False
    loom.current_chain_length = 1
    clip = make_clip("c0", created_at=0.0)
    loom._last_clip = clip
    loom.last_frame = PNG_MAGIC + b"pretend-frame"
    plan = loom.plan_next()
    assert plan.use_extend == clip
    assert plan.new_movement is False
    assert plan.seed_image == loom.last_frame


async def test_continuity_mid_chain_seed_is_absent_until_a_frame_exists():
    # Before any clip has landed there is no frame to continue from, and
    # inventing one would be worse than starting fresh.
    loom = ZoneLoom("main", "continuity", max_chain_length=5, clock=lambda: 0.0)
    loom._start_movement()
    loom._pending_handoff = False
    loom.current_chain_length = 1
    loom._last_clip = make_clip("c0", created_at=0.0)
    assert loom.plan_next().seed_image is None


async def test_continuity_ingest_tolerates_extraction_failure(tmp_path: Path):
    loom = ZoneLoom("main", "continuity", max_chain_length=5, clock=lambda: 0.0)
    good_clip_path = tmp_path / "missing.mp4"  # does not exist -> extraction fails
    clip = make_clip("c0", created_at=0.0)
    # Should not raise, and last_frame stays None (nothing to keep yet).
    await loom.ingest(clip, good_clip_path)
    assert loom.last_frame is None
    assert loom.current_chain_length == 1
    assert loom.playlist.size == 1


# ---------------------------------------------------------------------------
# ZoneLoom — live mode switching
# ---------------------------------------------------------------------------


async def test_mode_switch_mosaic_to_continuity_with_no_prior_frame():
    loom = ZoneLoom("main", "mosaic")
    await loom.ingest(make_clip("c1", created_at=0.0), Path("/whatever.mp4"))
    loom.set_mode("continuity")
    assert loom.mode == "continuity"
    plan = loom.plan_next()
    # Nothing to seed from yet (mosaic never extracted a frame).
    assert plan.new_movement is True
    assert plan.seed_image is None
    # Playlist untouched by the switch.
    assert loom.playlist.size == 1


async def test_mode_switch_continuity_to_mosaic_stops_seeding_and_back(tmp_path: Path):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    clip_path = _make_fixture_clip(tmp_path)
    loom = ZoneLoom("main", "continuity", max_chain_length=10, clock=lambda: 0.0)
    await loom.ingest(make_clip("c1", created_at=0.0), clip_path)
    frame_after_continuity = loom.last_frame
    assert frame_after_continuity is not None

    loom.set_mode("mosaic")
    assert loom.mode == "mosaic"
    assert loom.plan_next() == GenerationPlan()
    # last_frame preserved, not cleared, across the switch.
    assert loom.last_frame == frame_after_continuity

    loom.set_mode("continuity")
    plan = loom.plan_next()
    assert plan.new_movement is True
    assert plan.seed_image == frame_after_continuity


# ---------------------------------------------------------------------------
# ZoneLoom — manifest
# ---------------------------------------------------------------------------


async def test_manifest_revision_and_mode():
    loom = ZoneLoom("zoneX", "continuity", crossfade_s=3.5)
    await loom.ingest(make_clip("c1", created_at=0.0), Path("/whatever.mp4"))
    manifest = loom.manifest(revision=7)
    assert manifest.zone == "zoneX"
    assert manifest.mode == "continuity"
    assert manifest.revision == 7
    assert manifest.crossfade_s == 3.5
    assert len(manifest.entries) == loom.playlist.size


# ---------------------------------------------------------------------------
# ZoneLoom — thematic memory
# ---------------------------------------------------------------------------


def test_continuity_context_uses_only_theme_fields():
    loom = ZoneLoom("main", "continuity")
    assert loom.continuity_context() is None
    theme = make_theme(
        register="elegiac",
        movement="slow spiralling",
        elemental=["water", "deep blue"],
        motifs=["a secret confession"],
    )
    loom.remember_theme(theme)
    ctx = loom.continuity_context()
    assert ctx is not None
    assert "elegiac register" in ctx
    assert "slow spiralling movement" in ctx
    assert "water" in ctx and "deep blue" in ctx
    assert "secret confession" not in ctx


def test_thematic_memory_capped_at_fifty():
    loom = ZoneLoom("main", "continuity")
    for i in range(60):
        loom.remember_theme(make_theme("ambient", "drift", [], [f"motif{i}"]))
    assert len(loom.thematic_memory) == 50
    # Oldest dropped: motif0..motif9 gone, motif59 (most recent) present.
    assert loom.thematic_memory[0].motifs == ["motif10"]
    assert loom.thematic_memory[-1].motifs == ["motif59"]


def test_recall_motifs_deterministic_with_seeded_rng():
    loom = ZoneLoom("main", "continuity")
    for i in range(5):
        loom.remember_theme(make_theme("ambient", "drift", [], [f"m{i}a", f"m{i}b"]))

    result_1 = loom.recall_motifs(k=3, rng=random.Random(42))
    result_2 = loom.recall_motifs(k=3, rng=random.Random(42))
    assert result_1 == result_2
    assert len(result_1) == 3
    all_motifs = {motif for theme in loom.thematic_memory for motif in theme.motifs}
    assert set(result_1) <= all_motifs


def test_recall_motifs_empty_memory():
    loom = ZoneLoom("main", "continuity")
    assert loom.recall_motifs(k=3, rng=random.Random(0)) == []


def test_recall_motifs_caps_k_to_pool_size():
    loom = ZoneLoom("main", "continuity")
    loom.remember_theme(make_theme("ambient", "drift", [], ["only-one"]))
    result = loom.recall_motifs(k=5, rng=random.Random(0))
    assert result == ["only-one"]


# ---------------------------------------------------------------------------
# Provenance weighting — fills are connective tissue, not peers
# ---------------------------------------------------------------------------


def test_a_procedural_fill_is_worth_less_on_screen_than_a_diffusion_clip():
    """Fills are generated far more often than paid clips, so equal weighting
    lets them crowd out the material a party is actually paying for — which
    on screen reads as "it spent real money and looks procedural"."""
    from egregore.loom.playlist import WeightedPlaylist

    pl = WeightedPlaylist(half_life_min=45.0, clock=lambda: 1000.0)
    fill = ClipRef(id="a" * 16, path=Path("/tmp/a.mp4"), duration_s=6.0,
                   zone="main", backend="procedural", tier="mock",
                   created_at=1000.0)
    rich = ClipRef(id="b" * 16, path=Path("/tmp/b.mp4"), duration_s=6.0,
                   zone="main", backend="fal", tier="minimax-h3-max",
                   created_at=1000.0)
    pl.add(fill)
    pl.add(rich)

    # Same age, so only provenance separates them.
    weights = {e.clip_id: e.weight for e in pl.entries()}
    assert weights[rich.id] > weights[fill.id] * 2


def test_an_unknown_backend_keeps_full_weight():
    # A backend we have not met should not be quietly demoted.
    from egregore.loom.playlist import WeightedPlaylist

    pl = WeightedPlaylist(clock=lambda: 1000.0)
    clip = ClipRef(id="c" * 16, path=Path("/tmp/c.mp4"), duration_s=6.0,
                   zone="main", backend="some-new-vendor", tier="x",
                   created_at=1000.0)
    assert pl.backend_weight(clip) == 1.0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")
async def test_manifest_tells_screens_which_clips_form_a_movement(tmp_path: Path):
    # The Loom seeds clip N+1 from clip N's last frame, but the store never
    # knew which movement a clip belonged to, so the manifest said nothing
    # and the deck picked at random: the seam continuity was built for was
    # never shown. The manifest now carries movement and position.
    clip_path = _make_fixture_clip(tmp_path)
    loom = ZoneLoom("main", "continuity", max_chain_length=3, clock=lambda: 0.0)
    for i in range(3):
        loom.plan_next()
        await loom.ingest(make_clip(f"c{i}", created_at=0.0), clip_path)
    by_id = {e.clip_id: e for e in loom.manifest().entries}
    assert by_id["c0"].movement_id == by_id["c1"].movement_id == by_id["c2"].movement_id
    assert by_id["c0"].movement_id is not None
    assert [by_id[f"c{i}"].chain_index for i in range(3)] == [0, 1, 2]
