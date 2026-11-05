#!/bin/bash
# run_migrations.sh — wait for the backing services, then migrate the database,
# create/upgrade the search index and re-index all posts.

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

echo "Running database migrations and preparing the search index..."
python migrate.py --init

echo "Migrations completed successfully!"
