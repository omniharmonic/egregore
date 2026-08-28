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

from .abstractor import AbstractionError, Abstractor, HeuristicAbstractor, fallback_theme
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
    ) -> WeaveResult:
        self.cycles += 1

        if self._is_effectively_empty(window_text):
            return self._weave_fallback(grammar, drift, mood, continuity)

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
                prompt = synthesize_prompt(theme, grammar, continuity, drift, mood)
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

    # -- internals --

    def _is_effectively_empty(self, window_text: str) -> bool:
        return len((window_text or "").split()) < self.min_window_tokens

    def _weave_fallback(
        self,
        grammar: str,
        drift: float,
        mood: MoodState | None,
        continuity: str | None,
    ) -> WeaveResult:
        theme = fallback_theme(mood, self.last_theme)
        prompt = synthesize_prompt(theme, grammar, continuity, drift, mood)
        self.fallbacks += 1
        self.prompts_synthesized += 1
        log.info("weaver cycle fell back to features", extra={"has_mood": mood is not None})
        return WeaveResult(prompt=prompt, theme=theme, attempts=0, fallback=True)
