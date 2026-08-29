"""Content-addressed clip store (Architecture §2.6).

Clips are named by the sha256 of their own bytes, so the same rendered file
is always the same clip id no matter which backend produced it or how often
it is stored. Nothing about *why* a clip exists is kept here: no prompt, no
theme object, no transcript-derived text of any kind ever reaches this
module. `ClipRef` is content-blind by construction (types.py) and the store
adds nothing to it.

`wipe()` is the post-party cleanup path for PRD success criterion 6 —
"nothing was retained afterward except the generated video sequence itself",
and when the operator does not even want that, this removes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path

from egregore.types import ClipRef

log = logging.getLogger(__name__)

#: Read size for hashing. Clips are a few hundred KB to a few MB.
_CHUNK_BYTES = 1 << 20

#: Length of the hex prefix used as the clip id. 16 hex chars = 64 bits,
#: which is far more than enough for one night's few thousand clips.
ID_CHARS = 16

CLIP_SUFFIX = ".mp4"

#: Backends render into this subdirectory so the final move is a same-device
#: rename rather than a copy.
_INCOMING = ".incoming"
#: Provenance of every stored clip, so a fresh process can pick the pool
#: back up. Holds ids, durations, zones, backends and timestamps — never a
#: prompt and never any text.
_INDEX = "index.json"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()[:ID_CHARS]


def _move(src: Path, dest: Path) -> int:
    """Move `src` onto `dest` and return the resulting size in bytes."""
    shutil.move(str(src), str(dest))
    return dest.stat().st_size


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class ClipStore:
    """Content-addressed store for generated clips.

    The index lives in memory: the store is rebuilt from scratch each run,
    which is deliberate — a party does not inherit the previous party's
    material unless the operator points it at the same directory and the
    Loom chooses to rescan it.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.incoming_dir = self.dir / _INCOMING
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, ClipRef] = {}
        self._lock = asyncio.Lock()
        self._load_index()

    # -- persistence ---------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.dir / _INDEX

    def _load_index(self) -> None:
        """Rebuild the in-memory index from the sidecar, dropping any entry
        whose file is no longer there."""
        try:
            raw = json.loads(self.index_path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("clip index unreadable (%s); starting empty", type(exc).__name__)
            return
        for entry in raw if isinstance(raw, list) else []:
            try:
                path = self.path_for(str(entry["id"]))
                if not path.is_file():
                    continue
                self._index[str(entry["id"])] = ClipRef(
                    id=str(entry["id"]),
                    path=path,
                    duration_s=float(entry["duration_s"]),
                    zone=str(entry["zone"]),
                    backend=str(entry["backend"]),
                    tier=str(entry["tier"]),
                    created_at=float(entry.get("created_at", 0.0)),
                    movement_id=entry.get("movement_id"),
                    chain_index=int(entry.get("chain_index", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
        if self._index:
            log.info("clip store resumed clips=%d dir=%s", len(self._index), self.dir)

    def _save_index(self) -> None:
        rows = [
            {
                "id": r.id, "duration_s": r.duration_s, "zone": r.zone,
                "backend": r.backend, "tier": r.tier, "created_at": r.created_at,
                "movement_id": r.movement_id, "chain_index": r.chain_index,
            }
            for r in self._index.values()
        ]
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(self.index_path)

    # -- paths -------------------------------------------------------------

    def path_for(self, clip_id: str) -> Path:
        """Where a clip with this id lives, whether or not it exists yet."""
        return self.dir / f"{clip_id}{CLIP_SUFFIX}"

    def temp_path(self, suffix: str = CLIP_SUFFIX) -> Path:
        """A scratch path on the store's own filesystem, for a backend to
        render or download into before handing it to `put`."""
        return self.incoming_dir / f"{uuid.uuid4().hex}{suffix}"

    # -- store -------------------------------------------------------------

    async def put(
        self,
        tmp_path: str | Path,
        *,
        duration_s: float,
        zone: str,
        backend: str,
        tier: str,
        movement_id: str | None = None,
        chain_index: int = 0,
    ) -> ClipRef:
        """Hash `tmp_path`, move it into the store, and return its `ClipRef`.

        If a clip with identical bytes is already stored, the incoming file
        is discarded and the *existing* ref is returned. That is what
        content addressing means: identical bytes are the same clip, so the
        playlist can never end up holding it twice.
        """
        tmp_path = Path(tmp_path)
        clip_id = await asyncio.to_thread(_hash_file, tmp_path)
        dest = self.path_for(clip_id)

        async with self._lock:
            existing = self._index.get(clip_id)
            if existing is not None:
                await asyncio.to_thread(_unlink, tmp_path)
                log.debug("clip already stored id=%s zone=%s", clip_id, zone)
                return existing

            size = await asyncio.to_thread(_move, tmp_path, dest)
            ref = ClipRef(
                id=clip_id,
                path=dest,
                duration_s=float(duration_s),
                zone=zone,
                backend=backend,
                tier=tier,
                movement_id=movement_id,
                chain_index=chain_index,
            )
            self._index[clip_id] = ref
            await asyncio.to_thread(self._save_index)

        # Content-blind: id, provenance and size only.
        log.info(
            "clip stored id=%s zone=%s backend=%s tier=%s duration=%.1fs bytes=%d",
            clip_id,
            zone,
            backend,
            tier,
            ref.duration_s,
            size,
        )
        return ref

    def get(self, clip_id: str) -> ClipRef | None:
        return self._index.get(clip_id)

    def all(self) -> list[ClipRef]:
        """Every stored clip, oldest first."""
        return sorted(self._index.values(), key=lambda ref: ref.created_at)

    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, clip_id: object) -> bool:
        return clip_id in self._index

    # -- teardown ----------------------------------------------------------

    def wipe(self) -> int:
        """Delete every clip file and clear the index. Returns the count.

        Sweeps the directory as well as the index so an interrupted run
        leaves nothing behind.
        """
        removed = 0
        for ref in list(self._index.values()):
            if ref.path.exists():
                _unlink(ref.path)
                removed += 1
        self._index.clear()

        for stray in self.dir.glob(f"*{CLIP_SUFFIX}"):
            _unlink(stray)
            removed += 1
        for stray in self.incoming_dir.iterdir():
            if stray.is_file():
                _unlink(stray)
        _unlink(self.index_path)

        log.info("clip store wiped files=%d dir=%s", removed, self.dir)
        return removed
