"""Egregore command-line interface.

Commands:
  egregore setup                first-run wizard: probe the box, store keys
  egregore run <config.yaml>    start a party
  egregore check <config.yaml>  validate a config and print a summary
  egregore wipe <config.yaml>   delete the clip store (post-party cleanup)

``setup`` is the only place a credential is ever typed in. It writes
``~/.egregore/env`` at mode 0600 and never echoes what it stored -- the
whole point of keeping key entry off the web surface is undone by printing
the key back into a terminal someone is screen-sharing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def probe_environment() -> dict[str, str]:
    """What this machine can already do. Strings, so they print as-is."""
    import urllib.error
    import urllib.request

    from egregore.config import store as _store

    out: dict[str, str] = {}
    out["ffmpeg"] = "found" if shutil.which("ffmpeg") else "MISSING - brew install ffmpeg"

    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2).read(1)
        out["comfyui"] = "running on :8188"
    except (urllib.error.URLError, OSError, ValueError):
        out["comfyui"] = "not running (local diffusion unavailable)"

    onnx = os.environ.get("EGREGORE_PARAKEET_ONNX_DIR") or str(
        Path.home() / ".egregore" / "models" / "parakeet-v2-int8"
    )
    out["parakeet"] = f"found at {onnx}" if Path(onnx).is_dir() else "not installed"

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

    print("\n  egregore setup")
    print("  " + "-" * 46)
    for key, value in probe_environment().items():
        print(f"  {key:22s} {value}")

    wanted = (
        [args.non_interactive_secret]
        if args.non_interactive_secret
        else ["FAL_KEY", "GEMINI_API_KEY"]
    )
    print(f"\n  Keys are stored at {_store.env_path()} (mode 600) and are never")
    print("  sent to the browser. Leave blank to skip one.\n")

    for name in wanted:
        value = _prompt_secret(name)
        if value:
            _store.write_secret(name, value)
            print(f"  {name} saved")  # deliberately does not echo the value
        else:
            print(f"  {name} skipped")

    presets = sorted(Path("presets").glob("*.yaml"))
    if presets:
        print("\n  Presets:")
        for path in presets:
            print(f"    {path}")
    chosen = _prompt("\n  Which preset should `egregore run` use?", "presets/demo.yaml")
    print(f"\n  Ready:     uv run egregore run {chosen}")
    print("  Screens:   http://localhost:8420/?zone=main")
    print("  Settings:  http://localhost:8420/static/setup.html\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="egregore", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start a party from a config file")
    p_run.add_argument("config", type=Path)
    p_run.add_argument(
        "--ignore-settings",
        action="store_true",
        help="run the preset exactly as written, ignoring ~/.egregore/settings.yaml",
    )

    p_check = sub.add_parser("check", help="validate a config file")
    p_check.add_argument("config", type=Path)

    p_wipe = sub.add_parser("wipe", help="delete the clip store")
    p_wipe.add_argument("config", type=Path)

    p_setup = sub.add_parser("setup", help="first-run wizard: probe, keys, preset")
    p_setup.add_argument(
        "--non-interactive-secret",
        default=None,
        help="prompt for only this secret (used by tests)",
    )

    args = parser.parse_args(argv)

    # setup takes no config file, so it dispatches before load_config runs.
    if args.command == "setup":
        return _cmd_setup(args)

    from egregore.config.schema import load_config

    try:
        cfg = load_config(args.config)
    except Exception as e:  # pydantic errors are already descriptive
        print(f"config error in {args.config}:\n{e}", file=sys.stderr)
        return 2

    if args.command == "check":
        zones = ", ".join(z.id for z in cfg.zones)
        print(f"OK: {cfg.party.name!r}")
        print(f"  duration: {cfg.party.duration_hours}h  zones: {zones}")
        print(f"  backend: {cfg.generation.backend} -> {cfg.generation.fallback}")
        print(f"  budget (hard ceiling): ${cfg.budget.total_usd}")
        print(f"  asr: {cfg.asr.engine}  weaver: {cfg.weaver.engine}")
        mode = "LOCAL — nothing derived from speech leaves this machine" \
            if cfg.budget.total_usd == 0 else \
            "CLOUD-CAPABLE — abstracted prompts may be sent to the cloud backend"
        print(f"  privacy mode: {mode}")
        return 0

    if args.command == "wipe":
        store = Path(cfg.clip_store_dir)
        if store.exists():
            shutil.rmtree(store)
            print(f"wiped {store}")
        else:
            print(f"nothing at {store}")
        return 0

    # run
    import asyncio

    from egregore.app import run_party

    try:
        asyncio.run(run_party(cfg, ignore_settings=args.ignore_settings))
    except KeyboardInterrupt:
        print("\nshutdown: ring buffers zeroed, dream ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
