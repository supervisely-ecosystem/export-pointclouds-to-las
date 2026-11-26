"""A cloud file through the app's export with the platform's engine (skipped without the private wheel): every point
written once with the class and instance of the figure updated last among those holding it."""

import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

spc = pytest.importorskip("supervisely_pointcloud")

from src.export import describe, export_cloud  # noqa: E402
from src.labels import class_labels  # noqa: E402

PLY_TYPES = {"double": "d", "float": "f", "uint": "I", "int": "i", "ushort": "H", "short": "h", "uchar": "B", "char": "b"}


def write_pcd(path: Path, xyz: np.ndarray, intensity: np.ndarray) -> None:
    n = len(xyz)
    header = (
        "VERSION 0.7\nFIELDS x y z intensity\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\n"
        f"WIDTH {n}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\nDATA binary\n"
    )
    rows = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("i", "<f4")])
    rows["x"], rows["y"], rows["z"], rows["i"] = xyz[:, 0], xyz[:, 1], xyz[:, 2], intensity
    path.write_bytes(header.encode() + rows.tobytes())


def read_ply(path: Path):
    data = path.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    count, properties = 0, []
    for line in data[:end].decode().splitlines():
        parts = line.split()
        if parts[:2] == ["element", "vertex"]:
            count = int(parts[2])
        elif parts[0] == "property":
            properties.append((parts[1], parts[2]))
    dtype = np.dtype([(name, "<" + PLY_TYPES[kind]) for kind, name in properties])
    assert len(data) - end == count * dtype.itemsize
    return np.frombuffer(data, dtype, count, end)


class FakeApi:
    def __init__(self, annotation: dict, source: Path):
        self.pointcloud = NS(
            annotation=NS(download=lambda id: annotation),
            download_path=lambda id, path: shutil.copy(source, path),
        )


def figure(id, object_id, updated, indices):
    return {"id": id, "objectId": object_id, "geometryType": "point_cloud", "updatedAt": updated, "geometry": {"indices": indices}}


def test_cloud_file_export_labels_every_point(tmp_path):
    n = 2000
    rng = np.random.default_rng(3)
    # Positions on a 1 cm grid, all distinct: a written point is found again by its rounded position.
    grid = rng.choice(300 * 300 * 50, size=n, replace=False)
    xyz = np.stack([grid % 300, grid // 300 % 300, grid // 90000], axis=1).astype(np.float32) / 100
    source = tmp_path / "small.pcd"
    write_pcd(source, xyz, rng.uniform(0, 1, n).astype(np.float32))

    classes = [
        NS(id=1, name="floor", description="1 · semantic: one object per cloud", shape="point_cloud", settings={}),
        NS(id=2, name="pallet", description="21 · instance: one object per instance", shape="point_cloud", settings={}),
    ]
    labels = class_labels(classes, {})
    annotation = {
        "objects": [
            {"id": 10, "classId": 2, "createdAt": "2026-01-01T00:00:01Z", "updatedAt": "2026-01-01T00:00:01Z"},
            {"id": 11, "classId": 1, "createdAt": "2026-01-01T00:00:02Z", "updatedAt": "2026-01-01T00:00:02Z"},
            {"id": 12, "classId": 2, "createdAt": "2026-01-01T00:00:03Z", "updatedAt": "2026-01-01T00:00:03Z"},
        ],
        "figures": [
            figure(100, 11, "2026-01-01T00:00:20Z", list(range(400, 1200))),
            figure(101, 10, "2026-01-01T00:00:10Z", list(range(0, 500))),
            figure(102, 12, "2026-01-01T00:00:30Z", list(range(1100, 1300))),
        ],
    }
    expected = np.zeros((n, 2), np.int64)
    expected[0:500] = (21, 1)
    expected[400:1200] = (1, 0)
    expected[1100:1300] = (21, 2)

    out = tmp_path / "small.ply"
    result = export_cloud(FakeApi(annotation, source), NS(id=7, name="small.pcd", meta={}), labels, "ply", out, tmp_path)
    rows = read_ply(out)
    assert len(rows) == n
    index_of = {tuple(p): i for i, p in enumerate(np.round(xyz.astype(np.float64) * 100).astype(np.int64).tolist())}
    written = np.round(np.stack([rows["x"], rows["y"], rows["z"]], axis=1) * 100).astype(np.int64).tolist()
    order = [index_of[tuple(p)] for p in written]
    assert sorted(order) == list(range(n))
    got = np.zeros((n, 2), np.int64)
    got[order, 0] = rows["classification"]
    got[order, 1] = rows["instance_id"]
    assert (got == expected).all()

    report = describe(result, labels, NS(id=7, name="small.pcd"))
    assert report["points"] == n and report["streamed"] is False
    assert report["labeledPoints"] == 1300 and report["overlapPoints"] == 100 + 100
    assert report["classes"] == [
        {"class": 0, "names": ["unlabeled"], "points": 700},
        {"class": 1, "names": ["floor"], "points": 700},
        {"class": 21, "names": ["pallet"], "points": 600},
    ]
    assert [(o["figureId"], o["instance"], o["points"]) for o in report["objects"]] == [(101, 1, 400), (100, 0, 700), (102, 2, 200)]
    assert not list(tmp_path.glob("cloud-*")), "the downloaded file and its pack are removed"

    with pytest.raises(spc.ExportError):
        export_cloud(FakeApi(annotation, source), NS(id=7, name="small.pcd", meta={}), labels, "ply", out, tmp_path)
    assert out.read_bytes()[:3] == b"ply", "an existing file is kept"
