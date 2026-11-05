"""Pydantic request/response models and tag-name normalisation rules."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TAGS_PER_POST = 20
MAX_TAG_LENGTH = 50
# Strings may not contain NUL: PostgreSQL rejects it at execution time (which would be a 500).
NO_NUL = r"^[^\x00]*$"

SortOption = Literal["relevance", "newest", "oldest"]
TagSortOption = Literal["name", "posts"]


def normalize_tag(raw: str) -> str:
    """Trim, collapse internal whitespace and lower-case a tag name."""
    return " ".join(raw.split()).lower()


def normalize_tags(raw: list[str]) -> list[str]:
    """Normalise every name, drop duplicates (keeping order) and enforce the limits."""
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        name = normalize_tag(item)
        if not name:
            raise ValueError("tag names must not be blank")
        if "\x00" in name:
            raise ValueError("tag names must not contain NUL characters")
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f"tag names must be at most {MAX_TAG_LENGTH} characters")
        if name not in seen:
            seen.add(name)
            result.append(name)
    if len(result) > MAX_TAGS_PER_POST:
        raise ValueError(f"a post can have at most {MAX_TAGS_PER_POST} tags")
    return result


def parse_tag_filters(raw: list[str]) -> list[str]:
    """Normalise ``tag`` query-parameter values (de-duplicated); blank or over-long values are a ValueError."""
    result: list[str] = []
    for item in raw:
        name = normalize_tag(item)
        if not name:
            raise ValueError("tag must not be blank")
        if "\x00" in name:
            raise ValueError("tag must not contain NUL characters")
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f"tag must be at most {MAX_TAG_LENGTH} characters")
        if name not in result:
            result.append(name)
    return result


class PostCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=255, pattern=NO_NUL, examples=["Searching blogs with Elasticsearch"])
    content: str = Field(..., min_length=1, pattern=NO_NUL, examples=["Full-text search made simple..."])
    author: str = Field(..., min_length=1, max_length=100, pattern=NO_NUL, examples=["tai"])
    tags: list[str] = Field(default_factory=list, examples=[["elasticsearch", "python"]])

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)


class PostUpdate(PostCreate):
    """Full replacement (``PUT``). Omitting ``tags`` clears the post's tags."""


class PostPatch(BaseModel):
    """Partial update (``PATCH``). Only provided, non-null fields change; ``tags`` replaces the set."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: Optional[str] = Field(None, min_length=1, max_length=255, pattern=NO_NUL)
    content: Optional[str] = Field(None, min_length=1, pattern=NO_NUL)
    author: Optional[str] = Field(None, min_length=1, max_length=100, pattern=NO_NUL)
    tags: Optional[list[str]] = None

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return None if value is None else normalize_tags(value)


class PostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    author: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Optional[list[str]]) -> list[str]:
        return [] if value is None else list(value)


class PostList(BaseModel):
    items: list[PostOut]
    total: int
    page: int
    size: int
    pages: int


class SearchHit(PostOut):
    score: Optional[float] = None
    highlights: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_legacy_documents(cls, data):
        """Documents indexed by earlier versions lack ``updated_at``; keep search working
        until ``migrate.py --sync`` has rewritten them."""
        if isinstance(data, dict) and data.get("updated_at") is None and data.get("created_at") is not None:
            data = {**data, "updated_at": data["created_at"]}
        return data


class SearchResponse(BaseModel):
    results: list[SearchHit]
    total: int
    page: int
    size: int
    pages: int
    query: Optional[str] = None


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str] = None
    created_at: datetime
    post_count: int = 0


class TagPatch(BaseModel):
    """``description`` ``null`` or blank clears it."""

    model_config = ConfigDict(str_strip_whitespace=True)

    description: Optional[str] = Field(None, max_length=255, pattern=NO_NUL)

    @field_validator("description")
    @classmethod
    def _blank_clears(cls, value: Optional[str]) -> Optional[str]:
        return value or None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    services: dict[str, str]
