from openplaces_ph.licensing import (
    SOURCE_TERMS,
    attribution_text,
    has_odbl_source,
    licence_block,
    requires_share_alike,
)
from openplaces_ph.source_set import ALL_SOURCES


def test_every_source_has_terms():
    for source in ALL_SOURCES:
        terms = SOURCE_TERMS[source]
        assert terms["license"]
        assert terms["attribution"]
        assert terms["terms_url"].startswith("https://")


def test_overture_is_not_forced_to_one_blanket_license():
    assert SOURCE_TERMS["overture"]["license"] == "MULTI-LICENSE"


def test_odbl_presence_is_provenance_not_combined_license_decision():
    assert has_odbl_source(("overture", "osm"))
    assert has_odbl_source(("osm",))
    assert not has_odbl_source(("overture",))
    # Compatibility alias has the same factual meaning, but no longer asserts
    # that the combined output itself must be ODbL.
    assert requires_share_alike(("osm",))


def test_licence_block_lists_selected_sources_only():
    block = licence_block(("overture",))
    assert [entry["source"] for entry in block["sources"]] == ["overture"]
    assert block["combined_output_license"] == "UNDETERMINED"
    assert block["contains_odbl_source"] is False


def test_licence_block_carries_releases_and_observed_values():
    block = licence_block(
        ("overture", "osm"),
        {"overture_release": "2026-08-20.0", "osm": {"pbf": "x"}},
        observed_licenses={
            "overture": ["Apache-2.0", "CDLA-Permissive-2.0"],
            "osm": ["ODbL-1.0"],
        },
    )
    by_source = {entry["source"]: entry for entry in block["sources"]}
    assert by_source["overture"]["release"] == "2026-08-20.0"
    assert by_source["overture"]["declared_license"] == "MULTI-LICENSE"
    assert by_source["overture"]["observed_licenses"] == [
        "Apache-2.0",
        "CDLA-Permissive-2.0",
    ]
    assert block["contains_odbl_source"] is True
    assert block["combined_output_license"] == "UNDETERMINED"


def test_attribution_text_is_cautious_and_source_specific():
    text = attribution_text(
        ("overture", "osm"),
        scope_name="areas_demo",
        observed_licenses={"overture": ["Apache-2.0", "CDLA-Permissive-2.0"]},
    )
    assert "OpenStreetMap" in text
    assert "Overture" in text
    assert "Foursquare" not in text
    assert "areas_demo" in text
    assert "MULTI-LICENSE" in text
    assert "Apache-2.0, CDLA-Permissive-2.0" in text
    assert "not a legal conclusion" in text
