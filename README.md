<div align="center" markdown>

# Export Point Clouds to LAS / LAZ / PLY

<p align="center">
  <a href="#Overview">Overview</a> •
  <a href="#How-To-Run">How To Run</a> •
  <a href="#Output">Output</a> •
  <a href="#Classes-and-objects">Classes and objects</a> •
  <a href="#Notes">Notes</a>
</p>

[![](https://img.shields.io/badge/supervisely-ecosystem-brightgreen)](https://ecosystem.supervisely.com/apps/supervisely-ecosystem/export-pointclouds-to-las)
[![](https://img.shields.io/badge/slack-chat-green.svg?logo=slack)](https://supervisely.com/slack)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/supervisely-ecosystem/export-pointclouds-to-las)

</div>

# Overview

Exports a **point cloud** project (or one dataset with its nested datasets) for tools outside Supervisely: **one file
per point cloud** with **every point at full resolution**, its **class** and its **object**, as **LAZ** (default),
**LAS** or **PLY**.

- Each point gets the class index of the object it belongs to in `classification` and the object's number in
  `instance_id`; a point no object holds gets class 0, instance 0.
- The point's own values are kept: colour, intensity and the scan's other attributes (normals, curvature, …).
- Large streamed point clouds (imported as an octree) are read where Supervisely serves them, labelled by their
  segmentation objects, and written as they are read: the task holds about 1.4 GB of memory whatever the size of the
  cloud. A cloud of 728 million points with normals took 2 min 15 s and gave a 9.2 GB LAZ, then the upload.
- Point cloud files (PCD and the other formats of a regular point cloud project) are exported the same way, labelled
  by the point indices of their segmentation objects.

Only point cloud segmentation objects (classes of shape `Point Cloud`) label points. Cuboids and other shapes are
not exported.

# How To Run

1. Open the context menu of a **Point Clouds** project or dataset → `Download as` →
   `Export Point Clouds to LAS / LAZ / PLY`.
2. Choose the format and, if needed, the class indices (see [below](#Classes-and-objects)), and press **Run**.
3. When the task finishes, download the result from the `Tasks` page, or from `Team Files` → `tmp` → `supervisely` →
   `export` → `Export point clouds to LAS` → `<task id>_<project id>_<project name>/`.

# Output

```
<task id>_<project id>_<project name>/
  <dataset path>/<point cloud name>.laz     one file per point cloud (.las / .ply for the other formats)
  report.json                               the classes and, per point cloud, what was written
```

**LAS / LAZ**: LAS 1.4, point data record format 7.

| Field | Value |
|---|---|
| `X Y Z` | the point's position as Supervisely stores it, on the cloud's position grid (the scale is the grid step, 0.1 mm for a cloud imported with the default precision), so the export rounds nothing |
| `intensity`, `red green blue` | 16-bit: 8-bit colour times 256, intensity in 0..1 times 65535, integer values as they are |
| `classification` | the class index of the point's object, 0 for unlabeled points |
| `instance_id` (extra bytes, `uint32`) | the object's number in its class, 0 for semantic classes and unlabeled points |
| other extra bytes | the scan's attributes, e.g. `normalX normalY normalZ`, in their own type |

LAZ is the LASzip-compatible compressed form of the same file; PDAL, CloudCompare, LAStools and laspy read it.

**PLY**: binary little-endian, `x y z` (`double`), `red green blue`, `intensity`, `classification` (`uchar`),
`instance_id` (`uint`) and the attributes as vertex properties.

Points are written in the order Supervisely stores them, not in the order of the original file.

`report.json` lists every class with its index and whether it is semantic, and for every point cloud the points
written, labelled and unlabelled, the points held by several objects, the points of each class and of each object,
the file size and the time it took.

# Classes and objects

**Class index** (`classification`, 0–255; 0 is the unlabeled points):

1. the index given in the modal window, one `class name: index` per line, for example `pallet: 21`;
2. else the number the class description starts with, for example `21 · instance: one object per instance`;
3. else the next free number in the order of the project's classes, from 1.

**Object number** (`instance_id`):

- **semantic classes** (one object per class however often drawn, e.g. floor or wall): 0 for every point. A class is
  semantic when its **Semantic** setting is on (on Supervisely versions that have it), else when its description says
  `semantic`, for example `1 · semantic: one object per cloud`;
- **other classes**: 1, 2, 3, … per class, in the order the objects were created.

**Points in several objects**: the object updated last wins (the later of its own update and its figure's), so a
point repainted with another class takes that class. The report counts these points.

# Notes

- Requires Supervisely 6.19.0 or later.
- A streamed cloud is read by the user who runs the export, with that user's access to the project.
- The file of each point cloud is written to the task's disk before it is uploaded to Team Files: the agent needs
  free disk of about the output size (LAS is several times larger than LAZ).
- Objects saved for another version of a streamed point cloud (re-imported since) do not label it; the task log
  counts them.

## For developers

- Run locally: put `SERVER_ADDRESS` and `API_TOKEN` in `~/supervisely.env`, set `TEAM_ID`, `WORKSPACE_ID` and
  `PROJECT_ID` (or `DATASET_ID`) in `local.env`, and run `python src/main.py` from the repository root with it on
  `PYTHONPATH`. The point cloud engine (`supervisely_pointcloud`) comes from the app's image; it is not on PyPI.
- Tests: `python -m pytest tests` (`tests/test_export.py` runs only where `supervisely_pointcloud` is installed).
- Every release vX.Y.Z runs on its own image, `supervisely/export-pointclouds-to-las:<SDK>-X.Y.Z`, which
  `config.json` names and the Release workflow builds from the release's commit (`docker/Dockerfile.tmpl`; the
  `Image check` workflow runs `docker/check_image.py` on every pull request). To release: set `docker_image` to the
  next version (`dev_requirements.txt` pins the SDK), merge, then publish the GitHub release `vX.Y.Z`; `Publish app to
  production` releases it to production. A branch runs on dev from its branch release; point its `config.json` at a
  test image built with `Manual Build App Image From Template` first.
