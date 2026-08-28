# Wiring up Veo

## Where the key goes

Egregore reads one environment variable. Nothing else needs editing:

    export GEMINI_API_KEY="AIza..."

`build_ladder` adds the Veo rung only when **both** are true: the key is set,
and `budget.total_usd > 0`. A zero-budget preset can never reach the cloud no
matter what is in the environment — that is the privacy story, not a policy
toggle. If the key is missing you get a warning and the ladder falls through to
local/procedural, which is why an unset key degrades instead of crashing.

Nothing logs the key, and `health()` reports only whether one is present.

Persist it in your shell profile, or keep it in a `.env` you never commit:

    echo 'export GEMINI_API_KEY="AIza..."' >> ~/.zshrc

Verify before trusting it in front of an audience:

    uv run python scripts/verify_veo.py             # free: lists visible models
    uv run python scripts/verify_veo.py --generate  # billed: one real 4s clip

## Which key

**Google AI Studio API key, on a project with billing enabled**
(<https://aistudio.google.com/apikey>). Two things matter:

1. **Veo is not on the free tier.** A free key authenticates fine and fails at
   generation, which looks like a broken integration rather than a billing one.
2. **The tier sets your Veo quota, and quota is the real constraint** — see
   below.

The alternative is **Vertex AI**, which uses a service account and
`gcloud` ADC rather than an API key, and has separate quota. `VeoBackend` speaks
the Gemini API shape (`x-goog-api-key`, `:predictLongRunning`), so Vertex would
need a different `base_url` and an auth change. Only worth it if you hit quota
ceilings you cannot raise.

## Quota is the binding constraint, not money

Tiers are cumulative-spend gates on the billing project:

| tier | how you get there |
|---|---|
| Free | billing off — **no Veo** |
| Tier 1 | billing on |
| Tier 2 | $100 spent + 3 days |
| Tier 3 | $1,000 spent + 30 days |

Google does not publish per-tier Veo request caps in the docs; they are on the
[AI Studio rate-limit page](https://aistudio.google.com/rate-limit) for your
project. Developer reports put **Tier 1 around 10 Veo requests per day**, and
**failed generations still consume quota** — including refusals from the audio
safety filter, which fires on prompts that read as harmless.

Take that seriously when planning a party. At ~10 clips/day, Veo cannot be the
workhorse: budget-based cadence will happily plan a clip every 90 s and exhaust
the day's quota in a quarter of an hour, after which every request fails and the
ladder drops to local/procedural for the rest of the night. Treat the cloud rung
as *seasoning* — a slow, expensive, high-quality garnish over a local loop —
until you have measured your own quota on the AI Studio page.

## What the research changed in the code

Three Phase-0 assumptions in `forge/veo.py` were wrong, and two of them would
have broken a live party:

**`generateAudio: false` is rejected.** Veo 3.x always generates audio and
errors on the parameter, so every request would have failed. It is no longer
sent. PRD V-3 still holds, one step later: the Lens plays every clip muted, so
the room only hears itself.

**The price table was 2-4x low, and under-reserved.** It assumed a video-only
discount that does not exist. The Lite tier billed $0.64 for an 8 s clip while
the Governor reserved $0.48 — a hard-ceiling breach, which PRD B-2 says must be
structurally impossible. Prices are now per-tier *and* per-resolution, and
reservations use each tier's most expensive resolution so a resolution changed
at runtime cannot breach the ceiling:

| tier | 720p | 1080p | 4k | reserved for 8s |
|---|---|---|---|---|
| `veo-3.1-lite` | $0.05 | $0.08 | — | $1.28 |
| `veo-3.1-fast` | $0.10 | $0.12 | $0.30 | $4.80 |
| `veo-3.1-quality` | $0.40 | $0.40 | $0.60 | $9.60 |

**Lite cannot extend.** Scene extension is Veo 3.1 and 3.1 Fast only. The Lite
tier previously pointed at the Fast model id, hiding this; it now points at
`veo-3.1-lite-generate-preview` and asking it to extend raises rather than
silently generating an unrelated clip into a movement chain.

Extension is also **720p only**, up to 20 extensions in 7-second increments to a
148 s maximum — so continuity mode at 1080p will not extend. `MAX_CHAIN_LENGTH`
of 20 matches.

## Model ids move

`DEFAULT_MODEL_IDS` maps tiers to preview-channel ids:

    veo-3.1-lite  -> veo-3.1-lite-generate-preview
    veo-3.1-fast  -> veo-3.1-fast-generate-preview
    veo-3.1-quality -> veo-3.1-generate-preview

Google's changelog is genuinely ambiguous about the preview/GA split right now:
a deprecation notice points at April 2 2026, while the June 15 2026 entry still
recommends these same preview ids, and GA ids are described as living on the
Gemini Enterprise Agent Platform rather than the Gemini API. `verify_veo.py`
settles it for your key by listing what it can actually see. Override without a
code change if they have moved:

    VeoBackend(store, model_for_tier={"veo-3.1-quality": "veo-3.1-generate-001", ...})
