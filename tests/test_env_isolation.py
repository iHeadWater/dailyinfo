"""Pin the test suite's isolation from the developer's real ``.env``.

Two separate fixes on this branch were dead code that looked effective: a
conftest pin that never applied (its own teardown popped the module it meant
to pin), and a suite that stayed green only because per-test pins carried it.
Both were "verified" by running the suite and seeing green.

These assertions are the cheap mechanical check those fixes lacked: if the
isolation ever stops applying, this file goes red rather than a future test
quietly reading the developer's credentials.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()


def test_paths_env_file_does_not_point_at_the_repository():
    import paths

    assert paths.ENV_FILE != REPO_ROOT / ".env"
    assert not paths.ENV_FILE.exists()


def test_run_pipelines_reads_a_env_from_a_temp_dir():
    import run_pipelines as rp

    assert rp.PROJECT_ROOT != str(REPO_ROOT)
    assert not Path(rp.PROJECT_ROOT, ".env").exists()


def test_sibling_scripts_read_a_env_from_a_temp_dir():
    import backfill_push as bp
    import push_to_discord as pd
    import weekly_summary as ws

    assert bp.PROJECT_ROOT != str(REPO_ROOT)
    assert pd.PROJECT_ROOT != str(REPO_ROOT)
    assert ws.ENV_PATH != REPO_ROOT / ".env"
    assert not ws.ENV_PATH.exists()
