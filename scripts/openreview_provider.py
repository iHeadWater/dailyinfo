"""OpenReview API v2 provider used by the conference pipeline.

The provider deliberately exposes normalized dictionaries instead of leaking
``openreview-py`` model objects into the orchestration layer.  Authentication
is optional; when it is used, ``public_only`` keeps private notes out of
briefings by requiring public readers on every returned note.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class OpenReviewProviderError(RuntimeError):
    """Base error for a single OpenReview source."""


class OpenReviewConfigError(OpenReviewProviderError):
    """Raised when source or credential configuration is incomplete."""


class OpenReviewNotPublic(OpenReviewProviderError):
    """Raised when a venue exists but no public submissions are available."""


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
        return "NOT_PUBLIC"
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

    def __init__(self, config: dict, client: Any = None):
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
        self._authenticated = False
        self.client = client or self._build_client()

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

        kwargs = {"baseurl": self.baseurl}
        if username and password:
            kwargs.update(username=username, password=password)
            self._authenticated = True
        return openreview.api.OpenReviewClient(**kwargs)

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
        replies = [
            self.normalize_reply(note)
            for note in visible
            if _attr(note, "id", "") != forum_id and note is not root
        ]
        return submission, replies

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
        keywords = content_value(content, "keywords", [])
        invitations = _as_list(_attr(note, "invitations", []))
        return {
            "id": note_id,
            "forum_id": forum_id,
            "number": _attr(note, "number", None),
            "title": str(content_value(content, "title", "") or "").strip(),
            "abstract": str(content_value(content, "abstract", "") or "").strip(),
            "authors": [str(x) for x in _as_list(authors)],
            "keywords": [str(x) for x in _as_list(keywords)],
            "venue": str(content_value(content, "venue", "") or ""),
            "venue_id": venue_id,
            "status": status,
            "pdf": _absolute_openreview_url(content_value(content, "pdf", "")),
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


def invitation_matches(reply: dict, suffixes: Iterable[str]) -> bool:
    normalized = {str(s).lower().replace(" ", "_").lstrip("-/") for s in suffixes if s}
    for invitation in reply.get("invitations", []):
        tail = str(invitation).rsplit("/", 1)[-1].lower().replace(" ", "_")
        if tail in normalized:
            return True
    return False
