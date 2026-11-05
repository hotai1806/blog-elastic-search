from __future__ import annotations

from typing import Annotated, Optional

from elasticsearch import Elasticsearch
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from app import search
from app.config import settings
from app.deps import get_es
from app.routers.posts import AuthorFilterParam, PageParam, SizeParam, TagFilterParam, page_count, tag_filters
from app.schemas import SearchHit, SearchResponse, SortOption

router = APIRouter(prefix="/search", tags=["search"])


def check_result_window(page: int, size: int) -> None:
    if page * size > search.MAX_RESULT_WINDOW:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "page"),
                    "msg": f"page * size cannot exceed {search.MAX_RESULT_WINDOW}",
                    "input": {"page": page, "size": size},
                }
            ]
        )


@router.get("/", response_model=SearchResponse)
def search_posts(
    query: Annotated[
        Optional[str],
        Query(max_length=500, description="Full-text query over title (boosted) and content; omit to browse"),
    ] = None,
    author: AuthorFilterParam = None,
    tag: TagFilterParam = [],
    sort: Annotated[SortOption, Query(description="relevance (default; falls back to newest without a query), newest or oldest")] = "relevance",
    page: PageParam = 1,
    size: SizeParam = 10,
    es: Elasticsearch = Depends(get_es),
) -> SearchResponse:
    text = query.strip() if query else ""
    tags = tag_filters(tag)
    check_result_window(page, size)
    hits, total = search.search_posts(
        es,
        settings.elasticsearch_index,
        query=text or None,
        author=author,
        tags=tags,
        sort=sort,
        page=page,
        size=size,
    )
    return SearchResponse(
        results=[SearchHit.model_validate(hit) for hit in hits],
        total=total,
        page=page,
        size=size,
        pages=page_count(total, size),
        query=text or None,
    )
