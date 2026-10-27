"""Real SDK HTTP calls, authentication reload, and read-only tool exposure."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from scripts.mcp_queries import ReadOnlyQueries
from scripts.mcp_server import create_app, load_keys
from publication import PublicationStore
from publication.delivery import DeliveryState, DeliveryStateStore
from publication.serialization import deserialize_bundle

KEY_A = "test-key-for-reader-a-" + "a" * 32
KEY_B = "test-key-for-reader-b-" + "b" * 32
TOOLS = {"dailyinfo_list_briefings", "dailyinfo_get_briefing", "dailyinfo_get_item"}


def keys_file(tmp_path, mapping=None):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(mapping or {KEY_A: "reader_a", KEY_B: "reader_b"}))
    path.chmod(0o600)
    return path


def test_keys_fail_closed_on_invalid_permissions_and_format(tmp_path):
    path = keys_file(tmp_path)
    assert set(load_keys(path).values()) == {"reader_a", "reader_b"}
    path.chmod(0o644)
    with pytest.raises(ValueError, match="Invalid MCP key file"):
        load_keys(path)
    path.chmod(0o600)
    for content in (
        "{}",
        '{"short":"reader"}',
        '{"duplicate":"a","duplicate":"b"}',
        json.dumps({KEY_A: "same", KEY_B: "same"}),
    ):
        path.write_text(content)
        with pytest.raises(ValueError):
            load_keys(path)


@pytest.mark.parametrize("with_content", [False, True])
@pytest.mark.parametrize("visibility", ["delivered", "generated"])
def test_http_auth_routes_reload_and_protocol(
    tmp_path, caplog, with_content, visibility
):
    caplog.set_level(logging.INFO, logger="scripts.mcp_server")

    async def run():
        path = keys_file(tmp_path)
        root = tmp_path / "absent-content"
        bundle = None
        if with_content:
            fixture = Path(__file__).parent / "fixtures/publication_v1/bundle.json"
            bundle = deserialize_bundle(fixture.read_text(encoding="utf-8"))
            PublicationStore(root / "publications").save(bundle)
            now = datetime(2026, 8, 28, tzinfo=timezone.utc)
            DeliveryStateStore(root / "deliveries").save(
                DeliveryState(
                    schema_version=1,
                    briefing_id=bundle.briefing.id,
                    sink="discord",
                    status="success",
                    attempt_count=1,
                    first_attempted_at=now,
                    last_attempted_at=now,
                    delivered_at=now,
                )
            )
        app = create_app(
            ReadOnlyQueries(root, visibility=visibility),
            path,
            allowed_hosts=["testserver"],
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                assert (await http.get("/health")).json() == {
                    "status": "ok",
                    "mode": "read-only",
                }
                assert (await http.post("/mcp", json={})).status_code == 401
                assert (
                    await http.post(
                        "/mcp", headers={"Authorization": "Bearer wrong"}, json={}
                    )
                ).status_code == 401
                assert (
                    await http.get("/mcp", headers={"Authorization": f"Bearer {KEY_A}"})
                ).status_code == 405
                assert (
                    await http.get("/sse", headers={"Authorization": f"Bearer {KEY_A}"})
                ).status_code == 404

            async def read_as(key):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    headers={"Authorization": f"Bearer {key}"},
                ) as http:
                    async with streamable_http_client(
                        "http://testserver/mcp", http_client=http
                    ) as (reader, writer, session_id):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            assert session_id() is None
                            discovered = (await session.list_tools()).tools
                            assert {tool.name for tool in discovered} == TOOLS
                            assert all(
                                tool.annotations.readOnlyHint for tool in discovered
                            )
                            result = await session.call_tool(
                                "dailyinfo_list_briefings", {}
                            )
                            assert not result.isError
                            rows = result.structuredContent["briefings"]
                            assert len(rows) == (1 if with_content else 0)
                            if bundle:
                                assert rows[0]["content_state"] == visibility
                                body = await session.call_tool(
                                    "dailyinfo_get_briefing",
                                    {"briefing_id": rows[0]["id"]},
                                )
                                assert not body.isError
                                assert (
                                    body.structuredContent["body"]
                                    == bundle.briefing.body
                                )
                                item = await session.call_tool(
                                    "dailyinfo_get_item",
                                    {
                                        "briefing_id": rows[0]["id"],
                                        "item_id": bundle.items[0].id,
                                    },
                                )
                                assert not item.isError
                                assert (
                                    item.structuredContent["item"]["title"]
                                    == bundle.items[0].title
                                )
                            invalid = await session.call_tool(
                                "dailyinfo_get_briefing", {"briefing_id": "../../.env"}
                            )
                            assert invalid.isError
                            assert str(tmp_path) not in invalid.model_dump_json()

            await asyncio.gather(read_as(KEY_A), read_as(KEY_B))
            replacement = tmp_path / "replacement.json"
            replacement.write_text(json.dumps({KEY_B: "reader_b"}))
            replacement.chmod(0o600)
            replacement.replace(path)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                assert (
                    await http.post(
                        "/mcp", headers={"Authorization": f"Bearer {KEY_A}"}, json={}
                    )
                ).status_code == 401
                assert (
                    await http.post(
                        "/mcp",
                        headers={"Authorization": f"Bearer {KEY_B}"},
                        content=b"x" * 65537,
                    )
                ).status_code == 413
                path.write_text("invalid")
                assert (
                    await http.post(
                        "/mcp", headers={"Authorization": f"Bearer {KEY_B}"}, json={}
                    )
                ).status_code == 503
        if not with_content:
            assert not root.exists()

    asyncio.run(run())
    records = [r.getMessage() for r in caplog.records if r.name == "scripts.mcp_server"]
    assert any("user=reader_a tool=dailyinfo_list_briefings" in r for r in records)
    assert any("user=reader_b tool=dailyinfo_list_briefings" in r for r in records)
    assert all(KEY_A not in r and KEY_B not in r for r in records)


def test_origin_and_host_checks(tmp_path):
    async def run():
        path = keys_file(tmp_path)
        app = create_app(
            ReadOnlyQueries(tmp_path / "data"), path, allowed_hosts=["testserver"]
        )
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {
            "Authorization": f"Bearer {KEY_A}",
            "Accept": "application/json, text/event-stream",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                assert (
                    await http.post(
                        "/mcp",
                        json=payload,
                        headers={**headers, "Origin": "http://untrusted.example"},
                    )
                ).status_code == 403
                assert (
                    await http.post(
                        "/mcp",
                        json=payload,
                        headers={**headers, "Host": "untrusted.example"},
                    )
                ).status_code == 421

    asyncio.run(run())


def test_repeated_bad_credentials_are_throttled(tmp_path):
    async def run():
        app = create_app(ReadOnlyQueries(tmp_path / "data"), keys_file(tmp_path))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                for _ in range(30):
                    assert (await http.post("/mcp", json={})).status_code == 401
                response = await http.post("/mcp", json={})
                assert response.status_code == 429
                assert int(response.headers["Retry-After"]) > 0

    asyncio.run(run())


def test_slow_catalog_read_does_not_block_health(tmp_path, monkeypatch):
    async def run():
        queries = ReadOnlyQueries(tmp_path / "data")
        started, release = threading.Event(), threading.Event()

        def slow(**kwargs):
            started.set()
            release.wait(2)
            return {"briefings": [], "next_cursor": None}

        monkeypatch.setattr(queries, "list_briefings", slow)
        app = create_app(queries, keys_file(tmp_path), allowed_hosts=["testserver"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {KEY_A}"},
            ) as http:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http
                ) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        start = time.monotonic()
                        call = asyncio.create_task(
                            session.call_tool("dailyinfo_list_briefings", {})
                        )
                        try:
                            while not started.is_set():
                                await asyncio.sleep(0.005)
                            assert (
                                await http.get("http://testserver/health")
                            ).status_code == 200
                            assert time.monotonic() - start < 1
                        finally:
                            release.set()
                            await call

    asyncio.run(run())
