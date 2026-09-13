"""Functional coverage for the source-subset core edits introduced in 0.3."""

from __future__ import annotations

import itertools
import json

import pytest

pyarrow = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from openplaces_ph.config import Scope
from openplaces_ph.source_set import ALL_SOURCES, pair_dir_name, source_pairs
from openplaces_ph.tiles import Tile


SOURCE_SUBSETS = tuple(
    tuple(combo)
    for size in range(1, len(ALL_SOURCES) + 1)
    for combo in itertools.combinations(ALL_SOURCES, size)
)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("selected", SOURCE_SUBSETS)
def test_source_snapshot_checks_only_selected_sources(tmp_path, monkeypatch, selected):
    from openplaces_ph import snapshot

    root = tmp_path / "repo"
    tile = Tile(120, 14, 1.0)
    fsq_dir = root / "data" / "sources" / "demo" / "fsq"
    overture_dir = root / "data" / "sources" / "demo" / "overture"
    osm_dir = root / "data" / "cache" / "osm" / "tiles_1p0deg"

    # Make inactive sources deliberately incomplete. The function must not care.
    if "fsq" in selected:
        _write_json(root / "data/cache/foursquare/release.json", {"release": "fsq-r"})
        _write_json(fsq_dir / "_release.json", {"release": "fsq-r"})
        (fsq_dir / f"{tile.key}.parquet").write_bytes(b"placeholder")
    if "overture" in selected:
        _write_json(root / "data/cache/overture/release.json", {"release": "ov-r"})
        _write_json(overture_dir / "_release.json", {"release": "ov-r"})
        (overture_dir / f"{tile.key}.parquet").write_bytes(b"placeholder")
    if "osm" in selected:
        _write_json(osm_dir / "_SUCCESS.json", {"pbf": {"etag": "osm-r"}})

    monkeypatch.setattr(snapshot, "valid_parquet", lambda path: path.exists())

    result = snapshot.source_snapshot(
        root, [tile], fsq_dir, overture_dir, osm_dir, sources=selected
    )
    assert tuple(result["active_sources"]) == selected
    assert ("fsq_release" in result) is ("fsq" in selected)
    assert ("overture_release" in result) is ("overture" in selected)
    assert ("osm" in result) is ("osm" in selected)


@pytest.mark.parametrize("selected", SOURCE_SUBSETS)
def test_expected_shards_follow_selected_pairs(tmp_path, selected):
    from openplaces_ph import matching

    block = Tile(120, 14, 1.0)
    scope = Scope(
        name="demo",
        bboxes=((120.0, 14.0, 121.0, 15.0),),
        full_philippines=False,
    )
    pairs = source_pairs(selected)
    shards = matching._expected_shards(
        tmp_path / "edges", scope, [block], 0.5, pairs=pairs
    )

    # One 1-degree block contains four 0.5-degree children.
    assert len(shards) == 4 * len(pairs)
    expected_dirs = {pair_dir_name(a, b) for a, b in pairs}
    assert {path.parent.name for path in shards} == expected_dirs


def _source_table(source: str, source_id: str):
    return pyarrow.table(
        {
            "source": [source],
            "source_id": [source_id],
            "name": [f"{source} place"],
            "category": ["test"],
            "lon": [120.5],
            "lat": [14.5],
            "provenance": [None],
            "upstream_license": ["TEST"],
            "name_norm": [f"{source} place"],
            "name_tokens": [f"place {source}"],
        }
    )


def test_build_observations_reads_only_selected_sources(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from openplaces_ph import finalize

    source_files = {}
    for source in ALL_SOURCES:
        path = tmp_path / f"{source}.parquet"
        pq.write_table(_source_table(source, f"{source}-1"), path)
        source_files[source] = [path]

    monkeypatch.setattr(
        finalize,
        "_source_files",
        lambda source, *_args, **_kwargs: source_files[source],
    )

    scope = Scope(
        name="demo",
        bboxes=((120.0, 14.0, 121.0, 15.0),),
        full_philippines=False,
    )
    out = tmp_path / "observations.parquet"
    temp = tmp_path / "tmp"
    selected = ("overture", "osm")

    finalize.build_observations(
        scope,
        [Tile(120, 14, 1.0)],
        tmp_path / "fsq",
        tmp_path / "overture",
        tmp_path / "osm",
        out,
        temp,
        "512MB",
        sources=selected,
        rebuild=True,
    )

    result = pq.read_table(out, columns=["source"]).column("source").to_pylist()
    assert set(result) == set(selected)
    assert "fsq" not in result



def test_finalize_refuses_a_different_source_tile_inventory(tmp_path, monkeypatch):
    from openplaces_ph import finalize as finalize_module
    from openplaces_ph.snapshot import PipelineStateError

    selected = ("overture", "osm")
    tile = Tile(120, 14, 1.0)
    scope = Scope(
        name="demo",
        bboxes=((120.0, 14.0, 121.0, 15.0),),
        full_philippines=False,
    )
    edge_root = tmp_path / "data" / "work" / scope.slug / "edges"
    edge_root.mkdir(parents=True)
    snapshot = {
        "active_sources": list(selected),
        "overture_release": "ov-r",
        "osm": {"pbf": {"etag": "osm-r"}},
    }
    (edge_root / finalize_module.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "active_sources": list(selected),
                "source_tiles": ["not-the-current-tile"],
                "sources": snapshot,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(finalize_module, "clear_tile_file_caches", lambda: None)
    monkeypatch.setattr(finalize_module, "source_snapshot", lambda *_a, **_k: snapshot)

    with pytest.raises(PipelineStateError, match="source-tile inventory"):
        finalize_module.finalize(
            tmp_path,
            scope,
            [tile],
            tmp_path / "fsq",
            tmp_path / "overture",
            tmp_path / "osm",
            edge_root,
            tmp_path / "tmp",
            "256MB",
            sources=selected,
        )
