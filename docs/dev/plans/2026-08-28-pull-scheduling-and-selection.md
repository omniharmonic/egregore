# Pull Scheduling and Theme Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound generation lag at exactly one render, and when a render completes, choose the best theme from everything said meanwhile, scored by tunable salience / novelty / recency.

**Architecture:** The paid/local generation trigger becomes "the zone's Forge worker is idle AND the Governor's budget spacing is satisfied" — render latency stops being a scheduling input. On trigger, the ring buffer is split into pause-delimited segments, each abstracted and validated into a candidate `ThemeObject`, and a pure scoring function picks the winner. All knobs live in a new `SelectionConfig`, party-default with per-zone override, all live-settable.

**Tech Stack:** Python 3.11, asyncio, pydantic v2, pytest (asyncio auto mode), vanilla JS dashboard. Run everything with `uv run`.

**Spec:** `docs/superpowers/specs/2026-08-28-pull-scheduling-and-selection-design.md`

## Global Constraints

- Privacy (PRD 6.8): transcript text exists only in `scribe.RingBuffer` and transiently in weaver stage 1. Never logged, never in `repr`, never on disk, never in an exception message. Only validated abstractions are scored, shown, or sent.
- `egregore/types.py` and `egregore/config/schema.py` are frozen contracts (CONTRACTS.md). This plan extends `schema.py` **additively only** and notes it in CONTRACTS.md. `types.py` is not touched.
- Modules import only from `egregore.types`, `egregore.config.schema`, stdlib, declared deps — never a sibling module. `app.py` is where modules meet.
- Money is `Decimal`. Everything async-first.
- Lint must pass: `uv run ruff check .` Tests: `uv run pytest -q` (currently 404 passing).
- Commit after every task. Commit trailer:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01R38A4n4ywyEm3u5GLrAm9m
  ```
- Nothing that works today may stop working: fill lane, zone bleed, freeze, purge, continuity chains, mirror/commons topologies, budget ceiling.

---

## File map

| File | Responsibility |
|---|---|
| `egregore/scribe/ring.py` | +`Segment` (frozen, redacted repr) and `RingBuffer.segments(gap_s)` |
| `egregore/weaver/select.py` | NEW. `Candidate`, `ScoredCandidate`, `Selection`, `select()`. Pure, no text. |
| `egregore/weaver/weaver.py` | +`Weaver.weave_candidates(segments, ...)` |
| `egregore/weaver/__init__.py` | export the new names |
| `egregore/config/schema.py` | +`SelectionConfig`; `WeaverConfig.selection`; `ZoneConfig.selection` |
| `egregore/config/store.py` | +live keys |
| `egregore/forge/forge.py` | +`Forge.in_flight(zone)` |
| `egregore/governor/governor.py` | keep `throughput_floor_s` param (operator floor only); no code change needed beyond docstring |
| `egregore/app.py` | `_throughput_floor` → `_operator_floor`; loop rewrite; `LiveSettings.selection_for(zone)`; `last_selection`; `lag_s`; monitor candidates |
| `egregore/conductor/state.py` | +`zone_settings_handler` |
| `egregore/conductor/app.py` | `POST /api/zones/{zone}` accepts `selection`; `GET /api/zones` returns it |
| `lens/setup.html` | three sliders + gap slider per zone; monitor candidates list; lag in status |
| `presets/local-party.yaml` | `weaver.selection` with novelty 0.2 |
| `README.md`, `CONTRACTS.md` | document |
| `tests/test_scribe.py`, `tests/test_select.py` (NEW), `tests/test_weaver.py`, `tests/test_config_store.py`, `tests/test_forge.py`, `tests/test_integration.py`, `tests/test_conductor.py` | tests |

---

### Task 1: `RingBuffer.segments()` — split the window at pauses

**Files:**
- Modify: `egregore/scribe/ring.py` (after `snapshot()`, ~line 239)
- Test: `tests/test_scribe.py`

**Interfaces:**
- Produces: `Segment(text: str, started_at: float, ended_at: float, tokens: int)` frozen dataclass exported from `egregore.scribe`; `RingBuffer.segments(gap_s: float) -> list[Segment]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_scribe.py`)

```python
# ---------------------------------------------------------------------------
# Segments — the window split at pauses, for theme selection
# ---------------------------------------------------------------------------

from egregore.scribe import Segment  # noqa: E402


def test_segments_split_where_the_room_paused():
    ring, clock = make_ring()
    ring.add("we drove out to the coast", t=1000.0)
    ring.add("before sunrise", t=1002.0)
    clock.advance(20)
    ring.add("my grandmother kept shells", t=1020.0)
    segs = ring.segments(gap_s=6.0)
    assert len(segs) == 2
    assert segs[0].text == "we drove out to the coast before sunrise"
    assert segs[0].started_at == 1000.0 and segs[0].ended_at == 1002.0
    assert segs[0].tokens == 7
    assert segs[1].text == "my grandmother kept shells"


def test_segments_do_not_split_under_the_gap():
    ring, clock = make_ring()
    ring.add("one", t=1000.0)
    ring.add("two", t=1004.0)
    clock.advance(4)
    assert len(ring.segments(gap_s=6.0)) == 1


def test_segments_of_an_empty_ring_is_empty():
    ring, _ = make_ring()
    assert ring.segments(gap_s=6.0) == []


def test_segments_evict_first_like_snapshot():
    ring, clock = make_ring(window_s=10.0)
    ring.add("old", t=1000.0)
    clock.advance(30)
    ring.add("new", t=1030.0)
    segs = ring.segments(gap_s=6.0)
    assert [s.text for s in segs] == ["new"]


def test_segment_repr_is_redacted():
    s = Segment(text="a secret phrase", started_at=0.0, ended_at=1.0, tokens=3)
    assert "secret" not in repr(s) and "secret" not in str(s)
    assert "3" in repr(s)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scribe.py -k segments -q`
Expected: FAIL — `ImportError: cannot import name 'Segment'`

- [ ] **Step 3: Implement**

In `egregore/scribe/ring.py`, after the imports add:

```python
from dataclasses import dataclass
```

and after `__all__ = ["RingBuffer"]` change to `__all__ = ["RingBuffer", "Segment"]`, then add before `class RingBuffer`:

```python
@dataclass(frozen=True)
class Segment:
    """One stretch of speech between pauses. Text lives here and in the
    weaver's stage 1 — nowhere else. ``repr`` is counts only."""

    text: str
    started_at: float
    ended_at: float
    tokens: int

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Segment(<{self.tokens} tokens redacted>, "
            f"{self.started_at:.1f}..{self.ended_at:.1f})"
        )

    __str__ = __repr__
```

Then in `RingBuffer`, after `snapshot()`:

```python
    def segments(self, gap_s: float) -> list[Segment]:
        """The window split at pauses of at least ``gap_s`` seconds.

        Same boundary as ``snapshot()``: evicts first, never clears, and the
        text goes to weaver stage 1 and nowhere else. A pause is measured
        between consecutive fragment timestamps, so a room that talks
        without a break yields one segment and a back-and-forth yields
        several — which is what lets the selector weigh them.
        """
        self._evict()
        out: list[Segment] = []
        parts: list[str] = []
        start = end = 0.0
        for frag in self._fragments:
            if parts and frag.t - end >= gap_s:
                text = " ".join(parts)
                out.append(Segment(text, start, end, len(text.split())))
                parts = []
            if not parts:
                start = frag.t
            parts.append(frag.text)
            end = frag.t
        if parts:
            text = " ".join(parts)
            out.append(Segment(text, start, end, len(text.split())))
        return out
```

In `egregore/scribe/__init__.py` change `from egregore.scribe.ring import RingBuffer` to `from egregore.scribe.ring import RingBuffer, Segment` and add `"Segment"` to `__all__`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_scribe.py -q && uv run ruff check egregore/scribe`
Expected: all pass, lint clean.

- [ ] **Step 5: Commit**

```bash
git add egregore/scribe tests/test_scribe.py
git commit -m "feat(scribe): RingBuffer.segments splits the window at pauses"
```

---

### Task 2: `weaver/select.py` — pure scoring

**Files:**
- Create: `egregore/weaver/select.py`
- Modify: `egregore/weaver/__init__.py`
- Test: `tests/test_select.py` (new)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True) class Candidate: theme: ThemeObject; tokens: int; ended_at: float; started_at: float
  @dataclass(frozen=True) class ScoredCandidate: candidate: Candidate; salience: float; novelty: float; recency: float; score: float
  @dataclass(frozen=True) class Selection: winner: Candidate; scored: list[ScoredCandidate]; listened_s: float
  @dataclass(frozen=True) class Weights: salience: float = 0.5; novelty: float = 0.3; recency: float = 0.2
  def select(candidates, *, memory, weights, now, tau_s) -> Selection
  MIN_TAU_S = 30.0; MEMORY_DEPTH = 5
  ```

- [ ] **Step 1: Write the failing tests** — create `tests/test_select.py`:

```python
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
    r = {s.candidate: s.recency for s in sel.scored}
    assert r[just_now] == pytest.approx(math.exp(-2 / 60))


def test_novelty_alone_avoids_what_was_just_rendered():
    same = cand(50, NOW - 5, "tide", "shell")
    fresh = cand(50, NOW - 5, "gears", "lattice")
    memory = [theme("tide", "shell")]
    sel = select([same, fresh], memory=memory, weights=Weights(0, 1, 0), now=NOW, tau_s=60)
    assert sel.winner is fresh
    n = {s.candidate: s.novelty for s in sel.scored}
    assert n[same] == pytest.approx(0.0) and n[fresh] == pytest.approx(1.0)


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
    a = cand(10, NOW - 5, "a"); b = cand(50, NOW - 5, "b"); c = cand(30, NOW - 5, "c")
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_select.py -q`
Expected: FAIL — `ModuleNotFoundError: egregore.weaver.select`

- [ ] **Step 3: Implement** — create `egregore/weaver/select.py`:

```python
"""Theme selection — which of several things the room said gets rendered.

When a render finishes, the room has usually said more than one thing in
the meantime. Snapshotting the whole window and abstracting once flattens a
conversation into an average of itself; this picks instead. Every candidate
is a *validated* abstraction — nothing here sees, stores, or logs text.

Three signals, each in [0, 1], blended by operator-set weights:

* **salience** — the share of the window's words this segment holds. What the
  room dwelt on.
* **novelty** — how far the candidate is from what was recently rendered,
  measured as 1 minus the best set overlap of its motifs and elemental
  palette against the last few remembered themes.
* **recency** — how fresh it is, decaying exponentially with a time constant
  the caller sets to the last render's duration, so what was said *during*
  the render is weighted near one and older material fades.

Weights are normalised here so the operator's sliders need not sum to one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from egregore.types import ThemeObject

__all__ = [
    "MEMORY_DEPTH",
    "MIN_TAU_S",
    "Candidate",
    "ScoredCandidate",
    "Selection",
    "Weights",
    "select",
]

#: Recency decay is never sharper than this, so a fast cloud backend does not
#: reduce the choice to "whatever was said in the last four seconds".
MIN_TAU_S = 30.0
#: How many remembered themes novelty is measured against.
MEMORY_DEPTH = 5


@dataclass(frozen=True)
class Weights:
    salience: float = 0.5
    novelty: float = 0.3
    recency: float = 0.2


@dataclass(frozen=True)
class Candidate:
    """A validated theme and the shape of the speech it came from. No text."""

    theme: ThemeObject
    tokens: int
    ended_at: float
    started_at: float


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    salience: float
    novelty: float
    recency: float
    score: float


@dataclass(frozen=True)
class Selection:
    winner: Candidate
    scored: list[ScoredCandidate]   # best first
    listened_s: float               # earliest candidate start to now


def _bag(theme: ThemeObject) -> frozenset[str]:
    return frozenset(s.strip().lower() for s in (*theme.motifs, *theme.elemental) if s.strip())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def select(
    candidates: list[Candidate],
    *,
    memory: list[ThemeObject],
    weights: Weights,
    now: float,
    tau_s: float,
) -> Selection:
    """Score every candidate and return the best, with the working shown."""
    if not candidates:
        raise ValueError("no candidates to select from")
    total_w = weights.salience + weights.novelty + weights.recency
    if total_w <= 0:
        raise ValueError("selection weights must not all be zero")
    w_s, w_n, w_r = (
        weights.salience / total_w, weights.novelty / total_w, weights.recency / total_w
    )
    tau = max(float(tau_s), MIN_TAU_S)
    total_tokens = max(1, sum(c.tokens for c in candidates))
    recent = [_bag(t) for t in memory[-MEMORY_DEPTH:]]

    scored: list[ScoredCandidate] = []
    for c in candidates:
        salience = c.tokens / total_tokens
        bag = _bag(c.theme)
        novelty = 1.0 - max((_jaccard(bag, r) for r in recent), default=0.0)
        recency = math.exp(-max(0.0, now - c.ended_at) / tau)
        score = w_s * salience + w_n * novelty + w_r * recency
        scored.append(ScoredCandidate(c, salience, novelty, recency, score))

    # Best first; among equals, the most recently finished.
    scored.sort(key=lambda s: (s.score, s.candidate.ended_at), reverse=True)
    listened = now - min(c.started_at for c in candidates)
    return Selection(winner=scored[0].candidate, scored=scored, listened_s=max(0.0, listened))
```

Add to `egregore/weaver/__init__.py`:

```python
from .select import (
    MEMORY_DEPTH,
    MIN_TAU_S,
    Candidate,
    ScoredCandidate,
    Selection,
    Weights,
    select,
)
```
and the seven names to `__all__`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_select.py -q && uv run ruff check egregore/weaver`
Expected: 14 pass, lint clean.

- [ ] **Step 5: Commit**

```bash
git add egregore/weaver/select.py egregore/weaver/__init__.py tests/test_select.py
git commit -m "feat(weaver): pure theme selection by salience, novelty, recency"
```

---

### Task 3: `Weaver.weave_candidates()`

**Files:**
- Modify: `egregore/weaver/weaver.py` (add method after `weave()`)
- Test: `tests/test_weaver.py`

**Interfaces:**
- Consumes: `Segment` shape (duck-typed: `.text .started_at .ended_at .tokens`), `Candidate` from Task 2.
- Produces: `async def weave_candidates(self, segments, *, mood=None, max_candidates=6) -> list[Candidate]`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_weaver.py`)

```python
# ---------------------------------------------------------------------------
# Candidates — one validated theme per stretch of speech
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc  # noqa: E402

from egregore.weaver import Candidate, Weaver  # noqa: E402


@_dc(frozen=True)
class Seg:
    text: str
    started_at: float
    ended_at: float
    tokens: int


def seg(text: str, t: float) -> Seg:
    return Seg(text, t - 5, t, len(text.split()))


async def test_weave_candidates_yields_one_theme_per_segment():
    w = Weaver()
    segs = [
        seg("we drove out to the coast and the tide pools were glowing green", 100),
        seg("the scheduler keeps timing out and the latency is terrible", 200),
    ]
    cands = await w.weave_candidates(segs)
    assert len(cands) == 2
    assert all(isinstance(c, Candidate) for c in cands)
    assert cands[0].ended_at == 100 and cands[0].tokens == 13
    assert not hasattr(cands[0], "text")


async def test_weave_candidates_keeps_the_longest_when_capped():
    w = Weaver()
    segs = [
        seg("short one", 1),
        seg("this is a much longer stretch of talk about the ocean and its tides and light", 2),
        seg("medium length talk about gears", 3),
    ]
    cands = await w.weave_candidates(segs, max_candidates=2)
    assert sorted(c.tokens for c in cands) == [5, 15]


async def test_weave_candidates_drops_a_rejected_theme_without_purging():
    class Leaky:
        async def abstract(self, text, mood=None, *, attempt=0):
            # Copies a three-gram straight out of the text: the validator
            # must reject it.
            words = text.split()
            return ThemeObject(motifs=[" ".join(words[:3])])

    w = Weaver(Leaky())
    before = w.purges_requested
    cands = await w.weave_candidates([seg("one two three four five six", 1)])
    assert cands == []
    assert w.rejections == 1
    assert w.purges_requested == before


async def test_weave_candidates_of_nothing_is_nothing():
    assert await Weaver().weave_candidates([]) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_weaver.py -k weave_candidates -q`
Expected: FAIL — `ImportError: cannot import name 'Candidate'` / `AttributeError: weave_candidates`

- [ ] **Step 3: Implement** — in `egregore/weaver/weaver.py`:

Change the import block to add:
```python
from .select import Candidate
```

Add after `weave()` (before `# -- internals --`):

```python
    async def weave_candidates(
        self,
        segments,
        *,
        mood: MoodState | None = None,
        max_candidates: int = 6,
    ):
        """One validated theme per stretch of speech, for the selector.

        Each segment is abstracted and validated against *its own* text, so a
        candidate can never carry a phrase from a neighbouring segment. A
        rejected candidate is dropped and counted; it does not purge — purge
        stays reserved for the whole-window path, which the caller falls back
        to when nothing here survives.

        Returns ``Candidate`` objects only: theme plus the shape of the speech
        (token count, timestamps). No text leaves this method.
        """
        keep = sorted(segments, key=lambda s: s.tokens, reverse=True)[: max(1, int(max_candidates))]
        keep.sort(key=lambda s: s.started_at)
        out: list[Candidate] = []
        for s in keep:
            try:
                theme = await self.abstractor.abstract(s.text, mood, attempt=0)
            except AbstractionError:
                log.warning("weaver candidate stage-1 failed")
                continue
            verdict = validate_theme(theme, s.text)
            if not verdict.ok:
                self.rejections += 1
                log.warning("weaver candidate rejected", extra={"reasons": verdict.reasons})
                continue
            out.append(Candidate(theme=theme, tokens=int(s.tokens),
                                 ended_at=float(s.ended_at), started_at=float(s.started_at)))
        return out
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_weaver.py -q && uv run ruff check egregore/weaver`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add egregore/weaver/weaver.py tests/test_weaver.py
git commit -m "feat(weaver): weave_candidates validates one theme per segment"
```

---

### Task 4: `SelectionConfig` and live keys

**Files:**
- Modify: `egregore/config/schema.py` (`WeaverConfig` ~line 49, `ZoneConfig` ~line 158)
- Modify: `egregore/config/store.py` (`LIVE_KEYS`)
- Modify: `CONTRACTS.md`
- Test: `tests/test_config_store.py`

**Interfaces:**
- Produces: `SelectionConfig(salience, novelty, recency, segment_gap_s, max_candidates, recency_tau_s)`; `EgregoreConfig.weaver.selection`; `ZoneConfig.selection: SelectionConfig | None`; `EgregoreConfig.zone_selection(zone_id) -> SelectionConfig`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config_store.py`)

```python
# ---------------------------------------------------------------------------
# Selection config — party default, zone override, live keys
# ---------------------------------------------------------------------------


def test_selection_defaults():
    cfg = EgregoreConfig()
    s = cfg.weaver.selection
    assert (s.salience, s.novelty, s.recency) == (0.5, 0.3, 0.2)
    assert s.segment_gap_s == 6.0 and s.max_candidates == 6 and s.recency_tau_s is None


def test_selection_all_zero_weights_rejected():
    with pytest.raises(ValueError, match="weight"):
        EgregoreConfig.model_validate(
            {"weaver": {"selection": {"salience": 0, "novelty": 0, "recency": 0}}}
        )


def test_zone_selection_overrides_party_default():
    cfg = EgregoreConfig.model_validate({
        "weaver": {"selection": {"novelty": 0.9}},
        "zones": [{"id": "a"}, {"id": "b", "selection": {"novelty": 0.1}}],
    })
    assert cfg.zone_selection("a").novelty == 0.9
    assert cfg.zone_selection("b").novelty == 0.1
    assert cfg.zone_selection("b").salience == 0.5, "unset fields fall to the schema default"


def test_selection_keys_are_live():
    for k in ("salience", "novelty", "recency", "segment_gap_s", "max_candidates", "recency_tau_s"):
        assert f"weaver.selection.{k}" in store.LIVE_KEYS
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config_store.py -k selection -q`
Expected: FAIL — `AttributeError: selection`

- [ ] **Step 3: Implement**

In `egregore/config/schema.py`, before `class WeaverConfig`:

```python
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

    @model_validator(mode="after")
    def _some_weight(self) -> SelectionConfig:
        if self.salience + self.novelty + self.recency <= 0:
            raise ValueError("at least one selection weight must be above zero")
        return self
```

In `WeaverConfig` add:
```python
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
```

In `ZoneConfig` add (after `crossfade_s`):
```python
    selection: SelectionConfig | None = None  # None = weaver.selection
```

In `EgregoreConfig` after `zone_mode`:
```python
    def zone_selection(self, zone_id: str) -> SelectionConfig:
        for z in self.zones:
            if z.id == zone_id and z.selection is not None:
                return z.selection
        return self.weaver.selection
```

In `egregore/config/store.py` `LIVE_KEYS` add:
```python
    "weaver.selection.salience",
    "weaver.selection.novelty",
    "weaver.selection.recency",
    "weaver.selection.segment_gap_s",
    "weaver.selection.max_candidates",
    "weaver.selection.recency_tau_s",
```

In `CONTRACTS.md`, after the frozen-contracts paragraph, add:
```
> Additive extensions on 2026-08-28: `GenerationConfig.local_steps/local_resolution`
> and `SelectionConfig` (+`WeaverConfig.selection`, `ZoneConfig.selection`,
> `EgregoreConfig.zone_selection()`). Nothing existing changed shape.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config_store.py -q && uv run ruff check egregore/config`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add egregore/config CONTRACTS.md tests/test_config_store.py
git commit -m "feat(config): SelectionConfig, party default with zone override, live keys"
```

---

### Task 5: `Forge.in_flight()`

**Files:**
- Modify: `egregore/forge/forge.py` (after `queue_depth`, ~line 207)
- Test: `tests/test_forge.py`

**Interfaces:**
- Produces: `Forge.in_flight(zone: str) -> int` — paid jobs currently rendering (0 or 1 per zone).

- [ ] **Step 1: Write the failing test** (append to `tests/test_forge.py`)

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_forge.py -k in_flight -q`
Expected: FAIL — `AttributeError: in_flight`

- [ ] **Step 3: Implement** — after `queue_depth` in `forge.py`:

```python
    def in_flight(self, zone: str) -> int:
        """Paid jobs currently being rendered for ``zone`` — 0 or 1.

        The pull scheduler asks for a new clip only when this and the queue
        are both empty, which is what bounds lag at one render.
        """
        return self._inflight.get(zone, 0)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_forge.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add egregore/forge/forge.py tests/test_forge.py
git commit -m "feat(forge): in_flight(zone) for the pull scheduler"
```

---

### Task 6: The pull loop, `LiveSettings.selection_for`, `last_selection`, `lag_s`

This is the integration task. Everything above meets here.

**Files:**
- Modify: `egregore/app.py`:
  - `_throughput_floor` (lines 157–190) → rename `_operator_floor`, drop the backend probe
  - `_MAX_QUEUE_DEPTH` (line 154) → delete
  - `LiveSettings` (~line 213): add `selection: dict`, `selection_by_zone: dict[str, dict]`, `selection_for(zone) -> SelectionConfig`, live apply for `weaver.selection.*` and `zones.<id>.selection`
  - `ZonePipeline.__init__`: `self.throttled` → `self.waited_for_slot`; add `self.last_selection: dict | None`, `self._pending_lag_anchor: float | None`
  - `_generation_loop` (lines 505–615): rewrite the trigger and the prompt path
  - `on_clip` (line 492): compute `lag_s`
  - `status()`: replace `throttled`, add `last_selection`, `in_flight`
  - `run_party` (~line 752): `throughput_floor_s=_operator_floor(live)`
  - `_monitor`: add `candidates`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `RingBuffer.segments`, `Weaver.weave_candidates`, `select`, `Weights`, `SelectionConfig`, `Forge.in_flight`.
- Produces: zone status keys `in_flight`, `waited_for_slot`, `last_selection: {candidates:int, winner_score:float, listened_s:float, lag_s:float|None} | None`; monitor key `candidates: [{motifs, elemental, salience, novelty, recency, score, winner}]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_integration.py`)

```python
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

    async def generate(self, *a, **kw):
        self.started += 1
        await self.gate.wait()
        self.gate.clear()
        return await super().generate(*a, **kw)


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
        await party.wait_clips(1, "main")
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
        clips = await party.wait_clips(2, "main", timeout=30)
        assert all(c.backend == "procedural" for c in clips)
        slow.gate.set()


async def test_selection_is_recorded_and_lag_is_measured(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        await party.wait_clips(2, "main")
        deadline = time.monotonic() + 10
        while (pipe.last_selection is None or pipe.last_selection.get("lag_s") is None) \
                and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        sel = pipe.status()["last_selection"]
        assert sel is not None and sel["candidates"] >= 1
        assert sel["lag_s"] is not None and sel["lag_s"] >= 0
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
```

Add `from egregore.forge import ClipStore, MockBackend` to the imports if not present (check the top of the file — `MockBackend` and `ClipStore` are already imported for `Party`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_integration.py -k "in_flight or fill_lane_still or selection" -q`
Expected: FAIL — `AttributeError: waited_for_slot` / `selection_for`

- [ ] **Step 3: Implement**

**3a. Delete `_MAX_QUEUE_DEPTH`** (line 154) and the env read.

**3b. Replace `_throughput_floor`** (lines 157–190) with:

```python
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
```

Update the call in `run_party`: `throughput_floor_s=_operator_floor(live)` (was `_throughput_floor(cfg, ladder, live)`).

**3c. `LiveSettings`** — add fields and methods. Add the import `from egregore.config.schema import SelectionConfig` (it may already import `EgregoreConfig` from there; extend that line). Add fields after `local_resolution`:

```python
    #: Party-default selection knobs and per-zone overrides, as plain dicts
    #: so a live change can touch one field without rebuilding the model.
    selection: dict = dc_field(default_factory=dict)
    selection_by_zone: dict[str, dict] = dc_field(default_factory=dict)

    def selection_for(self, zone: str) -> SelectionConfig:
        """Zone override on top of the party default, validated."""
        merged = {**self.selection, **self.selection_by_zone.get(zone, {})}
        return SelectionConfig.model_validate(merged)

    def apply_zone_selection(self, zone: str, patch: dict) -> None:
        cur = dict(self.selection_by_zone.get(zone, {}))
        for k, v in patch.items():
            if v is None or v == "":
                cur.pop(k, None)          # clear an override → back to party default
            else:
                cur[k] = v
        SelectionConfig.model_validate({**self.selection, **cur})   # reject before storing
        self.selection_by_zone[zone] = cur
```

In `from_config`, add:
```python
            selection=cfg.weaver.selection.model_dump(),
            selection_by_zone={
                z.id: z.selection.model_dump(exclude_unset=True)
                for z in cfg.zones if z.selection is not None
            },
```

In `apply()`, add after the `aesthetic` block:
```python
        sel = (overrides.get("weaver") or {}).get("selection") or {}
        for k in ("salience", "novelty", "recency", "segment_gap_s",
                  "max_candidates", "recency_tau_s"):
            if k in sel:
                raw = sel[k]
                self.selection[k] = None if raw in (None, "") and k == "recency_tau_s" else raw
                changed.append(f"weaver.selection.{k}")
        if any(c.startswith("weaver.selection.") for c in changed):
            SelectionConfig.model_validate(self.selection)   # a bad blend must not be stored
```

**3d. `ZonePipeline.__init__`**: replace `self.throttled = 0` with:
```python
        #: Loop ticks where spacing was satisfied but the worker was busy —
        #: the number that says "the GPU is the bottleneck".
        self.waited_for_slot = 0
        #: How the last clip was chosen. Counts and scores only.
        self.last_selection: dict | None = None
        #: When the winning segment ended, so the landing clip can report
        #: how far behind the room it is.
        self._lag_anchor: float | None = None
```

**3e. `on_clip`**:
```python
    async def on_clip(self, clip: ClipRef) -> None:
        await self.loom.ingest(clip, clip.path)
        self.state.set_manifest(self.zone, self.loom.manifest())
        if self._lag_anchor is not None and self.last_selection is not None:
            # Wall-clock domain: the ring stamps with time.monotonic, so the
            # anchor is monotonic too.
            self.last_selection["lag_s"] = round(time.monotonic() - self._lag_anchor, 1)
            self._lag_anchor = None
```

**3f. `_generation_loop`** — replace the body from `while True:` through the `if not due and not fill: continue` block and the prompt path. The full new loop:

```python
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
                sel_cfg = self.live.selection_for(self.zone)
                segments = self.ring.segments(sel_cfg.segment_gap_s)
                window_tokens = sum(s.tokens for s in segments)
                borrowed: ThemeObject | None = None
                if window_tokens < self.weaver.min_window_tokens:
                    # Zone-to-zone bleed (L-7): a quiet or dead zone dreams
                    # on a neighbouring zone's most recent validated theme.
                    borrowed = self.bus.borrow_theme(self.zone)
                if borrowed is not None:
                    prompt = synthesize_prompt(
                        borrowed,
                        self.live.grammar,
                        self.loom.continuity_context(),
                        self.live.drift,
                        self.mood.state(),
                        abstraction=self.live.abstraction,
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
                        free_only=fill,
                    )
                    continue

                theme: ThemeObject | None = None
                fallback = False
                if window_tokens >= self.weaver.min_window_tokens:
                    candidates = await self.weaver.weave_candidates(
                        segments, mood=self.mood.state(),
                        max_candidates=sel_cfg.max_candidates,
                    )
                    if candidates:
                        now = time.monotonic()
                        tau = sel_cfg.recency_tau_s or self._last_render_s()
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
                                weights=Weights(sel_cfg.salience, sel_cfg.novelty,
                                                sel_cfg.recency),
                                now=now, tau_s=tau,
                            )
                            winner = selection.winner
                            listened = selection.listened_s
                            winner_score = selection.scored[0].score
                            scored_out = [{
                                "motifs": list(s.candidate.theme.motifs),
                                "elemental": list(s.candidate.theme.elemental),
                                "salience": round(s.salience, 3),
                                "novelty": round(s.novelty, 3),
                                "recency": round(s.recency, 3),
                                "score": round(s.score, 3),
                                "winner": s.candidate is winner,
                            } for s in selection.scored]
                        theme = winner.theme
                        self.last_selection = {
                            "candidates": len(candidates),
                            "winner_score": round(winner_score, 3),
                            "listened_s": round(listened, 1),
                            "lag_s": None,
                            "scored": scored_out,
                        }
                        self._lag_anchor = winner.ended_at
                        prompt = synthesize_prompt(
                            theme, self.live.grammar, self.loom.continuity_context(),
                            self.live.drift, self.mood.state(),
                            abstraction=self.live.abstraction,
                        )
                        self.weaver.prompts_synthesized += 1
                if theme is None:
                    # Nothing survived per-segment validation, or the window is
                    # thin: the whole-window path, which may purge, exactly as
                    # before.
                    result = await self.weaver.weave(
                        self.ring.snapshot(),
                        grammar=self.live.grammar,
                        drift=self.live.drift,
                        mood=self.mood.state(),
                        continuity=self.loom.continuity_context(),
                        abstraction=self.live.abstraction,
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
                    self.last_selection = None
                    self._lag_anchor = None
                # The outbound prompt is the one string this system is
                # willing to send to a third party, so showing it to the
                # operator is strictly safer than what already happens to it.
                self.last_prompt = prompt
                self.last_prompt_at = time.time()
                if not fill:
                    # A fill must not reset the paid cadence, or the budget
                    # would never be spent at all once filling starts.
                    self.governor.record_generation(self.zone)
                await self.forge.request(
                    zone=self.zone,
                    prompt=prompt,
                    duration_s=self.live.clip_duration_s,
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

    def _last_render_s(self) -> float:
        """The preferred backend's learned render time, for the recency tau."""
        try:
            return self.forge.backends[0].estimated_latency(
                self.cfg.generation.model
            ).total_seconds()
        except Exception:
            return 0.0
```

Add to the app.py imports: `from egregore.weaver import Weaver, Weights, build_abstractor, select, synthesize_prompt`.

**3g. `status()`**: replace `"throttled": self.throttled,` with:
```python
            "in_flight": self.forge.in_flight(self.zone),
            "waited_for_slot": self.waited_for_slot,
            "last_selection": (
                {k: v for k, v in self.last_selection.items() if k != "scored"}
                if self.last_selection else None
            ),
```

**3h. `_monitor`**: add to each zone dict:
```python
                        "candidates": (pipe.last_selection or {}).get("scored", []),
                        "listened_s": (pipe.last_selection or {}).get("listened_s"),
```

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest -q`
Expected: all pass. If `test_cadence_floor_override_is_live` or any freeze/mute/bleed drill fails, the loop rewrite broke a contract — fix the loop, not the test.

- [ ] **Step 5: Commit**

```bash
git add egregore/app.py tests/test_integration.py
git commit -m "feat: pull scheduling — one render in flight, prompt chosen from what was said meanwhile"
```

---

### Task 7: Per-zone selection over the API

**Files:**
- Modify: `egregore/conductor/state.py` (~line 108: add `zone_settings_handler`)
- Modify: `egregore/conductor/app.py` (`get_zones` ~line 542, `post_zone` ~line 568)
- Modify: `egregore/app.py` (register the handler next to `state.settings_handler`)
- Test: `tests/test_conductor.py`

**Interfaces:**
- Produces: `ConductorState.zone_settings_handler: Callable[[str, dict], None] | None`; `POST /api/zones/{zone}` accepts `{"selection": {salience, novelty, recency, segment_gap_s}}`; `GET /api/zones` returns `selection` per zone.

- [ ] **Step 1: Write the failing test** (append to `tests/test_conductor.py`; use the file's existing app/client fixture — read the top of the file for its name, it is `client` built from `make_app(state)`)

```python
def test_zone_selection_is_accepted_and_forwarded(client, state):
    seen = {}
    state.zone_settings_handler = lambda zone, patch: seen.update({zone: patch})
    state.zone_config.setdefault("main", {})
    r = client.post("/api/zones/main", json={"selection": {"novelty": 0.9, "segment_gap_s": 4}})
    assert r.status_code == 200, r.text
    assert seen == {"main": {"novelty": 0.9, "segment_gap_s": 4.0}}
    assert client.get("/api/zones").json()["zones"]["main"]["selection"]["novelty"] == 0.9


def test_zone_selection_rejects_a_bad_weight(client, state):
    state.zone_config.setdefault("main", {})
    r = client.post("/api/zones/main", json={"selection": {"novelty": 7}})
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_conductor.py -k zone_selection -q`
Expected: FAIL (400 "nothing changeable" / KeyError `selection`).

- [ ] **Step 3: Implement**

`state.py`, next to `settings_handler`:
```python
        #: Per-zone live settings that the pipeline owns (selection weights).
        self.zone_settings_handler: Callable[[str, dict], None] | None = None
```

`conductor/app.py` in `post_zone`, before `if not allowed:`:
```python
        if "selection" in patch:
            raw = patch.get("selection") or {}
            limits = {"salience": (0.0, 1.0), "novelty": (0.0, 1.0),
                      "recency": (0.0, 1.0), "segment_gap_s": (1.0, 60.0)}
            cleaned = {}
            for key, (lo, hi) in limits.items():
                if key not in raw:
                    continue
                if raw[key] in (None, ""):
                    cleaned[key] = None          # clear the zone override
                    continue
                try:
                    v = float(raw[key])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"selection.{key} must be a number"
                    ) from exc
                if not lo <= v <= hi:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"selection.{key} must be between {lo} and {hi}",
                    )
                cleaned[key] = v
            if cleaned:
                if state.zone_settings_handler is not None:
                    try:
                        state.zone_settings_handler(zone, cleaned)
                    except ValueError as exc:
                        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
                current = dict(state.zone_config.get(zone, {}).get("selection") or {})
                for k, v in cleaned.items():
                    if v is None:
                        current.pop(k, None)
                    else:
                        current[k] = v
                allowed["selection"] = current
```

In `get_zones`, add to the per-zone dict:
```python
                "selection": client.get("selection", {}),
```
Note: `get_config(zone)` builds a fresh dict from `zone_config`; check `ConductorState.get_config` — if it does not pass `selection` through, add `"selection": zone_cfg.get("selection", {}),` to the returned dict in `state.py` (~line 296).

`egregore/app.py` after `state.settings_handler = _apply_settings`:
```python
    def _apply_zone_settings(zone: str, patch: dict) -> None:
        live.apply_zone_selection(zone, patch)
        log.info("zone %s: selection changed: %s", zone, ", ".join(sorted(patch)))

    state.zone_settings_handler = _apply_zone_settings
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_conductor.py -q && uv run ruff check .`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add egregore/conductor egregore/app.py tests/test_conductor.py
git commit -m "feat(conductor): per-zone selection weights over the zones API"
```

---

### Task 8: Dashboard — sliders, candidates, lag

**Files:**
- Modify: `lens/setup.html` (`renderZones` ~line 906 pacing array; `renderMonitor` ~line 713; the zone status line — find where `pool`/`clip_count` is rendered in the status panel)

No unit tests for the page; verified by loading it in Task 9.

- [ ] **Step 1: Sliders.** In `renderZones`, after the pacing `forEach` block, add a second block:

```js
      // --- what gets rendered next, applied live ---
      //
      // When a render finishes the room has usually said several things.
      // These decide which one becomes the next clip.
      var sel = z.selection || {};
      [['salience', 'dwelt on', 0, 1, 0.05, sel.salience,
        'what the room spent the most words on'],
       ['novelty', 'new', 0, 1, 0.05, sel.novelty,
        'furthest from what was just shown'],
       ['recency', 'fresh', 0, 1, 0.05, sel.recency,
        'what was said most recently'],
       ['segment_gap_s', 'pause', 1, 30, 1, sel.segment_gap_s,
        'seconds of silence that end one thought and start the next']
      ].forEach(function (spec) {
        var key = spec[0], name = spec[1];
        var fallback = key === 'segment_gap_s' ? 6 :
          ({salience: 0.5, novelty: 0.3, recency: 0.2})[key];
        var row = document.createElement('div');
        row.className = 'row';
        var lab = document.createElement('label');
        lab.className = 'key'; lab.textContent = name;
        var r = document.createElement('input');
        r.type = 'range'; r.min = spec[2]; r.max = spec[3]; r.step = spec[4];
        r.value = (spec[5] == null ? fallback : spec[5]); r.style.minWidth = '220px';
        var num = document.createElement('span');
        num.className = 'val'; num.style.minWidth = '6ch';
        num.textContent = (+r.value).toFixed(2);
        var tag = document.createElement('span');
        tag.className = 'tag live'; tag.textContent = 'live';
        var note = document.createElement('span');
        note.className = 'off'; note.textContent = spec[6];
        r.addEventListener('input', function () { num.textContent = (+r.value).toFixed(2); });
        r.addEventListener('change', function () {
          var body = { selection: {} }; body.selection[key] = +r.value;
          fetch('/api/zones/' + encodeURIComponent(zone), {
            method: 'POST', credentials: 'same-origin',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(body)
          });
        });
        row.appendChild(lab); row.appendChild(r); row.appendChild(num);
        row.appendChild(tag); row.appendChild(note);
        box.appendChild(row);
      });
```

- [ ] **Step 2: Candidates in the monitor.** In `renderMonitor`, after the prompt-themes block (`el.monitor.appendChild(p);`), add:

```js
          if (z.candidates && z.candidates.length) {
            var ch = document.createElement('div');
            ch.className = 'row';
            var cl = document.createElement('span');
            cl.className = 'off';
            cl.textContent = 'listened ' + Math.round(z.listened_s || 0) + 's · '
              + z.candidates.length + ' candidate(s)';
            ch.appendChild(cl);
            el.monitor.appendChild(ch);
            z.candidates.forEach(function (c) {
              var row = document.createElement('div');
              row.className = 'transcript' + (c.winner ? ' prompt-themes' : '');
              var bars = ['salience', 'novelty', 'recency'].map(function (k) {
                var n = Math.round((c[k] || 0) * 8);
                return k.slice(0, 3) + ' ' + '█'.repeat(n) + '░'.repeat(8 - n);
              }).join('  ');
              row.textContent = (c.winner ? '▶ ' : '  ')
                + (c.motifs || []).join('; ')
                + (c.elemental && c.elemental.length ? ' — ' + c.elemental.join(', ') : '')
                + '\n   ' + bars + '   ' + (c.score || 0).toFixed(2);
              el.monitor.appendChild(row);
            });
          }
```

- [ ] **Step 3: Lag in the status line.** Find where the zone status renders clip counts (search `clip_count` in setup.html). Append, guarded:

```js
      if (zs.last_selection && zs.last_selection.lag_s != null) {
        meta.textContent += ' · lag ' + Math.round(zs.last_selection.lag_s) + 's';
      }
      if (zs.waited_for_slot) {
        meta.textContent += ' · waited ' + zs.waited_for_slot;
      }
```
(where `zs` is the zone's status object and `meta` the span already holding the pool count — adapt the variable names to what is there.)

- [ ] **Step 4: Preset and README.** In `presets/local-party.yaml` add under `weaver:` (create the block if absent):

```yaml
weaver:
  selection:
    salience: 0.5
    novelty: 0.2        # lower than default: a continuity chain wants coherence
    recency: 0.3
    segment_gap_s: 6.0
```

In `README.md`, in the live/restart table add a row `| **what gets rendered next (dwelt on / new / fresh / pause)** | |`, and after the "Tuning local generation" section add:

```markdown
### What gets rendered next

A clip is asked for only when the previous one has finished — never queued
behind it. So the prompt is always written from what the room said *during*
the last render, and imagery is one render behind the conversation, not a
compounding backlog of it.

When the render finishes, everything said meanwhile is split at pauses into
separate thoughts, each is abstracted and validated on its own, and one is
chosen by a blend of three signals you can set per zone, live:

| slider | what it favours |
|---|---|
| dwelt on | the thought the room spent the most words on |
| new | the thought furthest from what was just shown |
| fresh | the thought said most recently |

The monitor panel shows every candidate with its scores and marks the winner,
and the status line shows the measured lag from the last word of the winning
thought to the clip landing. In continuity mode a high "new" weight pulls
against the chain's coherence; the `local-party` preset sets it low.
```

- [ ] **Step 5: Lint, commit**

```bash
uv run ruff check . && uv run pytest -q
git add lens/setup.html presets/local-party.yaml README.md
git commit -m "feat(lens): selection sliders per zone, candidates in the monitor, lag in status"
```

---

### Task 9: Privacy sweep and live verification

**Files:**
- Test: `tests/test_privacy.py` — no code change expected; the fixture party now runs the candidate path. Run it and confirm it still passes: `uv run pytest tests/test_privacy.py -q`. If a sentinel appears in status or the monitor, the leak is in `scored_out` (motifs are validated, so this must not happen — investigate `validate_theme` coverage, do not filter output).

- [ ] **Step 1: Full suite + lint**

Run: `uv run ruff check . && uv run pytest -q`
Expected: all pass (≈ 425).

- [ ] **Step 2: Live run on this machine (30 min).** Kill any running party, clear ComfyUI's queue, then:

```bash
cd "<repo>"
SP=<scratchpad>
/usr/bin/curl -s -X POST http://127.0.0.1:8188/interrupt >/dev/null
pkill -INT -f "egregore run"; sleep 4; rm -rf var
EGREGORE_COMFY_WORKFLOW="$PWD/presets/comfyui/ltxv-2b-balanced.json" \
EGREGORE_COMFY_SEED_WORKFLOW="$PWD/presets/comfyui/ltxv-2b-seeded.json" \
EGREGORE_MONITOR=1 PYTHONUNBUFFERED=1 \
nohup uv run egregore run presets/local-party.yaml --ignore-settings > $SP/pull.log 2>&1 &
```

Poll every 30s for 30 minutes and record, per local clip:
```bash
/usr/bin/curl -s localhost:8420/api/status | /usr/bin/python3 -c "
import sys,json;z=json.load(sys.stdin)['zones']['main']
s=z.get('last_selection') or {}
print(f\"in_flight={z['in_flight']} queue={z['queue_depth']} waited={z['waited_for_slot']} \"
      f\"cands={s.get('candidates')} listened={s.get('listened_s')} lag={s.get('lag_s')} \"
      f\"chain={z['current_chain_length']}/{z['max_chain_length']}\")"
```

Acceptance (from the spec §5):
- `lag_s` ≤ (render wall from the log's `local clip ... wall=`) + 5s on every local clip
- `queue_depth` never exceeds 1
- candidate counts vary with the conversation
- `seeded=True` still appears in the log for mid-chain clips
- Open `http://<lan-ip>:8420/setup.html`: sliders present; move "new" to 0.9 on `main`, confirm the log line `zone main: selection changed: novelty` and that the next candidates list re-ranks.

- [ ] **Step 3: Commit anything the live run changed, and push**

```bash
git add -A && git commit -m "fix: <whatever the live run taught>"   # only if needed
git push origin main
```
