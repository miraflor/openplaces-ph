import pytest

from openplaces_ph.maintenance import (
    CleanupRefused,
    assert_removable,
    cleanup_targets,
    execute_cleanup,
    human_bytes,
    infer_output_sources,
    size_bytes,
)


def test_size_bytes_and_human_bytes(tmp_path):
    p = tmp_path / "x"
    p.mkdir()
    (p / "a.bin").write_bytes(b"x" * 1024)
    assert size_bytes(p) == 1024
    assert human_bytes(1024) == "1.00 KiB"
    assert size_bytes(tmp_path / "missing") == 0


def test_default_cleanup_never_removes_shared_osm_cache(tmp_path):
    root = tmp_path / "repo"
    cache = root / "data" / "cache" / "osm" / "tiles_1p0deg"
    cache.mkdir(parents=True)
    targets = cleanup_targets(root, "areas_demo")
    paths = {t.path for t in targets}
    assert (root / "data" / "tmp").resolve() in paths
    assert (root / "data" / "work" / "areas_demo").resolve() in paths
    assert cache.resolve() not in paths


def test_shared_osm_cache_requires_explicit_flag(tmp_path):
    root = tmp_path / "repo"
    cache = root / "data" / "cache" / "osm" / "tiles_1p0deg"
    cache.mkdir(parents=True)
    targets = cleanup_targets(root, None, remove_shared_osm_cache=True)
    assert cache.resolve() in {t.path for t in targets}


def test_all_flag_adds_sources_and_output(tmp_path):
    root = tmp_path / "repo"
    targets = cleanup_targets(
        root, "areas_demo", remove_sources=True, remove_output=True
    )
    paths = {t.path for t in targets}
    assert (root / "data" / "sources" / "areas_demo").resolve() in paths
    assert (root / "data" / "output" / "areas_demo").resolve() in paths


@pytest.mark.parametrize("slug", ["", ".", "..", "../../etc", "a/b", "a\\b", " x"])
def test_bad_scope_slugs_are_refused(tmp_path, slug):
    with pytest.raises(CleanupRefused):
        cleanup_targets(tmp_path / "repo", slug)


def test_assert_removable_rejects_paths_outside_data(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    assert_removable(root / "data" / "tmp", root)
    with pytest.raises(CleanupRefused):
        assert_removable(root / "src", root)
    with pytest.raises(CleanupRefused):
        assert_removable(tmp_path, root)


def test_execute_cleanup_removes_only_listed_paths(tmp_path):
    root = tmp_path / "repo"
    keep = root / "src" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("keep", encoding="utf-8")
    spill = root / "data" / "tmp"
    spill.mkdir(parents=True)
    (spill / "junk.bin").write_bytes(b"0" * 16)

    execute_cleanup(cleanup_targets(root, None), root)
    assert not spill.exists()
    assert keep.exists()


def test_infer_sources_from_new_summary(tmp_path):
    root = tmp_path / "repo"
    out = root / "data" / "output" / "areas_demo"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(
        '{"sources": {"active_sources": ["osm", "overture"]}}', encoding="utf-8"
    )
    assert infer_output_sources(root, "areas_demo") == ("overture", "osm")


def test_infer_sources_from_legacy_summary(tmp_path):
    root = tmp_path / "repo"
    out = root / "data" / "output" / "areas_demo"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(
        '{"sources": {"fsq_release": "a", "overture_release": "b", "osm": {}}}',
        encoding="utf-8",
    )
    assert infer_output_sources(root, "areas_demo") == ("fsq", "overture", "osm")


def test_infer_sources_returns_none_when_absent(tmp_path):
    assert infer_output_sources(tmp_path, "areas_demo") is None


def test_output_license_values_reads_distinct_source_licenses(tmp_path):
    duckdb = __import__("pytest").importorskip("duckdb")
    from openplaces_ph.maintenance import output_license_values

    out = tmp_path / "data" / "output" / "areas_demo"
    out.mkdir(parents=True)
    path = out / "canonical_pois.parquet"
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT * FROM (VALUES
                ('Apache-2.0', 'CDLA-Permissive-2.0', 'ODbL-1.0'),
                (NULL, 'Apache-2.0', 'ODbL-1.0')
            ) t(fsq_license, overture_license, osm_license)
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
        """
    )
    con.close()

    values = output_license_values(tmp_path, "areas_demo")
    assert values["fsq"] == ["Apache-2.0"]
    assert values["overture"] == ["Apache-2.0", "CDLA-Permissive-2.0"]
    assert values["osm"] == ["ODbL-1.0"]


def _write_valid_canonical_for_run_manifest_test(root, slug="areas_demo"):
    duckdb = pytest.importorskip("duckdb")
    out = root / "data" / "output" / slug
    out.mkdir(parents=True, exist_ok=True)
    path = out / "canonical_pois.parquet"
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT
                'overture:1'::VARCHAR AS canonical_id,
                'Demo Place'::VARCHAR AS canonical_name,
                120.5::DOUBLE AS lon,
                14.5::DOUBLE AS lat,
                2::INTEGER AS source_count,
                'double'::VARCHAR AS evidence_tier,
                NULL::VARCHAR AS fsq_id,
                'ov-1'::VARCHAR AS overture_id,
                'osm-1'::VARCHAR AS osm_id
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
        """
    )
    con.close()
    return out


def test_validate_cross_checks_run_manifest_and_warns_on_dirty_tree(tmp_path, capsys):
    from openplaces_ph.config import Scope
    from openplaces_ph.maintenance import validate_output

    out = _write_valid_canonical_for_run_manifest_test(tmp_path)
    (out / "run.json").write_text(
        '{"active_sources": ["overture", "osm"], "git_dirty": true}',
        encoding="utf-8",
    )
    scope = Scope("areas_demo", ((120.0, 14.0, 121.0, 15.0),), False)

    assert validate_output(
        tmp_path,
        scope,
        sources=("overture", "osm"),
        strict=False,
        as_json=False,
    )
    output = capsys.readouterr().out
    assert "run_built_from_dirty_tree" in output
    assert "warn" in output

    assert not validate_output(
        tmp_path,
        scope,
        sources=("overture", "osm"),
        strict=True,
        as_json=False,
    )


def test_validate_fails_when_run_manifest_source_set_disagrees(tmp_path):
    from openplaces_ph.config import Scope
    from openplaces_ph.maintenance import validate_output

    out = _write_valid_canonical_for_run_manifest_test(tmp_path)
    (out / "run.json").write_text(
        '{"active_sources": ["fsq", "osm"], "git_dirty": false}',
        encoding="utf-8",
    )
    scope = Scope("areas_demo", ((120.0, 14.0, 121.0, 15.0),), False)

    assert not validate_output(
        tmp_path,
        scope,
        sources=("overture", "osm"),
        strict=False,
        as_json=True,
    )


def test_validate_accepts_a_structurally_valid_empty_output(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from openplaces_ph.config import Scope
    from openplaces_ph.maintenance import validate_output

    out = tmp_path / "data" / "output" / "areas_empty"
    out.mkdir(parents=True)
    path = out / "canonical_pois.parquet"
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT
                NULL::VARCHAR AS canonical_id,
                NULL::VARCHAR AS canonical_name,
                NULL::DOUBLE AS lon,
                NULL::DOUBLE AS lat,
                NULL::INTEGER AS source_count,
                NULL::VARCHAR AS evidence_tier,
                NULL::VARCHAR AS fsq_id,
                NULL::VARCHAR AS overture_id,
                NULL::VARCHAR AS osm_id
            WHERE false
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
        """
    )
    con.close()
    (out / "run.json").write_text(
        '{"active_sources": ["overture", "osm"], "git_dirty": false}',
        encoding="utf-8",
    )

    scope = Scope("areas_empty", ((120.0, 14.0, 121.0, 15.0),), False)
    assert validate_output(
        tmp_path,
        scope,
        sources=("overture", "osm"),
        strict=False,
        as_json=False,
    )

