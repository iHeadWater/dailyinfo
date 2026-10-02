"""Phase 2D WebPublisher and cross-repository transaction tests."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import subprocess
import sys

import pytest

from publication import (
    DeliveryCoordinator,
    DeliveryStateStore,
    DeliveryStoreError,
    PublicationBriefingInput,
    PublicationFinalizer,
    PublicationItemInput,
    PublicationStore,
    WebPublishConfig,
    WebPublisher,
    serialize_web_briefing,
    serialize_web_item,
)
from publication.web import _PublishLock

UTC = timezone.utc
NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


def _item_input(
    category: str,
    *,
    item_id: str | None = None,
    summary: str = "A structured summary.",
    source_published_at=None,
    suffix: str = "001",
    moment: datetime = NOW,
) -> PublicationItemInput:
    return PublicationItemInput(
        source_name=f"{category}-source",
        source_url=f"https://example.com/{category}/{suffix}",
        external_id=f"{category}-external-{suffix}",
        explicit_id=item_id or f"{category}-item-{suffix}",
        source_published_at=source_published_at,
        title=f"{category} item {suffix}",
        summary=summary,
        why_it_matters=None,
        authors=[],
        tags=[],
        language="en",
        retrieved_at=moment,
        published_at=moment,
    )


def _bundle(
    category: str = "papers",
    *,
    date_value: str = "2026-08-27",
    item_id: str | None = None,
    summary: str = "A structured summary.",
    body: str | None = None,
    source_published_at=None,
    suffix: str = "001",
    moment: datetime = NOW,
):
    return PublicationFinalizer().finalize(
        PublicationBriefingInput(
            category=category,
            date=date_value,
            title=f"{category} briefing",
            generated_at=moment,
            published_at=moment,
            body=body or f"# {category}\n\nCanonical briefing body.",
        ),
        [
            _item_input(
                category,
                item_id=item_id,
                summary=summary,
                source_published_at=source_published_at,
                suffix=suffix,
                moment=moment,
            )
        ],
    )


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def _git_repo(tmp_path):
    remote = tmp_path / "web-remote.git"
    repo = tmp_path / "web"
    # -b main on the bare remote too: without it the remote's HEAD is whatever
    # `init.defaultBranch` happens to be (`master` on an unconfigured machine),
    # so the clone in the divergence test ends up without a `main` branch and
    # its `git push origin main` fails for a reason the test never meant to test.
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "README.md").write_text("test web checkout\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Bootstrap",
        "-c",
        "user.email=bootstrap@example.com",
        "commit",
        "-m",
        "bootstrap",
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def _config(repo, remote, *, validation_commands=(), lock_path=None, window_days=7):
    return WebPublishConfig(
        repo_path=repo,
        expected_remote=str(remote),
        expected_branch="main",
        validation_commands=validation_commands,
        lock_path=lock_path,
        window_days=window_days,
    )


def _publisher(
    tmp_path,
    repo,
    remote,
    *,
    validation_commands=(),
    store=None,
    runner=subprocess.run,
    window_days=7,
):
    return WebPublisher(
        _config(
            repo,
            remote,
            validation_commands=validation_commands,
            lock_path=tmp_path / "publish.lock",
            window_days=window_days,
        ),
        publication_store=store,
        runner=runner,
    )


def _commit_count(repo):
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


@pytest.mark.parametrize("category", ["papers", "ai_news", "code", "resource", "arxiv"])
def test_web_representation_is_deterministic_and_maps_all_categories(category):
    bundle = _bundle(category, source_published_at=None)
    item_text = serialize_web_item(bundle.items[0])
    briefing_text = serialize_web_briefing(bundle)

    assert item_text == serialize_web_item(bundle.items[0])
    assert briefing_text == serialize_web_briefing(bundle)
    assert "source_published_at: null" in item_text
    assert "why_it_matters: null" in item_text
    assert f"category: {category}" not in item_text
    assert f'category: "{category}"' in item_text
    assert f'id: "{bundle.briefing.id}"' in briefing_text


def test_web_publisher_creates_noop_and_mutable_update_without_duplicate_commit(
    tmp_path,
):
    repo, remote = _git_repo(tmp_path)
    publication_store = PublicationStore(tmp_path / "publications")
    bundle = publication_store.save(_bundle()).bundle
    publisher = _publisher(tmp_path, repo, remote, store=publication_store)
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    coordinator = DeliveryCoordinator(delivery_store, clock=lambda: NOW)

    first = coordinator.publish(bundle, publisher)
    assert first.status == "success"
    assert _commit_count(repo) == 2
    assert _git(repo, "log", "-1", "--format=%an").stdout.strip() == "DailyInfo Bot"
    assert _git(repo, "log", "-1", "--format=%s").stdout.startswith("publish(papers):")

    forced_noop = coordinator.publish(bundle, publisher, force=True)
    assert forced_noop.status == "success"
    assert _commit_count(repo) == 2

    updated = publication_store.save(
        _bundle(summary="A revised structured summary.")
    ).bundle
    updated_result = coordinator.publish(updated, publisher, force=True)
    assert updated_result.status == "success"
    assert _commit_count(repo) == 3
    assert (repo / "src/content/items/generated/papers/papers-item-001.md").exists()

    remote_head = _git(
        tmp_path, "--git-dir", str(remote), "rev-parse", "main"
    ).stdout.strip()
    assert remote_head == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_web_publisher_keeps_one_item_file_for_shared_item_relationships(tmp_path):
    repo, remote = _git_repo(tmp_path)
    publication_store = PublicationStore(tmp_path / "publications")
    first = publication_store.save(_bundle(date_value="2026-08-26")).bundle
    publisher = _publisher(tmp_path, repo, remote, store=publication_store)
    assert publisher.publish(first).status == "success"

    second = publication_store.save(_bundle(date_value="2026-08-27")).bundle
    assert publisher.publish(second).status == "success"

    item_files = list((repo / "src/content/items/generated/papers").glob("*.md"))
    briefing_files = list(
        (repo / "src/content/briefings/generated/2026/08/").rglob("papers.md")
    )
    assert len(item_files) == 1
    assert len(briefing_files) == 2
    item_text = item_files[0].read_text(encoding="utf-8")
    assert "papers-2026-08-26" in item_text
    assert "papers-2026-08-27" in item_text


def test_web_representation_uses_the_content_timezone():
    """Every emitted timestamp keeps its instant but speaks Asia/Shanghai.

    03:00 UTC is 11:00 in the content timezone; the site groups content by
    dates in the editorial calendar, so the representation -- not the instant
    -- is what it reads.
    """
    bundle = _bundle()
    item_text = serialize_web_item(bundle.items[0])
    briefing_text = serialize_web_briefing(bundle)

    # Anchored: an unanchored 'published_at: ...' substring would also match
    # the tail of 'source_published_at: ...'.
    assert re.search(r'^published_at: "2026-08-27T11:00:00\+08:00"$', item_text, re.M)
    assert re.search(r'^retrieved_at: "2026-08-27T11:00:00\+08:00"$', item_text, re.M)
    assert re.search(
        r'^generated_at: "2026-08-27T11:00:00\+08:00"$', briefing_text, re.M
    )
    assert re.search(
        r'^published_at: "2026-08-27T11:00:00\+08:00"$', briefing_text, re.M
    )


def test_item_date_prefix_matches_the_briefing_date_in_the_pre_dawn_window(
    tmp_path,
):
    """The site files Items by the *date prefix* of ``published_at``
    (dailyinfo-web ``itemsOnDate``).  04:04 on 2026-10-01 in Shanghai is
    20:04 on 09-30 in UTC -- the window the production cron runs in.  A UTC
    representation would file the whole briefing's Items one day early and
    leave the site's latest day with zero Items.
    """
    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    dawn = datetime(2026, 9, 30, 20, 4, tzinfo=UTC)
    bundle = store.save(_bundle(date_value="2026-10-01", moment=dawn)).bundle

    assert (
        _publisher(tmp_path, repo, remote, store=store).publish(bundle).status
        == "success"
    )

    item_text = (
        repo / "src/content/items/generated/papers/papers-item-001.md"
    ).read_text(encoding="utf-8")
    stamp = re.search(r'^published_at: "([^"]+)"', item_text, re.M).group(1)
    assert stamp.startswith("2026-10-01"), stamp
    assert stamp.endswith("+08:00"), stamp


def test_republish_viewed_from_a_later_data_root_preserves_membership(tmp_path):
    """The checkout is the durable membership record.

    Content published from an earlier data root exists only there; the store
    in use has no memory of it.  Re-publishing the same stable identity (a
    repository trending twice, a paper re-collected) must extend the Item's
    ``briefing_ids`` rather than replace them -- the site validates membership
    bidirectionally and fails the whole publication closed otherwise (the
    publication-v1 contract in dailyinfo-web, ``docs/contracts/
    publication-v1.md``, §7 and §11.7).
    """
    repo, remote = _git_repo(tmp_path)
    earlier_root = _publisher(
        tmp_path,
        repo,
        remote,
        store=PublicationStore(tmp_path / "root-a" / "publications"),
    )
    assert earlier_root.publish(_bundle(date_value="2026-09-30")).status == "success"

    later_root = _publisher(
        tmp_path,
        repo,
        remote,
        store=PublicationStore(tmp_path / "root-b" / "publications"),
    )
    assert later_root.publish(_bundle(date_value="2026-10-01")).status == "success"

    item_text = (
        repo / "src/content/items/generated/papers/papers-item-001.md"
    ).read_text(encoding="utf-8")
    assert "papers-2026-09-30" in item_text
    assert "papers-2026-10-01" in item_text


def test_briefing_update_that_drops_an_item_keeps_other_roots_membership(tmp_path):
    """The reconciliation path must drop only THIS briefing's membership.

    A later regeneration of 10-01 without the shared item reconciles the item
    file from the store -- which never knew the 09-30 briefing, published from
    an earlier data root.  Writing that store copy verbatim erased the 09-30
    membership too, and the site's reverse-membership check then blocked the
    whole category on every retry.
    """
    repo, remote = _git_repo(tmp_path)
    root_a = PublicationStore(tmp_path / "root-a" / "publications")
    earlier = _publisher(tmp_path, repo, remote, store=root_a)
    assert (
        earlier.publish(root_a.save(_bundle(date_value="2026-09-30")).bundle).status
        == "success"
    )

    root_b = PublicationStore(tmp_path / "root-b" / "publications")
    later = _publisher(tmp_path, repo, remote, store=root_b)
    assert (
        later.publish(root_b.save(_bundle(date_value="2026-10-01")).bundle).status
        == "success"
    )

    # Regenerate 10-01 with a different item: the shared identity is dropped.
    updated = root_b.save(_bundle(date_value="2026-10-01", suffix="002")).bundle
    assert later.publish(updated).status == "success"

    item_text = (
        repo / "src/content/items/generated/papers/papers-item-001.md"
    ).read_text(encoding="utf-8")
    assert "papers-2026-09-30" in item_text
    assert "papers-2026-10-01" not in item_text


def test_union_drops_a_recorded_membership_whose_briefing_file_is_gone(tmp_path):
    """A membership the site can no longer resolve is not preserved forever.

    If a briefing file disappears from the checkout (a site-side removal, or
    the state the reconciliation bug left behind), keeping the item's stale
    reference would fail validation on every future publish of that item.
    Dropping it here is the union path's job.

    ``window_days=0`` isolates that path: the retention sweep heals the same
    dangling state for its own reason (pinned by
    ``test_window_pruning_a_briefing_drops_it_from_surviving_items``), and
    with the sweep active it would mask this test's mutation.
    """
    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store, window_days=0)
    assert (
        publisher.publish(store.save(_bundle(date_value="2026-09-30")).bundle).status
        == "success"
    )
    assert (
        publisher.publish(store.save(_bundle(date_value="2026-10-01")).bundle).status
        == "success"
    )

    _git(
        repo,
        "rm",
        "--quiet",
        "src/content/briefings/generated/2026/09/30/papers.md",
    )
    _git(
        repo,
        "-c",
        "user.name=Operator",
        "-c",
        "user.email=operator@example.com",
        "commit",
        "-m",
        "operator removes a briefing",
    )
    _git(repo, "push", "origin", "main")

    assert (
        publisher.publish(
            store.save(_bundle(date_value="2026-10-01", summary="Revised.")).bundle
        ).status
        == "success"
    )

    item_text = (
        repo / "src/content/items/generated/papers/papers-item-001.md"
    ).read_text(encoding="utf-8")
    assert "papers-2026-10-01" in item_text
    assert "papers-2026-09-30" not in item_text


def test_union_drops_a_membership_the_briefing_no_longer_lists(tmp_path):
    """A Briefing file that exists but dropped the Item is equally unresolvable.

    The pair fails the site's forward check ("lists briefing which does not
    include the item"), so keeping the recorded membership would block every
    future publish of that Item just like a missing briefing file would.
    """
    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)
    assert (
        publisher.publish(store.save(_bundle(date_value="2026-09-30")).bundle).status
        == "success"
    )
    assert (
        publisher.publish(store.save(_bundle(date_value="2026-10-01")).bundle).status
        == "success"
    )

    briefing_path = repo / "src/content/briefings/generated/2026/09/30/papers.md"
    briefing_path.write_text(
        re.sub(
            r"^item_ids: .*$",
            "item_ids: []",
            briefing_path.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "--", "src/content/briefings/generated/2026/09/30/papers.md")
    _git(
        repo,
        "-c",
        "user.name=Operator",
        "-c",
        "user.email=operator@example.com",
        "commit",
        "-m",
        "operator detaches an item from a briefing",
    )
    _git(repo, "push", "origin", "main")

    assert (
        publisher.publish(
            store.save(_bundle(date_value="2026-10-01", summary="Revised.")).bundle
        ).status
        == "success"
    )

    item_text = (
        repo / "src/content/items/generated/papers/papers-item-001.md"
    ).read_text(encoding="utf-8")
    assert "papers-2026-10-01" in item_text
    assert "papers-2026-09-30" not in item_text


def test_web_validation_failure_rolls_back_generated_files_and_commit(tmp_path):
    repo, remote = _git_repo(tmp_path)
    failing_gate = (
        sys.executable,
        "-c",
        "raise SystemExit(1)",
    )
    publisher = _publisher(
        tmp_path,
        repo,
        remote,
        validation_commands=(failing_gate,),
    )

    result = publisher.publish(_bundle())

    assert result.status == "failed"
    assert _commit_count(repo) == 1
    assert not list((repo / "src/content").rglob("*.md"))
    assert _git(repo, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("failure", ["dirty", "branch", "remote"])
def test_web_publisher_rejects_checkout_precondition(tmp_path, failure):
    repo, remote = _git_repo(tmp_path)
    config = _config(
        repo,
        remote if failure != "remote" else tmp_path / "unexpected.git",
        lock_path=tmp_path / "publish.lock",
    )
    publisher = WebPublisher(config)
    if failure == "dirty":
        (repo / "uncommitted.txt").write_text("do not touch\n", encoding="utf-8")
    elif failure == "branch":
        _git(repo, "switch", "-c", "operator-branch")

    result = publisher.publish(_bundle())

    assert result.status == "failed"
    assert not (repo / "src/content").exists()
    assert _commit_count(repo) == 1


def test_push_failure_leaves_local_commit_and_retry_is_noop(tmp_path):
    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    bundle = store.save(_bundle()).bundle
    push_failed = {"value": True}

    def runner(command, **kwargs):
        if push_failed["value"] and command[:4] == ["git", "push", "origin", "main"]:
            return subprocess.CompletedProcess(command, 1, "", "simulated push failure")
        return subprocess.run(command, **kwargs)

    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    first = DeliveryCoordinator(delivery_store, clock=lambda: NOW).publish(
        bundle,
        _publisher(tmp_path, repo, remote, store=store, runner=runner),
    )
    assert first.status == "failed"
    assert first.external_ref
    assert _commit_count(repo) == 2
    assert (
        _git(tmp_path, "--git-dir", str(remote), "rev-parse", "main").stdout.strip()
        != first.external_ref
    )

    push_failed["value"] = False
    retry = DeliveryCoordinator(delivery_store, clock=lambda: NOW).publish(
        bundle,
        _publisher(tmp_path, repo, remote, store=store),
    )
    assert retry.status == "success"
    assert _commit_count(repo) == 2
    assert (
        _git(tmp_path, "--git-dir", str(remote), "rev-parse", "main").stdout.strip()
        == first.external_ref
    )


def test_delivery_state_failure_after_web_success_is_retryable_without_new_commit(
    tmp_path,
):
    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    bundle = store.save(_bundle()).bundle

    class FailingResultStore(DeliveryStateStore):
        def record_result(self, result, *, expected=None):
            raise DeliveryStoreError("state disk unavailable")

    delivery_root = tmp_path / "deliveries"
    failing_store = FailingResultStore(delivery_root)
    with pytest.raises(DeliveryStoreError, match="state disk unavailable"):
        DeliveryCoordinator(failing_store, clock=lambda: NOW).publish(
            bundle,
            _publisher(tmp_path, repo, remote, store=store),
        )
    assert _commit_count(repo) == 2
    pending = DeliveryStateStore(delivery_root).load(bundle.briefing.id, "web")
    assert pending is not None and pending.status == "pending"

    retry = DeliveryCoordinator(
        DeliveryStateStore(delivery_root), clock=lambda: NOW
    ).publish(
        bundle,
        _publisher(tmp_path, repo, remote, store=store),
    )
    assert retry.status == "success"
    assert _commit_count(repo) == 2


def test_web_publisher_rejects_divergent_history_without_force(tmp_path):
    repo, remote = _git_repo(tmp_path)
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "config", "user.email", "operator@example.com")
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "local.txt")
    _git(repo, "commit", "-m", "operator local change")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    (other / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "operator remote change")
    _git(other, "push", "origin", "main")

    result = _publisher(tmp_path, repo, remote).publish(_bundle())

    assert result.status == "failed"
    assert "diverged" in (result.error or "")
    assert _commit_count(repo) == 2
    assert not (repo / "src/content").exists()


def test_web_publisher_lock_fails_closed_without_touching_checkout(tmp_path):
    repo, remote = _git_repo(tmp_path)
    lock_path = tmp_path / "publish.lock"
    publisher = WebPublisher(_config(repo, remote, lock_path=lock_path))

    with _PublishLock(lock_path):
        result = publisher.publish(_bundle())

    assert result.status == "failed"
    assert "already in progress" in (result.error or "")
    assert _commit_count(repo) == 1
    assert not (repo / "src/content").exists()


def test_web_publisher_rejects_item_category_identity_migration(tmp_path):
    repo, remote = _git_repo(tmp_path)
    shared_id = "shared-stable-item"
    publisher = _publisher(tmp_path, repo, remote)
    assert publisher.publish(_bundle("papers", item_id=shared_id)).status == "success"
    before = _commit_count(repo)

    result = publisher.publish(_bundle("arxiv", item_id=shared_id))

    assert result.status == "failed"
    assert "identity migration" in (result.error or "")
    assert _commit_count(repo) == before


def test_web_publisher_rejects_ids_web_cannot_represent(tmp_path):
    repo, remote = _git_repo(tmp_path)
    publisher = _publisher(tmp_path, repo, remote)

    result = publisher.publish(_bundle(item_id="Bad:Stable"))

    assert result.status == "failed"
    assert "cannot be represented" in (result.error or "")
    assert _commit_count(repo) == 1
    assert _git(repo, "status", "--porcelain").stdout == ""


def _publish_days(publisher, store, days, *, suffix=None):
    """Publish one briefing per date; suffix pins the Item identity."""

    for date_value in days:
        year, month, day = (int(part) for part in date_value.split("-"))
        moment = datetime(year, month, day, 3, 0, tzinfo=UTC)
        bundle = store.save(
            _bundle(
                date_value=date_value,
                suffix=suffix or date_value.replace("-", ""),
                moment=moment,
            )
        ).bundle
        assert publisher.publish(bundle).status == "success"


def _briefing_file(repo, date_value, category="papers"):
    year, month, day = date_value.split("-")
    return (
        repo / "src/content/briefings/generated" / year / month / day / f"{category}.md"
    )


def _item_file(repo, suffix, category="papers"):
    return repo / f"src/content/items/generated/{category}/papers-item-{suffix}.md"


def _operator_commit(repo, message):
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=Operator",
        "-c",
        "user.email=operator@example.com",
        "commit",
        "-m",
        message,
    )
    _git(repo, "push", "origin", "main")


# ---------------------------------------------------------------------------
# Rolling retention window
# ---------------------------------------------------------------------------


def test_window_prunes_content_older_than_seven_days(tmp_path):
    """A window of 7 keeps the newest seven calendar days of content.

    Ten days of content: once 10-01 is the newest, 09-22..09-24 have fallen
    out -- Briefing and Item both -- while the cutoff day 09-25 itself and
    everything newer remain.
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)

    days = [f"2026-09-{day:02d}" for day in range(22, 31)] + ["2026-10-01"]
    _publish_days(publisher, store, days)

    for date_value in ("2026-09-22", "2026-09-23", "2026-09-24"):
        assert not _briefing_file(repo, date_value).exists(), date_value
        assert not _item_file(repo, date_value.replace("-", "")).exists(), date_value
    for date_value in [f"2026-09-{day:02d}" for day in range(25, 31)] + ["2026-10-01"]:
        assert _briefing_file(repo, date_value).exists(), date_value
        assert _item_file(repo, date_value.replace("-", "")).exists(), date_value


def test_window_is_idempotent_across_reruns(tmp_path):
    """A settled window prunes nothing, rewrites nothing, commits nothing.

    Re-delivering the identical newest day must leave the checkout and HEAD
    exactly where the previous run left them.
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)

    days = [f"2026-09-{day:02d}" for day in range(26, 31)] + [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]
    _publish_days(publisher, store, days)
    assert not _briefing_file(repo, "2026-09-26").exists()

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    count = _commit_count(repo)
    newest = store.load_bundle("papers-2026-10-03")
    assert publisher.publish(newest).status == "success"

    assert _commit_count(repo) == count
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_window_anchor_follows_the_newest_content_not_the_clock(tmp_path):
    """The cutoff derives from the newest content date, never from today.

    Every day in this fixture is in the past relative to the wall clock.  A
    clock anchor would put the cutoff near today and empty the public site;
    the content anchor keeps the newest seven days the store actually holds
    and prunes only what fell out of them (sync.mjs made the same decision,
    from the same outage scenario).
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)

    days = ["2026-09-15"] + [f"2026-09-{day:02d}" for day in range(19, 25)]
    _publish_days(publisher, store, days)

    assert not _briefing_file(repo, "2026-09-15").exists()
    assert not _item_file(repo, "20260915").exists()
    for date_value in [f"2026-09-{day:02d}" for day in range(19, 25)]:
        assert _briefing_file(repo, date_value).exists(), date_value


def test_expired_briefing_shared_with_the_other_publisher_keeps_their_section(
    tmp_path,
):
    """Pruning a shared Briefing removes only this publisher's part.

    dailyinfo-web's sync writes into the same ``YYYY/MM/DD/papers.md`` files,
    fencing its section with ``<!-- dailyinfo-sync:… -->`` markers and
    minting ``dailyinfo-…`` ids.  A file that carries such content must lose
    our body and our ids only -- their section stays verbatim and the file is
    deleted only once nothing else is left (their ``pruneBriefing`` makes the
    mirror-image decision).
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)
    _publish_days(publisher, store, ["2026-09-20"])

    sync_item_id = "dailyinfo-papers-nature-2026-09-20"
    sync_block = (
        "<!-- dailyinfo-sync:start -->\n"
        "## Nature 简报\n\nKept verbatim.\n"
        "<!-- dailyinfo-sync:end -->"
    )
    briefing = _briefing_file(repo, "2026-09-20")
    text = briefing.read_text(encoding="utf-8")
    assert 'item_ids: ["papers-item-20260920"]' in text
    text = text.replace(
        'item_ids: ["papers-item-20260920"]',
        f'item_ids: ["papers-item-20260920", "{sync_item_id}"]',
    )
    briefing.write_text(
        text.rstrip("\n") + "\n\n" + sync_block + "\n", encoding="utf-8"
    )

    sync_item = repo / f"src/content/items/generated/papers/{sync_item_id}.md"
    sync_item.write_text(
        "---\n"
        f'id: "{sync_item_id}"\n'
        'category: "papers"\n'
        'briefing_ids: ["papers-2026-09-20"]\n'
        "---\n\nSync item body.\n",
        encoding="utf-8",
    )
    sync_item_bytes = sync_item.read_bytes()
    _operator_commit(repo, "the sync path shares the 09-20 briefing")

    _publish_days(publisher, store, ["2026-10-01"])

    assert briefing.exists()
    pruned = briefing.read_text(encoding="utf-8")
    assert sync_block in pruned
    assert "Canonical briefing body" not in pruned
    assert sync_item_id in pruned
    assert "papers-item-20260920" not in pruned
    assert not _item_file(repo, "20260920").exists()
    assert sync_item.read_bytes() == sync_item_bytes
    assert _briefing_file(repo, "2026-10-01").exists()


def test_window_keeps_an_expired_item_a_living_briefing_still_claims(tmp_path):
    """Expiry is decided against the surviving Briefings, not the Item date alone.

    The site fails the whole publication closed when a Briefing lists an Item
    that is gone, so an Item whose own date fell out of the window survives
    while any in-window Briefing still lists it.  The checkout state is
    written by hand -- an operator edit, or what an older writer leaves
    behind -- because the sweep must read the files as it finds them, not as
    this pipeline would have written them.
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)
    _publish_days(publisher, store, ["2026-09-18", "2026-10-01"])

    # Age the 10-01 Item past the window without touching its membership.
    stale = _item_file(repo, "20261001")
    stale.write_text(
        re.sub(
            r'^published_at: ".*"$',
            'published_at: "2026-09-18T11:00:00+08:00"',
            stale.read_text(encoding="utf-8"),
            flags=re.M,
        ),
        encoding="utf-8",
    )
    # Control: an expired orphan must fall out in the same sweep.
    orphan = _item_file(repo, "orphan")
    orphan.write_text(
        "---\n"
        'id: "papers-item-orphan"\n'
        'category: "papers"\n'
        'published_at: "2026-09-10T11:00:00+08:00"\n'
        "briefing_ids: []\n"
        'title: "Orphan"\n'
        "---\n\nOrphan body.\n",
        encoding="utf-8",
    )
    _operator_commit(repo, "age an item and strand an orphan")

    _publish_days(publisher, store, ["2026-10-02"])

    assert stale.exists()
    assert "papers-2026-10-01" in stale.read_text(encoding="utf-8")
    assert not orphan.exists()
    assert _briefing_file(repo, "2026-10-01").exists()


def test_window_pruning_a_briefing_drops_it_from_surviving_items(tmp_path):
    """The reverse reference is pruned together with the Briefing.

    One stable identity collected on 09-27 and again on 10-04 (a repository
    trending twice) keeps its file with both memberships; once 09-27 falls
    out of the window, that dead reference must leave the file -- the site
    rejects ``Item.briefing_ids`` pointing at a missing Briefing, and the
    sweep is the only place the pair can heal.
    """

    repo, remote = _git_repo(tmp_path)
    store = PublicationStore(tmp_path / "publications")
    publisher = _publisher(tmp_path, repo, remote, store=store)
    _publish_days(publisher, store, ["2026-09-27"], suffix="20260927")
    _publish_days(publisher, store, ["2026-10-04"], suffix="20260927")

    assert not _briefing_file(repo, "2026-09-27").exists()
    item = _item_file(repo, "20260927")
    assert item.exists()
    item_text = item.read_text(encoding="utf-8")
    assert "papers-2026-10-04" in item_text
    assert "papers-2026-09-27" not in item_text


def test_publish_script_ignores_legacy_only_files(tmp_path):
    import publish_to_web

    legacy_root = tmp_path / "legacy"
    (legacy_root / "briefings/papers").mkdir(parents=True)
    (legacy_root / "briefings/papers/papers_briefing_2026-08-27.md").write_text(
        "# legacy\n", encoding="utf-8"
    )
    assert (
        publish_to_web.main(
            "2026-08-27",
            ["papers"],
            publication_store=PublicationStore(tmp_path / "empty-publications"),
            delivery_store=DeliveryStateStore(tmp_path / "deliveries"),
        )
        == 0
    )
