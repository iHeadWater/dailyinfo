"""Shared path resolution for dailyinfo workspace.

All scripts import from here to get WORKSPACE_ROOT.
Priority: .env > ~/Google Drive/dailyinfo/workspace > ~/.dailyinfo/workspace
"""

import os
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
ENV_FILE = PROJECT_ROOT / '.env'


def _resolve_workspace_root() -> pathlib.Path:
    """Resolve WORKSPACE_ROOT with fallback chain."""
    workspace_from_env = None
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith('WORKSPACE_ROOT=') and '=' in line:
                    val = line.split('=', 1)[1].strip()
                    if val:
                        path = pathlib.Path(val).expanduser().resolve()
                        if path.exists() or path.parent.exists():
                            workspace_from_env = path
                        break

    if workspace_from_env:
        return workspace_from_env

    google_drive = pathlib.Path.home() / 'Google Drive' / 'dailyinfo' / 'workspace'
    if google_drive.parent.exists():
        google_drive.parent.mkdir(parents=True, exist_ok=True)
        return google_drive

    return pathlib.Path.home() / '.dailyinfo' / 'workspace'


WORKSPACE_ROOT = _resolve_workspace_root()
BRIEFINGS_DIR = WORKSPACE_ROOT / 'briefings'
PUSHED_DIR = WORKSPACE_ROOT / 'pushed'
FRESHRSS_DATA = pathlib.Path.home() / '.freshrss' / 'data'