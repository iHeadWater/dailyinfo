"""Canonical content may be shared with a separate read-only process."""

import os
from pathlib import Path
import stat

import pytest

from publication import PublicationStore
from publication.serialization import deserialize_bundle


@pytest.mark.parametrize(
    "group_read,expected",
    [(None, 0o600), ("1", 0o640), ("0", 0o600), ("unexpected", 0o600)],
)
def test_content_sharing_is_explicit_and_survives_atomic_replacement(
    tmp_path, monkeypatch, group_read, expected
):
    monkeypatch.delenv("DAILYINFO_PUBLICATION_GROUP_READ", raising=False)
    if group_read is not None:
        monkeypatch.setenv("DAILYINFO_PUBLICATION_GROUP_READ", group_read)
    fixture = Path(__file__).parent / "fixtures/publication_v1/bundle.json"
    bundle = deserialize_bundle(fixture.read_text())
    store = PublicationStore(tmp_path / "publications")
    previous_umask = os.umask(0o077)
    try:
        store.save(bundle)
        # A later atomic replacement must retain the selected sharing policy.
        store._atomic_write_text(
            store._briefing_markdown_path(bundle.briefing), "updated body"
        )
    finally:
        os.umask(previous_umask)
    files = [path for path in store.root.rglob("*") if path.is_file()]
    assert len(files) >= 3
    assert all(stat.S_IMODE(path.stat().st_mode) == expected for path in files)
