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
caller that could serialise it. :func:`secrets_present` reports booleans, and
:func:`write_secret` is import-only for the CLI wizard.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .schema import EgregoreConfig

log = logging.getLogger(__name__)

#: Environment variables that hold credentials. Presence is reportable; the
#: values are not.
SECRET_NAMES: tuple[str, ...] = ("FAL_KEY", "GEMINI_API_KEY", "EGREGORE_PARTY_PASSWORD")

#: Settings the running party re-reads each generation cycle, so changing them
#: is an assignment rather than a reconstruction.
LIVE_KEYS: frozenset[str] = frozenset({
    "generation.clip_duration_s",
    "generation.resolution",
    "generation.local_steps",
    "generation.fill_duration_s",
    "generation.local_resolution",
    "continuity.default_mode",
    "aesthetic.drift",
    "aesthetic.grammar",
    "aesthetic.abstraction",
    "cadence_floor_s",
    "weaver.selection.salience",
    "weaver.selection.novelty",
    "weaver.selection.recency",
    "weaver.selection.segment_gap_s",
    "weaver.selection.max_candidates",
    "weaver.selection.recency_tau_s",
    "weaver.selection.lookback_s",
    "aesthetic.room_bias",
    "weaver.fallback_after_s",
})

#: Settings read once at start-up. Changing these builds a different backend
#: ladder, or moves a ceiling that reservations are already held against, so
#: they are persisted and applied on the next run.
RESTART_KEYS: frozenset[str] = frozenset({
    "generation.backend",
    "generation.fal_model",
    "generation.fallback",
    "generation.comfyui_url",
    "budget.total_usd",
    "continuity.topology",
    "asr.engine",
    "serving.bind",
    "weaver.llm.model",
    "weaver.llm.base_url",
    "weaver.engine",
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
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read %s (%s); treating as empty", path, type(exc).__name__)
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
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def deep_merge(base: dict, overlay: dict) -> dict:
    """Public alias — the API merges saved overrides with incoming ones."""
    return _deep_merge(base, overlay)


def apply_overlay(cfg: EgregoreConfig, overrides: dict) -> EgregoreConfig:
    """Return a new validated config with ``overrides`` merged in.

    Validation happens on the merged whole, so an override that is individually
    plausible but invalid in context is rejected before it can reach a party.
    The input config is never mutated.
    """
    merged = _deep_merge(cfg.model_dump(mode="json"), overrides)
    return EgregoreConfig.model_validate(merged)


def validate_overrides(overrides: dict) -> EgregoreConfig:
    """Reject an override set before anything is persisted.

    Validates against a default config rather than the running one: a value
    that only survives because of an unrelated preset field is not a setting
    we want to write to disk.
    """
    return apply_overlay(EgregoreConfig(), overrides)


def expand_dotted(data: dict) -> dict:
    """Turn any ``{"a.b": v}`` keys into ``{"a": {"b": v}}``, recursively.

    The dashboard nests before posting, but the dotted form is the obvious
    thing to send by hand — and it used to be accepted and then quietly
    dropped. It passed validation, because an unknown top-level key is
    ignored; it matched ``LIVE_KEYS`` verbatim, so the response reported it as
    applied; and it was written to ``settings.yaml`` as a literal key nothing
    reads. The setting looked saved and did nothing, permanently.

    Raises ``ValueError`` when a dotted key would have to write inside a value
    that is not a mapping: discarding one of the two silently is worse than
    refusing.
    """
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            value = expand_dotted(value)
        node = out
        parts = str(key).split(".")
        for part in parts[:-1]:
            existing = node.get(part)
            if existing is None:
                existing = node[part] = {}
            elif not isinstance(existing, dict):
                raise ValueError(f"{key!r} conflicts with a non-mapping value at {part!r}")
            node = existing
        leaf = parts[-1]
        if isinstance(node.get(leaf), dict) and isinstance(value, dict):
            node[leaf] = _deep_merge(node[leaf], value)
        elif leaf in node and not isinstance(node.get(leaf), dict) and isinstance(value, dict):
            raise ValueError(f"{key!r} conflicts with a non-mapping value at {leaf!r}")
        else:
            node[leaf] = value
    return out


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


def value_at(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# secrets — presence in, presence out
# ---------------------------------------------------------------------------


def _read_env_lines() -> list[str]:
    path = env_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


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

    Called by the setup wizard only; nothing in the web layer imports this.
    """
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}; expected one of {SECRET_NAMES}")
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [
        ln for ln in _read_env_lines()
        if not ln.removeprefix("export ").startswith(f"{name}=")
    ]
    kept.append(f'export {name}="{value}"')
    path.write_text("\n".join(kept) + "\n")
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# model catalogue
# ---------------------------------------------------------------------------


def _builtin_keys() -> frozenset[str]:
    from egregore.forge.fal import FAL_MODELS

    return frozenset(FAL_MODELS)


def builtin_keys() -> frozenset[str]:
    return _builtin_keys()


def model_from_json(entry: dict) -> Any:
    """Build one catalogue row from a payload, refusing prices that would
    under-reserve against the hard ceiling (PRD B-2)."""
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


def load_catalogue() -> dict:
    """Built-in models, then the user's file overriding and extending them.

    A malformed entry is dropped rather than defaulted. A model with no usable
    price would otherwise reserve nothing against the hard ceiling, which is
    exactly the failure PRD B-2 forbids.
    """
    from egregore.forge.fal import FAL_MODELS

    merged = dict(FAL_MODELS)
    for key, raw in _read_yaml(models_path()).items():
        try:
            merged[str(key)] = model_from_json(dict(raw))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            log.warning("dropping malformed catalogue entry %r: %s", key, exc)
    return merged


def catalogue_to_json(catalogue: dict) -> dict:
    """Serialise for the API. Prices become strings so no Decimal is lost to a
    float round-trip in JSON."""
    builtins = _builtin_keys()
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
            "builtin": key in builtins,
        }
        for key, m in catalogue.items()
    }


def save_catalogue(catalogue: dict) -> None:
    """Persist only what differs from the built-ins, so an upstream price
    correction still reaches a user who never edited that model."""
    from egregore.forge.fal import FAL_MODELS

    out: dict[str, dict] = {}
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
