"""Reusable local retrieval for RSS/API paper sources.

The conference pipeline owns the durable OpenReview checkpoint state.  This
module deliberately only deals with item-level retrieval so arXiv and future
paper sources can use the same keyword + Qwen embedding union without sharing
the conference database schema.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from datasource import Item
from embedding_retrieval import (
    EmbeddingRetrievalConfig,
    LlamaCppEmbeddingClient,
    QwenEmbeddingClient,
)
from text_match import normalise_text as _normalise
from text_match import phrase_matches as _phrase_matches


DEFAULT_KEYWORDS = [
    "hydrology",
    "hydrological",
    "flood",
    "flooding",
    "flood forecasting",
    "precipitation",
    "rainfall",
    "runoff",
    "streamflow",
    "discharge",
    "groundwater",
    "aquifer",
    "evapotranspiration",
    "water resource",
    "water management",
    "watershed",
    "catchment",
    "river basin",
    "snow",
    "ice",
    "glacier",
    "LSTM",
    "GRU",
    "RNN",
    "Transformer",
    "attention mechanism",
    "deep learning",
    "neural network",
    "machine learning",
    "time series|timeseries",
    "forecasting",
    "prediction",
    "data assimilation",
    "ensemble",
    "foundation model",
    "large model",
    "pre-trained|pretrained",
    "digital twin",
    "surrogate model",
    "physics-informed|physics informed",
    "PINN",
    "knowledge-guided|knowledge guided",
    "graph neural network",
    "GNN",
    "earth science",
    "geoscience",
    "climate change",
    "climate model",
    "global warming",
    "weather forecast",
    "NWP",
    "meteorology",
    "remote sensing",
    "satellite",
    "radar",
    "LiDAR",
    "GIS",
    "geospatial",
    "spatial analysis",
]


def _field_text(item: Item, field: str) -> str:
    field = field.casefold()
    if field == "title":
        return item.title
    if field in {"summary", "abstract", "content"}:
        return item.content
    if field in {"keywords", "keyword"}:
        values = item.extra.get("keywords", [])
        if isinstance(values, (list, tuple)):
            return " ".join(str(value) for value in values)
        return str(values or "")
    return str(item.extra.get(field, "") or "")


def _match_text(item: Item, config: dict) -> str:
    keyword_cfg = config.get("keyword_filter", {}) or {}
    fields = keyword_cfg.get("match_fields", ["title", "summary"])
    return _normalise(" ".join(_field_text(item, field) for field in fields))


def excluded_by_filters(item: Item, config: dict) -> bool:
    """Return True when an item matches a configured ``exclude_phrases`` entry.

    Exclusion is a hard veto and must be evaluated independently of the
    keyword channel: an embedding-only hit is still a retrieval hit, so
    folding exclusion into the keyword result would let a semantically
    similar paper re-enter the briefing through the union.
    """

    text = _match_text(item, config)
    for expression in (config.get("filters", {}) or {}).get("exclude_phrases", []):
        if any(
            _phrase_matches(text, alternative.strip())
            for alternative in str(expression).split("|")
        ):
            return True
    return False


def matched_keywords(item: Item, config: dict) -> list[str]:
    """Return configured keyword alternatives that occur in an item."""

    if excluded_by_filters(item, config):
        return []
    keyword_cfg = config.get("keyword_filter", {}) or {}
    text = _match_text(item, config)
    matches: list[str] = []
    for expression in keyword_cfg.get("keywords", DEFAULT_KEYWORDS):
        alternatives = [part.strip() for part in str(expression).split("|")]
        if any(_phrase_matches(text, alternative) for alternative in alternatives):
            matches.append(str(expression))
    return matches


def _paper_dict(item: Item) -> dict:
    return {
        "title": item.title,
        "abstract": item.content,
        "keywords": item.extra.get("keywords", []),
    }


@dataclass(frozen=True)
class RetrievalResult:
    selected: list[Item]
    keyword_matches: dict[str, list[str]]
    embedding_scores: dict[str, float]
    keyword_count: int
    embedding_count: int


class PaperRetriever:
    """Apply configurable keyword and embedding retrieval to :class:`Item`s."""

    def __init__(
        self,
        config: dict,
        *,
        embedding_client: Any | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.keyword_config = config.get("keyword_filter", {}) or {}
        self.keyword_enabled = bool(self.keyword_config.get("enabled", True))
        retrieval = config.get("retrieval", {}) or {}
        self.strategy = str(retrieval.get("strategy", "lexical"))
        if self.strategy not in {"lexical", "qwen3_embedding", "lexical_embedding_union"}:
            raise ValueError(f"unsupported paper retrieval strategy: {self.strategy}")
        self.use_embedding = self.strategy in {"qwen3_embedding", "lexical_embedding_union"}
        # Disabling the keyword channel restores the original unfiltered source
        # behaviour only when no embedding strategy was asked for. An explicit
        # embedding strategy is honoured on its own: silently dropping it would
        # turn "semantic retrieval, no keywords" into an unfiltered firehose.
        if self.keyword_config and not self.keyword_enabled and not self.use_embedding:
            self.strategy = "none"
        self.embedding_config = (
            EmbeddingRetrievalConfig.from_source(config) if self.use_embedding else None
        )
        self.embedding_client = embedding_client
        if self.use_embedding and self.embedding_client is None:
            assert self.embedding_config is not None
            if self.embedding_config.backend == "llama_cpp":
                self.embedding_client = LlamaCppEmbeddingClient(self.embedding_config)
            elif self.embedding_config.backend in {"qwen_fastapi", "transformers"}:
                self.embedding_client = QwenEmbeddingClient(self.embedding_config)
            else:
                raise ValueError(
                    f"unsupported embedding backend: {self.embedding_config.backend}"
                )

    @property
    def enabled(self) -> bool:
        return self.strategy != "none" and (
            self.keyword_enabled or self.use_embedding
        )

    def filter(self, items: list[Item]) -> RetrievalResult:
        if not items:
            return RetrievalResult([], {}, {}, 0, 0)
        if not self.enabled:
            return RetrievalResult(items, {}, {}, 0, 0)

        excluded_keys = {
            item.url or f"title:{item.title}"
            for item in items
            if excluded_by_filters(item, self.config)
        }
        keyword_matches_by_url = {
            item.url or f"title:{item.title}": matched_keywords(item, self.config)
            for item in items
        }
        mode = str(self.keyword_config.get("mode", "any")).casefold()
        if mode not in {"any", "all"}:
            raise ValueError("keyword_filter.mode must be 'any' or 'all'")
        configured_count = len(self.keyword_config.get("keywords", DEFAULT_KEYWORDS))
        keyword_hits = {
            key: values
            for key, values in keyword_matches_by_url.items()
            if (
                len(values) == configured_count
                if mode == "all"
                else bool(values)
            )
        }

        embedding_scores: dict[str, float] = {}
        if self.use_embedding:
            assert self.embedding_client is not None
            assert self.embedding_config is not None
            if self.logger:
                self.logger(
                    f"[EMBEDDING] scoring {len(items)} papers "
                    f"dimension={self.embedding_config.dimension} "
                    f"threshold={self.embedding_config.threshold:.3f} "
                    f"text_mode={self.embedding_config.text_mode} "
                    f"backend={self.embedding_config.backend}"
                )
            scores = self.embedding_client.score_papers([_paper_dict(item) for item in items])
            if len(scores) != len(items):
                raise RuntimeError("embedding retriever returned a mismatched number of scores")
            embedding_scores = {
                item.url or f"title:{item.title}": float(score)
                for item, score in zip(items, scores, strict=True)
            }

        selected: list[Item] = []
        embedding_count = 0
        for item in items:
            key = item.url or f"title:{item.title}"
            if key in excluded_keys:
                # exclude_phrases is a hard veto over every retrieval channel.
                continue
            keyword_hit = key in keyword_hits
            embedding_hit = bool(
                self.use_embedding
                and embedding_scores.get(key, -1.0)
                >= self.embedding_config.threshold  # type: ignore[union-attr]
            )
            if keyword_hit or embedding_hit:
                item.extra.setdefault("retrieval", {})
                item.extra["retrieval"].update(
                    {
                        "categories": [
                            name
                            for name, hit in (
                                ("keyword", keyword_hit),
                                ("embedding", embedding_hit),
                            )
                            if hit
                        ],
                        "keyword_matches": keyword_matches_by_url.get(key, []),
                        "embedding_score": embedding_scores.get(key),
                    }
                )
                selected.append(item)
                embedding_count += int(embedding_hit)

        return RetrievalResult(
            selected,
            keyword_matches_by_url,
            embedding_scores,
            len(keyword_hits),
            embedding_count,
        )


def arxiv_identity(item: Item) -> str:
    """Return a stable cross-source identity, preferring the arXiv ID."""

    url = (item.url or "").lower()
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#/]+)", url)
    if match:
        identifier = re.sub(r"\.pdf$", "", match.group(1))
        return re.sub(r"v\d+$", "", identifier)
    title = _normalise(item.title)
    return re.sub(r"[^\w]+", " ", title).strip()


def deduplicate_papers(items: list[Item], seen: set[str]) -> list[Item]:
    """Drop duplicate arXiv papers while preserving source priority/order."""

    result: list[Item] = []
    for item in items:
        key = arxiv_identity(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
