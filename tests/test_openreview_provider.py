from types import SimpleNamespace
import sys

import pytest


def _capabilities():
    from openreview_provider import VenueCapabilities

    return VenueCapabilities(
        venue_id="ICLR.cc/2026/Conference",
        submission_invitation="ICLR.cc/2026/Conference/-/Submission",
        submission_venue_id="ICLR.cc/2026/Conference/Submission",
    )


def test_content_value_unwraps_v2_and_plain_values():
    from openreview_provider import content_value

    content = {"title": {"value": "Paper"}, "year": 2026}
    assert content_value(content, "title") == "Paper"
    assert content_value(content, "year") == 2026
    assert content_value(content, "missing", "fallback") == "fallback"


def test_discover_venue_uses_dynamic_submission_name():
    from openreview_provider import OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Blind_Submission"},
            "submission_venue_id": {"value": "Venue/Under_Review"},
        }
    )
    client = SimpleNamespace(get_group=lambda _venue: group)
    provider = OpenReviewProvider({"venue_id": "Venue/2026/Conference"}, client=client)

    capabilities = provider.discover_venue()

    assert (
        capabilities.submission_invitation == "Venue/2026/Conference/-/Blind_Submission"
    )
    assert capabilities.submission_venue_id == "Venue/Under_Review"


def test_discover_venue_reports_private_submissions():
    from openreview_provider import OpenReviewNotPublic, OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Submission"},
            "public_submissions": {"value": False},
        }
    )
    provider = OpenReviewProvider(
        {"venue_id": "NeurIPS.cc/2026/Conference"},
        client=SimpleNamespace(get_group=lambda _venue: group),
    )

    with pytest.raises(OpenReviewNotPublic, match="does not expose public submissions"):
        provider.discover_venue()


def test_authenticated_discover_venue_allows_public_notes_when_group_flag_is_false():
    from openreview_provider import OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Submission"},
            "public_submissions": {"value": False},
        }
    )
    provider = OpenReviewProvider(
        {"venue_id": "ICML.cc/2026/Conference"},
        client=SimpleNamespace(get_group=lambda _venue: group),
    )
    provider._authenticated = True

    capabilities = provider.discover_venue()

    assert capabilities.submission_invitation == (
        "ICML.cc/2026/Conference/-/Submission"
    )


def test_authenticated_public_only_filters_private_notes():
    from openreview_provider import OpenReviewProvider

    public = {
        "id": "public",
        "forum": "public",
        "readers": ["everyone"],
        "content": {"title": {"value": "Public"}},
    }
    private = {
        "id": "private",
        "forum": "private",
        "readers": ["Venue/Authors"],
        "content": {"title": {"value": "Private"}},
    }
    client = SimpleNamespace(get_all_notes=lambda **_kwargs: [public, private])
    provider = OpenReviewProvider(
        {"venue_id": "ICLR.cc/2026/Conference", "public_only": True},
        client=client,
    )
    provider._authenticated = True

    papers = provider.fetch_submissions(_capabilities())

    assert [paper["title"] for paper in papers] == ["Public"]


def test_submission_normalizes_link_to_code_alias():
    from openreview_provider import OpenReviewProvider

    note = SimpleNamespace(
        id="paper-1",
        forum="paper-1",
        readers=["everyone"],
        content={
            "title": {"value": "Paper"},
            "Link To Code": {"value": "https://github.com/example/paper"},
        },
        invitations=["Venue/2026/Conference/-/Submission"],
        cdate=1,
        tmdate=2,
    )
    provider = OpenReviewProvider(
        {"venue_id": "Venue/2026/Conference"}, client=SimpleNamespace()
    )

    paper = provider.normalize_submission(note, _capabilities())

    assert paper["code_url"] == "https://github.com/example/paper"


def test_partial_credentials_are_rejected(monkeypatch):
    import openreview_provider as provider_module
    from openreview_provider import OpenReviewConfigError, OpenReviewProvider

    monkeypatch.setattr(provider_module, "ENV_FILE", provider_module.Path("/missing"))
    monkeypatch.setenv("OPENREVIEW_USERNAME", "user@example.com")
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)
    with pytest.raises(OpenReviewConfigError, match="must be set together"):
        OpenReviewProvider({"venue_id": "Venue/2026/Conference"})


def test_credentials_can_be_loaded_from_project_env(tmp_path, monkeypatch):
    import openreview_provider as provider_module

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENREVIEW_USERNAME=user@example.com\nOPENREVIEW_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provider_module, "ENV_FILE", env_file)
    monkeypatch.delenv("OPENREVIEW_USERNAME", raising=False)
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)

    assert provider_module._load_env_value("OPENREVIEW_USERNAME") == "user@example.com"
    assert provider_module._load_env_value("OPENREVIEW_PASSWORD") == "secret"


def test_api_timeout_wraps_login_and_followup_requests(monkeypatch):
    import openreview_provider as provider_module
    from openreview_provider import OpenReviewProvider

    class FakeSession:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return {"ok": True}

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

    class FakeClient:
        def __init__(self, baseurl):
            self.baseurl = baseurl
            self.session = FakeSession()

        def login_user(self, username, password):
            self.session.post(self.baseurl + "/login", json={"id": username})

    fake_openreview = SimpleNamespace(
        api=SimpleNamespace(OpenReviewClient=FakeClient)
    )
    monkeypatch.setitem(sys.modules, "openreview", fake_openreview)
    monkeypatch.setattr(provider_module, "ENV_FILE", provider_module.Path("/missing"))
    monkeypatch.setenv("OPENREVIEW_USERNAME", "user@example.com")
    monkeypatch.setenv("OPENREVIEW_PASSWORD", "secret")

    provider = OpenReviewProvider(
        {
            "venue_id": "Venue/2026/Conference",
            "api_connect_timeout_seconds": 3,
            "api_read_timeout_seconds": 17,
        }
    )
    provider.client.session.get("https://example.test/groups")

    assert provider._authenticated is True
    assert [call[2]["timeout"] for call in provider.client.session.calls] == [
        (3.0, 17.0),
        (3.0, 17.0),
    ]


def test_api_rate_limit_waits_once_before_retry(monkeypatch):
    import openreview_provider as provider_module
    from openreview_provider import OpenReviewProvider

    class Response:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

        def close(self):
            return None

    class FakeSession:
        def __init__(self):
            self.responses = [Response(429, {"Retry-After": "2"}), Response(200)]
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    class FakeClient:
        def __init__(self, baseurl):
            self.baseurl = baseurl
            self.session = FakeSession()

        def login_user(self, username, password):
            self.session.post(self.baseurl + "/login")

    fake_openreview = SimpleNamespace(
        api=SimpleNamespace(OpenReviewClient=FakeClient)
    )
    monkeypatch.setitem(sys.modules, "openreview", fake_openreview)
    monkeypatch.setattr(provider_module, "ENV_FILE", provider_module.Path("/missing"))
    monkeypatch.setenv("OPENREVIEW_USERNAME", "user@example.com")
    monkeypatch.setenv("OPENREVIEW_PASSWORD", "secret")
    waits = []
    monkeypatch.setattr(provider_module.time, "sleep", waits.append)

    provider = OpenReviewProvider(
        {
            "venue_id": "Venue/2026/Conference",
            "api_rate_limit_retries": 1,
            "api_rate_limit_max_wait_seconds": 10,
        }
    )

    assert provider._authenticated is True
    assert waits == [2.0]
    assert len(provider.client.session.calls) == 2


def test_runtime_reuses_one_authenticated_client(monkeypatch):
    import openreview_provider as provider_module
    from openreview_provider import OpenReviewRuntime

    class FakeSession:
        def request(self, _method, _url, **_kwargs):
            return SimpleNamespace(status_code=200, headers={}, close=lambda: None)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    class FakeClient:
        logins = 0

        def __init__(self, baseurl):
            self.baseurl = baseurl
            self.session = FakeSession()

        def login_user(self, _username, _password):
            type(self).logins += 1
            self.session.post(self.baseurl + "/login")

    fake_openreview = SimpleNamespace(
        api=SimpleNamespace(OpenReviewClient=FakeClient)
    )
    monkeypatch.setitem(sys.modules, "openreview", fake_openreview)
    monkeypatch.setattr(provider_module, "ENV_FILE", provider_module.Path("/missing"))
    monkeypatch.setenv("OPENREVIEW_USERNAME", "user@example.com")
    monkeypatch.setenv("OPENREVIEW_PASSWORD", "secret")

    runtime = OpenReviewRuntime({"venue_id": "Venue/2026/Conference"})
    first = runtime.provider({"venue_id": "ICLR.cc/2026/Conference"})
    second = runtime.provider({"venue_id": "ICML.cc/2026/Conference"})

    assert first.client is second.client is runtime.client
    assert FakeClient.logins == 1
    assert first._authenticated is True
    assert second._authenticated is True
    runtime.close()


def test_nonpublic_pipeline_mode_is_rejected():
    from openreview_provider import OpenReviewConfigError, OpenReviewProvider

    with pytest.raises(OpenReviewConfigError, match="public_only=false"):
        OpenReviewProvider(
            {"venue_id": "Venue/2026/Conference", "public_only": False},
            client=SimpleNamespace(),
        )


def test_openreview_errors_have_stable_outcomes():
    from openreview_provider import (
        OpenReviewConfigError,
        OpenReviewNotPublic,
        classify_openreview_error,
    )

    assert (
        classify_openreview_error(OpenReviewConfigError("bad config"))
        == "INVALID_CONFIG"
    )
    assert classify_openreview_error(OpenReviewNotPublic("private")) == "NOT_PUBLIC"
    assert (
        classify_openreview_error(RuntimeError("Invalid username or password"))
        == "AUTH_REQUIRED"
    )
    assert (
        classify_openreview_error(RuntimeError("Challenge verification required"))
        == "AUTH_REQUIRED"
    )
    assert classify_openreview_error(RuntimeError("429 rate limit")) == "RATE_LIMITED"
    assert classify_openreview_error(RuntimeError("Group Not Found")) == "INVALID_VENUE"


def test_submission_pages_use_after_cursor_and_exact_counts():
    from openreview_provider import OpenReviewProvider

    notes = [
        SimpleNamespace(
            id=f"note-{i}",
            forum=f"note-{i}",
            cdate=i,
            tmdate=i,
            readers=["everyone"],
            content={"title": {"value": f"Paper {i}"}},
            invitations=["Venue/2026/Conference/-/Submission"],
        )
        for i in range(1, 5)
    ]
    calls = []

    def get_notes(**kwargs):
        calls.append(kwargs)
        after = kwargs.get("after")
        if kwargs.get("with_count"):
            return notes[:2], len(notes)
        if after == "note-2":
            return notes[2:]
        return []

    client = SimpleNamespace(get_notes=get_notes)
    provider = OpenReviewProvider({"venue_id": "Venue/2026/Conference"}, client=client)
    pages = list(provider.iter_submission_pages(_capabilities(), page_size=2))

    assert [len(page.papers) for page in pages] == [2, 2]
    assert [page.cursor_after for page in pages] == ["note-2", "note-4"]
    assert [page.raw_count for page in pages] == [2, 2]
    assert calls[0]["with_count"] is True
    assert calls[1]["after"] == "note-2"
    assert calls[2]["after"] == "note-4"
