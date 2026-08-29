# Soak report — 2026-08-29-1417-cloud-minimax-4b-v2

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 12 (9 paid, 3 fill) |
| paid prompts rendered from the no-match fallback theme | 1 |
| clips stored | 12 — fal 4, procedural 8 |
| local renders | 0 (0 seeded) |
| render wall (s) | median —, range — |
| lag, last word → clip on disk (s) | median 60.1, range 10.0–293.1 over 8 clips |
| age of the chosen thought when picked (s) | median 57.5, range 0.4–292.4 |
| candidates per selection | median 5.0, range 1.0–6.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 0 |
| paid cycles held for speech | 45 |
| ring buffer peak | 331 tokens |
| validator rejections / purges | 2 / 0 |
| chain / movements at end | 0 / 0 |
| what the screen showed (beacon samples) | fal 63%, procedural 34%, unknown 3% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 216s | prompt contains “impasto”: **380s** | 164s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 330s | prompt contains “Depict these subjects directly”: **380s** | 51s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 438s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 548s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–64s), 3 prompt(s)

- **20s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: dawn-bleached sand; tide pools; salt-kissed stone; fog-hung sky; cracked shoreline.
  - Elemental palette and material: salt, stone, water, light, sea.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ dawn-bleached sand; tide pools; salt-kissed stone; fog-hung sky; cracked shoreline (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **60s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: inherited memory; a shape passed down; layers folded over time.
  - Elemental palette and material: dust, warm ochre, patina.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ dawn-bleached sand; tide pools; salt-kissed stone; fog-hung sky; cracked shoreline (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **100s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: stagnant water; dull light; foggy air; cold stones; muted reflections.
  - Elemental palette and material: water, stone, fog, light, cold.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ dawn-bleached sand; tide pools; salt-kissed stone; fog-hung sky; cracked shoreline (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (109s–171s), 2 prompt(s)

- **150s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.4s, thought was 0.4s old
  - ▶ dawn-bleached sand; tide pools; salt-kissed stone; fog-hung sky; cracked shoreline (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **180s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: crumbling staircases; flickering neon signs; rusted gears in a clockwork spider; dripping water in a spiral; shattered mirrors reflecting infinite rooms.
  - Elemental palette and material: metal, light, water, glass, time.
  - chose from 6 candidate(s), listened 63.6s, thought was 15.0s old
  - ▶ crumbling staircases; flickering neon signs; rusted gears in a clockwork spider; dripping water in a spiral; shattered mirrors reflecting infinite rooms (sal 0.174 nov 0.889 rec 0.607 → 0.54)
  - · weathered metal ridge; glinting wires; dew-kissed grass; mist-laced sky; cracked concrete (sal 0.174 nov 0.889 rec 0.427 → 0.477)
  - · flickering shadows; rustling leaves; distant thunder (sal 0.186 nov 0.867 rec 0.21 → 0.399)
  - · flickering bulbs; rusty wires; storm-lashed windows; static cling; cracked porcelain (sal 0.163 nov 0.933 rec 0.155 → 0.391)

### forest-rain (216s–285s), 0 prompt(s)


### city-night (330s–393s), 1 prompt(s)

- **380s** — Depict these subjects directly and recognisably, photographed with real materials and real light: stacked geometry; channels of moving light; density folding in; heat blooming; slow combustion.
  - Elemental palette and material: concrete, sodium yellow, glass, fire, ember orange.
  - chose from 5 candidate(s), listened 34.1s, thought was 2.6s old
  - ▶ stacked geometry; channels of moving light; density folding in; heat blooming; slow combustion (sal 0.167 nov 0.947 rec 0.915 → 0.663)
  - · vast blue depth; surface breaking into light; slow tidal pull; stacked geometry; channels of moving light (sal 0.167 nov 0.947 rec 0.738 → 0.601)
  - · heat blooming; slow combustion; edges curling into ash; dark expanse, points of light; distance without edges (sal 0.217 nov 1.0 rec 0.547 → 0.567)
  - · stacked geometry; channels of moving light; density folding in; gathering pressure; sheets of moving air (sal 0.233 nov 0.947 rec 0.32 → 0.478)

### kitchen (438s–503s), 0 prompt(s)


### high-desert (548s–617s), 1 prompt(s)

- **651s** — Depict these subjects directly and recognisably, photographed with real materials and real light: heat blooming; slow combustion; edges curling into ash; dark expanse, points of light; distance without edges.
  - Elemental palette and material: fire, ember orange, smoke, void black, silver.
  - chose from 5 candidate(s), listened 87.0s, thought was 27.4s old
  - ▶ heat blooming; slow combustion; edges curling into ash; dark expanse, points of light; distance without edges (sal 0.181 nov 0.75 rec 0.401 → 0.414)
  - · gathering pressure; sheets of moving air; stillness before release (sal 0.167 nov 1.0 rec 0.177 → 0.258)
  - · silver dust falling; stardust pooling on stone; flickering lanterns; weathered rock formations; neon fog (sal 0.236 nov 0.889 rec 0.13 → 0.217)
  - · crimson stone; ashen dust; bending sky (sal 0.181 nov 0.923 rec 0.073 → 0.169)

## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 19 | None | None | 0 | 1 | screen-1:? |
| 300s | forest-rain | 331 | 0 | 25 | 19.3 | 6 | 0 | 6 | screen-1:fal |
| 601s | high-desert | 275 | 0 | 25 | 10.0 | 5 | 0 | 7 | screen-1:fal |
| 901s | high-desert | 27 | 0 | 25 | 293.1 | 3 | 0 | 12 | screen-1:fal |
| 961s | high-desert | 0 | 0 | 45 | 293.1 | 3 | 0 | 12 | screen-1:fal |

Raw samples: `soak-2026-08-29-1417-cloud-minimax-4b-v2.samples.jsonl`.
