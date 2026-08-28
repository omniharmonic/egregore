#!/usr/bin/env python
"""Check a Gemini API key against what Egregore's Veo rung actually needs.

    uv run python scripts/verify_veo.py            # discovery only, free
    uv run python scripts/verify_veo.py --generate # one real 4s clip, billed

Discovery is free and answers the questions the docs are ambiguous about on
any given day: which Veo model ids this key can see, and whether they are
preview or GA. Generation is the only way to learn whether the key's project
is on a paid tier with Veo quota — that is not exposed anywhere readable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx

from egregore.forge.veo import COST_PER_SECOND_BY_RESOLUTION, DEFAULT_MODEL_IDS

BASE = "https://generativelanguage.googleapis.com/v1beta"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true", help="spend money on one 4s clip")
    ap.add_argument("--tier", default="veo-3.1-lite", choices=sorted(DEFAULT_MODEL_IDS))
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set. export it and re-run.", file=sys.stderr)
        return 2
    print(f"key present: ...{key[-4:]} ({len(key)} chars)")
    headers = {"x-goog-api-key": key}

    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.get(f"{BASE}/models", headers=headers, params={"pageSize": 200})
        if r.status_code >= 400:
            print(f"\nmodel list failed: HTTP {r.status_code}\n{r.text[:400]}", file=sys.stderr)
            return 1
        names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])]
        veo = sorted(n for n in names if "veo" in n.lower())
        print(f"\nveo models visible to this key ({len(veo)}):")
        for n in veo:
            print(f"  {n}")
        if not veo:
            print("  (none — this key cannot see any Veo model)")

        print("\nEgregore's tier -> model mapping:")
        ok = True
        for tier, model in sorted(DEFAULT_MODEL_IDS.items()):
            hit = "OK" if model in veo else "MISSING"
            if model not in veo:
                ok = False
            worst = max(COST_PER_SECOND_BY_RESOLUTION[tier].values())
            print(f"  {tier:20s} -> {model:34s} {hit}   (<= ${worst}/s)")
        if not ok:
            print("\nSome mapped ids are not visible. Override them without a code change:")
            print("  VeoBackend(..., model_for_tier={...}) in build_ladder,")
            print("  or pick ids from the list above.")

        if not args.generate:
            print("\nDiscovery only. Re-run with --generate to bill one 4s clip.")
            return 0 if ok else 1

        model = DEFAULT_MODEL_IDS[args.tier]
        worst = max(COST_PER_SECOND_BY_RESOLUTION[args.tier].values())
        print(f"\ngenerating 4s on {model} (bills up to ~${worst * 4})")
        payload = {
            "instances": [{"prompt": "Abstract symbolic imagery: vast blue depth, "
                                     "slow tidal pull, organic forms dissolving into "
                                     "geometric ones. No text, no faces."}],
            "parameters": {"aspectRatio": "16:9", "resolution": "720p", "durationSeconds": 4},
        }
        t0 = time.monotonic()
        r = await http.post(f"{BASE}/models/{model}:predictLongRunning",
                            headers=headers, json=payload)
        if r.status_code >= 400:
            print(f"submit failed: HTTP {r.status_code}\n{r.text[:600]}", file=sys.stderr)
            return 1
        op = r.json().get("name")
        print(f"operation: {op}")
        while time.monotonic() - t0 < 600:
            await asyncio.sleep(5)
            p = await http.get(f"{BASE}/{op}", headers=headers)
            body = p.json()
            if body.get("done"):
                if "error" in body:
                    print(f"FAILED after {time.monotonic()-t0:.0f}s: {body['error']}")
                    return 1
                print(f"done in {time.monotonic() - t0:.0f}s")
                print(f"response keys: {list(body.get('response', {}))}")
                return 0
            print(f"  polling... {time.monotonic() - t0:.0f}s")
        print("timed out after 600s", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
