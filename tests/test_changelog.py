"""Tests for release-note extraction in ``scripts/changelog.py``.

The release workflow refuses to publish when these helpers cannot produce notes,
so the failure modes matter as much as the happy path: a version that is absent,
a heading that is present but empty, and a longer version that merely starts with
the requested one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import changelog

REPO_ROOT = Path(__file__).parent.parent.resolve()

SAMPLE = """\
# Changelog

## [Unreleased]

## [0.3.10] - 2030-01-01

### Fixed

- A longer version must not answer for a shorter one.

## [0.3.1] - 2026-11-15

### Fixed

- The signature ignores `st_ino`.

### Changed

- Nothing else.

## [0.3.0] - 2026-11-08

### Added

- Something older.
"""


def test_section_keeps_its_subsections_and_stops_at_the_next_version():
    body = changelog.release_section(SAMPLE, "0.3.1")

    assert body.startswith("### Fixed")
    assert "st_ino" in body
    assert "### Changed" in body
    assert "older" not in body
    assert "longer version" not in body


def test_heading_accepts_a_leading_v_and_plain_form():
    assert changelog.release_section(SAMPLE, "v0.3.1") == changelog.release_section(
        SAMPLE, "0.3.1"
    )

    plain = "## 0.3.1 - 2026-11-15\n\n- note\n"
    assert changelog.release_section(plain, "0.3.1") == "- note"


def test_a_longer_version_does_not_answer_for_its_prefix():
    with pytest.raises(KeyError):
        changelog.release_section(SAMPLE, "0.3")


def test_missing_version_raises():
    with pytest.raises(KeyError, match="no section for 9.9.9"):
        changelog.release_section(SAMPLE, "9.9.9")


def test_empty_section_raises():
    text = "## [0.4.0] - 2030-01-01\n\n## [0.3.1] - 2026-11-15\n\n- note\n"

    with pytest.raises(ValueError, match="empty section for 0.4.0"):
        changelog.release_section(text, "0.4.0")


def test_unreleased_is_skipped_when_reading_the_latest_version():
    assert changelog.latest_released_version(SAMPLE) == "0.3.10"


def test_cli_prints_the_section_body(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")

    assert changelog.main(["notes", "v0.3.1", "--changelog", str(path)]) == 0
    assert "st_ino" in capsys.readouterr().out


def test_cli_check_is_silent_on_success(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")

    assert changelog.main(["check", "0.3.1", "--changelog", str(path)]) == 0
    assert capsys.readouterr().out == ""


def test_cli_reports_a_missing_section(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")

    assert changelog.main(["notes", "1.0.0", "--changelog", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no section for 1.0.0" in captured.err


def test_repository_changelog_documents_the_current_version():
    """The version being released must already have notes, or the tag is red."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]

    assert changelog.latest_released_version(text) == version
    assert changelog.release_section(text, version).strip()
