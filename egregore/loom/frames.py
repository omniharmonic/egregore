"""Last-frame extraction — the handoff mechanism behind movement continuity
(Architecture §3.2/§3.3).

Pulling the final frame of a clip via ``ffmpeg`` and handing it back as an
image-to-video seed is what lets continuity survive both the cloud
provider's chain-extension ceiling and the local backend, which has no
native extension at all. This module is deliberately narrow: one function,
one job, robust to the very short clips the mock/local backends can emit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

__all__ = ["FrameExtractionError", "extract_last_frame"]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_DEFAULT_TIMEOUT_S = 20.0


class FrameExtractionError(RuntimeError):
    """Raised when ffmpeg cannot produce a usable frame from a clip."""


async def _run_ffmpeg(args: list[str], timeout_s: float) -> tuple[bytes, bytes, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise FrameExtractionError("ffmpeg not found on PATH") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FrameExtractionError(f"ffmpeg timed out after {timeout_s:g}s") from exc
    return stdout, stderr, proc.returncode if proc.returncode is not None else -1


def _stderr_tail(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    lines = text.splitlines()
    return lines[-1] if lines else "(no ffmpeg output)"


async def extract_last_frame(clip_path: Path, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> bytes:
    """Extract the final frame of ``clip_path`` as PNG bytes.

    Primary strategy seeks near end-of-file (``-sseof -0.25``) and grabs one
    frame — fast, and correct for anything longer than a quarter second.
    Very short clips (a couple of frames, as the mock backend can produce)
    can make that seek land past the last decodable frame and yield nothing;
    when that happens this falls back to the *first* frame, which is still
    a defensible continuity seed for a clip that short and keeps the
    handoff mechanism from ever hard-failing on trivial input.

    Raises ``FrameExtractionError`` with an informative (but content-blind —
    this only ever touches media bytes and ffmpeg's own diagnostics) message
    if neither strategy produces a valid PNG.
    """
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise FrameExtractionError(f"clip not found: {clip_path}")

    last_frame_args = [
        "-y",
        "-sseof",
        "-0.25",
        "-i",
        str(clip_path),
        "-update",
        "1",
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-c:v",
        "png",
        "pipe:1",
    ]
    stdout, stderr, code = await _run_ffmpeg(last_frame_args, timeout_s)
    if code == 0 and stdout.startswith(PNG_MAGIC):
        return stdout

    # Fallback: first frame, for clips too short for the end-of-file seek.
    first_frame_args = [
        "-y",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-c:v",
        "png",
        "pipe:1",
    ]
    stdout2, stderr2, code2 = await _run_ffmpeg(first_frame_args, timeout_s)
    if code2 == 0 and stdout2.startswith(PNG_MAGIC):
        return stdout2

    detail = _stderr_tail(stderr2) if stderr2 else _stderr_tail(stderr)
    raise FrameExtractionError(
        f"ffmpeg could not extract a frame from {clip_path.name!r} "
        f"(last-frame exit={code}, first-frame exit={code2}): {detail}"
    )
