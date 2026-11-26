"""Offline tests of the memory bound on the objects' cells."""

import pytest

from src.resources import CELL_BYTES, EXPORT_RESERVE, CellsNeed, MemoryLimitError, cells_need, check_cells_memory, memory_limit


def test_cells_count_each_file_once():
    files = [{"hash": "a", "size": 100, "cells": 10}, {"hash": "b", "size": 50, "cells": 5}, {"hash": "a", "size": 100, "cells": 10}]
    need = cells_need(files)
    assert (need.figures, need.cells, need.file_bytes) == (2, 15, 150)
    assert need.bytes == 15 * CELL_BYTES + 2 * 150


def test_limit_from_cgroup_and_available_memory(tmp_path):
    v2, v1, meminfo = tmp_path / "memory.max", tmp_path / "limit_in_bytes", tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 64000000 kB\nMemAvailable:    8000000 kB\n")
    assert memory_limit(v2, v1, meminfo) == 8000000 * 1024
    v2.write_text("4294967296\n")
    assert memory_limit(v2, v1, meminfo) == 4294967296
    v2.write_text("max\n")
    assert memory_limit(v2, v1, meminfo) == 8000000 * 1024
    v2.unlink()
    v1.write_text(str(1 << 62))
    assert memory_limit(v2, v1, meminfo) == 8000000 * 1024
    meminfo.unlink()
    assert memory_limit(v2, v1, meminfo) is None


def test_cells_beyond_the_memory_left_are_refused():
    limit = EXPORT_RESERVE + (1 << 30)
    check_cells_memory("cloud", CellsNeed(3, (1 << 30) // CELL_BYTES - 100, 50), limit)
    check_cells_memory("cloud", CellsNeed(3, 10**12, 0), None)
    with pytest.raises(MemoryLimitError, match=r'The 3 objects of "cloud" hold 200,000,000 cells, about 1\.5 GiB'):
        check_cells_memory("cloud", CellsNeed(3, 200_000_000, 0), limit)
