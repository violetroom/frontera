"""Choose an endpoint for a ticket, decide promotion, and pick reviewers.

Pure functions over Config, Ticket and EndpointState; no I/O. See docs/ROUTING.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import REVIEW_LANE, Config, Endpoint, Launch
from .models import Ticket
from .state import EndpointState


class WaitReason(StrEnum):
    AWAITING_COMMAND = "awaiting_command"  # next usable endpoint needs an owner command
    BUSY = "busy"  # every usable endpoint is cooling or at capacity
    NO_ENDPOINT = "no_endpoint"  # nothing in the lane is enabled for this project
    NO_LANE = "no_lane"


@dataclass(frozen=True)
class Route:
    lane: str
    endpoint: Endpoint | None = None
    wait: WaitReason | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.endpoint is not None


def _candidates(names, cfg: Config, project: str) -> list[Endpoint]:
    eps = [cfg.endpoints[n] for n in names]
    return [e for e in eps if e.launch != Launch.DISABLED and e.allows_project(project)]


def _pick(lane: str, eps: list[Endpoint], state: EndpointState, owner_command: str | None) -> Route:
    if not eps:
        return Route(lane, wait=WaitReason.NO_ENDPOINT, detail="no enabled endpoint for this project")
    busy = []
    for ep in eps:
        if state.is_cooling(ep.name):
            busy.append(f"{ep.name} cooling")
            continue
        if state.active_runs(ep.name) >= ep.max_concurrent:
            busy.append(f"{ep.name} at capacity")
            continue
        if ep.launch == Launch.ON_COMMAND and not owner_command:
            return Route(lane, wait=WaitReason.AWAITING_COMMAND, detail=f"{ep.name} runs only on your command")
        return Route(lane, endpoint=ep)
    return Route(lane, wait=WaitReason.BUSY, detail=", ".join(busy))


def route(ticket: Ticket, cfg: Config, state: EndpointState, owner_command: str | None = None,
          endpoint: str | None = None) -> Route:
    """Resolve the endpoint for a ticket's current lane.

    ``endpoint`` (or the ticket's Pin Endpoint) forces one endpoint, still subject
    to its launch policy, project allowlist, cooling and capacity.
    """
    lane = cfg.lanes.get(ticket.lane)
    if lane is None:
        return Route(ticket.lane, wait=WaitReason.NO_LANE, detail=f"no lane {ticket.lane} in config")
    forced = endpoint or ticket.pin_endpoint
    if forced:
        if forced not in cfg.endpoints:
            return Route(lane.name, wait=WaitReason.NO_ENDPOINT, detail=f"unknown endpoint {forced}")
        return _pick(lane.name, _candidates([forced], cfg, ticket.project), state, owner_command)
    return _pick(lane.name, _candidates(lane.endpoints, cfg, ticket.project), state, owner_command)


def promotion_target(ticket: Ticket, cfg: Config) -> str | None:
    """The lane a ticket moves to when its current lane gives up, or None for Needs Decision."""
    lane = cfg.lanes.get(ticket.lane)
    return lane.promote_to if lane else None


def route_review(ticket: Ticket, cfg: Config, state: EndpointState, owner_command: str | None = None) -> tuple[Route, bool]:
    """Pick a reviewer, preferring one that did not author the PR.

    Returns (route, self_review). self_review is True when only the author's
    endpoint is available, which the dispatcher flags for closer human review.
    """
    lane = cfg.lanes.get(REVIEW_LANE)
    if lane is None:
        return Route(REVIEW_LANE, wait=WaitReason.NO_LANE, detail="no review lane in config"), False
    names = list(dict.fromkeys(lane.endpoints + lane.fallback))
    eps = _candidates(names, cfg, ticket.project)
    others = [e for e in eps if e.name != ticket.last_endpoint]
    if others:
        return _pick(REVIEW_LANE, others, state, owner_command), False
    r = _pick(REVIEW_LANE, eps, state, owner_command)
    return r, r.ok
