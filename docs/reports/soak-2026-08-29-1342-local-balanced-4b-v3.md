# Soak report — 2026-08-29-1342-local-balanced-4b-v3

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 11 (7 paid, 4 fill) |
| paid prompts rendered from the no-match fallback theme | 2 |
| clips stored | 10 — local 5, procedural 5 |
| local renders | 5 (5 seeded) |
| render wall (s) | median 192.4, range 164.4–194.4 |
| lag, last word → clip on disk (s) | median 209.7, range 200.8–343.8 over 5 clips |
| age of the chosen thought when picked (s) | median 45.1, range 0.7–157.3 |
| candidates per selection | median 4.0, range 1.0–6.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 633 |
| paid cycles held for speech | 25 |
| ring buffer peak | 332 tokens |
| validator rejections / purges | 3 / 0 |
| chain / movements at end | 2 / 2 |
| what the screen showed (beacon samples) | local 52%, procedural 48% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 216s | prompt contains “impasto”: **381s** | 164s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 329s | prompt contains “Depict these subjects directly”: **381s** | 52s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 439s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 547s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–64s), 3 prompt(s)

- **20s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves.
  - Elemental palette and material: salt, stone, light, water, wind.
  - chose from 1 candidate(s), listened 0.7s, thought was 0.7s old
  - ▶ exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **60s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: inherited memory; a shape passed down; layers folded over time.
  - Elemental palette and material: dust, warm ochre, patina.
  - chose from 1 candidate(s), listened 0.7s, thought was 0.7s old
  - ▶ exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **100s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.7s, thought was 0.7s old
  - ▶ exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (109s–171s), 3 prompt(s)

- **150s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: sunken wood; oily water; coral gardens; tide pools; sludge.
  - Elemental palette and material: salt, tide, currents, coral, moss.
  - chose from 1 candidate(s), listened 0.7s, thought was 0.7s old
  - ▶ exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **190s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: rusted clockwork; copper veins; squeezed light; tattered gears; dull oil.
  - Elemental palette and material: compressed air, worn metal, oil-stained stone, ticking weight, frozen grease.
  - chose from 1 candidate(s), listened 0.7s, thought was 0.7s old
  - ▶ exposed sandbars; cracked shoreline; dusk-hued sky; frosted air; hushed waves (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **210s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: crumbling staircases; wearing fabric; fading lanterns; recurrent cracks; dying embers.
  - Elemental palette and material: ash, echoes, shadows, cycles, decay.
  - chose from 4 candidate(s), listened 201.6s, thought was 45.1s old
  - ▶ crumbling staircases; wearing fabric; fading lanterns; recurrent cracks; dying embers (sal 0.221 nov 1.0 rec 0.791 → 0.633)
  - · salt-encrusted rocks; dusk-lit waves; dawn-ghosted dunes; wet sand; cracked earth (sal 0.25 nov 0.889 rec 0.351 → 0.423)
  - · cracked stone pools; bioluminescent water (sal 0.265 nov 0.786 rec 0.366 → 0.414)
  - · gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 0.265 nov 0.0 rec 0.47 → 0.304)

### forest-rain (216s–284s), 0 prompt(s)


### city-night (329s–394s), 1 prompt(s)

- **381s** — Depict these subjects directly and recognisably, photographed with real materials and real light: branching growth; slow unfurling; reaching upward.
  - Elemental palette and material: moss green, bark, sap.
  - chose from 4 candidate(s), listened 220.4s, thought was 123.7s old
  - ▶ branching growth; slow unfurling; reaching upward (sal 0.241 nov 1.0 rec 0.511 → 0.514)
  - · dripping awnings; emerald moss; shimmering leaves; misted light; quiet undergrowth (sal 0.241 nov 0.947 rec 0.442 → 0.473)
  - · rooftop grids; copper nets; dew-laced glass; skyward spires; sun-bleached tarps (sal 0.259 nov 1.0 rec 0.302 → 0.426)
  - · crumbling staircases; wearing fabric; fading lanterns; recurrent cracks; dying embers (sal 0.259 nov 0.0 rec 0.319 → 0.234)

### kitchen (439s–502s), 0 prompt(s)


### high-desert (547s–616s), 1 prompt(s)

- **571s** — Depict these subjects directly and recognisably, photographed with real materials and real light: opposing currents; fracture lines; held tension; a slow ascent; light through a threshold.
  - Elemental palette and material: hard shadow, deep crimson, static, cathedral blue, gold leaf.
  - chose from 6 candidate(s), listened 233.5s, thought was 15.1s old
  - ▶ opposing currents; fracture lines; held tension; a slow ascent; light through a threshold (sal 0.198 nov 1.0 rec 0.923 → 0.858)
  - · weathered card; oil-stained paper; stew pot; charred edges; dripping flame (sal 0.163 nov 0.947 rec 0.561 → 0.56)
  - · dark expanse, points of light; distance without edges; quiet drift of bodies; branching growth; slow unfurling (sal 0.163 nov 0.667 rec 0.584 → 0.55)
  - · flickering neon signs; rusted metal reflections; distant traffic trails; silent asphalt surfaces; fogged windows (sal 0.163 nov 1.0 rec 0.371 → 0.413)

## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|
| 1 | local | 4s | 192.5 | yes |
| 2 | local | 4s | 164.4 | yes |
| 3 | local | 4s | 194.4 | yes |
| 4 | local | 4s | 192.4 | yes |
| 5 | local | 4s | 186.4 | yes |

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 18 | None | None | 1 | 1 | screen-1:procedural |
| 300s | forest-rain | 332 | 1 | 25 | 200.8 | 4 | 6 | 6 | screen-1:procedural |
| 601s | high-desert | 275 | 1 | 25 | 318.2 | 6 | 8 | 8 | screen-1:procedural |
| 901s | high-desert | 27 | 1 | 25 | 207.5 | 6 | 1 | 9 | screen-1:procedural |
| 961s | high-desert | 0 | 1 | 25 | 343.8 | None | 2 | 10 | screen-1:local |

Raw samples: `soak-2026-08-29-1342-local-balanced-4b-v3.samples.jsonl`.
