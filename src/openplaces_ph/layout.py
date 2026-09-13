"""Where OpenPlaces reads and writes on disk.

Two problems are solved here.

First, the package locates its own data by walking up from ``__file__``, which
assumes an editable install inside the checkout. ``OPENPLACES_ROOT`` lets you
point the same installed package at a different working directory, which is
useful when the checkout sits on a small disk and the data does not. Every
pipeline function already receives ``root`` as an argument, so overriding what
the CLI passes is enough; no core module has to change.

Second, the Hugging Face cache is not always at ``~/.cache/huggingface``. The
0.3 candidate hardcoded that path, so ``storage`` under-reported and
``clean --hf-cache`` removed nothing for anyone who had set ``HF_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_ENV = "OPENPLACES_ROOT"
FSQ_REPO_DIR = "datasets--foursquare--fsq-os-places"


def checkout_root() -> Path:
    """The repository root, assuming ``src/openplaces_ph/layout.py``."""
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Working root for data, config, and outputs.

    ``OPENPLACES_ROOT`` wins when set. Otherwise the checkout is used.
    """
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return checkout_root()


def areas_config_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "config" / "areas.yml"


def data_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "data"


def hf_hub_cache() -> Path:
    """Resolve the Hugging Face hub cache the same way huggingface_hub does.

    Precedence: ``HF_HUB_CACHE``, then the deprecated
    ``HUGGINGFACE_HUB_CACHE``, then ``HF_HOME/hub``, then
    ``~/.cache/huggingface/hub``.
    """
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(variable)
        if value:
            return Path(value).expanduser().resolve()

    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return (Path(hf_home).expanduser() / "hub").resolve()

    return (Path.home() / ".cache" / "huggingface" / "hub").resolve()


def fsq_cache_dir() -> Path:
    """The Foursquare dataset directory inside the Hugging Face hub cache."""
    return hf_hub_cache() / FSQ_REPO_DIR


def describe_root() -> str:
    """One line for ``doctor`` output."""
    root = project_root()
    origin = "OPENPLACES_ROOT" if os.environ.get(ROOT_ENV) else "checkout"
    return f"{root}  (from {origin})"
