import json
import copy
from pathlib import Path

RATING_OPTIONS = [
    "0: strong reject",
    "2: reject",
    "4: below threshold",
    "6: marginally above threshold",
    "8: accept",
    "10: strong accept",
]


def _fixture():
    path = Path(__file__).parent / "fixtures" / "openreview" / "iclr_public_forum.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _config():
    return {
        "name": "openreview_iclr_2026",
        "display_name": "ICLR 2026",
        "category": "conference",
        "type": "api",
        "provider": "openreview",
        "venue_id": "ICLR.cc/2026/Conference",
        "enabled": True,
        "poll_interval_hours": 24,
        "full_rescan_interval_days": 7,
        "filters": {
            "strong_domain_keywords": ["hydrology", "streamflow", "flood"],
            "domain_context_keywords": ["climate", "remote sensing"],
            "method_keywords": ["forecasting", "transformer"],
            "exclude_phrases": ["watermark"],
        },
        "retrieval": {"strategy": "lexical", "version": 3},
        "reviews": {
            "rating_options": RATING_OPTIONS,
            "min_reviews_for_signal": 1,
        },
    }


class FakeProvider:
    def __init__(self, data):
        self.data = data
        self.submission_calls = []
        self.forum_calls = []

    def discover_venue(self):
        from openreview_provider import VenueCapabilities

        return VenueCapabilities(
            venue_id="ICLR.cc/2026/Conference",
            submission_invitation="ICLR.cc/2026/Conference/-/Submission",
            submission_venue_id="ICLR.cc/2026/Conference/Submission",
        )

    def fetch_submissions(self, _capabilities, min_cdate=None):
        self.submission_calls.append(min_cdate)
        return [dict(self.data["submission"])]

    def fetch_forum(self, forum_id, _capabilities):
        self.forum_calls.append(forum_id)
        return dict(self.data["submission"]), [dict(x) for x in self.data["replies"]]


def test_lexical_recall_uses_boundaries_and_domain_method_pair():
    from conference import lexical_recall

    cfg = _config()["filters"]
    assert lexical_recall(
        {"title": "Hydrology foundation models", "abstract": "", "keywords": []},
        cfg,
    )
    assert lexical_recall(
        {
            "title": "Climate prediction with transformers",
            "abstract": "",
            "keywords": [],
        },
        cfg,
    )
    assert not lexical_recall(
        {"title": "Robust image watermarking", "abstract": "", "keywords": []},
        cfg,
    )
    assert not lexical_recall(
        {"title": "A better transformer", "abstract": "", "keywords": []}, cfg
    )


def test_exclude_phrases_veto_embedding_hits_in_union_retrieval():
    from conference import EmbeddingRetrievalConfig, _retrieval_decision

    embedding_config = EmbeddingRetrievalConfig(threshold=0.45)
    decision = _retrieval_decision(
        lexical_hit=False,
        embedding_score=0.99,
        embedding_config=embedding_config,
        excluded=True,
    )

    assert decision.relevant is False
    assert decision.categories == ()
    assert "excluded_phrase=true" in decision.reason


def test_normalize_rating_requires_known_ordered_scale():
    from conference import normalize_rating

    assert normalize_rating("8: accept", RATING_OPTIONS) == 0.8
    assert normalize_rating(6, RATING_OPTIONS) == 0.6
    assert normalize_rating("8: accept", []) is None
    assert normalize_rating("unknown", RATING_OPTIONS) is None


def test_snapshot_uses_structured_decision_and_review_metrics():
    from conference import RelevanceDecision, build_snapshot

    data = _fixture()
    snapshot = build_snapshot(
        data["submission"],
        data["replies"],
        FakeProvider(data).discover_venue(),
        _config(),
        RelevanceDecision(True, 0.93, ("hydrology",), "directly relevant"),
    )

    assert snapshot["status"] == "accepted"
    assert snapshot["presentation"] == "poster"
    assert snapshot["review_metrics"]["rating_mean"] == 0.8
    assert snapshot["review_metrics"]["rating_raw_values"] == ["8: accept"]
    assert snapshot["review_metrics"]["strong_signal"] is True
    assert snapshot["paper"]["code_url"] == "https://github.com/example/hydrocast"
    assert snapshot["decision_text"].startswith("comment:")
    assert "extreme-flood experiments" in snapshot["author_responses"][0]["text"]


def test_briefing_prompt_requests_raw_ratings_without_internal_disclaimer():
    from conference import _briefing_prompt

    event = {
        "event_types_json": ["PAPER_DISCOVERED"],
        "after_json": {
            "paper": {"title": "Example"},
            "status": "under_review",
            "decision": "",
            "presentation": "",
            "camera_ready": False,
            "relevance": {"score": 0.8},
            "review_metrics": {"rating_raw_values": ["8: accept"]},
            "reviews": [{"rating_raw": "8: accept", "confidence_raw": "4", "text": ""}],
            "meta_reviews": [],
            "author_responses": [{"text": "Authors added flood experiments."}],
        },
        "before_json": None,
    }
    prompt = _briefing_prompt("iclr", "ICLR 2026", [event])

    assert "raw_review_ratings" in prompt
    assert "原始值" in prompt
    assert "Link To Code" in prompt
    assert "Reviewer 1" in prompt
    assert "Rebuttal / Author Response" in prompt
    assert "不要输出事件类型" in prompt


def test_clean_conference_briefing_removes_internal_boilerplate():
    from conference import _clean_conference_briefing

    content = (
        "以下 10 篇论文均由 PAPER_DISCOVERED 事件发现。\n\n"
        "### Example\n\n"
        "研究总结。\n\n"
        "文中所有状态、decision、评审统计均来自输入元数据。"
    )

    assert _clean_conference_briefing(content) == "### Example\n\n研究总结。"


def test_detect_event_types_pushes_author_response_change():
    from conference import detect_event_types

    before = {
        "content_hash": "a",
        "review_signature": "b",
        "decision": "",
        "presentation": "",
        "camera_ready": False,
        "status": "under_review",
        "author_response_signature": "old",
    }
    after = {**before, "author_response_signature": "new", "fingerprint": "new"}
    assert detect_event_types(before, after) == ["AUTHOR_RESPONSE_CHANGED"]


def test_pipeline_is_idempotent_and_detects_review_update(tmp_path):
    from conference import run_conference_source

    data = _fixture()
    provider = FakeProvider(data)
    calls = []

    def fake_ai(prompt, model="test", max_tokens=0):
        calls.append(prompt)
        return "### HydroCast\n\n公开状态：Accept (Poster)\n\n[OpenReview](https://openreview.net/forum?id=forum-hydro-1)"

    state_dir = tmp_path / "state"
    briefings_dir = tmp_path / "briefings"
    first = run_conference_source(
        _config(),
        {"model": "test-model"},
        fake_ai,
        state_dir,
        briefings_dir,
        "2026-08-20",
        provider=provider,
    )
    second = run_conference_source(
        _config(),
        {"model": "test-model"},
        fake_ai,
        state_dir,
        briefings_dir,
        "2026-08-20",
        force=True,
        provider=provider,
    )

    assert first.files_saved == 1
    assert second.files_saved == 0
    assert len(list((briefings_dir / "conference").glob("*.md"))) == 1

    data["replies"][0]["content"]["rating"]["value"] = "10: strong accept"
    data["replies"][0]["mdate"] += 1
    third = run_conference_source(
        _config(),
        {"model": "test-model"},
        fake_ai,
        state_dir,
        briefings_dir,
        "2026-08-20",
        force=True,
        provider=provider,
    )

    assert third.files_saved == 1
    assert third.events_created == 1
    assert len(list((briefings_dir / "conference").glob("*.md"))) == 2


def test_union_retrieval_selects_keyword_or_embedding(tmp_path):
    from conference import run_conference_source

    data = _fixture()
    embedding_only = copy.deepcopy(data)
    embedding_only["submission"]["id"] = "forum-embedding-1"
    embedding_only["submission"]["forum_id"] = "forum-embedding-1"
    embedding_only["submission"]["title"] = "Latent Dynamics for Complex Systems"
    embedding_only["submission"]["abstract"] = "A general representation method."
    embedding_only["submission"]["keywords"] = ["representation learning"]
    for reply in embedding_only["replies"]:
        reply["id"] += "-embedding"
        reply["forum_id"] = "forum-embedding-1"
        reply["replyto"] = "forum-embedding-1"

    class UnionProvider(FakeProvider):
        def fetch_submissions(self, _capabilities, min_cdate=None):
            self.submission_calls.append(min_cdate)
            return [dict(data["submission"]), dict(embedding_only["submission"])]

        def fetch_forum(self, forum_id, _capabilities):
            item = data if forum_id == "forum-hydro-1" else embedding_only
            return dict(item["submission"]), [dict(x) for x in item["replies"]]

    provider = UnionProvider(data)
    config = _config()
    config["retrieval"] = {
        "strategy": "lexical_embedding_union",
        "dimension": 512,
        "threshold": 0.5,
        "batch_size": 8,
    }
    prompts = []

    class FakeEmbeddingClient:
        def score_papers(self, papers):
            assert [paper["forum_id"] for paper in papers] == [
                "forum-hydro-1",
                "forum-embedding-1",
            ]
            return [0.10, 0.81]

    def fake_ai(prompt, model="test", max_tokens=0):
        prompts.append(prompt)
        return "### HydroCast\n\nEmbedding-selected paper."

    result = run_conference_source(
        config,
        {"model": "test-model"},
        fake_ai,
        tmp_path / "state",
        tmp_path / "briefings",
        "2026-08-20",
        provider=provider,
        embedding_client=FakeEmbeddingClient(),
    )

    assert result.relevant_papers == 2
    assert result.retrieval_candidates == 2
    assert result.files_saved == 1
    assert len(prompts) == 1


def test_pending_event_batches_drain_even_when_poll_is_not_due(tmp_path):
    from conference import run_conference_source

    first_data = _fixture()
    second_data = copy.deepcopy(first_data)
    second_data["submission"]["forum_id"] = "forum-hydro-2"
    second_data["submission"]["id"] = "forum-hydro-2"
    second_data["submission"]["title"] = "FloodCast for Global Watersheds"
    for reply in second_data["replies"]:
        reply["id"] += "-second"
        reply["forum_id"] = "forum-hydro-2"
        reply["replyto"] = "forum-hydro-2"

    class MultiProvider(FakeProvider):
        def __init__(self, datasets):
            super().__init__(datasets[0])
            self.datasets = {item["submission"]["forum_id"]: item for item in datasets}

        def fetch_submissions(self, _capabilities, min_cdate=None):
            self.submission_calls.append(min_cdate)
            return [dict(item["submission"]) for item in self.datasets.values()]

        def fetch_forum(self, forum_id, _capabilities):
            item = self.datasets[forum_id]
            return dict(item["submission"]), [dict(x) for x in item["replies"]]

    provider = MultiProvider([first_data, second_data])
    config = _config()
    config["max_events_per_briefing"] = 1

    def fake_ai(prompt, model="test", max_tokens=0):
        return "### Conference event"

    first = run_conference_source(
        config,
        {"model": "test-model"},
        fake_ai,
        tmp_path / "state",
        tmp_path / "briefings",
        "2026-08-20",
        provider=provider,
    )
    second = run_conference_source(
        config,
        {"model": "test-model"},
        fake_ai,
        tmp_path / "state",
        tmp_path / "briefings",
        "2026-08-20",
        provider=provider,
    )

    assert first.files_saved == 1
    assert second.files_saved == 1
    assert len(list((tmp_path / "briefings" / "conference").glob("*.md"))) == 2


def test_sync_outcome_does_not_advance_clocks_on_failure(tmp_path):
    from conference import ConferenceState

    state = ConferenceState(tmp_path / "state.sqlite")
    state.finish_sync(
        "openreview_iclr_2026",
        "ICLR.cc/2026/Conference",
        "SUCCESS",
        "initial sync",
        123456,
        True,
    )
    after_success = state.venue("openreview_iclr_2026")

    assert after_success["submission_watermark_ms"] == 123456
    assert after_success["last_full_sync_ms"] == after_success["last_poll_ms"]

    state.record_outcome(
        "openreview_iclr_2026",
        "ICLR.cc/2026/Conference",
        "RATE_LIMITED",
        "try again later",
    )
    after_failure = state.venue("openreview_iclr_2026")

    assert after_failure["last_outcome"] == "RATE_LIMITED"
    assert after_failure["last_message"] == "try again later"
    assert after_failure["last_poll_ms"] == after_success["last_poll_ms"]
    assert after_failure["last_full_sync_ms"] == after_success["last_full_sync_ms"]
    assert after_failure["submission_watermark_ms"] == 123456


def test_state_retries_transient_database_open_error(tmp_path, monkeypatch):
    import conference

    real_connect = conference.sqlite3.connect
    attempts = {"count": 0}

    def flaky_connect(*args, **kwargs):
        if attempts["count"] < 2:
            attempts["count"] += 1
            raise conference.sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(conference.sqlite3, "connect", flaky_connect)
    state = conference.ConferenceState(tmp_path / "state.sqlite")

    assert state.path.exists()
    assert attempts["count"] == 2


def test_sync_run_resumes_same_cursor_and_enforces_single_lease(tmp_path):
    from conference import RUN_ACTIVE, RUN_INTERRUPTED, ConferenceState

    state = ConferenceState(tmp_path / "state.sqlite")
    first = state.start_run(
        "openreview_iclr_2026",
        "ICLR.cc/2026/Conference",
        "full",
        "config-v1",
        "ICLR.cc/2026/Conference/-/Submission",
        None,
    )
    assert first["status"] == RUN_ACTIVE

    try:
        state.start_run(
            "openreview_iclr_2026",
            "ICLR.cc/2026/Conference",
            "full",
            "config-v1",
            "ICLR.cc/2026/Conference/-/Submission",
            None,
        )
    except RuntimeError as exc:
        assert "already has active run" in str(exc)
    else:
        raise AssertionError("second active lease should be rejected")

    state.persist_discovery_page(
        first["run_id"],
        [
            {
                "source": "openreview_iclr_2026",
                "forum_id": "forum-1",
                "paper": {"forum_id": "forum-1", "title": "Hydrology"},
                "metadata_hash": "hash-1",
                "stage": "PENDING_FORUM",
            }
        ],
        "note-1",
        2,
        1,
        1,
        1,
        100,
    )
    state.interrupt_run(first["run_id"], "Ctrl-C")
    resumed = state.start_run(
        "openreview_iclr_2026",
        "ICLR.cc/2026/Conference",
        "full",
        "config-v1",
        "ICLR.cc/2026/Conference/-/Submission",
        None,
    )

    assert resumed["run_id"] == first["run_id"]
    assert resumed["status"] == RUN_ACTIVE
    assert resumed["cursor_after"] == "note-1"
    assert state.sync_items(first["run_id"])[0]["forum_id"] == "forum-1"
    assert state.run(first["run_id"])["status"] != RUN_INTERRUPTED


def test_pipeline_emits_phase_progress_logs(tmp_path):
    from conference import run_conference_source

    data = _fixture()
    logs = []

    def fake_ai(prompt, model="test", max_tokens=0):
        return "### Conference event"

    result = run_conference_source(
        _config(),
        {"model": "test-model"},
        fake_ai,
        tmp_path / "state",
        tmp_path / "briefings",
        "2026-08-20",
        provider=FakeProvider(data),
        logger=logs.append,
    )

    assert result.outcome == "SUCCESS"
    assert any("[DISCOVERY]" in line for line in logs)
    assert any("[FORUM_POLL]" in line for line in logs)
    assert any("COMPLETE" in line for line in logs)


def test_pipeline_resumes_after_discovery_page_checkpoint(tmp_path):
    from conference import ConferenceState, run_conference_source
    from openreview_provider import SubmissionPage

    first = _fixture()
    second = copy.deepcopy(first)
    second["submission"]["id"] = "forum-hydro-2"
    second["submission"]["forum_id"] = "forum-hydro-2"
    second["submission"]["title"] = "FloodCast for Global Watersheds"
    for reply in second["replies"]:
        reply["id"] += "-2"
        reply["forum_id"] = "forum-hydro-2"
        reply["replyto"] = "forum-hydro-2"

    class InterruptingProvider(FakeProvider):
        def __init__(self):
            super().__init__(first)
            self.fail_after_first_page = True
            self.cursors = []
            self.datasets = {
                "forum-hydro-1": first,
                "forum-hydro-2": second,
            }

        def iter_submission_pages(
            self, _capabilities, min_cdate=None, after_id=None, page_size=1000
        ):
            self.cursors.append(after_id)
            if after_id is None:
                yield SubmissionPage([dict(first["submission"])], "cursor-1", 2, 1, 1)
                if self.fail_after_first_page:
                    raise RuntimeError("simulated page failure")
            else:
                yield SubmissionPage([dict(second["submission"])], "cursor-2", 2, 2, 1)

        def fetch_forum(self, forum_id, _capabilities):
            item = self.datasets[forum_id]
            return dict(item["submission"]), [dict(x) for x in item["replies"]]

    provider = InterruptingProvider()
    config = _config()
    config["full_rescan_interval_days"] = 30

    def fake_ai(prompt, model="test", max_tokens=0):
        return "### Conference event"

    state_dir = tmp_path / "state"
    try:
        run_conference_source(
            config,
            {"model": "test-model"},
            fake_ai,
            state_dir,
            tmp_path / "briefings",
            "2026-08-20",
            provider=provider,
        )
    except RuntimeError as exc:
        assert "simulated page failure" in str(exc)
    else:
        raise AssertionError("the first discovery page should fail")

    state = ConferenceState(state_dir / "openreview.sqlite3")
    active = state.active_run("openreview_iclr_2026")
    state.interrupt_run(active["run_id"], "test interruption")
    provider.fail_after_first_page = False

    result = run_conference_source(
        config,
        {"model": "test-model"},
        fake_ai,
        state_dir,
        tmp_path / "briefings",
        "2026-08-20",
        force=True,
        provider=provider,
    )

    assert result.outcome == "SUCCESS"
    assert provider.cursors == [None, "cursor-1"]
    assert result.submissions_scanned == 2
