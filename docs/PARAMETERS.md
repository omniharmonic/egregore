# Every parameter, what it does, and whether it is live

Everything here lives in a preset (`presets/*.yaml`) and, unless marked
**restart**, can be changed while the party runs from the dashboard
(`/static/setup.html`) — the next clip uses the new value. Restart values are
the few that would rebuild the renderer ladder or move a spend ceiling that
reservations are already held against; the dashboard saves them and applies
them on the next run.

Saved dashboard changes go to `~/.egregore/settings.yaml` and override the
preset on later runs; the start-up banner lists every override, and
`--ignore-settings` skips the file.

## The look

| parameter | default | what it does |
|---|---|---|
| `aesthetic.grammar` | a cinematic paragraph | The paragraph every prompt is built on. The single strongest lever over how the party looks. Edit it on the **look** panel; four starting registers are offered. |
| `aesthetic.abstraction` | 0.5 in the presets (schema 1.0) | How literal to be. **0** depicts the subjects recognisably, photographed; **0.5** shows them obliquely — close, partial, as texture; **1** pure abstraction, never a literal object. At 0.5 and below the LLM weaver is also asked for concrete, recognisable motifs. |
| `aesthetic.drift` | 0.35 | How far a prompt may wander from the themes it was given. 0 stays close; 1 lets the imagery drift into neighbouring associations. |
| `aesthetic.room_bias` | 0.5 in the presets (schema 1.0) | How much the *sound* of the room shapes the palette. **1**: a quiet room asks for a dark, low-frequency palette and a loud one for bright. **0.5**: only energy and rhythm. **0**: the room is not mentioned. A quiet room at 1 makes every clip dark. |

## Listening → themes

| parameter | default | what it does |
|---|---|---|
| `weaver.engine` | `auto` | Which brain turns talk into themes. `auto`: a local LLM if one answers, else the built-in matcher. `llm` requires one. `heuristic` pins the matcher. **restart** |
| `weaver.llm.autodetect` | true | With no `base_url`, look for LM Studio (`:1234`) and Ollama (`:11434`) and use the smallest chat model they list. `EGREGORE_LLM_AUTODETECT=0` disables. **restart** |
| `weaver.llm.base_url` | none | An explicit OpenAI-compatible endpoint. **restart** |
| `weaver.llm.model` | `qwen3:14b` | Used if the server lists it; otherwise the smallest chat model is chosen. A 4–8B model is right for this task. **restart** |
| `weaver.stage1_budget_s` | 10 | How long a render slot may wait for a thought that was not already abstracted in the background before the matcher stands in. Thoughts are abstracted as they arrive, so this rarely binds. |
| `weaver.fallback_after_s` | 120 | Silence a room may sit in before a clip is rendered from mood and memory alone. Below this, free fills cover the screen; a mood-only render is for a real lull, not for the first minutes while people arrive. |
| `privacy.ring_buffer_minutes` | 6 | How long transcript text exists, in memory only. The guarantee the signage makes. ≤ 10. |
| `privacy.ring_buffer_max_bytes` | 8192 | A second cap that can only shorten retention. |
| `privacy.signage_required` | true | The join page shows the notice guests must see. |

## Which thought becomes the next clip

Per zone (`zones[].selection`) with a party default (`weaver.selection`). The
three weights need not sum to one.

| parameter | default | what it does |
|---|---|---|
| `selection.salience` (**dwelt on**) | 0.35 | Favours the thought the room spent the most words on. |
| `selection.novelty` (**new**) | 0.2 | Favours the thought furthest from what was just shown. High values pull against a continuity chain's coherence. |
| `selection.recency` (**fresh**) | 0.45 | Favours what was said most recently. Recency-forward keeps the wall with the conversation. |
| `selection.segment_gap_s` (**pause**) | 6 | A pause this long ends one thought and starts the next. |
| `selection.lookback_s` | none = 2× the last render, ≥ 90s | How far back a thought may come from. Older speech competes only when nothing newer exists. |
| `selection.max_candidates` | 6 | The longest thoughts considered per selection. |
| `selection.recency_tau_s` | none = the last render's duration | The time constant of the freshness decay, floored at 30s. |

## Rendering

| parameter | default | what it does |
|---|---|---|
| `generation.backend` | `auto` | `local` (ComfyUI + LTX-Video), `fal`, `veo`, `procedural`, or `auto` (cloud while a budget lasts, then local, then procedural). **restart** |
| `generation.fallback` | `procedural` | What renders when the backend cannot. `procedural` never lets the room go dark. **restart** |
| `generation.local_quality` | `balanced` | How hard the local GPU works per clip. `fast` (8 steps, 512×320, ~80s on an Apple-silicon laptop), `balanced` (12, 640×384, ~2 min), `high` (20, 768×448, ~4 min). A bigger GPU shifts all three. |
| `generation.local_steps` / `local_resolution` | none | Exact overrides of the level, field by field; blank means the level decides. Sizes are multiples of 32. |
| `generation.comfyui_url` | `http://127.0.0.1:8188` | Where ComfyUI is. Point it at another machine to move the GPU work there. **restart** |
| `generation.clip_duration_s` | 4 (local) / 6 (cloud) | Seconds per rendered clip. Local: what the model renders in time. Cloud models have their own allowed lengths and snap to the nearest. |
| `generation.fill_duration_s` | 12 | How long a free procedural fill runs. Separate from `clip_duration_s` because a fill costs nothing and should be long enough to linger on. Up to 16. |
| `generation.fal_model` | `minimax-h3-max` | A key from the models panel. **restart** |
| `generation.resolution` | `1080p` | What a cloud backend is billed at. |
| `budget.total_usd` | 0 | A hard ceiling on cloud spend for the party. At 0 no cloud call is possible. **restart** |
| `budget.spend_curve` | flat | How the budget is paced across the night (`at: "30%", rate: 1.2`). |

## The loop

| parameter | default | what it does |
|---|---|---|
| `continuity.default_mode` | `continuity` in `local` | `continuity`: each clip grows out of the last one's final frame, in movements of `max_chain_length`. `mosaic`: independent clips (cloud models cannot continue their own clips). |
| `continuity.max_chain_length` | 8 | Clips per movement before a fresh start. |
| `continuity.topology` | `independent` | `independent`: each room hears itself. `commons`: one conversation, a loop per room. `mirror`: one conversation, one loop on every screen. **restart** |
| `continuity.loop_half_life_min` | 40 | How long a clip stays in heavy rotation. |
| `continuity.loop_floor_weight` | 0.2 | Even old clips keep this much chance of playing. |
| `continuity.active_pool_max` | 120 | Clips kept in rotation before the oldest are archived. |
| `zones[].playback_rate` (**speed**) | 0.7 | Below 1 the motion is languid and each clip holds the screen longer. |
| `zones[].crossfade_s` (**crossfade**) | 4 | Seconds one clip takes to dissolve into the next. |
| `zones[].hold_s` (**linger**) | 20 | The least wall time a composition stays up. A shorter clip dissolves into itself to get there. 0 = its own length. |

## The effects

| parameter | default | what it does |
|---|---|---|
| `zones[].lens_stack` | `["flow", "smoke", "bloom"]` | The WebGL passes, in order. Known: `flow`, `smoke`, `feedback`, `bloom`, `liquid`, `kaleidoscope`, `chroma`, `glitch`, `pixelsort`, `crt`, `corrupt`. Each has up to four parameters on the dashboard. All are audio-reactive; none change with the conversation. |
| `screens[].lens_stack` | inherits the zone | A named screen's own stack. |
| `screens[].loop_phase_offset` | 0 | Where in the pool a screen starts, so two screens in one room are not in step. |
| `screens[].audio_source` | `zone` | `zone` drives effects from the room's microphones; `local_mic` from the screen's own. |
| `?adapt=` on a screen's address | `scale` | What gives when a frame runs long: `scale` lowers internal resolution (the look stays), `passes` drops effects, `off` never adapts. |

## Rooms and devices

| parameter | default | what it does |
|---|---|---|
| `zones[].mic.type` | `fixture` | `usb` (a device on this machine), `network` (phones that enrol), `fixture` (a scripted conversation). **restart** |
| `zones[].mic.device` | none = system default | A device name as `egregore setup` lists them. **restart** |
| `zones[].screens` | | Named screens in this room. |
| `asr.engine` | `parakeet` in real presets | `parakeet`, `faster-whisper`, or `fixture`. **restart** |
| `serving.bind` | `0.0.0.0:8420` | Reachable from the LAN by default. **restart** |
| `serving.password_env` | `EGREGORE_PARTY_PASSWORD` | Set the variable to require a password to watch; settings always require it or loopback. |
| `party.duration_hours` | | Paces the budget and the loop's memory. |
| `demo_time_scale` | 1 | Speeds a scripted conversation up. Demo only. |

## Environment variables

| variable | what it does |
|---|---|
| `EGREGORE_MONITOR=1` | Expose the live transcript and candidate themes at `/api/monitor`, readable only from the host. |
| `EGREGORE_COMFY_WORKFLOW`, `EGREGORE_COMFY_SEED_WORKFLOW` | The ComfyUI graphs (text-to-video, image-to-video). `presets/comfyui/` ships them. |
| `EGREGORE_LLM_AUTODETECT=0` | Never look for a local LLM. |
| `EGREGORE_LLM_BASE_URL`, `EGREGORE_LLM_MODEL` | Pin the theme brain from the environment. |
| `EGREGORE_PARTY_PASSWORD` | Require a password to watch and to configure. |
| `EGREGORE_MIN_CLIP_INTERVAL_S` | Pin a minimum spacing between renders (the dashboard's cadence floor does the same, live). |
| `EGREGORE_MIN_TRANSCRIPT_WORDS` | Drop transcriptions shorter than this (default 3) — a room with music makes the recogniser invent short words. |
| `EGREGORE_HOME` | Where `env`, `settings.yaml` and `models.yaml` live (default `~/.egregore`). |
