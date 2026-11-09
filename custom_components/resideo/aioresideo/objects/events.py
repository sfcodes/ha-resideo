"""Event models for the SignalR live stream (see ``resideo-api-spec.md`` §9).

The data-sync hub invokes the client method ``events`` with one JSON string per notification.
Two families behave very differently:
  - ``LiveFeedEvent``       — real-time device state **with values** (``Body.PropertyName`` /
                              ``Body.Value`` / ``Body.SubscriptionExpiration``).
  - ``ChangeRequestSuccess`` / ``ChangeRequestFailure`` — command-dispatch confirmations
                              (``Body.TransactionId`` matches a REST write's ``202``); no values.
  - ``WaterFlow`` / ``ActuatorValve`` — **shutoff valve** notifications. Unlike thermostat
                              LiveFeed they are flat: ``Body`` carries the values directly (flow,
                              total, temperature, pressure / valve position).

:func:`parse_event` turns a raw frame argument (a JSON string or already-decoded dict) into a
typed event, or ``None`` for input we don't model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResideoLiveFeed:
    """A ``LiveFeedEvent`` — pushed device state with values (spec §9b)."""

    device_id: str | None
    property_name: str | None
    value: Any
    subscription_expiration: str | None = None


@dataclass(frozen=True)
class ResideoChangeConfirm:
    """A ``ChangeRequestSuccess`` / ``ChangeRequestFailure`` — a command dispatched (spec §9a)."""

    device_id: str | None
    transaction_id: str | None
    change_name: str | None
    change_direction: str | None
    success: bool


@dataclass(frozen=True)
class ResideoWaterFlow:
    """A shutoff valve ``WaterFlow`` reading (about every 10 s). Litres, L/min, °F, psi."""

    device_id: str | None
    flow_rate: float | None
    water_total: float | None
    temperature: float | None
    pressure: float | None


@dataclass(frozen=True)
class ResideoValveStatus:
    """A shutoff valve ``ActuatorValve`` position change.

    ``status`` is the raw ``Body.ValveStatus`` (``opening`` / ``open`` / ``closing`` / ``close``);
    ``command_source`` is who moved it (``app``, ...).
    """

    device_id: str | None
    status: str | None
    command_source: str | None = None


ResideoEvent = ResideoLiveFeed | ResideoChangeConfirm | ResideoWaterFlow | ResideoValveStatus


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_event(arg: str | dict[str, Any]) -> ResideoEvent | None:
    """Parse one ``events`` argument into a typed event (``None`` if unparseable/unmodeled)."""
    if isinstance(arg, str):
        try:
            notif = json.loads(arg)
        except (ValueError, TypeError):
            return None
    else:
        notif = arg
    if not isinstance(notif, dict):
        return None

    notif_type = notif.get("NotificationType")
    body = notif.get("Body") or {}
    device_id = notif.get("DeviceId")

    if notif_type == "LiveFeedEvent":
        return ResideoLiveFeed(
            device_id=device_id,
            property_name=body.get("PropertyName"),
            value=body.get("Value"),
            subscription_expiration=body.get("SubscriptionExpiration"),
        )
    if notif_type == "WaterFlow":
        return ResideoWaterFlow(
            device_id=device_id,
            flow_rate=_num(body.get("FlowRate")),
            water_total=_num(body.get("WaterTotal")),
            temperature=_num(body.get("Temperature")),
            pressure=_num(body.get("Pressure")),
        )
    if notif_type == "ActuatorValve":
        return ResideoValveStatus(
            device_id=device_id,
            status=body.get("ValveStatus"),
            command_source=body.get("CommandSource"),
        )
    if notif_type in ("ChangeRequestSuccess", "ChangeRequestFailure"):
        return ResideoChangeConfirm(
            device_id=device_id,
            transaction_id=body.get("TransactionId"),
            change_name=body.get("ChangeName"),
            change_direction=body.get("ChangeDirection"),
            success=notif_type == "ChangeRequestSuccess",
        )
    return None
