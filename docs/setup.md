# Setup — from arrival to first imagery in under 30 minutes

## 0. What you need

- **Core node**: any Linux/macOS machine. For fully local generation: one
  machine with a decent GPU (single-box mode is first-class — see
  `presets/local.yaml`).
- **Screens**: anything with a browser (laptop, mini-PC, TV stick, tablet).
- **Mics**: USB condenser per zone, placed away from speakers.
- **Network**: a dedicated router/AP for the party. Not venue guest wifi.
- `ffmpeg` on the core node's PATH.

## 1. Install (5 min)

```bash
git clone <this repo> && cd egregore
uv sync                      # core install — no GPU deps required
# optional, per hardware:
uv sync --extra asr          # faster-whisper local transcription
uv sync --extra mic          # sounddevice + webrtcvad for USB mics
```

## 2. Smoke test with zero hardware (2 min)

```bash
uv run egregore run presets/demo.yaml
```

Open `http://<core-ip>:8420/?zone=main` on any device on the network. Within
a couple of minutes, abstract imagery driven by a scripted conversation
appears. If this works, the whole pipeline works; everything else is
swapping inputs and backends.

## 3. Pick and edit a preset (10 min)

Copy the closest preset from `presets/` and edit:

- `zones`: one per listening area, with its mic device and screens.
- `budget.total_usd`: **this is the hard ceiling.** `0` = no cloud, ever.
- `aesthetic.grammar`: the party's visual language — the highest-leverage
  text in the system. Iterate on it.
- `lens_stack` per zone/screen, from the ten lenses: `feedback`,
  `kaleidoscope`, `flow`, `chroma`, `bloom`, `liquid` (organic), and
  `glitch`, `pixelsort`, `crt`, `corrupt` (structured glitch art —
  content-derived block displacement, luminance sorting, phosphor CRT,
  datamosh-style corruption; all clear to clean when the room is quiet).
  Tune live on any screen with `?stack=flow,glitch,crt`.
- `serving.password_env`: export `EGREGORE_PARTY_PASSWORD=...` before start
  (or leave unset on a trusted LAN — auth is then disabled).

### Fully local stack (one GPU box)

1. ASR: `asr.engine: faster-whisper` (CPU-capable) or `parakeet` (NeMo).
2. Themes: run Ollama (`ollama pull qwen3:14b`), set `weaver.llm.base_url:
   http://localhost:11434/v1`. Without an LLM the deterministic heuristic
   abstractor runs — coarser themes, same privacy guarantees.
3. Video: ComfyUI with an LTX-2 workflow at `generation.comfyui_url`. The
   `procedural` ffmpeg renderer is always available underneath it and needs
   nothing at all.

### Cloud tier (optional)

`export GEMINI_API_KEY=...`, set `generation.backend: auto` and a real
budget. The ladder is cloud → local → procedural; exhausted budget or an
outage fails over silently.

## 4. Start the night (2 min)

```bash
uv run egregore run my-party.yaml
```

The console prints the join URL and password once. On each screen: open the
URL with its zone (`/?zone=hearth&screen=bar-panel`), enter password, tap to
go fullscreen. Done — walk away.

- Operator dashboard: `http://<core-ip>:8420/static/status.html` (terminal-
  style live view; raw JSON at `/api/status`). Controls: `POST
  /api/control/freeze|mute|mode` — freeze generation, mute a zone, switch
  mosaic/continuity live.
- HUD on any screen: append `?hud=1`.

## 5. Shutdown

Ctrl-C. Ring buffers zero on shutdown. Clips remain in `var/clips/` only if
`privacy.export_dream: true`; otherwise wipe them with
`uv run egregore wipe my-party.yaml`.
