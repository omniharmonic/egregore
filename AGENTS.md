# AGENTS.md — setting up and running Egregore with an AI assistant

This file is for an AI coding assistant (Claude Code, Codex, Cursor, …) helping
a person install, configure, and run Egregore for a party. It is written so
you can follow it step by step, verify each step with a command, and know what
"working" looks like before moving on. A human reading it will do fine too.

Egregore is a Python 3.11 / FastAPI service with a vanilla-JS WebGL client.
It listens to a room, abstracts what is said into themes, renders them as
video, and plays a slow loop on screens. Speech never leaves the machine.

## 0. Ground rules

- Never log, print, store, or send transcript text. The only place it exists
  is an in-memory ring buffer. If a step would expose it, stop.
- Money is a hard ceiling (`budget.total_usd`). Never raise it without the
  person saying the number out loud. At `0` no cloud call is possible.
- Prefer changing a preset in `presets/` over editing code. Prefer changing
  a value on the dashboard over editing a preset. Everything meaningful is
  configuration.
- Verify with the commands here rather than assuming. Each section ends with
  what success looks like.

## 1. Install (every machine)

```bash
git clone https://github.com/omniharmonic/egregore && cd egregore
uv sync --extra dev                # core + tests
uv run egregore --help
```

If `uv` is missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
If `ffmpeg` is missing: `brew install ffmpeg` (macOS) or your package manager.

**Success:** `uv run egregore check presets/demo.yaml` prints `OK: 'Demo'`.

## 2. Prove the software with no hardware

```bash
uv run egregore run presets/demo.yaml
```

Open <http://localhost:8420/?zone=main> in a browser. Within ~30 seconds a
procedural clip appears with effects over it. Open
<http://localhost:8420/static/setup.html> and move the **speed** slider; the
motion changes without a reload.

**Success:** `curl -s localhost:8420/api/status | python3 -c "import sys,json;print(json.load(sys.stdin)['zones']['main']['active_clip_count'])"`
prints a number greater than 0 after a minute. Stop with Ctrl-C.

## 3. Probe what this machine can do

```bash
uv run egregore setup
```

Read the table it prints. Each row is a capability and how to get it:

| row | means | to fix |
|---|---|---|
| `ffmpeg` MISSING | procedural renderer cannot run | install ffmpeg |
| `comfyui` not running | no local video model | section 5 |
| `parakeet` not installed | no local transcription | section 4 |
| `audio_input` sounddevice not installed | no live microphone | `uv sync --extra mic` |
| `FAL_KEY` not set | no cloud video | paste it here, or skip |

It then asks for keys (blank to skip) and which preset to use. Keys go to
`~/.egregore/env` at mode 0600 and are never shown again.

## 4. Local transcription (Parakeet)

```bash
uv sync --extra mic
# Parakeet ONNX (int8) — see docs/local-hardware.md for the download; it lands at:
ls ~/.egregore/models/parakeet-v2-int8/config.json
```

On macOS the terminal needs microphone permission: System Settings → Privacy
& Security → Microphone. A `PortAudio -9986` error means it was denied.

**Success:** run `presets/local.yaml` with `EGREGORE_MONITOR=1`, speak near
the laptop, then `curl -s localhost:8420/api/status | python3 -c "import sys,json;print(json.load(sys.stdin)['zones']['main']['buffer_tokens'])"`
rises above 0. (The monitor endpoint shows the text itself; read it only on
the host, only to verify, never copy it anywhere.)

## 5. Local video (ComfyUI + LTX-Video)

ComfyUI must be running on `:8188` with the LTX-Video 2B weights and the
custom nodes listed in `docs/local-hardware.md`. Egregore ships the graphs
it needs in `presets/comfyui/`; the default pair is set with two environment
variables when starting a party:

```bash
EGREGORE_COMFY_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-balanced.json \
EGREGORE_COMFY_SEED_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-seeded.json \
uv run egregore run presets/local.yaml
```

How hard the GPU works is not in the graph: it is `generation.local_quality`
(`fast` ~80s a clip on an Apple-silicon laptop, `balanced` ~2 min,
`high` ~4 min), changeable live on the dashboard.

**Success:** `curl -s localhost:8188/system_stats` returns JSON, and within
~3 minutes of starting the party the log shows a line containing
`local clip backend=local` with a `wall=` time. The status page's zone row
shows a rising `clips` count and `lag` in seconds.

## 6. The theme brain (LM Studio or Ollama) — strongly recommended

Without an LLM, themes come from a fixed-vocabulary matcher and the wall
feels generic. With one, they come from the conversation.

Run LM Studio (server on `:1234`) or Ollama (`:11434`) with a **small chat
model** — 4B to 8B parameters is right; larger models take a minute per
thought and share the GPU with the video renderer. Egregore auto-detects the
server and picks the smallest chat model it lists. To pin one, set
`weaver.llm.model` in the preset.

**Success:** `curl -s localhost:8420/api/status | python3 -c "import sys,json;z=json.load(sys.stdin)['zones']['main'];print(z['weaver_engine'], z['weaver_model'])"`
prints `llm <model>`. If it prints `heuristic`, no server answered when the
party started; start one and restart the party.

## 7. Cloud video instead (fal.ai)

```bash
uv run egregore setup        # paste FAL_KEY
uv run egregore run presets/cloud.yaml
```

`budget.total_usd` in the preset is a hard ceiling. The dashboard's
**models** panel lists models and standard prices; a model is data, not code.

**Success:** the status page shows `spend` rising by the reservation per
clip and the `backends` row lists `fal` as healthy.

## 8. Phones and screens

Start any preset. The banner prints the join address. On a phone on the same
wifi, open it, pick a room, choose **listen**, **show**, or **both**. The
dashboard's **devices** panel shows each phone with its level; **mute** and
**remove** are there.

Any browser is a screen: `http://<host>:8420/?zone=<room>&screen=<name>`.
Add `&hud=1` to see what it is playing, its frame time, and its lag.

`presets/two-rooms.yaml` is the two-room, phones-as-microphones starting
point. `continuity.topology` (`independent` / `commons` / `mirror`) decides
whether rooms dream separately or together.

**Success:** `curl -s localhost:8420/api/status | python3 -c "import sys,json;print(json.load(sys.stdin)['now_playing'])"`
lists each screen with the clip it is showing and `shown_s`; a screen whose
`shown_s` keeps growing past a few minutes is stuck (the dashboard flags it).

## 9. Tuning the look, live

Open `/static/setup.html`. The panels, top to bottom:

- **look** — the grammar (the aesthetic every prompt is built on) and
  **abstraction** (0 literal … 1 abstract; 0.5 is the sweet spot).
- **generation** — backend, model, **local quality**, clip length, fill length,
  **room bias**, and the **theme model**.
- **zones** — per room: speed, crossfade, **linger**, the selection sliders
  (**dwelt on / new / fresh / pause**), the effect stack and its parameters.
- **monitor** — with `EGREGORE_MONITOR=1`: the transcript, every candidate
  theme with scores, the prompt that won. Host-only.

Every value has a note beside it. `docs/PARAMETERS.md` is the full list with
defaults and which few need a restart.

## 10. Running the party

```bash
EGREGORE_MONITOR=1 uv run egregore run presets/local.yaml
```

Leave it running. Watch `/static/status.html` for `clips`, `chain`, `lag`.
Ctrl-C stops it cleanly and cancels renders in flight. The clip pool on disk
is picked up again on the next start, so a restart does not go dark.

## 11. When something is wrong

| symptom | look at | likely |
|---|---|---|
| nothing like the conversation | `weaver_engine` in status | no LLM found; start one, restart |
| same clip for minutes | `now_playing.shown_s`; reload the screen | stuck deck (the watchdog now repicks) |
| clips too slow | `waited` in the zone row | GPU is the bottleneck; `local_quality: fast` |
| no clips at all, `held` rising | `buffer_tokens` | the room is silent, or the mic is not the one you think |
| everything dark | luminance of `var/clips-*/` | `room_bias: 0.5`, higher quality, gentler effects |
| preset values ignored | the start-up banner | `~/.egregore/settings.yaml` overrides; `--ignore-settings` |

`uv run pytest -q` must pass (about 470 tests, one minute) before any code
change is trusted. `tests/test_privacy.py` must never fail.

## 12. Repository map

```
egregore/app.py        the only place modules meet
egregore/scribe/       ring buffer (the privacy primitive), transcribers
egregore/weaver/       theme extraction, validator, selection, prompt synthesis
egregore/forge/        renderers (local ComfyUI, fal, veo, procedural), queue, clip store
egregore/loom/         continuity chains and the weighted playlist
egregore/listener/     audio features, VAD, microphones, phones
egregore/governor/     spend ledger and cadence
egregore/conductor/    the FastAPI app, sockets, node registry
lens/                  the browser client, effects, dashboard
presets/               party configurations; presets/comfyui/ the render graphs
tools/soak.py          the end-to-end soak test
docs/                  setup, hardware, fal, signage, parameters, reports
```
