"""fal.ai cloud backend — one client, many models (Architecture §2.6).

fal fronts a large catalogue of video models behind a single queue protocol, so
this backend is deliberately *model-agnostic*: the protocol lives in code and
each model is a row in :data:`FAL_MODELS`. Adding a model is data, not a new
backend, which is the whole reason for preferring fal over a per-vendor client.

Queue shape (https://fal.ai/docs/model-endpoints/queue):

    POST https://queue.fal.run/{model_id}                      -> 202 {request_id, ...}
    GET  https://queue.fal.run/{model_id}/requests/{id}/status -> IN_QUEUE|IN_PROGRESS|COMPLETED
    GET  https://queue.fal.run/{model_id}/requests/{id}        -> {"video": {"url": ...}}

Two deliberate choices:

* **Prices are the standard rates, never promotional ones.** A promo is a date
  with a cliff behind it, and the Governor reserves ahead of a generation that
  may land on the far side of it. Reserving at promo prices would breach the
  hard ceiling the day the promo ends — the exact class of bug the Veo price
  table had (PRD B-2).
* **`max_plausible_cost` prices the configured resolution** (fixed at
  construction) times a 2x safety factor, falling back to the model's most
  expensive resolution when that lookup misses. Reserving at the worst
  resolution unconditionally is safe but stranded most of a small budget
  behind reservations that settled at a sixth of their reserved amount.

Privacy: the validated outbound prompt is the only text that leaves the
building; it goes straight into the request body and is never logged here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
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

DEFAULT_BASE_URL = "https://queue.fal.run"

#: Terminal queue state. Anything else means keep polling.
STATUS_COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class FalModel:
    """One row of the catalogue: how to call a model and what it may cost."""

    #: Path segment in the queue URL, e.g. ``minimax/h3-max/text-to-video``.
    model_id: str
    #: Standard (non-promotional) US dollars per generated second, by the
    #: resolution string this model's API expects.
    price_per_second: dict[str, Decimal]
    #: Resolution used when the party config asks for one this model lacks.
    default_resolution: str
    #: Whole seconds this model will accept.
    allowed_durations_s: frozenset[int]
    #: Which backend can drive this row. Only "fal" today; carried so the
    #: catalogue can describe other providers without a second registry.
    provider: str = "fal"
    #: Extra body fields sent on every request (model-specific knobs).
    extra_input: dict = field(default_factory=dict)
    #: Rough wall time for one clip, refined by observation once running.
    #: Seeded low rather than high on purpose: the Governor will not schedule
    #: faster than this, so a pessimistic seed stalls the *first* clip and
    #: therefore stalls the measurement that would correct it.
    initial_latency_s: float = 20.0
    #: Most models take a first frame only as a URL we would have to host, so
    #: seeding is off unless a model is known to accept inline data.
    supports_image_seed: bool = False

    @property
    def worst_price_per_second(self) -> Decimal:
        return max(self.price_per_second.values())


#: The catalogue. Prices are fal's **standard** rates, deliberately not the
#: promotional ones — see the module docstring.
FAL_MODELS: dict[str, FalModel] = {
    # $0.025/s (480P) and $0.04/s (768P) while the launch promo runs; the
    # standard rates below are what the ceiling is held against.
    "minimax-h3-max": FalModel(
        model_id="minimax/h3-max/text-to-video",
        price_per_second={"480P": Decimal("0.05"), "768P": Decimal("0.08")},
        default_resolution="768P",
        allowed_durations_s=frozenset({5, 6, 7, 8}),
        extra_input={"prompt_expansion_mode": "balanced"},
        # Measured at ~7s for a 5s clip on 2026-08-28; seeded a little above
        # that so a slow queue does not immediately swamp the loop.
        initial_latency_s=20.0,
    ),
    "minimax-h3": FalModel(
        model_id="minimax/h3/text-to-video",
        price_per_second={"480P": Decimal("0.05"), "768P": Decimal("0.06")},
        default_resolution="768P",
        allowed_durations_s=frozenset({5, 6, 7, 8}),
        extra_input={"prompt_expansion_mode": "balanced"},
        initial_latency_s=20.0,
    ),
}

#: Party-config resolution strings -> the enum fal models actually accept.
_RESOLUTION_ALIASES = {
    "480p": "480P",
    "480P": "480P",
    "720p": "768P",
    "768p": "768P",
    "768P": "768P",
    "1080p": "768P",  # no model here goes higher; ask for the best available
    "4k": "768P",
}

SAFETY_FACTOR = Decimal("2")


class FalBackend:
    """A `VideoBackend` over fal's queue API, parameterised by model."""

    def __init__(
        self,
        store: ClipStore,
        *,
        model: str = "minimax-h3-max",
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        name: str = "fal",
        resolution: str = "768P",
        aspect_ratio: str = "16:9",
        poll_interval_s: float = 3.0,
        timeout_s: float = 900.0,
        request_timeout_s: float = 60.0,
        catalogue: dict[str, FalModel] | None = None,
        latency_smoothing: float = 0.3,
    ) -> None:
        self.name = name
        self.store = store
        self.catalogue = dict(catalogue or FAL_MODELS)
        if model not in self.catalogue:
            raise ValueError(
                f"unknown fal model {model!r}; known: {', '.join(sorted(self.catalogue))}"
            )
        self.model_key = model
        self.model = self.catalogue[model]
        # Constructor argument wins; otherwise the environment. Never logged.
        self.api_key = api_key if api_key is not None else os.environ.get("FAL_KEY")
        self.base_url = base_url.rstrip("/")
        self.aspect_ratio = aspect_ratio
        self.resolution = self._resolve_resolution(resolution)
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.request_timeout_s = request_timeout_s

        self.latency_smoothing = float(latency_smoothing)
        self._latency_s = float(self.model.initial_latency_s)
        self._observed = 0

        self._client = client
        self._owns_client = client is None

    def _resolve_resolution(self, requested: str) -> str:
        """Map a party-config resolution onto one this model accepts."""
        mapped = _RESOLUTION_ALIASES.get(requested, requested)
        if mapped in self.model.price_per_second:
            return mapped
        log.warning(
            "fal model %s has no resolution %r; using %s",
            self.model_key, requested, self.model.default_resolution,
        )
        return self.model.default_resolution

    # -- protocol ----------------------------------------------------------

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            allowed_durations_s=self.model.allowed_durations_s,
            # No model here can continue one of its own videos, so continuity
            # runs as mosaic rather than as a chain.
            supports_native_extend=False,
            supports_image_seed=self.model.supports_image_seed,
            # Only the model this rung was configured with. Advertising the
            # whole catalogue meant the Forge, asked for a tier this backend
            # does not offer, fell back to sorted(tiers)[0] — so an operator
            # who chose minimax-h3-max silently got minimax-h3 instead.
            tiers=frozenset({self.model_key}),
            max_chain_length=0,
        )

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
        """Worst-case dollars for one request. Never an expectation.

        Priced at the resolution this backend is actually configured for —
        which is fixed at construction — times ``SAFETY_FACTOR``. Falling back
        to the model's most expensive resolution whenever that lookup misses
        keeps the ceiling safe without over-reserving 6x on every clip, which
        would strand most of a small budget behind reservations that never
        settle for anywhere near their reserved amount.

        An unknown tier is charged as the most expensive model in the
        catalogue: a request we cannot price is not a free one.
        """
        model = self.catalogue.get(tier)
        if model is None:
            per_second = max(m.worst_price_per_second for m in self.catalogue.values())
        else:
            per_second = model.price_per_second.get(
                self.resolution, model.worst_price_per_second
            )
        return per_second * Decimal(int(duration_s)) * SAFETY_FACTOR

    def estimated_latency(self, tier: str) -> timedelta:
        """Observed wall time, so the Governor paces to the queue's real speed."""
        del tier
        return timedelta(seconds=self._latency_s)

    def _observe_latency(self, wall_s: float) -> None:
        if wall_s <= 0:
            return
        if self._observed == 0:
            self._latency_s = wall_s
        else:
            a = self.latency_smoothing
            self._latency_s = (a * wall_s) + ((1.0 - a) * self._latency_s)
        self._observed += 1

    async def health(self) -> BackendHealth:
        """No key means DOWN; that is the whole check.

        A live probe would make every start-up depend on the network, and the
        ladder already treats a failed generation as a reason to drop a rung.
        """
        if not self.api_key:
            return BackendHealth(BackendStatus.DOWN, "no FAL_KEY")
        return BackendHealth(BackendStatus.HEALTHY, f"api key present ({self.model_key})")

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
        del theme_hint
        if not self.api_key:
            raise RuntimeError(f"{self.name}: no API key configured")
        if extend_from is not None:
            raise RuntimeError(
                f"{self.name}: model {self.model_key!r} cannot continue a previous clip; "
                "run this zone in mosaic mode"
            )
        if seed_image is not None and not self.model.supports_image_seed:
            # Silently dropping the seed would break continuity handoff while
            # looking like it worked, so say so and let the caller retry clean.
            raise RuntimeError(
                f"{self.name}: model {self.model_key!r} takes no first-frame seed"
            )
        model = self.catalogue.get(tier, self.model)
        if duration_s not in model.allowed_durations_s:
            raise ValueError(
                f"{self.name}: duration {duration_s}s not in "
                f"{sorted(model.allowed_durations_s)} for {self.model_key}"
            )

        started = time.monotonic()
        status_url, result_url = await self._submit(model, prompt, duration_s)
        video_url = await self._poll(status_url, result_url)

        tmp_path = self.store.temp_path()
        try:
            await self._download(video_url, tmp_path)
            ref = await self.store.put(
                tmp_path,
                duration_s=float(duration_s),
                zone=zone,
                backend=self.name,
                tier=self.model_key,
            )
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        wall_s = time.monotonic() - started
        self._observe_latency(wall_s)
        log.info(
            "fal clip backend=%s model=%s zone=%s duration=%ds wall=%.1fs latency_est=%.1fs",
            self.name, self.model_key, zone, duration_s, wall_s, self._latency_s,
        )
        return ref

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- internals ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
            self._owns_client = True
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Key {self.api_key or ''}",
            "Content-Type": "application/json",
        }

    def _build_input(self, model: FalModel, prompt: str, duration_s: int) -> dict:
        body: dict = {
            "prompt": prompt,
            "duration": duration_s,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "seed": uuid.uuid4().int % (1 << 31),
        }
        body.update(model.extra_input)
        return body

    async def _submit(self, model: FalModel, prompt: str, duration_s: int) -> tuple[str, str]:
        """Queue a generation. Returns fal's own (status_url, response_url).

        Those URLs are taken from the reply rather than built from the model
        id, because fal does not put the whole id in them: submitting to
        ``minimax/h3-max/text-to-video`` yields status at
        ``minimax/h3-max/requests/{id}/status``. Constructing them here
        produced a 404 against the real service that no amount of mocking
        would have caught, since the mock built them the same wrong way.
        """
        log.info(
            "fal submit model=%s url=%s/%s", self.model_key, self.base_url, model.model_id
        )
        response = await self._http().post(
            f"{self.base_url}/{model.model_id}",
            json=self._build_input(model, prompt, duration_s),
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.name}: submit returned HTTP {response.status_code}"
            )
        body = response.json()
        request_id = body.get("request_id")
        if not request_id:
            raise RuntimeError(f"{self.name}: submit returned no request_id")
        status_url = body.get("status_url") or (
            f"{self.base_url}/{model.model_id}/requests/{request_id}/status"
        )
        response_url = body.get("response_url") or (
            f"{self.base_url}/{model.model_id}/requests/{request_id}"
        )
        return str(status_url), str(response_url)

    async def _poll(self, status_url: str, result_url: str) -> str:
        deadline = time.monotonic() + self.timeout_s
        while True:
            response = await self._http().get(status_url, headers=self._headers())
            if response.status_code < 400:
                body = response.json()
                if body.get("error"):
                    raise RuntimeError(
                        f"{self.name}: generation failed "
                        f"({body.get('error_type', 'unknown')})"
                    )
                if body.get("status") == STATUS_COMPLETED:
                    return await self._result(result_url)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.name}: no result after {self.timeout_s:.0f}s")
            await asyncio.sleep(self.poll_interval_s)

    async def _result(self, result_url: str) -> str:
        response = await self._http().get(result_url, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name}: result returned HTTP {response.status_code}")
        video = response.json().get("video") or {}
        url = video.get("url")
        if not url:
            raise RuntimeError(f"{self.name}: result carried no video url")
        return str(url)

    async def _download(self, url: str, dest: Path) -> None:
        # The media URL is pre-signed and public; sending the key to a CDN we
        # do not control would leak it for nothing.
        response = await self._http().get(url)
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name}: download returned HTTP {response.status_code}")
        if not response.content:
            raise RuntimeError(f"{self.name}: downloaded video was empty")
        await asyncio.to_thread(dest.write_bytes, response.content)
