# Configuration UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let someone clone Egregore, run one command, and drive it — including models we never shipped — without opening an editor.

**Architecture:** A new `egregore/config/store.py` owns three files under `~/.egregore/` (secrets, settings overlay, model catalogue) and is the single source of truth for which settings apply live versus needing a restart. The Conductor grows four password-gated endpoints over that module; a new `lens/setup.html` drives them. Secrets are written only by a CLI wizard and are never returned by any endpoint.

**Tech Stack:** Python 3.11, pydantic v2, FastAPI, PyYAML, vanilla JS (no build step), pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-configuration-ux-design.md`

## Global Constraints

- Python 3.11, type-annotated, ruff-clean under the repo config (`line-length = 100`).
- Money is `Decimal`, never float (CONTRACTS.md rule 4).
- Never log or return a secret's value. `tests/test_privacy.py` must keep passing.
- Runtime files live at `~/.egregore/`, never inside the repo (it is iCloud-synced).
- `egregore/types.py` stays frozen. `egregore/config/schema.py` may gain **optional** fields only.
- Async-first; blocking file I/O in request handlers goes through `asyncio.to_thread`.
- Every task ends ruff-clean with the full suite green: `uv run ruff check . && uv run pytest -q`.

---

### Task 1: Config store — paths, settings overlay, secrets presence

**Files:**
- Create: `egregore/config/store.py`
- Create: `tests/test_config_store.py`

**Interfaces:**
- Consumes: `EgregoreConfig`, `load_config` from `egregore.config.schema`.
- Produces: `egregore_home()`, `SETTINGS_PATH`, `MODELS_PATH`, `ENV_PATH`, `LIVE_KEYS`, `RESTART_KEYS`, `load_settings()`, `save_settings(dict)`, `apply_overlay(cfg, overrides) -> EgregoreConfig`, `secrets_present() -> dict[str, bool]`, `SECRET_NAMES`.

- [ ] **Step 1: Write the failing tests**

```python
"""Config store tests — the overlay, the atomic write, and the secret wall."""

from __future__ import annotations

import pytest

from egregore.config import store
from egregore.config.schema import EgregoreConfig


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    return tmp_path


def test_settings_round_trip():
    assert store.load_settings() == {}
    store.save_settings({"generation": {"clip_duration_s": 6}})
    assert store.load_settings() == {"generation": {"clip_duration_s": 6}}


def test_save_is_atomic_and_leaves_no_temp_files(home):
    store.save_settings({"generation": {"resolution": "480p"}})
    leftovers = [p.name for p in (home).iterdir() if p.name != "settings.yaml"]
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_overlay_merges_deeply_without_dropping_siblings():
    cfg = EgregoreConfig.model_validate(
        {"generation": {"backend": "fal", "clip_duration_s": 5}}
    )
    merged = store.apply_overlay(cfg, {"generation": {"clip_duration_s": 8}})
    assert merged.generation.clip_duration_s == 8
    assert merged.generation.backend == "fal", "sibling key must survive the overlay"


def test_overlay_returns_a_new_config_and_leaves_the_original_alone():
    cfg = EgregoreConfig.model_validate({"generation": {"clip_duration_s": 5}})
    merged = store.apply_overlay(cfg, {"generation": {"clip_duration_s": 8}})
    assert cfg.generation.clip_duration_s == 5
    assert merged is not cfg


def test_invalid_overlay_raises_rather_than_half_applying():
    cfg = EgregoreConfig()
    with pytest.raises(ValueError):
        store.apply_overlay(cfg, {"generation": {"clip_duration_s": 999}})


def test_live_and_restart_keys_are_disjoint_and_dotted():
    assert store.LIVE_KEYS.isdisjoint(store.RESTART_KEYS)
    for key in store.LIVE_KEYS | store.RESTART_KEYS:
        assert "." in key or key in {"cadence_floor_s"}, key


def test_secrets_report_presence_only(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "super-secret-value")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    present = store.secrets_present()
    assert present["FAL_KEY"] is True
    assert present["GEMINI_API_KEY"] is False
    assert "super-secret-value" not in repr(present)
    assert all(isinstance(v, bool) for v in present.values())


def test_env_file_is_read_for_presence_but_never_returned(home):
    (home / "env").write_text('export FAL_KEY="from-file-value"\n')
    store.load_env_file()
    assert store.secrets_present()["FAL_KEY"] is True
    assert "from-file-value" not in repr(store.secrets_present())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_config_store.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'egregore.config.store'`

- [ ] **Step 3: Write the implementation**

```python
"""Runtime configuration files (spec: configuration UX).

Three files live under ``~/.egregore/`` — deliberately outside the repository,
which is an iCloud-synced directory that secrets have no business entering:

    env             API keys, mode 0600, written only by ``egregore setup``
    settings.yaml   non-secret overrides applied over a preset
    models.yaml     the user's video-model catalogue

Effective configuration is ``preset YAML -> settings overlay -> env`` for
secrets, so a preset stays a valid standalone config and nothing breaks for
someone who never opens the settings UI.

There is deliberately no function here that returns a secret's *value* to a
caller that could serialise it. :func:`secrets_present` reports booleans.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .schema import EgregoreConfig

#: Environment variables that hold credentials. Presence is reportable; the
#: values are not.
SECRET_NAMES: tuple[str, ...] = ("FAL_KEY", "GEMINI_API_KEY", "EGREGORE_PARTY_PASSWORD")

#: Settings the running party re-reads each generation cycle, so changing them
#: is an assignment rather than a reconstruction.
LIVE_KEYS: frozenset[str] = frozenset({
    "generation.clip_duration_s",
    "generation.resolution",
    "continuity.default_mode",
    "aesthetic.drift",
    "cadence_floor_s",
})

#: Settings that are read once at start-up. Changing these builds a different
#: backend ladder or moves a ceiling that reservations are already held
#: against, so they are persisted and applied on the next run.
RESTART_KEYS: frozenset[str] = frozenset({
    "generation.backend",
    "generation.fal_model",
    "generation.fallback",
    "generation.comfyui_url",
    "budget.total_usd",
    "asr.engine",
    "serving.bind",
})


def egregore_home() -> Path:
    """Runtime directory. ``EGREGORE_HOME`` overrides it, which is what the
    tests use to avoid touching a developer's real configuration."""
    return Path(os.environ.get("EGREGORE_HOME") or Path.home() / ".egregore")


def settings_path() -> Path:
    return egregore_home() / "settings.yaml"


def models_path() -> Path:
    return egregore_home() / "models.yaml"


def env_path() -> Path:
    return egregore_home() / "env"


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    """Write via temp file and rename, so an interrupted save cannot truncate
    a working configuration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as handle:
            yaml.safe_dump(data, handle, sort_keys=True, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_settings() -> dict:
    return _read_yaml(settings_path())


def save_settings(data: dict) -> None:
    _write_yaml(settings_path(), data)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_overlay(cfg: EgregoreConfig, overrides: dict) -> EgregoreConfig:
    """Return a new validated config with ``overrides`` merged in.

    Validation happens on the merged whole, so an override that is individually
    plausible but invalid in context is rejected before it can reach a party.
    The input config is never mutated.
    """
    merged = _deep_merge(cfg.model_dump(mode="json"), overrides or {})
    return EgregoreConfig.model_validate(merged)


def load_env_file() -> None:
    """Load ``~/.egregore/env`` into the process environment.

    Accepts the ``export KEY="value"`` form the setup wizard writes. Existing
    environment variables win, so an explicit export in the shell always beats
    the file. Values go into ``os.environ`` and are not returned.
    """
    for raw in _read_env_lines():
        key, _, value = raw.partition("=")
        key = key.removeprefix("export ").strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _read_env_lines() -> list[str]:
    path = env_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def secrets_present() -> dict[str, bool]:
    """Which credentials are set. Booleans only — never the values."""
    from_file = {
        ln.partition("=")[0].removeprefix("export ").strip() for ln in _read_env_lines()
    }
    return {
        name: bool(os.environ.get(name)) or name in from_file for name in SECRET_NAMES
    }


def write_secret(name: str, value: str) -> None:
    """Persist one credential to ``~/.egregore/env`` at mode 0600.

    Called by the setup wizard only. Nothing in the web layer imports this.
    """
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}; expected one of {SECRET_NAMES}")
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [ln for ln in _read_env_lines() if not ln.removeprefix("export ").startswith(f"{name}=")]
    kept.append(f'export {name}="{value}"')
    path.write_text("\n".join(kept) + "\n")
    path.chmod(0o600)


def value_at(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_config_store.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add egregore/config/store.py tests/test_config_store.py
git commit -m "Add a runtime config store with an overlay and a secret wall"
```

---

### Task 2: Provider-aware catalogue backed by a user file

**Files:**
- Modify: `egregore/forge/fal.py` (add `provider` to `FalModel`)
- Modify: `egregore/config/store.py` (add catalogue load/save)
- Modify: `tests/test_config_store.py`

**Interfaces:**
- Consumes: `FalModel`, `FAL_MODELS` from `egregore.forge.fal`; `_read_yaml`/`_write_yaml` from Task 1.
- Produces: `store.load_catalogue() -> dict[str, FalModel]`, `store.save_catalogue(dict[str, FalModel])`, `store.catalogue_to_json(dict[str, FalModel]) -> dict`, `FalModel.provider`.

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal

from egregore.forge.fal import FAL_MODELS, FalModel


def test_builtin_catalogue_loads_when_no_user_file():
    loaded = store.load_catalogue()
    assert set(loaded) >= set(FAL_MODELS)
    assert loaded["minimax-h3-max"].model_id == "minimax/h3-max/text-to-video"


def test_user_file_extends_and_overrides_builtins(home):
    (home / "models.yaml").write_text(
        "kling-2-5:\n"
        "  provider: fal\n"
        "  model_id: fal-ai/kling-video/v2.5/text-to-video\n"
        "  price_per_second: {720P: '0.07'}\n"
        "  default_resolution: 720P\n"
        "  allowed_durations_s: [5, 10]\n"
        "minimax-h3-max:\n"
        "  provider: fal\n"
        "  model_id: minimax/h3-max/text-to-video\n"
        "  price_per_second: {480P: '0.99'}\n"
        "  default_resolution: 480P\n"
        "  allowed_durations_s: [5]\n"
    )
    loaded = store.load_catalogue()
    assert loaded["kling-2-5"].price_per_second["720P"] == Decimal("0.07")
    assert loaded["minimax-h3-max"].price_per_second["480P"] == Decimal("0.99")


def test_prices_load_as_decimal_never_float(home):
    (home / "models.yaml").write_text(
        "m:\n  provider: fal\n  model_id: x/y\n  price_per_second: {480P: 0.05}\n"
        "  default_resolution: 480P\n  allowed_durations_s: [5]\n"
    )
    price = store.load_catalogue()["m"].price_per_second["480P"]
    assert isinstance(price, Decimal), "money is Decimal (CONTRACTS.md rule 4)"


def test_malformed_entry_is_dropped_not_priced_at_zero(home, caplog):
    (home / "models.yaml").write_text(
        "broken:\n  provider: fal\n  model_id: x/y\n  price_per_second: {}\n"
        "  default_resolution: 480P\n  allowed_durations_s: [5]\n"
    )
    loaded = store.load_catalogue()
    assert "broken" not in loaded, "a model with no price would weaken the ceiling"


def test_catalogue_round_trips_through_save(home):
    original = store.load_catalogue()
    store.save_catalogue(original)
    assert set(store.load_catalogue()) == set(original)


def test_catalogue_to_json_is_serialisable_with_string_prices():
    import json
    payload = store.catalogue_to_json(store.load_catalogue())
    json.dumps(payload)
    assert payload["minimax-h3-max"]["price_per_second"]["480P"] == "0.05"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_config_store.py -k catalogue`
Expected: FAIL — `AttributeError: module 'egregore.config.store' has no attribute 'load_catalogue'`

- [ ] **Step 3: Add `provider` to `FalModel`**

In `egregore/forge/fal.py`, add to the `FalModel` dataclass, immediately after `model_id`:

```python
    #: Which backend can drive this row. Only "fal" today; carried so the
    #: catalogue can describe other providers without a second registry.
    provider: str = "fal"
```

Note `provider` needs a default because it follows a required field.

- [ ] **Step 4: Add catalogue functions to `egregore/config/store.py`**

```python
def load_catalogue() -> dict:
    """Built-in models, then the user's file overriding and extending them.

    A malformed entry is dropped rather than defaulted. A model with no usable
    price would otherwise reserve nothing against the hard ceiling, which is
    exactly the failure PRD B-2 forbids.
    """
    from egregore.forge.fal import FAL_MODELS, FalModel

    merged = dict(FAL_MODELS)
    for key, raw in _read_yaml(models_path()).items():
        try:
            prices = {
                str(res): Decimal(str(price))
                for res, price in (raw.get("price_per_second") or {}).items()
            }
            if not prices or any(p <= 0 for p in prices.values()):
                raise ValueError("price_per_second must be non-empty and positive")
            durations = frozenset(int(d) for d in raw["allowed_durations_s"])
            if not durations:
                raise ValueError("allowed_durations_s must be non-empty")
            merged[str(key)] = FalModel(
                model_id=str(raw["model_id"]),
                provider=str(raw.get("provider", "fal")),
                price_per_second=prices,
                default_resolution=str(raw["default_resolution"]),
                allowed_durations_s=durations,
                extra_input=dict(raw.get("extra_input") or {}),
                initial_latency_s=float(raw.get("initial_latency_s", 90.0)),
                supports_image_seed=bool(raw.get("supports_image_seed", False)),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            log.warning("dropping malformed catalogue entry %r: %s", key, exc)
    return merged


def catalogue_to_json(catalogue: dict) -> dict:
    """Serialise for the API. Prices become strings so no Decimal is lost to
    a float round-trip in JSON."""
    return {
        key: {
            "provider": m.provider,
            "model_id": m.model_id,
            "price_per_second": {r: str(p) for r, p in m.price_per_second.items()},
            "default_resolution": m.default_resolution,
            "allowed_durations_s": sorted(m.allowed_durations_s),
            "extra_input": dict(m.extra_input),
            "initial_latency_s": m.initial_latency_s,
            "supports_image_seed": m.supports_image_seed,
            "builtin": key in _builtin_keys(),
        }
        for key, m in catalogue.items()
    }


def _builtin_keys() -> frozenset[str]:
    from egregore.forge.fal import FAL_MODELS

    return frozenset(FAL_MODELS)


def save_catalogue(catalogue: dict) -> None:
    """Persist only what differs from the built-ins, so upstream price
    corrections still reach a user who never edited that model."""
    from egregore.forge.fal import FAL_MODELS

    out = {}
    for key, m in catalogue.items():
        if key in FAL_MODELS and FAL_MODELS[key] == m:
            continue
        out[key] = {
            "provider": m.provider,
            "model_id": m.model_id,
            "price_per_second": {r: str(p) for r, p in m.price_per_second.items()},
            "default_resolution": m.default_resolution,
            "allowed_durations_s": sorted(m.allowed_durations_s),
            "extra_input": dict(m.extra_input),
            "initial_latency_s": m.initial_latency_s,
            "supports_image_seed": m.supports_image_seed,
        }
    _write_yaml(models_path(), out)
```

Add to the imports at the top of `store.py`:

```python
import logging
from decimal import Decimal

log = logging.getLogger(__name__)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_config_store.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 6: Commit**

```bash
git add egregore/config/store.py egregore/forge/fal.py tests/test_config_store.py
git commit -m "Make the model catalogue user-extensible and provider-aware"
```

---

### Task 3: Settings, secrets, and models endpoints

**Files:**
- Modify: `egregore/conductor/state.py` (add `settings_handler`)
- Modify: `egregore/conductor/app.py` (four routes)
- Modify: `tests/test_conductor.py`

**Interfaces:**
- Consumes: `store.load_settings/save_settings/secrets_present/load_catalogue/save_catalogue/catalogue_to_json`, `store.LIVE_KEYS`, `store.RESTART_KEYS`.
- Produces: `GET /api/settings`, `POST /api/settings`, `GET /api/secrets`, `GET /api/models`, `POST /api/models`, `DELETE /api/models/{key}`; `ConductorState.settings_handler`.

- [ ] **Step 1: Write the failing tests**

```python
def test_secrets_endpoint_reports_presence_and_never_a_value(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "leak-me-if-you-can")
    app = create_app(ConductorState(), lens_dir=Path("lens"), password=None)
    with TestClient(app) as client:
        body = client.get("/api/secrets").json()
    assert body["FAL_KEY"] is True
    assert "leak-me-if-you-can" not in json.dumps(body)


def test_settings_endpoints_require_the_password_even_when_party_auth_is_off():
    # Watching the screens and reconfiguring the system are different trust
    # levels; party auth being disabled must not open the settings surface.
    app = create_app(ConductorState(), lens_dir=Path("lens"), password=None)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200  # party auth off
        assert client.get("/api/settings").status_code == 403
        assert client.get("/api/secrets").status_code == 403
        assert client.post("/api/settings", json={}).status_code == 403


def test_settings_endpoints_open_with_the_password(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    app = create_app(ConductorState(), lens_dir=Path("lens"), password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.get("/api/settings").status_code == 200


def test_settings_post_separates_live_from_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    applied: list[dict] = []
    state = ConductorState()
    state.settings_handler = lambda overrides: applied.append(overrides) or {"ok": True}
    app = create_app(state, lens_dir=Path("lens"), password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        body = client.post(
            "/api/settings",
            json={"generation": {"clip_duration_s": 6, "backend": "fal"}},
        ).json()
    assert "generation.clip_duration_s" in body["applied_live"]
    assert "generation.backend" in body["restart_required"]


def test_settings_post_rejects_an_invalid_value_without_persisting(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    app = create_app(ConductorState(), lens_dir=Path("lens"), password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/settings", json={"generation": {"clip_duration_s": 999}})
        assert r.status_code == 400
        assert client.get("/api/settings").json()["overrides"] == {}


def test_models_crud(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    app = create_app(ConductorState(), lens_dir=Path("lens"), password="pw")
    entry = {
        "provider": "fal",
        "model_id": "vendor/thing/text-to-video",
        "price_per_second": {"720P": "0.07"},
        "default_resolution": "720P",
        "allowed_durations_s": [5, 10],
    }
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post("/api/models", json={"key": "thing", **entry}).status_code == 200
        assert "thing" in client.get("/api/models").json()
        assert client.delete("/api/models/thing").status_code == 200
        assert "thing" not in client.get("/api/models").json()


def test_models_post_rejects_a_nonpositive_price(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    app = create_app(ConductorState(), lens_dir=Path("lens"), password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/models", json={
            "key": "free-lunch", "provider": "fal", "model_id": "x/y",
            "price_per_second": {"720P": "0"}, "default_resolution": "720P",
            "allowed_durations_s": [5],
        })
        # A zero price reserves nothing against the ceiling (PRD B-2).
        assert r.status_code == 400


def test_builtin_model_cannot_be_deleted(monkeypatch, tmp_path):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    app = create_app(ConductorState(), lens_dir=Path("lens"), password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.delete("/api/models/minimax-h3-max").status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_conductor.py -k "settings or secrets or models"`
Expected: FAIL — 404 on the new routes

- [ ] **Step 3: Add `settings_handler` to `ConductorState`**

In `egregore/conductor/state.py`, in `__init__`, beside `control_handler`:

```python
        #: Bound by the integration layer. Receives the live subset of a
        #: settings change and applies it to the running party.
        self.settings_handler: Callable[[dict], dict] | None = None
```

- [ ] **Step 4: Add the routes to `egregore/conductor/app.py`**

Add near the top of the module:

```python
from egregore.config import store as config_store
```

Then, inside `create_app` immediately before `return app`:

```python
    # -- configuration ------------------------------------------------------
    #
    # Reconfiguring the system is a higher trust level than watching it, so
    # these always demand the password even when party auth is disabled --
    # and where no password exists at all, they answer only on loopback.

    def require_operator(request: Request) -> None:
        if password:
            require_party(request)
            return
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "settings need a party password, or a request from this machine",
            )

    def _split_keys(overrides: dict) -> tuple[list[str], list[str]]:
        live, restart = [], []
        for dotted in _dotted_keys(overrides):
            if dotted in config_store.LIVE_KEYS:
                live.append(dotted)
            elif dotted in config_store.RESTART_KEYS:
                restart.append(dotted)
        return sorted(live), sorted(restart)

    @app.get("/api/settings", dependencies=[Depends(require_operator)])
    async def get_settings() -> dict:
        overrides = await asyncio.to_thread(config_store.load_settings)
        return {
            "overrides": overrides,
            "live_keys": sorted(config_store.LIVE_KEYS),
            "restart_keys": sorted(config_store.RESTART_KEYS),
            "effective": state.effective_config or {},
        }

    @app.post("/api/settings", dependencies=[Depends(require_operator)])
    async def post_settings(overrides: dict) -> dict:
        try:
            config_store.validate_overrides(overrides)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        live, restart = _split_keys(overrides)
        current = await asyncio.to_thread(config_store.load_settings)
        merged = config_store.deep_merge(current, overrides)
        await asyncio.to_thread(config_store.save_settings, merged)

        if live and state.settings_handler is not None:
            state.settings_handler(overrides)
        return {"applied_live": live, "restart_required": restart, "overrides": merged}

    @app.get("/api/secrets", dependencies=[Depends(require_operator)])
    async def get_secrets() -> dict[str, bool]:
        # Booleans only. No branch of this handler can reach a value.
        return await asyncio.to_thread(config_store.secrets_present)

    @app.get("/api/models", dependencies=[Depends(require_operator)])
    async def get_models() -> dict:
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        return config_store.catalogue_to_json(catalogue)

    @app.post("/api/models", dependencies=[Depends(require_operator)])
    async def post_model(entry: dict) -> dict:
        key = str(entry.pop("key", "")).strip()
        if not key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "model needs a key")
        try:
            model = config_store.model_from_json(entry)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        catalogue[key] = model
        await asyncio.to_thread(config_store.save_catalogue, catalogue)
        return config_store.catalogue_to_json({key: model})

    @app.delete("/api/models/{key}", dependencies=[Depends(require_operator)])
    async def delete_model(key: str) -> dict:
        if key in config_store.builtin_keys():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{key!r} ships with Egregore and cannot be deleted; override it instead",
            )
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        catalogue.pop(key, None)
        await asyncio.to_thread(config_store.save_catalogue, catalogue)
        return {"deleted": key}
```

- [ ] **Step 5: Add the helpers the routes need to `store.py`**

```python
def deep_merge(base: dict, overlay: dict) -> dict:
    """Public alias — the API merges saved overrides with incoming ones."""
    return _deep_merge(base, overlay)


def builtin_keys() -> frozenset[str]:
    return _builtin_keys()


def dotted_keys(data: dict, prefix: str = "") -> list[str]:
    """Flatten ``{"a": {"b": 1}}`` to ``["a.b"]`` for live/restart routing."""
    out: list[str] = []
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(dotted_keys(value, f"{path}."))
        else:
            out.append(path)
    return out


def validate_overrides(overrides: dict) -> EgregoreConfig:
    """Reject an override set before anything is persisted.

    Validates against a default config rather than the running one: a value
    that only survives because of an unrelated preset field is not a setting
    we want to write to disk.
    """
    return apply_overlay(EgregoreConfig(), overrides)


def model_from_json(entry: dict) -> Any:
    """Build one catalogue row from an API payload, refusing prices that
    would under-reserve against the hard ceiling."""
    from egregore.forge.fal import FalModel

    prices = {
        str(res): Decimal(str(price))
        for res, price in (entry.get("price_per_second") or {}).items()
    }
    if not prices:
        raise ValueError("price_per_second must name at least one resolution")
    if any(p <= 0 for p in prices.values()):
        raise ValueError("every price must be greater than zero")
    durations = frozenset(int(d) for d in entry.get("allowed_durations_s") or ())
    if not durations:
        raise ValueError("allowed_durations_s must list at least one duration")
    return FalModel(
        model_id=str(entry["model_id"]),
        provider=str(entry.get("provider", "fal")),
        price_per_second=prices,
        default_resolution=str(entry["default_resolution"]),
        allowed_durations_s=durations,
        extra_input=dict(entry.get("extra_input") or {}),
        initial_latency_s=float(entry.get("initial_latency_s", 90.0)),
        supports_image_seed=bool(entry.get("supports_image_seed", False)),
    )
```

Also add `effective_config: dict | None = None` to `ConductorState.__init__` so `GET /api/settings` can report what the party is actually running, and import `Request` in `app.py` if it is not already imported.

Replace the `_dotted_keys(overrides)` call in `_split_keys` with `config_store.dotted_keys(overrides)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_conductor.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 7: Commit**

```bash
git add egregore/conductor/ egregore/config/store.py tests/test_conductor.py
git commit -m "Serve settings, secret presence, and catalogue CRUD behind an operator gate"
```

---

### Task 4: Apply the live subset to a running party

**Files:**
- Modify: `egregore/app.py`
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: `store.load_settings`, `store.load_env_file`, `store.apply_overlay`, `ConductorState.settings_handler`.
- Produces: `LiveSettings` dataclass in `egregore/app.py` with `clip_duration_s: int`, `resolution: str`, `drift: float`, `cadence_floor_s: float | None`; `run_party` binds `state.settings_handler`.

- [ ] **Step 1: Write the failing test**

```python
async def test_live_settings_change_the_next_clip_without_a_restart(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        await party.wait_clips(1)
        handler = party.state.settings_handler
        assert handler is not None, "the integration layer must bind a settings handler"
        handler({"generation": {"clip_duration_s": 6}})
        assert party.live.clip_duration_s == 6


async def test_live_settings_reject_a_restart_only_key(tmp_path):
    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "fixture"}}])
    async with Party(cfg) as party:
        party.state.settings_handler({"budget": {"total_usd": 999}})
        # The ceiling is held against reservations already made; moving it
        # under them is exactly what the restart group exists to prevent.
        assert party.governor.ledger.ceiling != 999
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_integration.py -k live_settings`
Expected: FAIL — `settings_handler` is None

- [ ] **Step 3: Add `LiveSettings` and bind the handler in `egregore/app.py`**

```python
@dataclass
class LiveSettings:
    """The subset of configuration a running party re-reads each cycle.

    Everything here is read per generation, so changing it is an assignment
    rather than a reconstruction. Anything that would rebuild the backend
    ladder, or move a ceiling that reservations are already held against,
    belongs in ``store.RESTART_KEYS`` instead.
    """

    clip_duration_s: int
    resolution: str
    drift: float
    cadence_floor_s: float | None = None

    @classmethod
    def from_config(cls, cfg: EgregoreConfig) -> LiveSettings:
        return cls(
            clip_duration_s=cfg.generation.clip_duration_s,
            resolution=cfg.generation.resolution,
            drift=cfg.aesthetic.drift,
        )

    def apply(self, overrides: dict) -> list[str]:
        """Apply only the live keys present in ``overrides``; return what changed."""
        changed: list[str] = []
        gen = overrides.get("generation") or {}
        if "clip_duration_s" in gen:
            self.clip_duration_s = int(gen["clip_duration_s"])
            changed.append("generation.clip_duration_s")
        if "resolution" in gen:
            self.resolution = str(gen["resolution"])
            changed.append("generation.resolution")
        aes = overrides.get("aesthetic") or {}
        if "drift" in aes:
            self.drift = float(aes["drift"])
            changed.append("aesthetic.drift")
        if "cadence_floor_s" in overrides:
            raw = overrides["cadence_floor_s"]
            self.cadence_floor_s = float(raw) if raw else None
            changed.append("cadence_floor_s")
        return changed
```

In `run_party`, after the config is loaded and before the pipelines are built:

```python
    config_store.load_env_file()          # keys from ~/.egregore/env
    overrides = config_store.load_settings()
    if overrides:
        try:
            cfg = config_store.apply_overlay(cfg, overrides)
            log.info("settings overlay applied from %s", config_store.settings_path())
        except ValueError as exc:
            log.warning("ignoring invalid settings overlay (%s); using the preset", exc)
    live = LiveSettings.from_config(cfg)
```

After `state` is constructed:

```python
    def _apply_settings(payload: dict) -> dict:
        changed = live.apply(payload)
        log.info("live settings changed: %s", ", ".join(changed) or "nothing")
        return {"applied": changed}

    state.settings_handler = _apply_settings
    state.effective_config = cfg.model_dump(mode="json")
```

Pass `live=live` into each `ZonePipeline`, store it as `self.live`, and in `_generation_loop` read `self.live.clip_duration_s` and `self.live.drift` in place of `cfg.generation.clip_duration_s` and `cfg.aesthetic.drift` (three call sites: the bleed branch's `forge.request`, the `weaver.weave` call, and the main `forge.request`).

In `_throughput_floor`'s `probe`, consult the override first:

```python
    def probe() -> float:
        if live.cadence_floor_s:
            return live.cadence_floor_s
        try:
            return preferred.estimated_latency(tier).total_seconds()
        except Exception:
            return 0.0
```

`_throughput_floor` therefore takes `live` as a third argument.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_integration.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add egregore/app.py tests/test_integration.py
git commit -m "Apply the live settings subset to a running party"
```

---

### Task 5: `egregore setup` wizard

**Files:**
- Modify: `egregore/cli.py`
- Create: `tests/test_cli_setup.py`

**Interfaces:**
- Consumes: `store.write_secret`, `store.secrets_present`, `store.egregore_home`.
- Produces: `egregore setup` subcommand; `egregore.cli.probe_environment() -> dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Setup wizard tests — the wizard must never print a secret back."""

from __future__ import annotations

import pytest

from egregore import cli
from egregore.config import store


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    return tmp_path


def test_probe_reports_each_dependency():
    probe = cli.probe_environment()
    assert set(probe) >= {"ffmpeg", "comfyui", "parakeet", "audio_input"}
    assert all(isinstance(v, str) for v in probe.values())


def test_setup_writes_the_key_at_0600_and_never_echoes_it(monkeypatch, capsys, home):
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "s3cret-value")
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": "")
    assert cli.main(["setup", "--non-interactive-secret", "FAL_KEY"]) == 0
    env = home / "env"
    assert env.exists()
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert "s3cret-value" in env.read_text()
    assert "s3cret-value" not in capsys.readouterr().out


def test_setup_is_idempotent_and_replaces_rather_than_appends(monkeypatch, home):
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": "")
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "first")
    cli.main(["setup", "--non-interactive-secret", "FAL_KEY"])
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "second")
    cli.main(["setup", "--non-interactive-secret", "FAL_KEY"])
    body = (home / "env").read_text()
    assert body.count("FAL_KEY") == 1
    assert "second" in body and "first" not in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_cli_setup.py`
Expected: FAIL — `AttributeError: module 'egregore.cli' has no attribute 'probe_environment'`

- [ ] **Step 3: Implement the wizard in `egregore/cli.py`**

```python
def probe_environment() -> dict[str, str]:
    """What this machine can already do. Strings, so they print as-is."""
    import shutil
    import urllib.error
    import urllib.request
    from pathlib import Path as _Path

    from egregore.config import store as _store

    out: dict[str, str] = {}
    out["ffmpeg"] = "found" if shutil.which("ffmpeg") else "MISSING — brew install ffmpeg"

    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2).read(1)
        out["comfyui"] = "running on :8188"
    except (urllib.error.URLError, OSError, ValueError):
        out["comfyui"] = "not running (local diffusion unavailable)"

    onnx = os.environ.get("EGREGORE_PARAKEET_ONNX_DIR") or str(
        _Path.home() / ".egregore" / "models" / "parakeet-v2-int8"
    )
    out["parakeet"] = f"found at {onnx}" if _Path(onnx).is_dir() else "not installed"

    try:
        import sounddevice as sd  # type: ignore[import-not-found]

        names = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
        out["audio_input"] = ", ".join(names) if names else "no input devices"
    except Exception:
        out["audio_input"] = "sounddevice not installed (live mic unavailable)"

    for name, present in _store.secrets_present().items():
        out[name] = "set" if present else "not set"
    return out


def _prompt(text: str, default: str = "") -> str:
    raw = input(f"{text} " if not default else f"{text} [{default}] ").strip()
    return raw or default


def _prompt_secret(name: str) -> str:
    import getpass

    return getpass.getpass(f"  paste {name} (input hidden, blank to skip): ").strip()


def _cmd_setup(args: argparse.Namespace) -> int:
    from egregore.config import store as _store

    print("\n  egregore setup\n  " + "-" * 40)
    for key, value in probe_environment().items():
        print(f"  {key:14s} {value}")

    wanted = [args.non_interactive_secret] if args.non_interactive_secret else None
    if wanted is None:
        print("\n  Keys are stored at", _store.env_path(), "(mode 600) and never")
        print("  sent to the browser. Leave blank to skip one.\n")
        wanted = ["FAL_KEY", "GEMINI_API_KEY"]

    for name in wanted:
        value = _prompt_secret(name)
        if value:
            _store.write_secret(name, value)
            print(f"  {name} saved")   # deliberately does not echo the value

    print("\n  Presets:")
    for path in sorted(Path("presets").glob("*.yaml")):
        print(f"    {path}")
    chosen = _prompt("\n  Which preset should `egregore run` use?", "presets/demo.yaml")
    print(f"\n  Ready:  uv run egregore run {chosen}")
    print("  Then open http://localhost:8420/?zone=main")
    print("  Settings UI: http://localhost:8420/static/setup.html\n")
    return 0
```

Register the subcommand in `main`, beside `run`/`check`/`wipe`:

```python
    p_setup = sub.add_parser("setup", help="first-run wizard: probe, keys, preset")
    p_setup.add_argument("--non-interactive-secret", default=None,
                         help="prompt for only this secret (used by tests)")
```

`setup` takes no config file, so dispatch it **before** `load_config` is called:

```python
    if args.command == "setup":
        return _cmd_setup(args)
```

Add `import os` to `egregore/cli.py` if absent.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_cli_setup.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add egregore/cli.py tests/test_cli_setup.py
git commit -m "Add an egregore setup wizard that probes the box and stores keys at 0600"
```

---

### Task 6: `lens/setup.html`

**Files:**
- Create: `lens/setup.html`
- Modify: `lens/status.html` (header link)

**Interfaces:**
- Consumes: `GET/POST /api/settings`, `GET /api/secrets`, `GET/POST/DELETE /api/models`, `POST /api/join`.
- Produces: a page at `/static/setup.html`.

- [ ] **Step 1: Build the page**

Copy the `:root` custom properties, the `body`/panel/`.veil` styling, and the join-form markup and handler from `lens/status.html` so the two pages read as one product. Then add, in a single inline `<script>` with no imports:

- `loadAll()` — `Promise.all` over `/api/settings`, `/api/secrets`, `/api/models`; a 403 shows the join veil.
- A **Secrets** panel rendering one row per key: `FAL_KEY  detected ✓` in the accent colour, or `not set` dimmed with the hint `run: egregore setup`. No input field appears anywhere on this panel — the page has no way to send a secret.
- A **Settings** panel with `backend` (select, from `/api/settings` `restart_keys`), `fal_model` (select, populated from `/api/models`), `resolution`, `clip_duration_s`, `budget.total_usd`, `cadence_floor_s`. Each control is tagged from `live_keys`/`restart_keys` in the response, so the split is server-driven and the two can never disagree. Changing a live control POSTs immediately and flashes `applied`; changing a restart control POSTs and adds to a pending list.
- A sticky banner, hidden when empty: `N change(s) saved — restart egregore to apply` listing the pending dotted keys.
- A **Models** panel listing the catalogue with `provider`, `model_id`, prices, durations, and a `builtin` badge. Built-ins render their delete button disabled with the title `ships with Egregore — override instead`.
- An add/edit form: key, provider, model_id, one price row per resolution (add/remove rows), durations (comma-separated), extra_input (JSON textarea). Below it, a live readout recomputed on every keystroke:

```js
function reserveLine() {
  var worst = Math.max.apply(null, priceRows().map(function (r) { return r.price; }).concat([0]));
  var dur = Math.max.apply(null, durations().concat([0]));
  var reserve = worst * dur * 2;                       // SAFETY_FACTOR
  el.reserve.textContent =
    'reserves $' + reserve.toFixed(2) + ' per ' + dur + 's clip' +
    ' \u00b7 budget $' + budget.toFixed(2) + ' allows ' +
    (reserve > 0 ? Math.floor(budget / reserve) : 0) + ' in flight';
  el.reserve.classList.toggle('warn', reserve <= 0 || reserve > budget);
}
```

That line is the mitigation for letting prices be typed: a misplaced decimal shows up immediately as an implausible reservation or an in-flight count of zero, instead of silently weakening the ceiling.

- [ ] **Step 2: Link it from the dashboard**

In `lens/status.html`, in the header `<div id="link">` row, add:

```html
<a href="/static/setup.html" class="navlink">settings</a>
```

styled with `color: var(--dim)` and `:hover { color: var(--accent) }`.

- [ ] **Step 3: Verify against a running party**

```bash
uv run egregore run presets/demo.yaml &
open http://localhost:8420/static/setup.html
```

Check by hand: secrets show presence only; changing clip duration flashes `applied` and appears in `GET /api/settings`; changing backend adds a pending-restart entry; adding a model with price `0.05` shows a plausible reserve line and appears in the model dropdown; deleting a built-in is refused.

- [ ] **Step 4: Confirm no secret can reach the page**

```bash
FAL_KEY=leak-canary uv run egregore run presets/demo.yaml &
sleep 20
curl -s localhost:8420/api/secrets | grep -c leak-canary   # must print 0
curl -s localhost:8420/api/settings | grep -c leak-canary  # must print 0
```

- [ ] **Step 5: Commit**

```bash
git add lens/setup.html lens/status.html
git commit -m "Add a settings page that configures everything except secrets"
```

---

### Task 7: README

**Files:**
- Modify: `README.md`
- Modify: `docs/fal-setup.md`, `docs/local-hardware.md` (cross-links only)

- [ ] **Step 1: Rewrite `README.md`**

Sections, in order:

1. **What it is** — the two-sentence pitch from the current README, kept.
2. **Quickstart (60 seconds)** — `uv sync --extra dev`, `uv run egregore setup`, `uv run egregore run presets/demo.yaml`, open the URL. State plainly that demo mode needs no mic, no GPU, and no API key, and that it drives the real pipeline rather than a mock.
3. **Three ways to run it** — a table: *procedural* (ffmpeg, instant, free), *local diffusion* (ComfyUI + LTX, minutes per clip, free), *cloud* (fal or Veo, seconds, metered), with the preset that demonstrates each.
4. **Configuring it** — the settings page first (`/static/setup.html`), then the file locations, then the precedence rule `preset -> ~/.egregore/settings.yaml -> env`. Include the live-versus-restart table.
5. **Adding a model** — the dashboard form, then the equivalent `~/.egregore/models.yaml`, and the warning that the price is what the hard ceiling is computed from.
6. **Privacy** — transcripts never leave the ring buffer; only a validated, abstracted prompt crosses the network, and only in cloud mode; `tests/test_privacy.py` is the test that must never fail. Show the real worked example: *"grandmother… shells… ocean"* becomes *"vast blue depth; slow tidal pull"* with zero shared 3-grams.
7. **Costs** — MiniMax H3 Max at $0.025/s promo versus Veo 3.1 at $0.40/s, a worked 3-hour party for each, and the note that the ceiling is held against standard rates rather than promotional ones.
8. **Troubleshooting** — mic permission surfacing as `PortAudio -9986`; the LTXV VAE needing ComfyUI's `res_blocks` key layout rather than the published diffusers export; LM Studio not being able to run video models; ComfyUI needing the GGUF and VideoHelperSuite custom nodes.
9. **Development** — `uv run pytest -q`, `uv run ruff check .`, and a pointer to `CONTRACTS.md`.
10. **Links** to `docs/local-hardware.md`, `docs/fal-setup.md`, `docs/veo-setup.md`, and the PRD/architecture documents.

- [ ] **Step 2: Verify every command in the README actually runs**

```bash
uv sync --extra dev
uv run egregore check presets/demo.yaml
uv run ruff check .
uv run pytest -q
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/
git commit -m "Rewrite the README as the front door"
```

---

## Self-Review

**Spec coverage.** Store module → Task 1. Provider-aware catalogue and user file → Task 2. Four endpoints and the operator gate → Task 3. Live-versus-restart application → Task 4. `egregore setup` → Task 5. `setup.html` and the price mitigation → Task 6. README → Task 7. The spec's testing section is distributed across Tasks 1–3 and the manual leak check in Task 6, Step 4.

**Placeholders.** None: every code step carries real code, and the two prose-heavy steps (Task 6's page, Task 7's README) enumerate exact sections, exact endpoints, and exact copy.

**Type consistency.** `store.load_settings/save_settings/apply_overlay/secrets_present/load_catalogue/save_catalogue/catalogue_to_json/deep_merge/dotted_keys/validate_overrides/model_from_json/builtin_keys/write_secret/load_env_file/env_path/settings_path/models_path` are defined in Tasks 1–3 and used under those exact names afterwards. `FalModel.provider` is added in Task 2 before Task 3 serialises it. `ConductorState.settings_handler` and `.effective_config` are added in Task 3 and bound in Task 4. `LiveSettings.apply` returns `list[str]`, which is what Task 4's handler logs.

**One correction applied inline:** Task 3's first draft called a module-level `_dotted_keys`; the helper is `store.dotted_keys`, and Step 5 now says to use it.
