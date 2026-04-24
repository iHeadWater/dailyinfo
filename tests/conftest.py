"""Shared pytest fixtures for the dailyinfo test suite.

The production `scripts/` modules use flat imports like ``from paths import ...``
so we mirror that layout by prepending ``scripts/`` to ``sys.path`` before any
test tries to import them.

Every test also runs in an isolated data root (see :func:`tmp_data_root`) so
filesystem side effects never leak across tests or onto the developer's real
``~/.myagentdata/dailyinfo`` tree.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# Modules that cache path values at import time and therefore must be reloaded
# whenever we relocate the data root. Order matters: ``paths`` must come first
# because the others do ``from paths import ...``.
_RELOAD_ORDER = ("paths", "datasource", "run_pipelines", "push_to_discord", "cli")


def _reload_scripts_modules() -> None:
    """Reload scripts modules so they pick up the current env-driven paths."""
    for name in _RELOAD_ORDER:
        mod = sys.modules.get(name)
        if mod is not None:
            importlib.reload(mod)


@pytest.fixture(autouse=True)
def tmp_data_root(tmp_path, monkeypatch) -> Path:
    """Route every test's data writes to an isolated ``tmp_path`` subdir.

    Also sets ``DISCORD_BOT_TOKEN`` so importing ``push_to_discord`` does not
    hit its ``sys.exit`` guard, and clears ``OPENROUTER_API_KEY`` so pipeline
    tests start from a known state.
    """
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DAILYINFO_DATA_ROOT", str(data_root))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import paths  # noqa: F401

    importlib.reload(paths)
    for name in _RELOAD_ORDER[1:]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])

    yield data_root

    # Drop cached modules so the next test imports cleanly under its own env.
    for name in ("cli", "push_to_discord", "run_pipelines"):
        sys.modules.pop(name, None)


@pytest.fixture
def rss_db():
    """Minimal in-memory FreshRSS-like sqlite database.

    Contains three feeds and a mix of fresh/stale entries so tests can exercise
    the lookback cutoff, the ``use_content`` path, and the feed-id resolver.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE feed (id INTEGER PRIMARY KEY, url TEXT)")
    conn.execute(
        "CREATE TABLE entry ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " id_feed INTEGER, title TEXT, link TEXT, content TEXT, date INTEGER)"
    )

    feeds = [
        (1, "https://example.com/feed.xml"),
        (2, "https://news.example.com/rss?format=xml"),
        (3, "https://deep.example.com/rss"),
    ]
    for fid, url in feeds:
        conn.execute("INSERT INTO feed(id, url) VALUES (?, ?)", (fid, url))

    now = int(time.time())

    # Feed 1: 5 fresh entries + 1 stale entry
    for i in range(5):
        conn.execute(
            "INSERT INTO entry(id_feed, title, link, content, date)"
            " VALUES (?,?,?,?,?)",
            (
                1,
                f"Fresh Title {i}",
                f"https://example.com/a/{i}",
                "",
                now - i * 60,
            ),
        )
    conn.execute(
        "INSERT INTO entry(id_feed, title, link, content, date) VALUES (?,?,?,?,?)",
        (
            1,
            "Stale Title",
            "https://example.com/a/old",
            "",
            now - 48 * 3600,
        ),
    )

    # Feed 3: deep-content entries — one valid, one too-short, one huge.
    conn.execute(
        "INSERT INTO entry(id_feed, title, link, content, date) VALUES (?,?,?,?,?)",
        (
            3,
            "Deep Normal",
            "https://deep.example.com/a/1",
            "<p>Hello world</p>" + ("abc " * 200),
            now - 30 * 60,
        ),
    )
    conn.execute(
        "INSERT INTO entry(id_feed, title, link, content, date) VALUES (?,?,?,?,?)",
        (3, "Deep Short", "https://deep.example.com/a/2", "<p>hi</p>", now - 15 * 60),
    )
    conn.execute(
        "INSERT INTO entry(id_feed, title, link, content, date) VALUES (?,?,?,?,?)",
        (
            3,
            "Deep Long",
            "https://deep.example.com/a/3",
            "A" * 20000,
            now - 10 * 60,
        ),
    )

    conn.commit()
    yield conn
    conn.close()


class FakeResponse:
    """Tiny stand-in for :class:`requests.Response` used by :func:`fake_requests`."""

    def __init__(
        self,
        status: int = 200,
        text: str = "",
        json_data: Any = None,
        encoding: str = "utf-8",
    ) -> None:
        self.status_code = status
        self.text = text
        self._json = json_data
        self.encoding = encoding
        self.apparent_encoding = encoding

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RequestsRouter:
    """URL-prefix based router that feeds :func:`fake_requests`."""

    Response = FakeResponse

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], FakeResponse] = {}

    def register(
        self, url_prefix: str, response: FakeResponse, method: str = "GET"
    ) -> None:
        self._routes[(method.upper(), url_prefix)] = response

    def resolve(self, method: str, url: str) -> FakeResponse:
        matches = [
            key for key in self._routes if key[0] == method and url.startswith(key[1])
        ]
        if not matches:
            raise AssertionError(f"Unexpected {method} {url}")
        best = max(matches, key=lambda k: len(k[1]))
        return self._routes[best]


@pytest.fixture
def fake_requests(monkeypatch) -> _RequestsRouter:
    """Replace ``requests.get``/``requests.post`` with a prefix-based router."""
    import requests

    router = _RequestsRouter()

    def _get(url, *args, **kwargs):
        return router.resolve("GET", url)

    def _post(url, *args, **kwargs):
        return router.resolve("POST", url)

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(requests, "post", _post)
    return router


@pytest.fixture
def fake_call_ai(monkeypatch) -> Callable[[str], str]:
    """Patch ``run_pipelines.call_ai`` with a deterministic stub.

    Also stubs out ``time.sleep`` so pipeline integration tests run instantly
    rather than sleeping between batches.
    """
    import time as _time

    import run_pipelines

    def _stub(prompt, model="stub", max_tokens=0):
        return f"[AI-SUMMARY] {len(prompt)} chars, model={model}"

    monkeypatch.setattr(run_pipelines, "call_ai", _stub)
    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    return _stub


def fixture_path(name: str) -> Path:
    """Return an absolute path inside ``tests/fixtures``."""
    return FIXTURES_DIR / name


def read_fixture(name: str) -> str:
    """Read a fixture file as UTF-8 text."""
    return fixture_path(name).read_text(encoding="utf-8")
