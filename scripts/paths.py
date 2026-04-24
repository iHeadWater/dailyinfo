"""Shared path resolution for dailyinfo workspace.

All scripts import from here to get WORKSPACE_ROOT.

Data root is fixed at ``~/.myagentdata/dailyinfo`` so the tree is naturally
picked up by myopenclaw's ``backup-cron`` service. Override via the
``DAILYINFO_DATA_ROOT`` environment variable or ``.env`` entry when needed.
"""

import os
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
ENV_FILE = PROJECT_ROOT / ".env"

_DEFAULT_ROOT = pathlib.Path.home() / ".myagentdata" / "dailyinfo"


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


def _resolve_data_root() -> pathlib.Path:
    """Resolve the dailyinfo data root, honoring env overrides."""
    override = os.environ.get("DAILYINFO_DATA_ROOT", "") or _read_env_value(
        "DAILYINFO_DATA_ROOT"
    )
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return _DEFAULT_ROOT


WORKSPACE_ROOT = _resolve_data_root()
BRIEFINGS_DIR = WORKSPACE_ROOT / "briefings"
PUSHED_DIR = WORKSPACE_ROOT / "pushed"
FRESHRSS_DATA = WORKSPACE_ROOT / "freshrss" / "data"
