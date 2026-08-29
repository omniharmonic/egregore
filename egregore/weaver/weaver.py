"""The Weaver — one full abstraction → validation → synthesis cycle.

Flow (Architecture §2.4):

1. If the window is effectively empty, skip stage 1 entirely and synthesize
   from mood plus thematic memory (degradation ladder: "ASR produces nothing
   (loud room)" must not be guest-visible).
2. Otherwise stage 1 → validator.
3. On rejection, regenerate ONCE with ``attempt=1``.
4. On a second rejection, return ``rejected=True, purge_requested=True,
   prompt=None``. The caller purges that zone's ring buffer and skips the
   cycle silently: if abstraction has failed twice, the safest thing to do
   with the source text is destroy it.

Logging discipline: this module logs outcome flags, reason CODES and counts.
It never logs window text, theme fields, or the synthesized prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from egregore.types import MoodState, ThemeObject

from .abstractor import (
    DEFAULT_MOTIFS,
    AbstractionError,
    Abstractor,
    HeuristicAbstractor,
    fallback_theme,
)
from .select import Candidate
from .synthesis import synthesize_prompt
from .validator import ValidationResult, validate_theme

__all__ = ["MIN_WINDOW_TOKENS", "WeaveResult", "Weaver"]

log = logging.getLogger("egregore.weaver")

MIN_WINDOW_TOKENS = 6


@dataclass
class WeaveResult:
    """Outcome of one cycle. Carries no reference to the window text."""

    prompt: str | None
    theme: ThemeObject | None
    rejected: bool = False
    purge_requested: bool = False
    attempts: int = 0
    fallback: bool = False
    reasons: list[str] = field(default_factory=list)  # validator check names only

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.prompt is not None


class Weaver:
    """Owns one zone's two-stage cycle and its thematic memory."""

    def __init__(
        self,
        abstractor: Abstractor | None = None,
        *,
        min_window_tokens: int = MIN_WINDOW_TOKENS,
        max_attempts: int = 2,
    ) -> None:
        self.abstractor: Abstractor = abstractor or HeuristicAbstractor()
        self.min_window_tokens = min_window_tokens
        self.max_attempts = max(1, max_attempts)
        # Counters are content-blind and safe to surface in ZoneStatus.
        self.cycles = 0
        self.prompts_synthesized = 0
        self.rejections = 0
        self.purges_requested = 0
        self.fallbacks = 0
        self.last_theme: ThemeObject | None = None  # thematic memory (T-5)

    async def weave(
        self,
        window_text: str,
        *,
        grammar: str,
        drift: float = 0.4,
        mood: MoodState | None = None,
        continuity: str | None = None,
        abstraction: float = 1.0,
        room_bias: float = 1.0,
    ) -> WeaveResult:
        self.cycles += 1

        if self._is_effectively_empty(window_text):
            return self._weave_fallback(grammar, drift, mood, continuity, abstraction, room_bias)

        last: ValidationResult | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                theme = await self.abstractor.abstract(
                    window_text, mood, attempt=attempt - 1
                )
            except AbstractionError:
                log.warning("weaver stage-1 failed", extra={"attempt": attempt})
                continue
            last = validate_theme(theme, window_text)
            if last.ok:
                prompt = synthesize_prompt(
                    theme, grammar, continuity, drift, mood,
                    abstraction=abstraction, room_bias=room_bias,
                )
                self.prompts_synthesized += 1
                self.last_theme = theme
                log.info(
                    "weaver cycle accepted",
                    extra={"attempts": attempt, "motif_count": len(theme.motifs)},
                )
                return WeaveResult(prompt=prompt, theme=theme, attempts=attempt)
            self.rejections += 1
            log.warning(
                "weaver theme rejected",
                extra={"attempt": attempt, "reasons": last.reasons},
            )

        # Stage 1 failed twice (or errored twice): purge and skip, silently.
        self.purges_requested += 1
        log.warning(
            "weaver cycle purged",
            extra={
                "attempts": self.max_attempts,
                "reasons": last.reasons if last else ["stage1-error"],
            },
        )
        return WeaveResult(
            prompt=None,
            theme=None,
            rejected=True,
            purge_requested=True,
            attempts=self.max_attempts,
            reasons=list(last.reasons) if last else ["stage1-error"],
        )

    async def weave_candidates(
        self,
        segments,
        *,
        mood: MoodState | None = None,
        max_candidates: int = 6,
    ) -> list[Candidate]:
        """One validated theme per stretch of speech, for the selector.

        Each segment is abstracted and validated against *its own* text, so a
        candidate can never carry a phrase from a neighbouring segment. A
        rejected candidate is dropped and counted; it does not purge — purge
        stays reserved for the whole-window path, which the caller falls back
        to when nothing here survives.

        Returns ``Candidate`` objects only: theme plus the shape of the speech
        (token count, timestamps). No text leaves this method.
        """
        keep = sorted(segments, key=lambda s: s.tokens, reverse=True)
        keep = keep[: max(1, int(max_candidates))]
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
        return _consolidate(out)

    # -- internals --


    def _is_effectively_empty(self, window_text: str) -> bool:
        return len((window_text or "").split()) < self.min_window_tokens

    def _weave_fallback(
        self,
        grammar: str,
        drift: float,
        mood: MoodState | None,
        continuity: str | None,
        abstraction: float = 1.0, room_bias: float = 1.0) -> WeaveResult:
        theme = fallback_theme(mood, self.last_theme)
        prompt = synthesize_prompt(
            theme, grammar, continuity, drift, mood, abstraction=abstraction, room_bias=room_bias)
        self.fallbacks += 1
        self.prompts_synthesized += 1
        log.info("weaver cycle fell back to features", extra={"has_mood": mood is not None})
        return WeaveResult(prompt=prompt, theme=theme, attempts=0, fallback=True)


def _consolidate(cands: list[Candidate]) -> list[Candidate]:
    """Merge candidates that abstracted to the same theme, and keep the
    abstractor's no-match fallback only when nothing else survived.

    Two stretches of speech that map to one theme are one theme the room
    dwelt on twice: their words add up, which is what salience means. And a
    sentence the abstractor could not place — it returns ``DEFAULT_MOTIFS``
    for those — must never outrank one it could, which in the first soak it
    did, repeatedly.
    """
    real = [c for c in cands if tuple(c.theme.motifs) != DEFAULT_MOTIFS]
    pool = real or cands
    merged: dict[frozenset[str], Candidate] = {}
    for c in pool:
        key = frozenset(m.strip().lower() for m in (*c.theme.motifs, *c.theme.elemental))
        prev = merged.get(key)
        if prev is None:
            merged[key] = c
        else:
            merged[key] = Candidate(
                theme=prev.theme if prev.ended_at >= c.ended_at else c.theme,
                tokens=prev.tokens + c.tokens,
                ended_at=max(prev.ended_at, c.ended_at),
                started_at=min(prev.started_at, c.started_at),
            )
    return sorted(merged.values(), key=lambda c: c.started_at)
