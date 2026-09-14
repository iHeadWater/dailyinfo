"""Check that the tests guarding this branch's fixes can actually fail.

Each probe breaks one line of production code and asserts that a named test
goes red. A fix whose mutation leaves the suite green is a fix nothing
verifies -- and that, not any single bug, is what every review round on this
branch kept finding: a pin that never applied, a redaction with no test, a
test that could not fail.

Prose review found those; this script is meant to, from now on.

Run with a clean working tree: it edits files and restores them.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Probe:
    """One invariant, the edit that breaks it, and the test that must notice."""

    label: str
    path: str
    old: str
    new: str
    test: str


PROBES: tuple[Probe, ...] = (
    Probe(
        # The original finding: deleting the wiring from main() left the suite
        # green, so a refactor could have disabled the fallback silently.
        label="the fallback key comes from the getter, not a literal",
        path="scripts/run_pipelines.py",
        old="    glm_key = _get_glm_key()",
        new='    glm_key = ""',
        test=(
            "tests/test_run_pipelines.py"
            "::test_call_ai_uses_deepseek_primary_glm_fallback"
        ),
    ),
    Probe(
        # startswith("your_") passed this test's predecessor while accepting
        # the placeholder .env.example actually ships.
        label="placeholder keys are rejected on the environment path",
        path="scripts/run_pipelines.py",
        old='    key = os.environ.get("GLM_API_KEY", "")\n    if key and "your_" not in key:',
        new='    key = os.environ.get("GLM_API_KEY", "")\n    if key:',
        test=("tests/test_run_pipelines.py::test_load_glm_key_rejects_env_placeholder"),
    ),
    Probe(
        # The last unredacted provider string in the four scripts, and the
        # second time a security fix on this branch shipped without a test.
        label="a failed send redacts and bounds the response body",
        path="scripts/push_to_discord.py",
        old="{one_line(resp.text, DISCORD_BOT_TOKEN)[:BODY_EXCERPT_CHARS]}",
        new="{resp.text}",
        test=(
            "tests/test_push_to_discord.py"
            "::test_send_failure_redacts_and_bounds_the_response_body"
        ),
    ),
    Probe(
        # The isolation has to be asserted, or a future conftest edit that
        # stops applying it is invisible until someone's suite goes red.
        label="the suite reads .env from a temp dir, not the repository",
        path="tests/conftest.py",
        old='        ("run_pipelines", "PROJECT_ROOT", str(tmp_path)),',
        new='        ("run_pipelines", "PROJECT_ROOT", str(REPO_ROOT)),',
        test=(
            "tests/test_env_isolation.py"
            "::test_run_pipelines_reads_a_env_from_a_temp_dir"
        ),
    ),
    Probe(
        # A behaviour change described as a logging change: an empty
        # generation now skips the push instead of sending a bare header.
        label="an empty generation is not pushed",
        path="scripts/backfill_push.py",
        old="    if not content:",
        new="    if False:",
        test=(
            "tests/test_backfill_push.py"
            "::test_empty_generation_is_logged_as_skipped_not_as_a_failure"
        ),
    ),
)


def _run_test(node: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", node],
        cwd=REPO_ROOT,
        capture_output=True,
    ).returncode


def _working_tree_changes() -> str:
    """Modified tracked files, ignoring untracked ones.

    Only tracked files are edited here, and they are restored from their
    current content, so uncommitted work is preserved -- but a run killed
    mid-probe would leave the mutation sitting in it, which is worth refusing.
    Untracked files are never at risk, so they do not block.
    """
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    dirty = _working_tree_changes()
    if dirty:
        print("refusing to run: this script edits files and restores them,")
        print("and the working tree has uncommitted changes:\n")
        print(dirty)
        return 2

    failures: list[str] = []
    for probe in PROBES:
        target = REPO_ROOT / probe.path
        original = target.read_text(encoding="utf-8")
        found = original.count(probe.old)
        if found != 1:
            failures.append(
                f"{probe.label}: anchor occurs {found} times in {probe.path}, "
                "expected 1 -- the code moved, so the probe no longer tests it"
            )
            continue

        try:
            target.write_text(original.replace(probe.old, probe.new), encoding="utf-8")
            returncode = _run_test(probe.test)
        finally:
            target.write_text(original, encoding="utf-8")

        if returncode == 0:
            failures.append(
                f"{probe.label}: {probe.test} passed against the broken code, "
                "so it does not guard this"
            )
        else:
            print(f"ok    {probe.label}")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL  {failure}")
        return 1

    print(f"\n{len(PROBES)} probes, each turned its test red")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
