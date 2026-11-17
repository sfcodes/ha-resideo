"""Homes: real installations, each as the sanitized diagnostics download of one account.

A home is the evidence behind what the code claims about hardware its author doesn't own.
``python -m scripts.evidence add-home`` writes one; nothing here is edited by hand. See
CONTRIBUTING.md.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

HOMES = pathlib.Path(__file__).parent


def home_names() -> list[str]:
    return sorted(path.stem for path in HOMES.glob("*.json"))


def load_home(name: str) -> dict[str, Any]:
    return json.loads((HOMES / f"{name}.json").read_text())


def captured_pushes(device: dict[str, Any]) -> list[tuple[str, Any]]:
    """Every push payload one thermostat's capture holds, as ``(type, value)``, oldest first."""
    samples = [
        (sample["at"], name, sample["value"])
        for name, push in (device.get("pushes") or {}).items()
        for shape in push["shapes"]
        for sample in shape["samples"]
    ]
    return [(name, value) for _, name, value in sorted(samples, key=lambda s: s[0])]
