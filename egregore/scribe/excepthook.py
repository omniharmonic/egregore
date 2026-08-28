"""Privacy-scrubbing exception handler (Architecture §2.3, PRD P-2).

Transcript text lives in a ring buffer and transiently in weaver stage-1
locals. An unhandled exception is the classic way for that text to escape:
crash reporters, `rich`/IPython tracebacks, `cgitb`, and log aggregators all
happily print frame locals, and a repr of a list of fragments would put the
room's conversation into a log file forever.

So Egregore formats its own tracebacks. The default hook is not called at all;
this module walks the traceback, and for each frame prints locals with any
value that could carry transcript text replaced by a redaction marker:

* a :class:`~egregore.types.TextFragment` (already redacted in its own repr,
  belt-and-braces here),
* a :class:`~egregore.scribe.ring.RingBuffer`,
* any ``str`` longer than ``MAX_STR_CHARS`` characters,
* any container holding one of the above.

The exception's own ``args`` get the same treatment, since a long string in an
exception message is just as much of a leak as one in a local.

Everything here is defensive: a privacy hook that raises would take out the
process's last-resort error path, so every step is wrapped and the worst case
is an ugly-but-safe minimal traceback.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from types import TracebackType
from typing import Any

from egregore.types import TextFragment

logger = logging.getLogger(__name__)

__all__ = [
    "REDACTED",
    "MAX_STR_CHARS",
    "scrub_value",
    "format_exception_redacted",
    "install_privacy_excepthook",
    "privacy_asyncio_handler",
]

REDACTED = "<redacted: may contain transcript text>"
MAX_STR_CHARS = 64
MAX_LOCALS_PER_FRAME = 40


def _is_sensitive_type(value: Any) -> bool:
    # Imported here so this module stays importable even if ring.py is being
    # edited/reloaded, and to avoid an import cycle at module load.
    try:
        from egregore.scribe.ring import RingBuffer

        if isinstance(value, RingBuffer):
            return True
    except Exception:  # pragma: no cover - defensive
        pass
    return isinstance(value, TextFragment)


def scrub_value(value: Any, _depth: int = 0) -> str:
    """Return a safe repr of ``value``, or the redaction marker."""
    try:
        if _is_sensitive_type(value):
            return REDACTED
        if isinstance(value, str):
            return REDACTED if len(value) > MAX_STR_CHARS else repr(value)
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            if _depth >= 2:
                return f"<{type(value).__name__} len={len(value)}>"
            items = value.values() if isinstance(value, dict) else value
            for item in items:
                if _is_sensitive_type(item) or (
                    isinstance(item, str) and len(item) > MAX_STR_CHARS
                ):
                    return f"<{type(value).__name__} len={len(value)} redacted>"
            return f"<{type(value).__name__} len={len(value)}>"
        text = repr(value)
        return REDACTED if len(text) > 200 else text
    except Exception:  # pragma: no cover - a hostile __repr__ must not break us
        return "<unreprable>"


def _scrub_exception_args(exc: BaseException) -> None:
    """Replace long/sensitive strings in an exception's args, in place."""
    try:
        args = getattr(exc, "args", ())
        if not args:
            return
        new = []
        changed = False
        for arg in args:
            if _is_sensitive_type(arg) or (isinstance(arg, str) and len(arg) > MAX_STR_CHARS):
                new.append(REDACTED)
                changed = True
            else:
                new.append(arg)
        if changed:
            exc.args = tuple(new)
    except Exception:  # pragma: no cover - defensive
        pass


def format_exception_redacted(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
    *,
    include_locals: bool = True,
) -> str:
    """Format a traceback with frame locals scrubbed.

    Never raises: on any internal failure it degrades to the exception type
    name alone, which is always safe to print.
    """
    try:
        if exc is None:
            return "<no exception>\n"
        _scrub_exception_args(exc)
        lines: list[str] = ["Traceback (most recent call last) [egregore: locals redacted]:\n"]
        seen: set[int] = set()
        current: BaseException | None = exc
        current_tb = tb
        while current is not None:
            for frame_summary in _walk(current_tb, include_locals):
                lines.append(frame_summary)
            lines.extend(
                traceback.format_exception_only(type(current), current)  # already scrubbed args
            )
            nxt = current.__cause__ or current.__context__
            if nxt is None or id(nxt) in seen:
                break
            seen.add(id(current))
            lines.append("\nThe above exception was the direct cause of:\n\n")
            current, current_tb = nxt, nxt.__traceback__
        return "".join(lines)
    except Exception:  # pragma: no cover - last resort
        name = getattr(exc_type, "__name__", "Exception")
        return f"{name}: <traceback formatting failed; content withheld>\n"


def _walk(tb: TracebackType | None, include_locals: bool) -> list[str]:
    out: list[str] = []
    while tb is not None:
        frame = tb.tb_frame
        code = frame.f_code
        out.append(f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_name}\n')
        line = _source_line(code.co_filename, tb.tb_lineno)
        if line:
            out.append(f"    {line}\n")
        if include_locals:
            out.extend(_format_locals(frame.f_locals))
        tb = tb.tb_next
    return out


def _source_line(filename: str, lineno: int) -> str | None:
    try:
        import linecache

        return linecache.getline(filename, lineno).strip() or None
    except Exception:  # pragma: no cover - defensive
        return None


def _format_locals(f_locals: dict[str, Any]) -> list[str]:
    out: list[str] = []
    try:
        names = sorted(f_locals)[:MAX_LOCALS_PER_FRAME]
        for name in names:
            out.append(f"      {name} = {scrub_value(f_locals[name])}\n")
        if len(f_locals) > MAX_LOCALS_PER_FRAME:
            out.append(f"      ... {len(f_locals) - MAX_LOCALS_PER_FRAME} more local(s)\n")
    except Exception:  # pragma: no cover - defensive
        out.append("      <locals unavailable>\n")
    return out


def install_privacy_excepthook(*, include_locals: bool = True) -> None:
    """Install the scrubbing hook on ``sys.excepthook`` and ``threading.excepthook``.

    Idempotent, and safe to call before or after any other module's imports.
    The default hook is replaced, not wrapped, so no unscrubbed traceback is
    ever produced.
    """

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        try:
            text = format_exception_redacted(exc_type, exc, tb, include_locals=include_locals)
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:  # pragma: no cover - the hook must never raise
            try:
                sys.stderr.write(f"{getattr(exc_type, '__name__', 'Exception')}: <redacted>\n")
            except Exception:
                pass

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        _hook(args.exc_type, args.exc_value, args.exc_traceback)

    _hook.__egregore_privacy_hook__ = True  # type: ignore[attr-defined]
    sys.excepthook = _hook
    threading.excepthook = _thread_hook  # type: ignore[assignment]


def privacy_asyncio_handler(loop: Any, context: dict[str, Any]) -> None:
    """asyncio exception handler counterpart.

    Wire it up with ``loop.set_exception_handler(privacy_asyncio_handler)``.
    Logs the handler's message plus a scrubbed traceback, and nothing else
    from ``context`` — the default handler dumps the failing task's repr,
    which for a scribe task can include buffered text.
    """
    try:
        message = context.get("message") or "unhandled exception in asyncio task"
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            detail = format_exception_redacted(type(exc), exc, exc.__traceback__)
            logger.error("%s\n%s", message, detail)
        else:
            logger.error("%s", message)
    except Exception:  # pragma: no cover - the handler must never raise
        try:
            logger.error("unhandled asyncio exception <redacted>")
        except Exception:
            pass
