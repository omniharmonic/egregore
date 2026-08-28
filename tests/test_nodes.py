"""Node registry — who is connected, and who is allowed to be heard."""

from __future__ import annotations

import json

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


def test_enroll_rejects_an_empty_id():
    reg = NodeRegistry(clock=FakeClock())
    with pytest.raises(ValueError, match="id"):
        reg.enroll("   ", label="x", zone="main", role="both")


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


def test_mute_and_kick_of_an_unknown_node_report_rather_than_raise():
    reg = NodeRegistry(clock=FakeClock())
    assert reg.mute("ghost", True) is None
    assert reg.kick("ghost") is False


def test_wire_shape_is_json_safe_and_carries_no_audio():
    reg = NodeRegistry(clock=FakeClock())
    reg.enroll("n1", label="phone", zone="k", role="transmit")
    reg.heartbeat("n1", level=0.5)
    row = reg.all()[0].as_wire()
    json.dumps(row)
    assert set(row) == {
        "id", "label", "zone", "role", "muted", "level", "enrolled_at", "last_seen"
    }
