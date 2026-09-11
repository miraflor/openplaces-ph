"""Run the Overture boundary, tile mask, and normalization SQL on local data.

``_stream_overture_tile`` is replaced by a fake that writes GeoParquet with
the same bbox semantics as the overturemaps reader: strict inequalities on
both sides (row xmax > query xmin, row xmin < query xmax, same for y).
"""

import pyarrow.parquet as pq
import pytest

from openplaces_ph import sources
from openplaces_ph.config import Scope
from openplaces_ph.db import connect
from openplaces_ph.tiles import Tile

RELEASE = "2026-01-01.0"
LAND = "POLYGON((120 14, 122 14, 122 14.9, 120 14.9, 120 14))"
PLACES = [
    # id, name, lon, lat, operating_status, dataset
    ("edge", "Pier Uno", 121.0, 14.5, "open", "meta"),        # exactly on a block edge
    ("inner", "Jollibee", 120.5, 14.5, "open", "Foursquare"),
    ("closed", "Old Shop", 120.6, 14.6, "permanently_closed", "meta"),
    ("coast", "Harbor Grill", 120.5, 14.901, "open", "meta"),  # 111 m off the land edge
    ("sea", "Sea Point", 120.5, 14.95, "open", "meta"),        # ~5.5 km offshore
]


@pytest.fixture
def fake_overture(monkeypatch, tmp_path):
    requested = []

    def fake_stream(overture_type, bbox, release, target, *, retries=3):
        assert release == RELEASE
        requested.append((overture_type, bbox))
        target.parent.mkdir(parents=True, exist_ok=True)
        con = connect(tmp_path / "fake_tmp", "256MB", 1, spatial=True)
        try:
            if overture_type == "division_area":
                con.execute(
                    f"COPY (SELECT 'PH' AS country, 'country' AS subtype, true AS is_land, "
                    f"ST_GeomFromText('{LAND}') AS geometry) TO '{target.as_posix()}' (FORMAT PARQUET)"
                )
                return True
            xmin, ymin, xmax, ymax = bbox
            rows = [p for p in PLACES if xmin < p[2] < xmax and ymin < p[3] < ymax]
            if not rows:
                return False
            values = ", ".join(f"('{i}', '{n}', {x}, {y}, '{s}', '{d}')" for i, n, x, y, s, d in rows)
            con.execute(
                f"""COPY (
                    SELECT id, {{'primary': name}} AS names,
                           {{'primary': 'restaurant'}} AS categories,
                           [{{'property': '', 'dataset': dataset, 'record_id': id}}] AS sources,
                           status AS operating_status,
                           ST_Point(lon, lat) AS geometry
                    FROM (VALUES {values}) v(id, name, lon, lat, status, dataset)
                ) TO '{target.as_posix()}' (FORMAT PARQUET)"""
            )
            return True
        finally:
            con.close()

    monkeypatch.setattr(sources, "_stream_overture_tile", fake_stream)
    return requested


def test_overture_normalization_keeps_edge_and_coastal_points(tmp_path, fake_overture):
    root = tmp_path / "project"
    temp = tmp_path / "tmp"
    scope = Scope(name="test", bboxes=((120.0, 14.0, 122.0, 15.0),))

    boundary = sources.prepare_overture_boundary(root, temp, "256MB", RELEASE)
    tiles = sources.filter_tiles_to_boundary(
        sources.source_tiles_for_scope(scope, 1.0), boundary, temp, "256MB"
    )
    assert tiles == [Tile(120, 14, 1.0), Tile(121, 14, 1.0)]

    out_dir = sources.prepare_overture(root, scope, tiles, RELEASE, boundary, 1, temp, "256MB")
    rows = {}
    for tile in tiles:
        table = pq.read_table(out_dir / f"{tile.key}.parquet")
        for r in table.to_pylist():
            assert r["source_id"] not in rows, "a place was written to two tiles"
            rows[r["source_id"]] = (tile, r)

    # 0.2.0 requested the exact tile bbox and lost the point on lon=121.0.
    assert rows["edge"][0] == Tile(121, 14, 1.0)
    assert rows["coast"][1]["name_norm"] == "harbor grill"      # land tolerance
    assert "sea" not in rows                                      # clearly offshore
    assert "closed" not in rows                                   # operating_status
    assert rows["inner"][1]["upstream_license"] == "Apache-2.0"
    assert rows["edge"][1]["upstream_license"] == "CDLA-Permissive-2.0"

    place_bboxes = [bbox for kind, bbox in fake_overture if kind == "place"]
    assert all(bbox[0] < tile.west for bbox, tile in zip(place_bboxes, tiles))
