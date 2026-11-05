"""Elasticsearch integration: index definition, document shape, write-through and queries.

Every function takes the client and index name explicitly so the same code serves the
API, the ``migrate.py`` CLI and the tests.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Optional

from elasticsearch import ApiError, Elasticsearch, TransportError

logger = logging.getLogger(__name__)

MAX_RESULT_WINDOW = 10_000  # Elasticsearch's default index.max_result_window
BULK_CHUNK_SIZE = 500
SORT_OPTIONS = ("relevance", "newest", "oldest")

INDEX_SETTINGS: dict[str, Any] = {"number_of_shards": 1, "number_of_replicas": 0}
INDEX_MAPPINGS: dict[str, Any] = {
    "properties": {
        "id": {"type": "integer"},
        "title": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
        },
        "content": {"type": "text"},
        "author": {"type": "keyword"},
        "tags": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}


class SearchBackendError(RuntimeError):
    """Elasticsearch could not be reached or rejected the request."""


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except (ApiError, TransportError) as exc:
        raise SearchBackendError(str(exc)) from exc


def _body(response: Any) -> Any:
    """Unwrap an ``ObjectApiResponse`` (or pass a plain dict through)."""
    return getattr(response, "body", response)


def post_document(post: Any) -> dict[str, Any]:
    """The document indexed for a ``BlogPost``; mirrors ``schemas.PostOut``."""
    return {
        "id": post.id,
        "title": post.title,
        "content": post.content,
        "author": post.author,
        "tags": list(post.tags or []),
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat(),
    }


def ensure_index(es: Elasticsearch, index: str) -> bool:
    """Create ``index`` with the current mapping if it is missing. Returns True if created."""
    with _translate_errors():
        if es.indices.exists(index=index):
            return False
        es.indices.create(index=index, mappings=INDEX_MAPPINGS, settings=INDEX_SETTINGS)
    logger.info("Created Elasticsearch index %r", index)
    return True


def delete_index(es: Elasticsearch, index: str) -> bool:
    """Delete ``index`` if it exists. Returns True if something was deleted."""
    with _translate_errors():
        if not es.indices.exists(index=index):
            return False
        es.indices.delete(index=index)
    logger.info("Deleted Elasticsearch index %r", index)
    return True


def mapping_problems(es: Elasticsearch, index: str) -> list[str]:
    """Differences between the live mapping and ``INDEX_MAPPINGS`` (empty when compatible)."""
    with _translate_errors():
        response = _body(es.indices.get_mapping(index=index))
    live = next(iter(response.values()))["mappings"].get("properties", {})
    problems: list[str] = []
    for name, expected in INDEX_MAPPINGS["properties"].items():
        actual = live.get(name)
        if actual is None:
            problems.append(f"field {name!r} is missing")
        elif actual.get("type") != expected["type"]:
            problems.append(
                f"field {name!r} is mapped as {actual.get('type')!r}, expected {expected['type']!r}"
            )
    return problems


def index_post(es: Elasticsearch, index: str, post: Any) -> None:
    """Index (or re-index) one post and wait until it is searchable."""
    with _translate_errors():
        es.index(index=index, id=post.id, document=post_document(post), refresh="wait_for")


def remove_post(es: Elasticsearch, index: str, post_id: int) -> None:
    """Delete one post's document; a missing document is not an error."""
    with _translate_errors():
        es.options(ignore_status=404).delete(index=index, id=post_id, refresh="wait_for")


def reindex_posts(es: Elasticsearch, index: str, posts: Iterable[Any]) -> int:
    """Bulk-index ``posts`` in chunks. Returns the number of documents indexed."""
    indexed = 0
    chunk: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal indexed
        if not chunk:
            return
        with _translate_errors():
            response = _body(es.bulk(operations=chunk, refresh="wait_for"))
        if response.get("errors"):
            failed = [item for item in response.get("items", []) if "error" in item.get("index", {})]
            raise SearchBackendError(
                f"{len(failed)} document(s) failed to index; first error: {failed[0] if failed else response}"
            )
        indexed += len(chunk) // 2
        chunk.clear()

    for post in posts:
        chunk.append({"index": {"_index": index, "_id": post.id}})
        chunk.append(post_document(post))
        if len(chunk) >= BULK_CHUNK_SIZE * 2:
            flush()
    flush()
    return indexed


def build_search_query(
    *,
    query: Optional[str],
    author: Optional[str],
    tags: list[str],
    sort: str,
    page: int,
    size: int,
) -> dict[str, Any]:
    """Build the keyword arguments for ``Elasticsearch.search``. Pure, so it is unit-testable."""
    if sort not in SORT_OPTIONS:
        raise ValueError(f"sort must be one of {SORT_OPTIONS}, got {sort!r}")
    if page < 1 or size < 1:
        raise ValueError("page and size must be >= 1")

    must: dict[str, Any] = (
        {"multi_match": {"query": query, "fields": ["title^2", "content"]}}
        if query
        else {"match_all": {}}
    )
    bool_query: dict[str, Any] = {"must": [must]}
    filters: list[dict[str, Any]] = []
    if author:
        filters.append({"term": {"author": author}})
    for tag in tags:
        filters.append({"term": {"tags": tag}})
    if filters:
        bool_query["filter"] = filters

    effective_sort = "newest" if sort == "relevance" and not query else sort
    sort_clause = {
        "relevance": [{"_score": {"order": "desc"}}, {"created_at": {"order": "desc"}}, {"id": {"order": "desc"}}],
        "newest": [{"created_at": {"order": "desc"}}, {"id": {"order": "desc"}}],
        "oldest": [{"created_at": {"order": "asc"}}, {"id": {"order": "asc"}}],
    }[effective_sort]

    params: dict[str, Any] = {
        "query": {"bool": bool_query},
        "sort": sort_clause,
        "from_": (page - 1) * size,
        "size": size,
        "track_total_hits": True,
    }
    if query:
        params["highlight"] = {
            "fields": {
                "title": {"number_of_fragments": 0},
                "content": {"fragment_size": 150, "number_of_fragments": 3},
            }
        }
    return params


def search_posts(
    es: Elasticsearch,
    index: str,
    *,
    query: Optional[str],
    author: Optional[str],
    tags: list[str],
    sort: str,
    page: int,
    size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Run a search and return ``(hits, total)``; each hit is the document plus ``score``/``highlights``."""
    params = build_search_query(query=query, author=author, tags=tags, sort=sort, page=page, size=size)
    with _translate_errors():
        response = _body(es.search(index=index, **params))
    hits_section = response["hits"]
    total_raw = hits_section["total"]
    total = int(total_raw["value"]) if isinstance(total_raw, dict) else int(total_raw)
    results: list[dict[str, Any]] = []
    for hit in hits_section["hits"]:
        doc = dict(hit["_source"])
        doc["score"] = hit.get("_score")
        doc["highlights"] = hit.get("highlight") or {}
        results.append(doc)
    return results, total
