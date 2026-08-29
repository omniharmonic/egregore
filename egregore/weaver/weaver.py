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

import asyncio
import contextlib
import hashlib
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
        stage1_budget_s: float = 10.0,
        cache_size: int = 64,
        max_slow_calls: int = 3,
    ) -> None:
        self.abstractor: Abstractor = abstractor or HeuristicAbstractor()
        self.min_window_tokens = min_window_tokens
        self.max_attempts = max(1, max_attempts)
        #: How literal the party wants to be; steers an LLM's stage 1.
        self.abstraction = 1.0
        #: How long a render may wait on stage 1 for an uncached thought
        #: before the heuristic stands in.
        self.stage1_budget_s = float(stage1_budget_s)
        # Background abstraction. A closed stretch of speech is stable, so
        # its theme can be worked out while the GPU is busy and be ready the
        # instant a render slot opens. Keys are digests, never text.
        self._cache: dict[str, ThemeObject | None] = {}
        self._cache_order: list[str] = []
        self._cache_size = int(cache_size)
        self._queue: list[tuple[str, str, MoodState | None]] = []
        self._pending: set[str] = set()
        self._worker: asyncio.Task | None = None
        self._heuristic = HeuristicAbstractor()
        # A brain that keeps missing the budget is stood down. Measured: a
        # 27B model sharing the GPU with the renderer took 175s a call and
        # stretched a 100s render to 462s — the wall went procedural while
        # it thought. After max_slow_calls consecutive misses the heuristic
        # takes over for the rest of the party, and status says why.
        self.max_slow_calls = int(max_slow_calls)
        self._slow_streak = 0
        self._stood_down: str | None = None
        # Counters are content-blind and safe to surface in ZoneStatus.
        self.cycles = 0
        self.prompts_synthesized = 0
        self.rejections = 0
        self.purges_requested = 0
        self.fallbacks = 0
        self.last_theme: ThemeObject | None = None  # thematic memory (T-5)

    @property
    def engine_name(self) -> str:
        """What is writing the themes right now, for the status page."""
        if self._stood_down:
            return f"heuristic ({self._stood_down})"
        return getattr(self.abstractor, "name", "heuristic")

    def _note_call(self, seconds: float, *, timed_out: bool) -> None:
        if self._stood_down or isinstance(self.abstractor, HeuristicAbstractor):
            return
        if timed_out or seconds > self.stage1_budget_s:
            self._slow_streak += 1
            if self._slow_streak >= self.max_slow_calls:
                self._stood_down = (
                    f"{getattr(self.abstractor, 'name', 'llm')} too slow for this machine: "
                    f"{self._slow_streak} calls over {self.stage1_budget_s:.0f}s"
                )
                log.warning("weaver: %s — the heuristic takes over", self._stood_down)
                self.abstractor = self._heuristic
                self._queue.clear()
        else:
            self._slow_streak = 0
        # Counters are content-blind and safe to surface in ZoneStatus.

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
                self._steer()
                theme = await self.abstractor.abstract(window_text, mood, attempt=attempt - 1)
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

    # -- background abstraction ----------------------------------------------

    def _steer(self) -> None:
        """Tell a brain that can be steered how literal to be. The protocol
        stays as it was; a brain without the attribute is simply not steered."""
        if hasattr(self.abstractor, "abstraction"):
            self.abstractor.abstraction = self.abstraction

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    def cached(self, segment) -> ThemeObject | None:
        """The validated theme for a segment, if it has been worked out."""
        return self._cache.get(self._key(segment.text))

    def prime(self, segments, mood: MoodState | None = None) -> int:
        """Queue every *closed* segment for background abstraction.

        The last segment may still grow as the room keeps talking, so it is
        left for the moment a render actually needs it. Newest first, so a
        slow brain spends its time on what the next clip is likeliest to be
        about. Returns how many were queued.
        """
        closed = list(segments)[:-1]
        queued = 0
        for s in reversed(closed):
            k = self._key(s.text)
            if k in self._cache or k in self._pending:
                continue
            self._pending.add(k)
            self._queue.append((k, s.text, mood))
            queued += 1
        if queued and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._work())
        return queued

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for the background queue to empty (tests and shutdown)."""
        if self._worker is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._worker), timeout)

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def _work(self) -> None:
        while self._queue:
            k, text, mood = self._queue.pop()      # newest first
            theme: ThemeObject | None = None
            try:
                self._steer()
                started = asyncio.get_event_loop().time()
                candidate = await self.abstractor.abstract(text, mood, attempt=0)
                self._note_call(asyncio.get_event_loop().time() - started, timed_out=False)
                if validate_theme(candidate, text).ok:
                    theme = candidate
                else:
                    self.rejections += 1
            except AbstractionError as exc:
                # The message is content-free by construction (endpoint
                # error class or parse failure), so it is safe to log and is
                # the only way to tell a timeout from a bad reply.
                log.warning("weaver background stage-1 failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken brain must not kill the worker
                log.exception("weaver background stage-1 error")
            self._remember(k, theme)
            self._pending.discard(k)

    def _remember(self, k: str, theme: ThemeObject | None) -> None:
        self._cache[k] = theme
        self._cache_order.append(k)
        while len(self._cache_order) > self._cache_size:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)

    async def _abstract_within_budget(self, text: str, mood: MoodState | None) -> ThemeObject:
        """Stage 1 for a thought no one worked out in advance.

        Bounded, so a slow LLM costs quality on this one thought rather than
        lag on the whole wall: past the budget the heuristic answers instead.
        """
        self._steer()
        started = asyncio.get_event_loop().time()
        try:
            theme = await asyncio.wait_for(
                self.abstractor.abstract(text, mood, attempt=0),
                timeout=self.stage1_budget_s,
            )
        except TimeoutError:
            self._note_call(self.stage1_budget_s, timed_out=True)
            log.warning("weaver stage-1 over budget (%.0fs); heuristic stands in",
                        self.stage1_budget_s)
            return await self._heuristic.abstract(text, mood, attempt=0)
        self._note_call(asyncio.get_event_loop().time() - started, timed_out=False)
        return theme

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
            k = self._key(s.text)
            if k in self._cache:
                theme = self._cache[k]
                if theme is None:
                    continue                       # rejected in the background
            else:
                try:
                    theme = await self._abstract_within_budget(s.text, mood)
                except AbstractionError:
                    log.warning("weaver candidate stage-1 failed")
                    continue
                verdict = validate_theme(theme, s.text)
                if not verdict.ok:
                    self.rejections += 1
                    log.warning("weaver candidate rejected", extra={"reasons": verdict.reasons})
                    self._remember(k, None)
                    continue
                self._remember(k, theme)
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
