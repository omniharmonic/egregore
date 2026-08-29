"""CONDUCTOR — FastAPI media server and feature bus (Architecture §2.8).

``create_app`` wires the routes in the table there onto a
:class:`~egregore.conductor.state.ConductorState`, which is the only thing
this module reads. It never touches Weaver, Governor, Forge, Loom, Listener,
or Scribe -- the integration layer (``egregore/app.py``) is the one place
those get wired together, and it feeds this module through the state object
and the ``status_provider``/``clip_resolver`` callables alone.

**Content-safety.** Every route below serves only: clip bytes, manifest
metadata (clip ids/durations/weights/mode), feature frames (floats),
config passthrough, and the operator status dict. None of that data has a
field capable of holding transcript text or a generation prompt --
``egregore.types`` guarantees ``Manifest``/``FeatureFrame``/``MoodState``
are content-blind, and ``ConductorState`` never accepts a prompt string.
So there is no code path here that could leak conversation content even by
accident; the guarantee is structural, not a filter applied at the edge.

**Design decision this module exists to serve** (Architecture §2.8): source
clips ship to clients and every bit of compositing happens client-side in
Lens. The Conductor's job is therefore narrow -- move small files and small
JSON messages -- and stays flat in cost as screens are added.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from egregore.conductor.auth import COOKIE_NAME, RequireParty, check_password, sign, ws_authorized
from egregore.conductor.state import ConductorState
from egregore.config import store as config_store
from egregore.types import Manifest

logger = logging.getLogger(__name__)

__all__ = ["create_app"]

# How often /ws/features and /ws/manifest send an app-level heartbeat so
# intermediaries (Cloudflare Tunnel, browser NAT timeouts) don't reap an
# otherwise-idle connection (PRD D-4: screens self-heal, but a heartbeat
# means most of the time they never have to).
_HEARTBEAT_INTERVAL_S = 15.0

_CLIP_CACHE_CONTROL = "public, max-age=31536000, immutable"

_WS_UNAUTHORIZED = 4401


#: Lenses the client will accept, mirroring KNOWN_LENSES in lens/lens.js. The
#: settings page reads this rather than carrying its own copy.
KNOWN_LENSES = (
    "feedback", "kaleidoscope", "flow", "chroma", "bloom", "liquid",
    "glitch", "pixelsort", "crt", "corrupt", "smoke",
)

#: What each lens's four tunable parameters mean, for the operator's controls.
#: Values are [label, min, max] per slot; a lens with fewer knobs lists fewer.
LENS_PARAMS: dict[str, list] = {
    "smoke": [["drift", 0.0, 0.25], ["dispersion", 0.0, 1.0],
              ["persistence", 0.0, 0.95], ["detail", 0.5, 8.0]],
    "flow": [["warp", 0.0, 0.2], ["swirl", 0.0, 1.0], [], ["scale", 0.5, 8.0]],
    "feedback": [["decay", 0.6, 0.995], ["zoom", -0.05, 0.05]],
    "liquid": [["viscosity", 0.0, 1.0], ["refraction", 0.0, 1.0], [],
               ["scale", 0.5, 6.0]],
    "bloom": [["threshold", 0.0, 1.0], ["strength", 0.0, 1.0]],
    "chroma": [["separation", 0.0, 1.0]],
    "glitch": [["density", 0.0, 1.0], ["block", 0.0, 1.0]],
    "kaleidoscope": [["segments", 2.0, 16.0]],
    "pixelsort": [["threshold", 0.0, 1.0], ["length", 0.0, 1.0]],
    "crt": [["curvature", 0.0, 1.0], ["scanlines", 0.0, 1.0]],
    "corrupt": [["amount", 0.0, 1.0], ["drift", 0.0, 1.0]],
}

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _is_local(request: Request) -> bool:
    """True when the request came from the machine running the party."""
    return bool(request.client) and request.client.host in _LOOPBACK


def _audio_devices() -> dict:
    """Input devices, or an explanation of why we cannot list them.

    ``sounddevice`` is an optional extra, so a core install answers this
    honestly instead of 500ing at a UI that only wants to draw a dropdown.
    """
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "available": False,
            "reason": f"sounddevice not installed ({type(exc).__name__})",
            "devices": [],
        }
    try:
        devices = [
            {
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "default_samplerate": int(d.get("default_samplerate") or 0),
            }
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0
        ]
    except Exception as exc:  # a broken audio stack must not take the page down
        return {"available": False, "reason": str(exc)[:200], "devices": []}
    return {"available": True, "reason": "", "devices": devices}


class JoinRequest(BaseModel):
    password: str = ""


def _manifest_wire(manifest: Manifest) -> dict:
    """Manifest -> the JSON shape ``GET /api/manifest`` serves.

    Adds the ``/clips/{id}.mp4`` URL per entry; everything else is a
    straight field passthrough. Content-blind by construction -- there is
    no field here that could carry transcript text or a prompt.
    """
    return {
        "zone": manifest.zone,
        "mode": manifest.mode,
        "crossfade_s": manifest.crossfade_s,
        "revision": manifest.revision,
        "generated_at": manifest.generated_at,
        "entries": [
            {
                "clip_id": e.clip_id,
                "url": f"/clips/{e.clip_id}.mp4",
                "duration_s": e.duration_s,
                "weight": e.weight,
                "movement_id": e.movement_id,
            }
            for e in manifest.entries
        ],
    }


async def _manifest_reader(websocket: WebSocket, state: ConductorState, zone: str) -> None:
    """Like ``_reader``, but a screen may report what it is playing."""
    import time as _time

    while True:
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("type") == "playing":
            screen = str(msg.get("screen") or "")[:64] or "-"
            clip_id = str(msg.get("clip_id") or "")[:64]
            try:
                shown_s = round(float(msg.get("shown_s") or 0.0), 1)
            except (TypeError, ValueError):
                shown_s = 0.0
            state.now_playing.setdefault(zone, {})[screen] = {
                "clip_id": clip_id, "at": _time.time(), "shown_s": shown_s,
            }


async def _reader(websocket: WebSocket) -> None:
    """Block until the client disconnects.

    Lens never sends anything on these sockets; the only reason to read is
    to notice a disconnect promptly (recv raises ``WebSocketDisconnect``)
    instead of only finding out on the next failed send.
    """
    while True:
        await websocket.receive_text()


async def _writer(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Forward queued messages to ``websocket``; heartbeat when idle.

    A plain ``{"type": "ping"}`` app-level heartbeat, sent whenever the
    queue has produced nothing for ``_HEARTBEAT_INTERVAL_S`` -- keeps
    intermediaries (tunnels, browser idle timeouts) from reaping a
    connection that is alive but momentarily quiet.
    """
    while True:
        try:
            message = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL_S)
        except TimeoutError:
            await websocket.send_json({"type": "ping"})
            continue
        await websocket.send_json(message)


async def _serve_ws(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Run the reader/writer pair until either the client disconnects or a
    send fails, then tear both tasks down cleanly."""
    reader = asyncio.create_task(_reader(websocket))
    writer = asyncio.create_task(_writer(websocket, queue))
    try:
        await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        reader.cancel()
        writer.cancel()
        await asyncio.gather(reader, writer, return_exceptions=True)


def create_app(
    state: ConductorState,
    *,
    lens_dir: Path,
    password: str | None,
) -> FastAPI:
    """Build the Conductor's FastAPI app.

    Args:
        state: the shared view onto the running party (see
            :class:`~egregore.conductor.state.ConductorState`).
        lens_dir: filesystem directory containing the Lens client
            (``index.html``, ``lens.js``, ``shaders/``). Served at ``/``
            and mounted (minus templating) at ``/static``.
        password: the resolved shared party password, or ``None``/``""``
            to disable auth entirely (LAN-trusted default, PRD D-2). Never
            read from the environment here -- resolving
            ``ServingConfig.password_env`` (or generating and printing a
            fallback once) is the integration layer's job.
    """
    app = FastAPI(title="Egregore Conductor")
    require_party = RequireParty(password)

    # -- client app ---------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index(zone: str | None = Query(None)) -> FileResponse:
        """A screen when a zone is named, otherwise the join page.

        Every existing `/?zone=main` URL keeps working — wall displays and the
        operator's own tabs must not break — while someone who just types the
        IP into a phone gets asked what their device is for.
        """
        name = "index.html" if zone else "join.html"
        path = lens_dir / name
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} not installed")
        return FileResponse(path)

    # check_dir=False: lens/ is another module's territory and may not be
    # fully populated at app-construction time (e.g. under test); a missing
    # directory here should 404 individual asset requests, not crash startup.
    app.mount("/static", StaticFiles(directory=lens_dir, html=False, check_dir=False), name="static")

    # -- auth -----------------------------------------------------------

    @app.post("/api/join")
    async def join(payload: JoinRequest) -> JSONResponse:
        if not check_password(password, payload.password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
        resp = JSONResponse({"ok": True, "auth": "enabled" if password else "disabled"})
        if password:
            resp.set_cookie(
                COOKIE_NAME,
                sign(password),
                httponly=True,
                samesite="lax",
                max_age=60 * 60 * 24 * 30,
            )
        return resp

    # -- manifest ---------------------------------------------------------

    @app.get("/api/manifest", dependencies=[Depends(require_party)])
    async def get_manifest(zone: str = Query(...)) -> dict:
        manifest = state.get_manifest(zone)
        if manifest is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown zone {zone!r}")
        return _manifest_wire(manifest)

    @app.websocket("/ws/manifest")
    async def ws_manifest(websocket: WebSocket, zone: str = Query(...)) -> None:
        if not ws_authorized(password, websocket.cookies.get(COOKIE_NAME)):
            await websocket.close(code=_WS_UNAUTHORIZED)
            return
        await websocket.accept()

        queue = state.subscribe_manifest(zone)
        try:
            current = state.get_manifest(zone)
            if current is not None:
                await websocket.send_json({"type": "manifest", "revision": current.revision})
            reader = asyncio.create_task(_manifest_reader(websocket, state, zone))
            writer = asyncio.create_task(_writer(websocket, queue))
            try:
                await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                reader.cancel()
                writer.cancel()
                await asyncio.gather(reader, writer, return_exceptions=True)
        finally:
            state.unsubscribe_manifest(zone, queue)

    # -- feature bus ------------------------------------------------------

    @app.websocket("/ws/features")
    async def ws_features(websocket: WebSocket, zone: str = Query(...)) -> None:
        if not ws_authorized(password, websocket.cookies.get(COOKIE_NAME)):
            await websocket.close(code=_WS_UNAUTHORIZED)
            return
        await websocket.accept()

        state.connect_screen(zone)
        queue = state.subscribe_features(zone)
        try:
            frame = state.latest_frame(zone)
            if frame is not None:
                await websocket.send_json({"type": "features", **frame.as_wire()})
            mood = state.latest_mood(zone)
            if mood is not None:
                await websocket.send_json({"type": "mood", **mood.as_wire()})
            await _serve_ws(websocket, queue)
        finally:
            state.unsubscribe_features(zone, queue)
            state.disconnect_screen(zone)

    # -- clips --------------------------------------------------------------

    @app.get("/clips/{clip_id}.mp4", dependencies=[Depends(require_party)])
    async def get_clip(clip_id: str) -> FileResponse:
        path = state.resolve_clip(clip_id)
        if path is None or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown clip {clip_id!r}")
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": _CLIP_CACHE_CONTROL},
        )

    # -- status / config ----------------------------------------------------

    @app.get("/api/status", dependencies=[Depends(require_party)])
    async def get_status() -> dict:
        base = await state.status_provider() if state.status_provider is not None else {}
        return {
            **base,
            "screens_connected": state.screens_connected,
            "screens_connected_by_zone": state.screens_connected_by_zone(),
            "now_playing": state.now_playing,
        }

    @app.post("/api/control/{action}", dependencies=[Depends(require_party)])
    async def post_control(action: str, payload: dict | None = None) -> dict:
        """Operator controls (freeze/mute/mode). The handler is bound by the
        integration layer; a deployment without one exposes no controls."""
        if state.control_handler is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no controls bound")
        try:
            return await state.control_handler(action, payload or {})
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    @app.get("/api/config", dependencies=[Depends(require_party)])
    async def get_config(zone: str = Query(...), screen: str | None = Query(None)) -> dict:
        config = state.get_config(zone, screen)
        if config is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown zone {zone!r}")
        return config

    # -- configuration ------------------------------------------------------
    #
    # Reconfiguring the system is a higher trust level than watching it, so
    # these demand the password even when party auth is disabled. Where no
    # password exists at all, they answer only on loopback -- otherwise
    # disabling auth for guests would also hand them the spend ceiling.

    def require_operator(request: Request) -> None:
        if password:
            require_party(request)
            return
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "settings need a party password, or a request from this machine",
            )

    def _split_keys(overrides: dict) -> tuple[list[str], list[str]]:
        live, restart = [], []
        for dotted in config_store.dotted_keys(overrides):
            if dotted in config_store.LIVE_KEYS:
                live.append(dotted)
            elif dotted in config_store.RESTART_KEYS:
                restart.append(dotted)
        return sorted(live), sorted(restart)

    @app.get("/api/settings", dependencies=[Depends(require_operator)])
    async def get_settings() -> dict:
        overrides = await asyncio.to_thread(config_store.load_settings)
        return {
            "overrides": overrides,
            "live_keys": sorted(config_store.LIVE_KEYS),
            "restart_keys": sorted(config_store.RESTART_KEYS),
            "effective": state.effective_config or {},
        }

    @app.post("/api/settings", dependencies=[Depends(require_operator)])
    async def post_settings(overrides: dict) -> dict:
        try:
            # Accept the dotted form as well as the nested one. Without this a
            # hand-written {"generation.local_steps": 8} validates, reports as
            # applied, and is persisted as a key nothing ever reads.
            overrides = config_store.expand_dotted(overrides)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        try:
            await asyncio.to_thread(config_store.validate_overrides, overrides)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        live, restart = _split_keys(overrides)
        current = await asyncio.to_thread(config_store.load_settings)
        merged = config_store.deep_merge(current, overrides)
        await asyncio.to_thread(config_store.save_settings, merged)

        if live and state.settings_handler is not None:
            state.settings_handler(overrides)
        return {"applied_live": live, "restart_required": restart, "overrides": merged}

    @app.get("/api/secrets", dependencies=[Depends(require_operator)])
    async def get_secrets(request: Request) -> dict:
        # Booleans only. No branch of this handler can reach a value.
        present = await asyncio.to_thread(config_store.secrets_present)
        return {
            "present": present,
            "names": list(config_store.SECRET_NAMES),
            # The page uses this to decide whether to offer inputs at all,
            # rather than offering them and failing the write.
            "writable": _is_local(request),
        }

    @app.post("/api/secrets", dependencies=[Depends(require_operator)])
    async def post_secret(request: Request, entry: dict) -> dict:
        """Store one credential. Loopback only, and write-only.

        Deliberately stricter than the other settings routes: those can be
        reached with the party password from anywhere on the network, but a
        credential should require physical access to the machine running the
        party. There is no route that reads a value back out.
        """
        if not _is_local(request):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "keys can only be set from the machine running Egregore",
            )
        name = str(entry.get("name", ""))
        value = str(entry.get("value", ""))
        if not value.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "value is empty")
        try:
            await asyncio.to_thread(config_store.write_secret, name, value.strip())
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        present = await asyncio.to_thread(config_store.secrets_present)
        # The response says only that it is now set — never what was stored.
        return {"saved": name, "present": present, "restart_required": True}

    # -- nodes --------------------------------------------------------------
    #
    # Enrolling is open on purpose: a guest holding a phone should not have to
    # be told a password first, and that moment is the whole product. Anything
    # that *manages* a node is an operator action behind the party password.

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
        """Audio from a transmitting browser: binary 16-bit mono PCM frames.

        The socket stays open even when the node is muted or unknown — the
        audio is dropped here rather than the connection being torn down, so
        muting is instant and reversible and the phone never has to notice.

        Each frame is handled as it arrives rather than queued, so a node
        sending faster than the server can transcribe backs up its own socket
        instead of growing an unbounded buffer here.
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
                level = None
                if state.ingest_handler is not None:
                    level = await state.ingest_handler(zone, node_id, data, 16000)
                # Record the level the source actually measured, so the
                # operator's per-device meter is the phone's own signal
                # rather than a placeholder.
                state.nodes.heartbeat(node_id, level=level)
        except Exception:
            return

    @app.get("/api/monitor", dependencies=[Depends(require_operator)])
    async def get_monitor(request: Request) -> dict:
        """Live transcripts and prompts, for an operator watching their own room.

        This is the only route in the system that can return transcript text,
        and it exists because an operator cannot trust a pipeline they cannot
        see. It is therefore doubly gated: the integration layer only binds a
        provider when ``EGREGORE_MONITOR=1``, and the request must come from
        the machine running the party even when a password would otherwise be
        enough. Nothing here is stored; it reads the ring buffer that is being
        continuously evicted anyway.
        """
        if state.monitor_provider is None:
            return {
                "enabled": False,
                "why": "set EGREGORE_MONITOR=1 before starting the party to watch "
                       "transcripts; it is off by default because transcript text "
                       "otherwise never leaves the ring buffer",
                "zones": {},
            }
        if not _is_local(request):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "transcripts can only be watched from the machine running the party",
            )
        return {"enabled": True, "why": "", **state.monitor_provider()}

    @app.get("/api/audio-devices", dependencies=[Depends(require_operator)])
    async def get_audio_devices() -> dict:
        """Input devices this machine can hear, for the microphone picker."""
        return await asyncio.to_thread(_audio_devices)

    @app.get("/api/zones", dependencies=[Depends(require_operator)])
    async def get_zones() -> dict:
        effective = state.effective_config or {}
        zones = {z.get("id"): z for z in (effective.get("zones") or []) if z.get("id")}
        out = {}
        for zone in state.zone_config:
            client = state.get_config(zone) or {}
            source = zones.get(zone, {})
            out[zone] = {
                "lens_stack": client.get("lens_stack", []),
                "lens_params": client.get("lens_params", {}),
                "audio_source": client.get("audio_source", "zone"),
                "crossfade_s": client.get("crossfade_s", 2.0),
                "playback_rate": client.get("playback_rate", 1.0),
                "hold_s": client.get("hold_s", 0.0),
                "now_playing": state.now_playing.get(zone, {}),
                # What is actually in effect: party default from the preset,
                # then the zone's preset override, then anything changed live.
                # Returning only the live override made the sliders show the
                # schema's defaults rather than the party's.
                "selection": {
                    **((effective.get("weaver") or {}).get("selection") or {}),
                    **(source.get("selection") or {}),
                    **(state.zone_config[zone].get("selection") or {}),
                },
                "config_revision": state.config_revision(zone),
                "screens": sorted(state.zone_config[zone].get("screens", {})),
                "mic": source.get("mic", {}),
                "input_device": (state.input_devices or {}).get(zone),
                "screens_connected": state.screens_connected_for(zone),
            }
        return {
            "zones": out,
            "known_lenses": list(KNOWN_LENSES),
            "lens_params": LENS_PARAMS,
        }

    @app.post("/api/zones/{zone}", dependencies=[Depends(require_operator)])
    async def post_zone(zone: str, patch: dict) -> dict:
        """Change a zone's look while the party runs.

        ``lens_stack`` and ``audio_source`` reach the screens immediately.
        Microphone changes are not accepted here — the audio source is opened
        once at start-up, so pretending it can be swapped live would be a lie.
        """
        allowed = {}
        if "lens_stack" in patch:
            stack = [str(n) for n in (patch.get("lens_stack") or [])]
            unknown = [n for n in stack if n not in KNOWN_LENSES]
            if unknown:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"unknown lens {unknown!r}; known: {sorted(KNOWN_LENSES)}",
                )
            allowed["lens_stack"] = stack
        if "lens_params" in patch:
            raw = patch.get("lens_params") or {}
            if not isinstance(raw, dict):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "lens_params must be an object"
                )
            cleaned: dict[str, list[float]] = {}
            for lens, values in raw.items():
                if lens not in KNOWN_LENSES:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"unknown lens {lens!r}"
                    )
                try:
                    cleaned[lens] = [float(v) for v in list(values)[:4]]
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"{lens} parameters must be numbers",
                    ) from exc
            allowed["lens_params"] = cleaned
        if "playback_rate" in patch:
            try:
                rate = float(patch["playback_rate"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "playback_rate must be a number"
                ) from exc
            if not 0.25 <= rate <= 2.0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "playback_rate must be between 0.25 and 2.0",
                )
            allowed["playback_rate"] = rate
        if "hold_s" in patch:
            try:
                hold = float(patch["hold_s"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "hold_s must be a number"
                ) from exc
            if not 0.0 <= hold <= 300.0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "hold_s must be between 0 and 300"
                )
            allowed["hold_s"] = hold
        if "crossfade_override" in patch:
            try:
                xf = float(patch["crossfade_override"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "crossfade must be a number"
                ) from exc
            if not 0.2 <= xf <= 12.0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "crossfade must be between 0.2 and 12s"
                )
            allowed["crossfade_override"] = xf
        if "audio_source" in patch:
            source = str(patch["audio_source"])
            if source not in ("zone", "local_mic"):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "audio_source must be 'zone' or 'local_mic'",
                )
            allowed["audio_source"] = source
        if "selection" in patch:
            raw = patch.get("selection") or {}
            limits = {"salience": (0.0, 1.0), "novelty": (0.0, 1.0),
                      "recency": (0.0, 1.0), "segment_gap_s": (1.0, 60.0)}
            cleaned: dict = {}
            for key, (lo, hi) in limits.items():
                if key not in raw:
                    continue
                if raw[key] in (None, ""):
                    cleaned[key] = None          # clear the zone override
                    continue
                try:
                    v = float(raw[key])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"selection.{key} must be a number"
                    ) from exc
                if not lo <= v <= hi:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"selection.{key} must be between {lo} and {hi}",
                    )
                cleaned[key] = v
            if cleaned:
                if state.zone_settings_handler is not None:
                    try:
                        state.zone_settings_handler(zone, cleaned)
                    except ValueError as exc:
                        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
                current = dict(state.zone_config.get(zone, {}).get("selection") or {})
                for k, v in cleaned.items():
                    if v is None:
                        current.pop(k, None)
                    else:
                        current[k] = v
                allowed["selection"] = current
        if not allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "nothing changeable in that payload"
            )
        try:
            return state.set_zone_config(zone, allowed)
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"unknown zone {zone!r}"
            ) from exc

    @app.get("/api/models", dependencies=[Depends(require_operator)])
    async def get_models() -> dict:
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        return config_store.catalogue_to_json(catalogue)

    @app.post("/api/models", dependencies=[Depends(require_operator)])
    async def post_model(entry: dict) -> dict:
        payload = dict(entry)
        key = str(payload.pop("key", "")).strip()
        if not key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "model needs a key")
        try:
            model = config_store.model_from_json(payload)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        catalogue[key] = model
        await asyncio.to_thread(config_store.save_catalogue, catalogue)
        return config_store.catalogue_to_json({key: model})

    @app.delete("/api/models/{key}", dependencies=[Depends(require_operator)])
    async def delete_model(key: str) -> dict:
        if key in config_store.builtin_keys():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{key!r} ships with Egregore and cannot be deleted; override it instead",
            )
        catalogue = await asyncio.to_thread(config_store.load_catalogue)
        catalogue.pop(key, None)
        await asyncio.to_thread(config_store.save_catalogue, catalogue)
        return {"deleted": key}

    return app
