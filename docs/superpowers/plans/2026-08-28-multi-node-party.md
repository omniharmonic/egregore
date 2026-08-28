# Multi-node Party Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anyone on the party wifi opens an address, enrolls their phone as a microphone, a screen, or both, and is part of the system in ten seconds.

**Architecture:** A `NodeRegistry` holds who is connected. A `NetworkSource` implements the same `ZoneEvents` contract `MicSource` does, but is fed decoded PCM from a `/ws/ingest` WebSocket instead of an audio device — so nothing downstream knows the audio arrived over a socket. Many nodes per zone merge into one feature stream and one ring buffer. Three topologies decide whether zones dream separately, share a transcript pool, or mirror one loop.

**Tech Stack:** Python 3.11, FastAPI WebSockets, numpy, pydantic v2, vanilla JS (getUserMedia + AudioWorklet), pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-multi-node-party-design.md`

## Global Constraints

- Python 3.11, type-annotated, ruff-clean (`line-length = 100`).
- Modules import only from `egregore.types`, `egregore.config.schema`, stdlib and declared deps. `NetworkSource` must not import FastAPI — the conductor hands it decoded bytes.
- Transcript text lives only in the ring buffer and weaver stage 1. Never logged, never on disk, never in an exception message. `tests/test_privacy.py` keeps passing.
- Audio may cross the LAN; it may never reach the cloud. Transmitters gate locally so silence is never sent.
- Ingest sockets authenticate exactly as `/ws/features` does — `ws_authorized(password, cookie)`, close `4401` before accept when it fails.
- A muted node's audio is dropped at the **server**, not merely hidden in the UI.
- Existing screen URLs (`/?zone=main`) keep working.
- Every task ends with `uv run ruff check . && uv run pytest -q` green.

---

### Task 1: NodeRegistry

**Files:**
- Create: `egregore/conductor/nodes.py`
- Create: `tests/test_nodes.py`

**Interfaces:**
- Produces: `Node` dataclass (`id, label, zone, role, muted, enrolled_at, last_seen, level`), `NodeRegistry` with `enroll(node_id, *, label, zone, role) -> Node`, `heartbeat(node_id, level=None) -> Node | None`, `get(node_id) -> Node | None`, `all() -> list[Node]`, `transmitters(zone) -> list[Node]`, `mute(node_id, on) -> Node | None`, `kick(node_id) -> bool`, `expire(now=None) -> list[str]`, `ROLES`.

- [ ] **Step 1: Write the failing tests**

```python
"""Node registry — who is connected, and who is allowed to be heard."""

from __future__ import annotations

import pytest

from egregore.conductor.nodes import ROLES, NodeRegistry


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_enroll_is_idempotent_per_id():
    # A phone that reloads must rejoin, not appear twice.
    reg = NodeRegistry(clock=FakeClock())
    a = reg.enroll("n1", label="phone", zone="kitchen", role="transmit")
    b = reg.enroll("n1", label="phone renamed", zone="garden", role="both")
    assert len(reg.all()) == 1
    assert a.id == b.id
    assert b.label == "phone renamed" and b.zone == "garden" and b.role == "both"


def test_enroll_rejects_an_unknown_role():
    reg = NodeRegistry(clock=FakeClock())
    with pytest.raises(ValueError, match="role"):
        reg.enroll("n1", label="x", zone="main", role="overlord")
    assert set(ROLES) == {"transmit", "receive", "both"}


def test_heartbeat_keeps_a_node_live_and_records_its_level():
    clock = FakeClock()
    reg = NodeRegistry(clock=clock, ttl_s=30.0)
    reg.enroll("n1", label="phone", zone="k", role="transmit")
    clock.t += 20
    assert reg.heartbeat("n1", level=0.42) is not None
    clock.t += 20
    assert reg.expire() == [], "a node that keeps beating must not expire"
    assert reg.get("n1").level == pytest.approx(0.42)


def test_a_silent_node_expires():
    clock = FakeClock()
    reg = NodeRegistry(clock=clock, ttl_s=30.0)
    reg.enroll("n1", label="phone", zone="k", role="transmit")
    clock.t += 31
    assert reg.expire() == ["n1"]
    assert reg.get("n1") is None


def test_heartbeat_for_an_unknown_node_is_not_an_error():
    # A node that survived a server restart should be told to re-enroll,
    # not crash the socket handling its audio.
    reg = NodeRegistry(clock=FakeClock())
    assert reg.heartbeat("ghost") is None


def test_transmitters_excludes_receivers_and_muted_nodes():
    reg = NodeRegistry(clock=FakeClock())
    reg.enroll("t", label="t", zone="k", role="transmit")
    reg.enroll("b", label="b", zone="k", role="both")
    reg.enroll("r", label="r", zone="k", role="receive")
    reg.enroll("elsewhere", label="e", zone="garden", role="transmit")
    assert {n.id for n in reg.transmitters("k")} == {"t", "b"}

    reg.mute("b", True)
    assert {n.id for n in reg.transmitters("k")} == {"t"}
    reg.mute("b", False)
    assert {n.id for n in reg.transmitters("k")} == {"t", "b"}


def test_kick_removes_a_node():
    reg = NodeRegistry(clock=FakeClock())
    reg.enroll("n1", label="x", zone="k", role="both")
    assert reg.kick("n1") is True
    assert reg.kick("n1") is False
    assert reg.all() == []


def test_wire_shape_is_json_safe_and_carries_no_audio():
    reg = NodeRegistry(clock=FakeClock())
    reg.enroll("n1", label="phone", zone="k", role="transmit")
    reg.heartbeat("n1", level=0.5)
    row = reg.all()[0].as_wire()
    import json
    json.dumps(row)
    assert set(row) == {
        "id", "label", "zone", "role", "muted", "level", "enrolled_at", "last_seen"
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_nodes.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'egregore.conductor.nodes'`

- [ ] **Step 3: Write the implementation**

```python
"""Who is connected to the party (spec: multi-node party).

A *node* is a browser that enrolled: a phone acting as a microphone, a screen
showing the dream, or both. The registry is pure state with an injectable
clock — no I/O, no FastAPI — so the expiry and muting rules can be tested
without a server or a wall-clock sleep.

Enrollment is deliberately open: asking someone to type a password at the
moment they walk up with a phone defeats the point. Control is exercised
afterwards instead, which is why :meth:`mute` and :meth:`kick` exist and why
:meth:`transmitters` is the only thing the audio path is allowed to consult.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["Node", "NodeRegistry", "ROLES"]

#: What a device says it is here to do.
ROLES = ("transmit", "receive", "both")

#: A node is dropped after this long without a heartbeat. Long enough to ride
#: out a phone locking its screen mid-song, short enough that the operator's
#: list reflects the room.
DEFAULT_TTL_S = 45.0


@dataclass
class Node:
    id: str
    label: str
    zone: str
    role: str
    enrolled_at: float
    last_seen: float
    muted: bool = False
    #: Most recent input level, purely so the operator can see a node is alive
    #: and roughly how loud it is. Never any audio, never any text.
    level: float = 0.0

    @property
    def transmits(self) -> bool:
        return self.role in ("transmit", "both") and not self.muted

    def as_wire(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "zone": self.zone,
            "role": self.role,
            "muted": self.muted,
            "level": round(self.level, 4),
            "enrolled_at": self.enrolled_at,
            "last_seen": self.last_seen,
        }


class NodeRegistry:
    """Enrolled devices, keyed by the id their browser generated."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self._clock = clock
        self.ttl_s = float(ttl_s)
        self._nodes: dict[str, Node] = {}

    def enroll(self, node_id: str, *, label: str, zone: str, role: str) -> Node:
        """Register or update a node. Idempotent per id, so a phone that
        reloads rejoins rather than appearing twice."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        node_id = str(node_id).strip()
        if not node_id:
            raise ValueError("node id must not be empty")
        now = self._clock()
        existing = self._nodes.get(node_id)
        if existing is None:
            node = Node(
                id=node_id, label=label, zone=zone, role=role,
                enrolled_at=now, last_seen=now,
            )
            self._nodes[node_id] = node
            logger.info("node enrolled id=%s zone=%s role=%s", node_id, zone, role)
            return node
        existing.label = label
        existing.zone = zone
        existing.role = role
        existing.last_seen = now
        return existing

    def heartbeat(self, node_id: str, level: float | None = None) -> Node | None:
        """Mark a node alive. ``None`` for an unknown id — a node that
        outlived a server restart should be told to re-enroll, not raise
        inside the socket carrying its audio."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.last_seen = self._clock()
        if level is not None:
            node.level = float(level)
        return node

    def get(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def all(self) -> list[Node]:
        return sorted(self._nodes.values(), key=lambda n: (n.zone, n.label, n.id))

    def transmitters(self, zone: str) -> list[Node]:
        """Nodes whose audio this zone may use: right zone, right role, not
        muted. The audio path consults this and nothing else."""
        return [n for n in self.all() if n.zone == zone and n.transmits]

    def mute(self, node_id: str, on: bool) -> Node | None:
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.muted = bool(on)
        logger.info("node %s id=%s", "muted" if on else "unmuted", node_id)
        return node

    def kick(self, node_id: str) -> bool:
        removed = self._nodes.pop(node_id, None) is not None
        if removed:
            logger.info("node kicked id=%s", node_id)
        return removed

    def expire(self, now: float | None = None) -> list[str]:
        """Drop nodes that stopped beating. Returns the ids removed."""
        current = self._clock() if now is None else now
        stale = [n.id for n in self._nodes.values() if current - n.last_seen > self.ttl_s]
        for node_id in stale:
            self._nodes.pop(node_id, None)
        if stale:
            logger.info("nodes expired: %s", ", ".join(stale))
        return stale
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_nodes.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add egregore/conductor/nodes.py tests/test_nodes.py
git commit -m "Add a node registry for devices that enroll over the network"
```

---

### Task 2: NetworkSource and the per-zone feature merge

**Files:**
- Create: `egregore/listener/network.py`
- Modify: `egregore/listener/__init__.py`
- Create: `tests/test_network_source.py`

**Interfaces:**
- Consumes: `ZoneEvents`, `compute_features(pcm: np.ndarray, sample_rate: int, prev_rms: float) -> FeatureFrame`, `make_gate()` from `egregore.listener.vad`.
- Produces: `NetworkSource(events, *, zone, sample_rate=16000, gate=None, merge_window_s=2.0, clock=time.monotonic)` with `async feed(node_id: str, pcm: bytes, sample_rate: int) -> None`, `async run() -> None`, `stop() -> None`, `active_nodes() -> list[str]`, and `merge_frames(frames) -> FeatureFrame`.

- [ ] **Step 1: Write the failing tests**

```python
"""NetworkSource — audio that arrives over a socket instead of a device."""

from __future__ import annotations

import math
import struct

import pytest

from egregore.listener.network import NetworkSource, merge_frames
from egregore.listener.sources import ZoneEvents
from egregore.types import FeatureFrame


def pcm(amplitude: float, samples: int = 800, rate: int = 16000, freq: float = 220.0) -> bytes:
    """Mono 16-bit PCM of a sine at ``amplitude`` (0..1)."""
    out = bytearray()
    for i in range(samples):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<h", v)
    return bytes(out)


class Collect:
    def __init__(self):
        self.features: list[FeatureFrame] = []
        self.audio: list[tuple[bytes, int]] = []
        self.text: list[str] = []

    def events(self) -> ZoneEvents:
        async def on_features(f): self.features.append(f)
        async def on_text(t): self.text.append(t)
        async def on_audio(p, sr): self.audio.append((p, sr))
        return ZoneEvents(on_features=on_features, on_speech_text=on_text,
                          on_speech_audio=on_audio)


class AlwaysSpeech:
    name = "always"
    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool: return True


class NeverSpeech:
    name = "never"
    def is_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool: return False


async def test_pcm_in_produces_a_feature_frame_out():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", pcm(0.5), 16000)
    assert len(c.features) == 1
    assert c.features[0].rms > 0


async def test_silence_still_produces_features_but_no_speech():
    # The feature path is never gated (Architecture 2.1); the speech path is.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", pcm(0.0), 16000)
    assert len(c.features) == 1
    assert c.audio == []


async def test_gated_speech_reaches_the_scribe_with_its_sample_rate():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=AlwaysSpeech())
    payload = pcm(0.6)
    await src.feed("n1", payload, 16000)
    assert c.audio == [(payload, 16000)]


async def test_two_nodes_in_one_zone_both_reach_the_scribe():
    # A room with two phones must hear the conversation, not one person.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=AlwaysSpeech())
    await src.feed("n1", pcm(0.4), 16000)
    await src.feed("n2", pcm(0.5), 16000)
    assert len(c.audio) == 2
    assert set(src.active_nodes()) == {"n1", "n2"}


def test_merge_takes_the_per_field_max():
    # The energy of a room is the loudest thing in it.
    a = FeatureFrame(t=1.0, rms=0.2, low=0.9, mid=0.1, high=0.1, centroid=0.3, onset=0.0)
    b = FeatureFrame(t=2.0, rms=0.8, low=0.1, mid=0.4, high=0.2, centroid=0.7, onset=0.5)
    m = merge_frames([a, b])
    assert m.rms == pytest.approx(0.8)
    assert m.low == pytest.approx(0.9)
    assert m.onset == pytest.approx(0.5)
    assert m.t == pytest.approx(2.0), "timestamp comes from the newest frame"


def test_merge_of_one_frame_is_that_frame():
    a = FeatureFrame(t=1.0, rms=0.2, low=0.9, mid=0.1, high=0.1, centroid=0.3, onset=0.0)
    assert merge_frames([a]).rms == pytest.approx(0.2)


async def test_a_node_that_goes_quiet_ages_out_of_the_merge():
    clock = {"t": 1000.0}
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech(),
                        merge_window_s=2.0, clock=lambda: clock["t"])
    await src.feed("loud", pcm(0.9), 16000)
    clock["t"] += 5                      # 'loud' is now stale
    await src.feed("quiet", pcm(0.05), 16000)
    assert src.active_nodes() == ["quiet"]
    # The merged frame must reflect the quiet node alone, not a stale peak.
    assert c.features[-1].rms < 0.3


async def test_odd_length_payload_is_refused_rather_than_misread():
    # A truncated frame would otherwise be decoded one byte out of phase and
    # produce plausible-looking garbage.
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    with pytest.raises(ValueError, match="16-bit"):
        await src.feed("n1", b"\x01\x02\x03", 16000)


async def test_empty_payload_is_ignored():
    c = Collect()
    src = NetworkSource(c.events(), zone="k", gate=NeverSpeech())
    await src.feed("n1", b"", 16000)
    assert c.features == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_network_source.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'egregore.listener.network'`

- [ ] **Step 3: Write the implementation**

```python
"""Audio that arrives over a socket instead of an audio device.

``NetworkSource`` drives exactly the same :class:`ZoneEvents` callbacks
``MicSource`` does, so everything downstream — features, VAD gate, Scribe,
ring buffer — is unchanged and unaware. What differs is only where the PCM
came from.

It deliberately imports no web framework: the Conductor owns the socket and
hands this decoded bytes, which is what lets the merge and gating rules be
tested without a server.

Several phones may transmit into one zone. Their speech is transcribed
independently and lands in the same ring buffer, which is what makes a zone
hear a conversation rather than one person. Their *features* are merged into
a single ~30 Hz stream by taking the per-field maximum across nodes heard
from recently: the energy of a room is the loudest thing in it, and a max
degrades correctly to the single-node case.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import fields

import numpy as np

from egregore.types import FeatureFrame

from .features import compute_features
from .sources import ZoneEvents
from .vad import make_gate

log = logging.getLogger(__name__)

__all__ = ["NetworkSource", "merge_frames"]

#: How long a node's last frame keeps contributing to the merge. A phone that
#: stops sending should stop holding the room at its last peak.
DEFAULT_MERGE_WINDOW_S = 2.0


def merge_frames(frames: list[FeatureFrame]) -> FeatureFrame:
    """One frame representing several nodes: per-field max, newest timestamp.

    Raises ``ValueError`` on an empty list — a zone with no active node has
    nothing to publish, and inventing a zeroed frame would read as silence
    the room did not actually produce.
    """
    if not frames:
        raise ValueError("cannot merge an empty frame list")
    if len(frames) == 1:
        return frames[0]
    merged: dict[str, float] = {}
    for f in fields(FeatureFrame):
        values = [getattr(fr, f.name) for fr in frames]
        merged[f.name] = max(values)
    merged["t"] = max(fr.t for fr in frames)
    return FeatureFrame(**merged)


class NetworkSource:
    """A zone's microphone, assembled from whichever browsers are transmitting."""

    def __init__(
        self,
        events: ZoneEvents,
        *,
        zone: str = "main",
        sample_rate: int = 16000,
        gate: object | None = None,
        merge_window_s: float = DEFAULT_MERGE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.zone = zone
        self.sample_rate = sample_rate
        self.gate = gate if gate is not None else make_gate()
        self.merge_window_s = float(merge_window_s)
        self._clock = clock
        self._stopped = False
        #: node id -> (heard_at, frame)
        self._recent: dict[str, tuple[float, FeatureFrame]] = {}
        #: node id -> previous rms, so onset is a per-node delta
        self._prev_rms: dict[str, float] = {}

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Nothing to poll: this source is driven by :meth:`feed`.

        Present so the integration layer can treat every source the same way.
        """
        return None

    def active_nodes(self) -> list[str]:
        now = self._clock()
        return sorted(
            node for node, (heard, _) in self._recent.items()
            if now - heard <= self.merge_window_s
        )

    async def feed(self, node_id: str, pcm: bytes, sample_rate: int) -> None:
        """Handle one block of PCM from one node.

        ``pcm`` is 16-bit signed little-endian mono. Publishes a merged
        feature frame for the zone always, and forwards this node's audio to
        the Scribe when the gate says it contains speech.
        """
        if self._stopped or not pcm:
            return
        if len(pcm) % 2:
            raise ValueError("payload is not 16-bit mono PCM (odd byte count)")

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        frame = compute_features(
            samples, sample_rate, prev_rms=self._prev_rms.get(node_id, 0.0)
        )
        self._prev_rms[node_id] = frame.rms

        now = self._clock()
        self._recent[node_id] = (now, frame)
        live = [
            f for node, (heard, f) in self._recent.items()
            if now - heard <= self.merge_window_s
        ]
        await self.events.on_features(merge_frames(live))

        if self.events.on_speech_audio is not None and self.gate.is_speech(
            pcm, sample_rate
        ):
            await self.events.on_speech_audio(pcm, sample_rate)
```

- [ ] **Step 4: Export it**

In `egregore/listener/__init__.py`, add `NetworkSource` and `merge_frames` to the imports and `__all__`, following the pattern already used for `MicSource`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_network_source.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 6: Commit**

```bash
git add egregore/listener/ tests/test_network_source.py
git commit -m "Add a network audio source that merges several phones into one zone"
```

---

### Task 3: Ingest socket and node API

**Files:**
- Modify: `egregore/conductor/state.py` (registry + ingest handler)
- Modify: `egregore/conductor/app.py` (routes)
- Modify: `tests/test_conductor.py`

**Interfaces:**
- Consumes: `NodeRegistry`, `ws_authorized`.
- Produces: `ConductorState.nodes: NodeRegistry`, `ConductorState.ingest_handler: Callable[[str, str, bytes, int], Awaitable[None]] | None`; routes `POST /api/nodes`, `GET /api/nodes`, `POST /api/nodes/{node_id}/mute`, `DELETE /api/nodes/{node_id}`, `WS /ws/ingest`.

- [ ] **Step 1: Write the failing tests**

```python
def test_enrolling_a_node_and_listing_it(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        r = client.post("/api/nodes", json={
            "id": "n1", "label": "Ben's phone", "zone": "main", "role": "transmit"})
        assert r.status_code == 200
        assert r.json()["node"]["zone"] == "main"
        listed = client.get("/api/nodes").json()["nodes"]
    assert [n["id"] for n in listed] == ["n1"]


def test_enrolling_is_open_but_managing_nodes_needs_the_operator(cfg_home):
    # Walking up with a phone must not need a password; cutting someone off
    # is an operator action.
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        assert client.post("/api/nodes", json={
            "id": "n1", "label": "p", "zone": "main", "role": "both"}).status_code == 200
        assert client.get("/api/nodes").status_code == 401
        assert client.post("/api/nodes/n1/mute", json={"on": True}).status_code == 401
        client.post("/api/join", json={"password": "pw"})
        assert client.get("/api/nodes").status_code == 200
        assert client.post("/api/nodes/n1/mute", json={"on": True}).status_code == 200
        assert client.delete("/api/nodes/n1").status_code == 200


def test_enroll_rejects_a_bad_role(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        assert client.post("/api/nodes", json={
            "id": "n1", "label": "p", "zone": "main", "role": "root"}).status_code == 400


def test_ingest_feeds_the_zone_and_respects_a_mute(cfg_home):
    seen: list[tuple[str, str, int]] = []
    state = _cfg_state()

    async def ingest(zone, node_id, pcm, rate):
        seen.append((zone, node_id, len(pcm)))

    state.ingest_handler = ingest
    app = create_app(state, lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        client.post("/api/nodes", json={
            "id": "n1", "label": "p", "zone": "main", "role": "transmit"})
        with client.websocket_connect("/ws/ingest?zone=main&node=n1") as ws:
            ws.send_bytes(b"\x00\x01" * 100)
            ws.send_bytes(b"\x00\x02" * 100)
        assert len(seen) == 2

        # Muting must drop the audio at the server, not merely hide the node.
        client.post("/api/nodes/n1/mute", json={"on": True})
        with client.websocket_connect("/ws/ingest?zone=main&node=n1") as ws:
            ws.send_bytes(b"\x00\x03" * 100)
        assert len(seen) == 2, "a muted node's audio must not reach the zone"


def test_ingest_requires_the_password_when_one_is_set(cfg_home):
    state = _cfg_state()

    async def ingest(zone, node_id, pcm, rate):
        raise AssertionError("must not be reached")

    state.ingest_handler = ingest
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/ingest?zone=main&node=n1") as ws:
                ws.send_bytes(b"\x00\x01" * 10)
                ws.receive_text()


def test_ingest_from_an_unenrolled_node_is_ignored(cfg_home):
    seen: list = []
    state = _cfg_state()

    async def ingest(zone, node_id, pcm, rate):
        seen.append(node_id)

    state.ingest_handler = ingest
    app = create_app(state, lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/ingest?zone=main&node=ghost") as ws:
            ws.send_bytes(b"\x00\x01" * 100)
    assert seen == [], "audio from a node we never enrolled must be dropped"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_conductor.py -k node or ingest`
Expected: FAIL — 404 on the new routes

- [ ] **Step 3: Add the registry and handler to `ConductorState`**

In `egregore/conductor/state.py`, import `NodeRegistry` from `.nodes` and add to `__init__`:

```python
        #: Devices that enrolled over the network (spec: multi-node party).
        self.nodes = NodeRegistry()
        #: Bound by the integration layer: (zone, node_id, pcm, sample_rate).
        #: ``None`` means this deployment accepts no network audio.
        self.ingest_handler: (
            Callable[[str, str, bytes, int], Awaitable[None]] | None
        ) = None
```

- [ ] **Step 4: Add the routes to `egregore/conductor/app.py`**

Insert before `return app`:

```python
    # -- nodes --------------------------------------------------------------
    #
    # Enrolling is open on purpose: a guest holding a phone should not have to
    # be told a password first. Everything that *manages* a node is an
    # operator action and sits behind the party password.

    @app.post("/api/nodes")
    async def enroll_node(entry: dict) -> dict:
        try:
            node = state.nodes.enroll(
                str(entry.get("id", "")),
                label=str(entry.get("label", "") or "device"),
                zone=str(entry.get("zone", "") or "main"),
                role=str(entry.get("role", "") or "receive"),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return {"node": node.as_wire(), "zones": sorted(state.zone_config)}

    @app.get("/api/nodes", dependencies=[Depends(require_party)])
    async def list_nodes() -> dict:
        state.nodes.expire()
        return {"nodes": [n.as_wire() for n in state.nodes.all()]}

    @app.post("/api/nodes/{node_id}/mute", dependencies=[Depends(require_party)])
    async def mute_node(node_id: str, payload: dict | None = None) -> dict:
        node = state.nodes.mute(node_id, bool((payload or {}).get("on", True)))
        if node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown node {node_id!r}")
        return {"node": node.as_wire()}

    @app.delete("/api/nodes/{node_id}", dependencies=[Depends(require_party)])
    async def kick_node(node_id: str) -> dict:
        if not state.nodes.kick(node_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown node {node_id!r}")
        return {"kicked": node_id}

    @app.websocket("/ws/ingest")
    async def ws_ingest(websocket: WebSocket) -> None:
        """Audio from a transmitting browser.

        Binary frames of 16-bit mono PCM. The socket stays open even when the
        node is muted or unknown — the audio is dropped here rather than the
        connection being torn down, so muting is instant and reversible and a
        phone does not have to notice.
        """
        zone = websocket.query_params.get("zone", "main")
        node_id = websocket.query_params.get("node", "")
        if not ws_authorized(password, websocket.cookies.get(COOKIE_NAME)):
            await websocket.close(code=_WS_UNAUTHORIZED)
            return
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_bytes()
                node = state.nodes.get(node_id)
                if node is None or not node.transmits:
                    continue
                state.nodes.heartbeat(node_id)
                if state.ingest_handler is not None:
                    await state.ingest_handler(zone, node_id, data, 16000)
        except Exception:
            return
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_conductor.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 6: Commit**

```bash
git add egregore/conductor/ tests/test_conductor.py
git commit -m "Accept enrolled browsers as microphones over a websocket"
```

---

### Task 4: Wire NetworkSource into a zone

**Files:**
- Modify: `egregore/app.py`
- Modify: `tests/test_integration.py`

**Interfaces:**
- Consumes: `NetworkSource`, `ConductorState.ingest_handler`.
- Produces: `mic.type: "network"` builds a `NetworkSource`; `run_party` binds `state.ingest_handler` to route by zone; `ZonePipeline.network_source: NetworkSource | None`.

- [ ] **Step 1: Write the failing test**

```python
async def test_network_zone_transcribes_audio_that_arrived_over_the_wire(tmp_path):
    import math, struct

    cfg = _cfg(tmp_path, zones=[{"id": "main", "mic": {"type": "network"}}])
    async with Party(cfg) as party:
        pipe = party.pipelines["main"]
        assert pipe.network_source is not None, "a network zone needs a NetworkSource"

        # 0.5s of a loud tone, in 50ms blocks, as a phone would send it.
        for _ in range(10):
            block = bytearray()
            for i in range(800):
                block += struct.pack(
                    "<h", int(0.6 * 32767 * math.sin(2 * math.pi * 220 * i / 16000)))
            await pipe.network_source.feed("n1", bytes(block), 16000)

        # Features reached the bus whether or not the gate heard speech.
        assert party.state.latest_frame("main") is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_integration.py -k network_zone`
Expected: FAIL — `network_source` attribute does not exist

- [ ] **Step 3: Build the source in `ZonePipeline._build_source`**

In `egregore/app.py`, replace the `network` fall-through. After the `usb` branch, add:

```python
        if mic.type == "network":
            # Browsers enrolled as transmitters feed this over /ws/ingest.
            # It owns no device, so unlike a usb mic it cannot fail to open.
            from egregore.listener import NetworkSource

            try:
                self._transcriber = make_transcriber(cfg.asr.engine, cfg.asr.language)
            except (RuntimeError, ValueError) as e:
                log.warning(
                    "zone %s: no transcriber (%s); network audio will drive "
                    "features only", self.zone, e,
                )
            self.network_source = NetworkSource(events, zone=self.zone)
            return self.network_source
```

Add `self.network_source: NetworkSource | None = None` beside the other
`ZonePipeline.__init__` attributes, and import the type for the annotation.

- [ ] **Step 4: Route ingest to the right zone in `run_party`**

Beside where `state.settings_handler` is bound:

```python
    async def _ingest(zone: str, node_id: str, pcm: bytes, sample_rate: int) -> None:
        pipe = pipelines.get(zone)
        if pipe is None or pipe.network_source is None:
            return
        try:
            await pipe.network_source.feed(node_id, pcm, sample_rate)
        except ValueError as exc:      # a malformed frame is one node's bug
            log.warning("zone %s: bad ingest frame from a node (%s)", zone, exc)

    state.ingest_handler = _ingest
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_integration.py && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 6: Commit**

```bash
git add egregore/app.py tests/test_integration.py
git commit -m "Wire network audio into a zone, making mic.type network real"
```

---

### Task 5: Three topologies

**Files:**
- Modify: `egregore/config/schema.py` (`ContinuityConfig.topology`)
- Modify: `egregore/conductor/state.py` (`mirror_zone`)
- Modify: `egregore/app.py` (shared ring for `commons`)
- Modify: `tests/test_integration.py`, `tests/test_conductor.py`

**Interfaces:**
- Produces: `ContinuityConfig.topology: Literal["independent","commons","mirror"] = "independent"`; `ConductorState.mirror_zone: str | None`; `Party`/`run_party` share one `RingBuffer` across pipelines when topology is `commons`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_integration.py

async def test_commons_topology_shares_one_transcript_pool(tmp_path):
    cfg = _cfg(tmp_path, zones=[
        {"id": "kitchen", "mic": {"type": "fixture"}},
        {"id": "garden", "mic": {"type": "fixture"}},
    ])
    cfg.continuity.topology = "commons"
    async with Party(cfg) as party:
        rings = {id(p.ring) for p in party.pipelines.values()}
        assert len(rings) == 1, "commons means one pool for the whole party"


async def test_independent_topology_keeps_pools_separate(tmp_path):
    cfg = _cfg(tmp_path, zones=[
        {"id": "kitchen", "mic": {"type": "fixture"}},
        {"id": "garden", "mic": {"type": "fixture"}},
    ])
    assert cfg.continuity.topology == "independent"
    async with Party(cfg) as party:
        rings = {id(p.ring) for p in party.pipelines.values()}
        assert len(rings) == 2
```

```python
# tests/test_conductor.py

def test_mirror_serves_one_zones_manifest_to_every_zone(cfg_home):
    from egregore.types import Manifest, ManifestEntry

    state = ConductorState(
        clip_resolver=lambda c: None,
        zone_config={"kitchen": {"lens_stack": []}, "garden": {"lens_stack": []}},
    )
    state.set_manifest("kitchen", Manifest(
        zone="kitchen", mode="mosaic", crossfade_s=2.0, revision=0,
        generated_at=0.0,
        entries=[ManifestEntry(clip_id="abc123", duration_s=8.0, weight=1.0)],
    ))
    # Without mirroring, a zone with no manifest of its own has none.
    assert state.get_manifest("garden") is None

    state.mirror_zone = "kitchen"
    mirrored = state.get_manifest("garden")
    assert mirrored is not None
    assert [e.clip_id for e in mirrored.entries] == ["abc123"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_integration.py -k topology tests/test_conductor.py -k mirror`
Expected: FAIL — no `topology` field, no `mirror_zone`

- [ ] **Step 3: Add `topology` to the schema**

In `egregore/config/schema.py`, in `ContinuityConfig`:

```python
    # How zones relate. "independent": each zone hears only its own room and
    # renders its own loop. "commons": every mic in the party feeds one
    # transcript pool, but each zone still renders its own loop. "mirror":
    # one pool, one loop, every screen showing it at its own phase offset —
    # one generation stream regardless of how many zones exist.
    topology: Literal["independent", "commons", "mirror"] = "independent"
```

- [ ] **Step 4: Add `mirror_zone` to `ConductorState`**

In `__init__`:

```python
        #: When set, every zone is served this zone's manifest — the "mirror"
        #: topology. Screens keep their own loop_phase_offset, so the walls
        #: are in step without being identical.
        self.mirror_zone: str | None = None
```

And at the top of `get_manifest`:

```python
    def get_manifest(self, zone: str) -> Manifest | None:
        if self.mirror_zone is not None:
            zone = self.mirror_zone
        return self._manifests.get(zone)
```

- [ ] **Step 5: Share the ring for `commons`**

In `run_party` and in the `Party` test harness, build the shared ring before the pipelines and pass it in:

```python
    shared_ring = (
        RingBuffer.from_config("party", cfg.privacy)
        if cfg.continuity.topology == "commons" else None
    )
```

`ZonePipeline.__init__` gains `ring: RingBuffer | None = None` and uses it when
given: `self.ring = ring if ring is not None else RingBuffer.from_config(self.zone, cfg.privacy)`.

In `run_party`, after the pipelines exist:

```python
    if cfg.continuity.topology == "mirror" and cfg.zones:
        state.mirror_zone = cfg.zones[0].id
        log.info("topology mirror: every screen follows zone %s", state.mirror_zone)
```

Guard `shared_ring.start()`/`close()` so the shared buffer is started once and
closed once rather than per zone.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 7: Commit**

```bash
git add egregore/ tests/
git commit -m "Add commons and mirror topologies alongside independent zones"
```

---

### Task 6: The join page and the transmitter

**Files:**
- Create: `lens/join.html`
- Create: `lens/transmit.js`
- Modify: `egregore/conductor/app.py` (bare `/` serves the join page)
- Modify: `tests/test_conductor.py`

**Interfaces:**
- Consumes: `POST /api/nodes`, `WS /ws/ingest`.
- Produces: `GET /` serves `join.html`; `GET /?zone=…` still serves `index.html`.

- [ ] **Step 1: Write the failing test**

```python
def test_bare_root_serves_the_join_page_but_a_zone_url_still_serves_a_screen(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        join = client.get("/")
        screen = client.get("/?zone=main")
    assert "join" in join.text.lower() or "this device" in join.text.lower()
    assert 'id="gl"' in screen.text, "an existing screen URL must not break"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/test_conductor.py -k bare_root`
Expected: FAIL — `/` serves the lens, not a join page

- [ ] **Step 3: Route bare `/` to the join page**

Replace the `index` route in `create_app`:

```python
    @app.get("/", include_in_schema=False)
    async def index(zone: str | None = Query(None)) -> FileResponse:
        """A screen when a zone is named, otherwise the join page.

        Keeps every existing `/?zone=main` URL working while letting someone
        who just types the IP be asked what their device is for.
        """
        name = "index.html" if zone else "join.html"
        path = lens_dir / name
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} not installed")
        return FileResponse(path)
```

- [ ] **Step 4: Write `lens/join.html`**

Same tokens as `status.html` (`:root` palette, wordmark, monospace). Content:

- A heading naming the party, read from `POST /api/nodes`'s response.
- **What is this device?** three large buttons: `listen` (transmit), `show` (receive), `both`.
- **Which room?** a select populated from the `zones` array the enroll response returns, plus a free-text field so a room can be named on the spot.
- A **name this device** field, defaulting to something like `phone-a4f2` derived from the node id.
- A **join** button that POSTs to `/api/nodes`, stores `{id, role, zone}` in `localStorage` under `egregore.node`, then:
  - role `receive` → `location.href = '/?zone=' + zone`
  - role `transmit` → swap to a "listening" panel: a level meter, the zone name, and a **stop** button
  - role `both` → start transmitting, then navigate to the screen with `?zone=…&transmit=1`
- A **QR code** of `location.origin` so the first person can pass the address around, drawn to a `<canvas>` by a small inline QR encoder (no external script — the CSP for this page is the same self-contained rule the rest of `lens/` follows).
- On load, if `localStorage` already has a node, show "rejoin as <label>" alongside the fresh-join form.

- [ ] **Step 5: Write `lens/transmit.js`**

An ES module exporting `startTransmit({zone, nodeId, onLevel}) -> {stop()}`:

```js
// Capture, downsample, gate, send. Kept out of lens.js, which is about
// rendering. Gating here rather than on the server is deliberate: a party of
// phones streaming continuously is real bandwidth, and silence never leaving
// the device is a better privacy story than silence the server discards.
const TARGET_RATE = 16000;
const FRAME = 800;            // 50 ms at 16 kHz
const OPEN_RMS = 0.015;       // start sending
const CLOSE_RMS = 0.008;      // stop sending (hysteresis)
const HANGOVER_MS = 700;      // keep sending briefly after a pause
```

Implementation notes for the engineer:

- `getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}})`.
- `AudioContext` at the hardware rate; downsample to 16 kHz by simple averaging decimation into a `Float32Array`, then to `Int16Array`.
- Use an `AudioWorkletNode` when available and fall back to `ScriptProcessorNode` (Safari on iOS still needs it).
- Compute frame RMS; open above `OPEN_RMS`, close below `CLOSE_RMS` after `HANGOVER_MS`; only `ws.send(buffer)` while open. Call `onLevel(rms)` every frame regardless so the meter moves even in silence.
- Reconnect the socket with backoff on close, and stop cleanly on `stop()` (close the socket, disconnect the node, stop every track).

- [ ] **Step 6: Verify by hand against a running party**

```bash
uv run egregore run presets/party.yaml     # created in Task 8
```

Open the LAN address on a phone, choose **listen**, pick a room, and confirm:
the node appears in `GET /api/nodes`; `buffer_tokens` on `/api/status` climbs
while talking and stops while silent; muting the node from the dashboard stops
it climbing.

- [ ] **Step 7: Commit**

```bash
git add lens/join.html lens/transmit.js egregore/conductor/app.py tests/test_conductor.py
git commit -m "Add a join page that enrols a phone as a microphone or a screen"
```

---

### Task 7: Nodes panel and topology toggle on the dashboard

**Files:**
- Modify: `lens/setup.html`

- [ ] **Step 1: Add a nodes panel**

Polls `GET /api/nodes` every 2 s and renders a row per node: label, zone, role,
a level bar fed by `level`, and `mute`/`kick` buttons calling
`POST /api/nodes/{id}/mute` and `DELETE /api/nodes/{id}`. A muted node's row
dims and its button reads `unmute`. Empty state reads:
`no devices yet — open http://<host>:8420 on a phone to add one`, with the
host filled in from `location.host`.

- [ ] **Step 2: Add the topology control**

A three-way select in the generation panel bound to `continuity.topology`,
tagged `restart` (it changes which ring buffers exist), with one line of help:

```
independent  each room hears itself and renders its own loop
commons      every mic feeds one pool; each room renders its own loop
mirror       one pool, one loop, every screen at its own phase
```

- [ ] **Step 3: Verify by hand**

With two browser tabs enrolled as nodes, confirm both appear, levels move
while talking, mute silences one without disturbing the other, and kick
removes it from the list.

- [ ] **Step 4: Commit**

```bash
git add lens/setup.html
git commit -m "Show enrolled nodes and the party topology on the dashboard"
```

---

### Task 8: Party preset and documentation

**Files:**
- Create: `presets/party.yaml`
- Modify: `README.md`
- Modify: `docs/signage.md`

- [ ] **Step 1: Write `presets/party.yaml`**

Three network zones (`kitchen`, `dance-floor`, `garden`), `asr.engine: parakeet`,
`backend: procedural` with `fallback: none`, `topology: commons`,
`serving.bind: 0.0.0.0:8420`, `privacy.signage_required: true`, and a header
comment giving the two URLs: the LAN address for guests and
`/static/setup.html` for the operator.

- [ ] **Step 2: Update the README**

Add a **Run a party** section above **Three ways to run it**: start
`presets/party.yaml`, read the LAN address printed in the banner, let people
open it and choose listen / show / both. Add the topology table from the spec.
Extend the privacy section to state plainly that transmitting phones put audio
on the local network, that silence never leaves the device, and that nothing
reaches the cloud.

- [ ] **Step 3: Update `docs/signage.md`**

Add the sentence the posted notice needs: audio from phones acting as
microphones travels over the local network to the machine running Egregore,
is transcribed there, and is never stored or sent outside the building.

- [ ] **Step 4: Verify every command in the docs runs**

```bash
uv run egregore check presets/party.yaml
uv run ruff check . && uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add presets/party.yaml README.md docs/signage.md
git commit -m "Add a party preset and document multi-node setup"
```

---

## Self-Review

**Spec coverage.** Node model and registry → Task 1. Audio ingest and feature
merge → Tasks 2–4. Topologies → Task 5. Join page, QR, transmitter → Task 6.
Operator nodes panel and topology toggle → Task 7. Preset, README, signage →
Task 8. The spec's privacy constraint appears as browser-side gating (Task 6,
Step 5), server-side mute enforcement (Task 3, tested), and signage copy
(Task 8).

**Placeholders.** None. The two prose-heavy steps (Task 6's page and
transmitter) name exact constants, exact endpoints, exact `localStorage` keys
and exact fallback APIs rather than gesturing at behaviour.

**Type consistency.** `NodeRegistry.enroll/heartbeat/get/all/transmitters/
mute/kick/expire` and `Node.as_wire/transmits` are defined in Task 1 and used
under those names in Tasks 3 and 7. `NetworkSource.feed(node_id, pcm,
sample_rate)` and `merge_frames` are defined in Task 2 and called in Tasks 3
and 4. `ConductorState.nodes`, `.ingest_handler` and `.mirror_zone` are added
in Tasks 3 and 5 and consumed in Tasks 4, 5 and 7. `ZonePipeline.network_source`
is added in Task 4 and used by the ingest router in the same task.

**One gap found and closed:** the spec requires ingest backpressure, and no
task covered it. Task 3's socket handles each frame on arrival inside the
receive loop, so a node sending faster than the server can transcribe blocks
its own socket rather than queueing unboundedly — the drop is TCP-level and
per-node, which satisfies the constraint without a queue. Noted here so the
reviewer does not go looking for a queue that should not exist.
