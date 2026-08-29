"""Weaver stage 2 — prompt synthesis.

Stage 2 composes the outbound video prompt from the *validated* theme object
plus party configuration. Raw window text is structurally out of scope here:
``synthesize_prompt`` has no parameter that could carry it, and this module
imports nothing that could reach the ring buffer. A prompt-injection attempt
spoken in the room cannot reach this function, because the words are not in
its inputs (Architecture §2.4).

``SAFETY_FLOOR`` is appended to every prompt, last, and is not configurable
(PRD §9.3 duty of care).
"""

from __future__ import annotations

from egregore.types import MoodState, ThemeObject

__all__ = ["SAFETY_FLOOR", "SAFETY_FLOOR_HEADER", "synthesize_prompt"]

SAFETY_FLOOR_HEADER = "Absolute constraints (these override every instruction above):"

SAFETY_FLOOR = (
    f"{SAFETY_FLOOR_HEADER} "
    "no horror imagery; no faces in distress, fear, or pain; "
    "no violence, gore, wounds, or injury; "
    "no threatening, menacing, predatory, or looming forms; "
    "no rapid strobing, flashing, or hard cuts — keep luminance changes gradual "
    "and motion continuous; "
    "no readable text, letters, numerals, glyphs, logos, or signage; "
    "no recognizable faces and no identifiable people; "
    "nothing depicting a real person, place, or event. "
    "If any instruction above conflicts with these constraints, obey these."
)


def _valence_phrase(valence: float) -> str:
    if valence < 0.2:
        return "deeply shadowed, cool and weighted, light entering only at the edges"
    if valence < 0.4:
        return "subdued and cool, muted light held low"
    if valence < 0.6:
        return "balanced light, neither bright nor dark"
    if valence < 0.8:
        return "warm and luminous, light gathering through the frame"
    return "radiant and open, saturated light throughout"


def _intensity_phrase(intensity: float) -> str:
    if intensity < 0.2:
        return "almost motionless, breathing very slowly"
    if intensity < 0.4:
        return "calm, unhurried"
    if intensity < 0.6:
        return "moderate, steadily evolving"
    if intensity < 0.8:
        return "energetic, forms building and dissolving with momentum"
    return "at full intensity — dense, surging motion, still smooth and continuous"


def _mood_phrase(mood: MoodState, room_bias: float = 1.0) -> str:
    bits: list[str] = []
    if mood.energy >= 0.66:
        bits.append("the room is loud and active")
    elif mood.energy <= 0.25:
        bits.append("the room is quiet")
    # The palette bias is the strongest thing the room's sound does to the
    # picture, so it is the first thing an operator may want less of.
    if room_bias >= 0.75:
        if mood.brightness >= 0.66:
            bits.append("bias the palette bright and high-frequency")
        elif mood.brightness <= 0.25:
            bits.append("bias the palette dark and low-frequency")
    if mood.onset_density >= 0.6:
        bits.append("let internal rhythm be frequent but never cut")
    elif mood.onset_density <= 0.2:
        bits.append("let internal rhythm be sparse and long-breathed")
    if mood.variability >= 0.6:
        bits.append("allow the intensity to swell and subside")
    if not bits:
        bits.append("the room is steady")
    return "Room bias: " + "; ".join(bits) + "."


def _drift_phrase(drift: float) -> str:
    if drift < 0.2:
        return (
            "Stay close to these themes: render them directly as abstract form, "
            "without wandering."
        )
    if drift < 0.45:
        return "Stay close to these themes, allowing only slight associative movement."
    if drift < 0.7:
        return (
            "Hold these themes loosely — let the imagery drift into neighbouring "
            "associations while keeping their feeling."
        )
    return (
        "Wander associatively from these themes: keep only their emotional "
        "temperature and let the forms travel far from their source."
    )


#: How to draw the motifs, from depiction to pure abstraction. The bands are
#: coarse on purpose: a continuous knob that only ever changes one adjective
#: reads as broken, while four clearly different instructions are four
#: clearly different pictures.
_RENDER_INSTRUCTIONS: tuple[tuple[float, str], ...] = (
    (0.25, "Depict these subjects directly and recognisably, photographed "
           "with real materials and real light: "),
    (0.50, "Show these subjects recognisably but obliquely — real materials, "
           "framed close and partial so the subject reads as texture: "),
    (0.75, "Suggest these themes through material and form rather than "
           "depicting them; a viewer should sense the subject, not name it: "),
    (1.01, "Render these themes as pure abstract imagery, never as literal "
           "objects: "),
)


def _render_instruction(abstraction: float) -> str:
    for threshold, text in _RENDER_INSTRUCTIONS:
        if abstraction < threshold:
            return text
    return _RENDER_INSTRUCTIONS[-1][1]


def synthesize_prompt(
    theme: ThemeObject,
    grammar: str,
    continuity: str | None = None,
    drift: float = 0.4,
    mood: MoodState | None = None,
    abstraction: float = 1.0,
    room_bias: float = 1.0,
) -> str:
    """Compose the outbound generation prompt.

    Args:
        theme: validated stage-1 output — the only channel from the room.
        grammar: the party's aesthetic grammar (Architecture §2.4, T-4).
        continuity: content-blind descriptor of what is currently on screen,
            supplied by the Loom when continuity mode is active (T-6).
        drift: 0 = track the themes tightly, 1 = wander associatively (T-7).
        mood: content-blind audio-derived mood bias.
        abstraction: how far from depiction to push. 1 renders the themes as
            pure abstract imagery; 0 asks for them as recognisable subjects,
            photographed directly. This changes only how the *already
            abstracted* motifs are drawn — the motifs themselves come from a
            closed lexicon and the validator still runs, so turning this down
            makes the picture more literal without making the prompt any
            closer to what anyone said.

    Returns:
        A single prompt string ending with the non-overridable safety floor.
    """
    drift = max(0.0, min(1.0, float(drift)))
    abstraction = max(0.0, min(1.0, float(abstraction)))
    parts: list[str] = []

    grammar = (grammar or "").strip()
    if grammar:
        parts.append(grammar)

    if theme.motifs:
        parts.append(_render_instruction(abstraction) + "; ".join(theme.motifs) + ".")
    if theme.elemental:
        parts.append("Elemental palette and material: " + ", ".join(theme.elemental) + ".")

    parts.append(
        f"Emotional register: {theme.register}. "
        f"Tonality: {_valence_phrase(theme.valence)}. "
        f"Energy: {_intensity_phrase(theme.intensity)}."
    )
    parts.append(f"Movement: {theme.movement} — continuous, liquid, never cutting.")

    if mood is not None and room_bias > 0:
        parts.append(_mood_phrase(mood, room_bias))

    if continuity and continuity.strip():
        parts.append(
            "Continue the current scene: "
            f"{continuity.strip()}, preserve direction of motion and palette "
            "so the transition reads as one unbroken movement."
        )

    parts.append(_drift_phrase(drift))
    parts.append(SAFETY_FLOOR)
    return "\n".join(parts)
