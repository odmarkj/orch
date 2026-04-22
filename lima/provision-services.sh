#!/bin/bash
# Idempotent provisioning for shared native services in the orch VM.
#
# Installs PostgreSQL 17 + pgvector and Redis, enables them, and restores
# dumps from data-dumps/ if present. Safe to re-run against existing VMs —
# each step is a no-op when already satisfied.
#
# Invoked from lima/orch.yaml at VM creation. Run manually against an
# existing VM with:
#     limactl shell orch -- sudo bash /Users/joshuaodmark/Apps/orch/lima/provision-services.sh
#
# Requires root (uses sudo internally for user-context commands).

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { printf '[provision-services] %s\n' "$*"; }

# ── PostgreSQL 17 (pgdg repo — pg 17 is not in Ubuntu 24.04 default repos) ──
if ! dpkg -s postgresql-17 >/dev/null 2>&1; then
  log "Adding PostgreSQL APT repository (pgdg)"
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update

  log "Installing PostgreSQL 17 + pgvector"
  apt-get install -y postgresql-17 postgresql-client-17 postgresql-17-pgvector
else
  log "PostgreSQL 17 already installed"
fi

# ── Redis ──
if ! dpkg -s redis-server >/dev/null 2>&1; then
  log "Installing Redis"
  apt-get install -y redis-server
else
  log "Redis already installed"
fi

# ── Enable + start services (safe to re-run) ──
systemctl enable --now postgresql
systemctl reset-failed redis-server 2>/dev/null || true
systemctl enable --now redis-server

# ── Restore dumps if present and DB is empty ──
DUMP_DIR="/Users/joshuaodmark/Apps/orch/data-dumps"
if [ -f "$DUMP_DIR/postgres.sql" ]; then
  # Only restore if no user databases exist yet — avoid clobbering live data.
  USER_DB_COUNT=$(sudo -u postgres psql -tAc \
    "SELECT count(*) FROM pg_database WHERE datistemplate = false AND datname <> 'postgres';")
  if [ "$USER_DB_COUNT" -eq 0 ]; then
    log "Restoring PostgreSQL dump from $DUMP_DIR/postgres.sql"
    sudo -u postgres psql -f "$DUMP_DIR/postgres.sql" || true
  else
    log "Skipping pg dump restore — user databases already present"
  fi
fi

if [ -f "$DUMP_DIR/redis.rdb" ]; then
  CURRENT_KEYS=$(redis-cli DBSIZE 2>/dev/null | awk '{print $NF}')
  if [ "${CURRENT_KEYS:-0}" = "0" ]; then
    # Validate the dump against the installed redis-server before committing.
    # A dump from a newer Redis (e.g. RDB v12 from 7.4+) will crash an older
    # server's startup and leave the service in a failed state. redis-check-rdb
    # reads without loading, so it catches format-version mismatches first.
    if redis-check-rdb "$DUMP_DIR/redis.rdb" >/dev/null 2>&1; then
      log "Restoring Redis dump from $DUMP_DIR/redis.rdb"
      systemctl stop redis-server
      cp "$DUMP_DIR/redis.rdb" /var/lib/redis/dump.rdb
      chown redis:redis /var/lib/redis/dump.rdb
      systemctl start redis-server
    else
      log "WARNING: $DUMP_DIR/redis.rdb is incompatible with installed redis — skipping restore"
    fi
  else
    log "Skipping redis dump restore — keys already present"
  fi
fi

log "Done. PostgreSQL on :5432, Redis on :6379"
