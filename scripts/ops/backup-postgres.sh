#!/usr/bin/env bash
# Nightly PostgreSQL backup for the Code4Me study database (production-readiness B-05).
#
# Runs inside the `backup` service of docker-compose.prod.yml (a postgres image,
# so `pg_dump` matches the server major) or on the host against a reachable DB.
#
#   backup-postgres.sh            one backup now
#   backup-postgres.sh --loop     one backup now, then every BACKUP_INTERVAL_SECONDS
#
# Environment (libpq variables plus):
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE   connection (compose sets them)
#   BACKUP_DIR              where dumps land (default /backups)
#   BACKUP_RETENTION_DAYS   prune dumps older than this (default 14; 0 disables)
#   BACKUP_INTERVAL_SECONDS loop period (default 86400)
#   BACKUP_OFFHOST_CMD      optional command run with the dump path as $1 after a
#                           successful dump, e.g. `rclone copy "$1" remote:code4me`
#                           or `aws s3 cp "$1" s3://bucket/code4me/`; a failure is
#                           logged and does not delete the local copy.
#
# Output: <BACKUP_DIR>/code4me-<db>-<UTC timestamp>.dump (pg_dump custom
# format, compressed) plus a .sha256 sidecar. Restore with restore-postgres.sh.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"
PGDATABASE="${PGDATABASE:?PGDATABASE is required}"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

run_once() {
    mkdir -p "$BACKUP_DIR"
    # A run killed mid-dump leaves a .partial behind; it is never a valid backup.
    find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump.partial' -mmin +120 -print -delete | sed 's/^/removed stale /' >&2 || true
    local stamp file tmp offhost_ok=1
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    file="$BACKUP_DIR/code4me-${PGDATABASE}-${stamp}.dump"
    tmp="$file.partial"
    log "dumping $PGDATABASE from ${PGHOST:-localhost}:${PGPORT:-5432}"
    if ! pg_dump --format=custom --compress=6 --no-owner --no-privileges --file="$tmp" "$PGDATABASE"; then
        rm -f "$tmp"
        log "pg_dump FAILED"
        return 1
    fi
    mv "$tmp" "$file"
    ( cd "$BACKUP_DIR" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256" )
    log "wrote $file ($(du -h "$file" | cut -f1))"
    if [ -n "${BACKUP_OFFHOST_CMD:-}" ]; then
        if bash -c "$BACKUP_OFFHOST_CMD" _ "$file"; then
            log "off-host copy done"
        else
            offhost_ok=0
            log "off-host copy FAILED (local copies kept, retention skipped this run)"
        fi
    fi
    # Never prune local dumps while the off-host copy is failing: the local
    # disk is then the only copy there is.
    if [ "$offhost_ok" -eq 1 ] && [ "$BACKUP_RETENTION_DAYS" -gt 0 ] 2>/dev/null; then
        find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'code4me-*.dump' -o -name 'code4me-*.dump.sha256' \) \
            -mtime +"$BACKUP_RETENTION_DAYS" -print -delete | sed 's/^/pruned /' >&2 || true
    fi
}

if [ "${1:-}" = "--loop" ]; then
    while true; do
        run_once || true
        sleep "$BACKUP_INTERVAL_SECONDS"
    done
else
    run_once
fi
