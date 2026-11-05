"""Pydantic request/response models and tag-name normalisation rules."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TAGS_PER_POST = 20
MAX_TAG_LENGTH = 50

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
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f"tag names must be at most {MAX_TAG_LENGTH} characters")
        if name not in seen:
            seen.add(name)
            result.append(name)
    if len(result) > MAX_TAGS_PER_POST:
        raise ValueError(f"a post can have at most {MAX_TAGS_PER_POST} tags")
    return result


class PostCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=255, examples=["Searching blogs with Elasticsearch"])
    content: str = Field(..., min_length=1, examples=["Full-text search made simple..."])
    author: str = Field(..., min_length=1, max_length=100, examples=["tai"])
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

    title: Optional[str] = Field(None, min_length=1, max_length=255)
    content: Optional[str] = Field(None, min_length=1)
    author: Optional[str] = Field(None, min_length=1, max_length=100)
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
    model_config = ConfigDict(str_strip_whitespace=True)

    description: Optional[str] = Field(None, max_length=255)


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    services: dict[str, str]
