from __future__ import annotations

from pathlib import Path

from conference_web_provider import (
    ACLAnthologyProvider,
    CVFOpenAccessProvider,
    DBLPProvider,
    NeurIPSProceedingsProvider,
    WebConferenceNotReady,
    _extract_code_url_from_html,
    _extract_code_url_from_pdf,
)


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return FakeResponse(self.pages[url])

    def close(self):
        return None


def test_acl_provider_discovers_volumes_and_normalizes_papers(tmp_path: Path):
    event = "https://aclanthology.org/events/acl-2026/"
    volume = "https://aclanthology.org/volumes/2026.acl-long/"
    paper = "https://aclanthology.org/2026.acl-long.1/"
    session = FakeSession(
        {
            event: '<a href="/volumes/2026.acl-long/">ACL Long</a>',
            volume: """
              <ul><li><strong>
                <a href="/2026.acl-long.1/">HydroCast</a>
                <a href="/people/alice/">Alice</a>
              </strong></li></ul>
              <div id="abstract-2026--acl-long--1">
                A streamflow forecasting model.
              </div>
            """,
        }
    )
    provider = ACLAnthologyProvider(
        {
            "provider": "acl",
            "venue_id": "ACL2026",
            "url": event,
            "provider_cache_dir": str(tmp_path),
        },
        session=session,
    )

    pages = list(provider.iter_submission_pages(provider.discover_venue(), page_size=1))
    assert len(pages) == 1
    record = pages[0].papers[0]
    assert record["source_provider"] == "acl"
    assert record["forum_id"].startswith("acl:")
    assert record["title"] == "HydroCast"
    assert "streamflow" in record["abstract"]
    assert record["authors"] == ["Alice"]
    assert record["pdf"] == paper.rstrip("/") + ".pdf"
    assert record["landing_url"] == paper
    assert session.calls == [event, volume]


def test_cvf_provider_reads_detail_abstract_authors_and_pdf(tmp_path: Path):
    listing = "https://openaccess.thecvf.com/CVPR2026?day=all"
    paper = "https://openaccess.thecvf.com/content/CVPR2026/html/A/HydroCast_CVPR_2026_paper.html"
    session = FakeSession(
        {
            listing: """
              <dl>
                <dt class="ptitle"><a href="content/CVPR2026/html/A/HydroCast_CVPR_2026_paper.html">HydroCast</a></dt>
                <dd><a href="#">Alice</a>, <a href="#">Bob</a></dd>
              </dl>
            """,
            paper: """
              <div id="abstract">A flood forecasting architecture.</div>
              <a href="../pdf/A/HydroCast_CVPR_2026_paper.pdf">PDF</a>
            """,
        }
    )
    provider = CVFOpenAccessProvider(
        {
            "provider": "cvf",
            "venue_id": "CVPR2026",
            "url": listing,
            "provider_cache_dir": str(tmp_path),
        },
        session=session,
    )

    pages = list(provider.iter_submission_pages(provider.discover_venue()))
    record = pages[0].papers[0]
    assert record["source_provider"] == "cvf"
    assert record["title"] == "HydroCast"
    assert record["authors"] == ["Alice", "Bob"]
    assert "flood forecasting" in record["abstract"]
    assert record["pdf"].endswith("HydroCast_CVPR_2026_paper.pdf")
    assert record["landing_url"] == paper
    assert session.calls == [listing, paper]


def test_ecva_eccv_listing_uses_cvf_provider_shape(tmp_path: Path):
    listing = "https://www.ecva.net/papers.php"
    paper = "https://www.ecva.net/papers/eccv_2024/papers_ECCV/html/4_ECCV_2024_paper.php"
    session = FakeSession(
        {
            listing: """
              <dt class="ptitle"><a href="papers/eccv_2024/papers_ECCV/html/4_ECCV_2024_paper.php">HydroCast</a></dt>
              <dd>Alice, Bob</dd>
            """,
            paper: """
              <div id="abstract">A flood forecasting architecture.</div>
              <a href="../../../../papers/eccv_2024/papers_ECCV/papers/00004.pdf">pdf</a>
            """,
        }
    )
    provider = CVFOpenAccessProvider(
        {
            "provider": "cvf",
            "venue_id": "ECCV2024",
            "url": listing,
            "provider_cache_dir": str(tmp_path),
        },
        session=session,
    )
    record = list(provider.iter_submission_pages(provider.discover_venue()))[0].papers[0]
    assert record["title"] == "HydroCast"
    assert record["pdf"].endswith("00004.pdf")
    assert record["landing_url"] == paper


def test_web_provider_cursor_resumes_after_a_checkpoint():
    listing = "https://aclanthology.org/volumes/2026.acl-long/"
    session = FakeSession(
        {
            listing: """
              <strong><a href="/2026.acl-long.1/">One</a></strong>
              <strong><a href="/2026.acl-long.2/">Two</a></strong>
            """,
        }
    )
    provider = ACLAnthologyProvider(
        {"provider": "acl", "venue_id": "ACL2026", "url": listing},
        session=session,
    )
    capabilities = provider.discover_venue()
    first = list(provider.iter_submission_pages(capabilities, page_size=1))
    resumed = list(
        provider.iter_submission_pages(
            capabilities, page_size=1, after_id=first[0].cursor_after
        )
    )
    assert len(first) == 2
    assert len(resumed) == 1
    assert resumed[0].papers[0]["title"] == "Two"


def test_acl_provider_runs_through_generic_conference_pipeline(tmp_path: Path):
    from conference import run_conference_source

    listing = "https://aclanthology.org/volumes/2026.acl-long/"
    session = FakeSession(
        {
            listing: """
              <strong>
                <a href="/2026.acl-long.1/">Hydrology Forecasting with Transformers</a>
                <a href="/people/alice/">Alice</a>
              </strong>
              <div id="abstract-2026--acl-long--1">Streamflow forecasting.</div>
            """,
        }
    )
    provider = ACLAnthologyProvider(
        {"provider": "acl", "venue_id": "ACL2026", "url": listing},
        session=session,
    )
    result = run_conference_source(
        {
            "name": "acl_test",
            "display_name": "ACL Test",
            "provider": "acl",
            "venue_id": "ACL2026",
            "retrieval": {"strategy": "lexical"},
            "figures": {"enabled": False},
        },
        {"model": "test-model"},
        lambda *_args, **_kwargs: "### Hydrology paper\n\n简介。",
        tmp_path / "state",
        tmp_path / "briefings",
        "2026-08-23",
        provider=provider,
    )
    assert result.outcome == "SUCCESS"
    assert result.submissions_scanned == 1
    assert result.relevant_papers == 1
    assert result.events_created == 1


def test_web_source_not_ready_has_stable_outcome():
    from openreview_provider import classify_openreview_error

    assert classify_openreview_error(WebConferenceNotReady("not published")) == (
        "SOURCE_NOT_READY"
    )


def test_dblp_provider_normalizes_bibliographic_entries():
    listing = "https://dblp.org/db/conf/aaai/aaai2026.html"
    session = FakeSession(
        {
            listing: """
              <li class="entry inproceedings">
                <cite class="data"><span itemprop="author"><span itemprop="name">Alice</span></span>
                <span class="title" itemprop="name">HydroCast</span></cite>
                <li class="ee"><a itemprop="url" href="https://doi.org/10.1609/example">DOI</a></li>
                <li class="details"><a href="/rec/conf/aaai/Example26.html">details</a></li>
              </li>
            """,
        }
    )
    provider = DBLPProvider(
        {"provider": "dblp", "venue_id": "AAAI2026", "url": listing},
        session=session,
    )
    record = list(provider.iter_submission_pages(provider.discover_venue()))[0].papers[0]
    assert record["source_provider"] == "dblp"
    assert record["title"] == "HydroCast"
    assert record["authors"] == ["Alice"]
    assert record["status"] == "published"
    assert record["pdf"] == "https://doi.org/10.1609/example"


def test_neurips_provider_enriches_selected_paper_from_detail_page():
    listing = "https://proceedings.neurips.cc/paper_files/paper/2025/vol38-main-conference"
    paper = "https://proceedings.neurips.cc/paper_files/paper/2025/hash/abc-Abstract-Conference.html"
    session = FakeSession(
        {
            listing: """
              <li><div class="paper-content">
                <a title="paper title" href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">HydroCast</a>
                <span class="paper-authors">Alice, Bob</span>
              </div></li>
            """,
            paper: """
              <meta name="citation_pdf_url" content="/paper_files/paper/2025/file/abc-Paper-Conference.pdf">
              <h1 class="paper-title">HydroCast</h1>
              <p class="paper-abstract">A streamflow forecasting model.</p>
              <a href="https://github.com/example/hydrocast">Code</a>
            """,
        }
    )
    provider = NeurIPSProceedingsProvider(
        {"provider": "neurips", "venue_id": "NeurIPS2025", "url": listing},
        session=session,
    )
    capabilities = provider.discover_venue()
    record = list(provider.iter_submission_pages(capabilities))[0].papers[0]
    enriched, replies = provider.fetch_forum(record["forum_id"], capabilities)
    assert replies == []
    assert enriched["abstract"] == "A streamflow forecasting model."
    assert enriched["pdf"].endswith("abc-Paper-Conference.pdf")
    assert enriched["code_url"] == "https://github.com/example/hydrocast"


def test_publication_catalog_prompt_omits_review_sections():
    from conference import _briefing_prompt

    event = {
        "after_json": {
            "paper": {
                "title": "HydroCast",
                "source_provider": "cvf",
                "forum_id": "cvf:1",
            },
            "status": "published",
            "decision": "",
            "decision_text": "",
            "presentation": "",
            "camera_ready": True,
            "relevance": {},
            "review_metrics": {},
            "reviews": [],
            "meta_reviews": [],
            "author_responses": [],
        },
        "event_types_json": "[]",
        "before_json": {},
    }
    prompt = _briefing_prompt("cvf_cvpr_2026", "CVPR 2026", [event])
    assert "不要输出 Paper Decision" in prompt
    assert '"reviews"' not in prompt


def test_code_link_extraction_prefers_repository_anchor_and_filters_subpages():
    html = """
      <nav><a href="https://github.com/acl-org/acl-anthology">GitHub</a></nav>
      <a href="https://github.com/example/hydrocast/issues/1">issue</a>
      <a class="code" href="https://github.com/example/hydrocast?tab=readme">Code</a>
    """
    assert _extract_code_url_from_html(html) == "https://github.com/example/hydrocast"
    assert _extract_code_url_from_html(
        '<a href="https://gitlab.com/example/hydrocast">Code</a>'
    ) == ""
    assert _extract_code_url_from_pdf(b"not a pdf") == ""
