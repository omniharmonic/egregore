"""WEAVER — two-stage theme extraction, validation gate, prompt synthesis.

Stage 1 (``abstractor``) is the only code that reads raw window text.
The validator (``validator``) is the last line of defense between it and
everything else. Stage 2 (``synthesis``) is a pure function of the validated
theme object and cannot see raw text by construction.

    weaver = Weaver()                       # heuristic stage 1, no LLM needed
    result = await weaver.weave(text, grammar=cfg.aesthetic.grammar,
                                drift=cfg.aesthetic.drift, mood=mood)
    if result.purge_requested:
        ring_buffer.purge()                 # twice-failed abstraction: destroy source
    elif result.prompt:
        await backend.generate(result.prompt, ...)
"""

from __future__ import annotations

from .abstractor import (
    CONCEPT_CLUSTERS,
    REGISTERS,
    STAGE1_SYSTEM_PROMPT,
    AbstractionError,
    Abstractor,
    HeuristicAbstractor,
    LLMAbstractor,
    build_abstractor,
    fallback_theme,
    lexicon_vocabulary,
    theme_from_payload,
)
from .select import (
    MEMORY_DEPTH,
    MIN_TAU_S,
    Candidate,
    ScoredCandidate,
    Selection,
    Weights,
    select,
)
from .synthesis import SAFETY_FLOOR, SAFETY_FLOOR_HEADER, synthesize_prompt
from .validator import GAZETTEER, ValidationResult, validate_theme
from .weaver import MIN_WINDOW_TOKENS, Weaver, WeaveResult

__all__ = [
    "MEMORY_DEPTH",
    "MIN_TAU_S",
    "Candidate",
    "ScoredCandidate",
    "Selection",
    "Weights",
    "select",
    "CONCEPT_CLUSTERS",
    "GAZETTEER",
    "MIN_WINDOW_TOKENS",
    "REGISTERS",
    "SAFETY_FLOOR",
    "SAFETY_FLOOR_HEADER",
    "STAGE1_SYSTEM_PROMPT",
    "AbstractionError",
    "Abstractor",
    "HeuristicAbstractor",
    "LLMAbstractor",
    "ValidationResult",
    "WeaveResult",
    "Weaver",
    "build_abstractor",
    "fallback_theme",
    "lexicon_vocabulary",
    "synthesize_prompt",
    "theme_from_payload",
    "validate_theme",
]
