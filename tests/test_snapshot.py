"""Stop/resume and refresh must never mix source vintages.

Each test reproduces a sequence of sessions that 0.2.0 handled incorrectly.
"""

import json

import pyarrow.parquet as pq
import pytest

from openplaces_ph import sources
from openplaces_ph.config import Scope
from openplaces_ph.finalize import finalize
from openplaces_ph.matching import COMPLETE_NAME, MANIFEST_NAME, MatchConfig, prepare_matches
from openplaces_ph.snapshot import (
    adopt_unbound_dir,
    bind_dir_to_release,
    dir_release,
    discard_dir,
    source_snapshot,
)
from openplaces_ph.tiles import Tile

T = Tile(120, 14, 1.0)
SCOPE = Scope(name="test", bboxes=((120.0, 14.0, 121.0, 15.0),))


def _snapshot_one(project):
    project.tile("fsq", T, [("f1", "Jollibee", 120.5, 14.5)])
    project.tile("overture", T, [("o1", "Jollibee", 120.50002, 14.50002)])
    project.tile("osm", T, [("node/1", "Jollibee", 120.50001, 14.50001)])


def _refresh_to_snapshot_two(project):
    """What `--only sources --refresh-sources` does when both releases changed."""
    for source, release in (("fsq", "2026-02-01"), ("overture", "2026-02-01.0")):
        project.pin(source, release)
        bind_dir_to_release(project.directory(source), release, source)
    project.tile("fsq", T, [("f1", "Jollibee", 120.5, 14.5), ("f2", "Mercury Drug", 120.7, 14.7)])
    project.tile("overture", T, [("o1", "Jollibee", 120.50002, 14.50002),
                                 ("o2", "Mercury Drug", 120.70002, 14.70002)])


def _match(project, **kwargs):
    return prepare_matches(project.root, SCOPE, [T], 1.0, 0.25, project.fsq_dir,
                           project.overture_dir, project.osm_dir, 1, project.temp_dir,
                           "256MB", MatchConfig(), **kwargs)


def _finalize(project, edge_root):
    return finalize(project.root, SCOPE, [T], project.fsq_dir, project.overture_dir,
                    project.osm_dir, edge_root, project.temp_dir, "256MB")


def test_refresh_in_separate_sessions_rebuilds_matching_and_outputs(project):
    _snapshot_one(project)
    assert _finalize(project, _match(project))["canonical_pois"] == 1

    # Session 2: --only sources --refresh-sources. Sessions 3 and 4:
    # --only match, then --only finalize, without any rebuild flag.
    _refresh_to_snapshot_two(project)
    summary = _finalize(project, _match(project))

    # 0.2.0 reused the old shards and outputs here and still reported 1.
    assert summary["canonical_pois"] == 2
    assert summary["sources"]["fsq_release"] == "2026-02-01"
    assert summary["sources"]["overture_release"] == "2026-02-01.0"


def test_finalize_refuses_match_shards_from_another_snapshot(project):
    _snapshot_one(project)
    edge_root = _match(project)
    _refresh_to_snapshot_two(project)
    with pytest.raises(RuntimeError, match="different source snapshot"):
        _finalize(project, edge_root)


def test_finalize_refuses_unfinished_matching(project):
    _snapshot_one(project)
    edge_root = _match(project)
    (edge_root / COMPLETE_NAME).unlink()  # as after an interrupted --only match
    with pytest.raises(RuntimeError, match="Matching has not finished"):
        _finalize(project, edge_root)


def test_a_new_osm_extract_rebuilds_match_shards(project):
    _snapshot_one(project)
    edge_root = _match(project)
    marker = edge_root / "fsq__osm" / "marker.txt"
    marker.write_text("from the old OSM extract")
    project.set_osm_snapshot("v2")
    _match(project)
    assert not marker.exists()


def test_edge_checkpoints_without_a_snapshot_record_are_rebuilt_once(project):
    _snapshot_one(project)
    edge_root = _match(project)
    manifest = json.loads((edge_root / MANIFEST_NAME).read_text())
    del manifest["sources"]  # what 0.2.0 wrote
    (edge_root / MANIFEST_NAME).write_text(json.dumps(manifest))
    stale = next((edge_root / "fsq__osm").glob("*.parquet"))
    stale_mtime = stale.stat().st_mtime_ns

    _match(project)
    assert "sources" in json.loads((edge_root / MANIFEST_NAME).read_text())
    assert stale.stat().st_mtime_ns != stale_mtime  # recomputed, not reused

    # A second run with nothing changed reuses every shard.
    again = stale.stat().st_mtime_ns
    _match(project)
    assert stale.stat().st_mtime_ns == again


def test_snapshot_rejects_tiles_left_by_an_unfinished_refresh(project):
    _snapshot_one(project)
    project.pin("overture", "2026-03-01.0")  # re-pinned, but tiles not yet replaced
    with pytest.raises(RuntimeError, match="belong to release 2026-01-01.0"):
        source_snapshot(project.root, [T], project.fsq_dir, project.overture_dir, project.osm_dir)


def test_release_change_discards_the_whole_directory_first(project):
    _snapshot_one(project)
    old_tile = project.overture_dir / f"{T.key}.parquet"
    bind_dir_to_release(project.overture_dir, "2026-02-01.0", "overture")
    assert not old_tile.exists()
    assert dir_release(project.overture_dir) == "2026-02-01.0"
    # Rebinding to the same release keeps new work.
    project.tile("overture", T, [("o9", "Puregold", 120.2, 14.2)])
    bind_dir_to_release(project.overture_dir, "2026-02-01.0", "overture")
    assert old_tile.exists()


def test_unbound_tiles_from_0_2_0_are_adopted_under_the_old_pin(project):
    _snapshot_one(project)
    (project.fsq_dir / "_release.json").unlink()  # 0.2.0 wrote no manifest
    adopt_unbound_dir(project.fsq_dir, "2026-01-01", "fsq")
    assert dir_release(project.fsq_dir) == "2026-01-01"
    # A later re-pin therefore recognises those tiles as old.
    bind_dir_to_release(project.fsq_dir, "2026-02-01", "fsq")
    assert not (project.fsq_dir / f"{T.key}.parquet").exists()


def test_discard_dir_removes_leftovers_of_an_interrupted_discard(tmp_path):
    target = tmp_path / "edges"
    (target / "a").mkdir(parents=True)
    leftover = tmp_path / "edges.discard-123-456"
    (leftover / "b").mkdir(parents=True)
    discard_dir(target)
    assert not target.exists()
    assert not leftover.exists()


def test_foursquare_refresh_is_resumable_and_never_mixes_releases(project, monkeypatch):
    tiles = [Tile(120, 14, 1.0), Tile(121, 14, 1.0)]
    scope = Scope(name="test", bboxes=((120.0, 14.0, 122.0, 15.0),))
    latest = {"release": "2026-01-01"}
    calls = []

    def fake_worker(tile, root_s, scope_bboxes, scope_slug, release, temp_s, memory_limit):
        if latest.get("fail_after") is not None and len(calls) >= latest["fail_after"]:
            raise KeyboardInterrupt  # the user pressed Ctrl+C
        calls.append((tile.key, release))
        project.tile("fsq", tile, [(f"f-{tile.key}-{release}", "Jollibee", tile.west + 0.5, 14.5)])

    monkeypatch.setattr(sources, "_discover_fsq_release", lambda: latest["release"])
    monkeypatch.setattr(sources, "_fsq_worker", fake_worker)
    (project.fsq_dir / "_release.json").unlink()
    for path in project.fsq_dir.glob("*.parquet"):
        path.unlink()

    def run(refresh):
        sources.prepare_foursquare(project.root, scope, tiles, 1, project.temp_dir, "256MB",
                                   refresh=refresh)

    run(refresh=False)
    assert [r for _, r in calls] == ["2026-01-01", "2026-01-01"]

    # Refresh when upstream has not changed: nothing is downloaded again.
    calls.clear()
    run(refresh=True)
    assert calls == []

    # A new release appears; the refresh is interrupted after one tile.
    latest.update(release="2026-02-01", fail_after=1)
    calls.clear()
    with pytest.raises(KeyboardInterrupt):
        run(refresh=True)
    # Resuming WITHOUT the flag continues the new release (0.2.0 accepted the
    # remaining old-release tile as complete and mixed the two releases).
    latest["fail_after"] = None
    run(refresh=False)
    # source_id is "f-<tile key>-<release>"; tile keys contain no "-".
    releases = {
        pq.read_table(p)["source_id"][0].as_py().split("-", 2)[2]
        for p in project.fsq_dir.glob("*.parquet")
    }
    assert releases == {"2026-02-01"}
    assert [r for _, r in calls] == ["2026-02-01", "2026-02-01"]
