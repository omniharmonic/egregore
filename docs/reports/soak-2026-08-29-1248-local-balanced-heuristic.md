# Soak report — 2026-08-29-1248-local-balanced-heuristic

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 10 (5 paid, 5 fill) |
| paid prompts rendered from the no-match fallback theme | 1 |
| clips stored | 9 — local 3, procedural 6 |
| local renders | 3 (3 seeded) |
| render wall (s) | median 270.4, range 262.5–320.5 |
| lag, last word → clip on disk (s) | median 321.3, range 283.7–566.8 over 3 clips |
| age of the chosen thought when picked (s) | median 140.8, range 0.6–296.3 |
| candidates per selection | median 2.0, range 1.0–5.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 702 |
| paid cycles held for speech | 26 |
| ring buffer peak | 332 tokens |
| validator rejections / purges | 0 / 0 |
| chain / movements at end | 1 / 2 |
| what the screen showed (beacon samples) | local 24%, procedural 73%, unknown 3% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 219s | prompt contains “impasto”: **240s** | 22s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 331s | prompt contains “Depict these subjects directly”: **611s** | 280s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 439s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 547s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–66s), 3 prompt(s)

- **10s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape.
  - Elemental palette and material: water, deep blue, pressure, ash grey, cold water.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **60s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: inherited memory; a shape passed down; layers folded over time.
  - Elemental palette and material: dust, warm ochre, patina.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **100s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (111s–174s), 2 prompt(s)

- **150s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: dark expanse, points of light; distance without edges; quiet drift of bodies.
  - Elemental palette and material: void black, silver, cold light.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **190s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### forest-rain (219s–286s), 2 prompt(s)

- **240s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges.
  - Elemental palette and material: water, deep blue, pressure, void black, silver.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **330s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; gathering pressure; sheets of moving air.
  - Elemental palette and material: water, deep blue, pressure, slate grey, charged air.
  - chose from 2 candidate(s), listened 296.3s, thought was 296.3s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; gathering pressure; sheets of moving air (sal 0.438 nov 0.333 rec 0.397 → 0.398)
  - · gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 0.562 nov 0.0 rec 0.437 → 0.393)

### city-night (331s–394s), 0 prompt(s)


### kitchen (439s–502s), 0 prompt(s)


### high-desert (547s–615s), 1 prompt(s)

- **611s** — Depict these subjects directly and recognisably, photographed with real materials and real light: dark expanse, points of light; distance without edges; quiet drift of bodies.
  - Elemental palette and material: void black, silver, cold light.
  - chose from 5 candidate(s), listened 256.3s, thought was 21.2s old
  - ▶ dark expanse, points of light; distance without edges; quiet drift of bodies (sal 0.224 nov 0.0 rec 0.933 → 0.769)
  - · opposing currents; fracture lines; held tension; a slow ascent; light through a threshold (sal 0.224 nov 0.4 rec 0.857 → 0.748)
  - · inherited memory; a shape passed down; layers folded over time; descent; an absence given shape (sal 0.184 nov 1.0 rec 0.633 → 0.625)
  - · dark expanse, points of light; distance without edges; quiet drift of bodies; branching growth; slow unfurling (sal 0.184 nov 0.4 rec 0.649 → 0.577)

## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|
| 1 | local | 4s | 320.5 | yes |
| 2 | local | 4s | 270.4 | yes |
| 3 | local | 4s | 262.5 | yes |

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 18 | None | None | 1 | 1 | screen-1:? |
| 300s | forest-rain | 332 | 1 | 26 | None | 1 | 6 | 6 | screen-1:procedural |
| 601s | high-desert | 275 | 0 | 26 | 566.8 | 2 | 8 | 8 | screen-1:procedural |
| 901s | high-desert | 27 | 1 | 26 | 283.7 | 5 | 1 | 9 | screen-1:local |
| 951s | high-desert | 0 | 1 | 26 | 283.7 | 5 | 1 | 9 | screen-1:procedural |

Raw samples: `soak-2026-08-29-1248-local-balanced-heuristic.samples.jsonl`.
