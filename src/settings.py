"""Export options from the modal window, parsed once at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Dict

from src.labels import parse_overrides

FORMATS = ("laz", "las", "ply")
DEFAULT_FORMAT = "laz"


@dataclass(frozen=True)
class Settings:
    format: str = DEFAULT_FORMAT
    # Class index by class name, over the indices of the class descriptions and the project's order.
    overrides: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        fmt = (env.get("modal.state.format") or DEFAULT_FORMAT).strip().lower()
        if fmt not in FORMATS:
            raise ValueError(f"modal.state.format must be one of {', '.join(FORMATS)}, got {fmt!r}")
        return cls(format=fmt, overrides=parse_overrides(env.get("modal.state.classIndices")))
