"""Overture licence labels must come from provider dataset names only."""

import duckdb
import pyarrow.parquet as pq
import pytest

from openplaces_ph.config import Scope
from openplaces_ph.finalize import finalize
from openplaces_ph.matching import MatchConfig, prepare_matches
from openplaces_ph.sources import (
    FSQ_PROVENANCE_PATTERN,
    overture_fsq_provenance_sql,
    overture_license_sql,
)
from openplaces_ph.tiles import Tile


def _src(*parts):
    """Serialize like DuckDB's CAST(sources AS VARCHAR)."""
    return "[" + ", ".join(
        "{'property': '', 'dataset': %s, 'record_id': %s, 'update_time': '2026-01-01T00:00:00Z'}" % p
        for p in parts
    ) + "]"


CASES = [
    # (provenance, expected licence, declares Foursquare)
    (_src(("Foursquare", "4b0588f1f964a520aa3a22e3")), "Apache-2.0", True),
    # 0.2.0 matched "dac" inside this record id and said MIXED/REVIEW.
    (_src(("Foursquare", "4b0588f1f964a52dac3a22e3")), "Apache-2.0", True),
    (_src(("meta", "1234567890")), "CDLA-Permissive-2.0", False),
    # 0.2.0 matched "meta" inside the spider name and said MIXED/REVIEW.
    (_src(("AllThePlaces", "metamart_ph/1")), "CC0-1.0", False),
    # 0.2.0 labelled an unknown provider CDLA because of "dac" in its id.
    (_src(("NewProvider", "00dac000")), "UNKNOWN", False),
    (_src(("meta", "1"), ("Foursquare", "2")), "MIXED/REVIEW", True),
    (_src(("fsq", "2")), "Apache-2.0", True),
    ("[{'dataset': 'Foo, Bar', 'record_id': 1}]", "UNKNOWN", False),
    ("[{'dataset':'Foursquare'}]", "Apache-2.0", True),
    # No parsable dataset key: fall back to the whole text (0.2.0 behaviour).
    ("provenance text mentioning Foursquare", "Apache-2.0", True),
    (None, "UNKNOWN", False),
]


@pytest.mark.parametrize("provenance, licence, fsq", CASES)
def test_overture_licence_and_foursquare_flag(provenance, licence, fsq):
    con = duckdb.connect()
    try:
        got = con.execute(
            f"SELECT {overture_license_sql('p')}, {overture_fsq_provenance_sql('p')} "
            "FROM (SELECT ?::VARCHAR AS p)", [provenance]
        ).fetchone()
    finally:
        con.close()
    assert got == (licence, fsq)


def test_foursquare_pattern_has_single_backslashes():
    # 0.2.0 emitted \\bfsq\\b into SQL, which RE2 reads as a literal backslash.
    assert FSQ_PROVENANCE_PATTERN == "foursquare|" + "\\" + "bfsq" + "\\" + "b"


def test_finalize_rederives_overture_licences_from_provenance(project):
    t = Tile(120, 14, 1.0)
    scope = Scope(name="test", bboxes=((120.0, 14.0, 121.0, 15.0),))
    project.tile("fsq", t, [])
    project.tile("osm", t, [])
    # Tiles downloaded by 0.2.0 carry the old label; the fixture writes "test".
    project.tile("overture", t, [
        ("o1", "Jollibee", 120.5, 14.5, _src(("Foursquare", "4b0588f1f964a52dac3a22e3"))),
    ])
    edge_root = prepare_matches(project.root, scope, [t], 1.0, 0.25, project.fsq_dir,
                                project.overture_dir, project.osm_dir, 1, project.temp_dir,
                                "256MB", MatchConfig())
    finalize(project.root, scope, [t], project.fsq_dir, project.overture_dir, project.osm_dir,
             edge_root, project.temp_dir, "256MB")
    out = project.root / "data" / "output" / scope.slug
    obs = pq.read_table(out / "observations.parquet", columns=["upstream_license"])
    canonical = pq.read_table(out / "canonical_pois.parquet", columns=["overture_license"])
    assert obs["upstream_license"].to_pylist() == ["Apache-2.0"]
    assert canonical["overture_license"].to_pylist() == ["Apache-2.0"]
