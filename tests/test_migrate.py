from datetime import datetime

import pytest
from sqlalchemy import select

import migrate
from app import search
from app.config import settings
from app.models import BlogPost
from tests.fakes import FakeElasticsearch


@pytest.fixture()
def cli(monkeypatch, session_factory, fake_es):
    calls = []
    monkeypatch.setattr(migrate, "run_migrations", lambda: calls.append("migrate"))
    monkeypatch.setattr(migrate, "SessionLocal", session_factory)
    monkeypatch.setattr(migrate, "get_es_client", lambda: fake_es)
    with session_factory() as db:
        now = datetime(2025, 3, 3, 12, 0, 0)
        db.add_all([
            BlogPost(id=1, title="One", content="c", author="a", tags=["x"], created_at=now, updated_at=now),
            BlogPost(id=2, title="Two", content="c", author="a", tags=None, created_at=now, updated_at=now),
        ])
        db.commit()
    return calls


def test_default_runs_migrations_only(cli, fake_es):
    assert migrate.main([]) == 0
    assert cli == ["migrate"]
    assert fake_es.documents(settings.elasticsearch_index) == {}


def test_init_migrates_and_creates_index(cli, capsys):
    es = FakeElasticsearch()
    migrate.get_es_client = lambda: es  # noqa: E731 - patched for this test only
    assert migrate.main(["--init"]) == 0
    assert cli == ["migrate"]
    assert es.indices.exists(index=settings.elasticsearch_index)
    assert "created." in capsys.readouterr().out


def test_sync_indexes_every_post(cli, fake_es, capsys):
    assert migrate.main(["--sync"]) == 0
    assert cli == []
    docs = fake_es.documents(settings.elasticsearch_index)
    assert sorted(docs) == ["1", "2"]
    assert docs["2"]["tags"] == []
    assert "Successfully synced 2 posts" in capsys.readouterr().out


def test_reset_index_drops_recreates_and_syncs(cli, fake_es, capsys):
    stale = settings.elasticsearch_index
    fake_es.index(index=stale, id=99, document={"id": 99, "title": "stale"})
    assert migrate.main(["--reset-index"]) == 0
    docs = fake_es.documents(stale)
    assert sorted(docs) == ["1", "2"]
    assert search.mapping_problems(fake_es, stale) == []
    out = capsys.readouterr().out
    assert "Deleted" in out and "Recreated" in out


def test_elasticsearch_errors_exit_nonzero(cli, fake_es, capsys):
    fake_es.available = False
    assert migrate.main(["--sync"]) == 1
    assert "Elasticsearch error" in capsys.readouterr().err


def test_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        migrate.build_parser().parse_args(["--init", "--sync"])
