"""Headless ComfyUI backend for local LTX-2 generation (Architecture §2.6).

Zero marginal cost (PRD V-4, B-6), no native extension: continuity on this
rung is entirely last-frame image-to-video seeding (Architecture §3.2).

The workflow JSON below is a plausible minimal LTX-2 text-to-video graph, and
it is a *default*, not a contract. Every real ComfyUI install has its own node
versions, checkpoint filenames and output nodes, so the operator is expected
to export their own working graph in API format and pass it as `workflow=`.
The patch points are located by `class_type` rather than by node id so an
operator-supplied graph keeps working as long as it uses the same node types.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
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

DEFAULT_BASE_URL = "http://127.0.0.1:8188"
TIER_LTX2 = "ltx-2"
_TIERS = frozenset({TIER_LTX2})
_ALLOWED_DURATIONS = frozenset({4, 6, 8})
FPS = 24

#: Minimal LTX-2 text-to-video graph in ComfyUI API format. Operator-overridable.
DEFAULT_WORKFLOW: dict = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "ltx-2-video.safetensors"},
    },
    "2": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["1", 1]},
        "_meta": {"title": "positive"},
    },
    "3": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "text, watermark, logo, human faces, letters", "clip": ["1", 1]},
        "_meta": {"title": "negative"},
    },
    "4": {
        "class_type": "EmptyLTXVLatentVideo",
        "inputs": {"width": 1280, "height": 704, "length": 97, "batch_size": 1},
    },
    "5": {
        "class_type": "LTXVConditioning",
        "inputs": {"positive": ["2", 0], "negative": ["3", 0], "frame_rate": float(FPS)},
    },
    "6": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 30,
            "cfg": 3.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["1", 0],
            "positive": ["5", 0],
            "negative": ["5", 1],
            "latent_image": ["4", 0],
        },
    },
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
    "8": {
        "class_type": "VHS_VideoCombine",
        "inputs": {
            "images": ["7", 0],
            "frame_rate": FPS,
            "format": "video/h264-mp4",
            "filename_prefix": "egregore",
            "save_output": True,
        },
    },
}

#: Keys ComfyUI uses in a history entry's `outputs` for file-producing nodes.
_OUTPUT_KEYS = ("gifs", "videos", "images")


class ComfyUIBackend:
    """Local LTX-2 `VideoBackend` driven through the ComfyUI HTTP API."""

    def __init__(
        self,
        store: ClipStore,
        *,
        base_url: str = DEFAULT_BASE_URL,
        workflow: dict | None = None,
        client: httpx.AsyncClient | None = None,
        name: str = "local",
        poll_interval_s: float = 2.0,
        timeout_s: float = 900.0,
        health_timeout_s: float = 2.0,
        initial_latency_s: float = 60.0,
        latency_smoothing: float = 0.3,
        seed_workflow: dict | None = None,
    ) -> None:
        self.name = name
        # Seed for estimated_latency() until real timings arrive; every
        # completed generation moves it toward what this box actually does.
        self.initial_latency_s = float(initial_latency_s)
        self.latency_smoothing = float(latency_smoothing)
        self._latency_s = float(initial_latency_s)
        self._observed = 0
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.workflow = copy.deepcopy(workflow) if workflow is not None else copy.deepcopy(
            DEFAULT_WORKFLOW
        )
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.health_timeout_s = health_timeout_s
        self.client_id = uuid.uuid4().hex

        self._client = client
        self._owns_client = client is None
        #: Prompt ids we have queued and not yet seen finish. ComfyUI's queue
        #: lives in the server, so without this a restart leaves this party's
        #: renders running — four orphaned twelve-minute jobs ahead of a live
        #: one is indistinguishable from local video simply never appearing.
        self._inflight: set[str] = set()
        #: Graph used when a clip is seeded from the previous one's last
        #: frame. Without one this backend cannot honour a seed, and says so
        #: in its capabilities rather than accepting the bytes and dropping
        #: them — which is what it used to do.
        self.seed_workflow = (
            copy.deepcopy(seed_workflow) if seed_workflow is not None else None
        )

    # -- protocol ----------------------------------------------------------

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            allowed_durations_s=_ALLOWED_DURATIONS,
            supports_native_extend=False,
            supports_image_seed=self.seed_workflow is not None,
            tiers=_TIERS,
            max_chain_length=0,
        )

    def max_plausible_cost(self, duration_s: int, tier: str) -> Decimal:
        return Decimal("0")

    def estimated_latency(self, tier: str) -> timedelta:
        """What this box actually takes to render a clip, as observed.

        The same graph is minutes on a laptop's integrated GPU and seconds on a
        datacentre card, so a hardcoded constant is wrong everywhere. Each
        completed generation folds its wall time into an EWMA; until the first
        one lands this returns ``initial_latency_s``. The Governor paces the
        generation loop on this, which is what lets one config behave sanely on
        very different hardware (Architecture §2.6).
        """
        del tier  # one tier today; latency is a property of the box, not the tier
        return timedelta(seconds=self._latency_s)

    def _observe_latency(self, wall_s: float) -> None:
        """Fold one observed render time into the EWMA."""
        if wall_s <= 0:
            return
        if self._observed == 0:
            self._latency_s = wall_s
        else:
            a = self.latency_smoothing
            self._latency_s = (a * wall_s) + ((1.0 - a) * self._latency_s)
        self._observed += 1

    async def health(self) -> BackendHealth:
        try:
            response = await self._http().get(
                f"{self.base_url}/system_stats", timeout=self.health_timeout_s
            )
        except httpx.HTTPError as exc:
            return BackendHealth(BackendStatus.DOWN, type(exc).__name__)
        if response.status_code >= 400:
            return BackendHealth(BackendStatus.DOWN, f"HTTP {response.status_code}")
        return BackendHealth(BackendStatus.HEALTHY, "comfyui reachable")

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
        if duration_s not in _ALLOWED_DURATIONS:
            raise ValueError(
                f"{self.name}: duration {duration_s}s not in {sorted(_ALLOWED_DURATIONS)}"
            )

        started = time.monotonic()
        seed_name = None
        if seed_image is not None and self.seed_workflow is not None:
            seed_name = await self._upload_seed(seed_image)
        elif seed_image is not None:
            # Better to say so than to accept the bytes and quietly render an
            # unrelated clip into what the Loom believes is a continuous
            # movement.
            log.warning(
                "%s: a seed frame was offered but no seeded graph is configured; "
                "rendering unseeded", self.name,
            )
        workflow = self._patched_workflow(prompt, duration_s, seed_name)
        prompt_id = await self._submit(workflow)
        self._inflight.add(prompt_id)
        try:
            outputs = await self._poll(prompt_id)
        finally:
            self._inflight.discard(prompt_id)
        descriptor = _first_output(outputs)

        tmp_path = self.store.temp_path()
        try:
            await self._download(descriptor, tmp_path)
            ref = await self.store.put(
                tmp_path,
                duration_s=float(duration_s),
                zone=zone,
                backend=self.name,
                tier=TIER_LTX2,
            )
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        wall_s = time.monotonic() - started
        self._observe_latency(wall_s)
        log.info(
            "local clip backend=%s zone=%s duration=%ds wall=%.1fs latency_est=%.1fs",
            self.name,
            zone,
            duration_s,
            wall_s,
            self._latency_s,
        )
        return ref

    async def close(self) -> None:
        """Cancel anything still queued, then release the client.

        ComfyUI keeps its queue server-side, so work outlives the party that
        asked for it. Leaving it running means the next party waits behind
        renders nobody is going to watch.
        """
        await self.cancel_inflight()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def cancel_inflight(self) -> None:
        """Drop this backend's queued prompts and interrupt the running one."""
        if not self._inflight:
            return
        pending = sorted(self._inflight)
        self._inflight.clear()
        try:
            await self._http().post(
                f"{self.base_url}/queue", json={"delete": pending}, timeout=5.0
            )
            await self._http().post(f"{self.base_url}/interrupt", timeout=5.0)
        except httpx.HTTPError as exc:
            # A ComfyUI that is already gone is the common case here, and it
            # has taken the queue with it.
            log.debug("could not cancel %d queued prompt(s): %s", len(pending), exc)
        else:
            log.info("cancelled %d queued ComfyUI prompt(s)", len(pending))

    # -- internals ---------------------------------------------------------

    async def _upload_seed(self, png: bytes) -> str:
        """Put a still where ComfyUI's LoadImage can find it, return its name."""
        name = f"egregore-seed-{uuid.uuid4().hex[:12]}.png"
        response = await self._http().post(
            f"{self.base_url}/upload/image",
            files={"image": (name, png, "image/png")},
            data={"overwrite": "true", "type": "input"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.name}: seed upload returned HTTP {response.status_code}"
            )
        # ComfyUI may rename on collision; believe what it says it stored.
        return str(response.json().get("name") or name)

    def _patched_workflow(self, prompt: str, duration_s: int,
                          seed_name: str | None = None) -> dict:
        """Copy the template and patch in the prompt, length and a seed.

        Nodes are found by `class_type` (and `_meta.title` for the positive
        conditioning) so an operator-supplied graph works unchanged.
        """
        base = self.workflow if seed_name is None else self.seed_workflow
        workflow = copy.deepcopy(base)
        # LTX-2 latent length is in frames and wants 8n+1.
        frames = duration_s * FPS + 1

        positive_patched = False
        for node in workflow.values():
            class_type = node.get("class_type")
            inputs = node.setdefault("inputs", {})
            if class_type == "CLIPTextEncode":
                title = str(node.get("_meta", {}).get("title", "")).lower()
                if not positive_patched and "negative" not in title:
                    inputs["text"] = prompt
                    positive_patched = True
            elif class_type in (
                "EmptyLTXVLatentVideo", "EmptyLatentVideo", "LTXVImgToVideo"
            ):
                inputs["length"] = frames
            elif class_type == "LoadImage" and seed_name is not None:
                inputs["image"] = seed_name
            elif class_type == "KSampler":
                inputs["seed"] = uuid.uuid4().int % (1 << 32)

        if not positive_patched:
            raise RuntimeError(
                f"{self.name}: workflow has no CLIPTextEncode node to carry the prompt"
            )
        return workflow

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self._client

    async def _submit(self, workflow: dict) -> str:
        response = await self._http().post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name}: /prompt returned HTTP {response.status_code}")
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"{self.name}: /prompt returned no prompt_id")
        return str(prompt_id)

    async def _poll(self, prompt_id: str) -> dict:
        url = f"{self.base_url}/history/{prompt_id}"
        deadline = time.monotonic() + self.timeout_s
        while True:
            response = await self._http().get(url)
            if response.status_code < 400:
                entry = response.json().get(prompt_id)
                if entry:
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise RuntimeError(f"{self.name}: workflow execution failed")
                    outputs = entry.get("outputs") or {}
                    if outputs:
                        return outputs
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.name}: no output after {self.timeout_s:.0f}s")
            await asyncio.sleep(self.poll_interval_s)

    async def _download(self, descriptor: dict, dest: Path) -> None:
        params = {
            "filename": descriptor.get("filename", ""),
            "subfolder": descriptor.get("subfolder", ""),
            "type": descriptor.get("type", "output"),
        }
        response = await self._http().get(f"{self.base_url}/view", params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name}: /view returned HTTP {response.status_code}")
        if not response.content:
            raise RuntimeError(f"{self.name}: downloaded video was empty")
        await asyncio.to_thread(dest.write_bytes, response.content)


def _first_output(outputs: dict) -> dict:
    """First file descriptor in a ComfyUI history `outputs` block."""
    for node_output in outputs.values():
        for key in _OUTPUT_KEYS:
            items = node_output.get(key)
            if items:
                return dict(items[0])
    raise RuntimeError("local: workflow produced no video output")


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_WORKFLOW", "TIER_LTX2", "ComfyUIBackend"]
