"""OpenReview API v2 provider used by the conference pipeline.

The provider deliberately exposes normalized dictionaries instead of leaking
``openreview-py`` model objects into the orchestration layer.  Authentication
is optional; when it is used, ``public_only`` keeps private notes out of
briefings by requiring public readers on every returned note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import os
from pathlib import Path
import random
import time
from typing import Any, Iterable

import requests

from paper_metadata import extract_affiliations_from_pdf

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_API_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_API_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_RATE_LIMIT_RETRIES = 1
DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS = 90.0


class OpenReviewProviderError(RuntimeError):
    """Base error for a single OpenReview source."""


class OpenReviewConfigError(OpenReviewProviderError):
    """Raised when source or credential configuration is incomplete."""


class OpenReviewNotPublic(OpenReviewProviderError):
    """Raised when a venue exists but no public submissions are available."""


class OpenReviewRateLimited(OpenReviewProviderError):
    """Raised when OpenReview remains rate limited after the retry budget."""


@dataclass(frozen=True)
class SubmissionPage:
    """One resumable page of normalized submission notes."""

    papers: list[dict]
    cursor_after: str | None
    total: int | None
    page_number: int
    raw_count: int = 0


def classify_openreview_error(exc: Exception) -> str:
    """Map client/network exceptions to a stable source outcome."""

    if exc.__class__.__name__ in {"WebConferenceNotReady", "SourceNotReady"}:
        return "SOURCE_NOT_READY"
    if isinstance(exc, OpenReviewConfigError):
        return "INVALID_CONFIG"
    if isinstance(exc, OpenReviewNotPublic):
        return "NOT_PUBLIC"
    if isinstance(exc, OpenReviewRateLimited):
        return "RATE_LIMITED"
    text = f"{exc.__class__.__name__}: {exc}".casefold()
    if (
        "challenge" in text
        or "authentication" in text
        or "invalid username or password" in text
        or "401" in text
    ):
        return "AUTH_REQUIRED"
    if "forbidden" in text or "permission" in text or "403" in text:
        return "NOT_PUBLIC"
    if "notfound" in text or "not found" in text or "404" in text:
        return "INVALID_VENUE"
    if "ratelimit" in text or "rate limit" in text or "429" in text:
        return "RATE_LIMITED"
    return "API_ERROR"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def content_value(content: dict, key: str, default: Any = None) -> Any:
    """Unwrap an OpenReview v2 content value.

    API v2 normally uses ``{"value": ...}``, while fixtures and some older
    exported objects may already contain the scalar value.
    """

    value = (content or {}).get(key, default)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _load_env_value(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if value or not ENV_FILE.exists():
        return value
    prefix = f"{key}="
    with ENV_FILE.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or not line.startswith(prefix):
                continue
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def note_is_public(note: Any) -> bool:
    """Return whether a note is explicitly readable by everyone."""

    readers = _as_list(_attr(note, "readers", []))
    return "everyone" in readers


def _field_is_public(field: Any) -> bool:
    if not isinstance(field, dict):
        return True
    readers = field.get("readers")
    return readers is None or "everyone" in _as_list(readers)


def public_content(note: Any, enforce: bool) -> dict:
    """Return content with field-level private values removed."""

    content = dict(_attr(note, "content", {}) or {})
    if not enforce:
        return content
    return {key: value for key, value in content.items() if _field_is_public(value)}


def _link_to_code(content: dict) -> str:
    """Return the first public HTTP(S) code link from common venue field names."""

    aliases = {
        "link_to_code",
        "code",
        "code_url",
        "github",
        "repository",
        "software",
    }
    for key in content:
        normalized = str(key).strip().casefold().replace("-", "_").replace(" ", "_")
        if normalized not in aliases:
            continue
        for value in _as_list(content_value(content, key, "")):
            candidate = str(value or "").strip()
            if candidate.startswith(("https://", "http://")):
                return candidate
    return ""


def _absolute_openreview_url(value: Any) -> str:
    """Normalize OpenReview relative links to usable absolute URLs."""
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith(("https://", "http://")):
        return url
    if url.startswith("/"):
        return f"https://openreview.net{url}"
    return url


@dataclass(frozen=True)
class VenueCapabilities:
    venue_id: str
    submission_invitation: str
    submission_venue_id: str = ""
    public_submissions: bool | None = None
    withdrawn_venue_id: str = ""
    desk_rejected_venue_id: str = ""
    rejected_venue_id: str = ""
    review_name: str = "Official_Review"
    meta_review_name: str = "Meta_Review"
    decision_name: str = "Decision"
    rebuttal_name: str = "Rebuttal"
    decision_field_name: str = "decision"


class OpenReviewProvider:
    """Read submissions and replies from one OpenReview API v2 venue."""

    def __init__(
        self,
        config: dict,
        client: Any = None,
        authenticated: bool | None = None,
    ):
        self.config = config
        self.venue_id = str(config.get("venue_id", "")).strip()
        if not self.venue_id:
            raise OpenReviewConfigError("venue_id is required")
        self.baseurl = config.get(
            "baseurl",
            _load_env_value("OPENREVIEW_API_BASEURL_V2")
            or "https://api2.openreview.net",
        )
        self.public_only = bool(config.get("public_only", True))
        if not self.public_only:
            raise OpenReviewConfigError(
                "public_only=false is not supported by the Discord conference pipeline"
            )
        self._authenticated = bool(authenticated) if authenticated is not None else False
        self.api_timeout = self._api_timeout()
        self.client = client or self._build_client()
        self._pdf_bytes_cache: dict[str, bytes] = {}
        if client is not None and authenticated is None:
            self._authenticated = bool(
                getattr(client, "_dailyinfo_authenticated", False)
            )

    def _api_timeout(self) -> tuple[float, float]:
        """Return the connect/read timeout used for every API request.

        ``requests`` accepts a two-item timeout tuple, which prevents a slow
        OpenReview socket from waiting forever while still allowing a little
        more time for the response body than for connection establishment.
        A single ``api_timeout_seconds`` value remains available as a compact
        per-source override.
        """

        default = self.config.get("api_timeout_seconds")
        connect = self.config.get(
            "api_connect_timeout_seconds",
            default if default is not None else DEFAULT_API_CONNECT_TIMEOUT_SECONDS,
        )
        read = self.config.get(
            "api_read_timeout_seconds",
            default if default is not None else DEFAULT_API_READ_TIMEOUT_SECONDS,
        )
        try:
            connect = float(connect)
            read = float(read)
        except (TypeError, ValueError) as exc:
            raise OpenReviewConfigError(
                "OpenReview API timeouts must be positive numbers"
            ) from exc
        if connect <= 0 or read <= 0:
            raise OpenReviewConfigError(
                "OpenReview API timeouts must be positive numbers"
            )
        return connect, read

    def _install_api_timeout(self, client: Any) -> None:
        """Install a default timeout on an openreview-py session.

        openreview-py does not expose a client-wide timeout and its generated
        methods call ``session.get/post`` directly.  Wrapping the session's
        ``request`` method covers both login and all subsequent API calls,
        while preserving any explicit timeout supplied by the client itself.
        """

        session = getattr(client, "session", None)
        request = getattr(session, "request", None)
        if not callable(request) or getattr(session, "_dailyinfo_timeout", False):
            return
        timeout = self.api_timeout
        max_retries = max(
            0,
            int(self.config.get("api_rate_limit_retries", DEFAULT_RATE_LIMIT_RETRIES)),
        )
        max_wait = max(
            0.0,
            float(
                self.config.get(
                    "api_rate_limit_max_wait_seconds",
                    DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
                )
            ),
        )

        def retry_after(response: Any) -> float:
            headers = getattr(response, "headers", {}) or {}
            raw = headers.get("Retry-After") or headers.get("retry-after")
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
            try:
                payload = response.json()
                reset = ((payload.get("details") or {}).get("resetTime"))
                if isinstance(reset, (int, float)):
                    return max(0.0, float(reset) - time.time())
                if isinstance(reset, str):
                    value = reset.replace("Z", "+00:00")
                    return max(
                        0.0,
                        datetime.fromisoformat(value).timestamp() - time.time(),
                    )
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass
            return 0.0

        @wraps(request)
        def request_with_timeout(method: str, url: str, **kwargs: Any):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = timeout
            for attempt in range(max_retries + 1):
                response = request(method, url, **kwargs)
                if getattr(response, "status_code", None) != 429:
                    return response
                if attempt >= max_retries:
                    return response
                wait = min(max_wait, retry_after(response))
                if wait <= 0:
                    wait = min(max_wait, 1.0 + random.random())
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                if wait > 0:
                    time.sleep(wait)
            raise OpenReviewRateLimited("OpenReview rate limit retry budget exhausted")

        session.request = request_with_timeout
        session._dailyinfo_timeout = True

    def _build_client(self):
        username = _load_env_value("OPENREVIEW_USERNAME")
        password = _load_env_value("OPENREVIEW_PASSWORD")
        if bool(username) != bool(password):
            raise OpenReviewConfigError(
                "OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD must be set together"
            )
        try:
            import openreview
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised in install smoke tests
            raise OpenReviewConfigError(
                "openreview-py is not installed; run `uv sync` or `dailyinfo install`"
            ) from exc

        # Construct without credentials first so the timeout wrapper also
        # covers the login request made by openreview-py.
        client = openreview.api.OpenReviewClient(baseurl=self.baseurl)
        self._install_api_timeout(client)
        if username and password:
            client.login_user(username, password)
            self._authenticated = True
        client._dailyinfo_authenticated = self._authenticated
        return client

    def close(self) -> None:
        """Close any HTTP session owned by openreview-py, when exposed."""
        for owner in (self.client, getattr(self.client, "api_client", None)):
            for name in ("session", "_session"):
                session = getattr(owner, name, None)
                close = getattr(session, "close", None)
                if callable(close):
                    close()
                    return

    def _visible(self, note: Any) -> bool:
        # A guest client cannot receive private notes.  An authenticated client
        # must prove public visibility explicitly before content can leave this
        # provider.
        return not (self._authenticated and self.public_only) or note_is_public(note)

    def discover_venue(self) -> VenueCapabilities:
        group = self.client.get_group(self.venue_id)
        content = dict(_attr(group, "content", {}) or {})
        public_submissions = content_value(content, "public_submissions", None)
        if public_submissions is False and not self._authenticated:
            raise OpenReviewNotPublic(
                f"venue {self.venue_id} does not expose public submissions to guests"
            )
        submission_name = content_value(content, "submission_name", "")
        submission_id = content_value(content, "submission_id", "")
        if submission_id:
            invitation = str(submission_id)
        elif submission_name:
            invitation = f"{self.venue_id}/-/{submission_name}"
        else:
            raise OpenReviewConfigError(
                f"venue {self.venue_id} has no public submission invitation"
            )
        return VenueCapabilities(
            venue_id=self.venue_id,
            submission_invitation=invitation,
            submission_venue_id=str(
                content_value(content, "submission_venue_id", "") or ""
            ),
            public_submissions=(
                bool(public_submissions) if public_submissions is not None else None
            ),
            withdrawn_venue_id=str(
                content_value(content, "withdrawn_venue_id", "") or ""
            ),
            desk_rejected_venue_id=str(
                content_value(content, "desk_rejected_venue_id", "") or ""
            ),
            rejected_venue_id=str(
                content_value(content, "rejected_venue_id", "") or ""
            ),
            review_name=str(content_value(content, "review_name", "Official_Review")),
            meta_review_name=str(
                content_value(content, "meta_review_name", "Meta_Review")
            ),
            decision_name=str(content_value(content, "decision_name", "Decision")),
            rebuttal_name=str(content_value(content, "rebuttal_name", "Rebuttal")),
            decision_field_name=str(
                content_value(content, "decision_field_name", "decision")
            ),
        )

    def fetch_submissions(
        self, capabilities: VenueCapabilities, min_cdate: int | None = None
    ) -> list[dict]:
        return [
            paper
            for page in self.iter_submission_pages(capabilities, min_cdate=min_cdate)
            for paper in page.papers
        ]

    def iter_submission_pages(
        self,
        capabilities: VenueCapabilities,
        min_cdate: int | None = None,
        after_id: str | None = None,
        page_size: int = 1000,
        total_hint: int | None = None,
    ):
        """Yield explicit pages so callers can checkpoint the ``after`` cursor.

        ``openreview-py.get_all_notes`` hides pagination and its tqdm counter
        under-reports the final item of each page.  The conference pipeline
        uses this iterator instead, committing each page before continuing.
        """

        if not hasattr(self.client, "get_notes"):
            kwargs: dict[str, Any] = {"invitation": capabilities.submission_invitation}
            if min_cdate is not None:
                kwargs["mintcdate"] = int(min_cdate)
            notes = self.client.get_all_notes(**kwargs)
            yield SubmissionPage(
                papers=[
                    self.normalize_submission(note, capabilities)
                    for note in notes
                    if self._visible(note)
                ],
                cursor_after=None,
                total=len(notes),
                page_number=1,
                raw_count=len(notes),
            )
            return

        page_size = max(1, min(int(page_size), 1000))
        cursor = after_id
        page_number = 0
        total: int | None = total_hint
        while True:
            kwargs: dict[str, Any] = {
                "invitation": capabilities.submission_invitation,
                "limit": page_size,
                "sort": "id",
            }
            if cursor:
                kwargs["after"] = cursor
            if min_cdate is not None:
                kwargs["mintcdate"] = int(min_cdate)
            if total is None:
                kwargs["with_count"] = True
                notes, total = self.client.get_notes(**kwargs)
            else:
                notes = self.client.get_notes(**kwargs)
            if not notes:
                return
            page_number += 1
            last_id = str(_attr(notes[-1], "id", "") or "")
            if not last_id or last_id == cursor:
                raise OpenReviewProviderError(
                    "OpenReview pagination cursor did not advance"
                )
            yield SubmissionPage(
                papers=[
                    self.normalize_submission(note, capabilities)
                    for note in notes
                    if self._visible(note)
                ],
                cursor_after=last_id,
                total=total,
                page_number=page_number,
                raw_count=len(notes),
            )
            cursor = last_id

    def fetch_forum(
        self, forum_id: str, capabilities: VenueCapabilities
    ) -> tuple[dict | None, list[dict]]:
        notes = self.client.get_all_notes(forum=forum_id)
        visible = [note for note in notes if self._visible(note)]
        root = next(
            (
                note
                for note in visible
                if _attr(note, "id", "") == forum_id
                or _attr(note, "forum", "") in (None, "", _attr(note, "id", ""))
            ),
            None,
        )
        submission = (
            self.normalize_submission(root, capabilities) if root is not None else None
        )
        self._enrich_submission_affiliations(submission)
        replies = [
            self.normalize_reply(note)
            for note in visible
            if _attr(note, "id", "") != forum_id and note is not root
        ]
        return submission, replies

    def _enrich_submission_affiliations(self, submission: dict | None) -> None:
        """Use explicit Note fields, then the first PDF pages as a fallback."""

        if not submission or submission.get("affiliations"):
            return
        pdf_url = str(submission.get("pdf") or "").strip()
        if not pdf_url:
            return
        try:
            from conference_figures import DEFAULT_ALLOWED_HOSTS, download_pdf

            session = getattr(self.client, "session", None) or requests
            pdf_bytes = self._pdf_bytes_cache.get(pdf_url)
            if pdf_bytes is None:
                pdf_bytes = download_pdf(
                    pdf_url,
                    note_id=str(submission.get("forum_id") or ""),
                    session=session,
                    timeout=self.api_timeout,
                    allowed_hosts=DEFAULT_ALLOWED_HOSTS,
                )
                self._pdf_bytes_cache[pdf_url] = pdf_bytes
            affiliations = extract_affiliations_from_pdf(pdf_bytes)
        except Exception:
            affiliations = []
        if affiliations:
            submission["affiliations"] = affiliations
            submission["affiliation_source"] = "pdf"

    def normalize_submission(self, note: Any, capabilities: VenueCapabilities) -> dict:
        content = public_content(note, enforce=self._authenticated and self.public_only)
        note_id = str(_attr(note, "id", "") or "")
        forum_id = str(_attr(note, "forum", "") or note_id)
        venue_id = str(content_value(content, "venueid", "") or "")
        status = "unknown"
        if venue_id == capabilities.withdrawn_venue_id:
            status = "withdrawn"
        elif venue_id == capabilities.desk_rejected_venue_id:
            status = "desk_rejected"
        elif venue_id == capabilities.rejected_venue_id:
            status = "rejected"
        elif venue_id == capabilities.venue_id:
            status = "accepted"
        elif venue_id == capabilities.submission_venue_id:
            status = "under_review"

        authors = content_value(content, "authors", [])
        affiliations = []
        affiliation_source = "missing"
        for field_name in (
            "affiliations",
            "author_affiliations",
            "authoraffiliations",
            "institutions",
        ):
            candidate = content_value(content, field_name, [])
            if candidate:
                affiliations = [str(value) for value in _as_list(candidate) if str(value).strip()]
                if affiliations:
                    affiliation_source = "note"
                    break
        keywords = content_value(content, "keywords", [])
        raw_pdf = str(content_value(content, "pdf", "") or "").strip()
        invitations = _as_list(_attr(note, "invitations", []))
        return {
            "id": note_id,
            "note_id": note_id,
            "forum_id": forum_id,
            "source_provider": "openreview",
            "number": _attr(note, "number", None),
            "title": str(content_value(content, "title", "") or "").strip(),
            "abstract": str(content_value(content, "abstract", "") or "").strip(),
            "authors": [str(x) for x in _as_list(authors)],
            "affiliations": affiliations,
            "affiliation_source": affiliation_source,
            "keywords": [str(x) for x in _as_list(keywords)],
            "venue": str(content_value(content, "venue", "") or ""),
            "venue_id": venue_id,
            "status": status,
            "pdf": _absolute_openreview_url(raw_pdf),
            "landing_url": f"https://openreview.net/forum?id={forum_id}",
            # Keep the Note field as supplied.  OpenReview uses both relative
            # attachment paths and absolute URLs across venues/revisions.
            "pdf_field": raw_pdf,
            "code_url": _link_to_code(content),
            "cdate": int(_attr(note, "cdate", 0) or 0),
            "mdate": int(_attr(note, "tmdate", 0) or _attr(note, "mdate", 0) or 0),
            "invitations": [str(x) for x in invitations],
            "camera_ready": any(
                str(x).lower().endswith("camera_ready_revision") for x in invitations
            ),
        }

    def normalize_reply(self, note: Any) -> dict:
        content = public_content(note, enforce=self._authenticated and self.public_only)
        return {
            "id": str(_attr(note, "id", "") or ""),
            "forum_id": str(_attr(note, "forum", "") or ""),
            "replyto": str(_attr(note, "replyto", "") or ""),
            "invitations": [str(x) for x in _as_list(_attr(note, "invitations", []))],
            "content": content,
            "cdate": int(_attr(note, "cdate", 0) or 0),
            "mdate": int(_attr(note, "tmdate", 0) or _attr(note, "mdate", 0) or 0),
        }

class OpenReviewRuntime:
    """Own one authenticated OpenReview client for a pipeline run."""

    def __init__(self, config: dict):
        bootstrap = OpenReviewProvider(config)
        self.client = bootstrap.client
        self.authenticated = bootstrap._authenticated
        self._closed = False

    def provider(self, config: dict) -> OpenReviewProvider:
        if self._closed:
            raise RuntimeError("OpenReview runtime is closed")
        return OpenReviewProvider(
            config,
            client=self.client,
            authenticated=self.authenticated,
        )

    def close(self) -> None:
        if self._closed:
            return
        for owner in (self.client, getattr(self.client, "api_client", None)):
            for name in ("session", "_session"):
                session = getattr(owner, name, None)
                close = getattr(session, "close", None)
                if callable(close):
                    close()
                    self._closed = True
                    return
        self._closed = True


def invitation_matches(reply: dict, suffixes: Iterable[str]) -> bool:
    normalized = {str(s).lower().replace(" ", "_").lstrip("-/") for s in suffixes if s}
    for invitation in reply.get("invitations", []):
        tail = str(invitation).rsplit("/", 1)[-1].lower().replace(" ", "_")
        if tail in normalized:
            return True
    return False
