import logging

from fastapi.testclient import TestClient

from app import search
from app.config import settings
from app.main import app
from tests.fakes import FakeElasticsearch


def test_lifespan_creates_the_index(monkeypatch):
    es = FakeElasticsearch()
    monkeypatch.setattr("app.main.get_es_client", lambda: es)
    with TestClient(app):
        pass
    assert es.indices.exists(index=settings.elasticsearch_index)
    assert search.mapping_problems(es, settings.elasticsearch_index) == []


def test_lifespan_tolerates_elasticsearch_being_down(monkeypatch, caplog):
    es = FakeElasticsearch()
    es.available = False
    monkeypatch.setattr("app.main.get_es_client", lambda: es)
    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 200
    assert "unavailable at startup" in caplog.text


def test_lifespan_warns_about_outdated_mapping(monkeypatch, caplog):
    es = FakeElasticsearch()
    es.indices.create(index=settings.elasticsearch_index, mappings={"properties": {"title": {"type": "text"}}})
    monkeypatch.setattr("app.main.get_es_client", lambda: es)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            pass
    assert "outdated mapping" in caplog.text
    assert "--init" in caplog.text


def test_openapi_metadata_and_routes(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Blog Search API"
    assert schema["info"]["version"] == "1.0.0"
    assert {"/posts/", "/posts/{post_id}", "/search/", "/tags/", "/tags/{name}", "/health"} <= set(schema["paths"])
    assert client.get("/docs").status_code == 200


def test_compat_entry_point_exposes_the_same_app():
    import main

    assert main.app is app
