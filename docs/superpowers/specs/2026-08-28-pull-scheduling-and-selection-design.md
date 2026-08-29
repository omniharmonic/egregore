# Pull Scheduling and Theme Selection — Design

**Status:** approved in conversation, 2026-08-28
**Scope:** stage A of a three-stage pass (A: this; B: theme-driven shader
parameters; C: seamlessness and reliability polish). Each stage is a separate
spec, plan, and live verification.

## Problem

Generation is clock-driven. Every second the pipeline asks the Governor "is it
time?", where the interval is `max(budget cadence, 60s floor, observed render
latency)`. When due it snapshots the ring buffer, synthesises a prompt, and
enqueues the job at once. A serial worker per zone drains the queue, capped at
depth 3.

The prompt is bound at *enqueue*, but the render begins only when the worker
reaches the job. With one to three jobs ahead and ~100s local renders, a prompt
can describe the room 100–300s before its render starts, plus the render
itself. Lag compounds toward ~400s, and the latency estimate is seeded at 60s,
so the early part of every party over-enqueues. The 6-minute ring buffer holds
plenty of material; the system never chooses from it — it snapshots the whole
window and abstracts once.

Measured on 2026-08-28: local LTX at 8 steps / 512×320 renders a 4s clip in
96–110s. The material the room produced during that render is exactly what the
next clip should be about.

## Goals

1. Lag is bounded at one render. No clip is ever rendered from a prompt older
   than the render that preceded it.
2. When a render completes, the system looks at everything said meanwhile and
   picks the best theme, where *best* is a tunable blend of salience, novelty
   and recency.
3. Every knob is configuration, live-settable, per zone, with a party default.
4. No change to the privacy invariants (PRD 6.8): transcript text exists only
   in the ring buffer and transiently in weaver stage 1; only validated
   abstractions are scored, shown, or sent.
5. Nothing that works today stops working: the fill lane, zone bleed, freeze,
   purge, continuity chains, mirror/commons topologies, budget ceiling.

## Non-goals

- Late-binding the prompt inside the Forge worker (a "prompt provider"). With
  the queue always empty the gap between enqueue and dispatch is a health
  probe and a reservation — milliseconds. Kept as a future option for a
  remote/DGX split where network queuing is real.
- Moving the loop into the Forge. The Forge stays content-blind.
- Shader parameter driving from themes (stage B).

## 1. Scheduling — pull by idleness

The trigger for a paid or local render becomes:

    worker idle for this zone   AND   Governor spacing satisfied

where *idle* means nothing in flight and nothing queued on the zone's paid
lane, and *spacing* is the Governor's existing budget cadence and
`min_interval_s`. Money is still paced by the Governor; throughput no longer
is.

Changes:
- `Forge` exposes `in_flight(zone) -> int` alongside `queue_depth(zone)`. The
  loop requests only when both are zero.
- `Governor` no longer consumes `throughput_floor_s`. The constructor
  parameter is removed from `Governor.from_config`; `app._throughput_floor()`
  is deleted. The latency EWMA on each backend stays, for the dashboard and
  for the fill lane's `FILL_MAX_LATENCY_S` check.
- `_MAX_QUEUE_DEPTH` and the `throttled` counter are removed. A new counter,
  `waited_for_slot`, records loop ticks where spacing was satisfied but the
  worker was busy — the number that says "the GPU is the bottleneck".
- The fill lane is unchanged. It has its own free-only worker and covers an
  empty pool at party start and gaps between renders.
- `LiveSettings.cadence_floor_s` stays: it is an operator-set minimum spacing,
  a different thing from the measured throughput.

Consequence: the paid queue depth is always 0 or 1 (the job being rendered).
Lag from "last word of the winning segment" to "clip on disk" is one render.

## 2. Selection — `egregore/weaver/select.py`

### Segmentation (scribe side)

`RingBuffer.segments(gap_s: float) -> list[Segment]` splits the current
window at pauses of at least `gap_s` between consecutive fragment timestamps.
`Segment` is a frozen dataclass `(text: str, started_at: float, ended_at:
float, tokens: int)` with a redacted `repr`, living beside `TextFragment` in
`scribe`. Like `snapshot()`, it evicts first and never clears. Text crosses
the same boundary `snapshot()` already crosses: into weaver stage 1, nowhere
else.

### Candidates (weaver side)

`Weaver.weave_candidates(segments, *, mood, max_candidates) ->
list[Candidate]`:
- Keeps the `max_candidates` longest segments by token count (default 6).
- Abstracts each with the existing abstractor and validates it **against its
  own segment text** with the existing `validate_theme`. A rejected candidate
  is dropped, counted in `Weaver.rejections`, and does not trigger a purge —
  purge stays reserved for the whole-window path, which is the fallback when
  every candidate is rejected.
- Returns `Candidate(theme: ThemeObject, tokens: int, ended_at: float)`.
  `Candidate` carries no text.

### Scoring

`select(candidates, *, memory, on_screen, weights, now, tau_s) -> Selection`
is a pure function:

- `salience_i = tokens_i / Σ tokens`
- `novelty_i = 1 − max_j jaccard(bag_i, bag_j)` over `j` in the last 5
  themes of `memory` plus `on_screen` (the theme of the clip currently
  playing, if known). `bag` = set of lower-cased motifs ∪ elemental. With no
  memory, novelty is 1.
- `recency_i = exp(−(now − ended_at_i) / tau_s)`
- `score_i = w_s·salience_i + w_n·novelty_i + w_r·recency_i`, weights
  normalised to sum to 1 at scoring time so the sliders need not.
- Ties resolve to the most recent candidate.
- `Selection(winner: Candidate, scored: list[ScoredCandidate], listened_s:
  float)` where `listened_s = now − min(started_at)` and `ScoredCandidate`
  carries the candidate plus its four numbers.

`tau_s` defaults to the last render's wall time for the zone (the backend's
latency EWMA at the moment of selection), floored at 30s. Material said
during the render is weighted near 1; material older than two renders fades
below 0.15.

### The loop

When idle and spaced:

1. `segments = ring.segments(gap_s)`; if total tokens `< min_window_tokens`,
   take the existing fallback/bleed path exactly as today.
2. `candidates = await weaver.weave_candidates(...)`; if empty, take the
   existing whole-window `weave()` path (which may purge) exactly as today.
3. If one candidate, it wins without scoring.
4. Otherwise `selection = select(...)`; synthesise the winner's theme through
   the existing `synthesize_prompt` with grammar, drift, mood, continuity
   context and abstraction, unchanged.
5. `loom.remember_theme(winner.theme)`, record the generation, request.
6. Store `last_selection` on the pipeline for status and the monitor.

In continuity mode the winner's `movement` still flows through
`continuity_context()` as today. The novelty weight can pull against a
chain's coherence; that is a real trade and the slider is the operator's.
The `local-party` preset sets novelty 0.2.

## 3. Configuration

Additive extension to the frozen `schema.py` contract:

```python
class SelectionConfig(BaseModel):
    salience: float = Field(0.5, ge=0, le=1)
    novelty: float = Field(0.3, ge=0, le=1)
    recency: float = Field(0.2, ge=0, le=1)
    segment_gap_s: float = Field(6.0, ge=1.0, le=60.0)
    max_candidates: int = Field(6, ge=1, le=12)
    recency_tau_s: float | None = Field(None, ge=5.0)   # None = last render

class WeaverConfig(BaseModel):
    ...
    selection: SelectionConfig = Field(default_factory=SelectionConfig)

class ZoneConfig(BaseModel):
    ...
    selection: SelectionConfig | None = None   # overrides weaver.selection
```

A validator rejects all three weights being zero. Zones inherit through the
existing `inherit()` helper. Live keys added to `store.LIVE_KEYS`:
`weaver.selection.{salience,novelty,recency,segment_gap_s,max_candidates,
recency_tau_s}` and the per-zone form under `zones[].selection.*` routed
through `set_zone_config` like `playback_rate`.

## 4. Operator surface

- **Zones panel:** three sliders (salience / novelty / recency, 0–1, step
  0.05) beside pacing, plus "pause between thoughts" (`segment_gap_s`, 1–30s).
  `max_candidates` and `recency_tau_s` are in the settings panel, not per
  zone — they are rarely touched.
- **Monitor panel (EGREGORE_MONITOR=1):** a *candidates* list under the
  transcript: each candidate's motifs, the three sub-scores as small bars,
  total score, winner marked, and "listened 104s · 4 candidates". Motifs are
  validated abstractions, already shown today as the prompt.
- **Zone status (always):** `last_selection: {candidates: int, winner_score:
  float, listened_s: float, lag_s: float | None}` where `lag_s` is filled in
  when the clip lands: `clip.created_at − winner.ended_at`. This is the
  number that proves the system is contemporaneous.
- **Dashboard status line:** "lag 108s" next to the zone's pool size.

## 5. Reliability and verification

Unit tests:
- `segments()`: split at gap, no split under gap, single fragment, empty,
  eviction still applies, `Segment.repr` is redacted.
- `select()`: weights normalise; salience alone picks the longest; recency
  alone picks the newest; novelty alone avoids memory; tie → most recent;
  `tau` floor; one candidate short-circuits; no-memory novelty is 1.
- `weave_candidates()`: cap keeps longest; rejected candidate dropped without
  purge; `Candidate` has no text attribute.
- Config: defaults, zero-weights rejected, zone override wins, live apply
  changes the running weights, dotted and nested forms both accepted.
- Loop: no request while `in_flight > 0`; request on the tick after
  completion; fill lane still fills a thin pool during a render; freeze still
  holds; bleed still fires on an empty window.
- Privacy: the existing "no transcript text in any log record" harness runs
  over a party using the candidate path with a sentinel phrase in the window.

Live verification on this machine, real microphone, `local-party` preset,
30 minutes:
- `lag_s` ≤ render wall + 5s on every clip.
- Paid queue depth never exceeds 1.
- Candidate counts vary with the conversation (1 during monologue, several
  during back-and-forth).
- Continuity chain still builds seeded clips (`seeded=True` in the log).

## Files

- Create: `egregore/weaver/select.py`, `tests/test_select.py`
- Modify: `egregore/scribe/ring.py` (+`Segment`, `segments()`),
  `egregore/weaver/weaver.py` (+`weave_candidates`),
  `egregore/forge/forge.py` (+`in_flight`), `egregore/governor/governor.py`
  (−`throughput_floor_s`), `egregore/config/schema.py` (+`SelectionConfig`),
  `egregore/config/store.py` (live keys), `egregore/app.py` (loop, status,
  live apply), `egregore/conductor/state.py` (zone config passthrough),
  `lens/setup.html` (sliders, monitor candidates), `presets/local-party.yaml`,
  `README.md`, `CONTRACTS.md` (note the additive extension).
