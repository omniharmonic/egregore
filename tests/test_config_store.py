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
    leftovers = [p.name for p in home.iterdir() if p.name != "settings.yaml"]
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


def test_env_file_is_read_for_presence_but_never_returned(home, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    (home / "env").write_text('export FAL_KEY="from-file-value"\n')
    assert store.secrets_present()["FAL_KEY"] is True
    assert "from-file-value" not in repr(store.secrets_present())


def test_load_env_file_does_not_override_an_explicit_export(home, monkeypatch):
    # A key exported in the shell is the operator being deliberate; the file
    # is the fallback, not the authority.
    monkeypatch.setenv("FAL_KEY", "from-shell")
    (home / "env").write_text('export FAL_KEY="from-file"\n')
    store.load_env_file()
    import os

    assert os.environ["FAL_KEY"] == "from-shell"


def test_write_secret_is_0600_and_replaces_rather_than_appends(home):
    store.write_secret("FAL_KEY", "first")
    store.write_secret("FAL_KEY", "second")
    body = (home / "env").read_text()
    assert body.count("FAL_KEY") == 1
    assert "second" in body and "first" not in body
    assert oct((home / "env").stat().st_mode)[-3:] == "600"


def test_write_secret_refuses_an_unknown_name():
    with pytest.raises(ValueError, match="unknown secret"):
        store.write_secret("AWS_SECRET_ACCESS_KEY", "nope")


# ---------------------------------------------------------------------------
# Catalogue — built-ins plus whatever the operator adds
# ---------------------------------------------------------------------------


def test_builtin_catalogue_loads_when_no_user_file():
    from egregore.forge.fal import FAL_MODELS

    loaded = store.load_catalogue()
    assert set(loaded) >= set(FAL_MODELS)
    assert loaded["minimax-h3-max"].model_id == "minimax/h3-max/text-to-video"


def test_user_file_extends_and_overrides_builtins(home):
    from decimal import Decimal

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
    from decimal import Decimal

    (home / "models.yaml").write_text(
        "m:\n  provider: fal\n  model_id: x/y\n  price_per_second: {480P: 0.05}\n"
        "  default_resolution: 480P\n  allowed_durations_s: [5]\n"
    )
    price = store.load_catalogue()["m"].price_per_second["480P"]
    assert isinstance(price, Decimal), "money is Decimal (CONTRACTS.md rule 4)"


def test_malformed_entry_is_dropped_not_priced_at_zero(home):
    (home / "models.yaml").write_text(
        "broken:\n  provider: fal\n  model_id: x/y\n  price_per_second: {}\n"
        "  default_resolution: 480P\n  allowed_durations_s: [5]\n"
    )
    assert "broken" not in store.load_catalogue(), (
        "a model with no price would reserve nothing against the ceiling"
    )


def test_zero_priced_entry_is_dropped(home):
    (home / "models.yaml").write_text(
        "free:\n  provider: fal\n  model_id: x/y\n  price_per_second: {480P: '0'}\n"
        "  default_resolution: 480P\n  allowed_durations_s: [5]\n"
    )
    assert "free" not in store.load_catalogue()


def test_catalogue_round_trips_through_save(home):
    original = store.load_catalogue()
    store.save_catalogue(original)
    assert set(store.load_catalogue()) == set(original)


def test_save_catalogue_omits_unmodified_builtins(home):
    # Persisting an untouched built-in would freeze today's price into the
    # user's file and silently shadow an upstream correction.
    store.save_catalogue(store.load_catalogue())
    assert store._read_yaml(store.models_path()) == {}


def test_catalogue_to_json_is_serialisable_with_string_prices():
    import json

    payload = store.catalogue_to_json(store.load_catalogue())
    json.dumps(payload)
    assert payload["minimax-h3-max"]["price_per_second"]["480P"] == "0.05"
    assert payload["minimax-h3-max"]["builtin"] is True


def test_model_from_json_refuses_prices_that_weaken_the_ceiling():
    base = {
        "model_id": "x/y",
        "default_resolution": "720P",
        "allowed_durations_s": [5],
    }
    with pytest.raises(ValueError, match="greater than zero"):
        store.model_from_json({**base, "price_per_second": {"720P": "0"}})
    with pytest.raises(ValueError, match="greater than zero"):
        store.model_from_json({**base, "price_per_second": {"720P": "-1"}})
    with pytest.raises(ValueError, match="at least one resolution"):
        store.model_from_json({**base, "price_per_second": {}})
    with pytest.raises(ValueError, match="at least one duration"):
        store.model_from_json(
            {**base, "price_per_second": {"720P": "0.07"}, "allowed_durations_s": []}
        )


# ---------------------------------------------------------------------------
# Dotted keys arriving from the wire
#
# The dashboard nests before posting, but the dotted form is the obvious thing
# to send by hand and it used to be accepted and then silently dropped: it
# passed validation (pydantic ignores unknown top-level keys), it matched
# LIVE_KEYS verbatim so the response said "applied", and it was written to
# settings.yaml as a literal key that nothing ever reads. The setting looked
# saved and did nothing, for good.
# ---------------------------------------------------------------------------


def test_dotted_keys_are_expanded_into_the_shape_the_config_actually_uses():
    assert store.expand_dotted({"generation.local_steps": 8}) == {
        "generation": {"local_steps": 8}
    }


def test_expansion_merges_with_a_nested_sibling_rather_than_replacing_it():
    out = store.expand_dotted(
        {"generation": {"local_steps": 8}, "generation.local_resolution": "512x320"}
    )
    assert out == {"generation": {"local_steps": 8, "local_resolution": "512x320"}}


def test_already_nested_input_is_unchanged():
    nested = {"generation": {"local_steps": 8}, "budget": {"total_usd": 0}}
    assert store.expand_dotted(nested) == nested


def test_expansion_is_recursive():
    assert store.expand_dotted({"weaver": {"llm.base_url": "http://x"}}) == {
        "weaver": {"llm": {"base_url": "http://x"}}
    }


def test_a_dotted_key_colliding_with_a_non_dict_is_refused():
    # Silently discarding either value would be worse than saying so.
    with pytest.raises(ValueError, match="conflicts"):
        store.expand_dotted({"generation": 3, "generation.local_steps": 8})


# ---------------------------------------------------------------------------
# Selection config — party default, zone override, live keys
# ---------------------------------------------------------------------------


def test_selection_defaults():
    cfg = EgregoreConfig()
    s = cfg.weaver.selection
    assert (s.salience, s.novelty, s.recency) == (0.5, 0.3, 0.2)
    assert s.segment_gap_s == 6.0 and s.max_candidates == 6 and s.recency_tau_s is None


def test_selection_all_zero_weights_rejected():
    with pytest.raises(ValueError, match="weight"):
        EgregoreConfig.model_validate(
            {"weaver": {"selection": {"salience": 0, "novelty": 0, "recency": 0}}}
        )


def test_zone_selection_overrides_party_default():
    cfg = EgregoreConfig.model_validate({
        "weaver": {"selection": {"novelty": 0.9}},
        "zones": [{"id": "a"}, {"id": "b", "selection": {"novelty": 0.1}}],
    })
    assert cfg.zone_selection("a").novelty == 0.9
    assert cfg.zone_selection("b").novelty == 0.1
    assert cfg.zone_selection("b").salience == 0.5, "unset fields fall to the schema default"


def test_selection_keys_are_live():
    for k in ("salience", "novelty", "recency", "segment_gap_s", "max_candidates", "recency_tau_s"):
        assert f"weaver.selection.{k}" in store.LIVE_KEYS


def test_linger_and_fill_duration_defaults():
    cfg = EgregoreConfig()
    assert cfg.zones[0].hold_s == 0.0, "0 = a clip's own length"
    assert cfg.generation.fill_duration_s == 12
    assert "generation.fill_duration_s" in store.LIVE_KEYS


def test_hold_is_bounded():
    with pytest.raises(ValueError):
        EgregoreConfig.model_validate({"zones": [{"id": "a", "hold_s": 1000}]})


def test_lookback_and_room_bias_are_config_and_live():
    cfg = EgregoreConfig()
    assert cfg.weaver.selection.lookback_s is None, "None = about the last render"
    assert cfg.aesthetic.room_bias == 1.0
    assert "weaver.selection.lookback_s" in store.LIVE_KEYS
    assert "aesthetic.room_bias" in store.LIVE_KEYS


def test_local_quality_levels_resolve_to_steps_and_size():
    from egregore.config.schema import LOCAL_QUALITY, resolve_local_effort
    cfg = EgregoreConfig()
    assert cfg.generation.local_quality == "balanced"
    assert set(LOCAL_QUALITY) == {"fast", "balanced", "high"}
    assert resolve_local_effort(cfg.generation) == LOCAL_QUALITY["balanced"]
    fast = EgregoreConfig.model_validate({"generation": {"local_quality": "fast"}})
    assert resolve_local_effort(fast.generation)[0] < LOCAL_QUALITY["balanced"][0]
    # Explicit numbers win over the level, field by field.
    custom = EgregoreConfig.model_validate({"generation": {"local_quality": "high", "local_steps": 9}})
    steps, size = resolve_local_effort(custom.generation)
    assert steps == 9 and size == LOCAL_QUALITY["high"][1]
    assert "generation.local_quality" in store.LIVE_KEYS
