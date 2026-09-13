"""WebPublisher for the DailyInfo Publication Contract v1.

The publisher synchronizes one canonical briefing into a configured local
``dailyinfo-web`` checkout.  It deliberately owns only generated content,
while the Web repository remains responsible for schema validation, Astro,
RSS, sitemap, and Pages deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .delivery import sanitize_error
from .models import Item, PublicationBundle
from .serialization import briefing_to_dict, item_to_dict
from .validation import validate_bundle


logger = logging.getLogger(__name__)

WEB_SINK = "web"
DEFAULT_WEB_REMOTE = "git@github.com:CylenLC/dailyinfo-web.git"
DEFAULT_WEB_BRANCH = "main"
DEFAULT_WEB_VALIDATION_COMMANDS = (
    ("npm", "run", "validate"),
    ("npm", "run", "test"),
    ("npm", "run", "check"),
    ("npm", "run", "build"),
)
_WEB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class WebPublishError(RuntimeError):
    """Raised internally when a Web publication transaction cannot complete."""

    def __init__(
        self,
        message: str,
        *,
        external_ref: Optional[str] = None,
        committed: bool = False,
    ) -> None:
        super().__init__(message)
        self.external_ref = external_ref
        self.committed = committed


@dataclass(frozen=True)
class WebPublishConfig:
    """Configuration for one persistent local Web checkout."""

    repo_path: Path
    expected_remote: str = DEFAULT_WEB_REMOTE
    expected_branch: str = DEFAULT_WEB_BRANCH
    managed_items_dir: Path = Path("src/content/items/generated")
    managed_briefings_dir: Path = Path("src/content/briefings/generated")
    validation_commands: tuple[tuple[str, ...], ...] = DEFAULT_WEB_VALIDATION_COMMANDS
    timeout_seconds: float = 300.0
    lock_path: Optional[Path] = None

    def __post_init__(self) -> None:
        repo_path = Path(self.repo_path).expanduser().resolve()
        object.__setattr__(self, "repo_path", repo_path)
        for field_name in ("managed_items_dir", "managed_briefings_dir"):
            value = Path(getattr(self, field_name))
            if value.is_absolute() or ".." in value.parts:
                raise ValueError(f"{field_name} must be a relative path")
            object.__setattr__(self, field_name, value)
        if not self.expected_remote.strip():
            raise ValueError("expected_remote must be non-empty")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.expected_branch):
            raise ValueError("expected_branch contains unsupported characters")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.lock_path is None:
            lock_path = (
                repo_path.parent / f".{repo_path.name}.dailyinfo-web.publish.lock"
            )
            object.__setattr__(self, "lock_path", lock_path)
        else:
            object.__setattr__(
                self, "lock_path", Path(self.lock_path).expanduser().resolve()
            )

    @classmethod
    def from_env(cls) -> "WebPublishConfig":
        repo_value = _read_config_value("DAILYINFO_WEB_REPO")
        if not repo_value:
            raise WebPublishError(
                "DAILYINFO_WEB_REPO is required; configure the local dailyinfo-web checkout"
            )
        return cls(
            repo_path=Path(repo_value),
            expected_remote=(
                _read_config_value("DAILYINFO_WEB_REMOTE") or DEFAULT_WEB_REMOTE
            ),
            expected_branch=(
                _read_config_value("DAILYINFO_WEB_BRANCH") or DEFAULT_WEB_BRANCH
            ),
        )


def _read_config_value(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    try:
        from paths import ENV_FILE
    except ImportError:
        from scripts.paths import ENV_FILE

    if not ENV_FILE.exists():
        return ""
    try:
        with ENV_FILE.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise WebPublishError(f"cannot read DailyInfo config: {sanitize_error(exc)}")
    return ""


class _PublishLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "_PublishLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if isinstance(exc, BlockingIOError):
                raise WebPublishError("another Web publication is already in progress")
            raise WebPublishError(
                f"cannot acquire Web publication lock: {sanitize_error(exc)}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _yaml_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # JSON string literals are valid YAML double-quoted scalars and give us
        # deterministic escaping without adding a Python YAML dependency.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_value(item) for item in value) + "]"
    raise WebPublishError(f"unsupported Web frontmatter value: {type(value).__name__}")


def _frontmatter(data: Mapping[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.append(f"{key}:")
            for child_key, child_value in value.items():
                lines.append(f"  {child_key}: {_yaml_value(child_value)}")
        else:
            lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def serialize_web_item(item: Item) -> str:
    """Render an Item to the Web Markdown/frontmatter representation."""

    raw = item_to_dict(item)
    source = {
        "name": raw["source"]["name"],
        "url": raw["source"]["url"],
    }
    if raw["source"].get("external_id") is not None:
        source["external_id"] = raw["source"]["external_id"]
    data: dict[str, Any] = {
        "schema_version": raw["schema_version"],
        "id": raw["id"],
        "category": raw["category"],
        "title": raw["title"],
        "source": source,
        "authors": raw["authors"],
        "source_published_at": raw["source_published_at"],
        "retrieved_at": raw["retrieved_at"],
        "published_at": raw["published_at"],
    }
    if raw["updated_at"] is not None:
        data["updated_at"] = raw["updated_at"]
    data.update(
        {
            "summary": raw["summary"],
            "why_it_matters": raw["why_it_matters"],
            "tags": raw["tags"],
            "language": raw["language"],
            "briefing_ids": raw["briefing_ids"],
        }
    )
    return _frontmatter(data) + "\n"


def serialize_web_briefing(publication: PublicationBundle) -> str:
    """Render a Briefing with canonical metadata and its Markdown body."""

    raw = briefing_to_dict(publication.briefing)
    data: dict[str, Any] = {
        "schema_version": raw["schema_version"],
        "id": raw["id"],
        "category": raw["category"],
        "date": raw["date"],
        "title": raw["title"],
        "generated_at": raw["generated_at"],
        "published_at": raw["published_at"],
    }
    if raw["updated_at"] is not None:
        data["updated_at"] = raw["updated_at"]
    data["item_ids"] = raw["item_ids"]
    body = publication.briefing.body.rstrip("\n") + "\n"
    return _frontmatter(data) + "\n" + body


def _decode_scalar(value: str) -> Any:
    value = value.strip()
    if value == "null":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip('"').strip("'")


def _read_frontmatter(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WebPublishError(
            f"cannot read managed Web content {path.name}: {sanitize_error(exc)}"
        ) from exc
    if not text.startswith("---\n"):
        raise WebPublishError(f"managed Web content has no frontmatter: {path.name}")
    end = text.find("\n---", 4)
    if end < 0:
        raise WebPublishError(
            f"managed Web content has unterminated frontmatter: {path.name}"
        )
    return text[4:end]


def _frontmatter_field(path: Path, field: str) -> Any:
    frontmatter = _read_frontmatter(path)
    match = re.search(rf"^{re.escape(field)}:\s*(.*)$", frontmatter, re.MULTILINE)
    if match is None:
        raise WebPublishError(f"managed Web content is missing {field}: {path.name}")
    value = match.group(1).strip()
    if value:
        return _decode_scalar(value)
    # The generated representation uses inline JSON arrays.  The fallback
    # handles hand-authored multiline YAML lists without guessing scalars.
    values = []
    found = False
    for line in frontmatter[match.end() :].splitlines():
        if not line.strip():
            continue
        if not re.match(r"^\s+-\s+", line):
            break
        values.append(_decode_scalar(re.sub(r"^\s+-\s+", "", line)))
        found = True
    if found:
        return values
    return None


class WebPublisher:
    """Synchronize one canonical briefing to a persistent Web checkout."""

    sink = WEB_SINK

    def __init__(
        self,
        config: Optional[WebPublishConfig] = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        publication_store: Optional[Any] = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.config = config or WebPublishConfig.from_env()
        self.clock = clock
        self.publication_store = publication_store
        self.runner = runner
        self._last_external_ref: Optional[str] = None
        self._remote_needs_push = False
        self._commit_created = False

    def publish(self, publication: PublicationBundle):
        """Return a briefing-level ``PublishResult`` without leaking secrets."""

        from .publishers import PublishResult

        validate_bundle(publication)
        attempted_at = self.clock()
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("publisher clock must return a timezone-aware datetime")
        self._last_external_ref = None
        self._remote_needs_push = False
        self._commit_created = False
        try:
            with _PublishLock(self.config.lock_path):
                external_ref = self._publish_locked(publication)
            return PublishResult(
                sink=self.sink,
                publication_id=publication.briefing.id,
                status="success",
                attempted_at=attempted_at,
                external_ref=external_ref,
            )
        except WebPublishError as exc:
            return PublishResult(
                sink=self.sink,
                publication_id=publication.briefing.id,
                status="failed",
                attempted_at=attempted_at,
                external_ref=exc.external_ref or self._last_external_ref,
                error=sanitize_error(exc),
            )

    def _publish_locked(self, publication: PublicationBundle) -> str:
        self._validate_and_sync_repo()
        desired = self._desired_files(publication)
        originals = {
            path: path.read_bytes() if path.exists() else None for path in desired
        }
        changed: list[Path] = []
        try:
            for path, content in desired.items():
                if path.exists() and not path.is_file():
                    raise WebPublishError(
                        f"managed Web path is not a file: {path.name}"
                    )
                old = originals[path]
                if old == content:
                    continue
                self._atomic_write(path, content)
                changed.append(path)

            self._assert_owned_worktree(desired)
            self._run_web_gates()
            self._assert_owned_worktree(desired)
            self._run_git(("diff", "--check"), stage="git diff check")

            if not changed:
                head = self._git_stdout(("rev-parse", "HEAD"), stage="read HEAD")
                self._last_external_ref = head
                if self._remote_needs_push:
                    try:
                        self._run_git(
                            ("push", "origin", self.config.expected_branch),
                            stage="push pending Web publication",
                        )
                    except WebPublishError as exc:
                        raise WebPublishError(
                            str(exc), external_ref=head, committed=True
                        ) from exc
                self._assert_clean()
                logger.info(
                    "publication_id=%s category=%s sink=web action=noop item_count=%d commit=%s",
                    publication.briefing.id,
                    publication.briefing.category,
                    len(publication.items),
                    head,
                )
                return head

            relative_paths = [self._relative(path) for path in desired]
            self._run_git(("add", "--", *relative_paths), stage="stage Web content")
            staged = self._git_stdout(
                ("diff", "--cached", "--name-only"), stage="inspect staged Web content"
            )
            staged_paths = {line for line in staged.splitlines() if line}
            expected_paths = set(relative_paths)
            if not staged_paths.issubset(expected_paths):
                raise WebPublishError("Web transaction staged an unexpected path")
            if not staged_paths:
                raise WebPublishError(
                    "Web transaction expected changes but staged none"
                )
            self._run_git(
                ("diff", "--cached", "--check"), stage="staged Web diff check"
            )
            subject = f"publish({publication.briefing.category}): {publication.briefing.date.isoformat()}"
            body = (
                f"Publication-ID: {publication.briefing.id}\n"
                f"Schema-Version: {publication.schema_version}"
            )
            self._run_git(
                (
                    "-c",
                    "user.name=DailyInfo Bot",
                    "-c",
                    "user.email=dailyinfo-bot@users.noreply.github.com",
                    "commit",
                    "-m",
                    subject,
                    "-m",
                    body,
                ),
                stage="commit Web publication",
            )
            self._commit_created = True
            commit_sha = self._git_stdout(
                ("rev-parse", "HEAD"), stage="read Web commit"
            )
            self._last_external_ref = commit_sha
            try:
                self._run_git(
                    ("push", "origin", self.config.expected_branch),
                    stage="push Web publication",
                )
            except WebPublishError as exc:
                raise WebPublishError(
                    str(exc), external_ref=commit_sha, committed=True
                ) from exc
            self._assert_clean()
            logger.info(
                "publication_id=%s category=%s sink=web action=commit item_count=%d commit=%s",
                publication.briefing.id,
                publication.briefing.category,
                len(publication.items),
                commit_sha,
            )
            return commit_sha
        except WebPublishError:
            self._cleanup_transaction(desired, originals, changed)
            raise
        except Exception as exc:
            self._cleanup_transaction(desired, originals, changed)
            raise WebPublishError(
                f"Web publication failed: {sanitize_error(exc)}"
            ) from exc

    def _desired_files(self, publication: PublicationBundle) -> dict[Path, bytes]:
        item_files: dict[Path, bytes] = {}
        for item in publication.items:
            self._validate_web_id(item.id, "Item.id")
            for briefing_id in item.briefing_ids:
                self._validate_web_id(briefing_id, "Item.briefing_ids")
            path = self._item_path(item.category, item.id)
            item_files[path] = serialize_web_item(item).encode("utf-8")

        self._validate_web_id(publication.briefing.id, "Briefing.id")
        for item_id in publication.briefing.item_ids:
            self._validate_web_id(item_id, "Briefing.item_ids")
        briefing_path = self._briefing_path(publication)
        old_item_ids = set()
        if briefing_path.exists():
            raw_ids = _frontmatter_field(briefing_path, "item_ids")
            if not isinstance(raw_ids, list) or not all(
                isinstance(value, str) for value in raw_ids
            ):
                raise WebPublishError(
                    f"managed briefing item_ids is invalid: {briefing_path.name}"
                )
            old_item_ids = set(raw_ids)
        current_item_ids = set(publication.briefing.item_ids)
        removed_ids = old_item_ids - current_item_ids
        for item_id in sorted(removed_ids):
            if self.publication_store is None:
                raise WebPublishError(
                    "briefing update removed an Item; PublicationStore is required to reconcile relationships"
                )
            try:
                item = self.publication_store.load_item(
                    item_id, publication.briefing.category
                )
            except Exception as exc:
                raise WebPublishError(
                    f"cannot load removed Item {item_id}: {sanitize_error(exc)}"
                ) from exc
            item_files[self._item_path(item.category, item.id)] = serialize_web_item(
                item
            ).encode("utf-8")

        self._check_managed_identity_conflicts(publication)
        item_files[briefing_path] = serialize_web_briefing(publication).encode("utf-8")
        return item_files

    @staticmethod
    def _validate_web_id(value: str, field_name: str) -> None:
        if not _WEB_ID_RE.fullmatch(value):
            raise WebPublishError(
                f"{field_name} cannot be represented by the Web contract; "
                "stable IDs must match [a-z0-9][a-z0-9._-]*"
            )

    def _check_managed_identity_conflicts(self, publication: PublicationBundle) -> None:
        incoming = {item.id: item.category for item in publication.items}
        for path in self._managed_item_paths():
            item_id = _frontmatter_field(path, "id")
            category = _frontmatter_field(path, "category")
            if not isinstance(item_id, str) or not isinstance(category, str):
                raise WebPublishError(f"managed Item identity is invalid: {path.name}")
            if item_id in incoming and category != incoming[item_id]:
                raise WebPublishError(
                    f"Web Item identity migration rejected: {item_id} ({category} -> {incoming[item_id]})"
                )

    def _managed_item_paths(self) -> list[Path]:
        root = self.config.repo_path / self.config.managed_items_dir
        if not root.exists():
            return []
        return sorted(path for path in root.rglob("*.md") if path.is_file())

    def _item_path(self, category: str, item_id: str) -> Path:
        return (
            self.config.repo_path
            / self.config.managed_items_dir
            / category
            / f"{item_id}.md"
        )

    def _briefing_path(self, publication: PublicationBundle) -> Path:
        date = publication.briefing.date
        return (
            self.config.repo_path
            / self.config.managed_briefings_dir
            / f"{date.year:04d}"
            / f"{date.month:02d}"
            / f"{date.day:02d}"
            / f"{publication.briefing.category}.md"
        )

    def _validate_and_sync_repo(self) -> None:
        repo = self.config.repo_path
        if not repo.is_dir():
            raise WebPublishError(f"configured Web repo does not exist: {repo.name}")
        if not (repo / ".git").exists():
            raise WebPublishError("configured Web path is not a Git checkout")
        top = self._git_stdout(
            ("rev-parse", "--show-toplevel"), stage="validate Web checkout"
        )
        if Path(top).resolve() != repo:
            raise WebPublishError("configured Web path is not the Git checkout root")
        branch = self._git_stdout(
            ("branch", "--show-current"), stage="validate Web branch"
        )
        if branch != self.config.expected_branch:
            raise WebPublishError(
                f"Web checkout branch mismatch: expected {self.config.expected_branch}, got {branch or 'detached'}"
            )
        remote = self._git_stdout(
            ("remote", "get-url", "origin"), stage="validate Web remote"
        )
        if remote != self.config.expected_remote:
            raise WebPublishError(
                "Web checkout origin remote does not match configuration"
            )
        self._assert_clean()

        self._run_git(
            ("fetch", "origin", self.config.expected_branch), stage="fetch Web branch"
        )
        remote_ref = f"refs/remotes/origin/{self.config.expected_branch}"
        remote_head = self._git_stdout(
            ("rev-parse", remote_ref), stage="read Web remote"
        )
        local_head = self._git_stdout(("rev-parse", "HEAD"), stage="read Web HEAD")
        if local_head == remote_head:
            self._remote_needs_push = False
            return
        if self._is_ancestor(local_head, remote_head):
            self._run_git(
                ("pull", "--ff-only", "origin", self.config.expected_branch),
                stage="fast-forward Web checkout",
            )
            self._assert_clean()
            self._remote_needs_push = False
            return
        if self._is_ancestor(remote_head, local_head):
            subjects = self._git_stdout(
                ("log", "--format=%s", f"{remote_ref}..HEAD"),
                stage="inspect local Web commits",
            ).splitlines()
            if not subjects or not all(
                subject.startswith("publish(") or subject.startswith("publish:")
                for subject in subjects
            ):
                raise WebPublishError(
                    "Web checkout is ahead of origin with non-publisher commits"
                )
            self._remote_needs_push = True
            return
        raise WebPublishError(
            "Web checkout and origin have diverged; manual reconcile required"
        )

    def _run_web_gates(self) -> None:
        for command in self.config.validation_commands:
            self._run_process(command, stage=f"Web validation ({command[0]})")

    def _run_process(
        self, command: Sequence[str], *, stage: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            result = self.runner(
                list(command),
                cwd=str(self.config.repo_path),
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WebPublishError(f"{stage} failed: {sanitize_error(exc)}") from exc
        if check and result.returncode != 0:
            detail = result.stderr or result.stdout or "command failed"
            raise WebPublishError(f"{stage} failed: {sanitize_error(detail)}")
        return result

    def _run_git(
        self, args: Sequence[str], *, stage: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        return self._run_process(("git", *args), stage=stage, check=check)

    def _git_stdout(self, args: Sequence[str], *, stage: str) -> str:
        return (self._run_git(args, stage=stage).stdout or "").strip()

    def _is_ancestor(self, older: str, newer: str) -> bool:
        result = self._run_git(
            ("merge-base", "--is-ancestor", older, newer),
            stage="inspect Web history",
            check=False,
        )
        return result.returncode == 0

    def _assert_clean(self) -> None:
        status = self._git_status()
        if status:
            raise WebPublishError("Web checkout must be clean")

    def _assert_owned_worktree(self, desired: Mapping[Path, bytes]) -> None:
        status = self._git_status()
        if not status:
            return
        allowed = {self._relative(path) for path in desired}
        actual = set()
        for line in status.splitlines():
            if len(line) < 4:
                raise WebPublishError("Web checkout returned malformed Git status")
            actual.add(line[3:].strip().strip('"'))
        if not actual.issubset(allowed):
            raise WebPublishError(
                "Web transaction detected an unexpected worktree change"
            )

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.config.repo_path).as_posix()

    def _git_status(self) -> str:
        result = self._run_git(
            ("status", "--porcelain=v1", "--untracked-files=all"),
            stage="inspect Web checkout",
        )
        # Preserve the two leading porcelain status columns.  Calling
        # ``strip()`` here would turn `` M path`` into ``M path`` and make
        # the path parser drop its first character.
        return (result.stdout or "").rstrip("\r\n")

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except OSError:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise

    def _cleanup_transaction(
        self,
        desired: Mapping[Path, bytes],
        originals: Mapping[Path, Optional[bytes]],
        changed: Iterable[Path],
    ) -> None:
        # A push failure leaves an auditable local publisher commit in place;
        # only pre-commit failures are eligible for file rollback.
        if self._commit_created:
            return
        if self._last_external_ref is not None:
            try:
                subjects = self._git_stdout(
                    ("show", "-s", "--format=%s", self._last_external_ref),
                    stage="inspect Web transaction",
                )
            except WebPublishError:
                subjects = ""
            if subjects.startswith("publish(") or subjects.startswith("publish:"):
                return
        try:
            staged = self._run_git(
                ("diff", "--cached", "--name-only"),
                stage="inspect staged rollback",
                check=False,
            )
            if staged.returncode == 0 and staged.stdout:
                self._run_git(
                    ("reset", "--", *[self._relative(path) for path in desired]),
                    stage="unstage Web transaction",
                    check=False,
                )
            for path in changed:
                original = originals[path]
                current = path.read_bytes() if path.exists() else None
                expected = desired[path]
                if current != expected:
                    raise WebPublishError(
                        "Web transaction changed unexpectedly; refusing unsafe rollback"
                    )
                if original is None:
                    path.unlink()
                else:
                    self._atomic_write(path, original)
        except WebPublishError:
            raise
        except OSError as exc:
            raise WebPublishError(
                f"Web transaction rollback failed: {sanitize_error(exc)}"
            ) from exc


__all__ = [
    "DEFAULT_WEB_BRANCH",
    "DEFAULT_WEB_REMOTE",
    "DEFAULT_WEB_VALIDATION_COMMANDS",
    "WEB_SINK",
    "WebPublishConfig",
    "WebPublisher",
    "WebPublishError",
    "serialize_web_briefing",
    "serialize_web_item",
]
