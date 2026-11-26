"""What each point is labelled with: the class index of every point cloud class, which classes are semantic, and the
figures of a point cloud in priority order with their instance numbers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Classes whose objects label points: point cloud segmentation, and "any shape" classes, which may hold them too.
POINT_SHAPES = ("point_cloud", "any")
OCTREE = "point_cloud_octree"
INDICES = "point_cloud"
# "3 · instance: one object per instance": the index a class description starts with, then a separator (`·`, `•`, `∙`,
# or `-` / `.` before a space, so that "3-axis" and "3.5 m" are no index), then optionally the class kind.
DESCRIPTION_INDEX = re.compile(r"^\s*(\d+)\s*(?:[·•∙]|[.\-–](?=\s))\s*")
KIND = re.compile(r"(semantic|instance)\b", re.IGNORECASE)
SEMANTIC_SETTING = "isSemantic"
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


def parse_description(description: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """`(index, kind)` of a class description: the number it starts with (`3 · …`, `3 - …`, `3. …`), and `semantic` or
    `instance` when that is the first word after it (or the description's first word without an index)."""
    text = description or ""
    match = DESCRIPTION_INDEX.match(text)
    kind = KIND.match(text[match.end():] if match else text.lstrip())
    return int(match.group(1)) if match else None, kind.group(1).lower() if kind else None


def is_semantic(settings: Optional[Mapping[str, Any]], description: Optional[str]) -> bool:
    """The class flag `settings.isSemantic` when the class has one, else whether its description names the class
    `semantic` (`1 · semantic: one object per cloud`)."""
    flag = (settings or {}).get(SEMANTIC_SETTING)
    if isinstance(flag, bool):
        return flag
    return parse_description(description)[1] == "semantic"


def class_labels(classes: Sequence[Any], overrides: Mapping[str, int]) -> Dict[int, ClassLabel]:
    """The label of every point cloud class of a project (`classes` as `api.object_class.get_list` returns them,
    in the project's order). `overrides` (by class name) come first, then the index a description starts with; the
    other classes get the free indices 1, 2, … in the project's order (0 stays the unlabeled points'). Two classes
    may share an index only when both got it from `overrides`: otherwise their points could not be told apart."""
    pointish = [c for c in classes if c.shape in POINT_SHAPES]
    names = {c.name for c in pointish}
    unknown = sorted(set(overrides) - names)
    if unknown:
        raise LabelError(
            f"Class indices name classes the project has no point cloud class of: {', '.join(unknown)} "
            f"(it has: {', '.join(sorted(names)) or 'none'})"
        )
    fixed: Dict[int, Tuple[int, str]] = {}
    for c in pointish:
        if c.name in overrides:
            fixed[c.id] = (overrides[c.name], "override")
        else:
            index = parse_description(c.description)[0]
            if index is not None and index > MAX_INDEX:
                raise LabelError(
                    f'The description of class "{c.name}" starts with {index}, more than the {MAX_INDEX} a LAS class '
                    f'holds: change it, or set the class\'s index in the modal'
                )
            if index is not None:
                fixed[c.id] = (index, "description")
    taken = {index for index, _ in fixed.values()} | {UNLABELED}
    labels = {}
    next_index = 1
    for c in pointish:
        semantic = is_semantic(getattr(c, "settings", None), c.description)
        if c.id in fixed:
            index, source = fixed[c.id]
        else:
            while next_index in taken:
                next_index += 1
            if next_index > MAX_INDEX:
                raise LabelError(f"More than {MAX_INDEX} point cloud classes: set the indices of the rest by hand")
            index, source = next_index, "order"
            taken.add(index)
        labels[c.id] = ClassLabel(c.id, c.name, index, semantic, source)

    by_index: Dict[int, List[ClassLabel]] = {}
    for label in labels.values():
        by_index.setdefault(label.index, []).append(label)
    clashes = [
        f"{index}: " + ", ".join(f'"{l.name}" ({l.source})' for l in group)
        for index, group in sorted(by_index.items())
        if len(group) > 1 and any(l.source != "override" for l in group)
    ]
    if clashes:
        raise LabelError(
            "Classes would share a classification index, so their points could not be told apart: "
            + "; ".join(clashes)
            + ". Give them different indices in their descriptions or in the modal's class indices "
            "(classes merged on purpose: list each of them there)"
        )
    return labels


def mapping_warnings(labels: Mapping[int, ClassLabel]) -> List[str]:
    """What may be unintended in a valid mapping: indices numbered by the project's order next to indices from
    descriptions (the numbered ones change when classes are added or reordered)."""
    sources = {l.source for l in labels.values()}
    if "order" in sources and "description" in sources:
        ordered = sorted((l for l in labels.values() if l.source == "order"), key=lambda l: l.index)
        listed = ", ".join(f'"{l.name}" = {l.index}' for l in ordered)
        return [
            f"Some classes have an index in their description and these have none, so they got the next free "
            f"indices in the project's order: {listed}. Set them in the class descriptions or the modal if their "
            f"numbers matter"
        ]
    return []


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
    order the objects holding figures of this cloud were created (dense: removing an object renumbers the later ones;
    the report maps the numbers to object ids). The engine lets the last figure holding a point win, so the figures
    go least recently updated first, by the figure's own `updatedAt` (an edit of the object, such as a tag, does not
    reorder them). `tree_revision`: the streamed cloud's tree (its `point_cloud_octree` figures label it); `None` for
    a cloud file (its `point_cloud` figures do)."""
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
            if not file.get("hash"):
                skip("has no cell file")
                continue
            if geometry.get("treeRevision") != tree_revision:
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

    kept.sort(key=lambda item: (item[0].get("updatedAt") or "", item[0]["id"]))
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
