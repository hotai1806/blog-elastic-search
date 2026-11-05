from __future__ import annotations

import math
from typing import Annotated, Optional

import redis
from elasticsearch import Elasticsearch
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app import cache
from app.config import settings
from app.database import get_db
from app.deps import get_es, get_redis
from app.models import BlogPost
from app.schemas import PostCreate, PostList, PostOut, PostPatch, PostUpdate, parse_tag_filters
from app.services import posts as posts_service

router = APIRouter(prefix="/posts", tags=["posts"])

PageParam = Annotated[int, Query(ge=1, description="1-based page number")]
SizeParam = Annotated[int, Query(ge=1, le=100, description="Items per page")]
TagFilterParam = Annotated[
    list[str],
    Query(description="Filter by tag; repeat the parameter to require every tag"),
]


def page_count(total: int, size: int) -> int:
    return math.ceil(total / size) if total > 0 else 0


def tag_filters(raw: list[str]) -> list[str]:
    """Normalise ``tag`` query values; a blank value is a validation error (422)."""
    try:
        return parse_tag_filters(raw)
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "tag"), "msg": str(exc), "input": raw}]
        ) from None


def load_post_or_404(db: Session, post_id: int, *, for_update: bool = False) -> BlogPost:
    post = posts_service.get_post(db, post_id, for_update=for_update)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


@router.post("/", response_model=PostOut, status_code=status.HTTP_201_CREATED)
def create_post(
    data: PostCreate,
    db: Session = Depends(get_db),
    es: Elasticsearch = Depends(get_es),
) -> PostOut:
    post = posts_service.create_post(db, es, settings.elasticsearch_index, data)
    return PostOut.model_validate(post)


@router.get("/", response_model=PostList)
def list_posts(
    page: PageParam = 1,
    size: SizeParam = 10,
    author: Annotated[Optional[str], Query(min_length=1, max_length=100)] = None,
    tag: TagFilterParam = [],
    db: Session = Depends(get_db),
) -> PostList:
    items, total = posts_service.list_posts(
        db, page=page, size=size, author=author, tags=tag_filters(tag)
    )
    return PostList(
        items=[PostOut.model_validate(post) for post in items],
        total=total,
        page=page,
        size=size,
        pages=page_count(total, size),
    )


@router.get("/{post_id}", response_model=PostOut)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> PostOut:
    cached = cache.get_cached_post(redis_client, post_id)
    if cached is not None:
        return cached
    post = PostOut.model_validate(load_post_or_404(db, post_id))
    cache.cache_post(redis_client, post, settings.cache_ttl_seconds, only_if_absent=True)
    return post


@router.put("/{post_id}", response_model=PostOut)
def replace_post(
    post_id: int,
    data: PostUpdate,
    db: Session = Depends(get_db),
    es: Elasticsearch = Depends(get_es),
    redis_client: redis.Redis = Depends(get_redis),
) -> PostOut:
    post = load_post_or_404(db, post_id, for_update=True)
    post = posts_service.replace_post(db, es, settings.elasticsearch_index, post, data)
    result = PostOut.model_validate(post)
    cache.cache_post(redis_client, result, settings.cache_ttl_seconds)
    return result


@router.patch("/{post_id}", response_model=PostOut)
def patch_post(
    post_id: int,
    data: PostPatch,
    db: Session = Depends(get_db),
    es: Elasticsearch = Depends(get_es),
    redis_client: redis.Redis = Depends(get_redis),
) -> PostOut:
    post = load_post_or_404(db, post_id, for_update=True)
    post = posts_service.patch_post(db, es, settings.elasticsearch_index, post, data)
    result = PostOut.model_validate(post)
    cache.cache_post(redis_client, result, settings.cache_ttl_seconds)
    return result


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    es: Elasticsearch = Depends(get_es),
    redis_client: redis.Redis = Depends(get_redis),
) -> Response:
    post = load_post_or_404(db, post_id, for_update=True)
    posts_service.delete_post(db, es, settings.elasticsearch_index, post)
    cache.mark_post_deleted(redis_client, post_id, settings.cache_ttl_seconds)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
