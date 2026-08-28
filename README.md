# Egregore

**A collective dreaming engine for gathered spaces.**

Egregore listens to the conversations happening across a physical space,
distills them into abstract themes, and renders those themes as continuously
evolving, symbolic video on screens throughout the room. Speech never leaves
the building; only an abstracted, non-attributable prompt ever crosses the
network boundary, and only in cloud mode.

---

## Quickstart

No microphone, no GPU, and no API key required.

```bash
uv sync --extra dev
uv run egregore setup                    # probes the machine, stores any keys
uv run egregore run presets/demo.yaml
```

Then open:

| | |
|---|---|
| **Screens** | <http://localhost:8420/?zone=main> |
| **Status** | <http://localhost:8420/static/status.html> |
| **Settings** | <http://localhost:8420/static/setup.html> |

Demo mode is not a mock. It drives the whole real pipeline — ring buffer,
theme abstraction, privacy validator, spend ledger, generation queue, growing
loop, WebGL lens — from a scripted fixture conversation and a procedural
ffmpeg renderer. `ffmpeg` on your PATH is the only hard dependency.

## Three ways to run it

The backend ladder is ordered: the first rung that is healthy, affordable, and
working wins, and the procedural renderer is always last, so the dream never
starves regardless of GPUs, networks, or budgets.

| Backend | Needs | Speed | Cost | Try it |
|---|---|---|---|---|
| `procedural` | ffmpeg | instant | free | `presets/demo.yaml` |
| `local` | ComfyUI + a video model | ~1.5–5 min/clip | free | `presets/local-demo.yaml` |
| `fal` | `FAL_KEY` | ~1–2 min/clip | ~$0.025/s | `presets/fal-demo.yaml` |
| `veo` | `GEMINI_API_KEY` | ~1 min/clip | $0.05–0.60/s | `presets/house-party.yaml` |

Set `backend:` to your preference and `fallback:` to what should catch it.

## The cadence adapts to your hardware

Egregore does not ask a machine for imagery faster than it can produce it.
Each backend measures its own render time and the Governor paces generation to
what it reports, so one preset behaves correctly on a laptop and on a
datacentre GPU without being retuned:

```
wall=106.3s  latency_est=106.3s     # first real measurement replaces the seed
wall=106.1s  latency_est=106.2s     # EWMA
cadence interval_s: 60.0 -> 106.232 # the Governor followed it
```

If you want a fixed cadence instead, set **cadence floor** on the settings
page (or `EGREGORE_MIN_CLIP_INTERVAL_S`).

## Configuring it

Start with the **settings page** at `/static/setup.html`. Everything except
credentials can be changed there.

Configuration resolves in three layers:

```
presets/*.yaml            what you start from, checked into the repo
~/.egregore/settings.yaml overrides written by the settings page
~/.egregore/env           API keys, mode 0600, written by `egregore setup`
```

A preset stays a valid standalone config, so nothing breaks if you never open
the UI. All runtime files live outside the repository.

**What applies immediately, and what waits for a restart:**

| Live — next clip | Restart — next run |
|---|---|
| clip duration | backend and fallback |
| resolution | model selection |
| drift | API keys |
| continuity mode | budget ceiling |
| cadence floor | zones and microphones |
| freeze / mute | ComfyUI URL |

Backend choice and the budget ceiling are in the restart column deliberately:
rebuilding the ladder with clips in flight, or moving a ceiling that
reservations are already held against, both have real failure modes.

### Keys

Credentials are never entered through the browser and never sent to it. The
settings page reports `detected` or `not set` and points you at the wizard:

```bash
uv run egregore setup          # writes ~/.egregore/env at mode 0600
```

The dashboard binds to `0.0.0.0` by default, so anyone on the venue network
can watch the screens. Configuration is a different trust level: the settings
endpoints require the party password even when party auth is off, and where no
password is set at all they answer only on loopback.

Set a password with `EGREGORE_PARTY_PASSWORD` before exposing a party beyond a
trusted LAN.

## Adding a model

fal fronts a large catalogue behind one queue protocol, so adding a model is
data rather than code. Use the **models** panel on the settings page, or write
`~/.egregore/models.yaml` directly:

```yaml
kling-2-5:
  provider: fal
  model_id: fal-ai/kling-video/v2.5/text-to-video
  price_per_second: { 720P: "0.07" }
  default_resolution: 720P
  allowed_durations_s: [5, 10]
  extra_input: { cfg_scale: 0.5 }
```

`extra_input` carries per-model quirks — MiniMax's required
`prompt_expansion_mode` lives there rather than in the shared request builder.

> **The price is not decoration.** It is what the spend ceiling is computed
> from, so a wrong figure lets a party overspend. Record the **standard**
> rate, never a promotional one: a reservation is made before a generation
> that may land after the promo expires. The settings page shows the resulting
> per-clip reservation as you type, which is how a misplaced decimal becomes
> visible instead of silent.

Entries with a missing or non-positive price are dropped rather than
defaulted. Built-in models can be overridden by key but not deleted, so
upstream price corrections keep reaching you.

## Privacy

This is the part of the system with the least room for interpretation.

- Transcript text exists only inside the zone's ring buffer and transiently in
  the weaver's first stage. It is never logged, never written to disk, and
  never placed in an exception message.
- Only a validated, abstracted prompt may reach a backend, and only when a
  budget is set. A zero-budget preset cannot reach any cloud, whatever is in
  your environment.
- The feature bus that drives the shaders carries numbers only.

What that looks like on real speech — *"put that down, smell this, it's
basically the ocean… my grandmother used to keep shells like that in a bowl by
the door"* becomes:

> Render these themes as pure abstract imagery, never as literal objects:
> vast blue depth; surface breaking into light; slow tidal pull.
> Elemental palette and material: water, deep blue, pressure, pale gold

Zero shared three-word sequences with the source, and no proper nouns.
`tests/test_privacy.py` is the test that must never fail.

## Costs

Prices are per generated second, and video is billed whether or not you use
the audio track.

| Model | Rate | 5s clip | ~3h party (120 clips) |
|---|---|---|---|
| MiniMax H3 Max (480P, promo) | $0.025/s | $0.13 | ~$15 |
| MiniMax H3 Max (480P, standard) | $0.05/s | $0.25 | ~$30 |
| Veo 3.1 Fast (720p) | $0.10/s | $0.50 | ~$60 |
| Veo 3.1 (720p/1080p) | $0.40/s | $2.00 | ~$240 |

`budget.total_usd` is a hard ceiling the Governor cannot exceed, not a target.
Reservations are held against the standard rate with a 2× safety factor, so a
promo ending mid-party cannot breach it.

On Veo specifically, quota binds long before budget does — Tier 1 is reported
at around 10 video requests per day, and failed generations still consume it.
See [docs/veo-setup.md](docs/veo-setup.md).

## Troubleshooting

**The microphone produces `PortAudio -9986`.** macOS is refusing microphone
access to your terminal, and reports it as a device fault. Grant it in System
Settings → Privacy & Security → Microphone.

**`webrtcvad` fails to import with `No module named 'pkg_resources'`.** Install
`webrtcvad-wheels` instead; modern setuptools no longer ships `pkg_resources`.

**Parakeet fails with a dimension mismatch at 80 vs 128.** The ONNX directory
is missing its `config.json` declaring `features_size: 128`.

**ComfyUI rejects the LTX-Video VAE with `KeyError: post_quant_conv.weight`.**
The diffusers VAE published under `vae/` uses `resnets` where ComfyUI expects
`res_blocks`, and omits `per_channel_statistics`. Extract the VAE from the
single-file checkpoint instead — see
[docs/local-hardware.md](docs/local-hardware.md).

**LM Studio will not run a video model.** It has no video endpoint at all, and
returns HTTP 200 with an error body for any unknown route, which makes this
look like a different problem than it is. Video models need ComfyUI.

**ComfyUI is missing the GGUF or video nodes.** Install
[ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) and
[ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite).

## Development

```bash
uv run pytest -q          # the suite, offline: no GPU, no network, no audio device
uv run ruff check .
```

[CONTRACTS.md](CONTRACTS.md) describes the module boundaries and the
invariants that hold across them — read it before changing anything that
crosses a module.

## Documentation

- [Local hardware](docs/local-hardware.md) — ComfyUI, LTX-Video, Parakeet, mics
- [fal.ai setup](docs/fal-setup.md) — keys, catalogue, costs
- [Veo setup](docs/veo-setup.md) — keys, quota, pricing corrections
- [PRD](docs/egregore-01-prd.md) · [Architecture](docs/egregore-02-architecture.md) ·
  [Implementation plan](docs/egregore-03-implementation-plan.md)
- [Run of show](docs/run-of-show.md) · [Signage](docs/signage.md) ·
  [Setup](docs/setup.md)
