"""Tests for CONDUCTOR (Architecture §2.8): manifest/clip/status HTTP routes,
the feature and manifest WebSocket buses, and shared-password auth.

HTTP routes are exercised with an ``httpx.AsyncClient`` over
``ASGITransport`` (no real socket). WebSocket routes are exercised with
Starlette's synchronous ``TestClient`` — its ``.portal`` (an anyio blocking
portal into the app's own event loop) is used to call async
``ConductorState`` methods *from* the app's loop, which is the supported way
to mutate asyncio-backed state from a test running on a different thread.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from egregore.conductor import ConductorState, create_app
from egregore.types import FeatureFrame, Manifest, ManifestEntry, MoodState

PASSWORD = "dream on"


def make_state(tmp_path: Path, *, status_provider=None) -> tuple[ConductorState, Path]:
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()

    def resolver(clip_id: str) -> Path | None:
        candidate = clip_dir / f"{clip_id}.mp4"
        return candidate if candidate.is_file() else None

    state = ConductorState(
        clip_resolver=resolver,
        zone_config={"main": {"lens_stack": ["flow", "bloom"], "screens": {}}},
        status_provider=status_provider,
    )
    return state, clip_dir


def make_app(tmp_path: Path, state: ConductorState, *, password: str | None = None):
    lens_dir = tmp_path / "lens"
    lens_dir.mkdir(exist_ok=True)
    (lens_dir / "index.html").write_text("<html><body>lens</body></html>")
    return create_app(state, lens_dir=lens_dir, password=password)


def sample_manifest(zone: str = "main") -> Manifest:
    return Manifest(
        zone=zone,
        entries=[
            ManifestEntry(clip_id="clip-aaa", duration_s=8.0, weight=1.0, movement_id=None),
            ManifestEntry(clip_id="clip-bbb", duration_s=6.0, weight=0.5, movement_id="m1"),
        ],
        mode="mosaic",
        crossfade_s=1.5,
    )


# ---------------------------------------------------------------------------
# Manifest: set -> GET, revision bump, WS notification
# ---------------------------------------------------------------------------


async def test_manifest_set_and_get_returns_clip_urls_and_revision(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)
    state.set_manifest("main", sample_manifest())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/manifest", params={"zone": "main"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["zone"] == "main"
        assert body["mode"] == "mosaic"
        assert body["crossfade_s"] == 1.5
        assert body["revision"] == 1
        assert body["entries"][0]["clip_id"] == "clip-aaa"
        assert body["entries"][0]["url"] == "/clips/clip-aaa.mp4"
        assert body["entries"][0]["duration_s"] == 8.0
        assert body["entries"][1]["weight"] == 0.5
        assert body["entries"][1]["movement_id"] == "m1"

        # Second set() bumps the revision monotonically regardless of
        # whatever revision was on the incoming object.
        state.set_manifest("main", sample_manifest())
        resp2 = await client.get("/api/manifest", params={"zone": "main"})
        assert resp2.json()["revision"] == 2

        # Unknown zone -> 404.
        resp3 = await client.get("/api/manifest", params={"zone": "nowhere"})
        assert resp3.status_code == 404


def test_manifest_revision_bump_notifies_ws_subscriber(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/manifest?zone=main") as ws:
            # No manifest yet: first message should be the update after we
            # set one, not a stale/empty one.
            client.portal.call(state.set_manifest, "main", sample_manifest())
            msg = ws.receive_json()
            assert msg == {"type": "manifest", "revision": 1}

            client.portal.call(state.set_manifest, "main", sample_manifest())
            msg2 = ws.receive_json()
            assert msg2 == {"type": "manifest", "revision": 2}


def test_manifest_ws_sends_current_revision_immediately_on_connect(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)
    state.set_manifest("main", sample_manifest())

    with TestClient(app) as client:
        with client.websocket_connect("/ws/manifest?zone=main") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "manifest", "revision": 1}


# ---------------------------------------------------------------------------
# Feature bus: publish fans out to WS subscribers
# ---------------------------------------------------------------------------


def test_feature_publish_fans_out_to_ws_client(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)

    frame = FeatureFrame(t=1.0, rms=0.5, low=0.1, mid=0.2, high=0.3, centroid=0.4, onset=0.9)
    mood = MoodState(energy=0.7, variability=0.1, onset_density=0.2, brightness=0.6)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/features?zone=main") as ws:
            client.portal.call(state.publish_features, "main", frame)
            msg = ws.receive_json()
            assert msg["type"] == "features"
            assert msg["rms"] == 0.5
            assert msg["onset"] == 0.9

            client.portal.call(state.publish_mood, "main", mood)
            msg2 = ws.receive_json()
            assert msg2["type"] == "mood"
            assert msg2["energy"] == 0.7

        assert state.screens_connected_for("main") == 0  # disconnect decremented it


def test_feature_ws_sends_last_known_frame_and_mood_on_connect(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)

    frame = FeatureFrame(t=1.0, rms=0.5, low=0.1, mid=0.2, high=0.3, centroid=0.4, onset=0.9)
    mood = MoodState(energy=0.7, variability=0.1, onset_density=0.2, brightness=0.6)

    with TestClient(app) as client:
        client.portal.call(state.publish_features, "main", frame)
        client.portal.call(state.publish_mood, "main", mood)

        with client.websocket_connect("/ws/features?zone=main") as ws:
            first = ws.receive_json()
            second = ws.receive_json()
            types = {first["type"], second["type"]}
            assert types == {"features", "mood"}
            assert state.screens_connected_for("main") == 1


async def test_publish_features_does_not_block_on_slow_consumer(tmp_path):
    """A subscriber queue that is already full must not slow down (or
    error out) a publish — the oldest queued frame is dropped instead."""
    state, _ = make_state(tmp_path)
    queue = state.subscribe_features("main")

    # Fill the bounded queue past capacity by hand (no one draining it).
    for i in range(10):
        frame = FeatureFrame(t=float(i), rms=0.1, low=0.1, mid=0.1, high=0.1, centroid=0.1, onset=0.1)
        await asyncio.wait_for(state.publish_features("main", frame), timeout=0.5)

    # The queue never grew past its bound, and the publisher returned
    # promptly each time (the wait_for above would have failed otherwise).
    assert queue.qsize() <= 4
    # Latest value is still tracked for late joiners even though most
    # frames were dropped by slow consumers.
    assert state.latest_frame("main").t == 9.0


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------


async def test_clip_route_serves_bytes_with_immutable_cache_header(tmp_path):
    state, clip_dir = make_state(tmp_path)
    app = make_app(tmp_path, state)
    (clip_dir / "clip-aaa.mp4").write_bytes(b"fake mp4 bytes")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/clips/clip-aaa.mp4")
        assert resp.status_code == 200
        assert resp.content == b"fake mp4 bytes"
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"

        resp404 = await client.get("/clips/does-not-exist.mp4")
        assert resp404.status_code == 404


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_wrong_password_401_on_api_manifest(tmp_path):
    state, _ = make_state(tmp_path)
    state.set_manifest("main", sample_manifest())
    app = make_app(tmp_path, state, password=PASSWORD)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/manifest", params={"zone": "main"})
        assert resp.status_code == 401

        join = await client.post("/api/join", json={"password": "not it"})
        assert join.status_code == 401


async def test_join_with_right_password_sets_cookie_and_grants_access(tmp_path):
    state, _ = make_state(tmp_path)
    state.set_manifest("main", sample_manifest())
    app = make_app(tmp_path, state, password=PASSWORD)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        join = await client.post("/api/join", json={"password": PASSWORD})
        assert join.status_code == 200
        assert join.json()["ok"] is True
        assert "egregore_party" in join.cookies

        # The client's cookie jar now carries the session cookie.
        resp = await client.get("/api/manifest", params={"zone": "main"})
        assert resp.status_code == 200
        assert resp.json()["revision"] == 1


async def test_disabled_auth_mode_is_open(tmp_path):
    state, _ = make_state(tmp_path)
    state.set_manifest("main", sample_manifest())
    app = make_app(tmp_path, state, password=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/manifest", params={"zone": "main"})
        assert resp.status_code == 200

        join = await client.post("/api/join", json={"password": "anything"})
        assert join.status_code == 200
        assert join.json()["auth"] == "disabled"


def test_ws_closes_with_4401_on_bad_cookie(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state, password=PASSWORD)

    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/features?zone=main"):
                pass
        assert excinfo.value.code == 4401


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def test_status_returns_provider_dict(tmp_path):
    async def provider() -> dict:
        return {"zones": ["main"], "spend_usd": "1.23", "queue_depth": 0}

    state, _ = make_state(tmp_path, status_provider=provider)
    app = make_app(tmp_path, state)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["zones"] == ["main"]
        assert body["spend_usd"] == "1.23"
        assert body["screens_connected"] == 0
        assert "screens_connected_by_zone" in body


async def test_status_without_provider_still_reports_screens(tmp_path):
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["screens_connected"] == 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


async def test_config_merges_screen_override_over_zone_default(tmp_path):
    state, _ = make_state(tmp_path)
    state.zone_config["main"]["screens"]["proj1"] = {
        "lens_stack": ["kaleidoscope"],
        "loop_phase_offset": 0.25,
        "audio_source": "local_mic",
    }
    app = make_app(tmp_path, state)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/config", params={"zone": "main"})
        assert resp.json()["lens_stack"] == ["flow", "bloom"]

        resp2 = await client.get("/api/config", params={"zone": "main", "screen": "proj1"})
        body = resp2.json()
        assert body["lens_stack"] == ["kaleidoscope"]
        assert body["loop_phase_offset"] == 0.25
        assert body["audio_source"] == "local_mic"

        resp3 = await client.get("/api/config", params={"zone": "nowhere"})
        assert resp3.status_code == 404


# ---------------------------------------------------------------------------
# Configuration surface — settings, secret presence, catalogue
# ---------------------------------------------------------------------------

_LENS = Path(__file__).resolve().parent.parent / "lens"


def _cfg_state(**kw) -> ConductorState:
    """A ConductorState with nothing but the required resolver — these tests
    exercise the configuration surface, which reads none of the party state."""
    return ConductorState(clip_resolver=lambda clip_id: None, **kw)


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("EGREGORE_HOME", str(tmp_path))
    return tmp_path


def test_secrets_endpoint_reports_presence_and_never_a_value(cfg_home, monkeypatch):
    import json as _json

    monkeypatch.setenv("FAL_KEY", "leak-me-if-you-can")
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        body = client.get("/api/secrets").json()
    assert body["present"]["FAL_KEY"] is True
    assert "leak-me-if-you-can" not in _json.dumps(body)


def test_settings_endpoints_require_the_password_even_when_party_auth_is_off(cfg_home):
    # Watching the screens and reconfiguring the system are different trust
    # levels; party auth being disabled must not open the settings surface.
    app = create_app(_cfg_state(), lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200  # party auth off
        assert client.get("/api/settings").status_code == 403
        assert client.get("/api/secrets").status_code == 403
        assert client.get("/api/models").status_code == 403
        assert client.post("/api/settings", json={}).status_code == 403


def test_settings_endpoints_open_with_the_password(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401  # not joined yet
        client.post("/api/join", json={"password": "pw"})
        assert client.get("/api/settings").status_code == 200


def test_settings_post_separates_live_from_restart(cfg_home):
    applied: list[dict] = []
    state = _cfg_state()
    state.settings_handler = lambda overrides: applied.append(overrides) or {"ok": True}
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        body = client.post(
            "/api/settings",
            json={"generation": {"clip_duration_s": 6, "backend": "fal"}},
        ).json()
    assert body["applied_live"] == ["generation.clip_duration_s"]
    assert body["restart_required"] == ["generation.backend"]
    assert applied, "the live subset must reach the running party"


def test_settings_post_rejects_an_invalid_value_without_persisting(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/settings", json={"generation": {"clip_duration_s": 999}})
        assert r.status_code == 400
        assert client.get("/api/settings").json()["overrides"] == {}


def test_settings_persist_and_merge_across_posts(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        client.post("/api/settings", json={"generation": {"clip_duration_s": 6}})
        client.post("/api/settings", json={"aesthetic": {"drift": 0.7}})
        overrides = client.get("/api/settings").json()["overrides"]
    assert overrides["generation"]["clip_duration_s"] == 6
    assert overrides["aesthetic"]["drift"] == 0.7, "a later post must not erase an earlier one"


def test_models_crud(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    entry = {
        "provider": "fal",
        "model_id": "vendor/thing/text-to-video",
        "price_per_second": {"720P": "0.07"},
        "default_resolution": "720P",
        "allowed_durations_s": [5, 10],
    }
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post("/api/models", json={"key": "thing", **entry}).status_code == 200
        listed = client.get("/api/models").json()
        assert "thing" in listed
        assert listed["thing"]["builtin"] is False
        assert listed["minimax-h3-max"]["builtin"] is True
        assert client.delete("/api/models/thing").status_code == 200
        assert "thing" not in client.get("/api/models").json()


def test_models_post_rejects_a_nonpositive_price(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/models", json={
            "key": "free-lunch", "provider": "fal", "model_id": "x/y",
            "price_per_second": {"720P": "0"}, "default_resolution": "720P",
            "allowed_durations_s": [5],
        })
        # A zero price reserves nothing against the ceiling (PRD B-2).
        assert r.status_code == 400
        assert "free-lunch" not in client.get("/api/models").json()


def test_models_post_needs_a_key(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post("/api/models", json={"model_id": "x/y"}).status_code == 400


def test_builtin_model_cannot_be_deleted(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.delete("/api/models/minimax-h3-max")
        assert r.status_code == 400
        assert "minimax-h3-max" in client.get("/api/models").json()


# ---------------------------------------------------------------------------
# Zones, lenses, audio devices, and writing a key
# ---------------------------------------------------------------------------


def _zoned_state(**kw) -> ConductorState:
    return ConductorState(
        clip_resolver=lambda clip_id: None,
        zone_config={
            "main": {"lens_stack": ["flow", "feedback"], "screens": {"s1": {}}},
        },
        **kw,
    )


def test_zones_lists_the_live_look_and_the_lenses_on_offer(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        body = client.get("/api/zones").json()
    assert body["zones"]["main"]["lens_stack"] == ["flow", "feedback"]
    assert body["zones"]["main"]["audio_source"] == "zone"
    assert "kaleidoscope" in body["known_lenses"]


def test_changing_a_lens_stack_takes_effect_and_notifies_screens(cfg_home):
    state = _zoned_state()
    queue = state.subscribe_manifest("main")   # stand in for a connected screen
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/zones/main", json={"lens_stack": ["glitch", "crt"]})
        assert r.status_code == 200
        assert r.json()["lens_stack"] == ["glitch", "crt"]
        assert client.get("/api/config?zone=main").json()["lens_stack"] == ["glitch", "crt"]

    # A screen holding the manifest socket learns without being reloaded.
    assert queue.get_nowait() == {"type": "config", "revision": 1}


def test_unknown_lens_is_refused_rather_than_silently_dropped(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/zones/main", json={"lens_stack": ["flow", "not-a-lens"]})
        assert r.status_code == 400
        assert "not-a-lens" in r.json()["detail"]
        # and the zone is untouched
        assert client.get("/api/config?zone=main").json()["lens_stack"] == ["flow", "feedback"]


def test_audio_source_switches_between_the_zone_bus_and_a_local_mic(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post(
            "/api/zones/main", json={"audio_source": "local_mic"}
        ).json()["audio_source"] == "local_mic"
        assert client.post(
            "/api/zones/main", json={"audio_source": "nonsense"}
        ).status_code == 400


def test_zone_patch_refuses_a_microphone_change(cfg_home):
    # The audio device is opened once at start-up, so accepting this here
    # would report success for something that did not happen.
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post("/api/zones/main", json={"mic": {"type": "usb"}}).status_code == 400


def test_unknown_zone_404s(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.post("/api/zones/nope", json={"lens_stack": ["flow"]}).status_code == 404


def test_audio_devices_answers_even_without_sounddevice(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        body = client.get("/api/audio-devices").json()
    assert set(body) == {"available", "reason", "devices"}
    assert isinstance(body["devices"], list)


def test_writing_a_key_is_refused_from_off_the_machine(cfg_home):
    # TestClient reports its host as "testclient", i.e. not loopback, which
    # is exactly the case this rule exists for: the party password is enough
    # to change a lens stack, and deliberately not enough to set a credential.
    app = create_app(_cfg_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/secrets", json={"name": "FAL_KEY", "value": "abc"})
        assert r.status_code == 403
        assert client.get("/api/secrets").json()["writable"] is False
        assert client.get("/api/secrets").json()["present"]["FAL_KEY"] is False


# ---------------------------------------------------------------------------
# Nodes — enrolling a phone, and cutting one off
# ---------------------------------------------------------------------------


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


def test_ingest_from_a_receive_only_node_is_ignored(cfg_home):
    seen: list = []
    state = _cfg_state()

    async def ingest(zone, node_id, pcm, rate):
        seen.append(node_id)

    state.ingest_handler = ingest
    app = create_app(state, lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        client.post("/api/nodes", json={
            "id": "screen", "label": "tv", "zone": "main", "role": "receive"})
        with client.websocket_connect("/ws/ingest?zone=main&node=screen") as ws:
            ws.send_bytes(b"\x00\x01" * 100)
    assert seen == [], "a screen is not a microphone"


def test_ingest_requires_the_password_when_one_is_set(cfg_home):
    state = _cfg_state()

    async def ingest(zone, node_id, pcm, rate):
        raise AssertionError("must not be reached")

    state.ingest_handler = ingest
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/ingest?zone=main&node=n1") as ws:
            ws.send_bytes(b"\x00\x01" * 10)
            ws.receive_text()
    # Same refusal code the feature bus uses for an unauthenticated socket.
    assert exc.value.code == 4401


def test_bare_root_serves_the_join_page_but_a_zone_url_still_serves_a_screen(cfg_home):
    app = create_app(_cfg_state(), lens_dir=_LENS, password=None)
    with TestClient(app) as client:
        join = client.get("/")
        screen = client.get("/?zone=main")
    assert "this device" in join.text.lower()
    assert 'id="gl"' in screen.text, "an existing screen URL must not break"


def test_a_named_screen_inherits_its_zones_lenses(cfg_home):
    """The bug this guards: a screen entry carries every key with an explicit
    None when it has no override, and dict.get(key, fallback) returns that
    None rather than falling through. Every named screen therefore reported
    lens_stack: null, the client dropped to its built-in default, and a whole
    venue wore the same three lenses regardless of zone."""
    state = ConductorState(
        clip_resolver=lambda c: None,
        zone_config={
            "workshop": {
                "lens_stack": ["glitch", "chroma", "crt"],
                "screens": {
                    "bench": {"lens_stack": None, "loop_phase_offset": 0.5,
                              "audio_source": None},
                    "wall": {"lens_stack": ["bloom"], "loop_phase_offset": 0.0,
                             "audio_source": "local_mic"},
                },
            },
        },
    )
    # No override: inherit the zone's stack, keep the screen's own phase.
    bench = state.get_config("workshop", "bench")
    assert bench["lens_stack"] == ["glitch", "chroma", "crt"]
    assert bench["loop_phase_offset"] == 0.5
    assert bench["audio_source"] == "zone"

    # An explicit override still wins.
    wall = state.get_config("workshop", "wall")
    assert wall["lens_stack"] == ["bloom"]
    assert wall["audio_source"] == "local_mic"

    # And the zone itself is unchanged.
    assert state.get_config("workshop")["lens_stack"] == ["glitch", "chroma", "crt"]


def test_pacing_is_served_and_bounded(cfg_home):
    """Playback rate and crossfade are what separate a loop that pulses from
    one that flickers past, so both are live — and both are bounded, because
    a rate of 0 stops the room and a 30-second crossfade is a still image."""
    state = _zoned_state()
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        assert client.get("/api/config?zone=main").json()["playback_rate"] == 1.0

        assert client.post("/api/zones/main",
                           json={"playback_rate": 0.6}).status_code == 200
        assert client.get("/api/config?zone=main").json()["playback_rate"] == 0.6

        assert client.post("/api/zones/main",
                           json={"crossfade_override": 5.0}).status_code == 200
        assert client.get("/api/config?zone=main").json()["crossfade_s"] == 5.0

        for bad in ({"playback_rate": 0}, {"playback_rate": 9},
                    {"playback_rate": "fast"}, {"crossfade_override": 60},
                    {"crossfade_override": 0}):
            assert client.post("/api/zones/main", json=bad).status_code == 400


def test_zone_selection_is_accepted_and_forwarded(cfg_home):
    state = _zoned_state()
    seen = {}
    state.zone_settings_handler = lambda zone, patch: seen.update({zone: patch})
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/zones/main",
                        json={"selection": {"novelty": 0.9, "segment_gap_s": 4}})
        assert r.status_code == 200, r.text
        assert seen == {"main": {"novelty": 0.9, "segment_gap_s": 4.0}}
        body = client.get("/api/zones").json()
        assert body["zones"]["main"]["selection"]["novelty"] == 0.9


def test_zone_selection_rejects_a_bad_weight(cfg_home):
    app = create_app(_zoned_state(), lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/zones/main", json={"selection": {"novelty": 7}})
        assert r.status_code == 400


def test_zone_hold_reaches_the_screen_config(cfg_home):
    state = _zoned_state()
    app = create_app(state, lens_dir=_LENS, password="pw")
    with TestClient(app) as client:
        client.post("/api/join", json={"password": "pw"})
        r = client.post("/api/zones/main", json={"hold_s": 25})
        assert r.status_code == 200, r.text
        assert client.get("/api/config?zone=main").json()["hold_s"] == 25.0
        assert client.get("/api/zones").json()["zones"]["main"]["hold_s"] == 25.0
        assert client.post("/api/zones/main", json={"hold_s": -1}).status_code == 400


def test_a_screen_reports_what_it_is_playing(tmp_path):
    # The server cannot otherwise know which clip a screen chose: the
    # playlist is weighted and the client picks. The beacon is what lets an
    # operator (and a soak test) see that generated video is on screen.
    state, _ = make_state(tmp_path)
    app = make_app(tmp_path, state)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/manifest?zone=main") as ws:
            ws.send_json({"type": "playing", "screen": "screen-1", "clip_id": "abc123"})
            import time as _t
            deadline = _t.monotonic() + 2
            while _t.monotonic() < deadline and not state.now_playing.get("main"):
                _t.sleep(0.05)
        assert state.now_playing["main"]["screen-1"]["clip_id"] == "abc123"
        body = client.get("/api/status").json()
        assert body["now_playing"]["main"]["screen-1"]["clip_id"] == "abc123"
