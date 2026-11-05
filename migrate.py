#!/usr/bin/env python
"""Maintenance CLI: database migrations and Elasticsearch index management.

    python migrate.py                # alembic upgrade head
    python migrate.py --init         # migrations, then create the search index if missing
    python migrate.py --sync         # re-index every post from PostgreSQL
    python migrate.py --reset-index  # drop and recreate the index with the current mapping, then re-index
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


def run_migrations() -> None:
    """Upgrade the database schema to the latest Alembic revision."""
    print("Running database migrations...")
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    print("Migrations completed successfully.")


def setup_search_index() -> bool:
    """Create the Elasticsearch index if it does not exist. Returns True if created."""
    print("Setting up Elasticsearch index...")
    created = search.ensure_index(get_es_client(), settings.elasticsearch_index)
    print(
        f"Elasticsearch index {settings.elasticsearch_index!r} "
        + ("created." if created else "already exists.")
    )
    return created


def sync_search_index() -> int:
    """Re-index every post from PostgreSQL. Returns the number of documents indexed."""
    print("Syncing data to Elasticsearch...")
    es = get_es_client()
    index = settings.elasticsearch_index
    search.ensure_index(es, index)
    with SessionLocal() as db:
        posts = db.scalars(
            select(BlogPost)
            .order_by(BlogPost.id)
            .execution_options(yield_per=search.BULK_CHUNK_SIZE)
        )
        count = search.reindex_posts(es, index, posts)
    print(f"Successfully synced {count} posts to Elasticsearch.")
    return count


def reset_search_index() -> int:
    """Drop and recreate the index (picking up mapping changes), then re-index everything."""
    es = get_es_client()
    index = settings.elasticsearch_index
    if search.delete_index(es, index):
        print(f"Deleted Elasticsearch index {index!r}.")
    search.ensure_index(es, index)
    print(f"Recreated Elasticsearch index {index!r}.")
    return sync_search_index()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Database migration and search index utility")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="run migrations and create the search index")
    group.add_argument("--sync", action="store_true", help="re-index all posts into Elasticsearch")
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
