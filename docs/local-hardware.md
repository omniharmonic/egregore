# Running on local hardware

Egregore is meant to flex across very different boxes: a laptop that renders a
clip every few minutes, a workstation that is nearly live, a cloud API, or any
mix. Nothing below changes the pipeline — only which rung does the work and how
fast the room is asked for imagery.

## The cadence is not a constant

The cadence solver (`governor/cadence.py`) is purely economic: it answers *how
often can we afford this?* On a zero-budget local party that question has no
answer, so historically pacing fell back to a fixed floor that had nothing to do
with the hardware. A box needing 300 s per clip was still asked for one every
7.5 s, and the queue grew without bound — those extra requests were not more
imagery, only more backlog describing a room that had already moved on.

Pacing now has a second, physical half. `Governor` takes a `throughput_floor_s`
probe and never schedules faster than it reports:

    interval = max(budget_interval, throughput_floor())

`ComfyUIBackend` implements the probe by **measuring itself**. Each completed
render folds its wall time into an EWMA (`latency_smoothing`, default 0.3); the
first real observation replaces the seed outright, because a guess carries no
evidence worth averaging. `estimated_latency()` returns that value, so the
party re-paces itself as it runs, with no restart and no per-box tuning:

    wall=106.3s latency_est=106.3s     # seed (60 s) discarded
    wall=106.1s latency_est=106.2s     # EWMA
    cadence interval_s: 106.232        # Governor followed it

A second guard sits in front of the loop: if a zone already has
`EGREGORE_MAX_QUEUE_DEPTH` (default 3) clips pending, it stops asking. The
floor should make that unreachable; it exists for backends that slow down
*after* the estimate settles — a bigger model, a busy GPU, a degraded API.

## Choosing a video backend

`generation.backend` picks the preferred rung; `generation.fallback` is the
failover. The procedural ffmpeg renderer is always the last rung, so the dream
never starves regardless of GPUs, networks, or budgets.

| backend | needs | speed |
|---|---|---|
| `veo` | `GEMINI_API_KEY`, non-zero budget | seconds, metered |
| `local` | ComfyUI on `generation.comfyui_url` | hardware-dependent |
| `procedural` | ffmpeg only | instant, free |

## Choosing a ComfyUI graph

Two graphs are used: one for a fresh clip, one to continue a chain from the
previous clip's last frame. Point at them when starting a party:

    EGREGORE_COMFY_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-balanced.json \
    EGREGORE_COMFY_SEED_WORKFLOW=$PWD/presets/comfyui/ltxv-2b-seeded.json \
    uv run egregore run presets/local.yaml

How hard the GPU works is **not** in the graph. `generation.local_quality`
(`fast` / `balanced` / `high`) patches the sampler's steps and the latent
size into whichever graph is loaded, live from the dashboard, and
`local_steps` / `local_resolution` override it exactly. Measured on an Apple
M2 Max (32 GB, MPS), one 4 s clip, nothing else on the GPU:

| level | steps | size | per clip |
|---|---|---|---|
| `fast` | 8 | 512x320 | ~90 s |
| `balanced` | 12 | 512x320 | ~110 s |
| `high` | 12 | 640x384 | ~280 s |

Resolution is the cost; steps are nearly free by comparison. With a theme
LLM priming in the background on the same GPU, add roughly 70%.

Every install has its own node versions and filenames, so export your own
graph in **API format** if the shipped ones do not load. A graph only has to expose the three patch points Egregore writes into, located
by `class_type` so node ids may differ:

- a `CLIPTextEncode` whose `_meta.title` does **not** contain `negative` — the
  outbound prompt goes here
- `EmptyLTXVLatentVideo` — `length` is set to `duration_s * 24 + 1`
- `KSampler` — `seed` is randomised per clip

and produce a file under `gifs`, `videos`, or `images` in its history outputs.

### Model files this expects

    models/unet/ltx-video-2b-v0.9-Q6_K.gguf          # transformer (GGUF)
    models/text_encoders/t5-v1_1-xxl-encoder-Q5_K_M.gguf
    models/vae/ltx-video-2b-v0.9-vae.safetensors

The VAE must be in ComfyUI's key layout (`decoder.up_blocks.0.res_blocks.…`).
The diffusers export published under `vae/` in the Lightricks repo uses
`resnets` instead and omits `per_channel_statistics`, so ComfyUI rejects it —
extract the VAE from the single-file checkpoint instead:

```python
from safetensors import safe_open
from safetensors.torch import save_file
with safe_open("ltx-video-2b-v0.9.safetensors", "pt") as f:
    sd = {k[4:]: f.get_tensor(k) for k in f.keys() if k.startswith("vae.")}
save_file(sd, "models/vae/ltx-video-2b-v0.9-vae.safetensors")
```

Custom nodes required: **ComfyUI-GGUF** (city96) for the GGUF loaders and
**ComfyUI-VideoHelperSuite** for `VHS_VideoCombine`.

## Local transcription

`asr.engine: parakeet` prefers a local **int8 ONNX export** and falls back to
NeMo. The ONNX path is what makes Parakeet practical on Apple Silicon: NeMo is a
CUDA-first stack that installs poorly there, while the same TDT weights under
`onnxruntime` reach roughly 8x realtime on CoreML.

    pip install onnx-asr onnxruntime
    EGREGORE_PARAKEET_ONNX_DIR=~/.egregore/models/parakeet-v2-int8

The directory needs `encoder-model.int8.onnx`, `decoder_joint-model.int8.onnx`,
`nemo128.onnx`, `vocab.txt`, and a `config.json` giving `features_size` — a
model exported with 128 mel bins will fail at 80 with a dimension mismatch if
that file is missing.

Default lookup when the env var is unset: `~/.egregore/models/parakeet-v2-int8`.

## Live microphone

`mic.type: usb` with `device: null` takes the system default input.
`pip install sounddevice webrtcvad-wheels` — plain `webrtcvad` imports
`pkg_resources`, which modern setuptools no longer ships.

On macOS the terminal running Egregore needs Microphone permission (System
Settings -> Privacy & Security -> Microphone). Without it PortAudio fails at
stream open with `PaErrorCode -9986`, which reads like a device fault rather
than a permissions one.

## Overrides

| variable | effect |
|---|---|
| `EGREGORE_COMFY_WORKFLOW` | path to a ComfyUI graph in API format |
| `EGREGORE_COMFY_SEED_WORKFLOW` | the image-to-video graph used to continue a chain |
| `EGREGORE_MIN_CLIP_INTERVAL_S` | pin a minimum spacing between renders (the dashboard's cadence floor does the same, live) |
| `EGREGORE_PARAKEET_ONNX_DIR` | Parakeet ONNX model directory |
| `EGREGORE_PROCEDURAL_CODEC` | codec for the ffmpeg renderer |
