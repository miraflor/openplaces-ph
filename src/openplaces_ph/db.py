"""Small DuckDB configuration helpers.

The entire project is designed around one rule: *do not let DuckDB assume it
owns the whole machine*.  A modern analytical database can eagerly use a large
fraction of RAM and all CPU threads.  That is desirable on a server, but not on
a resource-constrained laptop that is also running Windows.
"""

from __future__ import annotations

from pathlib import Path
import duckdb


def connect(
    temp_dir: Path,
    memory_limit: str = "1GB",
    threads: int = 1,
    *,
    spatial: bool = False,
    httpfs: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Return a deliberately constrained DuckDB connection.

    Parameters
    ----------
    temp_dir:
        Directory used when a join/sort does not fit in RAM. Put this on an SSD
        when possible. DuckDB will spill there rather than crashing the process.
    memory_limit:
        Hard-ish DuckDB memory budget for this connection (e.g. ``"512MB"``).
    threads:
        Keep this low on aging hardware. One DuckDB thread plus OS caching is often
        faster overall than forcing 8 logical threads to fight over RAM/disk.
    spatial/httpfs:
        Load only the extensions needed by the current stage.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)

    # A persistent database is unnecessary here; all durable state is Parquet.
    con = duckdb.connect()

    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute(f"SET threads = {max(1, int(threads))}")

    # Large COPY/GROUP BY operations can use less memory if result insertion
    # order does not have to be preserved. All ordering that matters in this
    # project is explicit with ORDER BY.
    con.execute("SET preserve_insertion_order = false")

    # Keep spill files away from the repository and, ideally, on a fast disk.
    escaped = temp_dir.as_posix().replace("'", "''")
    con.execute(f"SET temp_directory = '{escaped}'")

    # Disable interactive progress bars because the CLI already reports durable
    # checkpoints. It also keeps logs readable when a run is resumed many times.
    con.execute("SET enable_progress_bar = false")

    if spatial:
        # INSTALL is idempotent. The first run may download the DuckDB extension;
        # subsequent runs load it from DuckDB's local extension cache.
        con.execute("INSTALL spatial; LOAD spatial;")

    if httpfs:
        con.execute("INSTALL httpfs; LOAD httpfs;")

        # Remote Parquet reads can fail transiently on a long overnight run.
        # DuckDB has native HTTP retry controls, so use them *inside* each
        # remote query in addition to the outer per-tile checkpoint/retry loop.
        # This is cheap insurance on consumer broadband/Wi-Fi.
        con.execute("SET http_retries = 6")
        con.execute("SET http_timeout = 120")
        con.execute("SET http_retry_wait_ms = 500")
        con.execute("SET http_retry_backoff = 2")

    # These settings reduce peak memory during Parquet/partitioned writes.
    # The trade-off is slightly more disk I/O, which is preferable to paging or
    # an out-of-memory failure on the target laptop.
    con.execute("SET write_buffer_row_group_count = 2")
    con.execute("SET partitioned_write_max_open_files = 16")

    return con
