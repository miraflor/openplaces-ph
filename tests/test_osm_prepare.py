"""OSM preparation with real Osmium against a local copy of "Geofabrik".

Skipped when the ``osmium`` command is not installed (for example on the
plain-pip CI job); the Conda environment in environment.yml provides it.
"""

import shutil
import subprocess

import pyarrow.parquet as pq
import pytest

from openplaces_ph import sources
from openplaces_ph.util import read_json

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")

OSM_V1 = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="14.5000" lon="120.9800">
    <tag k="amenity" v="restaurant"/><tag k="name" v="Jollibee"/>
  </node>
  <node id="2" version="1" lat="14.5100" lon="120.9900"><tag k="amenity" v="bench"/></node>
  <node id="3" version="1" lat="14.5200" lon="121.0100"><tag k="name" v="Just A Name"/></node>
  <node id="10" version="1" lat="14.6000" lon="121.0500"/>
  <node id="11" version="1" lat="14.6000" lon="121.0510"/>
  <node id="12" version="1" lat="14.6010" lon="121.0510"/>
  <node id="13" version="1" lat="14.6010" lon="121.0500"/>
  EXTRA
  <way id="100" version="1">
    <nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="10"/>
    <tag k="shop" v="mall"/><tag k="name" v="SM North EDSA"/>
  </way>
</osm>
"""
# Node ids must stay sorted before ways (Osmium requires ordered input).
EXTRA_V2 = """<node id="14" version="1" lat="14.5300" lon="120.9700">
    <tag k="shop" v="chemist"/><tag k="name" v="Mercury Drug"/>
  </node>"""


def _pbf(tmp_path, name, extra=""):
    xml = tmp_path / f"{name}.osm"
    xml.write_text(OSM_V1.replace("EXTRA", extra), encoding="utf-8")
    pbf = tmp_path / f"{name}.osm.pbf"
    subprocess.run(["osmium", "cat", str(xml), "-o", str(pbf), "-O"], check=True)
    return pbf.read_bytes()


def _names(tiles_dir):
    return {
        (r["source_id"], r["name"]): (round(r["lon"], 4), round(r["lat"], 4))
        for path in tiles_dir.rglob("*.parquet")
        for r in pq.read_table(path).to_pylist()
    }


def _gets(up):
    return sum(1 for method, _ in up.log if method == "GET")


def test_osm_preparation_refresh_is_sticky_and_versioned(tmp_path, upstream, monkeypatch):
    upstream.publish(_pbf(tmp_path, "v1"), '"pbf-v1"')
    monkeypatch.setattr(sources, "GEOFABRIK_PBF", upstream.url)
    root, temp = tmp_path / "project", tmp_path / "tmp"

    tiles = sources.prepare_osm(root, 1.0, temp, "256MB")
    first = _names(tiles)
    assert set(first) == {("node/1", "Jollibee"), ("way/100", "SM North EDSA")}
    # The mall polygon is reduced to a point inside its footprint.
    lon, lat = first[("way/100", "SM North EDSA")]
    assert 121.05 <= lon <= 121.051 and 14.60 <= lat <= 14.601
    assert read_json(tiles / "_SUCCESS.json")["pbf"]["etag"] == '"pbf-v1"'

    # A normal rerun and a refresh of an unchanged extract download nothing.
    sources.prepare_osm(root, 1.0, temp, "256MB")
    sources.prepare_osm(root, 1.0, temp, "256MB", refresh=True)
    assert _gets(upstream) == 1

    # A new extract is published, and the refresh fails part-way (network).
    upstream.publish(_pbf(tmp_path, "v2", EXTRA_V2), '"pbf-v2"')
    upstream.fail_status = 503
    with pytest.raises(Exception):
        sources.prepare_osm(root, 1.0, temp, "256MB", refresh=True)
    # The previous tiles are still complete and readable meanwhile.
    assert _names(tiles) == first

    # The next run continues the refresh even WITHOUT the flag.
    upstream.fail_status = None
    tiles = sources.prepare_osm(root, 1.0, temp, "256MB")
    assert ("node/14", "Mercury Drug") in _names(tiles)
    assert read_json(tiles / "_SUCCESS.json")["pbf"]["etag"] == '"pbf-v2"'
    assert not (root / "data" / "cache" / "osm" / "_REFRESH_PENDING.json").exists()
    # Intermediates of the old extract were removed.
    assert not list((root / "data" / "work" / "osm").glob("osm-poi*"))
