from argparse import Namespace

import pytest

from openplaces_ph.cli import (
    _delegate_legacy,
    build_parser,
    main,
    resolve_target,
    selected_sources,
)

AREAS_YML = (
    "philippines_bbox: [116.8, 4.4, 126.7, 21.3]\n"
    "areas:\n"
    "  demo: [120, 14, 121, 15]\n"
    "  iloilo_city: [122.5, 10.6, 122.7, 10.8]\n"
)


@pytest.fixture
def repo(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "areas.yml").write_text(AREAS_YML, encoding="utf-8")
    return tmp_path


def target(**kwargs):
    base = dict(areas=[], philippines=False, bbox=None, name=None)
    base.update(kwargs)
    return Namespace(**base)


def test_no_target_is_rejected(repo):
    with pytest.raises(SystemExit):
        resolve_target(target(), repo)


def test_no_target_is_allowed_when_optional(repo):
    assert resolve_target(target(), repo, optional=True) is None


def test_explicit_area_resolves(repo):
    scope = resolve_target(target(areas=["demo"]), repo)
    assert scope.name == "areas_demo"


def test_unknown_area_suggests_a_near_match(repo):
    with pytest.raises(SystemExit) as excinfo:
        resolve_target(target(areas=["iloilo_ciyt"]), repo)
    message = str(excinfo.value)
    assert "Unknown area" in message
    assert "iloilo_city" in message


def test_two_target_modes_are_rejected(repo):
    with pytest.raises(SystemExit):
        resolve_target(target(areas=["demo"], philippines=True), repo)


def test_bbox_requires_a_name(repo):
    with pytest.raises(SystemExit):
        resolve_target(target(bbox=[120.0, 14.0, 121.0, 15.0]), repo)


def test_bbox_rejects_inverted_extent(repo):
    with pytest.raises(SystemExit):
        resolve_target(
            target(bbox=[121.0, 14.0, 120.0, 15.0], name="oops"), repo
        )


def test_bbox_name_is_sanitised(repo):
    scope = resolve_target(
        target(bbox=[120.0, 14.0, 121.0, 15.0], name="Cebu City!"), repo
    )
    assert scope.name == "bbox_Cebu_City"


def test_missing_area_config_gives_a_clear_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        resolve_target(target(areas=["demo"]), tmp_path)
    assert "areas.yml" in str(excinfo.value)


def test_default_source_set():
    assert selected_sources(Namespace(sources=None, with_foursquare=False)) == (
        "overture",
        "osm",
    )


def test_with_foursquare_extends_the_default_set():
    args = Namespace(sources=None, with_foursquare=True)
    assert selected_sources(args) == ("fsq", "overture", "osm")


def test_explicit_single_source():
    args = Namespace(sources=["osm"], with_foursquare=False)
    assert selected_sources(args) == ("osm",)


def test_unknown_source_exits_cleanly_instead_of_raising():
    args = Namespace(sources=["overtue"], with_foursquare=False)
    with pytest.raises(SystemExit) as excinfo:
        selected_sources(args)
    assert "Unknown source" in str(excinfo.value)


def test_status_falls_back_to_the_recorded_source_set():
    args = Namespace(sources=None, with_foursquare=False)
    assert selected_sources(args, fallback=("fsq", "osm")) == ("fsq", "osm")


def test_bare_legacy_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        _delegate_legacy([])
    assert "national build" in str(excinfo.value)


def test_bare_invocation_prints_help_and_does_not_build(capsys):
    main([])
    out = capsys.readouterr().out
    assert "usage: openplaces" in out
    assert "build" in out


def test_version_is_handled_by_the_new_parser(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "openplaces" in capsys.readouterr().out


def test_clean_uses_source_tiles_not_sources():
    parser = build_parser()
    args = parser.parse_args(["clean", "demo", "--source-tiles"])
    assert args.remove_sources is True
    with pytest.raises(SystemExit):
        parser.parse_args(["clean", "demo", "--sources", "osm"])


def test_code_identity_refuses_an_unrelated_git_tree(monkeypatch, tmp_path):
    import subprocess
    import openplaces_ph.cli as cli

    monkeypatch.setattr(cli, "checkout_root", lambda: tmp_path)

    def fake_check_output(args, **_kwargs):
        if args[:3] == ["git", "ls-files", "--error-unmatch"]:
            raise subprocess.CalledProcessError(1, args)
        raise AssertionError(f"unexpected git call after failed ownership check: {args}")

    monkeypatch.setattr(cli.subprocess, "check_output", fake_check_output)
    assert cli.code_identity() == {"git_commit": None, "git_dirty": None}
