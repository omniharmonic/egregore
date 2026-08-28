"""Weaver stage 1 — abstraction.

Stage 1 is the only place in the system that reads raw window text. Its job
is to emit a ``ThemeObject`` and nothing else. Two implementations:

``HeuristicAbstractor``
    Deterministic, no LLM, used in demo mode and whenever no endpoint is
    configured. Its output vocabulary is a closed lexicon: input words are
    *matched against* keywords but never copied into the output, which makes
    this path structurally leak-proof (the validator still runs — defense in
    depth, not defense instead of).

``LLMAbstractor``
    Calls an OpenAI-compatible ``/chat/completions`` endpoint (llama.cpp,
    vLLM, …) with a strict schema prompt and parses the reply defensively.
    Model-mediated, therefore *not* leak-proof — that is exactly why the
    validator exists.

Neither implementation logs window text, embeds it in exceptions, or keeps it
after the call returns.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from egregore.config.schema import WeaverConfig
from egregore.types import MoodState, ThemeObject

from .validator import char_runs, normalize_words, word_ngrams

__all__ = [
    "Abstractor",
    "AbstractionError",
    "CONCEPT_CLUSTERS",
    "HeuristicAbstractor",
    "LLMAbstractor",
    "REGISTERS",
    "STAGE1_SYSTEM_PROMPT",
    "build_abstractor",
    "fallback_theme",
    "lexicon_vocabulary",
    "theme_from_payload",
]


class AbstractionError(RuntimeError):
    """Stage 1 failed. Message must never contain window text or model output."""


@runtime_checkable
class Abstractor(Protocol):
    """Stage-1 brain. ``attempt`` lets the Weaver ask for a varied retry."""

    async def abstract(
        self,
        window_text: str,
        mood: MoodState | None = None,
        *,
        attempt: int = 0,
    ) -> ThemeObject: ...


# ---------------------------------------------------------------------------
# Closed output lexicon
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptCluster:
    name: str
    keywords: frozenset[str]
    motifs: tuple[str, ...]
    elemental: tuple[str, ...]


def _cluster(name: str, keywords: str, motifs: tuple[str, ...], elemental: tuple[str, ...]):
    return ConceptCluster(name, frozenset(keywords.split()), motifs, elemental)


CONCEPT_CLUSTERS: tuple[ConceptCluster, ...] = (
    _cluster(
        "water",
        "ocean oceans sea seas water waters wave waves tide tides swim swimming "
        "beach shore shoreline coast coastal boat boats sail sailing drown drowning "
        "rain river rivers pool pools flood flooding stream creek bath shower "
        "cove reef tidepool tidepools puddle wet damp"
        "current currents salt saltwater surf lake harbour harbor dive diving",
        ("vast blue depth", "surface breaking into light", "slow tidal pull"),
        ("water", "deep blue", "pressure"),
    ),
    _cluster(
        "memory",
        "memory memories remember remembered remembering grandmother grandfather "
        "grandma grandpa childhood mother father family families ancestor ancestors "
        "past photograph photographs story stories heritage nostalgia inherited "
        "used remember reminds childhood younger kid kids growing raised "
        "hometown school reunion anniversary album keepsake heirloom generation "
        "generations granddad nan nana grandparents"
        "sister brother cousin aunt uncle parents",
        ("inherited memory", "a shape passed down", "layers folded over time"),
        ("dust", "warm ochre", "patina"),
    ),
    _cluster(
        "fire",
        "fire fires burn burns burning burned flame flames heat hot smoke smoking "
        "warm warmth glow glowing burnt bonfire fireplace stove lit torch "
        "sunburn heater"
        "ash embers ember candle candles spark sparks blaze scorched",
        ("heat blooming", "slow combustion", "edges curling into ash"),
        ("fire", "ember orange", "smoke"),
    ),
    _cluster(
        "night",
        "night nights star stars starlight moon moonlight dark darkness midnight "
        "cosmos universe galaxy galaxies orbit planet planets constellation "
        "evening evenings tonight late sleep sleeping asleep dream dreams "
        "dreaming quiet still bedtime nocturnal dusk twilight sunset"
        "telescope astronaut",
        ("dark expanse, points of light", "distance without edges", "quiet drift of bodies"),
        ("void black", "silver", "cold light"),
    ),
    _cluster(
        "work",
        "work working job jobs money boss office deadline deadlines career business "
        "rent bills salary wages market markets tax taxes client clients meeting "
        "quit quitting hired fired laid promotion promoted commute commuting "
        "shift shifts manager managers startup startups project projects team "
        "teams colleague colleagues freelance contract hustle overtime interview "
        "resume raise"
        "meetings shift shifts overtime economy",
        ("gears and pressure", "grid tightening", "repetition under load"),
        ("iron", "grey steel", "friction"),
    ),
    _cluster(
        "love",
        "love loves loved loving heart hearts kiss kissed lover partner romance "
        "romantic tender tenderness affection intimacy intimate wedding marriage "
        "date dating relationship relationships partner girlfriend boyfriend "
        "wife husband crush marry married wedding together intimacy affection "
        "flirt kiss"
        "married beloved crush",
        ("warmth held close", "two currents meeting", "soft gravity"),
        ("blush pink", "warm gold", "pulse"),
    ),
    _cluster(
        "grief",
        "died die dying death dead funeral grief grieving loss lost mourn mourning "
        "buried burial illness sick sickness hospital cancer goodbye farewell "
        "miss missing gone passed funeral mourn mourning loss lost lonely alone "
        "ache absence goodbye"
        "widow orphan",
        ("descent", "an absence given shape", "slow fade at the edges"),
        ("ash grey", "cold water", "hollow"),
    ),
    _cluster(
        "journey",
        "travel travelling traveling journey road roads train trains plane planes "
        "flight flights trip drive driving walk walking path paths leaving arrive "
        "drive drove driving trip trips flight flying airport train road roads "
        "travel travelling traveled route commute wander wandering move moving "
        "arrive leaving"
        "arriving distance map maps migration border airport crossing",
        ("long passage", "horizon receding", "threads pulled outward"),
        ("pale gold", "wind", "haze"),
    ),
    _cluster(
        "music",
        "music song songs sing singing sang dance dancing danced rhythm rhythms "
        "beat beats drum drums guitar band bands concert melody melodies choir "
        "playlist album albums track tracks loud bass vinyl dj speaker speakers "
        "volume tune tunes listen listening headphones set sets festival gig "
        "groove"
        "orchestra dj vinyl record",
        ("pulse made visible", "overlapping cycles", "waves of resonance"),
        ("vivid magenta", "vibration", "shimmer"),
    ),
    _cluster(
        "growth",
        "tree trees forest forests garden gardens grow growing grew plant plants "
        "leaf leaves root roots seed seeds bloom blooming flower flowers moss wood "
        "learn learning better change changing improve progress build building "
        "start starting new begin beginning garden plant plants seed grow grew"
        "woods branch branches soil harvest farm",
        ("branching growth", "slow unfurling", "reaching upward"),
        ("moss green", "bark", "sap"),
    ),
    _cluster(
        "city",
        "city cities street streets traffic building buildings apartment apartments "
        "subway metro neighborhood neighbourhood town towns crowd crowds urban "
        "street streets traffic subway metro bus neighbourhood neighborhood "
        "downtown apartment building buildings block corner bar bars cafe "
        "restaurant crowd crowded"
        "downtown bus taxi sidewalk block skyline",
        ("stacked geometry", "channels of moving light", "density folding in"),
        ("concrete", "sodium yellow", "glass"),
    ),
    _cluster(
        "weather",
        "storm storms rain raining wind winds thunder lightning cloud clouds snow "
        "sun sunny sunshine sunset sunrise light lighting golden warm hot heat "
        "sky skies breeze afternoon morning shade cloudy grey gray season "
        "seasons spring summer autumn fall"
        "snowing cold winter freezing fog mist hurricane drought humid monsoon",
        ("gathering pressure", "sheets of moving air", "stillness before release"),
        ("slate grey", "charged air", "vapour"),
    ),
    _cluster(
        "machine",
        "computer computers phone phones internet screen screens code coding "
        "software machine machines robot robots data network networks digital "
        "app apps server servers bug bugs database latency deploy deployed cloud "
        "model models ai build builds system systems api backend frontend rust "
        "python javascript scheduler cache memory compute gpu chip"
        "algorithm algorithms server model models app apps",
        ("lattice of signals", "recursive structure", "precision unfolding"),
        ("circuit teal", "chrome", "signal"),
    ),
    _cluster(
        "body",
        "body bodies breath breathe breathing tired exhausted exhaustion pain ache "
        "aching hands skin blood muscle rest resting heal healing yoga run running "
        "hungry food eat eating drink drinking sick ill sleep sleepy warm cold "
        "hurt sore stretch walk walking sit sitting hug touch"
        "stretch pulse sleep sleeping dream dreams",
        ("slow expansion and release", "pulse beneath a surface", "weight settling"),
        ("warm red", "tissue", "breath"),
    ),
    _cluster(
        "tension",
        "fight fighting argue argued argument angry anger war wars conflict tension "
        "protest politics political fear afraid stress stressed worry worried "
        "overwhelmed overwhelming pressure panic nervous tense cope coping "
        "burnout burnt frustrated frustrating exhausting difficult hard struggle "
        "struggling deadline pile piling much"
        "anxious anxiety threat crisis",
        ("opposing currents", "fracture lines", "held tension"),
        ("hard shadow", "deep crimson", "static"),
    ),
    _cluster(
        "celebration",
        "party parties birthday celebrate celebration laugh laughing laughter "
        "friend friends drink drinks festival holiday happy fun toast gathering "
        "party parties birthday cheers toast drinks laugh laughing laughter fun "
        "joy happy excited celebrate celebrating friends gathering together"
        "reunion feast cheers",
        ("bright scatter", "expanding circles of warmth", "overlapping motion"),
        ("gold", "warm spectrum", "effervescence"),
    ),
    _cluster(
        "spirit",
        "god gods spirit spiritual soul souls prayer pray meditation meditate "
        "sacred ritual rituals church temple mosque belief believe meaning silence "
        "believe belief meaning meant soul sacred ritual pray prayer meditate "
        "meditation wonder awe mystery presence"
        "mystery divine transcendent ceremony",
        ("a slow ascent", "light through a threshold", "stillness with weight"),
        ("cathedral blue", "gold leaf", "hush"),
    ),
)

DEFAULT_MOTIFS: tuple[str, ...] = ("formless drift", "soft accumulation", "quiet dispersal")
DEFAULT_ELEMENTAL: tuple[str, ...] = ("muted spectrum", "haze", "slow light")

REGISTERS: tuple[str, ...] = ("elegiac", "exuberant", "contemplative", "tense", "ambient")

# Two words, ten characters: too short to trip either overlap check, so it is
# always a legal movement no matter what was said in the room.
SAFE_MOVEMENT = "slow drift"

MOVEMENTS: tuple[str, ...] = (
    "held nearly still",
    "slow drift",
    "slow, spiralling inward",
    "steady unfolding",
    "surging and receding",
    "accelerating churn",
)

POSITIVE_WORDS: frozenset[str] = frozenset(
    """
    good great love loved lovely beautiful happy joy joyful warm warmth kind
    gentle bright wonderful amazing perfect sweet calm peace peaceful hope
    hopeful laugh laughing laughter free freedom alive glad delight delighted
    thank thanks grateful gratitude best better brilliant safe soft rich
    generous funny excited exciting celebrate proud comfort comforting
    """.split()
)

NEGATIVE_WORDS: frozenset[str] = frozenset(
    """
    bad awful terrible hate hated sad sadness angry anger afraid fear scared
    dark cold cruel hurt hurting pain painful lonely alone lost loss died die
    dead death sick sickness tired exhausted broken breaking worry worried
    anxious anxiety stress stressed hard difficult wrong sorry grief mourning
    empty heavy bitter ugly
    """.split()
)

INTENSE_WORDS: frozenset[str] = frozenset(
    """
    very really so much always never everything nothing huge massive intense
    urgent screaming shouting crazy insane wild extreme desperate overwhelming
    exploding rushing racing furious ecstatic
    """.split()
)

_WORD_RE = re.compile(r"[a-z]+")


def lexicon_vocabulary() -> frozenset[str]:
    """Every word the heuristic path can ever emit.

    Used by the privacy test to prove the closed lexicon is disjoint from the
    validator's name gazetteer.
    """
    words: set[str] = set()
    phrases: list[str] = [*DEFAULT_MOTIFS, *DEFAULT_ELEMENTAL, *REGISTERS, *MOVEMENTS]
    for cluster in CONCEPT_CLUSTERS:
        phrases.extend(cluster.motifs)
        phrases.extend(cluster.elemental)
    for phrase in phrases:
        words.update(_WORD_RE.findall(phrase.lower()))
    return frozenset(words)


# ---------------------------------------------------------------------------
# Heuristic (no-LLM) stage 1
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rotate(items: list[str], offset: int) -> list[str]:
    if not items:
        return items
    k = offset % len(items)
    return items[k:] + items[:k]


def _collision_filter(window_text: str) -> Callable[[str], bool]:
    """Build a predicate: would this lexicon phrase trip the validator?

    Uses the validator's own normalization so the two can never drift apart.
    Only the *phrase* is inspected; nothing from the window is retained.
    """
    ref_ngrams = word_ngrams(normalize_words(window_text))
    ref_runs = char_runs(window_text)

    def collides(phrase: str) -> bool:
        if ref_ngrams and word_ngrams(normalize_words(phrase)) & ref_ngrams:
            return True
        return bool(ref_runs and char_runs(phrase) & ref_runs)

    return collides


def _select(
    pool: list[str],
    defaults: tuple[str, ...],
    collides: Callable[[str], bool],
    attempt: int,
    limit: int,
) -> list[str]:
    """Take up to ``limit`` non-colliding phrases, backfilling from defaults."""
    chosen = [item for item in _dedupe(pool) if not collides(item)][:limit]
    if not chosen:
        chosen = [item for item in _rotate(list(defaults), attempt) if not collides(item)][:limit]
    return chosen


def _register_for(valence: float, intensity: float) -> str:
    if intensity >= 0.6 and valence < 0.4:
        return "tense"
    if intensity >= 0.6 and valence >= 0.6:
        return "exuberant"
    if valence < 0.4:
        return "elegiac"
    if valence >= 0.6:
        return "contemplative"
    return "ambient"


class HeuristicAbstractor:
    """Deterministic stage 1. Output words come only from the closed lexicon."""

    name = "heuristic"

    def __init__(self, *, max_motifs: int | None = None, max_elemental: int | None = None) -> None:
        self.max_motifs = max_motifs or ThemeObject.MAX_MOTIFS
        self.max_elemental = max_elemental or ThemeObject.MAX_ELEMENTAL

    async def abstract(
        self,
        window_text: str,
        mood: MoodState | None = None,
        *,
        attempt: int = 0,
    ) -> ThemeObject:
        return self.abstract_sync(window_text, mood, attempt=attempt)

    # Sync core so tests and the empty-window fallback can call it directly.
    def abstract_sync(
        self,
        window_text: str,
        mood: MoodState | None = None,
        *,
        attempt: int = 0,
    ) -> ThemeObject:
        words = _WORD_RE.findall(window_text.lower())
        counts = self._score_clusters(words)
        # Someone in the room may happen to *say* a lexicon phrase. That is a
        # coincidence, not a leak, but the validator cannot tell the difference
        # — so drop any candidate that would collide before offering it.
        collides = _collision_filter(window_text)

        motif_pool: list[str] = []
        elemental_pool: list[str] = []
        for cluster, _score in counts:
            motif_pool.extend(_rotate(list(cluster.motifs), attempt))
            elemental_pool.extend(_rotate(list(cluster.elemental), attempt))

        motifs = _select(motif_pool, DEFAULT_MOTIFS, collides, attempt, self.max_motifs)
        elemental = _select(
            elemental_pool, DEFAULT_ELEMENTAL, collides, attempt, self.max_elemental
        )

        valence = self._valence(words, mood)
        intensity = self._intensity(words, window_text, mood)
        register = _register_for(valence, intensity)
        if collides(register):
            register = "ambient"
        movement = MOVEMENTS[min(int(intensity * len(MOVEMENTS)), len(MOVEMENTS) - 1)]
        if collides(movement):
            movement = next((m for m in MOVEMENTS if not collides(m)), SAFE_MOVEMENT)

        return ThemeObject(
            motifs=motifs,
            register=register,
            valence=round(valence, 3),
            intensity=round(intensity, 3),
            movement=movement,
            elemental=elemental,
        )

    # -- internals --

    def _score_clusters(self, words: list[str]) -> list[tuple[ConceptCluster, int]]:
        scored: list[tuple[ConceptCluster, int]] = []
        for cluster in CONCEPT_CLUSTERS:
            hits = sum(1 for word in words if word in cluster.keywords)
            if hits:
                scored.append((cluster, hits))
        # Deterministic: score desc, then declaration order (stable sort).
        scored.sort(key=lambda pair: -pair[1])
        return scored

    def _valence(self, words: list[str], mood: MoodState | None) -> float:
        positive = sum(1 for w in words if w in POSITIVE_WORDS)
        negative = sum(1 for w in words if w in NEGATIVE_WORDS)
        if positive or negative:
            score = 0.5 + 0.5 * (positive - negative) / (positive + negative + 2.0)
        else:
            score = 0.5
        if mood is not None:
            score = 0.7 * score + 0.3 * _clamp(float(mood.valence))
        return _clamp(score)

    def _intensity(self, words: list[str], raw: str, mood: MoodState | None) -> float:
        if not words:
            base = 0.3
        else:
            emphatic = sum(1 for w in words if w in INTENSE_WORDS) / len(words)
            exclamations = raw.count("!") / max(len(words) / 10.0, 1.0)
            questions = raw.count("?") / max(len(words) / 20.0, 1.0)
            density = min(len(words) / 200.0, 1.0)  # a fuller window reads as busier
            base = 0.25 + 2.5 * emphatic + 0.25 * min(exclamations, 1.0)
            base += 0.1 * min(questions, 1.0) + 0.25 * density
        if mood is not None:
            base = 0.6 * base + 0.4 * _clamp(float(mood.energy) * 0.5 + float(mood.intensity) * 0.5)
        return _clamp(base)


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def fallback_theme(
    mood: MoodState | None = None,
    previous: ThemeObject | None = None,
) -> ThemeObject:
    """Feature-driven theme for a silent or unusable window.

    Degradation ladder (Architecture §6): "ASR produces nothing (loud room)"
    → prompt synthesis falls back to audio features plus thematic memory.
    """
    valence = _clamp(float(mood.valence)) if mood is not None else 0.5
    energy = _clamp(float(mood.energy)) if mood is not None else 0.25
    intensity = _clamp(float(mood.intensity)) if mood is not None else 0.35
    level = _clamp(0.5 * energy + 0.5 * intensity)

    motifs = list(previous.motifs[:2]) if previous and previous.motifs else []
    for motif in DEFAULT_MOTIFS:
        if motif not in motifs:
            motifs.append(motif)
    elemental = list(previous.elemental[:2]) if previous and previous.elemental else []
    for element in DEFAULT_ELEMENTAL:
        if element not in elemental:
            elemental.append(element)

    return ThemeObject(
        motifs=motifs[: ThemeObject.MAX_MOTIFS],
        register=_register_for(valence, level),
        valence=round(valence, 3),
        intensity=round(level, 3),
        movement=MOVEMENTS[min(int(level * len(MOVEMENTS)), len(MOVEMENTS) - 1)],
        elemental=elemental[: ThemeObject.MAX_ELEMENTAL],
    )


# ---------------------------------------------------------------------------
# LLM stage 1
# ---------------------------------------------------------------------------

STAGE1_SYSTEM_PROMPT = (
    "You distil overheard conversation into an ABSTRACT theme object. "
    "You must never reproduce anything you were given. "
    "Forbidden in your output: names of people or places, quotes, any phrase of "
    "three or more consecutive words from the input, numbers, contact details, "
    "job titles, or any detail that could identify a speaker. "
    "Write only conceptual, emotional and elemental abstractions in your own "
    "generic vocabulary.\n"
    "Reply with ONE JSON object and nothing else, matching exactly:\n"
    '{"motifs": [str, ...], "register": str, "valence": float, '
    '"intensity": float, "movement": str, "elemental": [str, ...]}\n'
    f"At most {ThemeObject.MAX_MOTIFS} motifs and {ThemeObject.MAX_ELEMENTAL} elemental "
    f"terms, each at most {ThemeObject.MAX_FIELD_CHARS} characters. "
    "valence and intensity are floats in [0,1]. "
    f"register is one of: {', '.join(REGISTERS)}."
)

_ATTEMPT_NUDGE = (
    " Your previous attempt was rejected as insufficiently abstract. "
    "Use entirely different, more generic wording."
)


def _extract_first_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced ``{...}`` out of a model reply.

    Raises AbstractionError with a content-free message on failure — the reply
    may contain transcript fragments and must never reach a log.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    raise AbstractionError("stage-1 reply contained no parseable JSON object")


def _clamp_float(value: Any, default: float) -> float:
    try:
        return _clamp(float(value))
    except (TypeError, ValueError):
        return default


def _clamp_strings(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())[: ThemeObject.MAX_FIELD_CHARS].strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def theme_from_payload(payload: dict[str, Any]) -> ThemeObject:
    """Coerce an arbitrary JSON payload into a schema-legal ThemeObject.

    Clamping here does not make the object *safe* — only the validator does
    that — it makes it well-shaped so the validator's reasons are meaningful.
    """
    register = payload.get("register")
    register = " ".join(register.split()) if isinstance(register, str) else "ambient"
    movement = payload.get("movement")
    movement = " ".join(movement.split()) if isinstance(movement, str) else "slow drift"
    return ThemeObject(
        motifs=_clamp_strings(payload.get("motifs"), ThemeObject.MAX_MOTIFS),
        register=(register or "ambient")[: ThemeObject.MAX_FIELD_CHARS],
        valence=_clamp_float(payload.get("valence"), 0.5),
        intensity=_clamp_float(payload.get("intensity"), 0.5),
        movement=(movement or "slow drift")[: ThemeObject.MAX_FIELD_CHARS],
        elemental=_clamp_strings(payload.get("elemental"), ThemeObject.MAX_ELEMENTAL),
    )


class LLMAbstractor:
    """Stage 1 against an OpenAI-compatible chat completions endpoint."""

    name = "llm"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        temperature: float = 0.4,
        max_tokens: int = 400,
        client: Any | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("EGREGORE_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("EGREGORE_LLM_MODEL", "")
        self.api_key = api_key if api_key is not None else os.environ.get("EGREGORE_LLM_API_KEY")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = client
        if not self.base_url or not self.model:
            raise ValueError(
                "LLMAbstractor needs a base_url and model "
                "(EGREGORE_LLM_BASE_URL / EGREGORE_LLM_MODEL)"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, window_text: str, mood: MoodState | None, attempt: int) -> dict[str, Any]:
        system = STAGE1_SYSTEM_PROMPT + (_ATTEMPT_NUDGE if attempt else "")
        user = window_text
        if mood is not None:
            user = (
                f"[room energy {mood.energy:.2f}, brightness {mood.brightness:.2f}]\n"
                f"{window_text}"
            )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature + (0.2 if attempt else 0.0),
            "max_tokens": self.max_tokens,
            "stream": False,
        }

    async def abstract(
        self,
        window_text: str,
        mood: MoodState | None = None,
        *,
        attempt: int = 0,
    ) -> ThemeObject:
        import httpx

        url = f"{self.base_url}/chat/completions"
        payload = self._payload(window_text, mood, attempt)
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=self._headers())
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, json=payload, headers=self._headers())
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            # Deliberately does not include the request body or response text.
            raise AbstractionError(f"stage-1 endpoint error: {type(exc).__name__}") from None
        except ValueError:
            raise AbstractionError("stage-1 endpoint returned non-JSON body") from None

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AbstractionError("stage-1 reply had no message content") from None
        if not isinstance(content, str):
            raise AbstractionError("stage-1 reply content was not a string")
        return theme_from_payload(_extract_first_json_object(content))


def build_abstractor(config: WeaverConfig | None = None, **kwargs: Any) -> Abstractor:
    """Pick a stage-1 brain from party config.

    ``engine: auto`` uses the LLM when an endpoint is configured and falls back
    to the deterministic heuristic otherwise — the pipeline never *depends* on
    an LLM being present (config docstring, Architecture §2.4 demo mode).
    """
    config = config or WeaverConfig()
    if config.engine == "heuristic":
        return HeuristicAbstractor()
    base_url = config.llm.base_url
    if not base_url:
        if config.engine == "llm":
            raise ValueError("weaver.engine is 'llm' but weaver.llm.base_url is unset")
        return HeuristicAbstractor()
    return LLMAbstractor(
        base_url=base_url,
        model=config.llm.model,
        api_key=os.environ.get(config.llm.api_key_env),
        **kwargs,
    )
