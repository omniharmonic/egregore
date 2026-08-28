# Wiring up fal.ai

fal fronts a large catalogue of video models behind one queue protocol. Egregore
treats that as the point: `FalBackend` implements the protocol once, and each
model is a row in `FAL_MODELS`. **Adding a model is data, not code.**

## Where the key goes

    export FAL_KEY="..."

The fal rung is added when the key is set **and** `budget.total_usd > 0`. A
zero-budget preset can never reach any cloud, whatever is in the environment.

Verify before trusting it:

    uv run python scripts/verify_fal.py             # free: auth + catalogue
    uv run python scripts/verify_fal.py --generate  # billed: one real clip

The free run posts a deliberately empty request: a good key is rejected with a
422 (validation), a bad one with 401/403. Nothing is generated either way.

## Running it

    export FAL_KEY="..."
    uv run egregore run presets/fal-demo.yaml
    # http://127.0.0.1:8440/?zone=main

The ladder in that preset is **fal -> ComfyUI/LTX -> procedural**. A refusal, an
outage, or an exhausted budget drops a rung, so the dream never starves.

## Configuration

```yaml
generation:
  backend: "fal"
  fal_model: "minimax-h3-max"   # key into FAL_MODELS
  resolution: "480p"            # mapped onto what the model offers
  clip_duration_s: 5            # MiniMax will not go below 5s
  fallback: "local"
```

Party configs speak `1080p`; these models top out at `768P`. Rather than fail a
request over a cosmetic mismatch, the backend maps the request onto the best
resolution the model has and logs it.

## The catalogue

| key | fal model id | 480P | 768P | durations |
|---|---|---|---|---|
| `minimax-h3-max` | `minimax/h3-max/text-to-video` | $0.05/s | $0.08/s | 5-8s |
| `minimax-h3` | `minimax/h3/text-to-video` | $0.05/s | $0.06/s | 5-8s |

Adding another fal video model is one entry in `egregore/forge/fal.py`:

```python
"some-model": FalModel(
    model_id="vendor/some-model/text-to-video",
    price_per_second={"480P": Decimal("0.03"), "720P": Decimal("0.06")},
    default_resolution="720P",
    allowed_durations_s=frozenset({5, 6, 8}),
    extra_input={"whatever_knob": "value"},   # model-specific body fields
),
```

`extra_input` is how per-model quirks stay out of the shared request builder —
MiniMax's required `prompt_expansion_mode` lives there, not in `_build_input`.

## Prices are the standard rates, never the promo

MiniMax H3 Max is **$0.025/s at 480P on the launch promo**, against $0.05
standard. The catalogue records **$0.05**, and the ceiling is held against that.

This is deliberate. A reservation is made *before* a generation that may land on
the far side of a promo's expiry, so reserving at promo prices would breach the
hard ceiling the day the promo ends. That is precisely the bug the Veo table
had. You pay promo prices; you are only ever *reserved* against standard ones,
and the ledger releases the difference on settle.

Reservations price the configured resolution times a 2x safety factor — at
480P, `$0.05 x 5s x 2 = $0.50` for a 5-second clip.

## What these models cannot do

- **No continuation.** No model in the catalogue can extend one of its own
  clips, so `supports_native_extend` is false and `max_chain_length` is 0. Run
  these zones in `mosaic` mode; asking for an extension raises rather than
  silently generating an unrelated clip into a movement chain.
- **No first-frame seed, yet.** MiniMax's image-to-video endpoint
  (`minimax/h3-max/image-to-video`) takes `image_url` — a *publicly reachable
  URL*, not inline bytes — while Egregore's continuity handoff produces raw PNG
  bytes. Wiring it up means uploading each seed frame to fal's storage first.
  Until then `supports_image_seed` is false and a seed raises rather than being
  quietly dropped.
- **Minimum 5 seconds.** `clip_duration_s: 4` is valid for Egregore but below
  MiniMax's floor; the backend refuses before spending anything and the ladder
  drops a rung.

## Cost in practice

At 480P promo, a 5-second clip is **$0.125**. A 3-hour party generating one clip
every 90 s is roughly 120 clips — about **$15** at promo, $30 at standard. For
comparison, Veo 3.1 Standard at $0.40/s would be $2.00 per 5-second clip, or
~$240 for the same party: **16x more**.

Keep `budget.total_usd` set to what you are actually willing to spend. It is a
hard ceiling the Governor cannot exceed, not a target.
