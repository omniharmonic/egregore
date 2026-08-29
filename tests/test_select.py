"""Theme selection — which of several things the room said gets rendered.

Pure scoring over validated abstractions. Nothing here ever sees text.
"""

from __future__ import annotations

import math

import pytest

from egregore.types import ThemeObject
from egregore.weaver.select import (
    MIN_TAU_S,
    Candidate,
    Selection,
    Weights,
    select,
)


def theme(*motifs: str, elemental: tuple[str, ...] = ()) -> ThemeObject:
    return ThemeObject(motifs=list(motifs), elemental=list(elemental))


def cand(tokens: int, ended_at: float, *motifs: str) -> Candidate:
    return Candidate(theme=theme(*motifs), tokens=tokens, ended_at=ended_at,
                     started_at=ended_at - 10.0)


NOW = 1000.0


def test_weights_are_normalised_so_sliders_need_not_sum_to_one():
    a = cand(90, NOW - 5, "tide")
    b = cand(10, NOW - 5, "gears")
    heavy = select([a, b], memory=[], weights=Weights(5, 0, 0), now=NOW, tau_s=60)
    light = select([a, b], memory=[], weights=Weights(0.5, 0, 0), now=NOW, tau_s=60)
    assert heavy.winner is a and light.winner is a
    assert heavy.scored[0].score == pytest.approx(light.scored[0].score)


def test_salience_alone_picks_the_segment_the_room_dwelt_on():
    long_ago = cand(80, NOW - 300, "tide")
    just_now = cand(20, NOW - 2, "gears")
    sel = select([long_ago, just_now], memory=[], weights=Weights(1, 0, 0), now=NOW, tau_s=60)
    assert sel.winner is long_ago


def test_recency_alone_picks_the_newest():
    long_ago = cand(80, NOW - 300, "tide")
    just_now = cand(20, NOW - 2, "gears")
    sel = select([long_ago, just_now], memory=[], weights=Weights(0, 0, 1), now=NOW, tau_s=60)
    assert sel.winner is just_now
    r = {id(s.candidate): s.recency for s in sel.scored}
    assert r[id(just_now)] == pytest.approx(math.exp(-2 / 60))


def test_novelty_alone_avoids_what_was_just_rendered():
    same = cand(50, NOW - 5, "tide", "shell")
    fresh = cand(50, NOW - 5, "gears", "lattice")
    memory = [theme("tide", "shell")]
    sel = select([same, fresh], memory=memory, weights=Weights(0, 1, 0), now=NOW, tau_s=60)
    assert sel.winner is fresh
    n = {id(s.candidate): s.novelty for s in sel.scored}
    assert n[id(same)] == pytest.approx(0.0) and n[id(fresh)] == pytest.approx(1.0)


def test_novelty_uses_elemental_as_well_as_motifs():
    a = Candidate(theme=theme("glow", elemental=("water",)), tokens=5, ended_at=NOW, started_at=NOW - 1)
    memory = [theme("other", elemental=("water",))]
    sel = select([a], memory=memory, weights=Weights(0, 1, 0), now=NOW, tau_s=60)
    assert sel.scored[0].novelty < 1.0


def test_novelty_only_looks_at_recent_memory():
    a = cand(5, NOW, "tide")
    old = [theme("tide")] + [theme(f"m{i}") for i in range(5)]   # 'tide' is 6 back
    sel = select([a], memory=old, weights=Weights(0, 1, 0), now=NOW, tau_s=60)
    assert sel.scored[0].novelty == pytest.approx(1.0)


def test_no_memory_means_everything_is_novel():
    a = cand(5, NOW, "tide")
    assert select([a], memory=[], weights=Weights(), now=NOW, tau_s=60).scored[0].novelty == 1.0


def test_tie_goes_to_the_most_recent():
    a = cand(50, NOW - 20, "tide")
    b = cand(50, NOW - 5, "gears")
    sel = select([a, b], memory=[], weights=Weights(1, 0, 0), now=NOW, tau_s=60)
    assert sel.winner is b


def test_tau_is_floored():
    a = cand(5, NOW - 10, "tide")
    sel = select([a], memory=[], weights=Weights(0, 0, 1), now=NOW, tau_s=1.0)
    assert sel.scored[0].recency == pytest.approx(math.exp(-10 / MIN_TAU_S))


def test_listened_spans_the_earliest_start_to_now():
    a = Candidate(theme=theme("a"), tokens=5, ended_at=NOW - 50, started_at=NOW - 90)
    b = Candidate(theme=theme("b"), tokens=5, ended_at=NOW - 2, started_at=NOW - 20)
    assert select([a, b], memory=[], weights=Weights(), now=NOW, tau_s=60).listened_s == pytest.approx(90)


def test_scored_is_sorted_best_first():
    a, b, c = cand(10, NOW - 5, "a"), cand(50, NOW - 5, "b"), cand(30, NOW - 5, "c")
    sel = select([a, b, c], memory=[], weights=Weights(1, 0, 0), now=NOW, tau_s=60)
    assert [s.candidate for s in sel.scored] == [b, c, a]


def test_empty_candidates_is_an_error():
    with pytest.raises(ValueError, match="no candidates"):
        select([], memory=[], weights=Weights(), now=NOW, tau_s=60)


def test_all_zero_weights_is_an_error():
    with pytest.raises(ValueError, match="weights"):
        select([cand(5, NOW, "a")], memory=[], weights=Weights(0, 0, 0), now=NOW, tau_s=60)


def test_selection_carries_no_text():
    sel = select([cand(5, NOW, "a")], memory=[], weights=Weights(), now=NOW, tau_s=60)
    assert isinstance(sel, Selection)
    assert not hasattr(sel.winner, "text")


def test_synthesis_room_bias_can_be_turned_down():
    from egregore.types import MoodState
    from egregore.weaver import synthesize_prompt
    quiet = MoodState(energy=0.1, brightness=0.1)
    t = theme("vast blue depth")
    full = synthesize_prompt(t, "grammar", None, 0.4, quiet)
    assert "bias the palette dark" in full
    soft = synthesize_prompt(t, "grammar", None, 0.4, quiet, room_bias=0.5)
    assert "bias the palette dark" not in soft and "the room is quiet" in soft
    off = synthesize_prompt(t, "grammar", None, 0.4, quiet, room_bias=0.0)
    assert "Room bias" not in off
