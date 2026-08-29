"""Weaver tests — the validator is the privacy boundary, so it gets the bulk.

Nothing here asserts on transcript content leaving the module; the point of
several tests is precisely that it cannot.
"""

from __future__ import annotations

import httpx
import pytest

from egregore.types import MoodState, ThemeObject
from egregore.weaver import (
    GAZETTEER,
    SAFETY_FLOOR,
    AbstractionError,
    HeuristicAbstractor,
    LLMAbstractor,
    Weaver,
    fallback_theme,
    lexicon_vocabulary,
    synthesize_prompt,
    validate_theme,
)
from egregore.weaver.abstractor import build_abstractor, theme_from_payload
from egregore.weaver.validator import (
    REASON_CAP_FIELD_CHARS,
    REASON_CAP_MOTIFS,
    REASON_CHAR_RUN,
    REASON_DIGITS,
    REASON_EMAIL,
    REASON_GAZETTEER,
    REASON_NGRAM,
    REASON_RANGE_INTENSITY,
    REASON_URL,
)

# A fixture window in the shape of real ASR output: lowercase, unpunctuated,
# with names, a phone number and an email planted in it.
REFERENCE = (
    "so miranda was telling me about the summer she spent on the coast with her "
    "grandmother the two of them would walk down to the water every evening and "
    "watch the tide come in she said the ocean always sounded like breathing "
    "anyway her number is 415 555 0198 and you can reach her at miranda.k@example.com "
    "before she flies out on tuesday"
)

OCEAN_GRANDMOTHER = (
    "i keep thinking about my grandmother and the ocean she taught me to swim in "
    "the cold water off the shore we would sit on the beach after and she would "
    "tell me stories from her childhood the waves coming in the whole time it is "
    "one of my oldest memories and it still feels warm"
)

GRAMMAR = (
    "Abstract, symbolic, non-representational. Deep saturated color, organic forms "
    "dissolving into geometric ones."
)


def clean_theme() -> ThemeObject:
    return ThemeObject(
        motifs=["inherited memory", "vast blue depth"],
        register="elegiac",
        valence=0.3,
        intensity=0.6,
        movement="slow, spiralling inward",
        elemental=["water", "deep blue", "pressure"],
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_clean_abstract_theme_passes():
    result = validate_theme(clean_theme(), REFERENCE)
    assert result.ok, result.reasons
    assert result.reasons == []


def test_catches_planted_three_gram():
    # "sounded like breathing" is lifted verbatim from the reference.
    theme = ThemeObject(motifs=["a room that sounded like breathing"])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_NGRAM in result.reasons


def test_three_gram_check_is_case_and_punctuation_insensitive():
    theme = ThemeObject(motifs=["Sounded, LIKE... breathing"])
    assert REASON_NGRAM in validate_theme(theme, REFERENCE).reasons


def test_catches_twelve_character_run_that_three_grams_miss():
    # Two words only — no 3-gram — but 12+ shared characters.
    theme = ThemeObject(movement="tide come in slowly")
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_CHAR_RUN in result.reasons


def test_catches_gazetteer_name_regardless_of_casing():
    # Reference has lowercase "miranda" (ASR output); theme capitalizes it.
    theme = ThemeObject(motifs=["Miranda as a distant shape"])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_GAZETTEER in result.reasons


def test_gazetteer_name_caught_even_without_reference():
    theme = ThemeObject(motifs=["the weight of grace"])  # "grace" is a given name
    assert REASON_GAZETTEER in validate_theme(theme, "unrelated text entirely").reasons


def test_catches_phone_shaped_digit_run():
    theme = ThemeObject(motifs=["a signal at 5550198"])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_DIGITS in result.reasons


def test_catches_email():
    theme = ThemeObject(elemental=["miranda.k@example.com"])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_EMAIL in result.reasons


def test_catches_url():
    theme = ThemeObject(motifs=["https://example.com/party"])
    assert REASON_URL in validate_theme(theme, "").reasons


def test_catches_over_cap_field():
    theme = ThemeObject(motifs=["x" * (ThemeObject.MAX_FIELD_CHARS + 1)])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert REASON_CAP_FIELD_CHARS in result.reasons


def test_catches_too_many_motifs_and_out_of_range_intensity():
    theme = ThemeObject(
        motifs=[f"drift {i}" for i in range(ThemeObject.MAX_MOTIFS + 2)],
        intensity=1.4,
    )
    result = validate_theme(theme, "")
    assert not result.ok
    assert REASON_CAP_MOTIFS in result.reasons
    assert REASON_RANGE_INTENSITY in result.reasons


def test_capitalized_token_echoing_reference_is_flagged():
    # "Coast" is not a name, but it is capitalized in the theme and present in
    # the reference — the sentence-initial allowlist must not cover it.
    theme = ThemeObject(motifs=["Coast"])
    result = validate_theme(theme, REFERENCE)
    assert not result.ok
    assert "capitalized-shared-token" in result.reasons


def test_sentence_initial_allowlist_does_not_trip():
    theme = ThemeObject(motifs=["The slow unfurling"], elemental=["moss green"])
    assert validate_theme(theme, REFERENCE).ok


def test_reasons_never_contain_reference_content():
    """Reason codes name the CHECK, never the offending text."""
    leaky = ThemeObject(
        motifs=["Miranda sounded like breathing", "call 4155550198"],
        movement="tide come in",
        elemental=["miranda.k@example.com"],
        intensity=2.0,
    )
    result = validate_theme(leaky, REFERENCE)
    assert not result.ok
    blob = " ".join(result.reasons).lower()
    for token in REFERENCE.lower().split():
        if len(token) >= 4:
            assert token not in blob, "validator reason leaked reference content"
    for motif in leaky.all_text():
        assert motif.lower() not in blob


def test_empty_reference_still_runs_identifier_and_name_checks():
    reasons = validate_theme(ThemeObject(motifs=["call 5551234"]), "").reasons
    assert REASON_DIGITS in reasons
    assert validate_theme(clean_theme(), "").ok


# ---------------------------------------------------------------------------
# HeuristicAbstractor
# ---------------------------------------------------------------------------


async def test_heuristic_finds_water_and_memory_motifs():
    theme = await HeuristicAbstractor().abstract(OCEAN_GRANDMOTHER, None)
    joined = " ".join(theme.motifs + theme.elemental).lower()
    assert "water" in joined or "blue" in joined or "tidal" in joined
    assert "memory" in joined or "passed down" in joined or "ochre" in joined
    assert theme.register in {"elegiac", "exuberant", "contemplative", "tense", "ambient"}
    assert 0.0 <= theme.valence <= 1.0 and 0.0 <= theme.intensity <= 1.0


async def test_heuristic_output_passes_its_own_validator():
    """The closed lexicon means the heuristic path cannot leak — assert it."""
    abstractor = HeuristicAbstractor()
    for window in (OCEAN_GRANDMOTHER, REFERENCE):
        for attempt in (0, 1, 2):
            theme = await abstractor.abstract(window, None, attempt=attempt)
            result = validate_theme(theme, window)
            assert result.ok, result.reasons


async def test_heuristic_never_copies_input_words():
    """Every output word must come from the closed lexicon, not the input."""
    theme = await HeuristicAbstractor().abstract(REFERENCE, None)
    vocabulary = lexicon_vocabulary()
    for field_text in theme.all_text():
        for word in field_text.lower().replace(",", " ").split():
            assert word in vocabulary, f"output word {word!r} is not from the lexicon"


async def test_heuristic_avoids_phrases_the_room_happened_to_say():
    """A guest saying a lexicon phrase is a coincidence, not a leak — but the
    validator cannot tell, so stage 1 must not offer the colliding phrase."""
    spoken = (
        "we were talking about the vast blue depth of the ocean and the water "
        "and the waves all night long under a dark expanse points of light"
    )
    theme = await HeuristicAbstractor().abstract(spoken, None)
    assert "vast blue depth" not in theme.motifs
    assert "dark expanse, points of light" not in theme.motifs
    assert theme.motifs  # still produced something usable
    assert validate_theme(theme, spoken).ok


async def test_collision_does_not_cost_a_retry():
    weaver = Weaver(HeuristicAbstractor())
    result = await weaver.weave(
        "the vast blue depth of the ocean and the slow tidal pull of the water",
        grammar=GRAMMAR,
        drift=0.4,
    )
    assert result.prompt is not None
    assert result.attempts == 1
    assert weaver.rejections == 0


def test_lexicon_disjoint_from_gazetteer():
    """Structural invariant: no lexicon phrase may collide with a given name."""
    assert not (lexicon_vocabulary() & GAZETTEER)


async def test_heuristic_is_deterministic():
    a = await HeuristicAbstractor().abstract(OCEAN_GRANDMOTHER, None)
    b = await HeuristicAbstractor().abstract(OCEAN_GRANDMOTHER, None)
    assert a == b


async def test_heuristic_attempt_varies_selection():
    abstractor = HeuristicAbstractor()
    first = await abstractor.abstract(OCEAN_GRANDMOTHER, None, attempt=0)
    second = await abstractor.abstract(OCEAN_GRANDMOTHER, None, attempt=1)
    assert first.motifs != second.motifs


async def test_heuristic_respects_schema_caps():
    dense = " ".join(
        [
            "ocean water fire flame night stars work money love heart death grief",
            "road train music dance forest garden city street storm rain computer",
            "code body breath fight war party birthday god prayer",
        ]
    )
    theme = await HeuristicAbstractor().abstract(dense, None)
    assert len(theme.motifs) <= ThemeObject.MAX_MOTIFS
    assert len(theme.elemental) <= ThemeObject.MAX_ELEMENTAL
    assert validate_theme(theme, dense).ok


async def test_valence_tracks_sentiment():
    warm = await HeuristicAbstractor().abstract(
        "it was such a beautiful happy day we laughed and everything felt kind and warm",
        None,
    )
    cold = await HeuristicAbstractor().abstract(
        "it was awful she died and everything felt cold and lonely and broken and sad",
        None,
    )
    assert warm.valence > cold.valence


async def test_mood_biases_the_heuristic():
    mood = MoodState(energy=0.95, intensity=0.95, valence=0.9)
    loud = await HeuristicAbstractor().abstract(OCEAN_GRANDMOTHER, mood)
    quiet = await HeuristicAbstractor().abstract(OCEAN_GRANDMOTHER, None)
    assert loud.intensity > quiet.intensity


def test_fallback_theme_is_valid_and_reuses_memory():
    previous = clean_theme()
    theme = fallback_theme(MoodState(energy=0.8, intensity=0.7, valence=0.6), previous)
    assert validate_theme(theme, "").ok
    assert theme.motifs[0] in previous.motifs


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def test_prompt_contains_grammar_motifs_and_safety_floor():
    prompt = synthesize_prompt(clean_theme(), GRAMMAR, None, 0.4, None)
    assert GRAMMAR in prompt
    assert "inherited memory" in prompt
    assert "deep blue" in prompt
    assert "elegiac" in prompt
    assert prompt.endswith(SAFETY_FLOOR)


def test_safety_floor_covers_every_required_prohibition():
    floor = SAFETY_FLOOR.lower()
    for phrase in (
        "horror",
        "distress",
        "violence",
        "threatening",
        "strobing",
        "readable text",
        "recognizable faces",
    ):
        assert phrase in floor


def test_safety_floor_is_last_and_present_in_every_variation():
    for drift in (0.0, 0.5, 1.0):
        for continuity in (None, "a slow blue spiral opening toward the upper left"):
            prompt = synthesize_prompt(
                clean_theme(), GRAMMAR, continuity, drift, MoodState(energy=0.7)
            )
            assert prompt.count(SAFETY_FLOOR) == 1
            assert prompt.endswith(SAFETY_FLOOR)


def test_continuity_clause_appears_only_when_supplied():
    without = synthesize_prompt(clean_theme(), GRAMMAR, None, 0.4, None)
    assert "continue the current scene" not in without.lower()
    with_continuity = synthesize_prompt(
        clean_theme(), GRAMMAR, "a slow blue spiral opening upward", 0.4, None
    )
    assert "continue the current scene" in with_continuity.lower()
    assert "a slow blue spiral opening upward" in with_continuity
    assert "preserve direction of motion" in with_continuity


def test_drift_wording_changes_with_drift():
    low = synthesize_prompt(clean_theme(), GRAMMAR, None, 0.0, None)
    high = synthesize_prompt(clean_theme(), GRAMMAR, None, 1.0, None)
    assert "stay close to these themes" in low.lower()
    assert "wander associatively" in high.lower()


def test_mood_bias_phrasing_present():
    prompt = synthesize_prompt(
        clean_theme(), GRAMMAR, None, 0.4, MoodState(energy=0.9, brightness=0.9)
    )
    assert "room bias" in prompt.lower()


def test_synthesis_signature_cannot_accept_raw_text():
    """Stage 2 is structurally out of reach of the ring buffer.

    The exact-set assertion is the point: adding a parameter here has to be
    a deliberate act, because a new string channel into stage 2 is exactly
    how transcript text would get into an outbound prompt. `abstraction` is
    a float that only selects between fixed instruction strings, so it
    cannot carry content — and the assertion below checks that rather than
    taking it on trust.
    """
    import inspect
    import typing

    sig = inspect.signature(synthesize_prompt)
    assert set(sig.parameters) == {
        "theme", "grammar", "continuity", "drift", "mood", "abstraction", "room_bias",
    }
    # Only `grammar` and `continuity` are text, and both are operator-supplied
    # rather than room-supplied. Nothing else may be a string. Resolved with
    # get_type_hints because the module uses postponed annotations, so the
    # raw signature reports these as the string "float".
    hints = typing.get_type_hints(synthesize_prompt)
    assert hints["abstraction"] is float
    # `room_bias` likewise only gates fixed phrases about the room's sound.
    assert hints["room_bias"] is float
    assert hints["drift"] is float


# ---------------------------------------------------------------------------
# Weaver cycle
# ---------------------------------------------------------------------------


class EchoAbstractor:
    """Adversarial stage 1: dumps raw input straight into the theme."""

    def __init__(self) -> None:
        self.calls = 0

    async def abstract(self, window_text, mood=None, *, attempt=0):
        self.calls += 1
        return ThemeObject(motifs=[window_text[:70]], register="elegiac")


class BrokenAbstractor:
    async def abstract(self, window_text, mood=None, *, attempt=0):
        raise AbstractionError("stage-1 endpoint error: ConnectError")


async def test_full_cycle_produces_a_prompt():
    weaver = Weaver(HeuristicAbstractor())
    result = await weaver.weave(
        OCEAN_GRANDMOTHER, grammar=GRAMMAR, drift=0.4, mood=MoodState(energy=0.5)
    )
    assert result.prompt is not None
    assert not result.rejected and not result.purge_requested
    assert result.attempts == 1
    assert SAFETY_FLOOR in result.prompt
    assert GRAMMAR in result.prompt
    assert result.theme is not None
    assert weaver.last_theme == result.theme
    assert weaver.prompts_synthesized == 1


async def test_prompt_shares_nothing_with_the_window():
    """End-to-end privacy check on the demo path."""
    weaver = Weaver(HeuristicAbstractor())
    result = await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    assert result.theme is not None
    assert validate_theme(result.theme, REFERENCE).ok
    assert "miranda" not in result.prompt.lower()
    assert "0198" not in result.prompt


async def test_double_rejection_requests_purge():
    echo = EchoAbstractor()
    weaver = Weaver(echo)
    result = await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    assert result.rejected
    assert result.purge_requested
    assert result.prompt is None
    assert result.theme is None
    assert result.attempts == 2
    assert echo.calls == 2
    assert weaver.rejections == 2
    assert weaver.purges_requested == 1
    assert result.reasons and all(" " not in r for r in result.reasons)


async def test_stage_one_errors_also_purge():
    weaver = Weaver(BrokenAbstractor())
    result = await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    assert result.purge_requested and result.prompt is None
    assert result.reasons == ["stage1-error"]


async def test_recovers_on_second_attempt():
    class FlakyAbstractor:
        def __init__(self) -> None:
            self.calls = 0

        async def abstract(self, window_text, mood=None, *, attempt=0):
            self.calls += 1
            if attempt == 0:
                return ThemeObject(motifs=[window_text[:70]])
            return ThemeObject(motifs=["vast blue depth"], elemental=["deep blue"])

    weaver = Weaver(FlakyAbstractor())
    result = await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    assert result.prompt is not None
    assert result.attempts == 2
    assert weaver.rejections == 1
    assert weaver.purges_requested == 0


async def test_empty_window_falls_back_to_features():
    weaver = Weaver(HeuristicAbstractor())
    for window in ("", "   ", "yeah", "um yeah ok"):
        result = await weaver.weave(
            window, grammar=GRAMMAR, drift=0.4, mood=MoodState(energy=0.8, valence=0.7)
        )
        assert result.prompt is not None
        assert result.fallback
        assert not result.purge_requested
        assert SAFETY_FLOOR in result.prompt
        assert result.theme is not None and result.theme.motifs


async def test_fallback_carries_thematic_memory_forward():
    weaver = Weaver(HeuristicAbstractor())
    first = await weaver.weave(OCEAN_GRANDMOTHER, grammar=GRAMMAR, drift=0.4)
    silent = await weaver.weave("", grammar=GRAMMAR, drift=0.4)
    assert first.theme is not None and silent.theme is not None
    assert set(silent.theme.motifs) & set(first.theme.motifs)


async def test_weaver_never_logs_content(caplog):
    caplog.set_level("DEBUG", logger="egregore.weaver")
    weaver = Weaver(EchoAbstractor())
    await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    ok = Weaver(HeuristicAbstractor())
    await ok.weave(OCEAN_GRANDMOTHER, grammar=GRAMMAR, drift=0.4)
    blob = " ".join(record.getMessage() for record in caplog.records).lower()
    for token in REFERENCE.lower().split() + OCEAN_GRANDMOTHER.lower().split():
        if len(token) >= 5:
            assert token not in blob


# ---------------------------------------------------------------------------
# LLMAbstractor (mocked transport — never a live server)
# ---------------------------------------------------------------------------


def llm_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_llm_abstractor_parses_clean_json():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode()
        return completion(
            '{"motifs": ["inherited memory", "vast blue depth"], "register": "elegiac", '
            '"valence": 0.3, "intensity": 0.6, "movement": "slow, spiralling inward", '
            '"elemental": ["water", "deep blue"]}'
        )

    async with llm_client(handler) as client:
        abstractor = LLMAbstractor(
            base_url="http://localhost:11434/v1",
            model="qwen3:14b",
            api_key="secret",
            client=client,
        )
        theme = await abstractor.abstract(OCEAN_GRANDMOTHER, MoodState(energy=0.4))

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["auth"] == "Bearer secret"
    assert theme.motifs == ["inherited memory", "vast blue depth"]
    assert theme.register == "elegiac"
    assert theme.valence == pytest.approx(0.3)
    assert theme.elemental == ["water", "deep blue"]
    assert validate_theme(theme, OCEAN_GRANDMOTHER).ok


async def test_llm_abstractor_extracts_json_from_chatter():
    noisy = (
        "<think>the speaker is nostalgic</think>\nSure! Here is the object:\n"
        '```json\n{"motifs": ["a shape passed down"], "register": "ambient", '
        '"valence": 0.4, "intensity": 0.5, "movement": "slow drift", '
        '"elemental": ["dust"]}\n```\nHope that helps.'
    )
    async with llm_client(lambda r: completion(noisy)) as client:
        theme = await LLMAbstractor(
            base_url="http://x/v1", model="m", client=client
        ).abstract("anything")
    assert theme.motifs == ["a shape passed down"]
    assert theme.movement == "slow drift"


async def test_llm_abstractor_clamps_hostile_payload():
    payload = (
        '{"motifs": ["a"' + ', "b"' * 20 + '], "register": 12, "valence": 9.5, '
        '"intensity": "loud", "movement": null, "elemental": "single string"}'
    )
    async with llm_client(lambda r: completion(payload)) as client:
        theme = await LLMAbstractor(
            base_url="http://x/v1", model="m", client=client
        ).abstract("anything")
    assert len(theme.motifs) <= ThemeObject.MAX_MOTIFS
    assert theme.register == "ambient"
    assert theme.movement == "slow drift"
    assert 0.0 <= theme.valence <= 1.0 and 0.0 <= theme.intensity <= 1.0
    assert theme.elemental == ["single string"]


async def test_llm_abstractor_retry_prompt_differs():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read().decode())
        return completion('{"motifs": ["formless drift"]}')

    async with llm_client(handler) as client:
        abstractor = LLMAbstractor(base_url="http://x/v1", model="m", client=client)
        await abstractor.abstract("some window", attempt=0)
        await abstractor.abstract("some window", attempt=1)
    assert seen[0] != seen[1]
    assert "rejected as insufficiently abstract" in seen[1]


async def test_llm_abstractor_raises_content_free_errors():
    window = "miranda said her number is 4155550198"

    async with llm_client(lambda r: completion("no json here at all")) as client:
        with pytest.raises(AbstractionError) as exc:
            await LLMAbstractor(base_url="http://x/v1", model="m", client=client).abstract(window)
    assert "miranda" not in str(exc.value).lower()

    async with llm_client(lambda r: httpx.Response(500, text=window)) as client:
        with pytest.raises(AbstractionError) as exc:
            await LLMAbstractor(base_url="http://x/v1", model="m", client=client).abstract(window)
    assert "miranda" not in str(exc.value).lower()

    async with llm_client(lambda r: httpx.Response(200, json={"unexpected": 1})) as client:
        with pytest.raises(AbstractionError):
            await LLMAbstractor(base_url="http://x/v1", model="m", client=client).abstract(window)


async def test_llm_output_still_goes_through_the_validator():
    """Model-mediated stage 1 is not trusted: a leaky reply must be rejected."""
    leak = '{"motifs": ["miranda on the coast"], "register": "elegiac"}'
    async with llm_client(lambda r: completion(leak)) as client:
        weaver = Weaver(LLMAbstractor(base_url="http://x/v1", model="m", client=client))
        result = await weaver.weave(REFERENCE, grammar=GRAMMAR, drift=0.4)
    assert result.purge_requested and result.prompt is None


def test_llm_abstractor_requires_endpoint():
    with pytest.raises(ValueError):
        LLMAbstractor(base_url="", model="")


def test_theme_from_payload_handles_empty_dict():
    theme = theme_from_payload({})
    assert theme.motifs == [] and theme.register == "ambient"
    assert validate_theme(theme, "").ok


def test_build_abstractor_defaults_to_heuristic(monkeypatch):
    from egregore.config.schema import WeaverConfig, WeaverLLMConfig

    assert isinstance(build_abstractor(), HeuristicAbstractor)
    assert isinstance(build_abstractor(WeaverConfig(engine="heuristic")), HeuristicAbstractor)

    monkeypatch.setenv("EGREGORE_LLM_API_KEY", "k")
    cfg = WeaverConfig(llm=WeaverLLMConfig(base_url="http://localhost:11434/v1"))
    built = build_abstractor(cfg)
    assert isinstance(built, LLMAbstractor)
    assert built.api_key == "k"

    with pytest.raises(ValueError):
        build_abstractor(WeaverConfig(engine="llm"))


# ---------------------------------------------------------------------------
# Lexicon coverage — ordinary conversation must reach real imagery
# ---------------------------------------------------------------------------


ROOM_TALK = {
    "work": "I finally quit that job in March and the commute alone was killing me",
    "tech": "we rewrote the scheduler in Rust because the garbage collector was "
            "killing our tail latency during peak",
    "feeling": "I have been feeling really overwhelmed lately, like everything is "
               "happening at once and I cannot keep up",
    "light": "the light through those windows in the afternoon goes all golden and "
             "warm right before sunset",
    "memory": "my grandmother kept shells like that in a bowl by the door",
    "night": "we stayed up talking until it got light again",
}


def test_ordinary_conversation_reaches_real_imagery():
    """The failure this guards against is silent and total.

    When no keyword cluster matches, the abstractor backfills from
    DEFAULT_MOTIFS and every prompt becomes the same generic drift — the
    system looks like it is working while saying nothing about the room. A
    lexicon of literal nouns missed four of six real conversations.
    """
    from egregore.weaver.abstractor import DEFAULT_MOTIFS, HeuristicAbstractor

    a = HeuristicAbstractor()
    generic = []
    for name, text in ROOM_TALK.items():
        theme = a.abstract_sync(text)
        if list(theme.motifs) == list(DEFAULT_MOTIFS[: len(theme.motifs)]):
            generic.append(name)
    assert not generic, f"fell back to generic imagery for: {generic}"


def test_contentless_chatter_still_falls_back():
    # Falling back is correct when there is genuinely no theme; the point is
    # that it should be the exception, not the norm.
    from egregore.weaver.abstractor import DEFAULT_MOTIFS, HeuristicAbstractor

    theme = HeuristicAbstractor().abstract_sync("wait, is there any more of that")
    assert list(theme.motifs) == list(DEFAULT_MOTIFS[: len(theme.motifs)])


# ---------------------------------------------------------------------------
# The abstraction dial
# ---------------------------------------------------------------------------


def _theme():
    from egregore.types import ThemeObject

    return ThemeObject(
        motifs=["vast blue depth", "slow tidal pull"],
        elemental=["water", "deep blue"], register="ambient",
        valence=0.5, intensity=0.4, movement="slow drift",
    )


def test_abstraction_changes_how_the_motifs_are_drawn():
    from egregore.weaver.synthesis import synthesize_prompt

    literal = synthesize_prompt(_theme(), "G.", abstraction=0.0)
    pure = synthesize_prompt(_theme(), "G.", abstraction=1.0)
    assert "Depict these subjects directly" in literal
    assert "pure abstract imagery" in pure
    assert literal != pure
    # The motifs themselves are unchanged: only the instruction moves.
    for prompt in (literal, pure):
        assert "vast blue depth" in prompt and "slow tidal pull" in prompt


def test_abstraction_is_clamped_not_trusted():
    from egregore.weaver.synthesis import synthesize_prompt

    assert "Depict these subjects directly" in synthesize_prompt(
        _theme(), "G.", abstraction=-5.0)
    assert "pure abstract imagery" in synthesize_prompt(
        _theme(), "G.", abstraction=99.0)


async def test_a_literal_setting_does_not_weaken_the_privacy_floor():
    """Turning the dial down makes the *picture* more literal. It must not
    make the prompt any closer to what was said: the motifs still come from
    the closed lexicon and the validator still runs."""
    from egregore.weaver.abstractor import HeuristicAbstractor
    from egregore.weaver.validator import normalize_words, word_ngrams
    from egregore.weaver.weaver import Weaver

    said = ("we drove out to the coast last summer and the tide pools were "
            "glowing green at night, my grandmother kept shells in a bowl")
    w = Weaver(abstractor=HeuristicAbstractor())
    for abstraction in (0.0, 0.5, 1.0):
        result = await w.weave(said, grammar="G.", drift=0.4,
                               abstraction=abstraction)
        assert result.prompt is not None
        shared = (word_ngrams(normalize_words(result.prompt), 3)
                  & word_ngrams(normalize_words(said), 3))
        assert not shared, f"leak at abstraction={abstraction}: {sorted(shared)[:2]}"
        assert "grandmother" not in result.prompt.lower()


# ---------------------------------------------------------------------------
# Candidates — one validated theme per stretch of speech
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc  # noqa: E402

from egregore.weaver import Candidate  # noqa: E402


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
        seg("gears and pressure and copper machines in the workshop", 3),
    ]
    cands = await w.weave_candidates(segs, max_candidates=2)
    assert sorted(c.tokens for c in cands) == [9, 16]


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


async def test_fallback_theme_never_competes_with_a_real_one():
    # The abstractor returns DEFAULT_MOTIFS when a segment matches nothing.
    # In the first soak those "formless drift" candidates won selections
    # against real themes — an unmatched sentence must not outrank a matched
    # one. They survive only when nothing else did.
    from egregore.weaver.abstractor import DEFAULT_MOTIFS
    w = Weaver()
    segs = [
        seg("um yeah okay so anyway right", 1),                     # matches nothing
        seg("the tide pools were glowing green under the kelp", 2),  # matches
    ]
    cands = await w.weave_candidates(segs)
    assert len(cands) == 1
    assert tuple(cands[0].theme.motifs) != DEFAULT_MOTIFS
    only = await w.weave_candidates([seg("um yeah okay so anyway right", 1)])
    assert len(only) == 1, "with nothing else, the fallback still yields a candidate"


async def test_identical_themes_merge_and_their_words_add_up():
    w = Weaver()
    segs = [
        seg("the tide pools were glowing green under the kelp tonight", 1),
        seg("the tide pools glowing green under all that kelp", 2),
        seg("gears and pressure and copper in the workshop", 3),
    ]
    cands = await w.weave_candidates(segs)
    bags = [frozenset(c.theme.motifs) for c in cands]
    assert len(bags) == len(set(bags)), "no two candidates share a theme"
    tide = next(c for c in cands if "tide" in " ".join(c.theme.motifs).lower() or "depth" in " ".join(c.theme.motifs).lower())
    assert tide.tokens == 10 + 9, "merged candidate carries both segments' words"
    assert tide.ended_at == 2 and tide.started_at == 1 - 5


# ---------------------------------------------------------------------------
# Background abstraction — the render never waits on the LLM
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # noqa: E402


class SlowCounting:
    """An abstractor that takes a while and counts its calls, so a test can
    tell what was cached from what was computed."""

    name = "slow"

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.calls = 0
        self.abstraction = 1.0
        self.seen_abstraction: list[float] = []

    async def abstract(self, text, mood=None, *, attempt=0):
        self.calls += 1
        self.seen_abstraction.append(self.abstraction)
        await _asyncio.sleep(self.delay)
        words = text.split()
        return ThemeObject(motifs=[f"theme of {len(words)} words"], elemental=["salt"])


async def test_prime_abstracts_closed_segments_in_the_background():
    slow = SlowCounting()
    w = Weaver(slow)
    segs = [seg("the tide pools were glowing green tonight", 10),
            seg("gears and pressure and copper", 20),
            seg("still being said right now", 30)]        # open: may still grow
    w.prime(segs)
    await w.drain(timeout=2.0)
    assert slow.calls == 2, "the open segment is not abstracted until it closes"
    assert w.cached(segs[0]) is not None and w.cached(segs[2]) is None


async def test_weave_candidates_uses_the_cache_and_does_not_recompute():
    slow = SlowCounting()
    w = Weaver(slow)
    segs = [seg("the tide pools were glowing green tonight", 10),
            seg("gears and pressure and copper", 20),
            seg("still being said right now, this very thought", 30)]
    w.prime(segs)
    await w.drain(timeout=2.0)
    before = slow.calls
    cands = await w.weave_candidates(segs)
    assert len(cands) == 3
    assert slow.calls == before + 1, "only the open segment needed a fresh call"


async def test_an_uncached_slow_abstraction_falls_back_within_budget():
    slow = SlowCounting(delay=5.0)
    w = Weaver(slow, stage1_budget_s=0.1)
    cands = await w.weave_candidates([seg("the tide pools were glowing green under the kelp", 10)])
    assert len(cands) == 1
    assert "words" not in " ".join(cands[0].theme.motifs), "heuristic stood in for the slow brain"


async def test_abstraction_level_reaches_stage_one():
    slow = SlowCounting()
    w = Weaver(slow)
    w.abstraction = 0.2
    await w.weave_candidates([seg("the tide pools were glowing green under the kelp", 10)])
    assert slow.seen_abstraction[-1] == 0.2


async def test_llm_stage_one_asks_for_concrete_subjects_at_low_abstraction():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return completion(
            '{"motifs": ["glowing tide pools"], "register": "contemplative", '
            '"valence": 0.5, "intensity": 0.4, "movement": "slow", "elemental": ["water"]}'
        )

    async with llm_client(handler) as client:
        a = LLMAbstractor(base_url="http://x/v1", model="m", client=client)
        await a.abstract("we drove to the coast and the tide pools glowed", abstraction=0.2)
        assert "recognisable" in captured["body"].lower() or "concrete" in captured["body"].lower()
        await a.abstract("we drove to the coast and the tide pools glowed", abstraction=0.9)
        assert "concrete" not in captured["body"].lower()


def test_build_abstractor_autodetects_a_local_llm_server(monkeypatch):
    monkeypatch.setenv("EGREGORE_LLM_AUTODETECT", "1")
    from egregore.config.schema import WeaverConfig
    from egregore.weaver.abstractor import build_abstractor

    def probe(base_url: str) -> list[str] | None:
        if "1234" in base_url:
            return ["text-embedding-nomic", "ltx-video", "qwen/qwen3.8-27b"]
        return None

    a = build_abstractor(WeaverConfig(), probe=probe)
    assert isinstance(a, LLMAbstractor)
    assert a.model == "qwen/qwen3.8-27b", "skips embedding and video models"
    assert "1234" in a.base_url


def test_build_abstractor_stays_heuristic_when_nothing_answers():
    from egregore.config.schema import WeaverConfig
    from egregore.weaver.abstractor import build_abstractor

    a = build_abstractor(WeaverConfig(), probe=lambda url: None)
    assert isinstance(a, HeuristicAbstractor)


def test_build_abstractor_prefers_the_configured_model_when_listed(monkeypatch):
    monkeypatch.setenv("EGREGORE_LLM_AUTODETECT", "1")
    from egregore.config.schema import WeaverConfig
    from egregore.weaver.abstractor import build_abstractor

    cfg = WeaverConfig.model_validate({"llm": {"model": "small-one"}})
    a = build_abstractor(cfg, probe=lambda url: ["big-one", "small-one"] if "1234" in url else None)
    assert a.model == "small-one"
