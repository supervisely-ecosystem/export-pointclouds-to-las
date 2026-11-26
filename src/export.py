"""One point cloud to one labelled file through the platform's point cloud engine (`supervisely_pointcloud`).

A streamed (octree) cloud is read where the platform serves it, with HTTP Range requests as the user who runs the
app, and its `point_cloud_octree` figures label it through their cell files. A point cloud file (PCD) is downloaded,
packed by the engine on the local disk with its exact positions, and labelled by the point indices of its
`point_cloud` figures, the numbering the labeling tool gives its points. Either way the engine streams every point,
at full resolution, into the output file.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import signal
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as WaitTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import requests
import supervisely as sly
import supervisely_pointcloud as spc

from src.labels import ClassLabel, FigurePlan, class_counts, plan_figures
from src.resources import cells_need, check_cells_memory, memory_limit
from src.settings import FORMATS

REQUEST_TIMEOUT = (30, 600)
# Retries of a read span an API restart: dropped connections, 429 and 5xx answers are retried 1, 2, 4, … 30 s apart
# for up to 10 minutes.
RETRY_SECONDS = 600
RETRY_MAX_DELAY = 30
CELL_DOWNLOADS = 8
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/\d+")
FILE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
POINT_CLOUD_EXTENSIONS = (".pcd", ".ply", ".las", ".laz")


class ExportError(RuntimeError):
    """A point cloud cannot be exported: the message says why."""


def session_for(api: sly.Api) -> requests.Session:
    session = requests.Session()
    session.headers.update(api.headers)
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def api_url(api: sly.Api, path: str) -> str:
    """A v3 API path; `api_server_address` is the address the task reaches the API at (in-cluster on Kubernetes)."""
    return f"{api.api_server_address.rstrip('/')}/v3/{path}"


def get(session: requests.Session, url: str, what: str, headers: Optional[dict] = None) -> requests.Response:
    """GET, retrying dropped connections, 429 and server errors with backoff for up to `RETRY_SECONDS`."""
    deadline = time.monotonic() + RETRY_SECONDS
    delay = 1.0
    while True:
        try:
            response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code < 500 and response.status_code != 429:
                break
            error = f"HTTP {response.status_code}"
        except requests.RequestException as e:
            error = str(e)
        if time.monotonic() + delay > deadline:
            raise ExportError(f"Reading {what} failed for {RETRY_SECONDS // 60} minutes, last: {error}")
        sly.logger.warning(f"Reading {what} failed ({error}); retrying in {delay:.0f} s")
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_DELAY)
    if response.status_code >= 400:
        try:
            details = response.json()
            message = (details.get("details") or {}).get("message") or details.get("error") or ""
        except ValueError:
            message = ""
        raise ExportError(f"Reading {what} failed: HTTP {response.status_code} {message}".strip())
    return response


class ServedArtifacts:
    """The `read(name, offset, length)` of the engine over the artifacts the platform serves for a streamed cloud,
    bound to one tree revision. Called from the engine's reader threads at once."""

    def __init__(self, api: sly.Api, cloud_id: int, tree_revision: str):
        self.base = api_url(api, f"point-clouds.octree/{int(cloud_id)}/{tree_revision}/")
        self.session = session_for(api)
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes = 0

    def read(self, name: str, offset: int, length: Optional[int]) -> bytes:
        if not FILE_NAME.match(name):
            raise ExportError(f"{name!r} is not an artifact of the point cloud")
        what = f"{name} of the point cloud"
        if length is None:
            data = get(self.session, self.base + name, what).content
        else:
            end = offset + length - 1
            response = get(self.session, self.base + name, what, {"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity"})
            match = CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not match or (int(match.group(1)), int(match.group(2))) != (offset, end):
                raise ExportError(f"Reading bytes {offset}-{end} of {what} returned HTTP {response.status_code} without that range")
            data = response.content
            if len(data) != length:
                raise ExportError(f"Reading bytes {offset}-{end} of {what} returned {len(data)} bytes")
        with self.lock:
            self.requests += 1
            self.bytes += len(data)
        return data


def download_cells(api: sly.Api, cloud_id: int, tree_revision: str, files: List[Mapping[str, Any]]) -> Dict[str, bytes]:
    """The cell file of every figure pointer, by hash, checked against its SHA-256."""
    session = session_for(api)
    base = api_url(api, f"point-clouds.octree.labels/{int(cloud_id)}/{tree_revision}/files/")

    def fetch(file: Mapping[str, Any]) -> tuple:
        digest = file["hash"]
        data = get(session, base + digest, f"the cells of an object ({digest[:12]}…)").content
        if hashlib.sha256(data).hexdigest() != digest:
            raise ExportError(f"The cell file {digest} does not hash to its name")
        return digest, data

    unique = list({f["hash"]: f for f in files}.values())
    with ThreadPoolExecutor(max_workers=CELL_DOWNLOADS) as pool:
        return dict(pool.map(fetch, unique))


@dataclass
class Result:
    path: Path
    summary: Dict[str, Any]
    plan: FigurePlan
    streamed: bool


def run_engine(job: Callable[[spc.CancelToken], Dict[str, Any]]) -> Dict[str, Any]:
    """Runs an engine call in a worker thread so that a stop request (SIGTERM / SIGINT, delivered to the main thread)
    cancels it: the engine then removes its partial output. A Docker agent's Stop kills the process outright instead,
    so no cancel runs there and the partial file stays in the task's folder."""
    token = spc.CancelToken()
    previous = {}

    def stop(signum, frame):
        sly.logger.warning("Stop requested: cancelling the export")
        token.cancel()

    on_main = threading.current_thread() is threading.main_thread()
    if on_main:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(job, token)
            while True:
                try:
                    return future.result(timeout=0.5)
                except WaitTimeout:
                    continue
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def progress_bar(name: str, total: int):
    bar = sly.tqdm_sly(desc=f"Exporting {name}", total=total, unit="points", unit_scale=True, unit_divisor=1000)
    state = {"done": 0}

    def report(stage: str, done: int, total: int) -> None:
        if done > state["done"]:
            bar.update(done - state["done"])
            state["done"] = done

    return bar, report


def export_cloud(
    api: sly.Api,
    cloud: Any,
    labels: Mapping[int, ClassLabel],
    fmt: str,
    output: Path,
    work_dir: Path,
    threads: Optional[int] = None,
    readers: int = 8,
) -> Result:
    """Writes `cloud` (`api.pointcloud.get_info_by_id`) with its labels as `output` in `fmt`."""
    if fmt not in FORMATS:
        raise ExportError(f"Unknown format {fmt!r}: choose one of {', '.join(FORMATS)}")
    annotation = api.pointcloud.annotation.download(cloud.id)
    octree = (cloud.meta or {}).get("octree")
    if octree:
        revision = octree.get("treeRevision")
        if not isinstance(revision, str) or not FILE_NAME.match(revision):
            raise ExportError(f'Point cloud "{cloud.name}" has no tree revision')
        plan = plan_figures(annotation, labels, revision)
        need = cells_need(f.file for f in plan.figures)
        limit = memory_limit()
        sly.logger.info(
            f"{cloud.name}: {len(plan.figures)} figures, {need.figures} cell files, {need.cells:,} cells "
            f"(~{need.bytes / 2**20:,.0f} MiB once read); the task may use "
            + (f"{limit / 2**30:.1f} GiB" if limit else "an unknown amount of memory")
        )
        check_cells_memory(cloud.name, need, limit)
        cells = download_cells(api, cloud.id, revision, [f.file for f in plan.figures])
        figures = [{"cells": cells[f.file["hash"]], "class": f.index, "instance": f.instance} for f in plan.figures]
        served = ServedArtifacts(api, cloud.id, revision)
        bar, report = progress_bar(cloud.name, int(octree.get("pointCount") or 0))
        summary = run_engine(
            lambda token: spc.export_points(
                served.read, revision, output, figures, format=fmt, threads=threads, readers=readers, progress=report, cancel=token
            )
        )
        bar.close()
        summary["http"] = {"requests": served.requests, "bytes": served.bytes}
        return Result(output, summary, plan, streamed=True)

    plan = plan_figures(annotation, labels, None)
    figures = [{"indices": f.indices, "class": f.index, "instance": f.instance} for f in plan.figures]
    scratch = Path(tempfile.mkdtemp(prefix="cloud-", dir=work_dir))
    try:
        suffix = Path(cloud.name).suffix.lower()
        source = scratch / f"cloud{suffix if suffix in POINT_CLOUD_EXTENSIONS else '.pcd'}"
        api.pointcloud.download_path(cloud.id, str(source))
        # Exact positions: the pack's grid reproduces the file's coordinates (precision 0).
        packed = run_engine(
            lambda token: spc.ingest(str(source), scratch / "pack", precision=0, threads=threads, cancel=token)
        )
        source.unlink()
        bar, report = progress_bar(cloud.name, int(packed["pointCount"]))
        summary = run_engine(
            lambda token: spc.export_points(
                scratch / "pack", packed["treeRevision"], output, figures, format=fmt, threads=threads, progress=report, cancel=token
            )
        )
        bar.close()
        summary["records"] = packed["recordCount"]
        return Result(output, summary, plan, streamed=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def describe(result: Result, labels: Mapping[int, ClassLabel], cloud: Any) -> Dict[str, Any]:
    """The report entry of one exported point cloud."""
    s = result.summary
    report = {
        "pointCloudId": cloud.id,
        "name": cloud.name,
        "streamed": result.streamed,
        "file": result.path.name,
        "format": s["format"],
        "size": s["size"],
        "points": s["pointCount"],
        "labeledPoints": s["labeledPoints"],
        "unlabeledPoints": s["unlabeledPoints"],
        "overlapPoints": s["overlapPoints"],
        "classes": class_counts(s["classes"], labels),
        "objects": [
            {"figureId": f.figure_id, "objectId": f.object_id, "class": labels[f.class_id].name, "index": f.index,
             "instance": f.instance, "points": points}
            for f, points in zip(result.plan.figures, s["figures"])
        ],
        "attributes": s["attributes"],
        "seconds": round(s["seconds"], 1),
    }
    if result.plan.skipped:
        report["skippedFigures"] = result.plan.skipped
    if s.get("ignoredIndices"):
        report["ignoredIndices"] = s["ignoredIndices"]
    if "las" in s:
        report["las"] = s["las"]
    return report
