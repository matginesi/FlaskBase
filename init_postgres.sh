#!/usr/bin/env bash
set -euo pipefail

# init_postgres.sh
#
# Default behavior: wipe ONLY the app's role + databases and recreate them.
# It does NOT touch cluster-wide config (pg_hba.conf), and does NOT start/stop services.
#
# Use --all to also:
#   - ensure postgresql service is running
#   - (optionally) add a local dev pg_hba.conf rule for password auth

DB_USER="flaskbase"
DB_PASS="flaskbase"
DB_NAME="flaskbase"
DB_TEST="flaskbase_test"
PG_HOST="127.0.0.1"
PG_PORT="5432"

RESET=1
DO_ALL=0
PATCH_PG_HBA=0

usage() {
  cat <<EOF
Usage: $0 [--no-reset] [--all] [--patch-pg-hba]

  --no-reset       Do not drop/recreate role+db. Only prints connection info.
  --all            Also manages service + optional pg_hba helper for local dev.
  --patch-pg-hba   (only with --all) Add a local rule for 127.0.0.1/32 to allow md5.

Notes:
  - Without flags, it only touches: role '${DB_USER}', db '${DB_NAME}', db '${DB_TEST}'.
  - It never touches other roles/databases.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-reset) RESET=0; shift ;;
    --all) DO_ALL=1; shift ;;
    --patch-pg-hba) PATCH_PG_HBA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1"; usage; exit 2 ;;
  esac
done

log() { echo "[*] $*"; }
die() { echo "[!] $*" >&2; exit 1; }

command -v psql >/dev/null 2>&1 || die "psql not found (install postgresql client)."

ensure_postgres_running() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not found; skipping service checks"
    return 0
  fi
  if systemctl is-active --quiet postgresql; then
    return 0
  fi
  log "Starting postgresql service (requires sudo)..."
  sudo systemctl start postgresql
}

patch_pg_hba_local() {
  if [[ $PATCH_PG_HBA -ne 1 ]]; then
    return 0
  fi
  if [[ $DO_ALL -ne 1 ]]; then
    die "--patch-pg-hba requires --all"
  fi
  # Best-effort for Arch-like defaults. If it fails, user can configure manually.
  local hba="/var/lib/postgres/data/pg_hba.conf"
  if [[ ! -f "$hba" ]]; then
    log "pg_hba.conf not found at $hba; skipping"
    return 0
  fi
  local rule="host    all             all             127.0.0.1/32            md5"
  if grep -qF "$rule" "$hba"; then
    log "pg_hba.conf already contains local md5 rule"
  else
    log "Patching pg_hba.conf (local dev md5 rule) (requires sudo)..."
    echo "$rule" | sudo tee -a "$hba" >/dev/null
    sudo systemctl reload postgresql || true
  fi
}

psql_as_postgres() {
  # shellcheck disable=SC2029
  sudo -iu postgres psql -v ON_ERROR_STOP=1 -qAt "$@"
}

terminate_and_drop_db() {
  local db="$1"
  # terminate sessions
  psql_as_postgres -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${db}' AND pid <> pg_backend_pid();" >/dev/null || true
  # drop
  psql_as_postgres -d postgres -c "DROP DATABASE IF EXISTS \"${db}\";" >/dev/null
}

drop_role() {
  local role="$1"
  psql_as_postgres -d postgres -c "DROP ROLE IF EXISTS \"${role}\";" >/dev/null
}

create_role_and_dbs() {
  psql_as_postgres -d postgres -c "CREATE ROLE \"${DB_USER}\" WITH LOGIN PASSWORD '${DB_PASS}';" >/dev/null
  psql_as_postgres -d postgres -c "CREATE DATABASE \"${DB_NAME}\" OWNER \"${DB_USER}\";" >/dev/null
  psql_as_postgres -d postgres -c "CREATE DATABASE \"${DB_TEST}\" OWNER \"${DB_USER}\";" >/dev/null
}

hardening() {
  local db="$1"
  # basic safe defaults for local dev
  psql_as_postgres -d "$db" -c "REVOKE ALL ON SCHEMA public FROM PUBLIC;" >/dev/null || true
  psql_as_postgres -d "$db" -c "GRANT ALL ON SCHEMA public TO \"${DB_USER}\";" >/dev/null || true
  psql_as_postgres -d "$db" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null || true
}

write_env() {
  local envfile=".env"
  local url="postgresql+psycopg://${DB_USER}:${DB_PASS}@${PG_HOST}:${PG_PORT}/${DB_NAME}"
  local test_url="postgresql+psycopg://${DB_USER}:${DB_PASS}@${PG_HOST}:${PG_PORT}/${DB_TEST}"
  local tmpfile
  tmpfile="$(mktemp)"

  upsert_env_var() {
    local file="$1"
    local key="$2"
    local value="$3"
    if grep -qE "^${key}=" "$file"; then
      sed -E "s#^${key}=.*#${key}=${value}#" "$file" > "$tmpfile"
      mv "$tmpfile" "$file"
    else
      printf "\n%s=%s\n" "$key" "$value" >> "$file"
    fi
  }

  if [[ -f "$envfile" ]]; then
    log "Updating database URLs in $envfile ..."
  else
    log "Creating $envfile with database URLs ..."
    : > "$envfile"
  fi

  upsert_env_var "$envfile" "DATABASE_URL" "$url"
  upsert_env_var "$envfile" "TEST_DATABASE_URL" "$test_url"
}

if [[ $DO_ALL -eq 1 ]]; then
  ensure_postgres_running
  patch_pg_hba_local
fi

if [[ $RESET -eq 1 ]]; then
  log "Wiping app databases/role only (safe default)..."
  terminate_and_drop_db "$DB_NAME"
  terminate_and_drop_db "$DB_TEST"
  drop_role "$DB_USER"

  log "Recreating app role/dbs..."
  create_role_and_dbs
  hardening "$DB_NAME"
  hardening "$DB_TEST"

  write_env
fi

cat <<EOF

╔══════════════════════════════════════════════════════════╗
║ PostgreSQL bootstrap completed                          ║
╠══════════════════════════════════════════════════════════╣
║ DB_USER   ${DB_USER}
║ DB_NAME   ${DB_NAME}
║ DB_TEST   ${DB_TEST}
║ HOST      ${PG_HOST}:${PG_PORT}
║ SCHEMA    public
║ ENV FILE  $(pwd)/.env
╚══════════════════════════════════════════════════════════╝

Next:
  python cli.py init-db-complete --force
  python cli.py serve
EOF
