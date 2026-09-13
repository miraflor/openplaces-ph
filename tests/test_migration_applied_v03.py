"""Guard against copying the task-oriented CLI without the core migration.

The current CLI calls ``prepare_matches(..., sources=...)`` and
``finalize(..., sources=...)``. Those source-aware signatures are part of the
pipeline contract. These tests turn a partial migration into an immediate,
readable failure instead of a run-time failure after source acquisition.
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
        "The source-aware core migration is incomplete."
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
