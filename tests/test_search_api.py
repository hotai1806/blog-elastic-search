import pytest

from tests.conftest import make_post


@pytest.fixture()
def seeded(client):
    make_post(client, title="Elasticsearch basics", content="How to index documents", author="alice", tags=["search", "elasticsearch"])
    make_post(client, title="Python tips", content="Using the Elasticsearch client from python", author="bob", tags=["python", "elasticsearch"])
    make_post(client, title="Cooking pasta", content="Boil water, add salt", author="alice", tags=["food"])
    return client


def ids(response):
    assert response.status_code == 200, response.text
    return [hit["id"] for hit in response.json()["results"]]


def test_search_by_query_ranks_title_matches_first_and_highlights(seeded):
    response = seeded.get("/search/", params={"query": "elasticsearch"})
    body = response.json()
    assert ids(response) == [1, 2]
    assert (body["total"], body["page"], body["size"], body["pages"], body["query"]) == (2, 1, 10, 1, "elasticsearch")
    first, second = body["results"]
    assert first["score"] > second["score"]
    assert first["highlights"]["title"] == ["<em>Elasticsearch</em> basics"]
    assert "<em>Elasticsearch</em>" in second["highlights"]["content"][0]
    assert set(first) >= {"id", "title", "content", "author", "tags", "created_at", "updated_at", "score", "highlights"}


def test_search_requires_every_requested_tag(seeded):
    assert ids(seeded.get("/search/", params={"tag": ["elasticsearch", "python"]})) == [2]
    assert ids(seeded.get("/search/", params={"tag": "Elasticsearch"})) == [2, 1]
    assert ids(seeded.get("/search/", params={"tag": "nope"})) == []


def test_search_filters_by_author(seeded):
    assert ids(seeded.get("/search/", params={"author": "alice"})) == [3, 1]
    assert ids(seeded.get("/search/", params={"author": "alice", "query": "pasta"})) == [3]


def test_blank_query_browses_newest_first(seeded):
    response = seeded.get("/search/", params={"query": "   "})
    assert ids(response) == [3, 2, 1]
    assert response.json()["query"] is None
    assert response.json()["results"][0]["highlights"] == {}


def test_sort_options(seeded):
    assert ids(seeded.get("/search/", params={"sort": "oldest"})) == [1, 2, 3]
    assert ids(seeded.get("/search/", params={"sort": "newest"})) == [3, 2, 1]
    assert ids(seeded.get("/search/", params={"sort": "relevance"})) == [3, 2, 1]
    assert ids(seeded.get("/search/", params={"sort": "oldest", "query": "elasticsearch"})) == [1, 2]


def test_search_pagination(seeded):
    page1 = seeded.get("/search/", params={"size": 2})
    assert ids(page1) == [3, 2]
    assert page1.json()["pages"] == 2
    assert ids(seeded.get("/search/", params={"size": 2, "page": 2})) == [1]
    assert ids(seeded.get("/search/", params={"size": 2, "page": 3})) == []


def test_search_rejects_result_window_overflow(client):
    response = client.get("/search/", params={"page": 101, "size": 100})
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["query", "page"]
    assert "10000" in error["msg"]
    assert client.get("/search/", params={"page": 100, "size": 100}).status_code == 200


@pytest.mark.parametrize("params", [{"size": 0}, {"page": 0}, {"sort": "bogus"}, {"query": "x" * 501}])
def test_search_validation(client, params):
    assert client.get("/search/", params=params).status_code == 422


def test_search_returns_503_when_elasticsearch_is_down(seeded, fake_es):
    fake_es.available = False
    response = seeded.get("/search/", params={"query": "elasticsearch"})
    assert response.status_code == 503


def test_search_reflects_updates_and_deletes(seeded):
    seeded.patch("/posts/3", json={"title": "Elasticsearch cooking"})
    assert ids(seeded.get("/search/", params={"query": "elasticsearch"})) == [3, 1, 2]
    seeded.delete("/posts/1")
    assert ids(seeded.get("/search/", params={"query": "elasticsearch"})) == [3, 2]


def test_search_rejects_blank_tag(seeded):
    response = seeded.get("/search/", params={"tag": ["python", "  "]})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "tag"]


def test_search_tolerates_documents_indexed_by_older_versions(seeded, fake_es):
    from tests.conftest import INDEX

    fake_es.put_raw_document(
        INDEX, 77,
        {"id": 77, "title": "Legacy elasticsearch post", "content": "old", "author": "x", "created_at": "2024-01-01T00:00:00"},
    )
    response = seeded.get("/search/", params={"query": "legacy"})
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert (hit["id"], hit["tags"], hit["updated_at"]) == (77, [], "2024-01-01T00:00:00")
