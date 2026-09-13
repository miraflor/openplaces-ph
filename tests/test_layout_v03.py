from pathlib import Path

from openplaces_ph import layout


def test_project_root_defaults_to_checkout(monkeypatch):
    monkeypatch.delenv(layout.ROOT_ENV, raising=False)
    assert layout.project_root() == layout.checkout_root()


def test_project_root_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(layout.ROOT_ENV, str(tmp_path))
    assert layout.project_root() == tmp_path.resolve()
    assert layout.data_dir() == tmp_path.resolve() / "data"
    assert layout.areas_config_path() == tmp_path.resolve() / "config" / "areas.yml"


def _clear_hf(monkeypatch):
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_hf_cache_default(monkeypatch):
    _clear_hf(monkeypatch)
    expected = (Path.home() / ".cache" / "huggingface" / "hub").resolve()
    assert layout.hf_hub_cache() == expected


def test_hf_cache_respects_hf_home(monkeypatch, tmp_path):
    _clear_hf(monkeypatch)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert layout.hf_hub_cache() == (tmp_path / "hub").resolve()


def test_hf_cache_respects_hf_hub_cache(monkeypatch, tmp_path):
    _clear_hf(monkeypatch)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "ignored"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "explicit"))
    assert layout.hf_hub_cache() == (tmp_path / "explicit").resolve()


def test_fsq_cache_dir_sits_inside_the_hub_cache(monkeypatch, tmp_path):
    _clear_hf(monkeypatch)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert layout.fsq_cache_dir().parent == tmp_path.resolve()
    assert layout.fsq_cache_dir().name == layout.FSQ_REPO_DIR
