"""Deterministic validation gate between Weaver stage 1 and stage 2.

This is the last line of defense against transcript leakage (Architecture
§2.4). Stage 1 is model-mediated and can put anything into free-form strings;
nothing downstream of this module ever sees raw text again, so every check
that matters happens here.

Privacy rules for this file:

* ``ValidationResult.reasons`` names the CHECK that failed and nothing else.
  It must never quote the theme, the reference text, or a matched token — a
  rejection reason is logged, and a reason carrying content would defeat the
  entire point of the gate.
* No function here logs, raises with, or persists any fragment of either
  input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from egregore.types import ThemeObject

__all__ = [
    "GAZETTEER",
    "SENTENCE_INITIAL_ALLOWLIST",
    "ValidationResult",
    "char_runs",
    "normalize_chars",
    "normalize_words",
    "validate_theme",
    "word_ngrams",
]

# --- tunables ---------------------------------------------------------------

NGRAM_N = 3
CHAR_RUN = 12
MIN_DIGIT_RUN = 4

# --- reason codes (check names only — never content) ------------------------

REASON_NGRAM = "3gram-overlap"
REASON_CHAR_RUN = "12char-overlap"
REASON_GAZETTEER = "gazetteer-name"
REASON_CAPITALIZED = "capitalized-shared-token"
REASON_REF_NAME = "reference-name-sweep"
REASON_DIGITS = "identifier-digits"
REASON_EMAIL = "identifier-email"
REASON_PHONE = "identifier-phone"
REASON_URL = "identifier-url"
REASON_CAP_MOTIFS = "cap-motifs"
REASON_CAP_ELEMENTAL = "cap-elemental"
REASON_CAP_FIELD_CHARS = "cap-field-chars"
REASON_RANGE_VALENCE = "range-valence"
REASON_RANGE_INTENSITY = "range-intensity"
REASON_FIELD_TYPE = "field-type"

# --- normalization ----------------------------------------------------------

_PUNCT_RE = re.compile(r"[^0-9a-z\s]")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

_DIGIT_RE = re.compile(rf"\d{{{MIN_DIGIT_RUN},}}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)\S+"
    r"|\b[A-Za-z0-9\-]+\.(?:com|net|org|io|co|edu|gov|app|dev|xyz|me)\b",
    re.IGNORECASE,
)
# Loose phone shape: 7+ digits with optional separators / country prefix.
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().\-]{5,}\d)")


def normalize_words(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    lowered = text.lower()
    stripped = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).split()


def normalize_chars(text: str) -> str:
    """Lowercase and collapse all whitespace to single spaces (punctuation kept)."""
    return _WS_RE.sub(" ", text.lower()).strip()


def word_ngrams(words: list[str], n: int = NGRAM_N) -> set[tuple[str, ...]]:
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def char_runs(text: str, size: int = CHAR_RUN) -> set[str]:
    norm = normalize_chars(text)
    if len(norm) < size:
        return set()
    return {norm[i : i + size] for i in range(len(norm) - size + 1)}


# --- gazetteer --------------------------------------------------------------
# ~200 common English given names, lowercase. Deliberately includes names that
# are also ordinary words (grace, mark, frank, jean, amber, ruby, iris) because
# ASR output is lowercase and those are exactly the leaks a naive NER misses.
# INVARIANT: no phrase the HeuristicAbstractor can emit may use any of these
# words — see tests/test_weaver.py::test_lexicon_disjoint_from_gazetteer.

GAZETTEER: frozenset[str] = frozenset(
    {
        # masculine-leaning
        "aaron", "adam", "adrian", "alan", "albert", "alexander", "andre", "andrew",
        "anthony", "arthur", "austin", "benjamin", "bernard", "bobby", "bradley",
        "brandon", "brian", "bruce", "bryan", "carl", "carlos", "charles",
        "christian", "christopher", "clarence", "cody", "colin", "connor", "craig",
        "curtis", "dale", "daniel", "darren", "david", "dennis", "derek", "diego",
        "donald", "douglas", "dylan", "edward", "elijah", "eric", "ethan", "eugene",
        "felix", "francisco", "frank", "franklin", "gabriel", "gary", "george",
        "gerald", "glenn", "gordon", "graham", "gregory", "harold", "harry",
        "hector", "henry", "howard", "hugo", "ian", "isaac", "ivan", "jack",
        "jacob", "james", "jason", "javier", "jeffrey", "jeremy", "jerome",
        "jerry", "jesse", "joel", "john", "jonathan", "jordan", "jose", "joseph",
        "joshua", "juan", "julian", "justin", "keith", "kenneth", "kevin", "kyle",
        "lawrence", "leon", "leonard", "liam", "logan", "louis", "lucas", "malcolm",
        "manuel", "marcus", "mark", "martin", "mason", "matthew", "maurice",
        "micah", "michael", "miguel", "nathan", "neil", "nicholas", "noah",
        "norman", "oliver", "omar", "oscar", "patrick", "paul", "pedro", "peter",
        "philip", "quentin", "rafael", "ralph", "randall", "raymond", "reginald",
        "richard", "robert", "roger", "ronald", "roy", "russell", "ryan", "samuel",
        "scott", "sean", "seth", "shane", "simon", "stanley", "stephen", "steven",
        "terrence", "theodore", "thomas", "timothy", "todd", "tyler", "victor",
        "vincent", "walter", "warren", "wayne", "wesley", "william", "zachary",
        # feminine-leaning
        "abigail", "adriana", "alice", "alicia", "allison", "amanda", "amber",
        "amelia", "amy", "andrea", "angela", "anna", "annette", "ashley",
        "audrey", "barbara", "beatrice", "bethany", "beverly", "brenda",
        "brittany", "camila", "carla", "carmen", "carol", "caroline", "catherine",
        "charlotte", "cheryl", "chloe", "christina", "christine", "clara",
        "colleen", "courtney", "cynthia", "danielle", "deborah", "denise", "diana",
        "diane", "dolores", "donna", "doris", "dorothy", "eleanor", "elena",
        "elizabeth", "ellen", "emily", "emma", "erica", "esther", "eva", "evelyn",
        "fiona", "frances", "gabriela", "gloria", "grace", "hannah", "heather",
        "helen", "ingrid", "irene", "iris", "isabella", "jacqueline", "janet",
        "janice", "jasmine", "jean", "jennifer", "jessica", "joan", "joanne",
        "josephine", "joyce", "judith", "julia", "julie", "karen", "katherine",
        "kathleen", "kathryn", "kayla", "kelly", "kimberly", "laura", "lauren",
        "leila", "leslie", "lillian", "linda", "lisa", "lorraine", "louise",
        "lucy", "lydia", "madison", "marcia", "margaret", "maria", "marie",
        "marilyn", "martha", "mary", "megan", "melanie", "melissa", "michelle",
        "mildred", "miranda", "molly", "monica", "nadia", "naomi", "natalie",
        "nicole", "nora", "olivia", "pamela", "patricia", "paula", "pauline",
        "priya", "rachel", "rebecca", "regina", "renee", "rhonda", "roberta",
        "rosemary", "ruby", "ruth", "samantha", "sandra", "sara", "sarah",
        "sharon", "sheila", "shirley", "sonia", "sophia", "stephanie", "susan",
        "sylvia", "tammy", "teresa", "tessa", "theresa", "tiffany", "valerie",
        "vanessa", "vera", "veronica", "victoria", "virginia", "vivian", "wendy",
        "yolanda", "yvonne", "zoe",
    }
)

# Capitalized tokens that are almost always sentence-initial function words
# rather than names. Kept deliberately small: over-allowlisting weakens the
# check, and stage-2 never needs capitalization anyway.
SENTENCE_INITIAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "a", "after", "all", "an", "and", "another", "as", "at", "before", "both",
        "but", "by", "each", "every", "for", "from", "he", "her", "here", "his",
        "how", "i", "if", "in", "into", "it", "its", "no", "not", "now", "of",
        "on", "one", "or", "over", "she", "so", "some", "that", "the", "their",
        "then", "there", "these", "they", "this", "through", "to", "two", "under",
        "we", "what", "when", "where", "which", "while", "who", "why", "with",
        "you", "your",
    }
)


# --- result -----------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the gate. ``reasons`` holds check names only, never content."""

    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


# --- the gate ---------------------------------------------------------------


def _theme_fields(theme: ThemeObject) -> list[str]:
    """Every free-form string on the theme, coerced defensively to str."""
    return [t if isinstance(t, str) else str(t) for t in theme.all_text()]


def _check_schema(theme: ThemeObject, reasons: list[str]) -> None:
    if len(theme.motifs) > ThemeObject.MAX_MOTIFS:
        reasons.append(REASON_CAP_MOTIFS)
    if len(theme.elemental) > ThemeObject.MAX_ELEMENTAL:
        reasons.append(REASON_CAP_ELEMENTAL)
    for value in theme.all_text():
        if not isinstance(value, str):
            reasons.append(REASON_FIELD_TYPE)
        elif len(value) > ThemeObject.MAX_FIELD_CHARS:
            reasons.append(REASON_CAP_FIELD_CHARS)
    if not 0.0 <= float(theme.valence) <= 1.0:
        reasons.append(REASON_RANGE_VALENCE)
    if not 0.0 <= float(theme.intensity) <= 1.0:
        reasons.append(REASON_RANGE_INTENSITY)


def _check_identifiers(fields: list[str], reasons: list[str]) -> None:
    for value in fields:
        if _DIGIT_RE.search(value):
            reasons.append(REASON_DIGITS)
        if _EMAIL_RE.search(value):
            reasons.append(REASON_EMAIL)
        if _URL_RE.search(value):
            reasons.append(REASON_URL)
        if _PHONE_RE.search(value):
            reasons.append(REASON_PHONE)


def _check_overlap(fields: list[str], reference_text: str, reasons: list[str]) -> None:
    ref_words = normalize_words(reference_text)
    ref_ngrams = word_ngrams(ref_words)
    ref_runs = char_runs(reference_text)

    for value in fields:
        if ref_ngrams and word_ngrams(normalize_words(value)) & ref_ngrams:
            reasons.append(REASON_NGRAM)
        if ref_runs and char_runs(value) & ref_runs:
            reasons.append(REASON_CHAR_RUN)


def _check_names(fields: list[str], reference_text: str, reasons: list[str]) -> None:
    ref_words = normalize_words(reference_text)
    ref_word_set = set(ref_words)
    banned_from_reference = ref_word_set & GAZETTEER

    for value in fields:
        for token in _WORD_RE.findall(value):
            lowered = token.lower()
            # (a) gazetteer sweep, case-insensitive
            if lowered in GAZETTEER:
                reasons.append(REASON_GAZETTEER)
            # (c) any name-shaped token present in the reference is banned
            #     from the output regardless of casing
            if lowered in banned_from_reference:
                reasons.append(REASON_REF_NAME)
            # (b) capitalized-in-theme token echoed by the reference
            if (
                token[0].isupper()
                and lowered in ref_word_set
                and lowered not in SENTENCE_INITIAL_ALLOWLIST
            ):
                reasons.append(REASON_CAPITALIZED)


def validate_theme(theme: ThemeObject, reference_text: str) -> ValidationResult:
    """Reject a theme object that could carry transcript content downstream.

    ``reference_text`` is the raw window the theme was abstracted from. It is
    read here and nowhere else in the returned value.
    """
    reasons: list[str] = []
    fields = _theme_fields(theme)

    _check_schema(theme, reasons)
    _check_identifiers(fields, reasons)
    if reference_text and reference_text.strip():
        _check_overlap(fields, reference_text, reasons)
        _check_names(fields, reference_text, reasons)
    else:
        # No reference: the gazetteer sweep still applies (a name is a name).
        _check_names(fields, "", reasons)

    # Dedupe, preserve first-seen order. These are constants, so the result
    # is safe to log verbatim.
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return ValidationResult(ok=not seen, reasons=seen)
