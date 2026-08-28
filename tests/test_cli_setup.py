"""Setup wizard tests.

The load-bearing one is that the wizard never prints a secret back: keeping
key entry off the web surface buys nothing if the CLI echoes it into a
terminal someone is screen-sharing.
"""

from __future__ import annotations

import pytest

from egregore import cli


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    return tmp_path


def test_probe_reports_each_dependency():
    probe = cli.probe_environment()
    assert set(probe) >= {"ffmpeg", "comfyui", "parakeet", "audio_input"}
    assert all(isinstance(v, str) for v in probe.values())


def test_probe_reports_secret_presence_not_values(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "do-not-print-me")
    probe = cli.probe_environment()
    assert probe["FAL_KEY"] == "set"
    assert "do-not-print-me" not in " ".join(probe.values())


def test_setup_writes_the_key_at_0600_and_never_echoes_it(monkeypatch, capsys, home):
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "s3cret-value")
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": default)
    assert cli.main(["setup", "--non-interactive-secret", "FAL_KEY"]) == 0

    env = home / "env"
    assert env.exists()
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert "s3cret-value" in env.read_text()
    assert "s3cret-value" not in capsys.readouterr().out


def test_setup_is_idempotent_and_replaces_rather_than_appends(monkeypatch, home):
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": default)
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "first")
    cli.main(["setup", "--non-interactive-secret", "FAL_KEY"])
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "second")
    cli.main(["setup", "--non-interactive-secret", "FAL_KEY"])

    body = (home / "env").read_text()
    assert body.count("FAL_KEY") == 1
    assert "second" in body and "first" not in body


def test_setup_skips_a_blank_key_without_writing_an_empty_value(monkeypatch, home):
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": default)
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "")
    assert cli.main(["setup", "--non-interactive-secret", "FAL_KEY"]) == 0
    assert not (home / "env").exists(), "a skipped key must not create a stub entry"


def test_setup_needs_no_config_file(monkeypatch, home):
    # `setup` is what someone runs *before* they have a config, so it must
    # dispatch ahead of any attempt to load one.
    monkeypatch.setattr(cli, "_prompt", lambda text, default="": default)
    monkeypatch.setattr(cli, "_prompt_secret", lambda name: "")
    assert cli.main(["setup"]) == 0
