"""Pure data models for the DailyInfo Publication Contract v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date, datetime
from typing import List, Optional


SCHEMA_VERSION = 1
CANONICAL_CATEGORIES = ("papers", "ai_news", "code", "resource", "arxiv")


class PublicationValidationError(ValueError):
    """Raised when a publication does not satisfy the canonical contract."""


class IdentityConflictError(PublicationValidationError):
    """Raised when an existing identity is presented under another category."""


@dataclass
class SourceMetadata:
    name: str
    url: str
    external_id: Optional[str] = None


@dataclass
class Item:
    schema_version: int
    id: str
    category: str
    title: str
    source: SourceMetadata
    authors: List[str]
    source_published_at: Optional[datetime]
    retrieved_at: datetime
    published_at: datetime
    updated_at: Optional[datetime]
    summary: str
    why_it_matters: Optional[str]
    tags: List[str]
    language: str
    briefing_ids: List[str] = field(default_factory=list)


@dataclass
class Briefing:
    schema_version: int
    id: str
    category: str
    date: Date
    title: str
    generated_at: datetime
    published_at: datetime
    updated_at: Optional[datetime]
    item_ids: List[str]
    body: str


@dataclass
class PublicationBundle:
    schema_version: int
    briefing: Briefing
    items: List[Item]
