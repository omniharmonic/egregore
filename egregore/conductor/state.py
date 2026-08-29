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

from egregore.conductor.nodes import NodeRegistry
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
#: (action, payload) -> result dict. Raises ValueError for a bad action or
#: payload; the route turns that into a 400.
ControlHandler = Callable[[str, dict], Awaitable[dict]]


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
        control_handler: ControlHandler | None = None,
    ) -> None:
        self.clip_resolver = clip_resolver
        self.zone_config: dict[str, dict] = dict(zone_config or {})
        self.status_provider = status_provider
        #: Operator control actions (freeze/mute/mode). Bound by the
        #: integration layer; None means the deployment exposes no controls.
        self.control_handler = control_handler
        #: Bound by the integration layer. Receives a settings change and
        #: applies the live subset to the running party, returning what it
        #: actually changed. ``None`` means this deployment exposes no
        #: runtime settings.
        self.settings_handler: Callable[[dict], dict] | None = None
        #: Per-zone live settings the pipeline owns (selection weights).
        self.zone_settings_handler: Callable[[str, dict], None] | None = None
        #: The config the party is actually running, as plain JSON, so the
        #: settings page can show effective values next to the overrides.
        self.effective_config: dict | None = None
        #: Bumped whenever a zone's client config changes, so a screen
        #: can tell a genuine change from a reconnect.
        self._config_revision: dict[str, int] = {}
        #: Devices that enrolled over the network (spec: multi-node party).
        self.nodes = NodeRegistry()
        #: Bound by the integration layer: (zone, node_id, pcm, sample_rate).
        #: ``None`` means this deployment accepts no network audio.
        self.ingest_handler: (
            Callable[[str, str, bytes, int], Awaitable[None]] | None
        ) = None
        #: When set, every zone is served this zone's manifest — the "mirror"
        #: topology. Screens keep their own loop_phase_offset, so the walls
        #: stay in step without being identical.
        self.mirror_zone: str | None = None
        #: zone -> the input device actually opened, by name. The config only
        #: records what *kind* of source a zone uses.
        self.input_devices: dict[str, str] = {}
        #: Bound by the integration layer ONLY when the operator asked for it.
        #: Returns the live transcript window and recent prompts, so a room
        #: can be watched while it is being listened to. Off by default: this
        #: is the one surface that can show transcript text, and PRD 6.8 says
        #: that text otherwise never leaves the ring buffer.
        self.monitor_provider: Callable[[], dict] | None = None

        self._manifests: dict[str, Manifest] = {}
        self._latest_frame: dict[str, FeatureFrame] = {}
        self._latest_mood: dict[str, MoodState] = {}

        self._feature_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._manifest_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

        self._screens_connected: dict[str, int] = defaultdict(int)

    # -- manifest bus ---------------------------------------------------

    def get_manifest(self, zone: str) -> Manifest | None:
        """The manifest a screen in ``zone`` should play.

        Under the "mirror" topology every zone resolves to one generating
        zone, which is what makes a venue read as a single organism without a
        second generation stream per room.
        """
        if self.mirror_zone is not None:
            zone = self.mirror_zone
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

    # -- live zone configuration ------------------------------------------

    def set_zone_config(self, zone: str, patch: dict) -> dict:
        """Merge ``patch`` into a zone's client config and tell its screens.

        Screens read ``/api/config`` once at boot, so a change made while a
        party is running would otherwise not reach them until someone
        reloaded every display in the room. The notice rides the manifest
        socket the screens already hold open, and carries a revision so a
        client can ignore one it has already applied.

        Raises ``KeyError`` for an unknown zone.
        """
        if zone not in self.zone_config:
            raise KeyError(zone)
        self.zone_config[zone].update(patch)
        self._config_revision[zone] = self._config_revision.get(zone, 0) + 1
        message = {"type": "config", "revision": self._config_revision[zone]}
        for queue in self._manifest_subs.get(zone, ()):
            _drop_oldest_put(queue, message)
        logger.info(
            "conductor[%s]: zone config updated (%s), revision=%d",
            zone, ", ".join(sorted(patch)), self._config_revision[zone],
        )
        return self.get_config(zone) or {}

    def config_revision(self, zone: str) -> int:
        return self._config_revision.get(zone, 0)

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
        override = zone_cfg.get("crossfade_override")
        if isinstance(override, int | float) and override > 0:
            crossfade_s = float(override)

        def inherit(key: str, default):
            """Screen value, else zone value, else default.

            A screen entry carries every key with an explicit ``None`` when it
            has no override, and ``dict.get(key, fallback)`` returns that
            ``None`` rather than falling through. That made every *named*
            screen report ``lens_stack: null``, so the client dropped to its
            built-in default and a whole venue wore the same three lenses no
            matter what its zones were configured with.
            """
            value = screen_cfg.get(key)
            if value is None:
                value = zone_cfg.get(key)
            return default if value is None else value

        return {
            "zone": zone,
            "screen": screen,
            "lens_stack": inherit("lens_stack", []),
            "lens_params": inherit("lens_params", {}),
            # Pacing. Below 1 the motion is languid and each clip holds the
            # screen longer, which is what separates a loop that pulses from
            # one that flickers past.
            "playback_rate": inherit("playback_rate", 1.0),
            "loop_phase_offset": inherit("loop_phase_offset", 0.0),
            # Screen overrides zone, zone overrides the default — the same
            # precedence lens_stack uses. Reading this from the screen alone
            # made a zone-level audio_source silently inert.
            "audio_source": inherit("audio_source", "zone"),
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
