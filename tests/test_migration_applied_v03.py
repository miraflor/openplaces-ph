"""Guard against copying the new files in without applying the migration.

The 0.3 CLI calls ``prepare_matches(..., sources=...)`` and
``finalize(..., sources=...)``, and imports ``legacy_cli``. None of those exist
until ``apply_v03.py`` edits the core modules. Copying only the new files
produces a package that imports cleanly and then fails at run time, in the
match stage, after the slow source acquisition has already finished.

These tests turn that into an immediate, readable failure.
"""

import inspect

import pytest


def _signature(module_name, function_name):
    module = pytest.importorskip(f"openplaces_ph.{module_name}")
    function = getattr(module, function_name, None)
    if function is None:
        pytest.fail(f"openplaces_ph.{module_name}.{function_name} does not exist")
    return inspect.signature(function)


@pytest.mark.parametrize(
    "module_name, function_name",
    [
        ("matching", "prepare_matches"),
        ("finalize", "finalize"),
        ("finalize", "build_observations"),
        ("snapshot", "source_snapshot"),
    ],
)
def test_core_functions_accept_a_source_set(module_name, function_name):
    signature = _signature(module_name, function_name)
    assert "sources" in signature.parameters, (
        f"{module_name}.{function_name} has no 'sources' parameter. "
        "Run apply_v03.py; the new files were copied without the migration."
    )


def test_matching_pairs_are_the_shared_source_set_object():
    matching = pytest.importorskip("openplaces_ph.matching")
    from openplaces_ph.source_set import ALL_PAIRS

    assert matching.PAIRS is ALL_PAIRS, (
        "matching.py must alias source_set.ALL_PAIRS rather than defining a "
        "second pair list. Pair order controls durable edge-directory names."
    )


def test_expected_shards_accepts_pairs():
    matching = pytest.importorskip("openplaces_ph.matching")
    function = getattr(matching, "_expected_shards", None)
    if function is None:
        pytest.skip("_expected_shards is private and may have been renamed")
    assert "pairs" in inspect.signature(function).parameters


def test_legacy_cli_exists():
    """A hard failure, not a skip: the CLI imports this module at run time."""
    import importlib.util

    found = importlib.util.find_spec("openplaces_ph.legacy_cli")
    assert found is not None, (
        "openplaces_ph.legacy_cli is missing. apply_v03.py creates it by "
        "copying the 0.2 cli.py before overwriting cli.py. Every 0.2 flag "
        "command fails without it."
    )
