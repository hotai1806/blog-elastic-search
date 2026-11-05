#!/bin/bash
# run_migrations.sh — wait for the backing services, then migrate the database,
# create/upgrade the search index and re-index all posts.

set -e

# host:port of a URL such as postgresql://user:pass@host:5432/db or http://host:9200
hostport() {
  python -c "import sys; from urllib.parse import urlparse; u = urlparse(sys.argv[1]); print(u.hostname or 'localhost', u.port or sys.argv[2])" "$1" "$2"
}

read -r PG_HOST PG_PORT <<< "$(hostport "${DATABASE_URL:-postgresql://bloguser:blogpassword@postgres:5432/blogdb}" 5432)"
read -r ES_HOST ES_PORT <<< "$(hostport "${ELASTICSEARCH_URL:-http://elasticsearch:9200}" 9200)"

echo "Waiting for PostgreSQL at $PG_HOST:$PG_PORT..."
while ! nc -z "$PG_HOST" "$PG_PORT"; do
  sleep 0.5
done
echo "PostgreSQL is ready!"

echo "Waiting for Elasticsearch at $ES_HOST:$ES_PORT..."
while ! nc -z "$ES_HOST" "$ES_PORT"; do
  sleep 0.5
done
echo "Elasticsearch is ready!"

echo "Running database migrations and preparing the search index..."
python migrate.py --init

echo "Migrations completed successfully!"
