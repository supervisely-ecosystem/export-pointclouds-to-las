"""What each point is labelled with: the class index of every point cloud class, which classes are semantic, and the
figures of a point cloud in priority order with their instance numbers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# Classes whose objects label points: point cloud segmentation, and "any shape" classes, which may hold them too.
POINT_SHAPES = ("point_cloud", "any")
OCTREE = "point_cloud_octree"
INDICES = "point_cloud"
# "3 · instance: one object per instance": the index a class description starts with.
DESCRIPTION_INDEX = re.compile(r"^\s*(\d{1,3})\s*[·•]")
SEMANTIC_SETTING = "isSemantic"
SEMANTIC_WORD = re.compile(r"\bsemantic\b", re.IGNORECASE)
MAX_INDEX = 255
UNLABELED = 0


class LabelError(ValueError):
    """The class mapping cannot be built: the message says what to fix."""


@dataclass(frozen=True)
class ClassLabel:
    class_id: int
    name: str
    index: int
    semantic: bool
    # Where the index comes from: "description", "order" or "override".
    source: str


def parse_overrides(text: Optional[str]) -> Dict[str, int]:
    """`class name: index` (or `=`) per line, or a JSON object `{"class name": index}`; blank lines and `#` comments
    are ignored."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise LabelError(f"Class indices: not valid JSON ({e})") from e
        if not isinstance(parsed, dict):
            raise LabelError("Class indices: the JSON must be an object of class names to indices")
        items = list(parsed.items())
    else:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cut = max(line.rfind(":"), line.rfind("="))
            if cut <= 0:
                raise LabelError(f'Class indices: "{line}" is not "class name: index"')
            items.append((line[:cut].strip(), line[cut + 1:].strip()))
    overrides = {}
    for name, value in items:
        try:
            index = int(value)
        except (TypeError, ValueError):
            index = -1
        if isinstance(value, bool) or not 0 <= index <= MAX_INDEX:
            raise LabelError(f'Class indices: "{name}" must map to an integer 0..{MAX_INDEX}, not {value!r}')
        overrides[str(name)] = index
    return overrides


def is_semantic(settings: Optional[Mapping[str, Any]], description: Optional[str]) -> bool:
    """The class flag `settings.isSemantic` when the class has one, else whether its description says `semantic`."""
    flag = (settings or {}).get(SEMANTIC_SETTING)
    if isinstance(flag, bool):
        return flag
    return bool(SEMANTIC_WORD.search(description or ""))


def class_labels(classes: Sequence[Any], overrides: Mapping[str, int]) -> Dict[int, ClassLabel]:
    """The label of every point cloud class of a project (`classes` as `api.object_class.get_list` returns them,
    in the project's order). A class whose description starts with `N ·` gets N; the others get 1, 2, … in the
    project's order, skipping indices taken that way (0 stays the unlabeled points'); `overrides` (by class name)
    win over both."""
    pointish = [c for c in classes if c.shape in POINT_SHAPES]
    names = {c.name for c in pointish}
    unknown = sorted(set(overrides) - names)
    if unknown:
        raise LabelError(
            f"Class indices name classes the project has no point cloud class of: {', '.join(unknown)} "
            f"(it has: {', '.join(sorted(names)) or 'none'})"
        )
    described = {}
    for c in pointish:
        match = DESCRIPTION_INDEX.match(c.description or "")
        if match and int(match.group(1)) <= MAX_INDEX:
            described[c.id] = int(match.group(1))
    taken = set(described.values()) | {UNLABELED}
    labels = {}
    next_index = 1
    for c in pointish:
        semantic = is_semantic(getattr(c, "settings", None), c.description)
        if c.name in overrides:
            index, source = overrides[c.name], "override"
        elif c.id in described:
            index, source = described[c.id], "description"
        else:
            while next_index in taken:
                next_index += 1
            if next_index > MAX_INDEX:
                raise LabelError(f"More than {MAX_INDEX} point cloud classes: set the indices of the rest by hand")
            index, source = next_index, "order"
            taken.add(index)
        labels[c.id] = ClassLabel(c.id, c.name, index, semantic, source)
    return labels


@dataclass
class PlannedFigure:
    figure_id: int
    object_id: int
    class_id: int
    index: int
    instance: int
    # point_cloud_octree: the cell file pointer {hash, size, cells}; point_cloud: the point indices.
    file: Optional[Dict[str, Any]] = None
    indices: Optional[List[int]] = None


@dataclass
class FigurePlan:
    figures: List[PlannedFigure]
    # Figures that label nothing in this export, by reason.
    skipped: Dict[str, int]


def plan_figures(annotation: Mapping[str, Any], labels: Mapping[int, ClassLabel], tree_revision: Optional[str]) -> FigurePlan:
    """The figures of a point cloud's annotation (`api.pointcloud.annotation.download`) that label its points, in
    priority order, each with its class index and instance: 0 for a semantic class, else 1, 2, … per class in the
    order the objects were created. The engine lets the last figure holding a point win, so the figures go least
    recently updated first, by the later of the figure's and its object's `updatedAt`: of the objects holding a point,
    the one updated last labels it. `tree_revision`: the streamed cloud's tree (its `point_cloud_octree` figures label
    it); `None` for a cloud file (its `point_cloud` figures do)."""
    objects = {o["id"]: o for o in annotation.get("objects") or []}
    kind = OCTREE if tree_revision is not None else INDICES
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    kept = []
    for figure in annotation.get("figures") or []:
        if figure.get("geometryType") != kind:
            continue
        geometry = figure.get("geometry") or {}
        owner = objects.get(figure.get("objectId"))
        class_id = (owner or {}).get("classId") or figure.get("classId")
        if class_id not in labels:
            skip("class is not a point cloud class")
            continue
        if kind == OCTREE:
            file = geometry.get("file") or {}
            if geometry.get("treeRevision") != tree_revision or not file.get("hash"):
                skip("labels another tree revision of the point cloud")
                continue
        elif not geometry.get("indices"):
            skip("holds no points")
            continue
        kept.append((figure, owner or {}, class_id))

    created: Dict[int, tuple] = {}
    object_class: Dict[int, int] = {}
    for figure, owner, class_id in kept:
        object_id = figure.get("objectId") or -figure["id"]
        key = (owner.get("createdAt") or figure.get("createdAt") or "", object_id)
        created[object_id] = min(created.get(object_id, key), key)
        object_class[object_id] = class_id
    instances: Dict[int, int] = {}
    per_class: Dict[int, int] = {}
    for object_id in sorted(created, key=created.__getitem__):
        class_id = object_class[object_id]
        if labels[class_id].semantic:
            instances[object_id] = 0
        else:
            per_class[class_id] = per_class.get(class_id, 0) + 1
            instances[object_id] = per_class[class_id]

    kept.sort(key=lambda item: (max(item[0].get("updatedAt") or "", item[1].get("updatedAt") or ""), item[0]["id"]))
    planned = []
    for figure, _, class_id in kept:
        object_id = figure.get("objectId") or -figure["id"]
        geometry = figure.get("geometry") or {}
        planned.append(
            PlannedFigure(
                figure_id=figure["id"],
                object_id=object_id,
                class_id=class_id,
                index=labels[class_id].index,
                instance=instances[object_id],
                file=geometry.get("file") if kind == OCTREE else None,
                indices=list(geometry["indices"]) if kind == INDICES else None,
            )
        )
    return FigurePlan(planned, skipped)


def class_counts(summary_classes: Iterable[Mapping[str, int]], labels: Mapping[int, ClassLabel]) -> List[Dict[str, Any]]:
    """The engine's per-class point counts with the names of the classes of each index."""
    names: Dict[int, List[str]] = {}
    for label in labels.values():
        names.setdefault(label.index, []).append(label.name)
    out = []
    for entry in summary_classes:
        index = entry["class"]
        named = names.get(index, [])
        out.append({"class": index, "names": ["unlabeled", *named] if index == UNLABELED else named, "points": entry["points"]})
    return out
