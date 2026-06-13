"""Shared path resolution for dailyinfo workspace.

All scripts import from here to get WORKSPACE_ROOT.

Data root is determined by ``DAILYINFO_ENV``:

=========  ===============  =====================================
Env        DAILYINFO_ENV    Data root
=========  ===============  =====================================
dev        ``dev``          ``~/.myagentdata/dailyinfo-dev``
staging    ``staging``      ``~/.myagentdata/dailyinfo-staging``
prod       ``prod``         ``~/.myagentdata/dailyinfo`` (default)
=========  ===============  =====================================

Override the data root entirely via ``DAILYINFO_DATA_ROOT`` env var or
``.env`` entry (takes precedence over ``DAILYINFO_ENV``). The default lives
under ``~/.myagentdata/`` so that any external backup solution watching
that directory can pick it up without extra configuration.
"""

import os
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
ENV_FILE = PROJECT_ROOT / ".env"

# Valid environment names
VALID_ENVS = ("dev", "staging", "prod")

# -----------------------------------------------------------------------
# Environment resolution
# -----------------------------------------------------------------------


def _read_env_value(key: str) -> str:
    """Read a key from .env without importing python-dotenv."""
    if not ENV_FILE.exists():
        return ""
    prefix = f"{key}="
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix) :].strip().strip('"').strip("'")
    return ""


def get_dailyinfo_env() -> str:
    """Return the current DAILYINFO_ENV (dev / staging / prod).

    Resolution order:
      1. ``DAILYINFO_DATA_ROOT`` is set → always ``prod`` (explicit root
         overrides env-based directory selection).
      2. ``DAILYINFO_ENV`` env var or .env entry.
      3. Default ``prod``.
    """
    # If DAILYINFO_DATA_ROOT is explicitly set, we assume prod (custom root)
    data_root_override = os.environ.get("DAILYINFO_DATA_ROOT", "") or _read_env_value(
        "DAILYINFO_DATA_ROOT"
    )
    if data_root_override:
        return "prod"

    env = os.environ.get("DAILYINFO_ENV", "") or _read_env_value("DAILYINFO_ENV")
    if env in VALID_ENVS:
        return env
    return "prod"


def _resolve_data_root() -> pathlib.Path:
    """Resolve the dailyinfo data root, honoring env overrides.

    Priority:
      1. ``DAILYINFO_DATA_ROOT`` — explicit path (used as-is).
      2. ``DAILYINFO_ENV`` — selects a standard suffix:
         dev → ``dailyinfo-dev``, staging → ``dailyinfo-staging``, prod → ``dailyinfo``.
      3. Default: ``~/.myagentdata/dailyinfo`` (prod).
    """
    override = os.environ.get("DAILYINFO_DATA_ROOT", "") or _read_env_value(
        "DAILYINFO_DATA_ROOT"
    )
    if override:
        return pathlib.Path(override).expanduser().resolve()

    env = get_dailyinfo_env()
    if env == "dev":
        suffix = "dailyinfo-dev"
    elif env == "staging":
        suffix = "dailyinfo-staging"
    else:
        suffix = "dailyinfo"

    return pathlib.Path.home() / ".myagentdata" / suffix


# -----------------------------------------------------------------------
# Public paths
# -----------------------------------------------------------------------
WORKSPACE_ROOT = _resolve_data_root()
BRIEFINGS_DIR = WORKSPACE_ROOT / "briefings"
PUSHED_DIR = WORKSPACE_ROOT / "pushed"
FRESHRSS_DATA = WORKSPACE_ROOT / "freshrss" / "data"
# Convenience: current environment name (for logging / display)
CURRENT_ENV = get_dailyinfo_env()

STATE_DIR = WORKSPACE_ROOT / "state"


# -----------------------------------------------------------------------
# Discord channel resolution (env-aware)
# -----------------------------------------------------------------------


def env_suffix() -> str:
    """Return the env-specific suffix for Discord channel env var names.

    prod    → ""         (DISCORD_CHANNEL_PAPERS)
    dev     → "_DEV"     (DISCORD_CHANNEL_PAPERS_DEV)
    staging → "_STAGING" (DISCORD_CHANNEL_PAPERS_STAGING)
    """
    env = get_dailyinfo_env()
    if env == "dev":
        return "_DEV"
    if env == "staging":
        return "_STAGING"
    return ""


def get_channel_id(category: str) -> str:
    """Resolve a Discord channel ID for the current environment.

    Tries the env-specific key first (e.g. DISCORD_CHANNEL_PAPERS_DEV),
    then falls back to the unsuffixed prod key with a warning.
    """
    import warnings

    suffix = env_suffix()
    env_key = f"DISCORD_CHANNEL_{category.upper()}{suffix}"
    value = os.environ.get(env_key, "") or _read_env_value(env_key)
    if value:
        return value

    # Non-prod: fall back to prod channel key with a warning
    if suffix:
        fallback_key = f"DISCORD_CHANNEL_{category.upper()}"
        fallback = os.environ.get(fallback_key, "") or _read_env_value(fallback_key)
        if fallback:
            warnings.warn(
                f"[env:{CURRENT_ENV}] {env_key} is empty — falling back to prod channel "
                f"({fallback_key}). Set {env_key} to isolate environments.",
                stacklevel=2,
            )
        return fallback

    return ""
