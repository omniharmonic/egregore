# Egregore

**A collective dreaming engine for gathered spaces.**

Egregore listens to the conversation in a room, distils it into themes, and
renders those themes as slow, continuous, symbolic video on the walls. What
people say never leaves the building; only an abstracted, non-attributable
prompt is ever sent anywhere, and only if you choose a cloud renderer.

It runs on one laptop. Phones on the same wifi can join as microphones or as
screens. Everything about the look — how literal, how fast, how long each
composition lingers, which effects — is a slider you can move while the party
is running.

---

## Five minutes, no hardware

```bash
git clone https://github.com/omniharmonic/egregore && cd egregore
uv sync --extra dev
uv run egregore run presets/demo.yaml
```

Open <http://localhost:8420/?zone=main>. That is the whole pipeline — a
scripted conversation, theme extraction, the privacy gate, the generation
queue, the growing loop, the WebGL effects — with a procedural renderer
standing in for a video model. `ffmpeg` on your PATH is the only dependency.

Then open <http://localhost:8420/static/setup.html> and move things.

## A real party

Pick one of five presets. Each is a commented guide to every knob in it.

| preset | what it is | needs |
|---|---|---|
| `presets/local.yaml` | **The default.** Real mic, real transcription, real video, all on this machine. | ComfyUI + LTX-Video, Parakeet, a mic. LM Studio or Ollama strongly recommended. |
| `presets/cloud.yaml` | Video from fal.ai. No GPU needed. | a fal key; a hard budget ceiling you set |
| `presets/demo.yaml` | Nothing but the software. | ffmpeg |
| `presets/two-rooms.yaml` | Phones as microphones, one screen per room. | as `local`, plus phones |
| `presets/soak-*.yaml` | Test rigs that play a scripted conversation through a loopback device. | see `tools/soak.py` |

```bash
uv run egregore setup                 # probes the machine, stores keys, says what is missing
uv run egregore run presets/local.yaml
```

The banner prints two addresses:

- **screens:** `http://<this-machine>:8420/?zone=main` — open on any display
- **join:** `http://<this-machine>:8420` — guests open it on a phone

[docs/setup.md](docs/setup.md) walks the install for each piece of hardware.
[AGENTS.md](AGENTS.md) is the same walk written for an AI assistant doing it
with you.

## Adding devices

Anyone on the wifi opens the join address and is asked one question — **what
is this device?**

| choice | what it does |
|---|---|
| **listen** | its microphone joins a room; what is said there shapes the dream |
| **show** | it becomes a screen playing that room's loop |
| **both** | it listens and shows at once |

Many phones can listen in one room: every one feeds that room's transcript, so
a zone hears a conversation rather than one person. A phone gates on the
device — audio is sent only while someone is speaking — and what is sent is
transcribed on the host and never stored.

Screens are just browsers. A laptop, a TV's browser, a projector's Chromebox:
open `?zone=<room>` on it. Give a screen a name (`&screen=projector`) to set
its own effects and offset in the preset. The dashboard shows what every
screen is playing and flags one that has stopped changing.

Enrolling needs no password — that moment is the point — so control happens
afterwards on the dashboard: mute, remove, restyle.

### How rooms relate

`continuity.topology` decides whether a venue is several dreams or one:

| topology | microphones | loops |
|---|---|---|
| `independent` | each room hears itself | each room its own |
| `commons` | all rooms feed one conversation | each room its own look |
| `mirror` | all rooms feed one conversation | one loop on every screen |

## What the wall is doing, and the knobs that shape it

Everything below is live — change it on the dashboard and the next clip uses
it — unless marked *restart*. The full list with defaults is in
[docs/PARAMETERS.md](docs/PARAMETERS.md).

**Listening → themes.** Speech is transcribed on this machine into a ring
buffer that forgets after `privacy.ring_buffer_minutes`. Each stretch of talk
between pauses (`selection.segment_gap_s`) is abstracted into a *theme* — a
few motifs, a palette, a register, a movement — by whichever brain is
available: a local LLM (LM Studio or Ollama, auto-detected; the biggest single
lever on whether the wall feels like the room) or a built-in matcher with a
fixed vocabulary. A validator then rejects any theme that carries a phrase,
a name, a number, or an address from the transcript. Only validated themes go
further.

**Themes → the next clip.** A clip is requested only when the previous one has
finished rendering, never queued behind it, so the wall is exactly one render
behind the room. Everything said during that render is scored by three
sliders — **dwelt on** (what the room spent words on), **new** (distance from
what was just shown), **fresh** (what was said last) — and the winner is
written into a prompt built on your **grammar** (the look, editable on the
dashboard) at your **abstraction**: 0 depicts the subject recognisably, 1
renders it as pure abstraction, 0.5 is the sweet spot. **room bias** decides
how much the sound of the room pushes the palette.

**One GPU, two jobs.** On a single machine the theme brain and the video
renderer share the GPU. Measured on an Apple-silicon laptop: a 4B model
priming themes in the background slowed 12-step 512×320 renders from ~110s
to ~190s. That is the price of prompts made from the conversation; pay it,
or put ComfyUI on another box (`generation.comfyui_url`), or set
`weaver.engine: heuristic` for the fastest local renders.

**Rendering.** `generation.backend` (*restart*) picks the renderer: `local`
(ComfyUI + LTX-Video on this or another machine), `fal`, `veo`, or
`procedural`. On local, **local quality** is the knob: `fast` (~90s a clip on
a laptop), `balanced` (~2 min, the default), `high` (~4.5 min); a workstation
shifts all three. The procedural renderer fills gaps for free and is never
the material. `budget.total_usd` is a hard ceiling for cloud spend; at zero,
no cloud call is even possible.

**The loop.** Clips join a weighted playlist; in `continuity` mode each one
grows out of the last one's final frame, and a screen plays a movement
through in order with a short match cut at each seam. Local renders are
polished after the fact — **stretch** slows them with motion-interpolated
frames and **boomerang** plays them forward then back — so a 4-second render
becomes a 16-second seamless breath for three seconds of CPU. On each screen, **speed** slows the
motion, **crossfade** is how long one clip dissolves into the next, and
**linger** is the least time a composition stays up — a short clip dissolves
into itself rather than cutting away.

**The effects.** A stack of WebGL passes over the video, chosen per zone or
per screen, each with tunable parameters, all audio-reactive. They are a
fixed look you set, not something the conversation changes. If a screen
cannot keep up it lowers its internal resolution, never the look.

## Which settings for my hardware

| you have | run | set | expect |
|---|---|---|---|
| a laptop with an Apple-silicon or mid-range GPU | `presets/local.yaml` | `local_quality: balanced`, a 4B theme model, `local_stretch: 2` | a new 16s clip every ~3 min; the wall half generated video after ten minutes and rising |
| the same, and it feels slow | `presets/local.yaml` | `local_quality: fast`, or `weaver.engine: heuristic` | a clip every ~1.5–2 min; generic themes if the LLM is off |
| a workstation GPU (24 GB+) | `presets/local.yaml` | `local_quality: high`, `clip_duration_s: 6`, an 8B theme model | richer frames, longer shots, the same cadence |
| two machines on the LAN | `presets/local.yaml` | `generation.comfyui_url` → the GPU box; the theme model here | local video *and* LLM themes with no GPU contention |
| no GPU, or a big room | `presets/cloud.yaml` | a fal key and a budget | a clip every ~1 min, ~$0.25–0.40 each, hard ceiling |
| nothing yet | `presets/demo.yaml` | — | the whole pipeline on procedural video |

The numbers are from this laptop (M2 Max) and scale with the GPU. Whatever
the renderer, the effects run at the screen's own resolution, so the look
does not depend on the clip's.

## Is it actually listening?

The dashboard answers both questions. The **audio** panel draws the live
numbers the effects are driven by; with a fixture microphone (the demo) it
says so. The **monitor** panel — only when the party is started with
`EGREGORE_MONITOR=1`, and only readable from the host — shows the live
transcript, every candidate theme with its scores, and the prompt that won.
The status line shows the measured **lag** from the last word of the winning
thought to its clip landing.

## Keys

Set them on the dashboard's **keys** panel — it only accepts writes from the
host machine, no route ever returns a value, and the field clears on save —
or in the terminal:

```bash
uv run egregore setup          # writes ~/.egregore/env at mode 0600
```

The dashboard binds to the LAN by default so any screen can watch.
Configuration is a higher trust level: settings routes require the party
password even when party auth is off, and with no password at all they
answer only on loopback. Set `EGREGORE_PARTY_PASSWORD` before exposing a
party beyond a trusted network.

## Adding a cloud model

fal fronts a large catalogue behind one protocol, so a model is data, not
code. Use the **models** panel or write `~/.egregore/models.yaml`:

```yaml
kling-2-5:
  provider: fal
  model_id: fal-ai/kling-video/v2.5/text-to-video
  price_per_second: { 720P: "0.07" }
  default_resolution: 720P
  allowed_durations_s: [5, 10]
  extra_input: { cfg_scale: 0.5 }
```

> **The price is not decoration.** It is what the spend ceiling is computed
> from. Record the **standard** rate, never a promotional one. The panel
> shows the per-clip reservation as you type.

## Privacy

- Transcript text exists only in the zone's ring buffer and transiently in
  theme extraction. It is never logged, written to disk, or put in an error.
- Only a validated, abstracted prompt may reach a renderer, and a cloud one
  only when a budget is set.
- The feature bus that drives the effects carries numbers only.
- A phone microphone sends audio only while someone is speaking, over the
  local network, to be transcribed on the host and dropped. Say so on the
  signage ([docs/signage.md](docs/signage.md)).

*"put that down, smell this, it's basically the ocean… my grandmother kept
shells like that by the door"* becomes, with the LLM weaver:

> luminous pools; receding water; inherited domestic traces; quiet mineral
> memory — water, stone, light, salt

Zero shared three-word sequences with the source, no names.
`tests/test_privacy.py` is the test that must never fail.

## Costs

| Model | Rate | 5s clip | ~3h party (120 clips) |
|---|---|---|---|
| Local (LTX-Video on your GPU) | $0 | $0 | $0 |
| MiniMax H3 Max, 480P | $0.05/s | $0.25 | ~$30 |
| MiniMax H3 Max, 768P | $0.08/s | $0.40 | ~$48 |
| Veo 3.1 Fast (720p) | $0.10/s | $0.50 | ~$60 |

`budget.total_usd` is a ceiling the Governor cannot exceed. Reservations are
held at the standard rate with a safety factor, so a promo ending mid-party
cannot breach it.

## Troubleshooting

**Nothing looks like the conversation.** Is a local LLM running? The status
page shows the *weaver engine*; `heuristic` means no LLM was found. Start LM
Studio or Ollama with a small chat model (4–8B is plenty; it is auto-detected
on restart) and lower **abstraction** toward 0.3.

**The wall shows the same clip for minutes.** The dashboard flags it. Reload
the screen; the deck now repicks on its own if a clip overstays.

**Clips arrive too slowly.** Drop **local quality** to `fast`, or shorten
`clip_duration_s`. The status line's `waited` count says the GPU is the
bottleneck; `held` says the room was silent.

**Everything is dark.** Lower **room bias** to 0.5 or 0, raise local quality,
and try a lighter effect stack (`feedback` smears; `bloom` and `flow` are
gentler).

**The microphone produces `PortAudio -9986`.** macOS is refusing microphone
access to your terminal. System Settings → Privacy & Security → Microphone.

**A stale `~/.egregore/settings.yaml` overrides the preset.** The banner
lists every overridden value; `--ignore-settings` bypasses the file.

More in [docs/local-hardware.md](docs/local-hardware.md) (ComfyUI, LTX
weights, Parakeet) and [docs/fal-setup.md](docs/fal-setup.md).

## Testing it end to end

```bash
uv run pytest -q                                   # ~470 tests, a minute
uv run egregore run presets/soak-local.yaml &      # then, on macOS with BlackHole:
python3 tools/soak.py --log <the party log> --out docs/reports
```

The soak plays a six-scene scripted conversation into a loopback device,
changes grammar, abstraction, selection and quality live at scene boundaries,
and writes a report with real numbers: lag per clip, candidates per
selection, how long each change took to show, what each screen played.
Two are in [docs/reports](docs/reports).

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
```

`egregore/app.py` is the only place modules meet; every module imports only
`egregore.types`, `egregore.config.schema`, and the standard library.
`CONTRACTS.md` describes the boundaries. Design notes and plans live in
`docs/dev/`. **[docs/HANDOFF.md](docs/HANDOFF.md)** is the state of the
project — what is proven, the measured trade-offs, known limitations, and
the roadmap — written for whoever picks it up next.
