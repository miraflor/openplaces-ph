"""General utilities with an emphasis on crash-safe file writes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq
import requests


def run_command(args: Iterable[object], cwd: Path | None = None) -> None:
    """Run an external command and fail immediately on a non-zero return code."""
    args = [str(x) for x in args]
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def atomic_json(path: Path, payload: dict) -> None:
    """Write JSON transactionally: complete file or old file, never half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def valid_parquet(path: Path) -> bool:
    """Cheap structural test used to decide whether a checkpoint is reusable."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        pq.ParquetFile(path)
        return True
    except Exception:
        return False


def resume_download(
    url: str,
    target: Path,
    refresh: bool = False,
    *,
    retries: int = 5,
) -> Path:
    """Download a large HTTP file with byte-range resume and retries.

    Crash/restart behavior
    ----------------------
    * The durable filename appears only after a complete response.
    * Incomplete bytes stay in ``*.part``.
    * The next retry **and** the next program invocation continue from that
      partial byte count when the server honors HTTP Range requests.

    This matters for the ~country-sized OSM PBF: a short Wi-Fi failure should
    not throw away hundreds of megabytes that are already on disk.
    """
    import time

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")

    if target.exists() and not refresh:
        print(f"[cache] {target}")
        return target

    if refresh:
        target.unlink(missing_ok=True)
        part.unlink(missing_ok=True)

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}

        print(f"[download] {url}")
        if have:
            print(f"[resume] {have / (1024**2):,.1f} MiB already present")

        try:
            with requests.get(
                url,
                stream=True,
                headers=headers,
                timeout=(30, 180),
            ) as r:
                # Some servers ignore Range. Restart once from byte zero rather
                # than appending a second full response to an incomplete file.
                if have and r.status_code != 206:
                    r.close()
                    part.unlink(missing_ok=True)
                    have = 0
                    raise RuntimeError(
                        "Server did not honor HTTP Range; restarting this download."
                    )

                r.raise_for_status()
                content_length = int(r.headers.get("content-length", 0))
                expected = content_length + have if content_length else 0
                got = have

                with part.open("ab" if have else "wb") as f:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        if expected:
                            print(f"\r  {got / expected:6.1%}", end="", flush=True)
                    # Push Python's userspace buffer to the OS before deciding
                    # this network attempt is complete. We do not fsync every
                    # chunk because that would be extremely slow on an old disk.
                    f.flush()

                if expected:
                    print()

            os.replace(part, target)
            return target

        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = min(30, 5 * attempt)
            current = part.stat().st_size if part.exists() else 0
            print(
                f"[download] attempt {attempt}/{retries} failed: {exc}\n"
                f"           preserving {current / (1024**2):,.1f} MiB; "
                f"retrying in {delay}s...",
                flush=True,
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def normalize_name(value: object) -> str:
    """Python equivalent of the SQL normalization used by the source pipeline.

    Primarily used by unit tests and small diagnostics; national matching does
    *not* call this function row by row. Names are normalized once at ingestion
    and compared inside DuckDB, which is much faster on an old CPU.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_paths(paths: list[Path]) -> str:
    """Create a DuckDB SQL list literal from local filesystem paths."""
    return "[" + ",".join("'" + p.as_posix().replace("'", "''") + "'" for p in paths) + "]"


def check_commands(commands: Iterable[str]) -> None:
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        raise RuntimeError(
            "Missing command(s): " + ", ".join(missing) + ". "
            "Create/activate the Conda environment from environment.yml first."
        )


def free_gb(path: Path) -> float:
    """Return free disk space at ``path`` in GiB."""
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024**3)
