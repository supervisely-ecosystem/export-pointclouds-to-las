"""Offline tests of the class indices, semantic classes, instance numbers and figure priority."""

from types import SimpleNamespace as NS

import pytest

from src.labels import (
    LabelError,
    class_counts,
    class_labels,
    is_semantic,
    mapping_warnings,
    parse_description,
    parse_overrides,
    plan_figures,
)
from src.settings import Settings

REVISION = "a" * 64


def cls(id, name, description="", shape="point_cloud", settings=None):
    return NS(id=id, name=name, description=description, shape=shape, settings=settings or {})


def test_overrides_lines_json_and_errors():
    assert parse_overrides(None) == {}
    assert parse_overrides("  \n") == {}
    assert parse_overrides("pallet: 21\n# note\n\nfloor marker = 18\nlabel: a: 3") == {
        "pallet": 21,
        "floor marker": 18,
        "label: a": 3,
    }
    assert parse_overrides('{"pallet": 21, "wall": 0}') == {"pallet": 21, "wall": 0}
    for bad in ("pallet", "pallet: x", "pallet: 256", "pallet: -1", '{"pallet": true}', '["pallet"]', "{oops"):
        with pytest.raises(LabelError):
            parse_overrides(bad)


@pytest.mark.parametrize(
    "description, parsed",
    [
        # The trial project's descriptions.
        ("3 · instance: one object per instance", (3, "instance")),
        ("1 · semantic: one object per cloud", (1, "semantic")),
        ("24 · instance: one object per instance", (24, "instance")),
        ("0·semantic", (0, "semantic")),
        ("3 • Instance", (3, "instance")),
        ("3 ∙ semantic", (3, "semantic")),
        ("3 - semantic: floor", (3, "semantic")),
        ("3. instance", (3, "instance")),
        ("  12 –  semantic", (12, "semantic")),
        ("300 · instance", (300, "instance")),
        ("semantic: one object per cloud", (None, "semantic")),
        # The kind is the first word after the index only.
        ("3 · instance (not semantic)", (3, "instance")),
        ("3 · walls, semantic", (3, None)),
        ("semantics of the floor", (None, None)),
        # A number that is part of the text is no index.
        ("3-axis rack", (None, None)),
        ("3.5 m beams", (None, None)),
        ("3 racks", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_description_index_and_kind(description, parsed):
    assert parse_description(description) == parsed


def test_semantic_flag_wins_over_the_description():
    assert is_semantic({"isSemantic": True}, "3 · instance")
    assert not is_semantic({"isSemantic": False}, "1 · semantic: one object per cloud")
    assert is_semantic({}, "1 · Semantic: one object per cloud")
    assert is_semantic(None, "semantic")
    assert not is_semantic({}, "3 · instance: one object per instance")
    assert not is_semantic({}, "3 · instance (not semantic)")
    assert not is_semantic({"isSemantic": "true"}, "")


def test_the_trial_class_list():
    semantic = {"floor", "wall", "roof", "roof beam", "floor marker", "conveyor"}
    names = [
        "floor", "wall", "wall column", "wall beam", "roof", "roof beam", "column", "gate", "door", "shelf",
        "shelf pillar", "shelf beam", "shelf support bar", "shelf support decking", "shelf guard", "safety guard",
        "safety fencing", "floor marker", "conveyor", "reflector", "pallet", "cage box", "ibc tank", "vehicle",
    ]
    classes = [
        cls(100 + i, name, f"{i} · semantic: one object per cloud" if name in semantic else f"{i} · instance: one object per instance")
        for i, name in enumerate(names, start=1)
    ]
    labels = class_labels(classes + [cls(1, "work unit", shape="cuboid_3d", settings={"isArea": True})], {})
    assert [(l.name, l.index, l.semantic, l.source) for l in labels.values()] == [
        (name, i, name in semantic, "description") for i, name in enumerate(names, start=1)
    ]
    assert mapping_warnings(labels) == []


def test_indices_from_overrides_descriptions_and_order():
    classes = [
        cls(1, "floor", "1 · semantic: one object per cloud"),
        cls(2, "rack"),
        cls(3, "pallet", "21 · instance: one object per instance"),
        cls(4, "work unit", shape="cuboid_3d"),
        cls(5, "cart"),
        cls(6, "box", "3 · instance"),
        cls(7, "any", shape="any"),
        cls(8, "wall", "2 · walls", settings={"isSemantic": True}),
    ]
    labels = class_labels(classes, {})
    got = {l.name: (l.index, l.semantic, l.source) for l in labels.values()}
    assert got == {
        "floor": (1, True, "description"),
        "rack": (4, False, "order"),
        "pallet": (21, False, "description"),
        "cart": (5, False, "order"),
        "box": (3, False, "description"),
        "any": (6, False, "order"),
        "wall": (2, True, "description"),
    }
    [warning] = mapping_warnings(labels)
    assert '"rack" = 4, "cart" = 5, "any" = 6' in warning

    # An override takes its index away from the order numbering, and replaces the class's own description index.
    labels = class_labels(classes, {"pallet": 7, "cart": 4})
    got = {l.name: (l.index, l.source) for l in labels.values()}
    assert got["pallet"] == (7, "override") and got["cart"] == (4, "override")
    assert got["rack"] == (5, "order") and got["any"] == (6, "order")
    assert mapping_warnings(class_labels([cls(1, "a"), cls(2, "b")], {})) == []

    with pytest.raises(LabelError, match="work unit"):
        class_labels(classes, {"work unit": 9})
    with pytest.raises(LabelError, match="More than 255"):
        class_labels([cls(i, f"c{i}") for i in range(1, 300)], {})


def test_shared_indices_are_refused_unless_every_class_is_overridden():
    with pytest.raises(LabelError, match=r'3: "a" \(description\), "b" \(description\)'):
        class_labels([cls(1, "a", "3 · instance"), cls(2, "b", "3 · semantic")], {})
    with pytest.raises(LabelError, match=r'3: "a" \(description\), "b" \(override\)'):
        class_labels([cls(1, "a", "3 · instance"), cls(2, "b")], {"b": 3})
    # The order numbering never hands out an index that an override or a description holds.
    labels = class_labels([cls(1, "a"), cls(2, "b")], {"b": 1})
    assert {l.name: l.index for l in labels.values()} == {"a": 2, "b": 1}
    # Classes merged on purpose: each of them overridden.
    labels = class_labels([cls(1, "a", "3 · instance"), cls(2, "b")], {"a": 5, "b": 5})
    assert {l.name: l.index for l in labels.values()} == {"a": 5, "b": 5}


def test_a_description_index_above_255_is_refused_unless_overridden():
    with pytest.raises(LabelError, match='"tall" starts with 300'):
        class_labels([cls(1, "tall", "300 · instance")], {})
    assert class_labels([cls(1, "tall", "300 · instance")], {"tall": 30})[1].index == 30


def obj(id, class_id, created, updated=None):
    return {"id": id, "classId": class_id, "createdAt": created, "updatedAt": updated or created}


def octree(id, object_id, updated, revision=REVISION, digest=None):
    file = {"hash": digest or f"{id:064x}", "size": 10, "cells": 1}
    return {
        "id": id,
        "objectId": object_id,
        "geometryType": "point_cloud_octree",
        "createdAt": updated,
        "updatedAt": updated,
        "geometry": {"version": 2, "treeRevision": revision, "file": file},
    }


def test_plan_orders_by_figure_update_and_numbers_instances_by_object_creation():
    labels = class_labels([cls(1, "floor", "1 · semantic"), cls(2, "pallet", "21 · instance"), cls(3, "area", shape="cuboid_3d")], {})
    annotation = {
        "objects": [
            # Created in another order than their ids: instances follow creation.
            obj(10, 2, "2026-01-01T00:00:03.000Z"),
            obj(11, 2, "2026-01-01T00:00:01.000Z"),
            obj(12, 1, "2026-01-01T00:00:02.000Z"),
            obj(13, 1, "2026-01-01T00:00:04.000Z"),
            # Updated after every figure (a tag, say): its figure keeps its own place.
            obj(14, 2, "2026-01-01T00:00:05.000Z", "2026-01-01T00:01:00.000Z"),
            obj(15, 3, "2026-01-01T00:00:06.000Z"),
        ],
        "figures": [
            octree(100, 10, "2026-01-01T00:00:30.000Z"),
            octree(101, 11, "2026-01-01T00:00:10.000Z"),
            octree(102, 12, "2026-01-01T00:00:20.000Z"),
            octree(103, 13, "2026-01-01T00:00:40.000Z"),
            octree(104, 14, "2026-01-01T00:00:05.000Z"),
            octree(105, 10, "2026-01-01T00:00:15.000Z"),
            octree(106, 15, "2026-01-01T00:00:50.000Z"),
            octree(107, 11, "2026-01-01T00:00:50.000Z", revision="b" * 64),
            {**octree(110, 11, "2026-01-01T00:00:50.000Z"), "geometry": {"version": 2, "treeRevision": REVISION}},
            {"id": 108, "objectId": 11, "geometryType": "cuboid_3d", "updatedAt": "2026-01-01T00:00:50.000Z"},
            {**octree(109, 13, "2026-01-01T00:00:40.000Z"), "id": 99},
        ],
    }
    plan = plan_figures(annotation, labels, REVISION)
    assert [f.figure_id for f in plan.figures] == [104, 101, 105, 102, 100, 99, 103]
    assert {f.figure_id: (f.index, f.instance) for f in plan.figures} == {
        101: (21, 1),
        105: (21, 2),
        102: (1, 0),
        100: (21, 2),
        99: (1, 0),
        103: (1, 0),
        104: (21, 3),
    }
    assert plan.figures[1].file["hash"] == f"{101:064x}"
    assert plan.skipped == {
        "class is not a point cloud class": 1,
        "labels another tree revision of the point cloud": 1,
        "has no cell file": 1,
    }


def test_plan_of_a_cloud_file_takes_its_point_indices():
    labels = class_labels([cls(1, "pallet", "21 · instance")], {})
    annotation = {
        "objects": [obj(10, 1, "2026-01-01T00:00:01.000Z"), obj(11, 1, "2026-01-01T00:00:02.000Z")],
        "figures": [
            {"id": 1, "objectId": 11, "geometryType": "point_cloud", "updatedAt": "2026-01-01T00:00:03.000Z", "geometry": {"indices": [5, 6]}},
            {"id": 2, "objectId": 10, "geometryType": "point_cloud", "updatedAt": "2026-01-01T00:00:04.000Z", "geometry": {"indices": [6, 7]}},
            {"id": 3, "objectId": 10, "geometryType": "point_cloud", "updatedAt": "2026-01-01T00:00:05.000Z", "geometry": {"indices": []}},
            octree(4, 10, "2026-01-01T00:00:06.000Z"),
        ],
    }
    plan = plan_figures(annotation, labels, None)
    assert [(f.figure_id, f.instance, f.indices, f.file) for f in plan.figures] == [(1, 2, [5, 6], None), (2, 1, [6, 7], None)]
    assert plan.skipped == {"holds no points": 1}


def test_class_counts_names_each_index():
    labels = class_labels([cls(1, "floor", "1 · semantic"), cls(2, "ground"), cls(3, "pallet")], {"floor": 1, "ground": 1})
    counts = class_counts([{"class": 0, "points": 5}, {"class": 1, "points": 7}, {"class": 2, "points": 1}], labels)
    assert counts == [
        {"class": 0, "names": ["unlabeled"], "points": 5},
        {"class": 1, "names": ["floor", "ground"], "points": 7},
        {"class": 2, "names": ["pallet"], "points": 1},
    ]


def test_settings_from_the_modal():
    assert Settings.from_env({}) == Settings("laz", {})
    assert Settings.from_env({"modal.state.format": " PLY ", "modal.state.classIndices": "pallet: 21"}) == Settings("ply", {"pallet": 21})
    with pytest.raises(ValueError, match="format"):
        Settings.from_env({"modal.state.format": "xyz"})
