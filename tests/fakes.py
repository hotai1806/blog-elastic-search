"""In-memory stand-ins for the parts of the Elasticsearch client the application uses.

The fake implements just enough of the query DSL (``bool`` / ``multi_match`` / ``term`` /
``ids`` / ``match_all``, sort, from/size, ``search_after``, highlight) for the API tests, and
mirrors the real client's shapes where the application depends on them: responses are
``ObjectApiResponse`` objects, ``hits.total`` is an object, ``_score`` is null on field
sorts, and a page beyond the 10 000 result window is a ``BadRequestError``. The exact DSL
the app sends is asserted separately in ``test_search_query.py`` and exercised for real by
the integration tests.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from elastic_transport import (
    ApiResponseMeta,
    HttpHeaders,
    NodeConfig,
    ObjectApiResponse,
    TransportError,
)
from elasticsearch import BadRequestError, NotFoundError

MAX_RESULT_WINDOW = 10_000
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


def _response(body: dict, status: int = 200) -> ObjectApiResponse:
    return ObjectApiResponse(body=body, meta=_meta(status))


def _not_found(reason: str) -> NotFoundError:
    return NotFoundError(message=reason, meta=_meta(404), body={"error": {"reason": reason}})


def _bad_request(error_type: str, reason: Optional[str] = None) -> BadRequestError:
    return BadRequestError(
        message=error_type, meta=_meta(400), body={"error": {"type": error_type, "reason": reason or error_type}}
    )


def _empty_index() -> dict[str, Any]:
    return {"mappings": {}, "settings": {}, "docs": {}}


class FakeIndices:
    def __init__(self, parent: "FakeElasticsearch") -> None:
        self._parent = parent

    @property
    def _store(self) -> dict[str, dict[str, Any]]:
        return self._parent._store

    def exists(self, index: str, **_: Any) -> bool:
        self._parent._check()
        return index in self._store

    def create(self, index: str, mappings: Optional[dict] = None, settings: Optional[dict] = None, **_: Any):
        self._parent._check()
        if index in self._store:
            raise _bad_request("resource_already_exists_exception", f"index [{index}] already exists")
        self._store[index] = {"mappings": dict(mappings or {}), "settings": dict(settings or {}), "docs": {}}
        return _response({"acknowledged": True, "shards_acknowledged": True, "index": index})

    def delete(self, index: str, **_: Any):
        self._parent._check()
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        del self._store[index]
        return _response({"acknowledged": True})

    def refresh(self, index: Optional[str] = None, **_: Any):
        self._parent._check()
        return _response({"_shards": {"total": 1, "successful": 1, "failed": 0}})

    def get_mapping(self, index: str, **_: Any):
        self._parent._check()
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        return _response({index: {"mappings": self._store[index]["mappings"]}})

    def put_mapping(self, index: str, properties: Optional[dict] = None, **_: Any):
        self._parent._check()
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        current = self._store[index]["mappings"].setdefault("properties", {})
        for name, definition in (properties or {}).items():
            existing = current.get(name)
            if existing is not None and existing.get("type") != definition.get("type"):
                raise _bad_request("illegal_argument_exception", f"mapper [{name}] cannot be changed")
            current[name] = dict(definition)
        return _response({"acknowledged": True})


class _WithOptions:
    """What ``FakeElasticsearch.options(...)`` returns: a client that ignores some statuses."""

    def __init__(self, parent: "FakeElasticsearch", ignore_status: set[int]) -> None:
        self._parent = parent
        self._ignore = ignore_status

    def delete(self, **kwargs: Any):
        try:
            return self._parent.delete(**kwargs)
        except NotFoundError:
            if 404 in self._ignore:
                return _response({"result": "not_found"}, status=404)
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
        for clause in body.get("must_not", []):
            if _evaluate(clause, doc) is not None:
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
    if kind == "ids":
        return 0.0 if str(doc.get("_fake_id")) in {str(v) for v in body["values"]} else None
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
        return {k: dict(v) for k, v in self._store.get(index, {}).get("docs", {}).items()}

    def get_document(self, index: str, doc_id: Any) -> Optional[dict]:
        doc = self._store.get(index, {}).get("docs", {}).get(str(doc_id))
        return dict(doc) if doc is not None else None

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, kwargs in self.calls if called == name]

    def put_raw_document(self, index: str, doc_id: Any, document: dict) -> None:
        """Store a document without going through the app (e.g. a legacy-shaped one)."""
        self._store.setdefault(index, _empty_index())["docs"][str(doc_id)] = dict(document)

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

    def info(self, **_: Any):
        self._check()
        return _response({"version": {"number": "8.10.1"}, "tagline": "fake"})

    def index(self, index: str, document: dict, id: Any = None, refresh: Any = None, **_: Any):
        self._check()
        self.calls.append(("index", {"index": index, "id": id, "refresh": refresh}))
        bucket = self._store.setdefault(index, _empty_index())  # Elasticsearch auto-creates
        bucket["docs"][str(id)] = dict(document)
        return _response({"_index": index, "_id": str(id), "result": "created"}, status=201)

    def delete(self, index: str, id: Any, refresh: Any = None, **_: Any):
        self._check()
        self.calls.append(("delete", {"index": index, "id": id, "refresh": refresh}))
        docs = self._store.get(index, {}).get("docs")
        if docs is None or str(id) not in docs:
            raise _not_found(f"document {id!r} not found in {index!r}")
        del docs[str(id)]
        return _response({"_index": index, "_id": str(id), "result": "deleted"})

    def bulk(self, operations: list[dict], refresh: Any = None, **_: Any):
        self._check()
        items = []
        pending = iter(operations)
        count = 0
        for action in pending:
            ((op, meta),) = action.items()
            count += 1
            bucket = self._store.setdefault(meta["_index"], _empty_index())
            doc_id = str(meta["_id"])
            if op == "index":
                bucket["docs"][doc_id] = dict(next(pending))
                items.append({"index": {"_index": meta["_index"], "_id": doc_id, "status": 201, "result": "created"}})
            elif op == "delete":
                if doc_id in bucket["docs"]:
                    del bucket["docs"][doc_id]
                    items.append({"delete": {"_index": meta["_index"], "_id": doc_id, "status": 200, "result": "deleted"}})
                else:
                    items.append({"delete": {"_index": meta["_index"], "_id": doc_id, "status": 404, "result": "not_found"}})
            else:
                raise NotImplementedError(f"FakeElasticsearch.bulk does not support {op!r}")
        self.calls.append(("bulk", {"count": count, "refresh": refresh}))
        return _response({"took": 1, "errors": False, "items": items})

    def search(
        self,
        index: str,
        query: Optional[dict] = None,
        sort: Optional[list] = None,
        from_: int = 0,
        size: int = 10,
        highlight: Optional[dict] = None,
        track_total_hits: Any = None,
        search_after: Optional[list] = None,
        source: Any = True,
        **_: Any,
    ):
        self._check()
        self.calls.append(("search", {"index": index, "query": query, "sort": sort, "from_": from_, "size": size}))
        if index not in self._store:
            raise _not_found(f"index {index!r} not found")
        if from_ + size > MAX_RESULT_WINDOW:
            raise _bad_request(
                "illegal_argument_exception",
                f"Result window is too large, from + size must be less than or equal to: [{MAX_RESULT_WINDOW}]",
            )

        matched: list[list[Any]] = []
        for doc_id, doc in self._store[index]["docs"].items():
            score = _evaluate(query, {**doc, "_fake_id": doc_id})
            if score is not None:
                matched.append([score, doc, doc_id])

        clauses = sort or [{"_score": {"order": "desc"}}]
        doc_order = {doc_id: position for position, doc_id in enumerate(self._store[index]["docs"])}

        def parse_clause(clause: Any) -> tuple[str, str]:
            if isinstance(clause, str):  # e.g. "_doc"
                return clause, "asc"
            ((field, spec),) = clause.items()
            return field, (spec.get("order", "asc") if isinstance(spec, dict) else spec)

        def value_of(item: list[Any], field: str) -> Any:
            if field == "_score":
                return item[0]
            if field == "_doc":
                return doc_order[item[2]]
            return item[1].get(field)

        for clause in reversed(clauses):
            field, order = parse_clause(clause)
            matched.sort(key=lambda item, f=field: _sort_key(value_of(item, f)), reverse=(order == "desc"))
        scored = any(parse_clause(clause)[0] == "_score" for clause in clauses)

        def sort_values(item: list[Any]) -> list[Any]:
            return [value_of(item, parse_clause(clause)[0]) for clause in clauses]

        if search_after is not None:
            orders = [parse_clause(clause)[1] for clause in clauses]

            def after(item: list[Any]) -> bool:
                # lexicographic comparison of the full sort tuple, honouring each key's order
                for value, limit, order in zip(sort_values(item), search_after, orders):
                    if _sort_key(value) == _sort_key(limit):
                        continue
                    return _sort_key(value) > _sort_key(limit) if order == "asc" else _sort_key(value) < _sort_key(limit)
                return False

            matched = [item for item in matched if after(item)]

        tokens = _query_tokens(query)
        hits = []
        for item in matched[from_ : from_ + size]:
            score, doc, doc_id = item
            hit: dict[str, Any] = {
                "_index": index,
                "_id": doc_id,
                "_score": score if scored else None,
                "sort": sort_values(item),
            }
            if source is not False:
                hit["_source"] = dict(doc)
            if highlight and tokens:
                snippets = _highlight(doc, highlight.get("fields", {}), tokens)
                if snippets:
                    hit["highlight"] = snippets
            hits.append(hit)
        body: dict[str, Any] = {
            "took": 1,
            "timed_out": False,
            "hits": {"total": {"value": len(matched), "relation": "eq"}, "max_score": None, "hits": hits},
        }
        if track_total_hits is False:
            body["hits"].pop("total")
        return _response(body)
