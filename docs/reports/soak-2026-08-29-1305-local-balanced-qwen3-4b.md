# Soak report — 2026-08-29-1305-local-balanced-qwen3-4b

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 9 (4 paid, 5 fill) |
| paid prompts rendered from the no-match fallback theme | 1 |
| clips stored | 8 — local 2, procedural 6 |
| local renders | 2 (2 seeded) |
| render wall (s) | median 282.6, range 278.5–286.8 |
| lag, last word → clip on disk (s) | median 361.2, range 302.0–420.4 over 2 clips |
| age of the chosen thought when picked (s) | median 40.0, range 0.1–141.8 |
| candidates per selection | median 1.0, range 1.0–5.0 |
| paid queue depth | max 1 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 665 |
| paid cycles held for speech | 28 |
| ring buffer peak | 307 tokens |
| validator rejections / purges | 0 / 1 |
| chain / movements at end | 8 / 1 |
| what the screen showed (beacon samples) | local 24%, procedural 73%, unknown 3% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 215s | prompt contains “impasto”: **250s** | 35s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 328s | prompt contains “Depict these subjects directly”: **370s** | 42s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 436s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 545s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–62s), 2 prompt(s)

- **60s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: formless drift; soft accumulation; quiet dispersal.
  - Elemental palette and material: muted spectrum, haze, slow light.
- **80s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.1s, thought was 0.1s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (107s–170s), 3 prompt(s)

- **110s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light.
  - Elemental palette and material: slate grey, charged air, vapour, water, deep blue.
  - chose from 1 candidate(s), listened 0.1s, thought was 0.1s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **190s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.1s, thought was 0.1s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **200s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: lattice of signals; recursive structure; precision unfolding.
  - Elemental palette and material: circuit teal, chrome, signal.
  - chose from 1 candidate(s), listened 0.1s, thought was 0.1s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### forest-rain (215s–283s), 1 prompt(s)

- **250s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges.
  - Elemental palette and material: water, deep blue, pressure, void black, silver.
  - chose from 1 candidate(s), listened 0.1s, thought was 0.1s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; vast blue depth; surface breaking into light (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### city-night (328s–391s), 1 prompt(s)

- **370s** — Depict these subjects directly and recognisably, photographed with real materials and real light: vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges.
  - Elemental palette and material: water, deep blue, pressure, void black, silver.
  - chose from 1 candidate(s), listened 141.8s, thought was 141.8s old
  - ▶ vast blue depth; surface breaking into light; slow tidal pull; dark expanse, points of light; distance without edges (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### kitchen (436s–500s), 0 prompt(s)


### high-desert (545s–614s), 1 prompt(s)

- **651s** — Depict these subjects directly and recognisably, photographed with real materials and real light: gathering pressure; sheets of moving air; stillness before release; heat blooming; slow combustion.
  - Elemental palette and material: slate grey, charged air, vapour, fire, ember orange.
  - chose from 5 candidate(s), listened 183.5s, thought was 40.0s old
  - ▶ gathering pressure; sheets of moving air; stillness before release; heat blooming; slow combustion (sal 0.184 nov 0.571 rec 0.869 → 0.771)
  - · dark expanse, points of light; distance without edges; quiet drift of bodies (sal 0.224 nov 0.667 rec 0.797 → 0.727)
  - · opposing currents; fracture lines; held tension; a slow ascent; light through a threshold (sal 0.224 nov 0.4 rec 0.728 → 0.645)
  - · inherited memory; a shape passed down; layers folded over time; descent; an absence given shape (sal 0.184 nov 1.0 rec 0.524 → 0.538)

## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|
| 1 | local | 4s | 286.8 | yes |
| 2 | local | 4s | 278.5 | yes |

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 19 | None | None | 1 | 1 | screen-1:? |
| 300s | forest-rain | 227 | 1 | 28 | None | 1 | 6 | 6 | screen-1:procedural |
| 601s | high-desert | 275 | 1 | 28 | 302.0 | 1 | 7 | 7 | screen-1:procedural |
| 901s | high-desert | 27 | 1 | 28 | 420.4 | 5 | 8 | 8 | screen-1:local |
| 951s | high-desert | 0 | 1 | 28 | 420.4 | 5 | 8 | 8 | screen-1:local |

Raw samples: `soak-2026-08-29-1305-local-balanced-qwen3-4b.samples.jsonl`.
