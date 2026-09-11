"""Run the real candidate SQL end to end on synthetic tiles.

Before 0.2.1 no test executed ``_candidate_sql``: the grid join, the halo,
the tile partitioning, and the provenance flag were only checked by reading.
"""

import random

import duckdb
import pyarrow.parquet as pq
import pytest

from openplaces_ph.config import Scope
from openplaces_ph.finalize import finalize
from openplaces_ph.matching import (
    EARTH_RADIUS_M,
    PAIRS,
    MatchConfig,
    acceptance_sql,
    prepare_matches,
)
from openplaces_ph.tiles import Tile

T1, T2 = Tile(120, 14, 1.0), Tile(121, 14, 1.0)
SCOPE = Scope(name="test", bboxes=((120.0, 14.0, 122.0, 15.0),))

NAMES = [
    "Jollibee", "Jolibee", "Jollibee SM North", "SM North Jollibee", "Mercury Drug",
    "Mercury Drugstore", "7-Eleven", "7 Eleven", "BDO", "ATM", "Puregold",
    "Puregold Jr", "Ministop", "Chowking", "Mang Inasal", "Aling Nena's Carinderia",
]


def _edges(edge_root):
    out = []
    for a, b in PAIRS:
        for path in sorted((edge_root / f"{a}__{b}").glob("*.parquet")):
            t = pq.read_table(path, columns=["left_source", "left_source_id",
                                             "right_source", "right_source_id"])
            out += list(zip(*(t[c].to_pylist() for c in t.column_names)))
    return out


def _brute_force(tables, cfg):
    """All cross-source pairs, no grid and no tiles: the recall oracle."""
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE obs(source VARCHAR, id VARCHAR, name_norm VARCHAR, "
                    "name_tokens VARCHAR, lon DOUBLE, lat DOUBLE)")
        for table in tables:
            con.execute("INSERT INTO obs SELECT source, source_id, name_norm, name_tokens, lon, lat "
                        "FROM read_parquet(?)", [str(table)])
        pair_list = ", ".join(f"('{a}', '{b}')" for a, b in PAIRS)
        rows = con.execute(f"""
            WITH p AS (
                SELECT a.source AS ls, a.id AS lid, b.source AS rs, b.id AS rid,
                       a.name_norm AS name_left, b.name_norm AS name_right,
                       greatest(jaro_winkler_similarity(a.name_norm, b.name_norm),
                                jaro_winkler_similarity(a.name_tokens, b.name_tokens)) AS name_score,
                       2 * {EARTH_RADIUS_M} * asin(sqrt(
                           pow(sin(radians(b.lat - a.lat) / 2), 2)
                           + cos(radians(a.lat)) * cos(radians(b.lat))
                           * pow(sin(radians(b.lon - a.lon) / 2), 2))) AS distance_m
                FROM obs a JOIN obs b ON (a.source, b.source) IN ({pair_list})
            )
            SELECT ls, lid, rs, rid FROM p
            WHERE distance_m <= {cfg.max_distance_m} AND {acceptance_sql(cfg)}
        """).fetchall()
    finally:
        con.close()
    return rows


@pytest.mark.parametrize("max_distance", [60.0, 120.0])
def test_match_finds_exactly_the_brute_force_edges(project, max_distance):
    # Clusters sit on a 1-degree block edge (lon 121.0), on a 0.25-degree
    # match-tile corner (120.75, 14.25), and inside a tile. Every pair that the
    # all-pairs oracle accepts must be found once, and nothing else.
    rng = random.Random(20260911)
    centres = [(121.0, 14.5), (120.75, 14.25), (120.4, 14.8)]
    written = []
    for source in ("fsq", "overture", "osm"):
        per_tile = {T1: [], T2: []}
        for c, (cx, cy) in enumerate(centres):
            for i in range(45):
                lon = cx + rng.uniform(-0.0015, 0.0015)
                lat = cy + rng.uniform(-0.0015, 0.0015)
                row = (f"{source}-{c}-{i}", rng.choice(NAMES), lon, lat)
                per_tile[T1 if lon < 121.0 else T2].append(row)
        for tile, rows in per_tile.items():
            written.append(project.tile(source, tile, rows))

    cfg = MatchConfig(max_distance_m=max_distance)
    edge_root = prepare_matches(
        project.root, SCOPE, [T1, T2], 1.0, 0.25, project.fsq_dir, project.overture_dir,
        project.osm_dir, 1, project.temp_dir, "256MB", cfg,
    )
    found = _edges(edge_root)
    expected = _brute_force(written, cfg)

    assert len(found) == len(set(found)), "an edge was generated twice"
    assert len(expected) > 50, "the synthetic data should produce many accepted pairs"
    assert set(found) == set(expected)


def test_match_refuses_to_run_on_a_missing_source_tile(project):
    project.tile("fsq", T1, [("f1", "Jollibee", 120.5, 14.5)])
    project.tile("osm", T1, [("node/1", "Jollibee", 120.50001, 14.50001)])
    # The Overture tile was never downloaded (acquisition interrupted).
    with pytest.raises(RuntimeError, match="overture: 1 of 1 source tiles are missing"):
        prepare_matches(project.root, SCOPE, [T1], 1.0, 0.25, project.fsq_dir,
                        project.overture_dir, project.osm_dir, 1, project.temp_dir,
                        "256MB", MatchConfig())
    # 0.2.0 wrote 16 durable, empty fsq__overture shards here.
    edge_root = project.root / "data" / "work" / SCOPE.slug / "edges"
    assert not list(edge_root.rglob("*.parquet"))


def test_matching_reads_only_the_blocks_that_finalize_reads(project):
    # T2 is outside the run's tile list (as after the land mask), but OSM is
    # partitioned nationally, so an OSM partition for T2 exists.
    project.tile("fsq", T1, [("f1", "Pier 7 Dive Shop", 120.99995, 14.5)])
    project.tile("overture", T1, [])
    project.tile("osm", T1, [])
    project.tile("osm", T2, [("node/9", "Pier 7 Dive Shop", 121.00003, 14.5)])

    edge_root = prepare_matches(project.root, SCOPE, [T1], 1.0, 0.25, project.fsq_dir,
                                project.overture_dir, project.osm_dir, 1, project.temp_dir,
                                "256MB", MatchConfig())
    assert _edges(edge_root) == []
    # 0.2.0 accepted the edge above and finalize then failed with
    # "Accepted-edge resolution mismatch" at the end of the run.
    summary = finalize(project.root, SCOPE, [T1], project.fsq_dir, project.overture_dir,
                       project.osm_dir, edge_root, project.temp_dir, "256MB")
    assert summary["canonical_pois"] == 1


def test_foursquare_derived_overture_links_are_not_independent(project):
    project.tile("fsq", T1, [("f1", "Jollibee", 120.5, 14.5), ("f2", "Mercury Drug", 120.6, 14.6)])
    project.tile("overture", T1, [
        ("o1", "Jollibee", 120.50001, 14.50001, "[{'property': '', 'dataset': fsq, 'record_id': x}]"),
        ("o2", "Mercury Drug", 120.60001, 14.60001, "[{'property': '', 'dataset': meta, 'record_id': 1}]"),
    ])
    project.tile("osm", T1, [])
    edge_root = prepare_matches(project.root, SCOPE, [T1], 1.0, 0.25, project.fsq_dir,
                                project.overture_dir, project.osm_dir, 1, project.temp_dir,
                                "256MB", MatchConfig())
    flags = {}
    for path in (edge_root / "fsq__overture").glob("*.parquet"):
        t = pq.read_table(path, columns=["right_source_id", "independent"])
        flags.update(zip(t["right_source_id"].to_pylist(), t["independent"].to_pylist()))
    # The "fsq" dataset name never matched in 0.2.0 (doubled backslashes).
    assert flags == {"o1": False, "o2": True}
