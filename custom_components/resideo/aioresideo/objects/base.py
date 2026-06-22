"""Base class for aioresideo data models — thin, read-only wrappers over API JSON."""

from __future__ import annotations

from typing import Any


class ResideoBaseObject:
    """Wraps a raw API ``dict`` and exposes typed ``@property`` accessors in subclasses."""

    def __init__(self, attributes: dict[str, Any] | None = None) -> None:
        self.attributes: dict[str, Any] = attributes or {}

    def _get(self, *path: str, default: Any = None) -> Any:
        """Safely walk a nested key path, returning ``default`` if any hop is missing."""
        cur: Any = self.attributes
        for key in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
            if cur is None:
                return default
        return cur

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.attributes!r}>"
