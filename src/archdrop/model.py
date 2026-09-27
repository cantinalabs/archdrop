"""The shapes the daemon, its backends and its clients agree on."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

_ids = itertools.count(1)


@dataclass
class Peer:
    id: str
    backend: str          # "quickshare"; more backends can be added
    name: str
    kind: str = "unknown"  # phone | tablet | laptop | unknown
    address: str = ""
    port: int = 0
    rssi: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "backend": self.backend, "name": self.name,
                "kind": self.kind, "address": self.address, "rssi": self.rssi}


# Transfer states. "pending" is an inbound transfer waiting for a person.
CONNECTING, PENDING, WAITING, ACTIVE, DONE, FAILED, REJECTED, CANCELLED = (
    "connecting", "pending", "waiting", "active", "done", "failed", "rejected", "cancelled")
FINISHED = {DONE, FAILED, REJECTED, CANCELLED}


@dataclass
class Transfer:
    backend: str
    direction: str        # "in" | "out"
    peer: str
    files: list[dict] = field(default_factory=list)   # {"name", "size"}
    kind: str = "files"   # files | text | url
    text: str = ""
    total: int = 0
    done: int = 0
    state: str = CONNECTING
    pin: str = ""
    error: str = ""
    saved: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"t{next(_ids)}")
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "backend": self.backend, "direction": self.direction,
            "peer": self.peer, "kind": self.kind, "state": self.state,
            "files": [f["name"] for f in self.files], "total": self.total,
            "done": self.done, "pin": self.pin, "error": self.error,
            "text": self.text[:500], "saved": self.saved, "created": self.created,
        }

    @property
    def finished(self) -> bool:
        return self.state in FINISHED
