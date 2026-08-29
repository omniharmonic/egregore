# Handoff — where Egregore stands, and where it goes next

*Written 2026-08-29 at the end of a long build-and-test session. Everything
described here is on `main` and pushed; 483 tests pass; the repo has no
other branches.*

## What it is

Egregore listens to a room, turns what is said into themes, renders those
themes as slow generative video, and plays a continuous loop on any screen
on the wifi — with WebGL effects over it. Speech never leaves the machine.
One laptop runs it; phones join as microphones or screens.

## What is proven, on real hardware, today

Every claim below was measured on an Apple M2 Max laptop with a scripted
conversation played through a loopback device (`tools/soak.py`) or spoken
into the real microphone. Reports with the numbers are in `docs/reports/`;
`COMPARISON-2026-08-29.md` is the summary.

- **The full local pipeline:** microphone → Parakeet → ring buffer → theme
  extraction → privacy validator → selection → prompt → ComfyUI/LTX-Video →
  clip store → manifest → screen. Continuity chains seed each clip from the
  last one's final frame (`seeded=True` in the log) and play through in
  order on the wall.
- **The cloud pipeline** (fal.ai MiniMax) on the same conversation: lag from
  the last word of a thought to its clip on disk, median **60s**, under a
  hard budget ceiling.
- **Pull scheduling:** a clip is requested only when the previous one has
  finished; the paid queue never exceeded depth 1 in any run. Lag is one
  render plus how old the chosen thought was.
- **Theme selection** from everything said during the render, scored by
  salience / novelty / recency with per-zone live sliders, with the
  abstractor's no-match theme never outranking a real one and stand-in
  themes yielding to worked-out ones.
- **A local LLM as the theme brain** (LM Studio / Ollama, auto-detected,
  smallest chat model preferred). Themes go from *"vast blue depth; surface
  breaking into light"* (heuristic) to *"dawn-bleached sand; tide pools;
  salt-kissed stone; fog-hung sky"* (4B model). Background priming means
  the render never waits on it; a brain that cannot answer unhurried is
  stood down and the status page says so.
- **Every live change takes effect on the next prompt or render:** grammar,
  abstraction, room bias, selection weights, quality level, stretch,
  boomerang, speed, crossfade, linger, effects. Restart-only values are
  listed in `docs/PARAMETERS.md`.
- **Local clips are polished after render** — 2× motion-interpolated slow
  motion then forward-and-back — so a 4s render is a 16s seamless loop for
  ~3s of CPU. Movements play in order with a short match cut at each seam.
- **Screens report what they play**; the dashboard flags a stuck one; the
  deck repicks on its own if a clip overstays.
- **A restart does not go dark:** the clip pool on disk is resumed.
- **Privacy:** `tests/test_privacy.py` sweeps prompts, logs, status and the
  clip store for sentinel phrases from the transcript. It has never failed.

## Measured trade-offs a host must know

| choice | cost | where it is written |
|---|---|---|
| local quality `fast` / `balanced` / `high` | ~90s / ~2 min / ~4.5 min per 4s clip on this laptop; resolution is the cost, steps are nearly free | README, PARAMETERS |
| a theme LLM on the same GPU as the renderer | ~+70% render time (110s → 190s) with a 4B model; a 27B is unusable (462s renders) | README "One GPU, two jobs" |
| abstraction 0.5 vs 0.85 | recognisable themes vs pure abstraction; 0.5 is the sweet spot for "it's listening" | PARAMETERS |
| room bias 1.0 | a quiet room makes every clip dark; 0.5 keeps energy and rhythm only | PARAMETERS |
| cloud | ~$0.25–0.40 per clip at standard prices, one ~35s render behind the room, no GPU contention | `presets/cloud.yaml`, README costs |

## Known limitations

- The last selection fixes (`standin_penalty`, priming thoughts closed by
  silence) and the movement-order playback landed *after* the last full
  soak. They are unit-tested and verified live by hand, not soaked. One more
  `tools/soak.py` run on `presets/soak-local.yaml` should show LLM themes
  winning later scenes, not only the first.
- Headless-browser timing is unreliable for judging playback pacing; judge
  pacing on a real screen (`?hud=1` shows the clip and how long it has
  been up).
- Local clips are dark at 512×320 with `room_bias` 1.0 (mean luminance
  ~35/255 in the first soaks). `room_bias: 0.5`, `high` quality, and a
  lighter effect stack (`feedback` smears) each help; not re-measured after
  the LLM and polish changes because the pool was wiped between runs.
- `high` quality was never soaked end to end.
- Only Qwen3-4B was small enough on this machine; other 4–8B models should
  behave the same (thinking is disabled for stage 1) but were not tried.
- The procedural renderer's palettes come from its own theme; a fill does
  not yet match the palette of the real clip it bridges into.
- `test_multi_zone_and_bleed` is flaky under CPU contention (a live party
  rendering on the same machine); it passes clean otherwise.

## Roadmap, in the order I would do it

1. **Soak the final selection and playback changes** (one run, 20 min) and
   fold the numbers into `COMPARISON`.
2. **Movement arcs.** A continuity chain plays as one long shot now; give it
   an arc — the seeded prompt for clip N+1 carries a movement phrase that
   swells, peaks and settles across the chain. Synthesis-only; no render
   cost.
3. **Fill as bridge.** Render a fill in the palette of the *next* real
   clip's theme rather than its own, so it dissolves into it instead of
   interrupting.
4. **Seed from the boomerang's peak** as an option, for "and then"
   continuity instead of "and back". A/B on the wall.
5. **Two-machine default for demos.** ComfyUI on a second box via
   `generation.comfyui_url` removes the GPU-sharing tax entirely; document
   it as the recommended demo rig. The DGX Spark plan is in
   `docs/dev/plans/NEXT-end-to-end-verification.md`.
6. **Luminance guard.** Measure each stored clip's mean luminance (ffmpeg
   `signalstats`, as the soak does) and, below a floor, bias the next
   prompt bright — closing the loop that `room_bias` opens.
7. **Native extension for backends that have it** (Veo): the Loom already
   offers `use_extend`; only the local and fal paths were exercised.
8. **A screen that stops changing should tell the operator**: the beacon
   and dashboard flag do this; a notification (sound, or a phone push
   through the join page) would close it.

## Where things are

```
README.md              the path a newcomer walks; hardware table; costs
AGENTS.md              the same walk for an AI assistant, with checks
docs/setup.md          four stages, each a working party
docs/PARAMETERS.md     every knob, default, live or restart
docs/local-hardware.md ComfyUI, LTX weights, Parakeet, graphs
docs/fal-setup.md      the cloud path
docs/signage.md        what guests must be told
docs/reports/          soak reports and the comparison
docs/dev/              design specs and implementation plans (history)
presets/               local, cloud, demo, two-rooms, soak-local, soak-cloud
presets/comfyui/       the render graphs
tools/soak.py          the end-to-end test rig
```

Runtime state lives outside the repo in `~/.egregore/` (`env` for keys at
mode 0600, `settings.yaml` for dashboard overrides — it silently overrides
presets; the banner lists what it changed — and `models.yaml` for the
cloud catalogue). Clips live in `var/clips-<preset>/`, resumed on restart,
removed with `uv run egregore wipe <preset>`.
