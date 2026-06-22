"""Account-graph device model (from GET /ris-public-api/api/v1/accounts)."""

from __future__ import annotations

import base64

from .base import ResideoBaseObject


class ResideoAccountDevice(ResideoBaseObject):
    """A device flattened from the consumer account graph by ``ResideoClient.iter_devices``.

    - ``device_id`` / ``mac`` is the raw **MAC** (e.g. ``5CFCE1B7F5BA``) — what the ``devsrv``
      device endpoints key on.
    - ``global_id`` is a base64 GraphQL global id of the form ``base64("<Type>:<id>")`` —
      e.g. decoding ``THlyaWNU…`` yields ``LyricThermostatDevice:5CFCE1B7F5BA``.
    - ``device_kind`` is that decoded ``<Type>`` and is the **reliable** device-class signal;
      ``global_device_type`` is only a model name (e.g. ``Denali_S1200``) that does *not*
      contain "thermostat", so it can't be relied on alone.
    """

    @property
    def name(self) -> str | None:
        return self.attributes.get("name")

    @property
    def location(self) -> str | None:
        return self.attributes.get("location")

    @property
    def device_id(self) -> str | None:
        return self.attributes.get("deviceId")

    # Alias — the devsrv endpoints call this the MAC.
    mac = device_id

    @property
    def global_device_type(self) -> str | None:
        """Model family, e.g. ``Denali_S1200`` (NOT a reliable device-class signal)."""
        return self.attributes.get("globalDeviceType")

    # Reads better at call sites (e.g. DeviceInfo.model).
    model = global_device_type

    @property
    def global_id(self) -> str | None:
        """The raw (base64) GraphQL global id."""
        return self.attributes.get("globalId") or self.attributes.get("id")

    @property
    def device_kind(self) -> str | None:
        """Decoded global-id ``<Type>``, e.g. ``LyricThermostatDevice`` (``None`` if undecodable)."""
        gid = self.global_id
        if not gid:
            return None
        try:
            decoded = base64.b64decode(gid + "=" * (-len(gid) % 4)).decode("utf-8", "ignore")
        except Exception:
            return None
        # decoded looks like "<Type>:<id>"
        return decoded.split(":", 1)[0] or None

    @property
    def is_thermostat(self) -> bool:
        kind = (self.device_kind or "").lower()
        gdt = (self.global_device_type or "").lower()
        return "thermostat" in kind or "thermostat" in gdt or "lyric" in gdt
