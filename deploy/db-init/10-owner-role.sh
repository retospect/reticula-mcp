#!/bin/sh
# Runs once, when the database container starts on an empty data directory
# (the image runs every file in /docker-entrypoint-initdb.d then), as the
# superuser named by POSTGRES_USER (reticula_admin in compose.yaml).
#
# It makes the role the server connects as, `reticula`, without superuser
# rights, and the database `reticula` owned by it. The three extensions the
# schema uses are created here, by the superuser, so the server's first
# migration finds them and needs no rights it does not have.
set -eu

: "${RETICULA_DB_PASSWORD:?RETICULA_DB_PASSWORD is not set for the database container}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    --set owner_password="$RETICULA_DB_PASSWORD" <<'SQL'
CREATE ROLE reticula LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'owner_password';
CREATE DATABASE reticula OWNER reticula;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname reticula <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
