"""FastAPI application factory and lifespan."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import search
from app.config import settings
from app.deps import get_es_client
from app.routers import health, posts, tags
from app.routers import search as search_router

logger = logging.getLogger(__name__)

DESCRIPTION = """
Create, read, update and delete blog posts stored in PostgreSQL, search them with
Elasticsearch (relevance ranking, highlighting, author/tag filters) and serve single
posts from a Redis cache.
"""


class SafeJSONResponse(JSONResponse):
    """JSON rendering that cannot fail on lone surrogates echoed back from client input."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Make sure the search index exists; a missing Elasticsearch must not stop the app."""
    index = settings.elasticsearch_index
    try:
        es = get_es_client()
        if not search.ensure_index(es, index):
            problems = search.mapping_problems(es, index)
            if problems:
                logger.warning(
                    "Elasticsearch index %r has an outdated mapping (%s). "
                    "Run `python migrate.py --init` to upgrade it.",
                    index,
                    "; ".join(problems),
                )
    except search.SearchBackendError as exc:
        logger.warning(
            "Elasticsearch is unavailable at startup (%s). Search and post writes will "
            "answer 503 until it is reachable; run `python migrate.py --init` afterwards.",
            exc,
        )
    yield


def create_app() -> FastAPI:
    logging.basicConfig(level=settings.log_level)
    app = FastAPI(
        title="Blog Search API",
        version="1.0.0",
        description=DESCRIPTION.strip(),
        lifespan=lifespan,
    )
    app.include_router(posts.router)
    app.include_router(search_router.router)
    app.include_router(tags.router)
    app.include_router(health.router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Same body as FastAPI's default handler, rendered so that invalid input (e.g. a lone
        # UTF-16 surrogate in a JSON string) is reported as 422 instead of crashing with 500.
        return SafeJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(search.SearchBackendError)
    async def search_backend_unavailable(_: Request, exc: search.SearchBackendError) -> JSONResponse:
        logger.error("Elasticsearch request failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "Search backend unavailable; try again later"},
        )

    return app


app = create_app()
