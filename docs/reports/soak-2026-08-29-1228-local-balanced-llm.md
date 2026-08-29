# Soak report — 2026-08-29-1228-local-balanced-llm

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 8 (3 paid, 5 fill) |
| paid prompts rendered from the no-match fallback theme | 1 |
| clips stored | 7 — local 1, procedural 6 |
| local renders | 1 (1 seeded) |
| render wall (s) | median 462.0, range 462.0–462.0 |
| lag, last word → clip on disk (s) | median 472.6, range 472.6–472.6 over 1 clips |
| age of the chosen thought when picked (s) | median 5.3, range 0.4–10.2 |
| candidates per selection | median 1.0, range 1.0–4.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 688 |
| paid cycles held for speech | 28 |
| ring buffer peak | 332 tokens |
| validator rejections / purges | 0 / 0 |
| chain / movements at end | 7 / 1 |
| what the screen showed (beacon samples) | local 10%, procedural 90% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 219s | prompt contains “impasto”: **240s** | 22s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 333s | prompt contains “Depict these subjects directly”: **511s** | 178s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 442s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 556s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–64s), 1 prompt(s)

- **20s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape.
  - Elemental palette and material: water, deep blue, pressure, ash grey, cold water.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (109s–174s), 3 prompt(s)

- **110s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: inherited memory; a shape passed down; layers folded over time.
  - Elemental palette and material: dust, warm ochre, patina.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **120s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **170s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### forest-rain (219s–288s), 2 prompt(s)

- **240s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **250s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges.
  - Elemental palette and material: water, deep blue, pressure, void black, silver.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### city-night (333s–397s), 0 prompt(s)


### kitchen (442s–511s), 1 prompt(s)

- **511s** — Depict these subjects directly and recognisably, photographed with real materials and real light: inherited memory; a shape passed down; layers folded over time; descent; an absence given shape.
  - Elemental palette and material: dust, warm ochre, patina, ash grey, cold water.
  - chose from 4 candidate(s), listened 253.6s, thought was 10.2s old
  - ▶ inherited memory; a shape passed down; layers folded over time; descent; an absence given shape (sal 0.25 nov 0.4 rec 0.978 → 0.848)
  - · stacked geometry; channels of moving light; density folding in; gathering pressure; sheets of moving air (sal 0.25 nov 0.75 rec 0.75 → 0.7)
  - · branching growth; slow unfurling; reaching upward (sal 0.25 nov 1.0 rec 0.614 → 0.616)
  - · vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges (sal 0.25 nov 0.0 rec 0.578 → 0.487)

### high-desert (556s–626s), 0 prompt(s)


## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|
| 1 | local | 4s | 462.0 | yes |

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 22 | None | None | 1 | 1 | — |
| 301s | forest-rain | 332 | 1 | 28 | None | 1 | 6 | 6 | screen-1:procedural |
| 602s | high-desert | 231 | 1 | 28 | 472.6 | 4 | 7 | 7 | screen-1:procedural |
| 902s | high-desert | 38 | 1 | 28 | 472.6 | 4 | 7 | 7 | screen-1:procedural |
| 962s | high-desert | 0 | 1 | 28 | 472.6 | 4 | 7 | 7 | screen-1:local |

Raw samples: `soak-2026-08-29-1228-local-balanced-llm.samples.jsonl`.
