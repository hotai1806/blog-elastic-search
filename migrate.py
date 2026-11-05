#!/usr/bin/env python
"""Maintenance CLI: database migrations and Elasticsearch index management.

    python migrate.py                # alembic upgrade head
    python migrate.py --init         # migrations, create/upgrade the search index, full re-index
    python migrate.py --sync         # re-index every post and remove documents with no database row
    python migrate.py --reset-index  # drop and recreate the index with the current mapping, then re-index

Elasticsearch is a derived copy of PostgreSQL, so every command here is safe to repeat.
Run --sync / --reset-index while the application is not taking writes: a post updated
while the re-index streams the table may be overwritten with the older snapshot until its
next update (deletes made meanwhile are reconciled away).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app import search
from app.config import settings
from app.database import SessionLocal
from app.deps import get_es_client
from app.models import BlogPost

ALEMBIC_INI = Path(__file__).resolve().with_name("alembic.ini")


def alembic_config(database_url: Optional[str] = None) -> Config:
    """Alembic configuration pointing at ``database_url`` (default: the app settings)."""
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = database_url or settings.database_url
    return config


def run_migrations(database_url: Optional[str] = None) -> None:
    """Upgrade the database schema to the latest Alembic revision."""
    print("Running database migrations...")
    command.upgrade(alembic_config(database_url), "head")
    print("Migrations completed successfully.")


def setup_search_index() -> str:
    """Create the index or bring its mapping up to date. Returns the ``prepare_index`` status."""
    print("Setting up Elasticsearch index...")
    index = settings.elasticsearch_index
    status = search.prepare_index(get_es_client(), index)
    messages = {
        "created": f"Elasticsearch index {index!r} created.",
        "ok": f"Elasticsearch index {index!r} already exists with the current mapping.",
        "extended": f"Elasticsearch index {index!r} mapping extended with new fields.",
        "rebuilt": f"Elasticsearch index {index!r} had an incompatible mapping and was rebuilt.",
    }
    print(messages[status])
    return status


def sync_search_index(*, prepare: bool = True) -> tuple[int, int]:
    """Re-index every post from PostgreSQL and delete documents that have no row.

    Returns ``(documents indexed, stale documents removed)``.
    """
    print("Syncing data to Elasticsearch...")
    es = get_es_client()
    index = settings.elasticsearch_index
    if prepare:
        search.prepare_index(es, index)
    with SessionLocal() as db:
        posts = db.scalars(
            select(BlogPost)
            .order_by(BlogPost.id)
            .execution_options(yield_per=search.BULK_CHUNK_SIZE)
        )
        count = search.reindex_posts(es, index, posts)
        # Queried after the bulk so posts deleted during the sync are reconciled away too.
        keep_ids = set(db.scalars(select(BlogPost.id)))
    removed = search.remove_documents(es, index, search.stale_document_ids(es, index, keep_ids))
    print(f"Successfully synced {count} posts to Elasticsearch ({removed} stale document(s) removed).")
    return count, removed


def reset_search_index() -> tuple[int, int]:
    """Drop and recreate the index (picking up mapping changes), then re-index everything."""
    index = settings.elasticsearch_index
    search.rebuild_index(get_es_client(), index)
    print(f"Recreated Elasticsearch index {index!r}.")
    return sync_search_index(prepare=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Database migration and search index utility")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--init",
        action="store_true",
        help="run migrations, create or upgrade the search index, then re-index all posts",
    )
    group.add_argument(
        "--sync",
        action="store_true",
        help="re-index all posts into Elasticsearch and remove documents without a database row",
    )
    group.add_argument(
        "--reset-index",
        action="store_true",
        help="drop and recreate the search index with the current mapping, then re-index",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=settings.log_level)
    args = build_parser().parse_args(argv)
    try:
        if args.init:
            run_migrations()
            setup_search_index()
            sync_search_index(prepare=False)
        elif args.sync:
            sync_search_index()
        elif args.reset_index:
            reset_search_index()
        else:
            run_migrations()
    except search.SearchBackendError as exc:
        print(f"Elasticsearch error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
