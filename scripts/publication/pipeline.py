"""Pipeline-facing helpers for the canonical Publication v1 boundary.

This module is deliberately small and delivery-sink agnostic.  It owns the
boundary between a heterogeneous pipeline result and the Phase 2A canonical
models, but it does not fetch data, call an LLM, or write files itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .adapters import PublicationItemInput, StructuredPublicationAdapter


class StructuredResultError(ValueError):
    """Raised when an LLM response cannot be correlated to its input items."""


@dataclass(frozen=True)
class StructuredItemResult:
    """One explicit source-ref-correlated enrichment result.

    ``raw_item`` remains the source-of-truth for source facts.  Only the
    explicitly returned enrichment fields are carried from the model.
    """

    source_ref: str
    source_name: str
    raw_item: Any
    summary: str
    why_it_matters: str | None = None
    tags: list[str] = field(default_factory=list)
    section: str | None = None
    display_title: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def source_ref(index: int) -> str:
    """Return a deterministic, batch-local correlation key."""

    if index < 0:
        raise ValueError("source-ref index must be non-negative")
    return f"item-{index + 1:04d}"


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) < 3:
            raise StructuredResultError("structured response has an empty code fence")
        language = lines[0][3:].strip().lower()
        if language and language not in {"json", "javascript"}:
            raise StructuredResultError(
                "structured response must be JSON, not a Markdown code block"
            )
        return "\n".join(lines[1:-1]).strip()
    return text


def parse_structured_response(
    raw: str, expected_refs: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Parse and validate the minimal JSON response contract.

    The parser intentionally accepts no Markdown fallback and no title-based
    matching.  A response must contain exactly one object for every expected
    ``source_ref``.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise StructuredResultError("structured response is empty")
    try:
        payload = json.loads(_strip_json_fence(raw))
    except (json.JSONDecodeError, StructuredResultError) as exc:
        if isinstance(exc, StructuredResultError):
            raise
        raise StructuredResultError("structured response is not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise StructuredResultError(
            "structured response must be an object with items[]"
        )

    expected = list(expected_refs)
    expected_set = set(expected)
    if len(expected_set) != len(expected):
        raise StructuredResultError("expected source refs must be unique")

    result: dict[str, dict[str, Any]] = {}
    for entry in payload["items"]:
        if not isinstance(entry, Mapping):
            raise StructuredResultError("each structured item must be an object")
        ref = entry.get("source_ref")
        if not isinstance(ref, str) or ref not in expected_set:
            raise StructuredResultError(f"unknown structured source_ref: {ref!r}")
        if ref in result:
            raise StructuredResultError(f"duplicate structured source_ref: {ref}")
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise StructuredResultError(f"missing summary for source_ref: {ref}")
        why = entry.get("why_it_matters")
        if why is not None and (not isinstance(why, str) or not why.strip()):
            raise StructuredResultError(
                f"why_it_matters must be non-empty text or null for {ref}"
            )
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            raise StructuredResultError(f"tags must be a list of strings for {ref}")
        result[ref] = {
            "summary": summary.strip(),
            "why_it_matters": why.strip() if isinstance(why, str) else None,
            "tags": [tag.strip() for tag in tags],
        }

    missing = expected_set - result.keys()
    if missing:
        raise StructuredResultError(
            "structured response is missing source_ref(s): "
            + ", ".join(sorted(missing))
        )
    return result


def structured_prompt(
    base_prompt: str, entries: str, expected_refs: Iterable[str]
) -> str:
    """Append the strict structured-output contract to an existing prompt."""

    refs = ", ".join(expected_refs)
    return (
        f"{base_prompt}\n\n"
        f"INPUT ITEMS WITH CORRELATION KEYS:\n{entries}\n\n"
        "IMPORTANT OUTPUT CONTRACT (overrides any earlier Markdown formatting):\n"
        "Return ONLY one valid JSON object, with no prose and no Markdown, "
        "in this shape:\n"
        '{"items":[{"source_ref":"item-0001","summary":"...",'
        '"why_it_matters":null,"tags":[]}]}'
        "\nEach input item must appear exactly once. Use these exact "
        "source_ref values: "
        f"{refs}. Never infer or replace a source_ref from a title or list position."
    )


def structured_entries(data_source: Any, items: list[Any]) -> tuple[str, list[str]]:
    """Format source items with explicit batch-local correlation keys."""

    refs = [source_ref(index) for index in range(len(items))]
    blocks = []
    for ref, item in zip(refs, items):
        blocks.append(f"[source_ref={ref}]\n{data_source.format_items([item])}")
    return "\n\n".join(blocks), refs


def results_from_response(
    raw: str,
    items: list[Any],
    *,
    retrieved_at: datetime | list[datetime],
    source_name: str | None = None,
    source_names: list[str] | None = None,
    sections: Mapping[str, str] | None = None,
    display_titles: Mapping[str, str] | None = None,
) -> list[StructuredItemResult]:
    """Join validated model output to source objects by ``source_ref`` only."""

    refs = [source_ref(index) for index in range(len(items))]
    if source_names is None:
        if source_name is None:
            raise ValueError("source_name or source_names is required")
        source_names = [source_name] * len(items)
    if len(source_names) != len(items):
        raise ValueError("source_names must match items")
    retrieved_times = (
        [retrieved_at] * len(items)
        if isinstance(retrieved_at, datetime)
        else list(retrieved_at)
    )
    if len(retrieved_times) != len(items):
        raise ValueError("retrieved_at values must match items")
    parsed = parse_structured_response(raw, refs)
    return [
        StructuredItemResult(
            source_ref=ref,
            source_name=item_source_name,
            raw_item=item,
            summary=parsed[ref]["summary"],
            why_it_matters=parsed[ref]["why_it_matters"],
            tags=parsed[ref]["tags"],
            section=sections.get(ref) if sections else None,
            display_title=display_titles.get(ref) if display_titles else None,
            retrieved_at=item_retrieved_at,
        )
        for ref, item, item_source_name, item_retrieved_at in zip(
            refs, items, source_names, retrieved_times
        )
    ]


class PublicationRunCollector:
    """Collect one category's structured results before one canonical finalize."""

    def __init__(self, category: str) -> None:
        self.category = category
        self.results: list[StructuredItemResult] = []
        self.failures: list[str] = []
        self._body_parts: list[str] = []
        self._pending_seen: list[tuple[Any, list[Any]]] = []

    def add_failure(self, message: str) -> None:
        self.failures.append(message)

    def add(self, results: Iterable[StructuredItemResult]) -> None:
        for result in results:
            # Do not silently deduplicate here.  Filtering/dedup belongs to
            # the source pipeline; a duplicate that reaches finalization must
            # fail closed instead of disappearing from the publication.
            self.results.append(result)

    def add_body(self, body: str) -> None:
        if body.strip():
            self._body_parts.append(body.strip())

    def defer_seen(self, data_source: Any, items: list[Any]) -> None:
        """Defer source dedup-state mutation until canonical persistence succeeds."""

        if items:
            self._pending_seen.append((data_source, list(items)))

    def commit_deferred_seen(self) -> None:
        for data_source, items in self._pending_seen:
            data_source.commit_seen(items)
        self._pending_seen.clear()

    @property
    def body(self) -> str:
        return "\n\n".join(self._body_parts)

    def item_inputs(self, *, published_at: datetime) -> list[PublicationItemInput]:
        inputs = []
        for result in self.results:
            raw = result.raw_item
            overrides = {
                "summary": result.summary,
                "why_it_matters": result.why_it_matters,
                "tags": result.tags,
            }
            if result.display_title is not None:
                overrides["title"] = result.display_title
            inputs.append(
                StructuredPublicationAdapter.item_from_pipeline(
                    raw,
                    source_name=result.source_name,
                    retrieved_at=result.retrieved_at,
                    published_at=published_at,
                    language="zh-CN",
                    **overrides,
                )
            )
        return inputs


def now_utc() -> datetime:
    """Return an aware UTC timestamp at a pipeline boundary."""

    return datetime.now(UTC)


def _read(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _extra(raw: Any) -> Mapping[str, Any]:
    value = _read(raw, "extra", {})
    return value if isinstance(value, Mapping) else {}
