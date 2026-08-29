# Comparison — local vs cloud, heuristic vs LLM, one laptop

Eight soak runs on 2026-08-29, same 634-word six-scene scripted conversation
through a loopback device, same four live setting changes at scene
boundaries. Apple M2 Max, 32 GB. Each run's full report (candidates, scores,
prompts, timeline) is beside this file.

## The table

| run | renderer | theme brain | clips (real / fill) | render wall, median | lag, median | thought age, median | on the wall (real %) |
|---|---|---|---|---|---|---|---|
| 0020 | local fast 8/512×320 | heuristic | 8 / 5 | 101 s | 146 s | 42 s | 34 |
| 0051 | local fast, after selection fixes | heuristic | 10 / 4 | 82 s | 119 s | 36 s | 35 |
| A 1228 | local balanced 12/640×384 | **27B** LLM, same GPU | 1 / 6 | **462 s** | 473 s | 5 s | 10 |
| B 1248 | local balanced 12/640×384 | heuristic | 3 / 6 | 270 s | 321 s | 141 s | 24 |
| C 1305 | local balanced 12/640×384 | 4B LLM (guard tripped) | 2 / 6 | 283 s | 361 s | 40 s | 24 |
| D 1324 | local balanced **12/512×320** | 4B LLM (guard tripped) | 5 / 6 | 182 s | 273 s | 88 s | 42 |
| E 1342 | local balanced 12/512×320 | **4B LLM, corrected policy** | 5 / 5 | 192 s | 210 s | 45 s | 52 |
| F2 1417 | **fal MiniMax H3 Max 480p** | 4B LLM | 4 / 8 | ~35 s | **60 s** | 58 s | 63 |

*lag* is the last word of the winning thought to its clip on disk. *thought
age* is how old that thought already was when the render slot opened — a
quiet room, not a slow pipeline. *on the wall* is the share of now-playing
beacon samples showing a real (non-fill) clip.

## What the numbers settled

**A 27B model cannot share a laptop GPU with the renderer.** Run A: the LLM
took 175 s a call under load and the one LTX clip that finished took 462 s.
The wall was procedural for 90% of the run. The system now stands a brain
down when its *unhurried* calls fail and says so in the status page; a 4B
model with thinking disabled answers in 5–15 s under render load and never
trips it.

**Resolution is the cost, not steps.** 12 steps at 512×320: ~110 s (run
0051's seam went 82 → 108 s for 8 → 12 steps). 12 steps at 640×384: ~270 s.
The quality ladder is now `fast` 8/512×320, `balanced` 12/512×320,
`high` 12/640×384 — measured, not estimated.

**Sharing the GPU with a 4B brain costs about 70% render time.** Heuristic
balanced (12/512×320) renders in ~110 s; with the 4B priming themes in the
background, 182–192 s (runs D, E). That is the price of prompts made from
the conversation. Cloud rendering removes it entirely (F2): the LLM has the
GPU to itself and the wall is one ~35 s render behind the room.

**The LLM's themes are a different product.** Same coast lines, three
brains:

- heuristic: *vast blue depth; surface breaking into light; slow tidal pull*
- 27B (idle GPU, 58 s): *luminous pools; receding water; inherited domestic
  traces; quiet mineral memory*
- 4B (under load, 6 s): *dawn-bleached sand; tide pools; salt-kissed stone;
  fog-hung sky; cracked shoreline*

and the workshop scene through the 4B: *rusted gears in a clockwork spider;
dripping water in a spiral; shattered mirrors reflecting infinite rooms*.
The validator rejected 1–3 LLM themes per run for carrying transcript
phrases — the gate working.

**Selection needed three fixes, all from the data.** The abstractor's
no-match theme was outranking real ones (fixed 0051); the freshest thought
was the one never worked out, so it arrived as a heuristic stand-in and won
on recency (runs E, F2 — fixed after F2 with `standin_penalty` and priming
of thoughts closed by silence). Those last fixes are tested but not yet
soaked; the next run should show LLM themes winning later scenes, not only
the first.

**Live changes take effect on the next prompt.** Grammar and abstraction
changes showed in the next paid prompt in every run — 5–164 s later,
depending only on how long the current render had left. `local_quality`
reached ComfyUI as changed steps every time.

**Pull scheduling held.** Paid queue depth never exceeded 1 in any run.

## What to run with, today

- **Local, one laptop:** `presets/local.yaml` — balanced, 4B brain, expect
  ~3 min from the last word to the clip; the wall is ~50% generated video
  after the first ten minutes and rising as the pool grows.
- **Local, fastest video:** `weaver.engine: heuristic` — ~2 min, generic
  themes.
- **Cloud:** `presets/cloud.yaml` — ~1 min from the last word to the clip,
  the LLM's themes intact, ~$0.25–0.40 a clip, hard ceiling.
- **Two machines:** ComfyUI on the second box via `generation.comfyui_url`
  gives local video *and* the LLM's themes without the 70%.

## Not measured

Luminance of the LLM-era clips (the pool was wiped between runs); the
`high` level end to end; a 4B brain with cloud *and* a second local box.
