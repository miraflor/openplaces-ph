"""Finalization on degenerate but legal inputs."""

import json

import pyarrow.parquet as pq

from openplaces_ph.config import Scope
from openplaces_ph.finalize import finalize
from openplaces_ph.matching import MatchConfig, prepare_matches
from openplaces_ph.tiles import Tile


def test_an_area_without_any_poi_finalizes(project):
    t = Tile(120, 14, 1.0)
    scope = Scope(name="test", bboxes=((120.0, 14.0, 121.0, 15.0),))
    for source in ("fsq", "overture", "osm"):
        project.tile(source, t, [])
    edge_root = prepare_matches(project.root, scope, [t], 1.0, 0.25, project.fsq_dir,
                                project.overture_dir, project.osm_dir, 1, project.temp_dir,
                                "256MB", MatchConfig())
    summary = finalize(project.root, scope, [t], project.fsq_dir, project.overture_dir,
                       project.osm_dir, edge_root, project.temp_dir, "256MB")
    # 0.2.0 failed here twice over: DuckDB wrote no GeoParquet metadata for
    # zero rows, and map_from_entries() returned NULL in write_summary.
    assert summary["canonical_pois"] == 0
    assert summary["evidence_tiers"] == {}
    canonical = project.root / "data" / "output" / scope.slug / "canonical_pois.parquet"
    geo = json.loads(pq.ParquetFile(canonical).schema_arrow.metadata[b"geo"])
    assert geo["primary_column"] == "geometry"
