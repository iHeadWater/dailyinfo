"""OpenReview API v2 provider for the conference ingestion pipeline.

The provider exposes normalized dictionaries instead of leaking
``openreview-py`` model objects into the orchestration layer.  Authentication
is optional at construction time; when it is used, ``public_only`` keeps
private notes out of the local store by requiring public readers on every
returned note.

This is the submission-metadata slice of the conference feature.  Replies,
reviews, PDF downloads and affiliation extraction deliberately live outside
this module so the ingestion path stays small and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import os
import random
import time
from typing import Any


from paths import _read_env_value

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

    if isinstance(exc, OpenReviewConfigError):
        return "INVALID_CONFIG"
    if isinstance(exc, OpenReviewNotPublic):
        return "NOT_OPEN"
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
    """Read a credential without bypassing ``DAILYINFO_ENV_FILE``.

    The PR version hard-coded ``PROJECT_ROOT/.env``; that defeats the decoy
    environment the test suite uses.  ``paths._read_env_value`` is the seam
    every other script already goes through.
    """

    return os.environ.get(key, "").strip() or _read_env_value(key)


def _load_credentials() -> tuple[str, str]:
    """Return ``(login_email, password)``.

    OpenReview authenticates by email; ``openreview-py`` only calls the
    parameter ``username``.  ``OPENREVIEW_EMAIL`` is the primary name, with
    ``OPENREVIEW_USERNAME`` kept as a fallback for existing installs and the
    original PR's configuration.
    """

    email = _load_env_value("OPENREVIEW_EMAIL") or _load_env_value(
        "OPENREVIEW_USERNAME"
    )
    return email, _load_env_value("OPENREVIEW_PASSWORD")


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


class OpenReviewProvider:
    """Read submissions from one OpenReview API v2 venue."""

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
                "public_only=false is not supported by the conference pipeline"
            )
        self._authenticated = (
            bool(authenticated) if authenticated is not None else False
        )
        self.api_timeout = self._api_timeout()
        self.client = client or self._build_client()
        if client is not None and authenticated is None:
            self._authenticated = bool(
                getattr(client, "_dailyinfo_authenticated", False)
            )

    def _api_timeout(self) -> tuple[float, float]:
        """Return the connect/read timeout used for every API request."""

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
        """Install a default timeout and 429 retry on an openreview-py session.

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
                reset = (payload.get("details") or {}).get("resetTime")
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
        email, password = _load_credentials()
        if bool(email) != bool(password):
            raise OpenReviewConfigError(
                "OPENREVIEW_EMAIL (or legacy OPENREVIEW_USERNAME) and "
                "OPENREVIEW_PASSWORD must be set together"
            )
        try:
            import openreview
        except ImportError as exc:  # pragma: no cover - exercised by install smoke
            raise OpenReviewConfigError(
                "openreview-py is not installed; run `uv sync` or `dailyinfo install`"
            ) from exc

        # Construct without credentials first so the timeout wrapper also
        # covers the login request made by openreview-py.
        client = openreview.api.OpenReviewClient(baseurl=self.baseurl)
        self._install_api_timeout(client)
        if email and password:
            client.login_user(email, password)
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
                affiliations = [
                    str(value) for value in _as_list(candidate) if str(value).strip()
                ]
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
            "pdf_field": raw_pdf,
            "code_url": _link_to_code(content),
            "cdate": int(_attr(note, "cdate", 0) or 0),
            "mdate": int(_attr(note, "tmdate", 0) or _attr(note, "mdate", 0) or 0),
            "invitations": [str(x) for x in invitations],
            "camera_ready": any(
                str(x).lower().endswith("camera_ready_revision") for x in invitations
            ),
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
