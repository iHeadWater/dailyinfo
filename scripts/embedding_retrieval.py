"""Embedding retrieval clients and paper formatting helpers.

The OpenReview pipeline uses :class:`LlamaCppEmbeddingClient` by default.
``QwenEmbeddingClient`` remains available as a migration-time adapter for the
previous Transformers/FastAPI process and benchmark scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import requests


DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
DOMAIN_SPECIFIC_QUERY_INSTRUCTION = (
    "Given a research-interest description, retrieve machine-learning conference "
    "papers that are directly relevant to hydrology, water resources, rainfall, "
    "runoff, floods, watersheds, river flow, or transferable Earth-system prediction."
)
DEFAULT_QUERY_INSTRUCTION = (
    "Retrieve conference papers relevant to AI for hydrology and water resources."
)
DEFAULT_QUERY_TEXT = (
    "AI for hydrology and water resources research, including rainfall-runoff and "
    "streamflow forecasting, flood prediction, watershed and river modelling, "
    "precipitation, hydroclimate, remote sensing of water, physics-informed machine "
    "learning, and foundation models for Earth-system prediction."
)


class EmbeddingServiceError(RuntimeError):
    """Raised when the local embedding service cannot return valid vectors."""


def paper_text(paper: dict, mode: str = "title_abstract_keywords") -> str:
    """Build a stable retrieval document from an OpenReview paper."""

    title = str(paper.get("title", "") or "").strip()
    abstract = str(paper.get("abstract", "") or "").strip()
    keywords = [
        str(value).strip() for value in paper.get("keywords", []) if str(value).strip()
    ]
    if mode == "title_only":
        return f"Title: {title}"
    if mode == "title_abstract":
        return f"Title: {title}\nAbstract: {abstract}"
    if mode == "title_abstract_keywords":
        return f"Title: {title}\nAbstract: {abstract}\nKeywords: {', '.join(keywords)}"
    raise ValueError(f"unsupported embedding text_mode: {mode}")


def _normalise(vector: list[float], dimension: int | None = None) -> list[float]:
    values = vector[:dimension] if dimension else vector
    magnitude = math.sqrt(sum(value * value for value in values))
    if not magnitude:
        raise EmbeddingServiceError("embedding vector has zero norm")
    return [value / magnitude for value in values]


def cosine_similarity(
    left: list[float], right: list[float], dimension: int | None = None
) -> float:
    """Return cosine similarity, applying MRL truncation when requested."""

    if len(left) != len(right):
        raise ValueError(f"embedding dimensions differ: {len(left)} != {len(right)}")
    left_norm = _normalise(left, dimension)
    right_norm = _normalise(right, dimension)
    return sum(a * b for a, b in zip(left_norm, right_norm, strict=True))


@dataclass(frozen=True)
class EmbeddingRetrievalConfig:
    backend: str = "llama_cpp"
    endpoint: str = DEFAULT_ENDPOINT
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    query_text: str = DEFAULT_QUERY_TEXT
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    text_mode: str = "title_abstract"
    dimension: int = 512
    threshold: float = 0.45
    batch_size: int = 2
    max_length: int = 1024
    timeout_seconds: int = 300

    @classmethod
    def from_source(cls, source: dict) -> "EmbeddingRetrievalConfig":
        cfg = source.get("retrieval", {})
        dimension = int(cfg.get("dimension", 512))
        if dimension < 32 or dimension > 1024:
            raise ValueError("retrieval.dimension must be between 32 and 1024")
        threshold = float(cfg.get("threshold", 0.45))
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("retrieval.threshold must be between -1 and 1")
        return cls(
            backend=str(cfg.get("backend", "llama_cpp")),
            endpoint=str(cfg.get("endpoint", DEFAULT_ENDPOINT)).rstrip("/"),
            model=str(cfg.get("model", "Qwen/Qwen3-Embedding-0.6B")),
            query_text=str(cfg.get("query_text", DEFAULT_QUERY_TEXT)),
            query_instruction=str(
                cfg.get("query_instruction", DEFAULT_QUERY_INSTRUCTION)
            ),
            text_mode=str(cfg.get("text_mode", "title_abstract")),
            dimension=dimension,
            threshold=threshold,
            batch_size=max(1, int(cfg.get("batch_size", 2))),
            max_length=max(32, int(cfg.get("max_length", 1024))),
            timeout_seconds=max(1, int(cfg.get("timeout_seconds", 300))),
        )


class QwenEmbeddingClient:
    """HTTP client for the local FastAPI embedding service."""

    def __init__(self, config: EmbeddingRetrievalConfig, session: Any = None):
        self.config = config
        self.session = session or requests.Session()
        self._query_embedding: list[float] | None = None

    def _embed(
        self,
        texts: list[str],
        *,
        input_type: str,
        instruction: str = "",
        dimension: int | None = None,
        batch_size: int | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "texts": texts,
            "input_type": input_type,
            "instruction": instruction,
            "dimension": dimension or self.config.dimension,
            "batch_size": batch_size or self.config.batch_size,
            "max_length": self.config.max_length,
        }
        try:
            response = self.session.post(
                f"{self.config.endpoint}/v1/embeddings",
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            try:
                response.raise_for_status()
                data = response.json()
            finally:
                # Explicitly release the underlying socket.  This matters for
                # long discovery runs and for injected/custom Sessions where
                # connection pooling is not reliably reclaimed by GC.
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (requests.RequestException, ValueError) as exc:
            raise EmbeddingServiceError(
                f"local embedding service request failed: {exc}"
            ) from exc
        vectors = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingServiceError(
                f"embedding service returned {len(vectors or [])} vectors for {len(texts)} texts"
            )
        return [[float(value) for value in vector] for vector in vectors]

    def query_embedding(self) -> list[float]:
        if self._query_embedding is None:
            self._query_embedding = self.embed_query(
                self.config.query_text, self.config.query_instruction
            )
        return self._query_embedding

    def embed_query(
        self,
        query_text: str,
        instruction: str,
        *,
        dimension: int | None = None,
    ) -> list[float]:
        return self._embed(
            [query_text],
            input_type="query",
            instruction=instruction,
            dimension=dimension,
        )[0]

    def embed_documents(
        self,
        texts: list[str],
        *,
        dimension: int | None = None,
        batch_size: int | None = None,
    ) -> list[list[float]]:
        return self._embed(
            texts,
            input_type="document",
            dimension=dimension,
            batch_size=batch_size,
        )

    def score_papers(self, papers: list[dict]) -> list[float]:
        """Score one OpenReview page against the cached query vector."""

        query = self.query_embedding()
        documents = [paper_text(paper, self.config.text_mode) for paper in papers]
        embeddings = self.embed_documents(documents)
        return [cosine_similarity(query, vector) for vector in embeddings]


def _query_input(instruction: str, query: str) -> str:
    """Format a Qwen3 query instruction for backends without input_type."""

    return f"Instruct: {instruction.strip()}\nQuery: {query.strip()}"


class LlamaCppEmbeddingClient:
    """OpenAI-compatible embedding adapter for a llama.cpp server."""

    def __init__(self, config: EmbeddingRetrievalConfig, session: Any = None):
        self.config = config
        self.session = session or requests.Session()
        self._query_embedding: list[float] | None = None

    def _request(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self.session.post(
                f"{self.config.endpoint}/v1/embeddings",
                json={"model": self.config.model, "input": texts},
                timeout=self.config.timeout_seconds,
            )
            try:
                response.raise_for_status()
                data = response.json()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise EmbeddingServiceError(
                f"llama.cpp embedding request failed: {exc}"
            ) from exc

        records = data.get("data") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise EmbeddingServiceError(
                "llama.cpp embedding response missing data list"
            )
        try:
            ordered = sorted(records, key=lambda item: int(item.get("index", 0)))
            vectors = [item.get("embedding") for item in ordered]
        except (AttributeError, TypeError, ValueError) as exc:
            raise EmbeddingServiceError(
                "llama.cpp embedding response has invalid data records"
            ) from exc
        if len(vectors) != len(texts) or any(
            not isinstance(vector, list) for vector in vectors
        ):
            raise EmbeddingServiceError(
                f"llama.cpp returned {len(vectors)} vectors for {len(texts)} texts"
            )
        try:
            return [[float(value) for value in vector] for vector in vectors]
        except (TypeError, ValueError) as exc:
            raise EmbeddingServiceError(
                "llama.cpp embedding response contains non-numeric values"
            ) from exc

    def embed_query(self, query_text: str, instruction: str) -> list[float]:
        return self._request([_query_input(instruction, query_text)])[0]

    def query_embedding(self) -> list[float]:
        if self._query_embedding is None:
            self._query_embedding = self.embed_query(
                self.config.query_text, self.config.query_instruction
            )
        return self._query_embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch_size = max(1, self.config.batch_size)
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._request(texts[start : start + batch_size]))
        return vectors

    def score_papers(self, papers: list[dict]) -> list[float]:
        query = self.query_embedding()
        documents = [paper_text(paper, self.config.text_mode) for paper in papers]
        embeddings = self.embed_documents(documents)
        return [
            cosine_similarity(query, vector, dimension=self.config.dimension)
            for vector in embeddings
        ]
