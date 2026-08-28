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
    async def index() -> FileResponse:
        index_path = lens_dir / "index.html"
        if not index_path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "lens client not installed")
        return FileResponse(index_path)

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
            await _serve_ws(websocket, queue)
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
    async def get_secrets() -> dict[str, bool]:
        # Booleans only. No branch of this handler can reach a value.
        return await asyncio.to_thread(config_store.secrets_present)

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
