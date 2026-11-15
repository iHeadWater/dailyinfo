"""Read release notes out of ``CHANGELOG.md``.

The tag-triggered release workflow publishes the section a human wrote, so this
module is the only thing between "the notes exist" and "the notes ship". It
lives here rather than in a YAML ``run:`` block because that is what makes it
testable: ``tests/test_changelog.py`` pins a missing version, an empty section,
and a heading that merely shares the version's prefix.

Usage::

    python -m scripts.changelog check v0.3.1   # exit 1 when the notes are missing
    python -m scripts.changelog notes v0.3.1   # print the section body
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"

# A level-2 heading that names a released version. Brackets and a leading "v"
# are optional so the file can also be written as "## 0.3.1 - 2026-11-15".
# "## [Unreleased]" has no digits and therefore never matches.
_VERSION_HEADING = re.compile(
    r"^##[ \t]+\[?v?(?P<version>\d+\.\d+\.\d+)\]?[^\n]*$", re.MULTILINE
)


def normalize_version(raw: str) -> str:
    """Accept ``v0.3.1`` as well as ``0.3.1``."""
    return raw.strip().removeprefix("v")


def release_section(text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, heading excluded.

    The body runs until the next version heading (or the end of the file), so
    ``###`` subsections stay inside their release. Raises ``KeyError`` when the
    version has no heading and ``ValueError`` when the heading has no body.
    """
    version = normalize_version(version)
    headings = list(_VERSION_HEADING.finditer(text))
    for index, match in enumerate(headings):
        if match.group("version") != version:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end() : end].strip()
        if not body:
            raise ValueError(f"CHANGELOG.md has an empty section for {version}")
        return body
    raise KeyError(f"CHANGELOG.md has no section for {version}")


def latest_released_version(text: str) -> str:
    """Return the first released version heading, skipping ``[Unreleased]``."""
    match = _VERSION_HEADING.search(text)
    if match is None:
        raise KeyError("CHANGELOG.md has no released version heading")
    return match.group("version")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read release notes from CHANGELOG.md")
    parser.add_argument("command", choices=("check", "notes"))
    parser.add_argument("version", help="tag or version, e.g. v0.3.1")
    parser.add_argument("--changelog", type=pathlib.Path, default=CHANGELOG)
    args = parser.parse_args(argv)

    text = args.changelog.read_text(encoding="utf-8")
    try:
        body = release_section(text, args.version)
    except (KeyError, ValueError) as exc:
        # argparse does not own this failure: the file is readable, its content
        # is what makes the release impossible.
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1

    if args.command == "notes":
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
