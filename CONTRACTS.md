# Egregore — Module Contracts (build coordination)

This file governs the parallel build. **`egregore/types.py` and
`egregore/config/schema.py` are frozen contracts** — read them, build against
them, do not edit them. If a contract is genuinely wrong, note it in your
final report instead of changing it.

Source documents: `docs/egregore-01-prd.md`, `docs/egregore-02-architecture.md`,
`docs/egregore-03-implementation-plan.md`. Architecture §2 defines each module.

## Ownership map (one agent per row — do not touch other rows' files)

| Module | Files | Responsibility |
|---|---|---|
| scribe | `egregore/scribe/`, `tests/test_scribe.py` | Ring buffer (privacy primitive), transcriber engines incl. fixture engine |
| weaver | `egregore/weaver/`, `tests/test_weaver.py` | Two-stage theme extraction + validator + prompt synthesis + safety floor |
| governor | `egregore/governor/`, `tests/test_governor.py` | Spend ledger, hard ceiling, cadence solver, spend curve |
| forge | `egregore/forge/`, `tests/test_forge.py` | VideoBackend impls (mock/veo/local), generation queue, failover ladder, clip store |
| loom | `egregore/loom/`, `tests/test_loom.py` | Continuity state machine, weighted playlist, manifests, last-frame extraction |
| listener | `egregore/listener/`, `tests/test_listener.py` | Audio features, VAD, mood integrator, fixture/simulated sources |
| conductor | `egregore/conductor/`, `tests/test_conductor.py` | FastAPI app: manifest, clips, feature bus WS, status, auth |
| lens | `lens/` | WebGL2 browser client, six lens shaders |
| integration | `egregore/app.py`, `egregore/cli.py`, `tests/test_privacy.py`, `presets/` | Wiring, demo mode, the privacy test |

## Cross-module rules

1. **Import only from `egregore.types`, `egregore.config.schema`, stdlib, and
   declared deps** (numpy, fastapi, httpx, pydantic, yaml). Never from a
   sibling module — the integration layer wires modules together.
2. **Privacy invariants** (non-negotiable, PRD §6.8):
   - Transcript text exists only inside `scribe.RingBuffer` and transiently in
     weaver stage-1 input. Never logged, never in `repr`, never on disk, never
     in an exception message.
   - Log token counts and occupancy, never content.
   - Only the validated outbound prompt string may reach a backend.
3. **Optional heavy deps** (nemo, faster-whisper, webrtcvad, sounddevice) are
   imported lazily inside the class that needs them, with a clear error if
   missing. The core install must work without any of them.
4. **Money is `Decimal`**, never float.
5. **Everything async-first** (asyncio). Blocking work (ffmpeg, model calls)
   goes through `asyncio.to_thread` or subprocess exec.
6. Python 3.11, type-annotated, ruff-clean (`ruff check` with repo config).
   Tests must pass offline with no GPU, no network, no audio device.
7. ffmpeg is available on PATH; shell out to it (no moviepy/opencv).

## Demo mode (how v1 is verified end-to-end in CI)

- `asr.engine: fixture` + `mic.type: fixture` — a scripted conversation file
  (lines with timestamps) plays into the ring buffer at real or scaled speed.
- Weaver runs its deterministic `HeuristicAbstractor` when no LLM endpoint is
  configured (keyword/lexicon → motifs, register, valence). Same two-stage
  path, same validator — only stage-1's brain is swapped.
- Forge `mock` backend renders real MP4s with ffmpeg `lavfi` procedural
  sources, parameterized by the ThemeObject (palette from valence/elemental,
  speed from intensity/movement). Supports `seed_image` (first frame overlay
  dissolve) so continuity handoff is exercised for real.
- The Lens client is served by the Conductor and verified in headless Chromium.
