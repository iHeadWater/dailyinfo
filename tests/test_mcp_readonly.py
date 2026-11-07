"""Read-only MCP queries must never expose unpublished content or write state."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from publication import PublicationStore
from publication.delivery import DeliveryState, DeliveryStateStore
from publication.serialization import deserialize_bundle
from scripts.mcp_queries import QueryError, ReadOnlyQueries

FIXTURE = Path(__file__).parent / "fixtures/publication_v1/bundle.json"


def seed(
    root, *, day="2026-08-27", category="papers", sink="discord", status="success"
):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    identity = f"{category}-{day}"
    data["briefing"].update(id=identity, category=category, date=day)
    for item in data["items"]:
        item.update(
            id=item["id"] + "-" + day, category=category, briefing_ids=[identity]
        )
    data["briefing"]["item_ids"] = [item["id"] for item in data["items"]]
    bundle = deserialize_bundle(json.dumps(data))
    PublicationStore(root / "publications").save(bundle)
    if status is not None:
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        DeliveryStateStore(root / "deliveries").save(
            DeliveryState(
                schema_version=1,
                briefing_id=identity,
                sink=sink,
                status=status,
                attempt_count=1,
                first_attempted_at=now,
                last_attempted_at=now,
                delivered_at=now if status == "success" else None,
            )
        )
    return bundle


def fingerprint(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_large_briefing_reads_each_item_a_bounded_number_of_times(
    tmp_path, monkeypatch
):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    template = data["items"][0]
    data["items"] = []
    for number in range(80):
        item = deepcopy(template)
        item["id"] = f"large-item-{number}"
        data["items"].append(item)
    data["briefing"]["item_ids"] = [item["id"] for item in data["items"]]
    bundle = deserialize_bundle(json.dumps(data))
    store = PublicationStore(tmp_path / "publications")
    store.save(bundle)
    queries = ReadOnlyQueries(tmp_path, visibility="generated")
    original = queries.store._read_item
    reads = 0

    def counted(path):
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr(queries.store, "_read_item", counted)
    assert queries.get_briefing(bundle.briefing.id)["body"] == bundle.briefing.body
    assert reads <= 3 * len(bundle.items)
    bundle.briefing.body = "Updated after the first request"
    store.save(bundle)
    assert queries.get_briefing(bundle.briefing.id)["body"] == bundle.briefing.body


def test_queries_are_read_only_and_filter_actual_delivery(tmp_path):
    root = tmp_path / "catalog"
    visible = seed(root)
    seed(root, day="2026-08-28", status=None)
    seed(root, day="2026-08-29", status="failed")
    seed(root, day="2026-08-30", sink="web")
    before = fingerprint(root)
    queries = ReadOnlyQueries(root)
    result = queries.list_briefings()
    assert [b["id"] for b in result["briefings"]] == [visible.briefing.id]
    assert queries.get_briefing(visible.briefing.id)["body"] == visible.briefing.body
    assert (
        queries.get_item(visible.briefing.id, visible.items[0].id)["item"]["title"]
        == visible.items[0].title
    )
    assert before == fingerprint(root)
    for day in ("2026-08-28", "2026-08-29", "2026-08-30"):
        with pytest.raises(QueryError, match="NOT_FOUND"):
            queries.get_briefing(f"papers-{day}")
    with pytest.raises(QueryError, match="INVALID_ARGUMENT"):
        queries.list_briefings(sink="web")


def test_date_category_and_stable_pagination(tmp_path):
    seed(tmp_path, day="2026-08-26")
    seed(tmp_path, day="2026-08-27", category="arxiv")
    seed(tmp_path, day="2026-08-28")
    queries = ReadOnlyQueries(tmp_path)
    first = queries.list_briefings(limit=1)
    assert first["briefings"][0]["date"] == "2026-08-28"
    second = queries.list_briefings(limit=1, cursor=first["next_cursor"])
    assert second["briefings"][0]["category"] == "arxiv"
    assert (
        len(
            queries.list_briefings(date_from="2026-08-27", categories=["papers"])[
                "briefings"
            ]
        )
        == 1
    )
    with pytest.raises(QueryError, match="INVALID_CURSOR"):
        queries.list_briefings(categories=["arxiv"], cursor=first["next_cursor"])
    seed(tmp_path, day="2026-08-29")
    with pytest.raises(QueryError, match="STALE_CURSOR"):
        queries.list_briefings(cursor=first["next_cursor"])


@pytest.mark.parametrize(
    "arguments",
    [
        {"date_from": "2026-02-30"},
        {"date_from": "20260827"},
        {"date_from": "2026-09-01", "date_to": "2026-08-01"},
        {"categories": ["weekly"]},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "../../secret"},
        {"sink": "all"},
    ],
)
def test_invalid_inputs_are_rejected(tmp_path, arguments):
    with pytest.raises(QueryError):
        ReadOnlyQueries(tmp_path).list_briefings(**arguments)


def test_item_membership_and_briefing_paths(tmp_path):
    first = seed(tmp_path)
    other = seed(tmp_path, day="2026-08-28")
    queries = ReadOnlyQueries(tmp_path)
    with pytest.raises(QueryError, match="NOT_FOUND"):
        queries.get_item(first.briefing.id, other.items[0].id)
    with pytest.raises(QueryError, match="INVALID_ARGUMENT"):
        queries.get_briefing("../../.env")


def test_body_pagination_detects_changes(tmp_path):
    bundle = seed(tmp_path)
    queries = ReadOnlyQueries(tmp_path)
    one = queries.get_briefing(bundle.briefing.id, limit=30)
    two = queries.get_briefing(
        bundle.briefing.id, cursor=one["next_cursor"], limit=1000
    )
    assert one["body"] + two["body"] == bundle.briefing.body
    changed = deepcopy(bundle)
    changed.briefing.body += "\nNew published content."
    PublicationStore(tmp_path / "publications").save(changed)
    with pytest.raises(QueryError, match="STALE_CURSOR"):
        queries.get_briefing(bundle.briefing.id, cursor=one["next_cursor"])


def test_empty_root_is_not_created(tmp_path):
    root = tmp_path / "absent"
    assert ReadOnlyQueries(root).list_briefings()["briefings"] == []
    assert not root.exists()


def test_generated_mode_reads_without_fabricating_delivery(tmp_path):
    bundle = seed(tmp_path, status=None)
    before = fingerprint(tmp_path)
    queries = ReadOnlyQueries(tmp_path, visibility="generated")
    result = queries.list_briefings()
    assert result["briefings"][0]["id"] == bundle.briefing.id
    assert result["briefings"][0]["content_state"] == "generated"
    assert result["briefings"][0]["sink"] is None
    assert result["briefings"][0]["delivered_at"] is None
    assert queries.get_briefing(bundle.briefing.id)["content_state"] == "generated"
    assert (
        queries.get_item(bundle.briefing.id, bundle.items[0].id)["content_state"]
        == "generated"
    )
    assert fingerprint(tmp_path) == before
    assert not (tmp_path / "deliveries").exists()
    assert ReadOnlyQueries(tmp_path).list_briefings()["briefings"] == []
    with pytest.raises(QueryError, match="INVALID_ARGUMENT"):
        queries.list_briefings(sink="discord")


def test_invalid_visibility_is_not_silently_accepted(tmp_path):
    with pytest.raises(ValueError):
        ReadOnlyQueries(tmp_path, visibility="all")


def test_corrupt_state_is_sanitized(tmp_path):
    seed(tmp_path)
    path = tmp_path / "deliveries/discord/papers-2026-08-27.json"
    path.write_text("secret-private-path-content", encoding="utf-8")
    with pytest.raises(QueryError, match="CONTENT_UNAVAILABLE") as error:
        ReadOnlyQueries(tmp_path).list_briefings()
    assert str(tmp_path) not in str(error.value)
    assert "secret-private" not in str(error.value)


def test_symlinks_do_not_escape_data_root(tmp_path):
    seed(tmp_path)
    path = tmp_path / "publications/items/papers/escape.json"
    path.symlink_to(FIXTURE)
    with pytest.raises(QueryError, match="CONTENT_UNAVAILABLE"):
        ReadOnlyQueries(tmp_path).list_briefings()


def test_concurrent_changes_fail_closed(tmp_path, monkeypatch):
    seed(tmp_path)
    queries = ReadOnlyQueries(tmp_path)
    original = queries.store.list_briefings
    path = next((tmp_path / "publications/briefings").rglob("briefing.json"))

    def change(**kwargs):
        result = original(**kwargs)
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(queries.store, "list_briefings", change)
    with pytest.raises(QueryError, match="CONTENT_CHANGED"):
        queries.list_briefings()
