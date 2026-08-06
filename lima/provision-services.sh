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

# ── Redis 8.x (official packages.redis.io repo) ──
# Ubuntu 24.04 ships redis 7.0.15, whose RDB format tops out at v11. The seed
# dump in data-dumps/redis.rdb is RDB v12 (from a 7.4+ instance), so 7.0.15
# crash-loops on startup. Redis 8.x reads both v11 and v12, so install from the
# official repo instead of the distro package.
REDIS_LIST=/etc/apt/sources.list.d/redis.list
REDIS_KEYRING=/usr/share/keyrings/redis-archive-keyring.gpg
if [ ! -f "$REDIS_LIST" ] || ! dpkg -s redis-server 2>/dev/null | grep -q '^Version: 6:8'; then
  log "Adding Redis APT repository (packages.redis.io)"
  apt-get install -y ca-certificates curl gpg
  curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o "$REDIS_KEYRING"
  chmod 644 "$REDIS_KEYRING"
  echo "deb [signed-by=$REDIS_KEYRING] https://packages.redis.io/deb $(. /etc/os-release; echo "$VERSION_CODENAME") main" \
    > "$REDIS_LIST"
  apt-get update

  log "Installing Redis 8.x"
  apt-get install -y redis-server
else
  log "Redis 8.x already installed"
fi

# ── systemd drop-in: the packages.redis.io binary is built WITHOUT systemd
# notify support, so the stock unit's Type=notify fails (systemd SIGTERMs it
# and hits the start-limit). Force Type=simple. ──
REDIS_DROPIN_DIR=/etc/systemd/system/redis-server.service.d
if [ ! -f "$REDIS_DROPIN_DIR/override.conf" ]; then
  log "Installing redis-server systemd drop-in (Type=simple)"
  install -d "$REDIS_DROPIN_DIR"
  cat > "$REDIS_DROPIN_DIR/override.conf" <<'EOF'
[Service]
Type=simple
EOF
  systemctl daemon-reload
fi

# ── redis.conf hardening: the redis.io package conffile defaults to
# `daemonize yes` (breaks Type=simple — redis forks away and systemd tears down
# the child) and binds 127.0.0.1 only. The shared-services design needs the VM
# boundary as the security perimeter, so bind 0.0.0.0. Idempotent — only flip
# when the current value differs. Leaves port/dir/save rules untouched. ──
REDIS_CONF=/etc/redis/redis.conf
if [ -f "$REDIS_CONF" ]; then
  if grep -Eq '^daemonize yes' "$REDIS_CONF"; then
    log "Setting daemonize no in $REDIS_CONF"
    sed -i 's/^daemonize yes/daemonize no/' "$REDIS_CONF"
  fi
  if ! grep -Eq '^bind 0\.0\.0\.0' "$REDIS_CONF"; then
    log "Setting bind 0.0.0.0 in $REDIS_CONF"
    sed -i 's/^bind .*/bind 0.0.0.0/' "$REDIS_CONF"
  fi
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
    # NOTE: now that we install Redis 8.x (above), this guard is no longer
    # load-bearing — 8.x reads RDB v11 AND v12, so the v12 seed dump loads
    # fine. It was also a false negative on the old 7.0.15: redis-check-rdb
    # there reported "0 keys read" / exit 0 on the v12 dump, so the broken dump
    # was copied in anyway and crash-looped startup. The real fix was the
    # version bump; this check is kept as a harmless belt-and-suspenders.
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
