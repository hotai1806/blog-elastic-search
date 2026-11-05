"""In-memory stand-ins for the parts of the Elasticsearch client the application uses.

The fake implements just enough of the query DSL (``bool`` / ``multi_match`` / ``term`` /
``match_all``, sort, from/size, highlight) for the API tests. The exact DSL the app sends
is asserted separately in ``test_search_query.py`` and exercised for real by the
integration tests.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig, TransportError
from elasticsearch import BadRequestError, NotFoundError

_WORD = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: Any) -> list[str]:
    return [match.lower() for match in _WORD.findall(str(text))]


def _meta(status: int) -> ApiResponseMeta:
    return ApiResponseMeta(
        status=status,
        http_version="1.1",
        headers=HttpHeaders(),
        duration=0.0,
        node=NodeConfig("http", "fake-es", 9200),
    )


def _not_found(reason: str) -> NotFoundError:
    return NotFoundError(message=reason, meta=_meta(404), body={"error": {"reason": reason}})


class FakeIndices:
    def __init__(self, parent: "FakeElasticsearch") -> None:
        self._parent = parent

    @property
    def _store(self) -> dict[str, dict[str, Any]]:
        return self._parent._store

    def exists(self, index: str, **_: Any) -> bool:
        self._parent._check()
        return index in self._store

    def create(self, index: str, mappings: Optional[dict] = None, settings: Optional[dict] = None, **_: Any) -> dict:
        self._parent._check()
        if index in self._store:
            raise BadRequestError(
                message="resource_already_exists_exception", meta=_meta(400), body={}
            )
        self._store[index] = {"mappings": dict(mappings or {}), "settings": dict(settings or {}), "docs": {}}
        return {"acknowledged": True, "index": index}

    def delete(self, index: str, **_: Any) -> dict:
        self._parent._check()
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        del self._store[index]
        return {"acknowledged": True}

    def refresh(self, index: Optional[str] = None, **_: Any) -> dict:
        self._parent._check()
        return {}

    def get_mapping(self, index: str, **_: Any) -> dict:
        self._parent._check()
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        return {index: {"mappings": self._store[index]["mappings"]}}


class _WithOptions:
    """What ``FakeElasticsearch.options(...)`` returns: a client that ignores some statuses."""

    def __init__(self, parent: "FakeElasticsearch", ignore_status: set[int]) -> None:
        self._parent = parent
        self._ignore = ignore_status

    def delete(self, **kwargs: Any) -> dict:
        try:
            return self._parent.delete(**kwargs)
        except NotFoundError:
            if 404 in self._ignore:
                return {"result": "not_found"}
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parent, name)


def _query_tokens(query: Optional[dict]) -> list[str]:
    if not query:
        return []
    ((kind, body),) = query.items()
    if kind == "multi_match":
        return tokenize(body["query"])
    if kind == "bool":
        tokens: list[str] = []
        for clause in body.get("must", []):
            tokens.extend(_query_tokens(clause))
        return tokens
    return []


def _evaluate(query: Optional[dict], doc: dict) -> Optional[float]:
    """Score ``doc`` against ``query``; ``None`` means it does not match."""
    if not query:
        return 1.0
    ((kind, body),) = query.items()
    if kind == "match_all":
        return 1.0
    if kind == "bool":
        score = 0.0
        for clause in body.get("must", []):
            part = _evaluate(clause, doc)
            if part is None:
                return None
            score += part
        for clause in body.get("filter", []):
            if _evaluate(clause, doc) is None:
                return None
        return score
    if kind == "multi_match":
        query_tokens = tokenize(body["query"])
        score = 0.0
        for spec in body["fields"]:
            name, _, boost = spec.partition("^")
            field_tokens = tokenize(doc.get(name, ""))
            score += float(boost or 1) * sum(1 for token in query_tokens if token in field_tokens)
        return score if score > 0 else None
    if kind == "term":
        ((field, value),) = body.items()
        if isinstance(value, dict):
            value = value["value"]
        actual = doc.get(field)
        if isinstance(actual, list):
            return 0.0 if value in actual else None
        return 0.0 if actual == value else None
    raise NotImplementedError(f"FakeElasticsearch does not understand {kind!r} queries")


def _mark(text: str, tokens: list[str]) -> str:
    return _WORD.sub(
        lambda m: f"<em>{m.group(0)}</em>" if m.group(0).lower() in tokens else m.group(0), text
    )


def _highlight(doc: dict, fields: dict[str, dict], tokens: list[str]) -> dict[str, list[str]]:
    snippets: dict[str, list[str]] = {}
    for field, spec in fields.items():
        value = str(doc.get(field, ""))
        positions = [m.start() for m in _WORD.finditer(value) if m.group(0).lower() in tokens]
        if not positions:
            continue
        if spec.get("number_of_fragments", 5) == 0:
            snippets[field] = [_mark(value, tokens)]
        else:
            size = spec.get("fragment_size", 100)
            start = max(0, positions[0] - size // 2)
            snippets[field] = [_mark(value[start : start + size], tokens)]
    return snippets


def _sort_key(value: Any) -> tuple[bool, Any]:
    return (value is None, value)


class FakeElasticsearch:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self.indices = FakeIndices(self)
        self.available = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _check(self) -> None:
        if not self.available:
            raise TransportError("simulated Elasticsearch outage")

    # ---- helpers for assertions -------------------------------------------------
    def documents(self, index: str) -> dict[str, dict]:
        return dict(self._store.get(index, {}).get("docs", {}))

    def get_document(self, index: str, doc_id: Any) -> Optional[dict]:
        return self._store.get(index, {}).get("docs", {}).get(str(doc_id))

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, kwargs in self.calls if called == name]

    # ---- client surface ---------------------------------------------------------
    def options(self, ignore_status: Any = None, **_: Any) -> _WithOptions:
        if ignore_status is None:
            statuses: set[int] = set()
        elif isinstance(ignore_status, int):
            statuses = {ignore_status}
        else:
            statuses = set(ignore_status)
        return _WithOptions(self, statuses)

    def ping(self, **_: Any) -> bool:
        return self.available

    def info(self, **_: Any) -> dict:
        self._check()
        return {"version": {"number": "8.10.1"}, "tagline": "fake"}

    def index(self, index: str, document: dict, id: Any = None, refresh: Any = None, **_: Any) -> dict:
        self._check()
        self.calls.append(("index", {"index": index, "id": id, "refresh": refresh}))
        bucket = self._store.setdefault(index, {"mappings": {}, "settings": {}, "docs": {}})
        bucket["docs"][str(id)] = dict(document)
        return {"_index": index, "_id": str(id), "result": "created"}

    def delete(self, index: str, id: Any, refresh: Any = None, **_: Any) -> dict:
        self._check()
        self.calls.append(("delete", {"index": index, "id": id, "refresh": refresh}))
        docs = self._store.get(index, {}).get("docs")
        if docs is None or str(id) not in docs:
            raise _not_found(f"document {id!r} not found in {index!r}")
        del docs[str(id)]
        return {"_index": index, "_id": str(id), "result": "deleted"}

    def bulk(self, operations: list[dict], refresh: Any = None, **_: Any) -> dict:
        self._check()
        self.calls.append(("bulk", {"count": len(operations) // 2, "refresh": refresh}))
        items = []
        pending = iter(operations)
        for action in pending:
            ((op, meta),) = action.items()
            if op != "index":
                raise NotImplementedError(f"FakeElasticsearch.bulk does not support {op!r}")
            document = next(pending)
            bucket = self._store.setdefault(meta["_index"], {"mappings": {}, "settings": {}, "docs": {}})
            bucket["docs"][str(meta["_id"])] = dict(document)
            items.append({"index": {"_index": meta["_index"], "_id": str(meta["_id"]), "status": 201}})
        return {"took": 1, "errors": False, "items": items}

    def search(
        self,
        index: str,
        query: Optional[dict] = None,
        sort: Optional[list] = None,
        from_: int = 0,
        size: int = 10,
        highlight: Optional[dict] = None,
        track_total_hits: Any = None,
        **_: Any,
    ) -> dict:
        self._check()
        self.calls.append(("search", {"index": index, "query": query, "sort": sort, "from_": from_, "size": size}))
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")

        matched: list[list[Any]] = []
        for doc in self._store[index]["docs"].values():
            score = _evaluate(query, doc)
            if score is not None:
                matched.append([score, doc])

        clauses = sort or [{"_score": {"order": "desc"}}]
        for clause in reversed(clauses):
            ((field, spec),) = clause.items()
            order = spec.get("order", "asc") if isinstance(spec, dict) else spec
            if field == "_score":
                matched.sort(key=lambda item: item[0], reverse=(order == "desc"))
            else:
                matched.sort(key=lambda item, f=field: _sort_key(item[1].get(f)), reverse=(order == "desc"))
        scored = any("_score" in clause for clause in clauses)

        tokens = _query_tokens(query)
        hits = []
        for score, doc in matched[from_ : from_ + size]:
            hit: dict[str, Any] = {
                "_index": index,
                "_id": str(doc.get("id")),
                "_score": score if scored else None,
                "_source": dict(doc),
            }
            if highlight and tokens:
                snippets = _highlight(doc, highlight.get("fields", {}), tokens)
                if snippets:
                    hit["highlight"] = snippets
            hits.append(hit)
        return {
            "took": 1,
            "timed_out": False,
            "hits": {"total": {"value": len(matched), "relation": "eq"}, "max_score": None, "hits": hits},
        }
