#!/bin/bash
# run_migrations.sh

set -e

echo "Waiting for PostgreSQL to be ready..."
while ! nc -z postgres 5432; do
  sleep 0.5
done
echo "PostgreSQL is ready!"

echo "Waiting for Elasticsearch to be ready..."
while ! nc -z elasticsearch 9200; do
  sleep 0.5
done
echo "Elasticsearch is ready!"

echo "Running database migrations..."
python migrate.py --init

echo "Syncing data to Elasticsearch..."
python migrate.py --sync

echo "Migrations completed successfully!"