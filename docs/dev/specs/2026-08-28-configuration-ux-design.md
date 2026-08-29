# Configuration UX — design

**Status:** approved 2026-08-28

## Problem

Egregore is configured entirely by hand-written YAML presets, and its cloud
backends are selected by environment variables read once at start-up. Someone
who clones the repo has to read three documents and edit Python to try a model
we did not ship. The goal is that a newcomer can go from `git clone` to imagery
on screen without opening an editor, and can point the system at a different
video model without forking it.

Three constraints shape everything below.

**Secrets must not cross the party network.** `serving.bind` defaults to
`0.0.0.0:8420` and auth is disabled unless a password is set, so today anyone on
the venue wifi can reach `/api/*`. A settings endpoint that accepted API keys
would let a guest read or replace billable credentials.

**Structural changes mid-party are not safe.** Backends are constructed once and
the Forge may hold in-flight jobs; the spend ledger's ceiling is load-bearing
(PRD B-2) and moving it under a committed reservation breaks reconciliation.

**Prices are ceiling inputs, not decoration.** `max_plausible_cost` is computed
from the catalogue, so a mistyped price silently weakens the hard ceiling. This
is the main risk introduced by letting users define models through a form.

## Shape

    ~/.egregore/env             secrets, 0600, never read by the web layer
    ~/.egregore/settings.yaml   non-secret overrides, written by the UI
    ~/.egregore/models.yaml     model catalogue, written by the UI, hand-editable
    presets/*.yaml              unchanged; the shipped starting points

Effective config resolves as **preset YAML → settings overlay → env for
secrets**. A preset therefore remains a valid standalone config and nothing
breaks for someone who never opens the UI. All three runtime files live outside
the repository: this project sits in an iCloud-synced directory, and secrets
must not sync.

## Components

### `egregore/config/store.py` (new)

Owns the three runtime files. Pure I/O and merge logic, no web or party
concepts, so it can be tested without a server.

- `load_settings() -> dict` / `save_settings(dict)` — atomic write via a temp
  file and rename, so an interrupted save cannot truncate a working config.
- `apply_overlay(cfg: EgregoreConfig, overrides: dict) -> EgregoreConfig` —
  returns a new validated config; invalid overlays raise and leave the original
  untouched.
- `load_catalogue() -> dict[str, FalModel]` — built-ins first, then the user
  file overriding and extending by key.
- `save_catalogue(dict)`.
- `secrets_present() -> dict[str, bool]` — presence only. There is deliberately
  no function anywhere in this module that returns a secret's value to a caller
  that could serialise it.

`LIVE_KEYS` and `RESTART_KEYS` are declared here as the single source of truth
for which settings apply immediately, and are consumed by both the API and the
UI so the two cannot disagree.

### `egregore/cli.py` — `egregore setup`

The first-run path is a CLI wizard rather than a web page because it must work
before a server exists. It probes the machine (ffmpeg, ComfyUI on the configured
URL, Parakeet ONNX directory, audio input devices), reports what it found,
prompts for API keys, writes `~/.egregore/env` with mode 0600, offers a starting
preset, and prints the URL to open. Keys enter the system here or through the
environment, and nowhere else.

### Conductor endpoints

All four require the party password **even when party auth is otherwise
disabled**: configuring the system is a different trust level from watching it.
Where party auth is off entirely (no password configured at all), the settings
routes bind to loopback only.

- `GET /api/settings` — effective config, which keys are overridden, and the
  set of pending changes that need a restart.
- `POST /api/settings` — validate against the schema, apply the live subset to
  the running party, persist everything, return the new state.
- `GET /api/secrets` — `{"FAL_KEY": true, "GEMINI_API_KEY": false}`. Presence
  only; values never leave the process.
- `GET`/`POST`/`DELETE /api/models` — catalogue CRUD.

### Live versus restart

Live: cadence floor, clip duration, resolution, continuity mode, and the
existing freeze/mute. These are read per-cycle by the generation loop, so
applying them is assignment rather than reconstruction.

Restart: backend choice, model selection, API keys, zones, mics, and the budget
ceiling. The UI persists them and shows a pending-changes banner. The ceiling is
in this group deliberately — see Problem, above.

### `lens/setup.html` (new)

A separate page rather than growth in `status.html`, which is already 474 lines
and does its own job well. Linked from the dashboard header, sharing its visual
language and its join/password flow.

Two sections. **Settings**: backend, model, resolution, duration, budget,
cadence — live fields apply on change, restart fields mark themselves pending.
**Models**: the catalogue list plus an add/edit form.

The model form mitigates the price risk three ways: prices must parse as
positive decimals within a sane range; the form shows `reserves $X.XX per clip`
recomputed live from what has been typed, so a decimal-point error is visible
rather than silent; and saving shows the resulting worst-case cost for one clip
next to the configured budget ceiling.

### Catalogue becomes provider-aware

`FalModel` gains a `provider` field (`"fal"` today) so the catalogue can
describe entries for other backends without a second registry, and so the UI
dropdown is populated from merged data rather than a hardcoded list. Existing
entries default to `"fal"`, so this is additive.

## Testing

- `store.py`: merge precedence, atomic-write behaviour under an interrupted
  save, invalid overlay leaves config untouched, `secrets_present` never
  returns a value.
- Endpoints: password required even with party auth disabled; `GET /api/secrets`
  never contains a key's value under any input; live keys change a running
  party's behaviour; restart keys do not.
- Catalogue: user file overrides a built-in of the same key; a malformed entry
  is rejected rather than silently producing a zero price.
- A test asserting that no settings response body can contain a value from
  `~/.egregore/env`, mirroring the existing privacy leak-scan style.

## Out of scope

Hot-swapping the backend ladder, editing zones or mics through the UI, and
remote configuration over the public tunnel.
