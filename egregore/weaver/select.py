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
    return frozenset(
        s.strip().lower() for s in (*theme.motifs, *theme.elemental) if s.strip()
    )


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
