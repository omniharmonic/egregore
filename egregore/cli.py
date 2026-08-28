"""Egregore command-line interface.

Commands:
  egregore run <config.yaml>    start a party
  egregore check <config.yaml>  validate a config and print a summary
  egregore wipe <config.yaml>   delete the clip store (post-party cleanup)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="egregore", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start a party from a config file")
    p_run.add_argument("config", type=Path)

    p_check = sub.add_parser("check", help="validate a config file")
    p_check.add_argument("config", type=Path)

    p_wipe = sub.add_parser("wipe", help="delete the clip store")
    p_wipe.add_argument("config", type=Path)

    args = parser.parse_args(argv)

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
        asyncio.run(run_party(cfg))
    except KeyboardInterrupt:
        print("\nshutdown: ring buffers zeroed, dream ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
