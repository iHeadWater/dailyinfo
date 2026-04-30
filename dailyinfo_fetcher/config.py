"""Environment variable management."""
import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent.parent / ".env"


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

DISCORD_BOT_TOKEN: str = _env.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY: str = _env.get("ANTHROPIC_API_KEY", "")
LIB_USERNAME: str = _env.get("LIB_USERNAME", "")
LIB_PASSWORD: str = _env.get("LIB_PASSWORD", "")
GITHUB_TOKEN: str = _env.get("GITHUB_TOKEN", "")
TAVILY_API_KEY: str = _env.get("TAVILY_API_KEY", "")
JINA_API_KEY: str = _env.get("JINA_API_KEY", "")
OPENROUTER_API_KEY: str = _env.get("OPENROUTER_API_KEY", "")
DOWNLOAD_DIR: Path = Path(_env.get("DOWNLOAD_DIR", "./downloads"))
