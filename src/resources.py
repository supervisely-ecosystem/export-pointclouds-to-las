"""Whether the cells of a streamed cloud's objects fit in the task's memory, checked before any cell file is read.

The engine holds every cell of every exported object decoded at once (8 bytes a cell), next to the cell files
themselves (held by the app and copied once into the engine), on top of its own working set: about 1.4 GiB with 8
threads and 8 readers whatever the size of the cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CELL_BYTES = 8
# The export's own working set (1.35 GiB measured on a 728M-point cloud) plus headroom for the Python side.
EXPORT_RESERVE = 2 << 30
CGROUP_V2 = Path("/sys/fs/cgroup/memory.max")
CGROUP_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
MEMINFO = Path("/proc/meminfo")
# cgroup v1 reports "no limit" as a huge number.
NO_LIMIT = 1 << 60


class MemoryLimitError(RuntimeError):
    """The objects' cells would not fit in the task's memory: the message says how much they need."""


@dataclass(frozen=True)
class CellsNeed:
    figures: int
    cells: int
    file_bytes: int

    @property
    def bytes(self) -> int:
        return self.cells * CELL_BYTES + 2 * self.file_bytes


def cells_need(files: Iterable[Mapping[str, Any]]) -> CellsNeed:
    """The cells and file bytes of figure cell file pointers (`{hash, size, cells}`), each file counted once."""
    unique = {f["hash"]: f for f in files}
    return CellsNeed(
        figures=len(unique),
        cells=sum(int(f.get("cells") or 0) for f in unique.values()),
        file_bytes=sum(int(f.get("size") or 0) for f in unique.values()),
    )


def memory_limit(cgroup_v2: Path = CGROUP_V2, cgroup_v1: Path = CGROUP_V1, meminfo: Path = MEMINFO) -> Optional[int]:
    """The memory the task may use in bytes: its cgroup's limit, or the memory the host has available when that is
    smaller or there is no limit; None when neither is readable."""
    limits = []
    for path in (cgroup_v2, cgroup_v1):
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if text.isdigit() and int(text) < NO_LIMIT:
            limits.append(int(text))
        break
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                limits.append(int(line.split()[1]) * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return min(limits) if limits else None


def check_cells_memory(name: str, need: CellsNeed, limit: Optional[int]) -> None:
    """Refuses an export whose cells would take more than the task's memory leaves beside the export itself."""
    if limit is None or need.bytes + EXPORT_RESERVE <= limit:
        return
    gib = 1 << 30
    raise MemoryLimitError(
        f'The {need.figures} objects of "{name}" hold {need.cells:,} cells, about {need.bytes / gib:.1f} GiB once read, '
        f"and the export needs about {EXPORT_RESERVE / gib:.0f} GiB besides; the task may use {limit / gib:.1f} GiB. "
        f"Run the export on an agent with more memory"
    )
