"""ConductorState — the Conductor's only window onto the rest of the system
(Architecture §2.8).

The integration layer owns the actual pipeline (Weaver, Governor, Forge,
Loom, Listener); the Conductor never reaches into it. Instead the
integration layer pushes updates into this object -- a manifest here, a
feature frame there -- and the FastAPI app in :mod:`egregore.conductor.app`
only ever *reads* it. This is what keeps the Conductor content-blind by
construction: nothing in this file has a field for transcript text or
prompt strings, so there is no path by which either could arrive here.

Two independent fan-out buses live here:

* **Manifest bus** -- ``set_manifest`` bumps a per-zone revision counter and
  notifies ``/ws/manifest`` subscribers with ``{"revision": N}``. The client
  refetches ``GET /api/manifest`` on receipt; the socket carries no clip
  data itself.
* **Feature bus** -- ``publish_features``/``publish_mood`` fan frames out to
  every ``/ws/features`` subscriber for that zone at up to ~30 Hz. Each
  subscriber is a small bounded queue (maxsize 4); a slow consumer has its
  oldest queued frame dropped rather than backpressuring the publisher --
  losing a stale frame is invisible on screen, stalling the publish loop is
  not (Architecture §2.8: "audio reactivity is low-latency").
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path

from egregore.types import FeatureFrame, Manifest, MoodState

logger = logging.getLogger(__name__)

__all__ = ["ConductorState"]

# Bounded so a stalled WS consumer can never make the publisher wait; frames
# are cheap to regenerate at ~30 Hz so dropping the oldest is lossless enough
# to be invisible.
_FEATURE_QUEUE_MAXSIZE = 4
_MANIFEST_QUEUE_MAXSIZE = 8

ClipResolver = Callable[[str], "Path | None"]
StatusProvider = Callable[[], Awaitable[dict]]


def _drop_oldest_put(queue: asyncio.Queue, item: object) -> None:
    """``put_nowait``, evicting the oldest item first if the queue is full.

    Never raises ``QueueFull`` and never awaits -- safe to call from a
    synchronous fan-out loop with an arbitrary number of subscribers.
    """
    while True:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - racing consumer
                continue


class ConductorState:
    """Shared, in-process state the Conductor app views but never mutates
    on its own initiative.

    Args:
        clip_resolver: maps a clip id to its file path, or ``None`` if the
            id is unknown. Owned by Forge's clip store; the Conductor only
            calls it.
        zone_config: per-zone passthrough config for ``/api/config``, e.g.
            ``{"main": {"lens_stack": [...], "screens": {"screen1": {...}}}}``.
            Plain dicts, supplied once at construction -- the Conductor does
            not interpret or validate their contents beyond the keys it
            reads back out in :meth:`get_config`.
        status_provider: async callable returning the operator dashboard
            dict for ``GET /api/status``. May be set later (the integration
            layer typically isn't ready until wiring completes); ``None``
            until then, in which case the route reports an empty status.
    """

    def __init__(
        self,
        *,
        clip_resolver: ClipResolver,
        zone_config: dict[str, dict] | None = None,
        status_provider: StatusProvider | None = None,
    ) -> None:
        self.clip_resolver = clip_resolver
        self.zone_config: dict[str, dict] = dict(zone_config or {})
        self.status_provider = status_provider

        self._manifests: dict[str, Manifest] = {}
        self._latest_frame: dict[str, FeatureFrame] = {}
        self._latest_mood: dict[str, MoodState] = {}

        self._feature_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._manifest_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

        self._screens_connected: dict[str, int] = defaultdict(int)

    # -- manifest bus ---------------------------------------------------

    def get_manifest(self, zone: str) -> Manifest | None:
        return self._manifests.get(zone)

    def set_manifest(self, zone: str, manifest: Manifest) -> Manifest:
        """Install ``manifest`` as the current one for ``zone``.

        The stored revision always increments from the previous one for
        this zone (starting at 1), regardless of whatever ``revision`` the
        caller put on the incoming object -- monotonic revision numbering is
        this object's guarantee, not the caller's. Notifies every
        ``/ws/manifest`` subscriber for the zone with the new revision.
        """
        previous = self._manifests.get(zone)
        manifest.zone = zone
        manifest.revision = (previous.revision + 1) if previous is not None else 1
        self._manifests[zone] = manifest
        self._notify_manifest_subs(zone, manifest.revision)
        logger.debug("conductor[%s]: manifest set, revision=%d", zone, manifest.revision)
        return manifest

    def subscribe_manifest(self, zone: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_MANIFEST_QUEUE_MAXSIZE)
        self._manifest_subs[zone].add(queue)
        return queue

    def unsubscribe_manifest(self, zone: str, queue: asyncio.Queue) -> None:
        self._manifest_subs[zone].discard(queue)

    def _notify_manifest_subs(self, zone: str, revision: int) -> None:
        message = {"type": "manifest", "revision": revision}
        for queue in self._manifest_subs.get(zone, ()):
            _drop_oldest_put(queue, message)

    # -- feature bus ------------------------------------------------------

    async def publish_features(self, zone: str, frame: FeatureFrame) -> None:
        """Fan ``frame`` out to every ``/ws/features`` subscriber for ``zone``.

        Never awaits on a subscriber; a full queue has its oldest frame
        dropped instead, so one slow client can never delay this call.
        """
        self._latest_frame[zone] = frame
        for queue in self._feature_subs.get(zone, ()):
            _drop_oldest_put(queue, {"type": "features", **frame.as_wire()})

    async def publish_mood(self, zone: str, mood: MoodState) -> None:
        """Fan ``mood`` out to every ``/ws/features`` subscriber for ``zone``."""
        self._latest_mood[zone] = mood
        for queue in self._feature_subs.get(zone, ()):
            _drop_oldest_put(queue, {"type": "mood", **mood.as_wire()})

    def latest_frame(self, zone: str) -> FeatureFrame | None:
        return self._latest_frame.get(zone)

    def latest_mood(self, zone: str) -> MoodState | None:
        return self._latest_mood.get(zone)

    def subscribe_features(self, zone: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_FEATURE_QUEUE_MAXSIZE)
        self._feature_subs[zone].add(queue)
        return queue

    def unsubscribe_features(self, zone: str, queue: asyncio.Queue) -> None:
        self._feature_subs[zone].discard(queue)

    # -- screen config passthrough ----------------------------------------

    def get_config(self, zone: str, screen: str | None = None) -> dict | None:
        """What a Lens client needs to configure itself for ``zone`` (and
        optionally a specific ``screen``), or ``None`` if the zone is unknown.

        A screen's ``lens_stack``/``loop_phase_offset``/``audio_source``
        override the zone default when present in ``zone_config``; otherwise
        the zone-level value applies. ``crossfade_s`` comes from the zone's
        current manifest when one has been set, else a documented default.
        """
        zone_cfg = self.zone_config.get(zone)
        if zone_cfg is None:
            return None
        screens = zone_cfg.get("screens", {})
        screen_cfg = screens.get(screen, {}) if screen else {}

        manifest = self._manifests.get(zone)
        crossfade_s = manifest.crossfade_s if manifest is not None else 2.0

        return {
            "zone": zone,
            "screen": screen,
            "lens_stack": screen_cfg.get("lens_stack", zone_cfg.get("lens_stack", [])),
            "loop_phase_offset": screen_cfg.get("loop_phase_offset", 0.0),
            "audio_source": screen_cfg.get("audio_source", "zone"),
            "crossfade_s": crossfade_s,
        }

    # -- screens connected --------------------------------------------------

    def connect_screen(self, zone: str) -> int:
        """Record a screen joining ``zone``'s feature/manifest feed. Returns
        the new per-zone count."""
        self._screens_connected[zone] += 1
        return self._screens_connected[zone]

    def disconnect_screen(self, zone: str) -> int:
        """Record a screen leaving. Floored at zero -- disconnects are never
        double-counted into the negatives."""
        self._screens_connected[zone] = max(0, self._screens_connected[zone] - 1)
        return self._screens_connected[zone]

    def screens_connected_for(self, zone: str) -> int:
        return self._screens_connected.get(zone, 0)

    @property
    def screens_connected(self) -> int:
        """Total screens connected across all zones."""
        return sum(self._screens_connected.values())

    def screens_connected_by_zone(self) -> dict[str, int]:
        return dict(self._screens_connected)

    # -- clips ---------------------------------------------------------

    def resolve_clip(self, clip_id: str) -> Path | None:
        return self.clip_resolver(clip_id)

    def __repr__(self) -> str:  # pragma: no cover - operator convenience
        return (
            f"ConductorState(zones={list(self._manifests)}, "
            f"screens_connected={self.screens_connected})"
        )
