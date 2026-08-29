# Soak report — 2026-08-29-1324-local-balanced-4b-v2

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 11 (7 paid, 4 fill) |
| paid prompts rendered from the no-match fallback theme | 2 |
| clips stored | 11 — local 5, procedural 6 |
| local renders | 5 (5 seeded) |
| render wall (s) | median 182.3, range 132.2–256.6 |
| lag, last word → clip on disk (s) | median 272.5, range 219.8–493.8 over 5 clips |
| age of the chosen thought when picked (s) | median 87.6, range 0.8–265.3 |
| candidates per selection | median 2.0, range 1.0–6.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 619 |
| paid cycles held for speech | 25 |
| ring buffer peak | 331 tokens |
| validator rejections / purges | 0 / 0 |
| chain / movements at end | 3 / 2 |
| what the screen showed (beacon samples) | local 42%, procedural 58% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 218s | prompt contains “impasto”: **240s** | 23s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 331s | prompt contains “Depict these subjects directly”: **511s** | 180s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 439s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 548s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–64s), 2 prompt(s)

- **30s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape.
  - Elemental palette and material: water, deep blue, pressure, ash grey, cold water.
  - chose from 1 candidate(s), listened 0.8s, thought was 0.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **100s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.8s, thought was 0.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (109s–173s), 2 prompt(s)

- **150s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.8s, thought was 0.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **190s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.8s, thought was 0.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### forest-rain (218s–286s), 2 prompt(s)

- **240s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges.
  - Elemental palette and material: water, deep blue, pressure, void black, silver.
  - chose from 1 candidate(s), listened 0.8s, thought was 0.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; descent; an absence given shape (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **280s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; heat blooming; slow combustion.
  - Elemental palette and material: water, deep blue, pressure, fire, ember orange.
  - chose from 2 candidate(s), listened 265.3s, thought was 265.3s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; heat blooming; slow combustion (sal 0.5 nov 0.571 rec 0.355 → 0.449)
  - · gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 0.5 nov 0.0 rec 0.429 → 0.368)

### city-night (331s–394s), 0 prompt(s)


### kitchen (439s–503s), 1 prompt(s)

- **511s** — Depict these subjects directly and recognisably, photographed with real materials and real light: inherited memory; a shape passed down; layers folded over time; descent; an absence given shape.
  - Elemental palette and material: dust, warm ochre, patina, ash grey, cold water.
  - chose from 4 candidate(s), listened 285.4s, thought was 48.1s old
  - ▶ inherited memory; a shape passed down; layers folded over time; descent; an absence given shape (sal 0.25 nov 1.0 rec 0.824 → 0.784)
  - · stacked geometry; channels of moving light; density folding in; gathering pressure; sheets of moving air (sal 0.25 nov 0.75 rec 0.513 → 0.51)
  - · branching growth; slow unfurling; reaching upward (sal 0.25 nov 1.0 rec 0.354 → 0.408)
  - · vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges (sal 0.25 nov 0.0 rec 0.317 → 0.278)

### high-desert (548s–617s), 0 prompt(s)


## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|
| 1 | local | 4s | 256.6 | yes |
| 2 | local | 4s | 228.4 | yes |
| 3 | local | 4s | 182.3 | yes |
| 4 | local | 4s | 132.2 | yes |
| 5 | local | 4s | 132.2 | yes |

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 18 | None | None | 1 | 1 | screen-1:procedural |
| 300s | forest-rain | 331 | 1 | 25 | 272.5 | 2 | 7 | 7 | screen-1:procedural |
| 601s | high-desert | 275 | 1 | 25 | 493.8 | 4 | 8 | 8 | screen-1:local |
| 901s | high-desert | 27 | 1 | 25 | 219.8 | 5 | 2 | 10 | screen-1:procedural |
| 961s | high-desert | 0 | 1 | 25 | 344.3 | None | 3 | 11 | screen-1:procedural |

Raw samples: `soak-2026-08-29-1324-local-balanced-4b-v2.samples.jsonl`.
