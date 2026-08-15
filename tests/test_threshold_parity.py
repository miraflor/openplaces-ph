"""The SQL that produces the data and the Python that documents it must agree.

``accept_pair`` is the version people read, test, and cite; the DuckDB CASE is
the version that actually decides what lands in the canonical layer. In 0.1.0
they had drifted: at ``--max-distance 200`` the SQL accepted a 150 m link that
``accept_pair`` rejected, and no test noticed. Both are now generated from
``acceptance_bands``, and this test holds them to it.
"""

import random

import duckdb
import pytest

from openplaces_ph.matching import GENERIC_NAMES, MatchConfig, accept_pair, acceptance_sql

NAMES = sorted(GENERIC_NAMES) + [
    "jollibee", "mercury drug", "sm north edsa", "7 eleven", "bdo", "sm",
    "national bookstore", "puregold", "aling nena s carinderia", "",
]


@pytest.mark.parametrize("max_distance", [5.0, 10.0, 30.0, 60.0, 120.0, 200.0])
def test_sql_ladder_matches_python_mirror(max_distance):
    cfg = MatchConfig(max_distance_m=max_distance)
    rng = random.Random(20260815)

    rows = []
    for _ in range(4000):
        rows.append((
            rng.uniform(0.0, max_distance * 1.5),
            round(rng.uniform(0.5, 1.0), 4),
            rng.choice(NAMES),
            rng.choice(NAMES),
        ))
    # Pin the band edges explicitly; random floats rarely land on them.
    for edge in (0.0, 15.0, 20.0, 50.0, 90.0, max_distance):
        for score in (0.70, 0.82, 0.90, 0.94, 0.99, 1.0):
            rows.append((edge, score, "jollibee", "jollibee"))
            rows.append((edge, score, "atm", "atm"))

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t(distance_m DOUBLE, name_score DOUBLE, "
            "name_left VARCHAR, name_right VARCHAR)"
        )
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
        result = con.execute(
            f"SELECT distance_m, name_score, name_left, name_right, "
            f"{acceptance_sql(cfg)} AS accepted FROM t"
        ).fetchall()
    finally:
        con.close()

    assert len(result) == len(rows)
    for distance_m, name_score, left, right, sql_accepted in result:
        expected = accept_pair(distance_m, name_score, left, right, max_distance_m=max_distance)
        assert bool(sql_accepted) == expected, (distance_m, name_score, left, right)


def test_nothing_beyond_the_radius_survives_the_sql():
    cfg = MatchConfig(max_distance_m=120.0)
    con = duckdb.connect()
    try:
        accepted = con.execute(
            "SELECT " + acceptance_sql(cfg) + " FROM (SELECT 121.0 AS distance_m, "
            "1.0 AS name_score, 'jollibee' AS name_left, 'jollibee' AS name_right)"
        ).fetchone()[0]
    finally:
        con.close()
    assert accepted is False
