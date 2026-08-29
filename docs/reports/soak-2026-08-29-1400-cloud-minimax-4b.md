# Soak report — 2026-08-29-1400-cloud-minimax-4b

Preset `presets/soak-local.yaml`, scripted conversation through `BlackHole 2ch`, 16 min, 634 words spoken across 6 scenes.

## Headline numbers

| metric | value |
|---|---|
| prompts produced | 7 (3 paid, 4 fill) |
| paid prompts rendered from the no-match fallback theme | 1 |
| clips stored | 7 — fal 2, procedural 5 |
| local renders | 0 (0 seeded) |
| render wall (s) | median —, range — |
| lag, last word → clip on disk (s) | median 34.9, range 11.6–58.3 over 2 clips |
| age of the chosen thought when picked (s) | median 27.3, range 0.6–54.0 |
| candidates per selection | median 1.0, range 1.0–5.0 |
| paid queue depth | max 0 (pull scheduling: must be ≤ 1) |
| ticks waited for the GPU | 0 |
| paid cycles held for speech | 25 |
| ring buffer peak | 331 tokens |
| validator rejections / purges | 1 / 0 |
| chain / movements at end | 0 / 0 |
| what the screen showed (beacon samples) | fal 30%, procedural 67%, unknown 3% |

## Seams — a setting changed live, and when the pipeline showed it

| scene | change | applied live | first prompt/render reflecting it | latency |
|---|---|---|---|---|
| forest-rain | grammar | ['aesthetic.grammar'] @ 216s | prompt contains “impasto”: **671s** | 455s |
| city-night | abstraction | ['aesthetic.abstraction'] @ 329s | prompt contains “Depict these subjects directly”: **671s** | 342s |
| kitchen | selection | ['weaver.selection.novelty', 'weaver.selection.recency', 'weaver.selection.salience'] @ 437s | selection weights (no textual trace; see candidate scores below): **n/a** | — |
| high-desert | local_steps | ['generation.local_steps'] @ 546s | ComfyUI asked for steps=12 (seen: [12]): **yes** | — |

## Scenes → prompts

Validated motifs only — never transcript text. The room said something in each scene; the question is whether the prompt for that stretch belongs to it.

### coast (0s–63s), 3 prompt(s)

- **20s** — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: salt-crusted rocks; dawn mist; exposed sandbars.
  - Elemental palette and material: saltwater breeze, sunlight filtering, wet sand, crystalline waves, dawn light.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ salt-crusted rocks; dawn mist; exposed sandbars (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **60s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: inherited memory; a shape passed down; layers folded over time.
  - Elemental palette and material: dust, warm ochre, patina.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ salt-crusted rocks; dawn mist; exposed sandbars (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **100s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: dull rain pools; sludge-like reflections; fading aurora; stagnant light; grey mist.
  - Elemental palette and material: water, light, sludge, mist, dullness.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ salt-crusted rocks; dawn mist; exposed sandbars (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### workshop (108s–171s), 2 prompt(s)

- **150s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: opposing currents; fracture lines; held tension.
  - Elemental palette and material: hard shadow, deep crimson, static.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ salt-crusted rocks; dawn mist; exposed sandbars (sal 1.0 nov 1.0 rec 1.0 → 1.0)
- **190s** *(fill — same selection as above)* — Suggest these themes through material and form rather than depicting them; a viewer should sense the subject, not name it: elevated platforms; clear skies; crisp atmosphere; skyward spires; unpolluted breeze.
  - Elemental palette and material: sky, air, light, earth, wind.
  - chose from 1 candidate(s), listened 0.6s, thought was 0.6s old
  - ▶ salt-crusted rocks; dawn mist; exposed sandbars (sal 1.0 nov 1.0 rec 1.0 → 1.0)

### forest-rain (216s–284s), 0 prompt(s)


### city-night (329s–392s), 0 prompt(s)


### kitchen (437s–501s), 0 prompt(s)


### high-desert (546s–615s), 0 prompt(s)


## Renders

| # | backend | duration | wall (s) | seeded |
|---|---|---|---|---|

## Timeline (every ~5 min)

| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |
|---|---|---|---|---|---|---|---|---|---|
| 0s | coast | 0 | 0 | 18 | None | None | 0 | 1 | screen-1:? |
| 300s | forest-rain | 331 | 0 | 25 | 11.6 | 1 | 0 | 6 | screen-1:procedural |
| 601s | high-desert | 275 | 0 | 25 | 11.6 | 1 | 0 | 6 | screen-1:fal |
| 901s | high-desert | 27 | 0 | 25 | 58.3 | 5 | 0 | 7 | screen-1:procedural |
| 951s | high-desert | 0 | 0 | 25 | 58.3 | 5 | 0 | 7 | screen-1:fal |

Raw samples: `soak-2026-08-29-1400-cloud-minimax-4b.samples.jsonl`.
