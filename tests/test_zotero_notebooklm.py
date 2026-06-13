"""Tests for the Zotero -> NotebookLM briefing workflow."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _paper(**overrides):
    import zotero_notebooklm as zn

    values = {
        "key": "PAPER1",
        "item_type": "journalArticle",
        "title": "Hydrology Foundation Models",
        "creators": ["Ada Lovelace", "Grace Hopper"],
        "date_added": "2026-05-27T12:00:00Z",
        "year": "2026",
        "venue": "Journal of AI Hydrology",
        "doi": "10.1234/example",
        "url": "https://example.test/paper",
        "abstract": "A compact abstract.",
        "tags": ["ai4science", "hydrology"],
    }
    values.update(overrides)
    return zn.ZoteroPaper(**values)


def test_parse_date_defaults_and_validates():
    import zotero_notebooklm as zn

    assert zn.parse_date("2026-05-27") == dt.date(2026, 5, 27)
    assert isinstance(zn.parse_date(None), dt.date)

    try:
        zn.parse_date("2026/05/27")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("invalid date should raise")


def test_fetch_zotero_papers_for_date_filters_type_and_date(monkeypatch):
    import zotero_notebooklm as zn

    rows = [
        {
            "key": "KEEP",
            "data": {
                "key": "KEEP",
                "itemType": "journalArticle",
                "title": "Keep Me",
                "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
                "dateAdded": "2026-05-27T12:00:00Z",
                "date": "2026-04-01",
                "publicationTitle": "Nature Water",
                "DOI": "10.1000/keep",
                "tags": [{"tag": "water"}],
            },
        },
        {
            "key": "SKIPTYPE",
            "data": {
                "itemType": "book",
                "title": "Not a paper",
                "dateAdded": "2026-05-27T13:00:00Z",
            },
        },
        {
            "key": "OLDER",
            "data": {
                "itemType": "journalArticle",
                "title": "Older",
                "dateAdded": "2026-05-26T12:00:00Z",
            },
        },
    ]

    monkeypatch.setattr(zn, "_zotero_get", lambda path, **kwargs: rows)

    papers = zn.fetch_zotero_papers_for_date(dt.date(2026, 5, 27), limit=10)

    assert [paper.key for paper in papers] == ["KEEP"]
    assert papers[0].creators == ["Ada Lovelace"]
    assert papers[0].venue == "Nature Water"
    assert papers[0].tags == ["water"]


def test_resolve_collection_and_collection_scoped_fetch(monkeypatch):
    import zotero_notebooklm as zn

    calls = []

    def fake_get(path, **kwargs):
        calls.append(path)
        if path.startswith("/api/users/0/collections?"):
            return [{"key": "RTMA4NWL", "data": {"key": "RTMA4NWL", "name": "water"}}]
        if path.startswith("/api/users/0/collections/RTMA4NWL/items/top?"):
            return [
                {
                    "key": "PAPERW",
                    "data": {
                        "key": "PAPERW",
                        "itemType": "preprint",
                        "title": "Water Paper",
                        "dateAdded": "2026-05-27T01:00:00Z",
                    },
                }
            ]
        return []

    monkeypatch.setattr(zn, "_zotero_get", fake_get)

    collection = zn.resolve_zotero_collection("water")
    papers = zn.fetch_zotero_papers_for_date(
        dt.date(2026, 5, 27),
        collection_key=collection["key"],
    )

    assert collection["key"] == "RTMA4NWL"
    assert [paper.key for paper in papers] == ["PAPERW"]
    assert any("/collections/RTMA4NWL/items/top?" in call for call in calls)


def test_attach_and_copy_pdfs_copies_and_records_missing(tmp_path, monkeypatch):
    import zotero_notebooklm as zn

    source = tmp_path / "source paper.pdf"
    source.write_bytes(b"%PDF-1.4 test")
    missing = tmp_path / "missing.pdf"
    paper = _paper(key="PAPERPDF")

    def fake_zotero_get(path, **kwargs):
        if path.endswith("/items/PAPERPDF/children"):
            return [
                {
                    "key": "ATTACH1",
                    "data": {
                        "key": "ATTACH1",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "title": "source paper.pdf",
                    },
                },
                {
                    "key": "MISSING",
                    "data": {
                        "key": "MISSING",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "title": "missing.pdf",
                    },
                },
                {
                    "key": "NOTE",
                    "data": {
                        "key": "NOTE",
                        "itemType": "attachment",
                        "contentType": "text/html",
                        "title": "not-pdf.html",
                    },
                },
            ]
        if path.endswith("/items/ATTACH1/file/view/url"):
            return source.as_uri()
        if path.endswith("/items/MISSING/file/view/url"):
            return missing.as_uri()
        raise AssertionError(path)

    monkeypatch.setattr(zn, "_zotero_get", fake_zotero_get)

    zn.attach_and_copy_pdfs([paper], tmp_path / "pdfs")

    assert len(paper.pdfs) == 2
    assert paper.pdfs[0].status == "copied"
    assert Path(paper.pdfs[0].copied_path).read_bytes() == b"%PDF-1.4 test"
    assert paper.pdfs[1].status == "missing"
    assert "not found" in paper.pdfs[1].error


def test_attachment_lookup_failure_does_not_block(tmp_path, monkeypatch):
    import zotero_notebooklm as zn

    paper = _paper(key="BROKEN")

    def broken_lookup(item_key):
        raise RuntimeError(f"cannot inspect {item_key}")

    monkeypatch.setattr(zn, "fetch_pdf_attachments", broken_lookup)

    zn.attach_and_copy_pdfs([paper], tmp_path / "pdfs")

    assert paper.pdfs[0].status == "lookup_failed"
    assert "cannot inspect BROKEN" in paper.pdfs[0].error


def test_open_missing_pdf_attempt_is_recorded(tmp_path, monkeypatch):
    import zotero_notebooklm as zn

    paper = _paper(key="OPENME")
    missing = tmp_path / "cloud-only.pdf"
    opened = []

    def fake_zotero_get(path, **kwargs):
        if path.endswith("/items/OPENME/children"):
            return [
                {
                    "key": "ATTACHCLOUD",
                    "data": {
                        "key": "ATTACHCLOUD",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "title": "cloud-only.pdf",
                    },
                }
            ]
        if path.endswith("/items/ATTACHCLOUD/file/view/url"):
            return missing.as_uri()
        raise AssertionError(path)

    def fake_open(target):
        opened.append(str(target))
        return "viewer failed"

    monkeypatch.setattr(zn, "_zotero_get", fake_zotero_get)
    monkeypatch.setattr(zn, "_try_open_target", fake_open)

    zn.attach_and_copy_pdfs(
        [paper],
        tmp_path / "pdfs",
        open_missing=True,
        open_wait_seconds=0,
    )

    assert opened == [zn._zotero_attachment_uri("ATTACHCLOUD"), str(missing)]
    assert paper.pdfs[0].open_attempted is True
    assert paper.pdfs[0].open_target.endswith(str(missing))
    assert "Zotero URI open failed: viewer failed" in paper.pdfs[0].error
    assert "local file path failed: viewer failed" in paper.pdfs[0].error


def test_render_source_index_and_manual_steps_include_expected_materials(tmp_path):
    import zotero_notebooklm as zn

    paper = _paper()
    copied = tmp_path / "pdfs" / "paper.pdf"
    paper.pdfs.append(
        zn.PdfAttachment(
            key="ATTACH1",
            title="paper.pdf",
            source_path=str(copied),
            copied_path=str(copied),
            status="copied",
        )
    )

    index = zn.render_source_index([paper], dt.date(2026, 5, 27))
    assert "Hydrology Foundation Models" in index
    assert "Zotero key: `PAPER1`" in index
    assert "paper.pdf" in index
    assert "中文论文简报" in index

    paths = zn.WorkflowPaths(
        output_dir=tmp_path,
        pdf_dir=tmp_path / "pdfs",
        source_index=tmp_path / "source_index.md",
        briefing=tmp_path / "briefing.md",
        status_json=tmp_path / "notebooklm.json",
        manual_steps=tmp_path / "MANUAL_NOTEBOOKLM_STEPS.md",
        prompt_file=tmp_path / "briefing_prompt.md",
        audio=tmp_path / "audio_overview.mp3",
        video=tmp_path / "video_overview.mp4",
    )
    manual = zn.render_manual_steps(paths, "both", "Notebook", ["not logged in"])
    assert "Audio Overview" in manual
    assert "Video Overview" in manual
    assert "not logged in" in manual


def test_run_zotero_brief_manual_only_writes_material_package(tmp_data_root, monkeypatch):
    import zotero_notebooklm as zn

    paper = _paper()
    calls = {"adapter": 0}

    def fake_attach(papers, pdf_dir, **kwargs):
        pdf_dir.mkdir(parents=True, exist_ok=True)
        copied = pdf_dir / "paper.pdf"
        copied.write_bytes(b"%PDF-1.4 test")
        papers[0].pdfs.append(
            zn.PdfAttachment(
                key="ATTACH1",
                title="paper.pdf",
                source_path=str(copied),
                copied_path=str(copied),
                status="copied",
            )
        )

    class AdapterShouldNotRun:
        def run(self, **kwargs):
            calls["adapter"] += 1
            raise AssertionError("manual-only should not call NotebookLM")

    monkeypatch.setattr(zn, "fetch_zotero_papers_for_date", lambda target_date, **kwargs: [paper])
    monkeypatch.setattr(zn, "attach_and_copy_pdfs", fake_attach)

    code = zn.run_zotero_brief(
        date_str="2026-05-27",
        artifact="video",
        manual_only=True,
        adapter=AdapterShouldNotRun(),
    )

    out = tmp_data_root / "zotero" / "2026-05-27"
    status = json.loads((out / "notebooklm.json").read_text(encoding="utf-8"))
    assert code == 0
    assert calls["adapter"] == 0
    assert (out / "source_index.md").exists()
    assert (out / "pdfs" / "paper.pdf").exists()
    assert "手动生成简报" in (out / "briefing.md").read_text(encoding="utf-8")
    assert "Video Overview" in (out / "MANUAL_NOTEBOOKLM_STEPS.md").read_text(encoding="utf-8")
    assert status["manual_only"] is True
    assert status["papers"][0]["pdfs"][0]["status"] == "copied"


def test_run_zotero_brief_uses_adapter_and_records_success(tmp_data_root, monkeypatch):
    import zotero_notebooklm as zn

    paper = _paper()

    def fake_attach(papers, pdf_dir, **kwargs):
        pdf_dir.mkdir(parents=True, exist_ok=True)

    class FakeAdapter:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            kwargs["paths"].briefing.write_text("NotebookLM 简报\n", encoding="utf-8")
            return {
                "ok": True,
                "mode": "fake",
                "notebook_id": "notebook-1",
                "source_ids": ["source-1"],
                "artifact_ids": {"audio": "audio-1"},
                "errors": [],
                "warnings": [],
            }

    adapter = FakeAdapter()
    monkeypatch.setattr(zn, "fetch_zotero_papers_for_date", lambda target_date, **kwargs: [paper])
    monkeypatch.setattr(zn, "attach_and_copy_pdfs", fake_attach)

    code = zn.run_zotero_brief(date_str="2026-05-27", artifact="audio", adapter=adapter)

    out = tmp_data_root / "zotero" / "2026-05-27"
    status = json.loads((out / "notebooklm.json").read_text(encoding="utf-8"))
    assert code == 0
    assert adapter.calls[0]["artifact"] == "audio"
    assert (out / "briefing.md").read_text(encoding="utf-8") == "NotebookLM 简报\n"
    assert status["notebooklm"]["notebook_id"] == "notebook-1"
    assert status["notebooklm"]["artifact_ids"]["audio"] == "audio-1"


def test_run_zotero_brief_failure_keeps_manual_fallback(tmp_data_root, monkeypatch):
    import zotero_notebooklm as zn

    paper = _paper()

    class FailingAdapter:
        def run(self, **kwargs):
            return {
                "ok": False,
                "mode": "failed",
                "notebook_id": "",
                "source_ids": [],
                "artifact_ids": {},
                "errors": ["notebooklm is not installed"],
                "warnings": [],
            }

    monkeypatch.setattr(zn, "fetch_zotero_papers_for_date", lambda target_date, **kwargs: [paper])
    monkeypatch.setattr(zn, "attach_and_copy_pdfs", lambda papers, pdf_dir, **kwargs: pdf_dir.mkdir(parents=True, exist_ok=True))

    code = zn.run_zotero_brief(date_str="2026-05-27", artifact="both", adapter=FailingAdapter())

    out = tmp_data_root / "zotero" / "2026-05-27"
    status = json.loads((out / "notebooklm.json").read_text(encoding="utf-8"))
    assert code == 0
    assert "自动化未完成" in (out / "briefing.md").read_text(encoding="utf-8")
    assert "notebooklm is not installed" in (out / "MANUAL_NOTEBOOKLM_STEPS.md").read_text(encoding="utf-8")
    assert status["notebooklm"]["ok"] is False
    assert status["notebooklm"]["errors"] == ["notebooklm is not installed"]
