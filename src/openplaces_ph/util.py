"""General utilities with an emphasis on crash-safe file writes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
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
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True))
        fh.flush()
        # Manifests decide whether hours of checkpoints are reused, so make the
        # bytes durable before the rename publishes them.
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """Return a JSON object from ``path``, or ``None`` if absent or unreadable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def valid_parquet(path: Path) -> bool:
    """Cheap structural test used to decide whether a checkpoint is reusable."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        pq.ParquetFile(path)
        return True
    except Exception:
        return False


# Bytes read before each write. A dropped connection loses at most one chunk;
# larger chunks mean fewer system calls on an old disk.
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024


def _meta_path(path: Path) -> Path:
    """Sidecar that records which upstream version a (partial) file holds."""
    return path.with_name(path.name + ".meta.json")


def _response_meta(url: str, response: requests.Response, total: int | None) -> dict:
    return {
        "url": url,
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "bytes": total,
    }


def _if_range_validator(meta: dict | None) -> str | None:
    """A validator usable in ``If-Range``: a strong ETag, else Last-Modified."""
    if not meta:
        return None
    etag = meta.get("etag")
    if etag and not str(etag).startswith("W/"):
        return str(etag)
    return meta.get("last_modified") or None


def _same_version(local: dict | None, remote: dict | None) -> bool:
    if not local or not remote:
        return False
    if local.get("etag") and remote.get("etag"):
        return local["etag"] == remote["etag"]
    if local.get("last_modified") and remote.get("last_modified"):
        return (
            local["last_modified"] == remote["last_modified"]
            and local.get("bytes") == remote.get("bytes")
        )
    return False


def _content_range(header: str | None) -> tuple[int | None, int | None]:
    """Parse ``bytes 100-199/1000`` or ``bytes */1000`` into (start, total)."""
    if not header:
        return None, None
    match = re.match(r"\s*bytes\s+(?:(\d+)-\d+|\*)/(\d+|\*)", header)
    if not match:
        return None, None
    start = int(match.group(1)) if match.group(1) else None
    total = int(match.group(2)) if match.group(2) != "*" else None
    return start, total


def download_identity(path: Path) -> dict:
    """Identity of a completed download, recorded next to derived checkpoints."""
    meta = read_json(_meta_path(path))
    if meta:
        return {k: meta.get(k) for k in ("url", "etag", "last_modified", "bytes")}
    # Downloaded by <= 0.2.0, which kept no sidecar.
    stat = path.stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _download_attempt(url: str, part: Path, part_meta: Path, timeout) -> None:
    """One network attempt that leaves ``part`` complete or raises."""
    have = part.stat().st_size if part.exists() else 0
    # Byte counts below must describe the bytes on disk, so ask the server not
    # to apply a transfer encoding that ``iter_content`` would silently undo.
    headers = {"Accept-Encoding": "identity"}
    if have:
        validator = _if_range_validator(read_json(part_meta))
        if validator is None:
            # Nothing proves that these bytes belong to the file the server
            # holds *now*. Geofabrik replaces its extract daily; appending the
            # tail of a newer file to the head of an older one produces a PBF
            # of exactly the right length that Osmium cannot read.
            print("[download] partial file has no recorded upstream version; restarting at byte 0")
            part.unlink(missing_ok=True)
            have = 0
        else:
            # If-Range: continue only if the upstream file is still the same
            # version; otherwise the server answers 200 with the whole new file.
            headers.update({"Range": f"bytes={have}-", "If-Range": validator})

    print(f"[download] {url}")
    if have:
        print(f"[resume] {have / (1024**2):,.1f} MiB already present")

    with requests.get(url, stream=True, headers=headers, timeout=timeout) as r:
        if have and r.status_code == 416:
            _, total = _content_range(r.headers.get("Content-Range"))
            if total == have:
                return  # already complete: the process stopped just before promotion
            part.unlink(missing_ok=True)
            raise RuntimeError("Server rejected the resume range; restarting at byte 0.")
        r.raise_for_status()

        if have and r.status_code == 206:
            start, total = _content_range(r.headers.get("Content-Range"))
            if start != have:
                part.unlink(missing_ok=True)
                raise RuntimeError("Server resumed at an unexpected offset; restarting at byte 0.")
            if total is None and r.headers.get("Content-Length"):
                total = have + int(r.headers["Content-Length"])
            mode = "ab"
        else:
            if have:
                print("[download] upstream file changed (or Range unsupported); restarting at byte 0")
            have = 0
            total = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
            # Order matters: remove the old bytes before recording the new
            # version, so a validator never describes bytes it did not produce.
            part.unlink(missing_ok=True)
            atomic_json(part_meta, _response_meta(url, r, total))
            mode = "wb"

        got = have
        with part.open(mode) as f:
            for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  {got / total:6.1%}", end="", flush=True)
            # Push Python's buffer to the OS *and* to the disk before deciding
            # this attempt is complete. Without the fsync, a power loss can
            # leave a .part size that the data never reached. We do not fsync
            # every chunk because that would be very slow on an old disk.
            f.flush()
            os.fsync(f.fileno())
        if total:
            print()

    # A response can end early without raising. Promoting a short file would
    # poison the cache: every later run would treat it as complete.
    if total is not None and got != total:
        raise RuntimeError(
            f"Incomplete download: got {got} of {total} bytes. Partial bytes are preserved; retrying."
        )


def resume_download(
    url: str,
    target: Path,
    refresh: bool = False,
    *,
    retries: int = 5,
    timeout=(30, 180),
) -> Path:
    """Download a large HTTP file with byte-range resume and retries.

    Crash/restart behavior
    ----------------------
    * The durable filename appears only after a complete response.
    * Incomplete bytes stay in ``*.part``; ``*.part.meta.json`` records the
      upstream version (ETag / Last-Modified) that those bytes came from.
    * A resume sends ``If-Range`` with that version, so bytes of two different
      upstream versions are never joined into one file.
    * ``refresh=True`` asks the server (HEAD) whether a newer version exists
      and reuses the local file when it does not. A network failure during
      that check raises instead of deleting a good local file.

    This matters for the ~country-sized OSM PBF: a short Wi-Fi failure should
    not throw away hundreds of megabytes that are already on disk.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    target_meta = _meta_path(target)
    part_meta = _meta_path(part)

    if target.exists():
        if not refresh:
            print(f"[cache] {target}")
            return target
        with requests.head(
            url, allow_redirects=True, timeout=timeout, headers={"Accept-Encoding": "identity"}
        ) as head:
            head.raise_for_status()
            length = head.headers.get("Content-Length")
            remote = _response_meta(url, head, int(length) if length else None)
        if _same_version(read_json(target_meta), remote):
            print(f"[cache] {target} (upstream version unchanged)")
            return target
        target.unlink()
        target_meta.unlink(missing_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _download_attempt(url, part, part_meta, timeout)
            meta = read_json(part_meta) or {"url": url}
            meta["bytes"] = part.stat().st_size
            os.replace(part, target)
            atomic_json(target_meta, meta)
            part_meta.unlink(missing_ok=True)
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
    """Exact Python mirror of the SQL normalization used at ingestion.

    The SQL (``sources._sql_name_norm``) is what produces the data; this mirror
    is for tests and small diagnostics, and ``tests/test_normalization_parity``
    holds the two together. Each step corresponds to one SQL function:

    1. ``replace('&', ' and ')``
    2. ``strip_accents``: canonical decomposition (NFD), then every mark
       (Unicode category M*) is removed;
    3. ``lower``;
    4. ``regexp_replace('[^[:alnum:]]+', ' ')`` where ``[:alnum:]`` is
       **ASCII only** in DuckDB's RE2, then ``trim``.

    Known consequences of step 4, kept deliberately visible here: letters that
    do not decompose to ASCII are removed (``Æ``, ``Ø``, ``ß``), and so are
    compatibility forms such as full-width ``ＳＭ`` or the ligature ``ﬁ``. A name
    written only in a non-Latin script normalizes to ``""``, and ingestion then
    drops that observation.

    The mirror is exact for every character in this Python's Unicode database.
    DuckDB's utf8proc can know newer characters (Python 3.11 ships Unicode
    14.0); a mark added later is removed by the SQL but kept here.
    """
    if value is None:
        return ""
    text = str(value).replace("&", " and ")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


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
