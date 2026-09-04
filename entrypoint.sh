#!/bin/sh
set -e

echo "Waiting for postgres to become available..."
while ! nc -z "$DB_HOSTNAME" "$DB_PORT"; do
    sleep 1
done

echo "PostgreSQL started"
exec "$@"
