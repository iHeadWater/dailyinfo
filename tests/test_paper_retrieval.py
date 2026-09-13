"""Tests for shared keyword + local embedding paper retrieval."""

from datasource import Item


def _item(title: str, abstract: str = "", url: str = "") -> Item:
    return Item(
        title=title,
        date="2026-08-21",
        content=abstract,
        url=url or f"https://arxiv.org/abs/{title}",
    )


def test_keyword_filter_matches_title_or_summary_and_respects_boundaries():
    from paper_retrieval import PaperRetriever

    retriever = PaperRetriever(
        {
            "keyword_filter": {
                "enabled": True,
                "mode": "any",
                "match_fields": ["title", "summary"],
                "keywords": ["water", "timeseries|time series"],
            }
        }
    )
    selected = retriever.filter(
        [
            _item("Water forecasting"),
            _item("Image watermarking"),
            _item("River model", "A time series prediction method"),
        ]
    )

    assert [item.title for item in selected.selected] == [
        "Water forecasting",
        "River model",
    ]
    assert selected.keyword_count == 2


def test_keyword_all_mode_requires_every_configured_keyword():
    from paper_retrieval import PaperRetriever

    retriever = PaperRetriever(
        {
            "keyword_filter": {
                "enabled": True,
                "mode": "all",
                "match_fields": ["title"],
                "keywords": ["hydrology", "forecasting"],
            }
        }
    )
    selected = retriever.filter(
        [_item("Hydrology forecasting"), _item("Hydrology"), _item("Forecasting")]
    )
    assert [item.title for item in selected.selected] == ["Hydrology forecasting"]


def test_union_retrieval_keeps_embedding_only_hits():
    from paper_retrieval import PaperRetriever

    class FakeEmbedding:
        def score_papers(self, papers):
            return [0.2, 0.91]

    retriever = PaperRetriever(
        {
            "keyword_filter": {
                "enabled": True,
                "mode": "any",
                "match_fields": ["title"],
                "keywords": ["hydrology"],
            },
            "retrieval": {
                "strategy": "lexical_embedding_union",
                "threshold": 0.8,
                "dimension": 32,
            },
        },
        embedding_client=FakeEmbedding(),
    )
    selected = retriever.filter(
        [_item("Computer vision"), _item("River representation learning")]
    )
    assert [item.title for item in selected.selected] == [
        "River representation learning"
    ]
    assert selected.embedding_count == 1
    assert selected.selected[0].extra["retrieval"]["categories"] == ["embedding"]


def test_disabling_keyword_filter_restores_unfiltered_behavior():
    """Without an embedding strategy, disabling keywords means no filtering."""

    from paper_retrieval import PaperRetriever

    retriever = PaperRetriever(
        {
            "keyword_filter": {"enabled": False, "keywords": ["hydrology"]},
            "retrieval": {"strategy": "lexical"},
        }
    )
    items = [_item("Any paper"), _item("Another paper")]
    assert retriever.filter(items).selected == items


def test_disabling_keyword_filter_keeps_an_explicit_embedding_strategy():
    """`enabled: false` must not silently degrade into an unfiltered firehose.

    Asking for a semantic-only strategy while turning the keyword channel off
    is a reasonable configuration; it has to keep filtering on cosine score.
    """

    from paper_retrieval import PaperRetriever

    class FakeEmbedding:
        def score_papers(self, papers):
            return [0.91 if "river" in p["title"].casefold() else 0.02 for p in papers]

    retriever = PaperRetriever(
        {
            "keyword_filter": {"enabled": False, "keywords": ["hydrology"]},
            "retrieval": {
                "strategy": "qwen3_embedding",
                "threshold": 0.45,
                "dimension": 32,
            },
        },
        embedding_client=FakeEmbedding(),
    )
    result = retriever.filter([_item("Unrelated cooking paper"), _item("River routing")])

    assert [item.title for item in result.selected] == ["River routing"]
    assert result.embedding_count == 1


def test_exclude_phrases_veto_embedding_only_hits():
    from paper_retrieval import PaperRetriever

    class FakeEmbedding:
        def score_papers(self, papers):
            return [0.99] * len(papers)

    retriever = PaperRetriever(
        {
            "keyword_filter": {
                "enabled": True,
                "mode": "any",
                "match_fields": ["title", "summary"],
                "keywords": ["hydrology"],
            },
            # Mirrors config/sources.json: both forms are listed because word
            # boundaries stop "watermark" from matching "watermarking".
            "filters": {"exclude_phrases": ["watermark", "watermarking"]},
            "retrieval": {
                "strategy": "lexical_embedding_union",
                "threshold": 0.45,
                "dimension": 32,
            },
        },
        embedding_client=FakeEmbedding(),
    )
    selected = retriever.filter(
        [_item("Robust watermarking for LLMs"), _item("Hydrology forecasting")]
    )

    assert [item.title for item in selected.selected] == ["Hydrology forecasting"]


def test_deduplicate_papers_prefers_first_source_and_arxiv_id():
    from paper_retrieval import deduplicate_papers

    seen = set()
    selected = deduplicate_papers(
        [
            _item("First", url="https://arxiv.org/abs/2401.12345"),
            _item("Same paper", url="https://arxiv.org/pdf/2401.12345.pdf"),
            _item("Same title"),
            _item("Same title"),
        ],
        seen,
    )
    assert [item.title for item in selected] == ["First", "Same title"]
