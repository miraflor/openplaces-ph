"""End-to-end behaviour of the resumable greedy clustering stage.

Covers the three properties the national run depends on: the one-per-source
constraint, rank order deciding who wins a contested merge, and the fact that
an interrupted run resumes to exactly the same clustering.
"""

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from openplaces_ph.finalize import cluster_edges, compress_parents

SOURCE_CODE = {"fsq": 1, "overture": 2, "osm": 4}


def _observations(tmp_path, sources, row_group_size=2):
    table = pa.table({
        "row_id": pa.array(range(len(sources)), type=pa.int64()),
        "source_code": pa.array([SOURCE_CODE[s] for s in sources], type=pa.uint8()),
    })
    path = tmp_path / "observations.parquet"
    pq.write_table(table, path, row_group_size=row_group_size)
    return path


def _ranked(tmp_path, pairs, row_group_size=100, name="ranked_pairs.parquet"):
    table = pa.table({
        "left_id": pa.array([a for a, _ in pairs], type=pa.int64()),
        "right_id": pa.array([b for _, b in pairs], type=pa.int64()),
    })
    path = tmp_path / name
    pq.write_table(table, path, row_group_size=row_group_size)
    return path


def _roots(path):
    t = pq.read_table(path)
    return dict(zip(t["row_id"].to_pylist(), t["cluster_root"].to_pylist()))


def test_one_observation_per_source(tmp_path):
    obs = _observations(tmp_path, ["fsq", "overture", "osm", "fsq"])
    ranked = _ranked(tmp_path, [(0, 1), (1, 2), (2, 3)])
    roots = _roots(cluster_edges(obs, ranked, tmp_path / "c.parquet", tmp_path / "state"))
    assert roots[0] == roots[1] == roots[2]
    # The second FSQ record cannot join a cluster that already holds one.
    assert roots[3] != roots[0]


def test_rank_order_decides_contested_merges(tmp_path):
    # Two OSM records both plausibly match the same FSQ record; only the
    # higher-ranked link (listed first) may win.
    obs = _observations(tmp_path, ["fsq", "osm", "osm"])
    ranked = _ranked(tmp_path, [(0, 2), (0, 1)])
    roots = _roots(cluster_edges(obs, ranked, tmp_path / "c.parquet", tmp_path / "state"))
    assert roots[0] == roots[2]
    assert roots[1] != roots[0]


def test_resume_reproduces_an_uninterrupted_run(tmp_path):
    rng = np.random.default_rng(4)
    sources = list(rng.choice(["fsq", "overture", "osm"], size=400))
    pairs = [(int(a), int(b)) for a, b in
             rng.integers(0, len(sources), size=(600, 2)) if a != b]

    full_dir = tmp_path / "full"
    full_dir.mkdir()
    obs = _observations(full_dir, sources, row_group_size=64)
    ranked = _ranked(full_dir, pairs, row_group_size=25)
    expected = _roots(cluster_edges(obs, ranked, full_dir / "c.parquet", full_dir / "state"))

    # Same inputs, but forcing a transactional snapshot after every row group,
    # then discarding the finished cluster file and resuming from the snapshot.
    part_dir = tmp_path / "resumed"
    part_dir.mkdir()
    obs2 = _observations(part_dir, sources, row_group_size=64)
    ranked2 = _ranked(part_dir, pairs, row_group_size=25)
    cluster_edges(obs2, ranked2, part_dir / "c.parquet", part_dir / "state",
                  checkpoint_seconds=0.0)
    (part_dir / "c.parquet").unlink()
    resumed = _roots(cluster_edges(obs2, ranked2, part_dir / "c.parquet", part_dir / "state",
                                   checkpoint_seconds=0.0))
    assert resumed == expected


def test_empty_edge_set_leaves_every_observation_singleton(tmp_path):
    obs = _observations(tmp_path, ["fsq", "osm"])
    ranked = _ranked(tmp_path, [])
    roots = _roots(cluster_edges(obs, ranked, tmp_path / "c.parquet", tmp_path / "state"))
    assert roots == {0: 0, 1: 1}


@pytest.mark.parametrize("chain", [10, 1000])
def test_vectorised_path_compression_matches_naive_find(chain):
    parent = np.arange(chain, dtype=np.int64)
    parent[1:] = np.arange(chain - 1)          # a maximally deep chain
    compressed = compress_parents(parent.copy())

    def naive(p, x):
        while p[x] != x:
            x = int(p[x])
        return x

    assert compressed.tolist() == [naive(parent, i) for i in range(chain)]


def test_checkpoint_parent_is_uint32_not_python_int_state(tmp_path):
    obs = _observations(tmp_path, ["fsq", "overture", "osm"])
    ranked = _ranked(tmp_path, [(0, 1), (1, 2)], row_group_size=1)
    state = tmp_path / "state"
    cluster_edges(obs, ranked, tmp_path / "c.parquet", state, checkpoint_seconds=0.0)

    # The durable representation mirrors the working representation: four-byte
    # parents plus one-byte rank and source mask.  This is the 8 GB-safe design.
    with np.load(state / "uf_checkpoint.npz", allow_pickle=False) as z:
        assert z["parent"].dtype == np.uint32
        assert z["rank"].dtype == np.uint8
        assert z["mask"].dtype == np.uint8
