"""Tests for path resolution in ``scripts/paths.py``."""

from __future__ import annotations

import importlib
from pathlib import Path


def _reload_paths():
    import paths

    return importlib.reload(paths)


def test_default_root_when_no_override(monkeypatch, tmp_path):
    """Without any env override, the root points at ``~/.myagentdata/dailyinfo``."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    monkeypatch.delenv("DAILYINFO_ENV", raising=False)

    empty_env = tmp_path / ".env"
    empty_env.write_text("# nothing here\n", encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", empty_env)
    paths = _reload_paths()
    monkeypatch.setattr(paths, "ENV_FILE", empty_env)

    resolved = paths._resolve_data_root()
    assert resolved == Path.home() / ".myagentdata" / "dailyinfo"


def test_env_var_overrides_default(monkeypatch, tmp_path):
    """``DAILYINFO_DATA_ROOT`` env variable takes priority."""
    target = tmp_path / "from-env"
    monkeypatch.setenv("DAILYINFO_DATA_ROOT", str(target))
    paths = _reload_paths()

    assert paths.WORKSPACE_ROOT == target.resolve()
    assert paths.BRIEFINGS_DIR == target.resolve() / "briefings"
    assert paths.PUSHED_DIR == target.resolve() / "pushed"
    assert paths.FRESHRSS_DATA == target.resolve() / "freshrss" / "data"


def test_env_file_value_used_when_env_unset(monkeypatch, tmp_path):
    """When no env var is set, ``.env`` entry is honored."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)

    target = tmp_path / "from-dotenv"
    env_file = tmp_path / ".env"
    env_file.write_text(f'DAILYINFO_DATA_ROOT="{target}"\n', encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", env_file)

    assert paths._read_env_value("DAILYINFO_DATA_ROOT") == str(target)
    assert paths._resolve_data_root() == target.resolve()


def test_read_env_value_handles_quoting_and_missing_key(monkeypatch, tmp_path):
    """``_read_env_value`` strips quotes and returns empty string for unknown keys."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FOO='quoted-value'\n" 'BAR="double-quoted"\n' "BAZ=plain\n",
        encoding="utf-8",
    )

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", env_file)

    assert paths._read_env_value("FOO") == "quoted-value"
    assert paths._read_env_value("BAR") == "double-quoted"
    assert paths._read_env_value("BAZ") == "plain"
    assert paths._read_env_value("MISSING") == ""


def test_autouse_fixture_points_root_at_tmp(tmp_data_root):
    """The autouse fixture in conftest redirects WORKSPACE_ROOT into tmp_path."""
    import paths

    assert paths.WORKSPACE_ROOT == tmp_data_root.resolve()
    assert paths.BRIEFINGS_DIR.parent == tmp_data_root.resolve()


# -----------------------------------------------------------------------
# Environment separation tests
# -----------------------------------------------------------------------


def test_env_dev_redirects_to_dailyinfo_dev(monkeypatch, tmp_path):
    """DAILYINFO_ENV=dev routes data to ~/.myagentdata/dailyinfo-dev."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    monkeypatch.setenv("DAILYINFO_ENV", "dev")

    empty_env = tmp_path / ".env"
    empty_env.write_text("# nothing\n", encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", empty_env)
    paths = _reload_paths()
    monkeypatch.setattr(paths, "ENV_FILE", empty_env)

    assert paths._resolve_data_root() == Path.home() / ".myagentdata" / "dailyinfo-dev"
    assert paths.CURRENT_ENV == "dev"


def test_env_staging_redirects_to_dailyinfo_staging(monkeypatch, tmp_path):
    """DAILYINFO_ENV=staging routes data to ~/.myagentdata/dailyinfo-staging."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    monkeypatch.setenv("DAILYINFO_ENV", "staging")

    empty_env = tmp_path / ".env"
    empty_env.write_text("# nothing\n", encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", empty_env)
    paths = _reload_paths()
    monkeypatch.setattr(paths, "ENV_FILE", empty_env)

    assert paths._resolve_data_root() == Path.home() / ".myagentdata" / "dailyinfo-staging"
    assert paths.CURRENT_ENV == "staging"


def test_env_prod_is_default(monkeypatch, tmp_path):
    """Without DAILYINFO_ENV set, the default is prod."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    monkeypatch.delenv("DAILYINFO_ENV", raising=False)

    empty_env = tmp_path / ".env"
    empty_env.write_text("# nothing\n", encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", empty_env)
    paths = _reload_paths()
    monkeypatch.setattr(paths, "ENV_FILE", empty_env)

    assert paths._resolve_data_root() == Path.home() / ".myagentdata" / "dailyinfo"
    assert paths.CURRENT_ENV == "prod"


def test_data_root_overrides_env_selection(monkeypatch, tmp_path):
    """DAILYINFO_DATA_ROOT takes priority over DAILYINFO_ENV."""
    target = tmp_path / "custom-root"
    monkeypatch.setenv("DAILYINFO_DATA_ROOT", str(target))
    monkeypatch.setenv("DAILYINFO_ENV", "dev")

    paths = _reload_paths()
    # DATA_ROOT wins, so env is forced to prod
    assert paths.WORKSPACE_ROOT == target.resolve()
    assert paths.CURRENT_ENV == "prod"


def test_invalid_env_falls_back_to_prod(monkeypatch, tmp_path):
    """An invalid DAILYINFO_ENV value falls back to prod."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    monkeypatch.setenv("DAILYINFO_ENV", "invalid")

    empty_env = tmp_path / ".env"
    empty_env.write_text("# nothing\n", encoding="utf-8")

    import paths

    monkeypatch.setattr(paths, "ENV_FILE", empty_env)
    paths = _reload_paths()
    monkeypatch.setattr(paths, "ENV_FILE", empty_env)

    assert paths.CURRENT_ENV == "prod"
    assert paths._resolve_data_root() == Path.home() / ".myagentdata" / "dailyinfo"
