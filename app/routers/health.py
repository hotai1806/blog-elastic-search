from __future__ import annotations

import redis
from elasticsearch import ApiError, Elasticsearch, TransportError
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_es, get_redis
from app.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(
    response: Response,
    db: Session = Depends(get_db),
    es: Elasticsearch = Depends(get_es),
    redis_client: redis.Redis = Depends(get_redis),
) -> HealthOut:
    services: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        services["database"] = "ok"
    except SQLAlchemyError as exc:
        services["database"] = f"error: {type(exc).__name__}"

    try:
        services["elasticsearch"] = "ok" if es.ping() else "error: ping failed"
    except (ApiError, TransportError) as exc:
        services["elasticsearch"] = f"error: {type(exc).__name__}"

    try:
        redis_client.ping()
        services["redis"] = "ok"
    except redis.RedisError as exc:
        services["redis"] = f"error: {type(exc).__name__}"

    healthy = all(value == "ok" for value in services.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(status="ok" if healthy else "degraded", services=services)
