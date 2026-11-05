from __future__ import annotations

from typing import Annotated, Optional

from elasticsearch import Elasticsearch
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import search
from app.config import settings
from app.deps import get_es
from app.routers.posts import PageParam, SizeParam, page_count
from app.schemas import SearchHit, SearchResponse, SortOption, normalize_tag

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/", response_model=SearchResponse)
def search_posts(
    query: Annotated[
        Optional[str],
        Query(max_length=500, description="Full-text query over title (boosted) and content; omit to browse"),
    ] = None,
    author: Annotated[Optional[str], Query(min_length=1, max_length=100, description="Exact author")] = None,
    tag: Annotated[list[str], Query(description="Filter by tag; repeat the parameter to require every tag")] = [],
    sort: Annotated[SortOption, Query(description="relevance (default; falls back to newest without a query), newest or oldest")] = "relevance",
    page: PageParam = 1,
    size: SizeParam = 10,
    es: Elasticsearch = Depends(get_es),
) -> SearchResponse:
    text = query.strip() if query else ""
    tags: list[str] = []
    for raw in tag:
        name = normalize_tag(raw)
        if name and name not in tags:
            tags.append(name)
    if page * size > search.MAX_RESULT_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page * size cannot exceed {search.MAX_RESULT_WINDOW}",
        )
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
