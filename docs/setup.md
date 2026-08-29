# Setup — from a fresh machine to a party

Four stages, each ending in something you can see. Stop at whichever one
matches the hardware you have; every stage is a working party.

If an AI assistant is helping you, hand it [AGENTS.md](../AGENTS.md) — the
same walk with a verification command at every step.

## 1. The software (5 minutes, any machine)

```bash
git clone https://github.com/omniharmonic/egregore && cd egregore
uv sync --extra dev
uv run egregore run presets/demo.yaml
```

Open <http://localhost:8420/?zone=main>. A scripted conversation is being
turned into themes, prompts and procedural video, with effects over it. This
is the whole pipeline; the rest is swapping in real inputs and a real
renderer.

Needs: `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and `ffmpeg`
(`brew install ffmpeg`).

## 2. A real microphone and real transcription (15 minutes)

```bash
uv sync --extra local          # sounddevice, webrtcvad-wheels, onnx-asr, onnxruntime
uv run egregore setup          # tells you what it found
```

**Parakeet** (recommended; ~8× realtime on Apple silicon): download the int8
ONNX export into `~/.egregore/models/parakeet-v2-int8/` — it needs
`encoder-model.int8.onnx`, `decoder_joint-model.int8.onnx`, `nemo128.onnx`,
`vocab.txt` and a `config.json` declaring `features_size: 128`. See
[local-hardware.md](local-hardware.md#local-transcription). Or use
`asr.engine: faster-whisper` (`uv sync --extra whisper`) on any CPU.

macOS asks the terminal for microphone permission the first time. If you see
`PortAudio -9986`, it was denied: System Settings → Privacy & Security →
Microphone.

Run `presets/local.yaml` with `generation.backend: procedural` for now — real
ears, free video — and speak near the machine. With `EGREGORE_MONITOR=1` the
dashboard's **monitor** panel shows what it heard and the themes it made.

## 3. A theme brain (5 minutes, strongly recommended)

Install [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.com),
load a **small chat model** (Qwen3-4B, Gemma-3-4B, Llama-3.1-8B — 4 to 8B is
the right size; bigger takes a minute per thought and fights the video
renderer for the GPU), and start the server. Egregore finds it on the next
start and says so in the status page's *weaver engine* row.

This is the difference between prompts made from a fixed vocabulary and
prompts made from what people actually said.

## 4. Local video (30–60 minutes, one GPU machine)

Install [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with two custom
nodes — **ComfyUI-GGUF** and **ComfyUI-VideoHelperSuite** — and the
LTX-Video 2B weights laid out as [local-hardware.md](local-hardware.md)
describes. Start it on `:8188`.

```bash
EGREGORE_COMFY_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-balanced.json \
EGREGORE_COMFY_SEED_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-seeded.json \
EGREGORE_MONITOR=1 uv run egregore run presets/local.yaml
```

The first clip lands in about two minutes at the default `local_quality:
balanced`. Too slow? `fast` on the dashboard. A workstation? `high`. The
ComfyUI machine need not be this one: set `generation.comfyui_url`.

### Or: cloud video instead of a GPU

```bash
uv run egregore setup            # paste FAL_KEY
uv run egregore run presets/cloud.yaml
```

`budget.total_usd` is a hard ceiling. See [fal-setup.md](fal-setup.md).

## 5. The room

- **Screens:** any browser. `http://<host>:8420/?zone=main&screen=<name>`,
  tap to go fullscreen. Name screens in the preset to give each its own
  effects.
- **Phones:** the join address the banner prints. *listen* makes a phone a
  microphone for a room; *show* makes it a screen. `presets/two-rooms.yaml`
  is the multi-room starting point.
- **Network:** a router you control, not venue guest wifi (which often blocks
  device-to-device traffic).
- **Signage:** [signage.md](signage.md). The join page shows the notice.
- **Password:** `export EGREGORE_PARTY_PASSWORD=...` before starting if the
  network is not trusted. Settings always need it or the host machine.

## 6. During and after

Everything on `/static/setup.html` is live: the grammar, abstraction, room
bias, quality, speed, crossfade, linger, the selection sliders, the effects.
`/static/status.html` shows clips, chain, lag, and what each screen is
playing. [PARAMETERS.md](PARAMETERS.md) explains every one.

Ctrl-C stops the party, cancels renders in flight, and zeroes the ring
buffers. Clips stay in `var/clips-*/` so the next start does not go dark;
`uv run egregore wipe <preset>` removes them.
