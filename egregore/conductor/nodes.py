"""Who is connected to the party (spec: multi-node party).

A *node* is a browser that enrolled: a phone acting as a microphone, a screen
showing the dream, or both. The registry is pure state with an injectable
clock — no I/O, no FastAPI — so the expiry and muting rules can be tested
without a server or a wall-clock sleep.

Enrollment is deliberately open: asking someone to type a password at the
moment they walk up with a phone defeats the point. Control is exercised
afterwards instead, which is why :meth:`mute` and :meth:`kick` exist and why
:meth:`transmitters` is the only thing the audio path is allowed to consult.

Nothing here holds audio or text. ``level`` is a single float so the operator
can see a node is alive and roughly how loud it is; it is not a recording and
cannot be turned back into one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["ROLES", "Node", "NodeRegistry"]

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
        """Register or update a node.

        Idempotent per id, so a phone that reloads rejoins rather than
        appearing twice — which is why the id lives in the browser's
        localStorage rather than being assigned here.
        """
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
        """Mark a node alive.

        ``None`` for an unknown id — a node that outlived a server restart
        should be told to re-enroll, not raise inside the socket carrying
        its audio.
        """
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
        stale = [
            n.id for n in self._nodes.values() if current - n.last_seen > self.ttl_s
        ]
        for node_id in stale:
            self._nodes.pop(node_id, None)
        if stale:
            logger.info("nodes expired: %s", ", ".join(stale))
        return stale
