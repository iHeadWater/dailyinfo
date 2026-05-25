"""Environment variable management with multi-env support.

The ``DAILYINFO_ENV`` variable (dev / staging / prod) determines which
set of credentials and channels to load.  In non-prod environments,
channel keys are suffixed with ``_DEV`` or ``_STAGING`` so that
development pushes never land in production channels.
"""

import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent.parent / ".env"

# Valid environment names
VALID_ENVS = ("dev", "staging", "prod")


def _load_env() -> dict:
    env = {}
    if _ENV_PATH.exists():
        try:
            from dotenv import dotenv_values

            env = dict(dotenv_values(_ENV_PATH))
        except ImportError:
            with open(_ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


_env = _load_env()


def get_current_env() -> str:
    """Return the active DAILYINFO_ENV (dev / staging / prod)."""
    val = _env.get("DAILYINFO_ENV", "") or os.environ.get("DAILYINFO_ENV", "")
    if val in VALID_ENVS:
        return val
    return "prod"


def _env_suffix() -> str:
    """Return the env-specific suffix for channel variable names.

    prod  → ""       (DISCORD_CHANNEL_PAPERS)
    dev   → "_DEV"   (DISCORD_CHANNEL_PAPERS_DEV)
    staging → "_STAGING" (DISCORD_CHANNEL_PAPERS_STAGING)
    """
    env = get_current_env()
    if env == "dev":
        return "_DEV"
    elif env == "staging":
        return "_STAGING"
    return ""


def _get_channel_env_key(category: str) -> str:
    """Return the env var key for a Discord channel in the current env.

    Example:
        prod  + "papers" → DISCORD_CHANNEL_PAPERS
        dev   + "papers" → DISCORD_CHANNEL_PAPERS_DEV
    """
    return f"DISCORD_CHANNEL_{category.upper()}{_env_suffix()}"


def get_channel_id(category: str) -> str:
    """Resolve a Discord channel ID for the current environment.

    Falls back to the unsuffixed key if the env-specific one is empty,
    so a dev setup that reuses prod channels still works (with a warning).
    """
    env_key = _get_channel_env_key(category)
    value = _env.get(env_key, "") or os.environ.get(env_key, "")
    if value:
        return value

    # Fallback: try unsuffixed key (prod channel) — only in non-prod
    current = get_current_env()
    if current != "prod":
        fallback_key = f"DISCORD_CHANNEL_{category.upper()}"
        fallback = _env.get(fallback_key, "") or os.environ.get(fallback_key, "")
        if fallback:
            import warnings
            warnings.warn(
                f"[env:{current}] {env_key} is empty — falling back to prod channel "
                f"({fallback_key}). Set {env_key} to isolate environments.",
                stacklevel=2,
            )
        return fallback

    return ""


CURRENT_ENV = get_current_env()

# -----------------------------------------------------------------------
# Public config values
# -----------------------------------------------------------------------
DISCORD_BOT_TOKEN: str = _env.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY: str = _env.get("ANTHROPIC_API_KEY", "")
LIB_USERNAME: str = _env.get("LIB_USERNAME", "")
LIB_PASSWORD: str = _env.get("LIB_PASSWORD", "")
GITHUB_TOKEN: str = _env.get("GITHUB_TOKEN", "")
GITHUB_REPO: str = _env.get("GITHUB_REPO", "")
TAVILY_API_KEY: str = _env.get("TAVILY_API_KEY", "")
JINA_API_KEY: str = _env.get("JINA_API_KEY", "")
OPENROUTER_API_KEY: str = _env.get("OPENROUTER_API_KEY", "")
DOWNLOAD_DIR: Path = Path(_env.get("DOWNLOAD_DIR", "./downloads"))
