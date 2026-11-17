"""Evidence of what the cloud pushed and how it answered our writes, kept for diagnostics.

The API is reverse-engineered, so a claim about it is only as good as the payload behind it.
The debug log has that payload, but only while debug logging is on, and with the thermostat's
MAC in every line. This keeps a small, bounded sample in memory from the moment the
integration loads, so one diagnostics download answers "does this push type exist, and what
does it look like?" and "was this write accepted, and did the device confirm it?".
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from homeassistant.util import dt as dt_util

from .aioresideo import (
    LIVE_FEED_MERGED_PROPERTIES,
    ResideoChangeConfirm,
    ResideoCommand,
    ResideoLiveFeed,
)

# A push type can arrive in several shapes (a ``Sensor`` push from the thermostat carries
# different keys than one from a battery sensor), and in a plain "last N" buffer the common
# shape would crowd the rare one out. So samples are kept per distinct shape.
PUSH_SHAPES = 12  # most recently seen shapes kept per push type
PUSH_SAMPLES = 3  # newest payloads kept per shape
WRITE_HISTORY = 30  # newest commands + change confirmations kept per thermostat


def _now() -> str:
    return dt_util.utcnow().isoformat(timespec="milliseconds")


def _shape(value: Any) -> Any:
    """The key structure of a payload with its values dropped (hashable)."""
    if isinstance(value, dict):
        return tuple((key, _shape(value[key])) for key in sorted(value))
    if isinstance(value, list):
        return frozenset(_shape(item) for item in value)
    return None


@dataclass
class _Shape:
    count: int = 0
    samples: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=PUSH_SAMPLES))


@dataclass
class _PushType:
    first_seen: str
    last_seen: str = ""
    count: int = 0
    shapes: dict[Any, _Shape] = field(default_factory=dict)  # least recently seen first


class ResideoCapture:
    """Bounded record of the pushes received and the commands sent, per thermostat."""

    def __init__(self) -> None:
        self.since = _now()
        self._pushes: dict[str | None, dict[str, _PushType]] = {}
        self._writes: dict[str | None, deque[dict[str, Any]]] = {}
        self._merged_since_read: dict[str, int] = {}

    def record_read(self, mac: str) -> None:
        """Note that the state held for ``mac`` was just replaced by a full read of the cloud."""
        self._merged_since_read[mac] = 0

    def record_merge(self, mac: str) -> None:
        """Note that one push was merged into the state held for ``mac``."""
        self._merged_since_read[mac] = self._merged_since_read.get(mac, 0) + 1

    def merged_since_read(self, mac: str) -> int:
        """How many pushes the state held for ``mac`` has taken since its last full read.

        At zero that state is the read itself, so comparing it with the cloud says nothing
        about how pushes are merged.
        """
        return self._merged_since_read.get(mac, 0)

    def record_push(self, event: ResideoLiveFeed) -> None:
        """Count one ``LiveFeedEvent`` and keep its payload as a sample of its shape."""
        now = _now()
        push = self._pushes.setdefault(event.device_id, {}).setdefault(
            str(event.property_name), _PushType(first_seen=now)
        )
        push.last_seen = now
        push.count += 1
        key = _shape(event.value)
        shape = push.shapes.pop(key, None) or _Shape()
        push.shapes[key] = shape  # re-inserted last, so the dict stays ordered by recency
        shape.count += 1
        shape.samples.append({"at": now, "value": deepcopy(event.value)})
        if len(push.shapes) > PUSH_SHAPES:
            del push.shapes[next(iter(push.shapes))]

    def record_command(self, command: ResideoCommand) -> None:
        """Keep one command as sent, with the cloud's ``202`` body or its refusal."""
        entry: dict[str, Any] = {"at": _now(), "command": command.command, "body": command.body}
        if command.error is None:
            entry["result"] = command.result
        else:
            entry["error"] = {
                "type": type(command.error).__name__,
                "message": str(command.error),
                "status": getattr(command.error, "status", None),
                "response": getattr(command.error, "body", None),
            }
        self._history(command.mac).append(entry)

    def record_change(self, event: ResideoChangeConfirm) -> None:
        """Attach a ``ChangeRequest`` confirmation to the command whose transaction it answers.

        One that answers no command on record is kept as an entry of its own, with its
        transaction id, rather than dropped.
        """
        confirmation = {
            "at": _now(),
            "change": event.change_name,
            "direction": event.change_direction,
            "success": event.success,
        }
        history = self._history(event.device_id)
        if event.transaction_id is not None:
            for entry in reversed(history):
                result = entry.get("result")
                if isinstance(result, dict) and result.get("TransactionId") == event.transaction_id:
                    entry["confirmation"] = confirmation
                    return
        history.append({**confirmation, "transaction_id": event.transaction_id})

    def _history(self, mac: str | None) -> deque[dict[str, Any]]:
        return self._writes.setdefault(mac, deque(maxlen=WRITE_HISTORY))

    def pushes(self, mac: str) -> dict[str, Any]:
        """Every push type seen for ``mac`` by name, each shape's newest samples first."""
        return {
            name: {
                "merged_in_memory": name in LIVE_FEED_MERGED_PROPERTIES,
                "count": push.count,
                "first_seen": push.first_seen,
                "last_seen": push.last_seen,
                "shapes": [
                    {"count": shape.count, "samples": list(reversed(shape.samples))}
                    for shape in reversed(push.shapes.values())
                ],
            }
            for name, push in sorted(self._pushes.get(mac, {}).items())
        }

    def writes(self, mac: str) -> list[dict[str, Any]]:
        """The commands sent to ``mac`` and the confirmations received, oldest first."""
        return list(self._writes.get(mac, ()))
