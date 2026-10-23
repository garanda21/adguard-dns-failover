"""Block detection based on hayahora.futbol data.

Based on https://github.com/parrazam/monitor-hay-futbol: for each ISP, an IP
is blocked when its latest state change is ``true``. A block is active when any
ISP has at least ``min_ips`` blocked IPs or a blocked percentage >= threshold.

The absolute count matters because hayahora.futbol tracks thousands of IPs per
ISP: outside matches 0-6 are blocked, during a block 150-900 (only ~4-10%).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)


@dataclass
class IspStatus:
    isp: str
    total: int
    blocked: int

    @property
    def pct(self) -> float:
        return self.blocked * 100.0 / self.total if self.total else 0.0


@dataclass
class BlockStatus:
    active: bool
    isps: list[IspStatus]
    last_update: str | None
    # Requested ISPs (ISPS filter) that are not present in the data.
    unknown_isps: list[str] = field(default_factory=list)


def evaluate(payload: dict, threshold_pct: float, min_ips: int = 0,
             isps_filter: list[str] | None = None) -> BlockStatus:
    """Evaluate the payload; ``isps_filter`` limits the check to those ISPs (case-insensitive)."""
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise ValueError("Unexpected payload: missing 'data' list")

    per_isp: dict[str, IspStatus] = {}
    for entry in entries:
        isp = entry.get("isp")
        if not isp:
            continue
        changes = entry.get("stateChanges") or []
        blocked = bool(changes) and changes[-1].get("state") is True
        status = per_isp.setdefault(isp, IspStatus(isp, 0, 0))
        status.total += 1
        status.blocked += int(blocked)

    isps = sorted(per_isp.values(), key=lambda s: s.isp)
    unknown: list[str] = []
    if isps_filter:
        wanted = {name.strip().lower() for name in isps_filter if name.strip()}
        known = {s.isp.lower() for s in isps}
        unknown = sorted(name for name in wanted if name not in known)
        isps = [s for s in isps if s.isp.lower() in wanted]
    active = any(s.pct >= threshold_pct or (min_ips > 0 and s.blocked >= min_ips) for s in isps)
    return BlockStatus(active=active, isps=isps, last_update=payload.get("lastUpdate"), unknown_isps=unknown)


def check(api_url: str, threshold_pct: float, min_ips: int = 0, timeout: float = 15.0,
          isps_filter: list[str] | None = None) -> BlockStatus:
    resp = requests.get(api_url, timeout=timeout, headers={"User-Agent": "dns-blockswitch"})
    resp.raise_for_status()
    return evaluate(resp.json(), threshold_pct, min_ips, isps_filter)
