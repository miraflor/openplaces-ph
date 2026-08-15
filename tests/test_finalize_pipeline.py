"""Integration smoke test for the finalize chain on tiny synthetic sources.

No network and no real POI data: the point is to exercise the SQL that was
restructured in 0.2.0 (streaming row-id assignment, the split between wide
id-annotated edges and the narrow ranked pair list, and the join back to
cluster membership) rather than to validate matching quality.
"""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from openplaces_ph.config import Scope
from openplaces_ph.finalize import (
    build_canonical,
    build_edge_ids,
    build_match_edges,
    build_observations,
    build_ranked_pairs,
    cluster_edges,
    write_summary,
)
from openplaces_ph.tiles import Tile

SCOPE = Scope(name="test", bboxes=((120.0, 14.0, 121.0, 15.0),))
TILE = Tile(120, 14, 1.0)

OBS_FIELDS = [
    ("source", pa.string()), ("source_id", pa.string()), ("name", pa.string()),
    ("category", pa.string()), ("lon", pa.float64()), ("lat", pa.float64()),
    ("provenance", pa.string()), ("upstream_license", pa.string()),
    ("name_norm", pa.string()), ("name_tokens", pa.string()),
]


def _write_source(path, source, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {
        "source": [source] * len(rows),
        "source_id": [r[0] for r in rows],
        "name": [r[1] for r in rows],
        "category": ["shop"] * len(rows),
        "lon": [r[2] for r in rows],
        "lat": [r[3] for r in rows],
        "provenance": [r[4] if len(r) > 4 else None for r in rows],
        "upstream_license": ["Apache-2.0"] * len(rows),
        "name_norm": [r[1] for r in rows],
        "name_tokens": [r[1] for r in rows],
    }
    pq.write_table(pa.table(cols, schema=pa.schema(OBS_FIELDS)), path)


def _write_edges(path, edges):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "left_source": [e[0] for e in edges],
        "left_source_id": [e[1] for e in edges],
        "right_source": [e[2] for e in edges],
        "right_source_id": [e[3] for e in edges],
        "distance_m": pa.array([e[4] for e in edges], type=pa.float64()),
        "name_score": pa.array([e[5] for e in edges], type=pa.float32()),
        "category_score": pa.array([0.5] * len(edges), type=pa.float32()),
        "score": pa.array([e[6] for e in edges], type=pa.float32()),
        "independent": [True] * len(edges),
    }), path)


@pytest.fixture
def built(tmp_path):
    fsq = tmp_path / "sources" / "fsq"
    overture = tmp_path / "sources" / "overture"
    osm = tmp_path / "sources" / "osm"

    _write_source(fsq / f"{TILE.key}.parquet", "fsq", [
        ("f1", "jollibee", 120.50, 14.50),
        ("f2", "mercury drug", 120.60, 14.60),
        ("f3", "unmatched cafe", 120.70, 14.70),
        # Dropped by the validity filter, so row ids must stay dense.
        ("f4", "", 120.80, 14.80),
    ])
    _write_source(overture / f"{TILE.key}.parquet", "overture", [
        ("o1", "jollibee", 120.500005, 14.500005, "[{'dataset':'Foursquare'}]"),
        ("o2", "mercury drug", 120.600005, 14.600005, "[{'dataset':'meta'}]"),
    ])
    _write_source(osm / f"tile_x={TILE.ix}" / f"tile_y={TILE.iy}" / "part.parquet", "osm", [
        ("node/1", "jollibee", 120.500009, 14.500009),
        ("node/2", "mercury drug", 120.600009, 14.600009),
    ])

    edge_root = tmp_path / "edges"
    _write_edges(edge_root / "fsq__overture" / "t.parquet", [
        ("fsq", "f1", "overture", "o1", 1.0, 1.0, 0.99),
        ("fsq", "f2", "overture", "o2", 1.0, 1.0, 0.97),
    ])
    _write_edges(edge_root / "fsq__osm" / "t.parquet", [
        ("fsq", "f1", "osm", "node/1", 2.0, 1.0, 0.98),
    ])
    _write_edges(edge_root / "overture__osm" / "t.parquet", [
        # Jollibee has all three pairwise links: a directly closed triple.
        ("overture", "o1", "osm", "node/1", 2.0, 1.0, 0.95),
        # Mercury has only two links in total and is completed transitively.
        ("overture", "o2", "osm", "node/2", 2.0, 1.0, 0.96),
    ])

    out = tmp_path / "out"
    obs = build_observations(SCOPE, [TILE], fsq, overture, osm,
                             out / "observations.parquet", tmp_path / "tmp", "512MB")
    ids = build_edge_ids(obs, edge_root, out / "edge_ids.parquet", tmp_path / "tmp", "512MB")
    ranked = build_ranked_pairs(ids, out / "ranked_pairs.parquet", tmp_path / "tmp", "512MB")
    clusters = cluster_edges(obs, ranked, out / "clusters.parquet", out / "state")
    edges = build_match_edges(ids, clusters, out / "match_edges.parquet", tmp_path / "tmp", "512MB")
    canonical = build_canonical(
        obs, clusters, edges, out / "canonical_pois.parquet", tmp_path / "tmp", "512MB"
    )
    summary = write_summary(
        canonical, obs, edges, out / "summary.json", tmp_path / "tmp", "512MB"
    )
    return {
        "obs": obs,
        "ids": ids,
        "ranked": ranked,
        "clusters": clusters,
        "edges": edges,
        "canonical": canonical,
        "summary": summary,
    }


def test_row_ids_are_dense_and_invalid_rows_are_dropped(built):
    t = pq.read_table(built["obs"])
    row_ids = t["row_id"].to_pylist()
    assert row_ids == list(range(len(row_ids)))
    assert len(row_ids) == 7  # 4 fsq - 1 empty name, + 2 overture, + 2 osm
    assert "" not in t["name"].to_pylist()


def test_every_edge_resolves_to_two_observations(built):
    ids = pq.read_table(built["ids"])
    assert ids.num_rows == 5
    valid = set(pq.read_table(built["obs"])["row_id"].to_pylist())
    assert set(ids["left_id"].to_pylist()) <= valid
    assert set(ids["right_id"].to_pylist()) <= valid


def test_ranked_pairs_are_strongest_first_and_narrow(built):
    ranked = pq.read_table(built["ranked"])
    assert ranked.column_names == ["left_id", "right_id"]
    ids = pq.read_table(built["ids"]).to_pylist()
    score_of = {(r["left_id"], r["right_id"]): r["score"] for r in ids}
    scores = [score_of[(l, r)] for l, r in
              zip(ranked["left_id"].to_pylist(), ranked["right_id"].to_pylist())]
    assert scores == sorted(scores, reverse=True)


def test_clusters_group_the_triple_and_keep_the_singleton_alone(built):
    obs = pq.read_table(built["obs"]).to_pylist()
    roots = dict(zip(pq.read_table(built["clusters"])["row_id"].to_pylist(),
                     pq.read_table(built["clusters"])["cluster_root"].to_pylist()))
    by_key = {(r["source"], r["source_id"]): roots[r["row_id"]] for r in obs}

    jollibee = {by_key[("fsq", "f1")], by_key[("overture", "o1")], by_key[("osm", "node/1")]}
    mercury = {by_key[("fsq", "f2")], by_key[("overture", "o2")], by_key[("osm", "node/2")]}
    assert len(jollibee) == 1
    assert len(mercury) == 1
    assert jollibee != mercury
    assert by_key[("fsq", "f3")] not in (jollibee | mercury)


def test_match_edges_flag_links_kept_by_the_final_clustering(built):
    edges = pq.read_table(built["edges"]).to_pylist()
    assert len(edges) == 5
    assert all(e["same_cluster_final"] for e in edges)
    assert {"left_source_id", "right_source_id", "score", "same_cluster_final"} <= set(edges[0])


def test_canonical_output_exposes_transitivity_and_provenance(built):
    rows = pq.read_table(built["canonical"]).to_pylist()
    by_name = {r["canonical_name"]: r for r in rows}

    assert set(by_name) == {"jollibee", "mercury drug", "unmatched cafe"}

    # Jollibee has all three accepted pairwise links, so it is not merely held
    # together by a transitive path.  Overture declares Foursquare provenance,
    # therefore only two of its three source layers are known-independent.
    jol = by_name["jollibee"]
    assert jol["source_count"] == 3
    assert jol["known_independent_source_count"] == 2
    assert jol["completed_transitively"] is False
    assert jol["cluster_max_pair_distance_m"] > 0

    # Mercury has FSQ<->Overture and Overture<->OSM, but no FSQ<->OSM edge.
    merc = by_name["mercury drug"]
    assert merc["source_count"] == 3
    assert merc["known_independent_source_count"] == 3
    assert merc["completed_transitively"] is True

    single = by_name["unmatched cafe"]
    assert single["source_count"] == 1
    assert single["completed_transitively"] is False


def test_canonical_is_real_geoparquet(built):
    metadata = pq.ParquetFile(built["canonical"]).schema_arrow.metadata or {}
    assert b"geo" in metadata


def test_summary_counts_transitive_clusters(built):
    summary = built["summary"]
    assert summary["observations"] == 7
    assert summary["candidate_links_accepted_by_thresholds"] == 5
    assert summary["accepted_links_within_final_cluster"] == 5
    assert summary["canonical_pois"] == 3
    assert summary["canonical_pois_completed_transitively"] == 1
    assert summary["evidence_tiers"] == {"single": 1, "triple": 2}


def test_edge_resolution_mismatch_fails_loudly(tmp_path):
    """A missing source ID must not quietly remove an accepted graph edge."""
    observations = tmp_path / "observations.parquet"
    pq.write_table(pa.table({
        "row_id": pa.array([0, 1], type=pa.int64()),
        "source": ["fsq", "osm"],
        "source_id": ["f1", "node/1"],
    }), observations)

    edge_root = tmp_path / "edges"
    _write_edges(edge_root / "fsq__osm" / "bad.parquet", [
        ("fsq", "f1", "osm", "node/MISSING", 1.0, 1.0, 0.99),
    ])

    with pytest.raises(RuntimeError, match="resolution mismatch"):
        build_edge_ids(
            observations,
            edge_root,
            tmp_path / "edge_ids.parquet",
            tmp_path / "tmp",
            "512MB",
        )
