# Blog Search API — completion design

Date: 2026-11-05
Status: implemented under stated assumptions (autonomous session; no interactive approval was possible)

## 1. Intent and current state

The repository is a small blog service: FastAPI + PostgreSQL (source of truth) +
Elasticsearch (full-text search) + Redis (read cache), run with `docker compose`.

What exists today:

- `main.py`: one-file app with `POST /posts/`, `GET /posts/{id}` (Redis-cached),
  `PUT /posts/{id}`, `GET /search/`, and a hand-rolled `/docs` route. It declares its
  own `BlogPost` model (no tags) and calls `create_all()` at import time.
- `models.py`: a richer, **unused** model set (`BlogPost.tags` ARRAY column, `Tag`
  table, `post_tags` link table).
- Alembic migrations `001` (blog_posts) and `002` (tags ARRAY + `tags` + `post_tags`),
  with `migrations/env.py` importing `Base` from `main` (so Alembic never sees `Tag`).
- `migrate.py` (`--init` runs Alembic + creates the ES index; `--sync` re-indexes).
- No tests, empty README, no delete/list endpoints, no cache invalidation on update,
  two different ES mappings (one in `main.py`, one in `migrate.py`).

"Complete this repo" is read as: **make this a coherent, working, tested, documented
service that uses the schema it already ships**, not as adding unrelated product features.

## 2. Assumptions (made because no clarification was possible)

| # | Assumption | Why |
|---|-----------|-----|
| A1 | PostgreSQL remains the source of truth; Elasticsearch is a derived index; Redis is a best-effort cache. | Matches the existing architecture and `migrate.py --sync`. |
| A2 | Tags are modelled with the `Tag`/`post_tags` tables as the canonical registry **and** the `blog_posts.tags` ARRAY column kept in sync as a denormalised copy (used for fast reads and the ES document). | Both exist in migration `002` and in `models.py`; dropping either would be a schema decision the author did not make. One function owns the sync. |
| A3 | Tag names are normalised: trimmed, internal whitespace collapsed, lower-cased, de-duplicated, 1–50 chars, at most 20 per post. | Keeps `tags.name UNIQUE` meaningful and filters predictable. |
| A4 | Create/update endpoints take a JSON body (Pydantic) instead of query-string parameters, and return the post resource directly (`201` on create) instead of `{"message", "post"}`. | Query-string bodies cannot carry tag lists or long content safely; resource responses are the FastAPI/REST norm. This is a breaking change for any existing client and is documented in the README. |
| A5 | A `blog_posts.updated_at` column is added (migration `003`). | An update endpoint without a modification timestamp is incomplete. |
| A6 | Existing pins in `requirements.txt` are kept (FastAPI 0.103.1, SQLAlchemy 2.0.20, elasticsearch 8.9.0, redis 4.6.0, Alembic 1.12.0, Pydantic 2.3.0) and the Docker image stays on Python 3.11. | They are the author's choice and are mutually compatible; verified locally on Python 3.11. |
| A7 | No authentication, authorisation, or rate limiting. | Out of scope for the current repo; nothing in it hints at it. |
| A8 | Timestamps are naive UTC (`DateTime` without timezone), as in the existing schema. | Changing column types is out of scope. Documented in the README. |

## 3. Architecture

```
client ──HTTP──▶ FastAPI app (app/)
                   ├─ routers/posts   ──▶ services/posts ──▶ PostgreSQL (SQLAlchemy)
                   │                                     ├─▶ Elasticsearch (index/delete, refresh=wait_for)
                   │                                     └─▶ Redis (write-through / tombstone post:{id})
                   ├─ routers/search  ──▶ search.py      ──▶ Elasticsearch (_search)
                   ├─ routers/tags    ──▶ services/tags  ──▶ PostgreSQL
                   └─ routers/health  ──▶ DB / ES / Redis pings
migrate.py ──▶ Alembic upgrade head, ES index creation, bulk re-index
```

### 3.1 Package layout

```
app/
  __init__.py
  config.py        Settings read from environment (.env supported via python-dotenv)
  database.py      engine, SessionLocal, get_db()
  deps.py          get_es(), get_redis() FastAPI dependencies (lazy singletons)
  models.py        SQLAlchemy 2.0 models: BlogPost, Tag, post_tags
  schemas.py       Pydantic v2 request/response models
  search.py        index name/mapping, ensure_index, index_post, delete_post,
                   build_search_query (pure), search_posts, reindex_all
  cache.py         get / set-if-absent / write-through / tombstone (best effort)
  services/posts.py  create/get/list/update/patch/delete + tag sync
  services/tags.py   list/get/update tags with post counts
  routers/{posts,search,tags,health}.py
  main.py          create_app(), lifespan (ensure index, tolerant), router wiring
main.py            2-line shim: `from app.main import app` (keeps `uvicorn main:app` working)
migrate.py         CLI: --init | --sync | --reset-index | (default: migrate only)
migrations/        Alembic env imports app.models; versions 001, 002, 003
tests/             unit/API tests (SQLite + in-memory fakes) and env-gated integration tests
```

Top-level `models.py` is removed (moved to `app/models.py`).

### 3.2 Data model (`app/models.py`)

- `BlogPost`: `id`, `title` (255, indexed), `content` (Text), `author` (100, indexed),
  `created_at`, `updated_at` (naive UTC, python-side default/onupdate), `tags`
  (`ARRAY(String(50))` on PostgreSQL, `JSON` variant on SQLite for tests),
  `tag_relations` (many-to-many to `Tag`, ordered by name).
- `Tag`: `id`, `name` (50, unique), `description` (255, nullable), `created_at`,
  `posts` back-reference.
- `post_tags`: link table with `ON DELETE CASCADE` both ways.
- `Base.metadata.create_all()` is **not** called by the app; Alembic owns the schema.

### 3.3 Migrations

- `migrations/env.py` imports `app.models.Base` and takes the URL from `DATABASE_URL`.
- `001`: unchanged semantics; the deprecated `Inspector.from_engine` call becomes
  `sa.inspect(bind)` and index creation is made idempotent.
- `002`: unchanged.
- `003_add_updated_at_to_posts`: backfills NULL `created_at` values (possible on
  pre-Alembic tables) and makes the column `NOT NULL DEFAULT now()`, then adds
  `blog_posts.updated_at TIMESTAMP NOT NULL DEFAULT now()` backfilled from `created_at`;
  downgrade drops `updated_at`.

### 3.4 API

All list endpoints are 1-based `page` (default 1) with `size` 1–100 (default 10).

| Method | Path | Body / params | Response |
|--------|------|---------------|----------|
| POST | `/posts/` | `PostCreate {title, content, author, tags?}` | `201 PostOut` |
| GET | `/posts/` | `page, size, author?, tag*` (repeatable, AND; newest first) | `PostList {items, total, page, size, pages}` |
| GET | `/posts/{id}` | — | `PostOut` (served from Redis when cached) |
| PUT | `/posts/{id}` | `PostUpdate` (all fields; `tags` omitted ⇒ `[]`) | `PostOut` |
| PATCH | `/posts/{id}` | `PostPatch` (any subset; `tags` present ⇒ replaced) | `PostOut` |
| DELETE | `/posts/{id}` | — | `204` |
| GET | `/search/` | `query?, author?, tag* (repeatable, AND), page, size, sort=relevance|newest|oldest` | `SearchResponse {results[{…PostOut, score, highlights}], total, page, size, pages, query}` |
| GET | `/tags/` | `sort=name|posts` | `list[TagOut {id, name, description, created_at, post_count}]` |
| GET | `/tags/{name}` | — | `TagOut` |
| PATCH | `/tags/{name}` | `TagPatch {description}` | `TagOut` |
| GET | `/health` | — | `200/503 HealthOut {status, services{database, elasticsearch, redis}}` |

- `PostOut`: `id, title, content, author, tags, created_at, updated_at`.
- `query` is optional on `/search/`: without it the endpoint browses by filters
  (sorted newest unless told otherwise); `sort=relevance` without `query` falls back
  to `newest`. `page*size` beyond 10 000 hits is rejected with `422` (ES result window).
- Validation errors are FastAPI's default `422` body (`detail` is a list), including the
  result-window check and a blank `tag` filter value. Unknown post/tag ⇒ `404`.
- The hand-rolled `/docs` route is deleted; FastAPI's built-in `/docs`, `/redoc`
  and `/openapi.json` carry the title/version/description.

### 3.5 Elasticsearch (`app/search.py`)

- One index (`ELASTICSEARCH_INDEX`, default `blog_posts`), 1 shard / 0 replicas,
  explicit mapping: `id integer, title text (+ .keyword), content text, author keyword,
  tags keyword, created_at date, updated_at date`.
- Document = `PostOut` fields. Writes use `refresh="wait_for"` so a post is
  searchable as soon as the HTTP response returns.
- `build_search_query(query, author, tags, sort, page, size)` is a pure function
  returning the kwargs for `Elasticsearch.search` (`query`, `sort`, `from_`, `size`,
  `highlight`, `track_total_hits=True`):
  - `bool.must`: `multi_match` on `title^2, content` (or `match_all` without `query`);
  - `bool.filter`: `term author`, one `term tags` per requested tag;
  - sort: relevance ⇒ `_score desc, created_at desc`; newest ⇒ `created_at desc`;
    oldest ⇒ `created_at asc`;
  - highlight on `title` and `content`.
- Failures (`elasticsearch.ApiError`, `elasticsearch.TransportError`) map to `503`.
- Writes never rely on Elasticsearch's auto-create: the first write of a process verifies
  (and if needed creates) the index with the explicit mapping, memoised per client.
  `ensure_index` treats a concurrent `resource_already_exists_exception` as success.
- `prepare_index` (used by `migrate.py`) adds missing fields with `put_mapping` and
  rebuilds the index when a field's type conflicts (Elasticsearch cannot change types).
- `SearchHit` tolerates documents indexed by the previous version (no `updated_at`,
  no `tags`) so search keeps working until they are re-indexed.

### 3.6 Write-path consistency

PostgreSQL is authoritative, so the ES write happens **inside** the DB transaction:

```
begin → mutate ORM objects → flush (ids/defaults assigned)
      → ES index/delete (refresh=wait_for)
      → commit → refresh Redis entry (write-through) or store a delete tombstone
```

If the ES call fails the session is rolled back and the client gets `503`; nothing
is persisted. Update and delete endpoints load the row with `SELECT … FOR UPDATE`, so
concurrent writers to one post are serialised and each re-indexes the state it commits
(and the tags ARRAY column cannot drift from `post_tags`). If the commit itself fails
after a successful ES write, `python migrate.py --sync` reconciles the index: it re-indexes
every row and deletes documents whose row does not exist.

### 3.7 Cache (`app/cache.py`)

- Key `post:{id}`, value = `PostOut` JSON, TTL `CACHE_TTL_SECONDS` (default 3600).
- `GET /posts/{id}` reads through and fills the cache only if the key is absent (`SET NX`);
  `PUT/PATCH` write the fresh post through after commit; `DELETE` stores a tombstone for the
  cache lifetime. A slow reader therefore cannot re-populate a stale or deleted post.
- Every Redis error is caught and logged; the request proceeds against the DB.

### 3.8 `migrate.py`

| Flag | Action |
|------|--------|
| (none) | Alembic `upgrade head` |
| `--init` | upgrade; create the index or upgrade its mapping (`prepare_index`); full re-index |
| `--sync` | `prepare_index`; chunked `bulk` re-index of every post; delete documents with no row |
| `--reset-index` | delete the index, recreate with the current mapping, then re-index |

Alembic is invoked through its Python API (`alembic.command.upgrade`) with the database URL
passed via `config.attributes["database_url"]` (precedence: programmatic > `DATABASE_URL` >
`alembic.ini`), not a subprocess. `--sync`/`--reset-index` are documented to run while the
app is not taking writes.

### 3.9 Configuration

`DATABASE_URL`, `ELASTICSEARCH_URL`, `ELASTICSEARCH_INDEX`, `REDIS_URL`,
`CACHE_TTL_SECONDS`, `LOG_LEVEL`. Defaults match `docker-compose.yml`. A `.env`
file is loaded if present; `.env.example` documents the variables.

### 3.10 Docker

- `Dockerfile` gets a default `CMD` (migrations then uvicorn) and a `.dockerignore`.
- `docker-compose.yml`: obsolete `version:` key removed, `depends_on` uses
  `condition: service_healthy`, app command becomes `uvicorn app.main:app`.
- `run_migrations.sh` is kept (belt-and-braces readiness wait).

## 4. Testing strategy

1. **Unit/API tests (always run, no services needed)** — `pytest`:
   SQLite in-memory engine (`StaticPool`), an in-memory `FakeElasticsearch` that
   implements the subset used by the app (`indices.exists/create/delete/refresh`,
   `index`, `delete` via `options(ignore_status=404)`, `search` with
   `bool/multi_match/term/match_all`, sort, paging, highlight, `ping`), and
   `fakeredis.FakeRedis`. Dependencies are overridden on the FastAPI app.
   Coverage: every endpoint's happy path and error path, tag normalisation and
   sync, cache read-through + write-through/tombstone rules, ES-failure rollback, lifespan
   tolerance when ES is down, `build_search_query` exact DSL, `/health` degraded.
2. **Integration tests (opt-in)** — skipped unless `INTEGRATION_DATABASE_URL`,
   `INTEGRATION_ELASTICSEARCH_URL` and `INTEGRATION_REDIS_URL` are set. They run
   Alembic `upgrade head` on a real PostgreSQL, exercise the real Elasticsearch
   (dedicated test index) and Redis through the HTTP API, and `downgrade base` on
   teardown. They were executed in this session against PostgreSQL 16,
   Elasticsearch 8.10.1 and Redis 7.
3. **CI** — GitHub Actions workflow running unit tests, then integration tests
   against service containers (postgres, elasticsearch, redis).

## 5. Out of scope

Authentication, pagination of `/tags/`, deleting tags, search-result caching,
async database drivers, Elasticsearch aliases/zero-downtime reindex, changing
timestamp column types.

## 6. Revision history

- **2026-11-05, after review.** A three-lens design critique (35 issues, 11 verified
  before the session's usage limit hit) and a two-lens code review (8 findings, all
  reproduced against live services) led to: lazy, mapping-safe index creation on the
  write path and mapping upgrade/rebuild in `migrate.py`; row locks on update/delete;
  race-free cache rules (`SET NX` reads, write-through updates, delete tombstones);
  reconciliation of orphan documents in `--sync`; repeatable `tag` on `GET /posts/`;
  blank `tag` filters rejected with FastAPI's `422` shape; legacy-document tolerance in
  search results; migration `003` hardened for NULL `created_at`; `exec uvicorn` for
  signal handling; longer Elasticsearch health start periods; bounded `/health` pings;
  Alembic URL precedence and `disable_existing_loggers=False`.
- **2026-11-05, final review round** (live Elasticsearch, API contract, tests/ops/docs
  lenses; 10 findings upheld): NUL characters rejected at the API boundary; upper bound on
  `page`; safe rendering of validation errors containing lone surrogates; string-typed
  Elasticsearch error bodies translated to 503; searches create a missing index instead of
  answering 503 forever; `unmapped_type` on sort clauses and a `_doc` tiebreaker in the
  reconciliation scan; `%` escaped in the Alembic URL; bounded PostgreSQL connect timeout;
  readiness waits derived from the configured URLs; blank tag descriptions clear the field;
  the row-lock integration test drives the HTTP endpoints.
