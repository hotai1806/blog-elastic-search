from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import search
from app.config import settings
from app.database import get_db
from app.deps import get_es, get_redis
from app.main import app
from app.models import Base
from tests.fakes import FakeElasticsearch

INDEX = settings.elasticsearch_index


@pytest.fixture()
def engine(tmp_path):
    """Fresh file-backed SQLite database per test.

    A file (in WAL mode) rather than ``:memory:`` so that the test's own session and the
    application's sessions each get their own connection, like with PostgreSQL.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    # The pysqlite driver only works with SAVEPOINTs when SQLAlchemy drives the
    # transaction itself (official SQLAlchemy recipe).
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_es() -> FakeElasticsearch:
    es = FakeElasticsearch()
    search.ensure_index(es, INDEX)
    return es


@pytest.fixture()
def fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture()
def client(session_factory, fake_es, fake_redis) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_es] = lambda: fake_es
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        # No ``with``: the lifespan (real Elasticsearch) must not run here.
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def make_post(client: TestClient, **overrides) -> dict:
    payload = {
        "title": "Hello world",
        "content": "The first post",
        "author": "tai",
        "tags": ["intro"],
    }
    payload.update(overrides)
    response = client.post("/posts/", json=payload)
    assert response.status_code == 201, response.text
    return response.json()
