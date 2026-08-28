# Multi-node party — design

**Status:** approved 2026-08-28

## Problem

Egregore today hears exactly one machine: the one it runs on. A zone's
microphone is a `sounddevice` handle opened at start-up, so a party with
conversation in four rooms is a party Egregore is deaf to in three of them.
Screens have the same shape of problem in reverse — a display is a browser
someone had to be told a URL for.

The goal is that a person at a party opens an address on their phone, chooses
whether that device listens, displays, or both, picks the room they are in, and
is part of the system ten seconds later. Many phones in one room should make
that room hear better, not louder.

`MicConfig.type` already accepts `"network"`, and the integration layer logs
`mic type 'network' not wired in v1`. This builds the thing that slot was
reserved for.

## Constraints

**The privacy posture changes, deliberately.** Audio currently never leaves the
machine that captured it. Browser transmitters put speech on the local network.
This stays inside the building and still never reaches the cloud, and the ring
buffer invariant is untouched — but it is a real change, so: transmitters gate
locally and send only what sounds like speech, silence never leaves the device,
and the signage copy says that audio crosses the LAN.

**Enrollment is open.** Requiring a password at the moment someone walks up with
a phone defeats the point. Control is exercised after the fact: every node is
visible to the operator with its zone, role and live level, and can be muted or
kicked. A muted node's audio is dropped at the server, not merely ignored by the
UI.

**Existing URLs keep working.** `/?zone=main` must remain a screen, so wall
displays and the operator's own tabs do not break.

## Model

A **node** is a browser that has enrolled. It has an id (random, in
`localStorage`, so a reload rejoins rather than duplicating), a label, a zone,
and a role: `transmit`, `receive`, or `both`.

A **zone** stays a room. Many nodes may transmit into one zone; every fragment
they produce lands in that zone's single ring buffer, so the zone hears the
conversation rather than one person.

## Audio ingest

    browser mic → getUserMedia → 16 kHz mono Int16
                → local speech gate (energy + hangover)
                → WS /ws/ingest?zone=<z>&node=<id>  (binary frames)
                → NetworkSource
                → features → feature bus
                → gated PCM → transcriber → ring buffer

`NetworkSource` implements the same `ZoneEvents` contract `MicSource` does, so
nothing downstream knows or cares that the audio arrived over a socket. It owns
no audio device and cannot block on one.

**Feature merging.** A zone with several transmitters publishes one feature
frame at ~30 Hz: the per-field maximum across nodes that have sent audio within
the last two seconds. The energy of a room is the loudest thing in it, and a max
degrades correctly to the single-node case. A node that goes quiet ages out of
the merge rather than pinning the room at zero.

**Backpressure.** Ingest frames are handled on arrival and never queued
unboundedly; a node sending faster than the server can transcribe has its
oldest pending audio dropped. Losing a fragment is invisible; stalling the
event loop is not.

## Topologies

Selected by `continuity.topology`, switchable live from the dashboard.

| mode | transcript pool | generation | streams |
|---|---|---|---|
| `independent` | per zone | per zone | N |
| `commons` | one, party-wide | per zone | N |
| `mirror` | one, party-wide | one | 1 |

`independent` is today's behaviour and stays the default.

`commons` gives every `ZonePipeline` the same `RingBuffer` instance. Each zone
still weaves and generates, so the kitchen and the dance floor both dream the
whole party while looking different.

`mirror` needs no new generation machinery: one zone generates, and every
screen is served that zone's manifest. Per-screen `loop_phase_offset` already
exists and is what keeps the walls from being identical.

Switching topology at runtime affects which pool is read and which manifest is
served. It does not rebuild the backend ladder, so it is a live change.

## Components

- `egregore/listener/network.py` — `NetworkSource` and the per-zone feature
  merge. No FastAPI import: it receives decoded PCM from the conductor and is
  testable without a server.
- `egregore/conductor/nodes.py` — `NodeRegistry`: enroll, heartbeat, list,
  mute, kick, expire. Pure state, no I/O.
- Conductor routes — `POST /api/nodes` (enroll), `GET /api/nodes` (operator),
  `POST /api/nodes/{id}/mute`, `DELETE /api/nodes/{id}`, and
  `WS /ws/ingest`.
- `lens/join.html` — role and zone picker, with a QR of the LAN URL to pass
  around. Bare `/` serves this when the device has not enrolled; `/?zone=…`
  continues to serve the screen.
- `lens/transmit.js` — capture, downsample, gate, send. Kept out of `lens.js`,
  which is already 500 lines and about rendering.
- Dashboard — a nodes panel with live levels and mute/kick, and the topology
  toggle.

## Testing

- `NetworkSource`: PCM in produces feature frames out; gated speech reaches
  `on_speech_audio`; a silent stream produces features and no speech.
- Two nodes into one zone: both transcripts reach the same ring buffer, and
  the merged feature frame is the per-field max.
- A node that stops sending ages out of the merge within its window.
- Muting a node drops its audio at the server: its PCM produces no ring
  fragment while another node in the same zone still does.
- `NodeRegistry`: enroll is idempotent per id, heartbeat keeps a node live,
  a node with no heartbeat expires, kick removes it.
- Topology: `mirror` serves one manifest to every zone; `commons` gives every
  pipeline the same ring instance; `independent` keeps them distinct.
- Ingest auth mirrors the feature bus: where a password is set, an
  unauthenticated ingest socket is refused.
- The existing privacy suite keeps passing unchanged.

## Out of scope

Node-to-node audio (nodes talk to the server only), transcoding anything but
16 kHz mono PCM, and any transport beyond the LAN.
