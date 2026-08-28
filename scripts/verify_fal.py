#!/usr/bin/env python
"""Check a fal.ai key against what Egregore's fal rung actually needs.

    uv run python scripts/verify_fal.py             # catalogue + auth check, free
    uv run python scripts/verify_fal.py --generate  # one real clip, billed

fal has no free "list my models" endpoint, so the only honest way to know a key
works is to spend a little. The default run still catches the common failure —
a key that is absent, malformed, or unauthorised — by making one unauthenticated
-shaped call and reading the status code.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx

from egregore.config import store as config_store
from egregore.forge.fal import DEFAULT_BASE_URL, FAL_MODELS, SAFETY_FACTOR

PROMPT = (
    "Abstract symbolic imagery: vast blue depth, surface breaking into light, "
    "slow tidal pull. Deep saturated color, organic forms dissolving into "
    "geometric ones. No text, no faces, no literal objects."
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true", help="spend money on one clip")
    ap.add_argument("--model", default="minimax-h3-max", choices=sorted(FAL_MODELS))
    ap.add_argument("--duration", type=int, default=5)
    ap.add_argument("--resolution", default="480P", choices=["480P", "768P"])
    args = ap.parse_args()

    # The setup wizard writes ~/.egregore/env; read it the same way a
    # party does, so a key that works there works here.
    config_store.load_env_file()
    key = os.environ.get("FAL_KEY")
    if not key:
        print("FAL_KEY is not set. export it and re-run.", file=sys.stderr)
        return 2
    print(f"key present: ...{key[-4:]} ({len(key)} chars)")

    print("\ncatalogue (standard prices — promos are deliberately not trusted):")
    for name, m in sorted(FAL_MODELS.items()):
        prices = "  ".join(f"{res} ${p}/s" for res, p in sorted(m.price_per_second.items()))
        print(f"  {name:16s} {m.model_id:32s} {prices}")
        print(f"  {'':16s} durations {sorted(m.allowed_durations_s)}  "
              f"reserves ${m.worst_price_per_second * 8 * SAFETY_FACTOR} for 8s")

    model = FAL_MODELS[args.model]
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=120.0) as http:
        if not args.generate:
            # A deliberately empty body: a good key gets a 422 (validation),
            # a bad one gets 401/403. Either way nothing is generated or billed.
            r = await http.post(
                f"{DEFAULT_BASE_URL}/{model.model_id}", json={}, headers=headers
            )
            if r.status_code in (401, 403):
                print(f"\nAUTH FAILED: HTTP {r.status_code} — key rejected by fal")
                return 1
            print(f"\nauth OK (empty request rejected as HTTP {r.status_code}, "
                  "which is validation, not auth)")
            print("Re-run with --generate to bill one real clip.")
            return 0

        est = model.price_per_second[args.resolution] * args.duration
        print(f"\ngenerating {args.duration}s at {args.resolution} on {model.model_id}")
        print(f"  <= ${est} at standard rates (less while a promo is running)")
        body = {
            "prompt": PROMPT,
            "duration": args.duration,
            "resolution": args.resolution,
            "aspect_ratio": "16:9",
            **model.extra_input,
        }
        t0 = time.monotonic()
        r = await http.post(
            f"{DEFAULT_BASE_URL}/{model.model_id}", json=body, headers=headers
        )
        if r.status_code >= 400:
            print(f"submit failed: HTTP {r.status_code}\n{r.text[:600]}", file=sys.stderr)
            return 1
        submitted = r.json()
        rid = submitted["request_id"]
        print(f"request_id: {rid}")

        # fal's own URLs: it does not put the whole model id in them.
        status_url = submitted["status_url"]
        result_url = submitted["response_url"]
        while time.monotonic() - t0 < 900:
            await asyncio.sleep(3)
            s = await http.get(status_url, headers=headers)
            body = s.json()
            if body.get("error"):
                print(f"FAILED: {body.get('error_type')} — {body.get('error')}")
                return 1
            status = body.get("status")
            if status == "COMPLETED":
                res = (await http.get(result_url, headers=headers)).json()
                url = (res.get("video") or {}).get("url")
                print(f"done in {time.monotonic() - t0:.0f}s")
                print(f"video: {url}")
                if url:
                    v = await http.get(url)
                    print(f"downloaded {len(v.content)} bytes ({v.headers.get('content-type')})")
                return 0
            print(f"  {status} ... {time.monotonic() - t0:.0f}s")
        print("timed out", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
