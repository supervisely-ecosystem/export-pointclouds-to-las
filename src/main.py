"""Export point clouds to LAS / LAZ / PLY: one file per point cloud with every point, its class and its object.

Headless task from "Download as" on a point cloud project or dataset. The files go to Team Files under
`/tmp/supervisely/export/Export point clouds to LAS/<task>_<project id>_<project name>/<dataset path>/`, next to
`report.json` (per point cloud: points written, per-class counts, overlaps, file size).
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List

import supervisely as sly
from dotenv import load_dotenv

import src.workflow as w
from src.export import describe, export_cloud
from src.labels import class_labels, mapping_warnings
from src.settings import Settings

RESULT_DIR_NAME = "Export point clouds to LAS"
REPORT_NAME = "report.json"
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe(name: str) -> str:
    return UNSAFE.sub("_", name).strip() or "_"


def dataset_paths(api: sly.Api, project_id: int, dataset_id) -> Dict[int, str]:
    """The datasets to export (the project's, or the context dataset and the datasets nested in it) by id, each with
    its path of names from the project root."""
    datasets = {d.id: d for d in api.dataset.get_list(project_id, recursive=True)}

    def path(ds) -> str:
        names = []
        while ds is not None:
            names.insert(0, safe(ds.name))
            ds = datasets.get(ds.parent_id)
        return "/".join(names)

    def inside(ds) -> bool:
        while ds is not None:
            if ds.id == dataset_id:
                return True
            ds = datasets.get(ds.parent_id)
        return False

    chosen = [d for d in datasets.values() if dataset_id is None or inside(d)]
    return {d.id: path(d) for d in sorted(chosen, key=lambda d: path(d))}


def log_classes(labels) -> None:
    for label in sorted(labels.values(), key=lambda l: (l.index, l.name)):
        kind = "semantic" if label.semantic else "instance"
        sly.logger.info(f"Class {label.name!r}: classification {label.index} ({kind}, index from {label.source})")


def log_result(entry: dict) -> None:
    sly.logger.info(
        f"{entry['name']}: {entry['points']:,} points written to {entry['file']} ({entry['size'] / 2**30:.2f} GiB) "
        f"in {entry['seconds']} s; {entry['labeledPoints']:,} labelled, {entry['unlabeledPoints']:,} unlabelled, "
        f"{entry['overlapPoints']:,} in several objects (the figure updated last wins)",
        # The per-object counts can run to thousands of entries: they stay in report.json.
        extra={"report": {k: v for k, v in entry.items() if k != "objects"}},
    )
    for c in entry["classes"]:
        sly.logger.info(f"  class {c['class']} ({', '.join(c['names']) or 'no class'}): {c['points']:,} points")
    if entry.get("skippedFigures"):
        sly.logger.warning(f"  figures not exported: {entry['skippedFigures']}")
    if entry.get("ignoredIndices"):
        sly.logger.warning(f"  {entry['ignoredIndices']} point indices name no point of the cloud")


def upload(api: sly.Api, team_id: int, local: Path, remote: str):
    """Streams a file to Team Files (one request, retried from the start) and checks the stored SHA-256."""
    progress = sly.tqdm_sly(desc=f"Uploading {local.name}", total=local.stat().st_size, unit="B", unit_scale=True)
    sly.run_coroutine(api.file.upload_async(team_id, str(local), remote, raise_check_error=True, progress_cb=progress))
    progress.close()
    info = api.file.get_info_by_path(team_id, remote)
    if info is None:
        raise RuntimeError(f"{remote} is not in Team Files after its upload")
    return info


def main() -> None:
    if not sly.is_production():
        load_dotenv("local.env")
        load_dotenv(os.path.expanduser("~/supervisely.env"))
    settings = Settings.from_env()
    api = sly.Api.from_env()
    team_id = sly.env.team_id()
    task_id = sly.env.task_id(raise_not_found=False)
    project_id = sly.env.project_id()
    dataset_id = sly.env.dataset_id(raise_not_found=False)
    project = api.project.get_info_by_id(project_id)
    if project is None:
        raise RuntimeError(f"Project {project_id} was not found")
    sly.logger.info(f"Exporting project {project.name!r} ({project.id}) as {settings.format.upper()}")
    w.workflow_input(api, project.id)

    labels = class_labels(api.object_class.get_list(project.id), settings.overrides)
    if not labels:
        sly.logger.warning("The project has no point cloud classes: every point is written unlabelled (class 0)")
    log_classes(labels)
    for warning in mapping_warnings(labels):
        sly.logger.warning(warning)

    work_dir = Path(sly.app.get_data_dir())
    work_dir.mkdir(parents=True, exist_ok=True)
    run_name = f"{task_id or int(time.time())}_{project.id}_{safe(project.name)}"
    remote_root = f"{sly.team_files.RECOMMENDED_EXPORT_PATH.rstrip('/')}/{RESULT_DIR_NAME}/{run_name}/"
    reports: List[dict] = []
    uploaded = []
    for ds_id, ds_path in dataset_paths(api, project.id, dataset_id).items():
        names = set()
        for cloud in api.pointcloud.get_list(ds_id):
            cloud = api.pointcloud.get_info_by_id(cloud.id)
            stem = safe(Path(cloud.name).stem)
            name = f"{stem}.{settings.format}" if stem not in names else f"{stem}_{cloud.id}.{settings.format}"
            names.add(stem)
            local = work_dir / name
            if local.exists():
                local.unlink()
            sly.logger.info(f"Exporting {ds_path}/{cloud.name} (id {cloud.id})")
            try:
                result = export_cloud(api, cloud, labels, settings.format, local, work_dir)
                entry = {"dataset": ds_path, **describe(result, labels, cloud)}
                log_result(entry)
                info = upload(api, team_id, local, f"{remote_root}{ds_path}/{name}")
            finally:
                local.unlink(missing_ok=True)
            entry["teamFilesPath"] = info.path
            reports.append(entry)
            uploaded.append(info)
    if not reports:
        raise RuntimeError("No point clouds to export in the chosen project or dataset")

    report_path = work_dir / REPORT_NAME
    report = {
        "project": {"id": project.id, "name": project.name},
        "format": settings.format,
        "classes": [
            {"name": l.name, "classification": l.index, "semantic": l.semantic, "indexFrom": l.source}
            for l in sorted(labels.values(), key=lambda l: (l.index, l.name))
        ],
        "pointClouds": reports,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    report_info = api.file.upload(team_id, str(report_path), remote_root + REPORT_NAME)
    total = sum(r["points"] for r in reports)
    sly.logger.info(
        f"Exported {len(reports)} point cloud(s), {total:,} points, {sum(r['size'] for r in reports) / 2**30:.2f} GiB "
        f"to Team Files {remote_root}"
    )
    output = uploaded[0] if len(uploaded) == 1 else report_info
    if task_id is not None:
        if len(uploaded) == 1:
            api.task.set_output_archive(task_id, output.id, output.name, file_url=output.storage_path)
        else:
            api.task.set_output_directory(task_id, report_info.id, remote_root)
    w.workflow_output(api, output, remote_root)


if __name__ == "__main__":
    sly.main_wrapper("main", main)
