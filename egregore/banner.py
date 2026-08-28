"""Terminal banner. The whole product UX is a terminal; dress accordingly."""

from __future__ import annotations

from egregore.config.schema import EgregoreConfig

_DIM = "\033[2m"
_GRN = "\033[38;5;84m"
_RST = "\033[0m"

WORDMARK = r"""
 ███████╗ ██████╗ ██████╗ ███████╗ ██████╗  ██████╗ ██████╗ ███████╗
 ██╔════╝██╔════╝ ██╔══██╗██╔════╝██╔════╝ ██╔═══██╗██╔══██╗██╔════╝
 █████╗  ██║  ███╗██████╔╝█████╗  ██║  ███╗██║   ██║██████╔╝█████╗
 ██╔══╝  ██║   ██║██╔══██╗██╔══╝  ██║   ██║██║   ██║██╔══██╗██╔══╝
 ███████╗╚██████╔╝██║  ██║███████╗╚██████╔╝╚██████╔╝██║  ██║███████╗
 ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝
"""


def print_banner(cfg: EgregoreConfig, *, password: str | None, backends: list[str]) -> None:
    zone = cfg.zones[0].id if cfg.zones else "main"
    port = cfg.serving.port
    local = cfg.budget.total_usd == 0
    privacy = (
        "LOCAL // nothing derived from speech leaves this machine"
        if local
        else f"CLOUD-CAPABLE // abstracted prompts only // hard ceiling ${cfg.budget.total_usd}"
    )
    lines = [
        f"{_GRN}{WORDMARK}{_RST}",
        f"{_DIM} a collective dreaming engine for gathered spaces{_RST}",
        "",
        f" {_GRN}::{_RST} party    {cfg.party.name}  ({cfg.party.duration_hours}h)",
        f" {_GRN}::{_RST} zones    " + ", ".join(z.id for z in cfg.zones),
        f" {_GRN}::{_RST} ladder   " + " -> ".join(backends),
        f" {_GRN}::{_RST} privacy  {privacy}",
        "",
        f" {_GRN}>{_RST} screens  http://<this-host>:{port}/?zone={zone}",
        f" {_GRN}>{_RST} operator http://<this-host>:{port}/static/status.html",
        f" {_GRN}>{_RST} password {password if password else '(auth disabled — trusted LAN)'}",
        "",
        f"{_DIM} the room is listening. ^C ends the dream and zeroes every buffer.{_RST}",
        "",
    ]
    print("\n".join(lines))
