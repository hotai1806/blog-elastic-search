import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app import cache
from app.models import BlogPost, Tag
from app.services import posts as posts_service
from app import search
from app.schemas import PostCreate, PostOut
from tests.conftest import INDEX, make_post


def test_create_post_returns_201_normalises_tags_and_indexes_document(client, fake_es):
    body = make_post(client, tags=["Python", " python", "Search Engines"])
    assert body["id"] == 1
    assert body["tags"] == ["python", "search engines"]
    assert body["title"] == "Hello world"
    assert body["created_at"] == body["updated_at"]
    datetime.fromisoformat(body["created_at"])

    doc = fake_es.get_document(INDEX, 1)
    assert doc["title"] == "Hello world"
    assert doc["tags"] == ["python", "search engines"]
    assert doc["created_at"] == body["created_at"]
    assert fake_es.calls_named("index")[-1]["refresh"] == "wait_for"


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "c", "author": "a"},  # missing title
        {"title": "   ", "content": "c", "author": "a"},  # blank title
        {"title": "x" * 256, "content": "c", "author": "a"},  # too long
        {"title": "t", "content": "c", "author": "a", "tags": "python"},  # tags not a list
        {"title": "t", "content": "c", "author": "a", "tags": [""]},  # blank tag
        {"title": "t", "content": "c", "author": "a", "tags": [f"t{i}" for i in range(21)]},  # too many
    ],
)
def test_create_post_validation_errors(client, payload):
    response = client.post("/posts/", json=payload)
    assert response.status_code == 422


def test_create_post_requires_a_json_body(client):
    response = client.post("/posts/?title=a&content=b&author=c")
    assert response.status_code == 422


def test_get_post_returns_it_and_caches_it(client, fake_redis):
    created = make_post(client)
    response = client.get("/posts/1")
    assert response.status_code == 200
    assert response.json() == created
    assert fake_redis.exists("post:1") == 1
    assert 0 < fake_redis.ttl("post:1") <= 3600


def test_get_post_is_served_from_cache_until_invalidated(client, db):
    make_post(client)
    assert client.get("/posts/1").json()["title"] == "Hello world"

    post = db.get(BlogPost, 1)
    post.title = "changed behind the cache"
    db.commit()

    assert client.get("/posts/1").json()["title"] == "Hello world"


def test_get_post_ignores_malformed_cache_entry(client, fake_redis):
    make_post(client)
    fake_redis.set("post:1", b"{not json")
    response = client.get("/posts/1")
    assert response.status_code == 200
    assert response.json()["title"] == "Hello world"
    assert fake_redis.get("post:1").startswith(b"{")


def test_get_missing_post_is_404(client):
    response = client.get("/posts/999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Post not found"}


def test_list_posts_is_newest_first_and_paginated(client):
    for i in range(1, 4):
        make_post(client, title=f"Post {i}")
    page1 = client.get("/posts/", params={"size": 2}).json()
    assert [p["id"] for p in page1["items"]] == [3, 2]
    assert (page1["total"], page1["page"], page1["size"], page1["pages"]) == (3, 1, 2, 2)
    page2 = client.get("/posts/", params={"size": 2, "page": 2}).json()
    assert [p["id"] for p in page2["items"]] == [1]


def test_list_posts_filters_by_author_and_normalised_tag(client):
    make_post(client, title="A", author="alice", tags=["python"])
    make_post(client, title="B", author="bob", tags=["python", "go"])
    make_post(client, title="C", author="alice", tags=["go"])

    by_author = client.get("/posts/", params={"author": "alice"}).json()
    assert [p["title"] for p in by_author["items"]] == ["C", "A"]

    by_tag = client.get("/posts/", params={"tag": "  PYTHON "}).json()
    assert [p["title"] for p in by_tag["items"]] == ["B", "A"]
    assert by_tag["total"] == 2

    both = client.get("/posts/", params={"author": "bob", "tag": "go"}).json()
    assert [p["title"] for p in both["items"]] == ["B"]

    every = client.get("/posts/", params={"tag": ["python", "GO"]}).json()
    assert [p["title"] for p in every["items"]] == ["B"]
    assert every["total"] == 1


def test_list_posts_rejects_blank_or_overlong_tag(client):
    make_post(client)
    response = client.get("/posts/", params={"tag": " "})
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["query", "tag"]
    assert "blank" in error["msg"]
    assert client.get("/posts/", params={"tag": "x" * 51}).status_code == 422


def test_list_posts_empty(client):
    assert client.get("/posts/").json() == {"items": [], "total": 0, "page": 1, "size": 10, "pages": 0}


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}, {"page": "x"}])
def test_list_posts_rejects_bad_pagination(client, params):
    assert client.get("/posts/", params=params).status_code == 422


def test_put_replaces_everything_and_clears_omitted_tags(client, fake_es, fake_redis):
    created = make_post(client, tags=["python"])
    client.get("/posts/1")
    assert fake_redis.exists("post:1") == 1

    response = client.put("/posts/1", json={"title": "New", "content": "Body", "author": "bob"})
    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["content"], body["author"], body["tags"]) == ("New", "Body", "bob", [])
    assert body["created_at"] == created["created_at"]
    assert body["updated_at"] > created["updated_at"]

    assert json.loads(fake_redis.get("post:1")) == body  # written through
    assert fake_es.get_document(INDEX, 1)["title"] == "New"
    assert fake_es.get_document(INDEX, 1)["tags"] == []
    assert client.get("/posts/1").json() == body
    # the tag itself survives, now unused
    assert client.get("/tags/").json()[0] == {**client.get("/tags/python").json(), "post_count": 0}


def test_put_requires_all_fields(client):
    make_post(client)
    assert client.put("/posts/1", json={"title": "only"}).status_code == 422


def test_put_missing_post_is_404(client):
    response = client.put("/posts/42", json={"title": "t", "content": "c", "author": "a"})
    assert response.status_code == 404


def test_patch_updates_only_given_fields(client, fake_es, fake_redis):
    created = make_post(client, tags=["python"])
    client.get("/posts/1")
    response = client.patch("/posts/1", json={"title": "Patched"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Patched"
    assert body["content"] == created["content"]
    assert body["tags"] == ["python"]
    assert body["updated_at"] > created["updated_at"]
    assert json.loads(fake_redis.get("post:1"))["title"] == "Patched"
    assert fake_es.get_document(INDEX, 1)["title"] == "Patched"


def test_patch_replaces_tags_when_given_and_ignores_null(client):
    make_post(client, tags=["python"])
    assert client.patch("/posts/1", json={"tags": ["Go", "rust"]}).json()["tags"] == ["go", "rust"]
    assert client.patch("/posts/1", json={"tags": None, "title": None}).json()["tags"] == ["go", "rust"]
    assert client.patch("/posts/1", json={"tags": []}).json()["tags"] == []


def test_patch_with_empty_body_changes_nothing(client, fake_es):
    created = make_post(client)
    index_calls = len(fake_es.calls_named("index"))
    body = client.patch("/posts/1", json={}).json()
    assert body == created
    assert len(fake_es.calls_named("index")) == index_calls


def test_patch_validation_and_404(client):
    make_post(client)
    assert client.patch("/posts/1", json={"title": ""}).status_code == 422
    assert client.patch("/posts/9", json={"title": "x"}).status_code == 404


def test_delete_post_removes_everything(client, fake_es, fake_redis, db):
    make_post(client, tags=["python"])
    client.get("/posts/1")
    response = client.delete("/posts/1")
    assert response.status_code == 204
    assert response.content == b""

    assert client.get("/posts/1").status_code == 404
    assert db.get(BlogPost, 1) is None
    assert fake_es.get_document(INDEX, 1) is None
    assert fake_es.calls_named("delete")[-1]["refresh"] == "wait_for"
    assert fake_redis.get("post:1") == cache.TOMBSTONE
    assert client.delete("/posts/1").status_code == 404
    assert client.get("/tags/python").json()["post_count"] == 0


def test_create_rolls_back_when_search_is_down(client, fake_es, db):
    fake_es.available = False
    response = client.post(
        "/posts/", json={"title": "t", "content": "c", "author": "a", "tags": ["brand-new"]}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Search backend unavailable; try again later"}
    assert db.scalars(select(BlogPost)).all() == []
    assert db.scalars(select(Tag)).all() == []


def test_update_keeps_old_data_when_search_is_down(client, fake_es, db):
    make_post(client, title="Original")
    fake_es.available = False
    assert client.put("/posts/1", json={"title": "New", "content": "c", "author": "a"}).status_code == 503
    assert client.patch("/posts/1", json={"title": "New"}).status_code == 503
    db.expire_all()
    assert db.get(BlogPost, 1).title == "Original"
    assert fake_es.get_document(INDEX, 1)["title"] == "Original"


def test_delete_keeps_post_when_search_is_down(client, fake_es, db):
    make_post(client)
    fake_es.available = False
    assert client.delete("/posts/1").status_code == 503
    assert db.get(BlogPost, 1) is not None
    fake_es.available = True
    assert client.get("/posts/1").status_code == 200


def test_tags_are_shared_between_posts(client, db):
    make_post(client, tags=["python", "search"])
    make_post(client, tags=["python"])
    assert sorted(tag.name for tag in db.scalars(select(Tag))) == ["python", "search"]
    tags = {tag["name"]: tag["post_count"] for tag in client.get("/tags/").json()}
    assert tags == {"python": 2, "search": 1}


def test_concurrent_tag_creation_is_tolerated(db, session_factory, fake_es, monkeypatch):
    other = session_factory()
    other.add(Tag(name="race"))
    other.commit()
    other.close()

    # Simulate losing the race: our lookup sees nothing, but the INSERT collides.
    monkeypatch.setattr(posts_service, "_existing_tags", lambda _db, _names: {})
    post = posts_service.create_post(
        db, fake_es, INDEX, PostCreate(title="t", content="c", author="a", tags=["race", "other"])
    )
    assert post.tags == ["race", "other"]
    assert [tag.name for tag in db.scalars(select(Tag).order_by(Tag.name))] == ["other", "race"]
    assert db.get(BlogPost, post.id) is not None


def test_slow_reader_cannot_resurrect_a_deleted_post(client, fake_redis, db):
    """A GET that read the row just before a DELETE must not re-populate the cache."""
    make_post(client)
    snapshot = PostOut.model_validate(db.get(BlogPost, 1))
    db.close()
    assert client.delete("/posts/1").status_code == 204
    # ...the slow reader now finishes and tries to cache what it read
    cache.cache_post(fake_redis, snapshot, 3600, only_if_absent=True)
    assert client.get("/posts/1").status_code == 404
    assert fake_redis.get("post:1") == cache.TOMBSTONE


def test_slow_reader_cannot_overwrite_a_newer_update(client, fake_redis, db):
    make_post(client, title="Original")
    snapshot = PostOut.model_validate(db.get(BlogPost, 1))
    db.close()
    client.put("/posts/1", json={"title": "Updated", "content": "c", "author": "a"})
    cache.cache_post(fake_redis, snapshot, 3600, only_if_absent=True)
    assert client.get("/posts/1").json()["title"] == "Updated"


def test_first_write_creates_a_missing_index_with_the_mapping(client, fake_es):
    """Startup could not create the index (Elasticsearch was down): the first write must."""
    fake_es.indices.delete(index=INDEX)
    search.forget_verified_indexes()
    make_post(client, author="Jane Doe", tags=["machine learning"])
    assert fake_es.indices.get_mapping(index=INDEX)[INDEX]["mappings"] == search.INDEX_MAPPINGS
    assert search.mapping_problems(fake_es, INDEX) == []
