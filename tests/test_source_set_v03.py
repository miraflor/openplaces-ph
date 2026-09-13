import pytest

from openplaces_ph.source_set import (
    ALL_PAIRS,
    ALL_SOURCES,
    DEFAULT_SOURCES,
    SourceSelectionError,
    display_sources,
    is_default,
    normalize_sources,
    pair_dir_name,
    source_pairs,
)


def test_default_is_zero_auth_pair():
    assert DEFAULT_SOURCES == ("overture", "osm")
    assert normalize_sources(None) == ("overture", "osm")
    assert is_default(("osm", "overture"))


def test_aliases_are_normalized_and_deduplicated():
    assert normalize_sources(
        ["foursquare", "openstreetmap", "fsq", "overture", "OSM", " Overture "]
    ) == ALL_SOURCES


def test_order_is_independent_of_caller_order():
    assert normalize_sources(["osm", "fsq"]) == normalize_sources(["fsq", "osm"])
    assert normalize_sources(["osm", "fsq"]) == ("fsq", "osm")


def test_pair_generation_is_dynamic():
    assert source_pairs(("overture", "osm")) == (("overture", "osm"),)
    assert source_pairs(("fsq",)) == ()
    assert len(source_pairs(ALL_SOURCES)) == 3


def test_pair_members_follow_all_sources_order():
    for a, b in ALL_PAIRS:
        assert ALL_SOURCES.index(a) < ALL_SOURCES.index(b)


def test_all_pairs_is_the_full_set():
    assert ALL_PAIRS == source_pairs(ALL_SOURCES)
    assert ALL_PAIRS == (("fsq", "overture"), ("fsq", "osm"), ("overture", "osm"))


def test_pair_dir_name_is_stable():
    assert pair_dir_name("overture", "osm") == "overture__osm"


def test_unknown_source_fails_with_a_typed_error():
    with pytest.raises(SourceSelectionError) as excinfo:
        normalize_sources(["overtue"])
    assert "Unknown source" in str(excinfo.value)
    assert "overture" in str(excinfo.value)


def test_empty_selection_fails():
    with pytest.raises(SourceSelectionError):
        normalize_sources([])


def test_source_selection_error_is_a_value_error():
    assert issubclass(SourceSelectionError, ValueError)


def test_display_names():
    assert display_sources(("osm", "overture")) == "Overture Maps Places, OpenStreetMap"
