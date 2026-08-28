"""Google Veo 3.1 cloud backend (Architecture §2.6).

Long-running-operation shape: submit to `models/{model}:predictLongRunning`,
poll the returned operation until `done`, then download the sample's video
URI. Nothing is streamed and nothing is cached provider-side that we rely
on beyond the operation's own lifetime.

Two things are deliberate and load-bearing:

* PRD V-3 (the room supplies its own sound) is enforced at playback, not at
  generation. Veo 3.x always generates audio and *rejects* a `generateAudio`
  parameter, so sending one fails the request; the Lens plays every clip muted
  instead. The corollary is economic: the video-only rate V-3 assumed does not
  exist, so `COST_PER_SECOND_BY_RESOLUTION` carries the real, higher prices.
* `max_plausible_cost` is a *worst case*, not an estimate. The Governor
  reserves against it so the hard ceiling holds even if the published price
  table is wrong (Architecture §2.5). The published per-second numbers are
  multiplied by `SAFETY_FACTOR`.

Privacy: the validated outbound prompt is the only text that leaves the
building, it is passed straight through to the request body, and it is
never logged here.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from egregore.types import (
    BackendCapabilities,
    BackendHealth,
    BackendStatus,
    ClipRef,
    ThemeObject,
)

from .store import ClipStore

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

TIER_LITE = "veo-3.1-lite"
TIER_FAST = "veo-3.1-fast"
TIER_QUALITY = "veo-3.1-quality"

_TIERS = frozenset({TIER_LITE, TIER_FAST, TIER_QUALITY})

#: US dollars per generated second, per tier, per resolution — verified against
#: the published Gemini API price list (August 2026). Architecture §2.5 carried
#: a video-only table roughly 2-4x low; Veo 3.x always generates audio and has
#: no video-only rate, so the discount those numbers assumed does not exist.
#: Reserving against the old table under-reserved the Lite tier, which is the
#: one thing the ceiling may never do (PRD B-2).
COST_PER_SECOND_BY_RESOLUTION: dict[str, dict[str, Decimal]] = {
    TIER_LITE: {"720p": Decimal("0.05"), "1080p": Decimal("0.08")},
    TIER_FAST: {"720p": Decimal("0.10"), "1080p": Decimal("0.12"), "4k": Decimal("0.30")},
    TIER_QUALITY: {"720p": Decimal("0.40"), "1080p": Decimal("0.40"), "4k": Decimal("0.60")},
}

#: Worst case across every resolution a tier offers. Reservations use this
#: rather than the configured resolution so a resolution changed at runtime,
#: or one the price table does not know, still cannot breach the ceiling.
COST_PER_SECOND: dict[str, Decimal] = {
    tier: max(prices.values()) for tier, prices in COST_PER_SECOND_BY_RESOLUTION.items()
}

#: Reserve twice the published price. A cost model wrong by 2x still cannot
#: breach the ceiling; the Governor reconciles down on settle.
SAFETY_FACTOR = Decimal("2")

#: Tier to provider model id. ASSUMPTION (Phase 0, Architecture §9): these
#: ids are preview-channel names and move. The operator can override the
#: whole mapping via the `model_for_tier` constructor argument rather than
#: waiting for a release.
DEFAULT_MODEL_IDS: dict[str, str] = {
    TIER_LITE: "veo-3.1-lite-generate-preview",
    TIER_FAST: "veo-3.1-fast-generate-preview",
    TIER_QUALITY: "veo-3.1-generate-preview",
}

#: Tiers whose model can continue a video it generated (Veo 3.1 and 3.1 Fast).
#: Lite cannot, so continuity mode on the Lite tier must not chain.
EXTENDABLE_TIERS = frozenset({TIER_FAST, TIER_QUALITY})

_LATENCY_S: dict[str, float] = {TIER_LITE: 45.0, TIER_FAST: 60.0, TIER_QUALITY: 120.0}

_ALLOWED_DURATIONS = frozenset({4, 6, 8})

#: Extension cap observed in Architecture §2.6 / §3.2, pending Phase 0.
MAX_CHAIN_LENGTH = 20


class VeoBackend:
    """Veo 3.1 `VideoBackend`."""

    def __init__(
        self,
        store: ClipStore,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        name: str = "veo",
        resolution: str = "1080p",
        aspect_ratio: str = "16:9",
        poll_interval_s: float = 5.0,
        timeout_s: float = 600.0,
        request_timeout_s: float = 60.0,
        model_for_tier: dict[str, str] | None = None,
        send_generate_audio: bool = False,
    ) -> None:
        self.send_generate_audio = send_generate_audio
        self.name = name
        self.store = store
        # Constructor argument wins; otherwise the environment. Never logged.
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.resolution = resolution
        self.aspect_ratio = aspect_ratio
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.request_timeout_s = request_timeout_s
        self.model_for_tier = dict(model_for_tier or DEFAULT_MODEL_IDS)

        self._client = client
        self._owns_client = client is None

        #: clip id -> provider-side video URI. Native extension only accepts a
        #: provider-generated source video (Architecture §2.6), so a local
        #: path is useless to it; we remember what the provider gave us.
        self._remote_uri: dict[str, str] = {}

    # -- protocol ----------------------------------------------------------

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            allowed_durations_s=_ALLOWED_DURATIONS,
            supports_native_extend=True,
            supports_image_seed=True,
            tiers=_TIERS,
            max_chain_length=MAX_CHAIN_LENGTH,
        )

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
        """Worst-case dollars for one request. Never an expectation.

        Deliberately reserves against the tier's most expensive resolution
        rather than the configured one: the ceiling must hold even if the
        resolution changes under us or the price table is stale.
        """
        per_second = COST_PER_SECOND.get(tier)
        if per_second is None:
            # Unknown tier: charge as if it were the most expensive one.
            per_second = max(COST_PER_SECOND.values())
        return per_second * Decimal(int(duration_s)) * SAFETY_FACTOR

    def estimated_latency(self, tier: str) -> timedelta:
        return timedelta(seconds=_LATENCY_S.get(tier, 90.0))

    async def health(self) -> BackendHealth:
        """No key means DOWN; that is the whole check.

        A live probe would make every test and every offline start-up depend
        on the network, and the ladder already treats a failed generation as
        a reason to drop to the next rung, so a network probe would buy very
        little for what it costs.
        """
        if not self.api_key:
            return BackendHealth(BackendStatus.DOWN, "no GEMINI_API_KEY")
        return BackendHealth(BackendStatus.HEALTHY, "api key present")

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        tier: str,
        seed_image: bytes | None = None,
        extend_from: ClipRef | None = None,
        theme_hint: ThemeObject | None = None,
        zone: str = "default",
    ) -> ClipRef:
        """Submit, poll, download, store. `theme_hint` is ignored by design —
        the cloud sees only the validated prompt string."""
        if not self.api_key:
            raise RuntimeError(f"{self.name}: no API key configured")
        if duration_s not in _ALLOWED_DURATIONS:
            raise ValueError(
                f"{self.name}: duration {duration_s}s not in {sorted(_ALLOWED_DURATIONS)}"
            )
        if tier not in _TIERS:
            raise ValueError(f"{self.name}: unknown tier {tier!r}")

        payload = self._build_payload(prompt, duration_s, tier, seed_image, extend_from)
        model = self.model_for_tier.get(tier, DEFAULT_MODEL_IDS[TIER_QUALITY])

        started = time.monotonic()
        operation = await self._submit(model, payload)
        result = await self._poll(operation)
        uri = _extract_video_uri(result)

        tmp_path = self.store.temp_path()
        try:
            await self._download(uri, tmp_path)
            ref = await self.store.put(
                tmp_path,
                duration_s=float(duration_s),
                zone=zone,
                backend=self.name,
                tier=tier,
                movement_id=extend_from.movement_id if extend_from else None,
                chain_index=(extend_from.chain_index + 1) if extend_from else 0,
            )
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        self._remote_uri[ref.id] = uri
        log.info(
            "veo clip backend=%s zone=%s tier=%s duration=%ds wall=%.1fs",
            self.name,
            zone,
            tier,
            duration_s,
            time.monotonic() - started,
        )
        return ref

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- internals ---------------------------------------------------------

    def _build_payload(
        self,
        prompt: str,
        duration_s: int,
        tier: str,
        seed_image: bytes | None,
        extend_from: ClipRef | None,
    ) -> dict:
        instance: dict = {"prompt": prompt}

        if seed_image is not None:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(seed_image).decode("ascii"),
                "mimeType": "image/png",
            }

        if extend_from is not None:
            # ASSUMPTION (Phase 0): extension takes the *provider's* URI for a
            # video it generated itself, not an upload. A clip that did not
            # come from this backend in this process cannot be extended, and
            # saying so loudly beats silently generating an unrelated clip.
            if tier not in EXTENDABLE_TIERS:
                # Veo 3.1 Lite has no continuation mode. Failing here beats
                # silently generating an unrelated clip into a movement chain.
                raise RuntimeError(
                    f"{self.name}: tier {tier!r} cannot extend video "
                    f"(only {sorted(EXTENDABLE_TIERS)} support continuation)"
                )
            uri = self._remote_uri.get(extend_from.id)
            if uri is None:
                raise RuntimeError(
                    f"{self.name}: cannot extend clip {extend_from.id} — "
                    "no provider-side video reference (extension accepts only "
                    "provider-generated source video)"
                )
            instance["video"] = {"uri": uri}

        parameters: dict = {
            "aspectRatio": self.aspect_ratio,
            "resolution": self.resolution,
            "durationSeconds": duration_s,
        }
        if self.send_generate_audio:
            # Veo 3.x generates audio unconditionally and *rejects* this
            # parameter, so sending it fails the request outright. PRD V-3 is
            # still honoured, one step later: the Lens plays every clip muted,
            # so the room only ever hears itself. What V-3 can no longer buy is
            # the video-only discount it assumed — that rate no longer exists,
            # which is why the price table above went up. Kept as a flag for
            # API surfaces (Veo 2, some proxies) that do accept it.
            parameters["generateAudio"] = False
        return {"instances": [instance], "parameters": parameters}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
            self._owns_client = True
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"}

    async def _submit(self, model: str, payload: dict) -> str:
        url = f"{self.base_url}/models/{model}:predictLongRunning"
        response = await self._http().post(url, json=payload, headers=self._headers())
        _raise_for_status(self.name, response, "submit")
        body = response.json()
        operation = body.get("name")
        if not operation:
            raise RuntimeError(f"{self.name}: submit returned no operation name")
        return str(operation)

    async def _poll(self, operation: str) -> dict:
        url = f"{self.base_url}/{operation.lstrip('/')}"
        deadline = time.monotonic() + self.timeout_s
        while True:
            response = await self._http().get(url, headers=self._headers())
            _raise_for_status(self.name, response, "poll")
            body = response.json()
            if body.get("done"):
                error = body.get("error")
                if error:
                    raise RuntimeError(
                        f"{self.name}: operation failed "
                        f"({error.get('code')}: {error.get('message')})"
                    )
                return body
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{self.name}: operation not done after {self.timeout_s:.0f}s"
                )
            await asyncio.sleep(self.poll_interval_s)

    async def _download(self, uri: str, dest: Path) -> None:
        response = await self._http().get(uri, headers={"x-goog-api-key": self.api_key or ""})
        _raise_for_status(self.name, response, "download")
        data = response.content
        if not data:
            raise RuntimeError(f"{self.name}: downloaded video was empty")
        await asyncio.to_thread(dest.write_bytes, data)


def _raise_for_status(name: str, response: httpx.Response, stage: str) -> None:
    if response.status_code >= 400:
        # Provider error text can echo the request; keep only the status.
        raise RuntimeError(f"{name}: {stage} returned HTTP {response.status_code}")


def _extract_video_uri(operation: dict) -> str:
    """Pull the sample URI out of a completed operation body."""
    try:
        samples = operation["response"]["generateVideoResponse"]["generatedSamples"]
        uri = samples[0]["video"]["uri"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("veo: completed operation had no video sample") from exc
    if not uri:
        raise RuntimeError("veo: completed operation had an empty video uri")
    return str(uri)


__all__ = [
    "COST_PER_SECOND",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_IDS",
    "MAX_CHAIN_LENGTH",
    "SAFETY_FACTOR",
    "TIER_FAST",
    "TIER_LITE",
    "TIER_QUALITY",
    "VeoBackend",
]
