# Blog Search API

A small blog back end built with **FastAPI**: posts live in **PostgreSQL** (the source of
truth), full-text search with relevance ranking, highlighting and filters is served by
**Elasticsearch**, and single-post reads are cached in **Redis**. The repository ships with
Alembic migrations, a maintenance CLI, a Docker Compose stack and a test suite.

```
             ┌─────────────────────┐
  HTTP ────▶ │  FastAPI (app/)     │ ──▶ PostgreSQL   blog_posts, tags, post_tags   (source of truth)
             │  posts / search /   │ ──▶ Elasticsearch  blog_posts index           (written inside the DB transaction)
             │  tags / health      │ ──▶ Redis          post:{id}, 1 h TTL          (best-effort read cache)
             └─────────────────────┘
  python migrate.py  ──▶ Alembic migrations · create the index · bulk re-index from PostgreSQL
```

## Quick start

```bash
docker compose up --build
```

The `app` container waits for the three services, runs `python migrate.py --init`
(Alembic `upgrade head`, search index creation or upgrade, full re-index) and then starts
Uvicorn on <http://localhost:8000>. Interactive docs: <http://localhost:8000/docs>
(Swagger) and <http://localhost:8000/redoc>.

```bash
# create a post
curl -s -X POST localhost:8000/posts/ -H 'content-type: application/json' \
  -d '{"title":"Searching blogs with Elasticsearch","content":"Full-text search made simple.","author":"tai","tags":["Elasticsearch","Python"]}'

# search it (title matches are boosted; matches are highlighted with <em>)
curl -s 'localhost:8000/search/?query=elasticsearch&tag=python'

# browse the newest posts with a given tag, straight from PostgreSQL
curl -s 'localhost:8000/posts/?tag=python&size=5'

# tags with usage counts
curl -s 'localhost:8000/tags/?sort=posts'
```

### Running without Docker

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # point DATABASE_URL / ELASTICSEARCH_URL / REDIS_URL at your services
python migrate.py --init      # migrations + search index + re-index
uvicorn app.main:app --reload
```

## API

All list endpoints take a 1-based `page` (default 1) and a `size` of 1–100 (default 10)
and return `total` and `pages`. Timestamps are ISO 8601 in UTC (no offset suffix).

| Method | Path | Body / query parameters | Response |
|--------|------|-------------------------|----------|
| `POST` | `/posts/` | `{title, content, author, tags?: [..]}` | `201` post |
| `GET` | `/posts/` | `page`, `size`, `author`, `tag` (repeatable, AND; newest first) | `{items, total, page, size, pages}` |
| `GET` | `/posts/{id}` | — | post (served from Redis when cached) |
| `PUT` | `/posts/{id}` | `{title, content, author, tags?}` — full replacement; omitting `tags` clears them | post |
| `PATCH` | `/posts/{id}` | any subset of `{title, content, author, tags}`; `tags` replaces the whole set | post |
| `DELETE` | `/posts/{id}` | — | `204` |
| `GET` | `/search/` | `query`, `author`, `tag` (repeatable, AND), `sort`, `page`, `size` | `{results, total, page, size, pages, query}` |
| `GET` | `/tags/` | `sort=name` (default) or `sort=posts` | `[{id, name, description, created_at, post_count}]` |
| `GET` | `/tags/{name}` | — | tag |
| `PATCH` | `/tags/{name}` | `{description}` (`null` clears it) | tag |
| `GET` | `/health` | — | `200` or `503` with per-service status |

A post looks like:

```json
{
  "id": 1,
  "title": "Searching blogs with Elasticsearch",
  "content": "Full-text search made simple.",
  "author": "tai",
  "tags": ["elasticsearch", "python"],
  "created_at": "2026-11-05T10:15:30.123456",
  "updated_at": "2026-11-05T10:15:30.123456"
}
```

Validation: `title` 1–255 characters, `author` 1–100, `content` non-empty, at most 20
tags of up to 50 characters each. Leading/trailing whitespace is stripped. Errors come
back as FastAPI's standard `422` body; unknown posts/tags are `404`; if Elasticsearch
cannot be reached, writes and searches answer `503` (see below).

### Tags

Tag names are normalised on every input (request bodies, `tag=` filters and `/tags/{name}`):
trimmed, internal whitespace collapsed, lower-cased and de-duplicated, so `"Machine  Learning"`
and `"machine learning"` are the same tag. A blank tag is rejected with `422` everywhere.
On both `GET /posts/` and `GET /search/` the `tag` parameter can be repeated
(`?tag=python&tag=search`) and every listed tag is required. Tags are created on first use, keep their
`description` and stay listed (with `post_count: 0`) after the last post using them is
removed. Each tag is also stored as a keyword in the search index, so `tag=` filters are exact.

### Search

`GET /search/` runs a `multi_match` query over `title` (weight 2) and `content`, with
`author` and `tag` as exact filters.

| Parameter | Meaning |
|-----------|---------|
| `query` | Full-text query. Blank/omitted ⇒ browse by filters only. |
| `author` | Exact author filter. |
| `tag` | Exact tag filter; repeat for AND (`?tag=python&tag=search`). |
| `sort` | `relevance` (default; `newest` is used when there is no query), `newest`, `oldest`. |
| `page`, `size` | Pagination; `page × size` may not exceed 10 000 (Elasticsearch's result window). |

Each result is a post plus `score` (null when not sorting by relevance) and `highlights`,
e.g. `{"title": ["Searching blogs with <em>Elasticsearch</em>"], "content": [...]}`.

## Consistency, caching and failure behaviour

- **PostgreSQL is authoritative.** On create/update/delete the Elasticsearch write happens
  *inside* the database transaction (`flush → index → commit`, with `refresh=wait_for`), so a
  post is searchable as soon as the response returns, and if Elasticsearch is down the
  transaction is rolled back and the client gets `503` — nothing is persisted on one side only.
  Update and delete endpoints take a row lock (`SELECT … FOR UPDATE`), so concurrent writers
  to the same post queue up and each one re-indexes the state it actually committed. If a
  commit fails after the index write, `python migrate.py --sync` reconciles the index
  (re-indexes every row and deletes documents that have no row).
- **The index is never auto-created by a write.** The first write of a process checks that
  the index exists with the explicit mapping (keyword `author`/`tags`), so a start-up while
  Elasticsearch was down cannot leave you with Elasticsearch's dynamic mapping and silently
  broken filters.
- **Redis is best effort.** `GET /posts/{id}` reads through the cache (`post:{id}`, TTL
  `CACHE_TTL_SECONDS`). Readers only fill an absent key (`SET NX`), updates write the fresh
  post through, and deletes leave a tombstone for the cache lifetime, so a slow reader that
  fetched the row just before a write cannot leave a stale or deleted post in the cache. Any
  Redis error is logged and the request is served from PostgreSQL. Remaining window: a
  writer that stalls between its commit and its cache write while a later writer completes
  both could leave the earlier value until the TTL expires.
- **Startup never blocks on Elasticsearch.** The app creates the index if it is missing; if
  Elasticsearch is unreachable it logs a warning and keeps serving the database-backed
  endpoints (search and post writes return `503` until it is back). If the live index has an
  outdated mapping the log says so; `python migrate.py --init` (or `--sync`) upgrades it.
- `GET /health` pings all three services (with short timeouts) and returns `503` with
  `status: "degraded"` when any of them fails.

## Database migrations and the maintenance CLI

| Command | What it does |
|---------|--------------|
| `python migrate.py` | `alembic upgrade head` |
| `python migrate.py --init` | migrations; create the search index or upgrade its mapping; full re-index |
| `python migrate.py --sync` | upgrade the mapping if needed, bulk re-index every post, delete documents with no row |
| `python migrate.py --reset-index` | drop and recreate the index with the current mapping, then re-index |

Mapping upgrades add missing fields in place; a field whose type changed cannot be altered
by Elasticsearch, so the index is rebuilt and re-indexed from PostgreSQL. Run `--sync` and
`--reset-index` while the app is not taking writes: a post updated while the re-index streams
the table may be overwritten with the older snapshot until its next update.

Plain Alembic works too (`alembic upgrade head`, `alembic downgrade -1`,
`alembic revision -m "..."`); `migrations/env.py` reads `DATABASE_URL`. Migration `001` is
idempotent for databases that were created by the original one-file app before Alembic
existed, so `python migrate.py` upgrades those in place.

### Upgrading an existing deployment

This version changes the HTTP contract and the search mapping:

- `POST /posts/` and `PUT /posts/{id}` take a JSON body instead of query-string parameters
  and return the post itself (`201` on create) instead of `{"message", "post"}`.
- The hand-written `/docs` route is gone; FastAPI's built-in `/docs`, `/redoc` and
  `/openapi.json` replace it.
- `GET /search/` now returns `{results, total, page, size, pages, query}` instead of
  `{results, count}`, and `query` is optional.
- The index mapping now includes `id`, `tags` (keyword) and `updated_at`.
  `python migrate.py --init` (run automatically by the Docker image) extends an old mapping
  in place and re-indexes every post; an index whose fields were auto-created with the wrong
  type is rebuilt. Documents indexed by the old version remain searchable in the meantime.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://bloguser:blogpassword@postgres:5432/blogdb` | SQLAlchemy/psycopg2 URL |
| `ELASTICSEARCH_URL` | `http://elasticsearch:9200` | Elasticsearch endpoint |
| `ELASTICSEARCH_INDEX` | `blog_posts` | Index name |
| `REDIS_URL` | `redis://redis:6379` | Redis endpoint |
| `CACHE_TTL_SECONDS` | `3600` | Post cache lifetime |
| `LOG_LEVEL` | `INFO` | Python logging level |

A `.env` file in the working directory is loaded automatically (`.env.example` lists the
variables); `docker-compose.yml` sets them for the stack.

## Tests

```bash
pytest                       # unit/API tests: SQLite + in-memory fakes, no services needed
```

The integration tests run the real migrations, Elasticsearch queries and Redis cache
through the HTTP API. They need live services and are skipped unless these variables are
set. **They drop and recreate the `blog_posts`, `tags`, `post_tags` and `alembic_version`
tables in the target database and use the `blog_posts_integration_test` index — point them
at a scratch database.**

```bash
INTEGRATION_DATABASE_URL=postgresql://bloguser:blogpassword@localhost:5432/blogdb \
INTEGRATION_ELASTICSEARCH_URL=http://localhost:9200 \
INTEGRATION_REDIS_URL=redis://localhost:6379/1 \
pytest -m integration tests/integration
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs the unit tests on every push
and the integration tests against PostgreSQL, Elasticsearch and Redis service containers.

## Project layout

```
app/
  main.py            application factory, lifespan (index bootstrap), error handlers
  config.py          settings from the environment
  database.py        engine / session / get_db dependency
  deps.py            lazily created Elasticsearch and Redis clients
  models.py          SQLAlchemy models (BlogPost, Tag, post_tags)
  schemas.py         Pydantic request/response models, tag normalisation
  search.py          index mapping, write-through, query builder, bulk re-index
  cache.py           best-effort Redis cache helpers
  services/          posts.py (transactions + Elasticsearch), tags.py
  routers/           posts.py, search.py, tags.py, health.py
migrations/          Alembic environment and versions 001–003
migrate.py           maintenance CLI (see above)
main.py              compatibility shim so `uvicorn main:app` still works
tests/               unit/API tests (fakes.py holds the in-memory Elasticsearch) and tests/integration/
docker-compose.yml   app + PostgreSQL 15 + Elasticsearch 8.10 + Redis 7
docs/superpowers/specs/  design notes and the assumptions behind this implementation
```

### Design notes

`docs/superpowers/specs/2026-11-05-blog-search-completion-design.md` records the design and
the assumptions it rests on. The main ones: PostgreSQL stays the source of truth with
Elasticsearch as a derived index; tags are kept both in the `tags`/`post_tags` tables (the
registry) and in the denormalised `blog_posts.tags` array that the API and the index read;
timestamps stay naive UTC as in the original schema; no authentication is included.
