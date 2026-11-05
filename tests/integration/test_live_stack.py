"""End-to-end tests against live PostgreSQL, Elasticsearch and Redis.

Enabled by setting INTEGRATION_DATABASE_URL, INTEGRATION_ELASTICSEARCH_URL and
INTEGRATION_REDIS_URL (see the README). The database is migrated with Alembic
(``upgrade head``) before the tests and downgraded to ``base`` afterwards; a dedicated
search index and only ``post:*`` Redis keys are touched.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from alembic import command
from alembic.config import Config
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient
from sqlalchemy import ARRAY, String, create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

import migrate
from app import search
from app.config import settings
from app.database import get_db
from app.deps import get_es, get_redis
from app.main import app
from app.models import Tag
from app.schemas import PostCreate
from app.services import posts as posts_service

DB_URL = os.getenv("INTEGRATION_DATABASE_URL")
ES_URL = os.getenv("INTEGRATION_ELASTICSEARCH_URL")
REDIS_URL = os.getenv("INTEGRATION_REDIS_URL")
TEST_INDEX = "blog_posts_integration_test"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DB_URL and ES_URL and REDIS_URL),
        reason="set INTEGRATION_DATABASE_URL, INTEGRATION_ELASTICSEARCH_URL and INTEGRATION_REDIS_URL",
    ),
]

ALL_TABLES = ["post_tags", "tags", "blog_posts", "alembic_version"]


def alembic_config() -> Config:
    os.environ["DATABASE_URL"] = DB_URL  # read by migrations/env.py
    return Config(str(ALEMBIC_INI))


def drop_everything(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {', '.join(ALL_TABLES)} CASCADE"))


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(DB_URL)
    drop_everything(engine)
    command.upgrade(alembic_config(), "head")
    yield engine
    command.downgrade(alembic_config(), "base")
    assert not {"blog_posts", "tags", "post_tags"} & set(inspect(engine).get_table_names())
    engine.dispose()


@pytest.fixture(scope="module")
def es_client() -> Iterator[Elasticsearch]:
    es = Elasticsearch(ES_URL, request_timeout=30)
    assert es.ping(), f"Elasticsearch at {ES_URL} is not reachable"
    yield es
    search.delete_index(es, TEST_INDEX)
    es.close()


@pytest.fixture(scope="module")
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(REDIS_URL)
    client.ping()
    yield client
    for key in client.scan_iter("post:*"):
        client.delete(key)
    client.close()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def client(engine, es_client, redis_client, session_factory, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "elasticsearch_index", TEST_INDEX)
    search.delete_index(es_client, TEST_INDEX)
    search.ensure_index(es_client, TEST_INDEX)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE post_tags, tags, blog_posts RESTART IDENTITY CASCADE"))
    for key in redis_client.scan_iter("post:*"):
        redis_client.delete(key)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_es] = lambda: es_client
    app.dependency_overrides[get_redis] = lambda: redis_client
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def create(client: TestClient, **overrides) -> dict:
    payload = {"title": "Hello", "content": "Body", "author": "tai", "tags": []}
    payload.update(overrides)
    response = client.post("/posts/", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [hit["id"] for hit in response.json()["results"]]


def test_migrated_schema_matches_the_models(engine):
    inspector = inspect(engine)
    assert {"blog_posts", "tags", "post_tags", "alembic_version"} <= set(inspector.get_table_names())
    columns = {col["name"]: col for col in inspector.get_columns("blog_posts")}
    assert {"id", "title", "content", "author", "created_at", "updated_at", "tags"} <= set(columns)
    tags_type = columns["tags"]["type"]
    assert isinstance(tags_type, ARRAY) and isinstance(tags_type.item_type, String)
    assert tags_type.item_type.length == 50
    assert columns["updated_at"]["nullable"] is False
    assert {ix["name"] for ix in inspector.get_indexes("blog_posts")} >= {"ix_blog_posts_title", "ix_blog_posts_author"}
    assert {fk["referred_table"] for fk in inspector.get_foreign_keys("post_tags")} == {"blog_posts", "tags"}


def test_health_is_ok_against_live_services(client):
    response = client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


def test_post_lifecycle_end_to_end(client, es_client, redis_client, session_factory):
    first = create(client, title="Elasticsearch basics", content="How to index documents", author="alice", tags=["Search", "elasticsearch"])
    second = create(client, title="Python tips", content="Using the Elasticsearch client", author="bob", tags=["python", "elasticsearch"])
    assert first["tags"] == ["search", "elasticsearch"]

    # visible in Elasticsearch immediately (refresh=wait_for)
    assert es_client.get(index=TEST_INDEX, id=first["id"])["_source"]["tags"] == ["search", "elasticsearch"]
    response = client.get("/search/", params={"query": "elasticsearch"})
    assert ids(response) == [first["id"], second["id"]]
    hit = response.json()["results"][0]
    assert hit["highlights"]["title"] == ["<em>Elasticsearch</em> basics"]
    assert hit["score"] > response.json()["results"][1]["score"]

    # read-through cache
    assert client.get(f"/posts/{first['id']}").json() == first
    assert redis_client.exists(f"post:{first['id']}") == 1
    assert 0 < redis_client.ttl(f"post:{first['id']}") <= settings.cache_ttl_seconds

    # update invalidates the cache and re-indexes
    updated = client.put(f"/posts/{first['id']}", json={"title": "Renamed", "content": "x", "author": "alice", "tags": ["renamed"]})
    assert updated.status_code == 200, updated.text
    assert redis_client.exists(f"post:{first['id']}") == 0
    assert ids(client.get("/search/", params={"query": "renamed"})) == [first["id"]]
    assert ids(client.get("/search/", params={"query": "basics"})) == []
    assert client.get(f"/posts/{first['id']}").json()["title"] == "Renamed"

    patched = client.patch(f"/posts/{second['id']}", json={"tags": ["python"]})
    assert patched.json()["tags"] == ["python"]
    assert ids(client.get("/search/", params={"tag": "elasticsearch"})) == []

    # tag registry with counts
    tags = {t["name"]: t["post_count"] for t in client.get("/tags/").json()}
    assert tags == {"search": 0, "elasticsearch": 0, "python": 1, "renamed": 1}

    # delete removes the document and the cache entry
    assert client.delete(f"/posts/{first['id']}").status_code == 204
    assert client.get(f"/posts/{first['id']}").status_code == 404
    assert not es_client.exists(index=TEST_INDEX, id=first["id"])
    assert redis_client.exists(f"post:{first['id']}") == 0
    with session_factory() as db:
        assert db.scalar(select(Tag).where(Tag.name == "renamed")) is not None


def test_multi_word_tags_filter_exactly(client):
    ml = create(client, title="ML", tags=["Machine Learning"])
    create(client, title="Other", tags=["machine"])
    assert ids(client.get("/search/", params={"tag": "machine learning"})) == [ml["id"]]
    assert ids(client.get("/search/", params={"tag": "learning"})) == []
    assert [p["id"] for p in client.get("/posts/", params={"tag": "Machine  Learning"}).json()["items"]] == [ml["id"]]


def test_sorting_pagination_and_author_filter(client):
    created = [create(client, title=f"Post {i}", author="alice" if i % 2 else "bob") for i in range(1, 6)]
    all_ids = [p["id"] for p in created]
    assert ids(client.get("/search/", params={"sort": "newest"})) == all_ids[::-1]
    assert ids(client.get("/search/", params={"sort": "oldest"})) == all_ids
    page2 = client.get("/search/", params={"size": 2, "page": 2})
    assert ids(page2) == all_ids[::-1][2:4]
    assert page2.json()["total"] == 5 and page2.json()["pages"] == 3
    assert ids(client.get("/search/", params={"author": "alice", "sort": "oldest"})) == all_ids[0::2]
    listed = client.get("/posts/", params={"size": 2}).json()
    assert [p["id"] for p in listed["items"]] == all_ids[::-1][:2]


def test_reindex_from_database_rebuilds_the_index(client, es_client, session_factory, monkeypatch):
    posts = [create(client, title=f"Reindex {i}", tags=["bulk"]) for i in range(3)]
    search.delete_index(es_client, TEST_INDEX)
    monkeypatch.setattr(migrate, "SessionLocal", session_factory)
    monkeypatch.setattr(migrate, "get_es_client", lambda: es_client)
    assert migrate.reset_search_index() == 3
    assert ids(client.get("/search/", params={"tag": "bulk", "sort": "oldest"})) == [p["id"] for p in posts]
    assert search.mapping_problems(es_client, TEST_INDEX) == []


def test_concurrent_tag_creation_on_postgres(client, es_client, session_factory, monkeypatch):
    with session_factory() as other:
        other.add(Tag(name="race"))
        other.commit()
    monkeypatch.setattr(posts_service, "_existing_tags", lambda _db, _names: {})
    with session_factory() as db:
        post = posts_service.create_post(
            db, es_client, TEST_INDEX, PostCreate(title="t", content="c", author="a", tags=["race", "fresh"])
        )
        assert post.tags == ["race", "fresh"]
        existing_id = db.scalar(select(Tag).where(Tag.name == "race")).id
        assert {tag.name: tag.id for tag in post.tag_relations}["race"] == existing_id
    assert client.get("/tags/race").json()["post_count"] == 1


def test_upgrade_from_pre_alembic_database(engine, es_client):
    """A database created by the original app's ``create_all()`` upgrades cleanly."""
    cfg = alembic_config()
    drop_everything(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE blog_posts (id SERIAL PRIMARY KEY, title VARCHAR(255) NOT NULL, "
            "content TEXT NOT NULL, author VARCHAR(100) NOT NULL, created_at TIMESTAMP)"
        ))
        conn.execute(text("CREATE INDEX ix_blog_posts_id ON blog_posts (id)"))
        conn.execute(text(
            "INSERT INTO blog_posts (title, content, author, created_at) "
            "VALUES ('old', 'legacy row', 'tai', '2025-01-01 00:00:00')"
        ))
    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("blog_posts")}
    assert {"tags", "updated_at"} <= columns
    assert {"tags", "post_tags"} <= set(inspector.get_table_names())
    with engine.connect() as conn:
        created, updated = conn.execute(text("SELECT created_at, updated_at FROM blog_posts")).one()
        assert updated == created

    # and the migration chain is reversible
    command.downgrade(cfg, "base")
    assert "blog_posts" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
