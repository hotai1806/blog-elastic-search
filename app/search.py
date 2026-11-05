"""Elasticsearch integration: index definition, document shape, write-through and queries.

Every function takes the client and index name explicitly so the same code serves the
API, the ``migrate.py`` CLI and the tests.
"""
from __future__ import annotations

import logging
import weakref
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Optional

from elasticsearch import ApiError, BadRequestError, Elasticsearch, TransportError

logger = logging.getLogger(__name__)

MAX_RESULT_WINDOW = 10_000  # Elasticsearch's default index.max_result_window
BULK_CHUNK_SIZE = 500
SCAN_PAGE_SIZE = 1_000
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


# Indexes each client has verified to exist with the explicit mapping, per process.
# Writing to a missing index would otherwise let Elasticsearch auto-create it with a
# dynamic mapping (author/tags as ``text``), which silently breaks the keyword filters.
_verified_indexes: "weakref.WeakKeyDictionary[Any, set[str]]" = weakref.WeakKeyDictionary()


def forget_verified_indexes() -> None:
    """Drop the per-process memo of verified indexes (tests)."""
    _verified_indexes.clear()


def _mark_verified(es: Any, index: str) -> None:
    _verified_indexes.setdefault(es, set()).add(index)


def _describe(exc: Exception) -> str:
    """``str(exc)`` plus Elasticsearch's own reason when the response carries one."""
    body = getattr(exc, "body", None)
    reason = body.get("error", {}).get("reason") if isinstance(body, dict) else None
    return f"{exc}: {reason}" if reason else str(exc)


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except (ApiError, TransportError) as exc:
        raise SearchBackendError(_describe(exc)) from exc


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


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------


def ensure_index(es: Elasticsearch, index: str) -> bool:
    """Create ``index`` with the current mapping if it is missing. Returns True if created."""
    with _translate_errors():
        if es.indices.exists(index=index):
            _mark_verified(es, index)
            return False
        try:
            es.indices.create(index=index, mappings=INDEX_MAPPINGS, settings=INDEX_SETTINGS)
        except BadRequestError as exc:
            if "resource_already_exists_exception" not in str(exc):
                raise
            # Another process created it between our check and the create call.
            logger.info("Elasticsearch index %r was created concurrently", index)
            _mark_verified(es, index)
            return False
    _mark_verified(es, index)
    logger.info("Created Elasticsearch index %r", index)
    return True


def _ensure_verified(es: Elasticsearch, index: str) -> None:
    """Make sure the index exists with our mapping before the first write of this process."""
    if index not in _verified_indexes.get(es, ()):
        ensure_index(es, index)


def delete_index(es: Elasticsearch, index: str) -> bool:
    """Delete ``index`` if it exists. Returns True if something was deleted."""
    with _translate_errors():
        if not es.indices.exists(index=index):
            return False
        es.indices.delete(index=index)
    _verified_indexes.get(es, set()).discard(index)
    logger.info("Deleted Elasticsearch index %r", index)
    return True


def _mapping_diff(es: Elasticsearch, index: str) -> tuple[dict[str, Any], list[str]]:
    """Return ``(missing field definitions, descriptions of type conflicts)`` for ``index``."""
    with _translate_errors():
        response = _body(es.indices.get_mapping(index=index))
    live = next(iter(response.values()))["mappings"].get("properties", {})
    missing: dict[str, Any] = {}
    conflicts: list[str] = []
    for name, expected in INDEX_MAPPINGS["properties"].items():
        actual = live.get(name)
        if actual is None:
            missing[name] = expected
        elif actual.get("type") != expected["type"]:
            conflicts.append(
                f"field {name!r} is mapped as {actual.get('type')!r}, expected {expected['type']!r}"
            )
    return missing, conflicts


def mapping_problems(es: Elasticsearch, index: str) -> list[str]:
    """Differences between the live mapping and ``INDEX_MAPPINGS`` (empty when compatible)."""
    missing, conflicts = _mapping_diff(es, index)
    return [f"field {name!r} is missing" for name in missing] + conflicts


def rebuild_index(es: Elasticsearch, index: str) -> None:
    """Drop and recreate ``index`` with the current mapping. Documents are lost: re-index after."""
    for _attempt in range(2):
        delete_index(es, index)
        ensure_index(es, index)
        if not mapping_problems(es, index):
            return
        # A write slipped in between the delete and the create and auto-created the
        # index with a dynamic mapping; try once more.
        logger.warning("Elasticsearch index %r was recreated concurrently; rebuilding again", index)
    raise SearchBackendError(f"could not recreate index {index!r} with the expected mapping")


def prepare_index(es: Elasticsearch, index: str) -> str:
    """Bring ``index`` to the current mapping.

    Returns ``"created"``, ``"ok"``, ``"extended"`` (missing fields were added in place) or
    ``"rebuilt"`` (a field's type differed, which Elasticsearch cannot change, so the index
    was dropped and recreated; the caller must re-index).
    """
    if ensure_index(es, index):
        return "created"
    missing, conflicts = _mapping_diff(es, index)
    if conflicts:
        logger.warning("Rebuilding Elasticsearch index %r: %s", index, "; ".join(conflicts))
        rebuild_index(es, index)
        return "rebuilt"
    if missing:
        with _translate_errors():
            es.indices.put_mapping(index=index, properties=missing)
        logger.info("Added fields %s to Elasticsearch index %r", sorted(missing), index)
        return "extended"
    return "ok"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def index_post(es: Elasticsearch, index: str, post: Any) -> None:
    """Index (or re-index) one post and wait until it is searchable."""
    _ensure_verified(es, index)
    with _translate_errors():
        es.index(index=index, id=post.id, document=post_document(post), refresh="wait_for")


def remove_post(es: Elasticsearch, index: str, post_id: int) -> None:
    """Delete one post's document; a missing document (or index) is not an error."""
    with _translate_errors():
        es.options(ignore_status=404).delete(index=index, id=post_id, refresh="wait_for")


def _run_bulk(es: Elasticsearch, operations: list[dict[str, Any]]) -> None:
    with _translate_errors():
        response = _body(es.bulk(operations=operations, refresh="wait_for"))
    if response.get("errors"):
        failed = [
            item for item in response.get("items", [])
            if "error" in next(iter(item.values()), {})
        ]
        raise SearchBackendError(
            f"{len(failed)} bulk operation(s) failed; first error: {failed[0] if failed else response}"
        )


def reindex_posts(es: Elasticsearch, index: str, posts: Iterable[Any]) -> int:
    """Bulk-index ``posts`` in chunks. Returns the number of documents indexed."""
    _ensure_verified(es, index)
    indexed = 0
    chunk: list[dict[str, Any]] = []
    for post in posts:
        chunk.append({"index": {"_index": index, "_id": post.id}})
        chunk.append(post_document(post))
        if len(chunk) >= BULK_CHUNK_SIZE * 2:
            _run_bulk(es, chunk)
            indexed += len(chunk) // 2
            chunk = []
    if chunk:
        _run_bulk(es, chunk)
        indexed += len(chunk) // 2
    return indexed


def stale_document_ids(es: Elasticsearch, index: str, keep_ids: Iterable[Any]) -> list[str]:
    """Ids of documents in ``index`` that are not in ``keep_ids`` (scanned with ``search_after``)."""
    keep = {str(value) for value in keep_ids}
    stale: list[str] = []
    search_after: Optional[list[Any]] = None
    while True:
        kwargs: dict[str, Any] = {
            "index": index,
            "query": {"match_all": {}},
            "sort": [{"id": {"order": "asc", "unmapped_type": "long"}}],
            "size": SCAN_PAGE_SIZE,
            "source": False,
            "track_total_hits": False,
        }
        if search_after is not None:
            kwargs["search_after"] = search_after
        with _translate_errors():
            hits = _body(es.search(**kwargs))["hits"]["hits"]
        if not hits:
            break
        stale.extend(hit["_id"] for hit in hits if hit["_id"] not in keep)
        search_after = hits[-1]["sort"]
        if len(hits) < SCAN_PAGE_SIZE:
            break
    return stale


def remove_documents(es: Elasticsearch, index: str, doc_ids: Iterable[str]) -> int:
    """Bulk-delete documents by id. Returns the number of delete operations sent."""
    removed = 0
    chunk: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        chunk.append({"delete": {"_index": index, "_id": doc_id}})
        if len(chunk) >= BULK_CHUNK_SIZE:
            _run_bulk(es, chunk)
            removed += len(chunk)
            chunk = []
    if chunk:
        _run_bulk(es, chunk)
        removed += len(chunk)
    return removed


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


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
        doc.setdefault("id", hit["_id"])  # documents are keyed by post id
        doc["score"] = hit.get("_score")
        doc["highlights"] = hit.get("highlight") or {}
        results.append(doc)
    return results, total
