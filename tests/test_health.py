import fakeredis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.deps import get_redis
from app.main import app


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "services": {"database": "ok", "elasticsearch": "ok", "redis": "ok"},
    }


def test_health_reports_elasticsearch_down(client, fake_es):
    fake_es.available = False
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["elasticsearch"] == "error: ping failed"
    assert body["services"]["database"] == "ok"


def test_health_reports_redis_down(client):
    server = fakeredis.FakeServer()
    server.connected = False
    app.dependency_overrides[get_redis] = lambda: fakeredis.FakeRedis(server=server)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["services"]["redis"].startswith("error:")


def test_health_reports_database_down(client):
    broken = sessionmaker(bind=create_engine("postgresql://nobody:nothing@127.0.0.1:1/nodb"))

    def broken_db():
        session = broken()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = broken_db
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["services"]["database"].startswith("error:")
