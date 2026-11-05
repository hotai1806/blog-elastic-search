from datetime import datetime
from types import SimpleNamespace

import pytest

from app import search
from tests.fakes import FakeElasticsearch

INDEX = "unit_index"


def fake_post(post_id: int, **overrides):
    now = datetime(2025, 3, 3, 12, 0, 0)
    values = {
        "id": post_id, "title": f"Post {post_id}", "content": "body", "author": "a",
        "tags": ["t"], "created_at": now, "updated_at": now,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_post_document_shape_and_null_tags():
    doc = search.post_document(fake_post(7, tags=None))
    assert doc == {
        "id": 7, "title": "Post 7", "content": "body", "author": "a", "tags": [],
        "created_at": "2025-03-03T12:00:00", "updated_at": "2025-03-03T12:00:00",
    }


def test_ensure_index_is_idempotent():
    es = FakeElasticsearch()
    assert search.ensure_index(es, INDEX) is True
    assert search.ensure_index(es, INDEX) is False
    assert es.indices.get_mapping(index=INDEX)[INDEX]["mappings"] == search.INDEX_MAPPINGS


def test_delete_index_reports_whether_it_existed():
    es = FakeElasticsearch()
    assert search.delete_index(es, INDEX) is False
    search.ensure_index(es, INDEX)
    assert search.delete_index(es, INDEX) is True
    assert not es.indices.exists(index=INDEX)


def test_mapping_problems_detects_missing_and_mismatched_fields():
    es = FakeElasticsearch()
    es.indices.create(index=INDEX, mappings={"properties": {"title": {"type": "text"}, "tags": {"type": "text"}}})
    problems = search.mapping_problems(es, INDEX)
    assert "field 'tags' is mapped as 'text', expected 'keyword'" in problems
    assert "field 'updated_at' is missing" in problems
    search.delete_index(es, INDEX)
    search.ensure_index(es, INDEX)
    assert search.mapping_problems(es, INDEX) == []


def test_index_post_waits_for_refresh_and_remove_ignores_missing():
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    search.index_post(es, INDEX, fake_post(1))
    assert es.calls_named("index") == [{"index": INDEX, "id": 1, "refresh": "wait_for"}]
    search.remove_post(es, INDEX, 1)
    search.remove_post(es, INDEX, 1)  # second delete must not raise
    assert es.get_document(INDEX, 1) is None


def test_reindex_posts_chunks_bulk_requests(monkeypatch):
    monkeypatch.setattr(search, "BULK_CHUNK_SIZE", 2)
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    count = search.reindex_posts(es, INDEX, (fake_post(i) for i in range(1, 6)))
    assert count == 5
    assert [c["count"] for c in es.calls_named("bulk")] == [2, 2, 1]
    assert all(c["refresh"] == "wait_for" for c in es.calls_named("bulk"))
    assert sorted(int(k) for k in es.documents(INDEX)) == [1, 2, 3, 4, 5]


def test_reindex_posts_raises_on_item_errors(monkeypatch):
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    monkeypatch.setattr(
        es, "bulk",
        lambda operations, **_: {"errors": True, "items": [{"index": {"_id": "1", "status": 400, "error": {"reason": "bad"}}}]},
    )
    with pytest.raises(search.SearchBackendError, match="1 bulk operation"):
        search.reindex_posts(es, INDEX, [fake_post(1)])


def test_writes_create_a_missing_index_with_the_explicit_mapping():
    es = FakeElasticsearch()
    search.index_post(es, INDEX, fake_post(1))
    assert es.indices.get_mapping(index=INDEX)[INDEX]["mappings"] == search.INDEX_MAPPINGS
    search.delete_index(es, INDEX)
    assert search.reindex_posts(es, INDEX, [fake_post(1), fake_post(2)]) == 2
    assert search.mapping_problems(es, INDEX) == []


def test_ensure_index_tolerates_a_concurrent_create(monkeypatch):
    from tests.fakes import _bad_request

    es = FakeElasticsearch()
    monkeypatch.setattr(es.indices, "create", lambda **_: (_ for _ in ()).throw(_bad_request("resource_already_exists_exception")))
    assert search.ensure_index(es, INDEX) is False
    monkeypatch.setattr(es.indices, "create", lambda **_: (_ for _ in ()).throw(_bad_request("illegal_argument_exception")))
    es._store.pop(INDEX, None)
    with pytest.raises(search.SearchBackendError):
        search.ensure_index(es, INDEX)


def test_prepare_index_creates_extends_or_rebuilds():
    es = FakeElasticsearch()
    assert search.prepare_index(es, INDEX) == "created"
    assert search.prepare_index(es, INDEX) == "ok"

    search.delete_index(es, INDEX)
    es.indices.create(index=INDEX, mappings={"properties": {"title": {"type": "text"}, "content": {"type": "text"}}})
    es.put_raw_document(INDEX, 1, {"title": "kept"})
    assert search.prepare_index(es, INDEX) == "extended"
    assert search.mapping_problems(es, INDEX) == []
    assert es.get_document(INDEX, 1) == {"title": "kept"}  # extending keeps documents

    search.delete_index(es, INDEX)
    es.indices.create(index=INDEX, mappings={"properties": {"tags": {"type": "text"}}})
    es.put_raw_document(INDEX, 1, {"title": "lost"})
    assert search.prepare_index(es, INDEX) == "rebuilt"
    assert search.mapping_problems(es, INDEX) == []
    assert es.documents(INDEX) == {}


def test_rebuild_index_gives_up_when_the_index_keeps_reappearing(monkeypatch):
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    monkeypatch.setattr(search, "mapping_problems", lambda *_: ["field 'tags' is mapped as 'text', expected 'keyword'"])
    with pytest.raises(search.SearchBackendError, match="could not recreate"):
        search.rebuild_index(es, INDEX)


def test_stale_documents_are_found_across_pages_and_removed(monkeypatch):
    monkeypatch.setattr(search, "SCAN_PAGE_SIZE", 2)
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    search.reindex_posts(es, INDEX, [fake_post(i) for i in range(1, 6)])
    stale = search.stale_document_ids(es, INDEX, keep_ids={1, 3, 5})
    assert stale == ["2", "4"]
    assert search.remove_documents(es, INDEX, stale) == 2
    assert sorted(es.documents(INDEX)) == ["1", "3", "5"]
    assert search.stale_document_ids(es, INDEX, keep_ids={1, 3, 5}) == []
    assert search.remove_documents(es, INDEX, ["999"]) == 1  # a missing document is not an error


def test_fake_rejects_pages_beyond_the_result_window():
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    with pytest.raises(search.SearchBackendError, match="Result window"):
        search.search_posts(es, INDEX, query=None, author=None, tags=[], sort="newest", page=101, size=100)


def test_transport_errors_become_search_backend_error():
    es = FakeElasticsearch()
    es.available = False
    with pytest.raises(search.SearchBackendError):
        search.ensure_index(es, INDEX)
    with pytest.raises(search.SearchBackendError):
        search.search_posts(es, INDEX, query="x", author=None, tags=[], sort="relevance", page=1, size=10)


def test_search_posts_parses_hits_and_total():
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    search.index_post(es, INDEX, fake_post(1, title="Elasticsearch guide", content="Indexing docs"))
    search.index_post(es, INDEX, fake_post(2, title="Other", content="Nothing here"))
    hits, total = search.search_posts(es, INDEX, query="elasticsearch", author=None, tags=[], sort="relevance", page=1, size=10)
    assert total == 1
    assert hits[0]["id"] == 1
    assert hits[0]["score"] > 0
    assert hits[0]["highlights"]["title"] == ["<em>Elasticsearch</em> guide"]


def test_search_posts_accepts_legacy_integer_total(monkeypatch):
    es = FakeElasticsearch()
    monkeypatch.setattr(es, "search", lambda **_: {"hits": {"total": 3, "hits": []}})
    hits, total = search.search_posts(es, INDEX, query=None, author=None, tags=[], sort="newest", page=1, size=10)
    assert (hits, total) == ([], 3)
