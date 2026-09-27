#!/usr/bin/env bash
# Restore a backup-postgres.sh dump (production-readiness B-05).
#
#   restore-postgres.sh <dump-file> --to-database <name> [--create]   drill into a scratch DB
#   restore-postgres.sh <dump-file> --yes                              restore INTO $PGDATABASE (destructive)
#
# Environment: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE (libpq). Stop the
# backend and celery-worker before a destructive restore; the dump is applied
# with --clean --if-exists, so existing objects are replaced.
set -euo pipefail

DUMP="${1:?usage: restore-postgres.sh <dump-file> (--to-database <name> [--create] | --yes)}"
shift
TARGET="${PGDATABASE:-}"
CREATE=0
CONFIRMED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --to-database) TARGET="$2"; shift 2 ;;
        --create) CREATE=1; shift ;;
        --yes) CONFIRMED=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -f "$DUMP" ] || { echo "dump not found: $DUMP" >&2; exit 2; }
[ -n "$TARGET" ] || { echo "no target database (set PGDATABASE or --to-database)" >&2; exit 2; }
if [ -f "$DUMP.sha256" ]; then
    ( cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256" ) || { echo "checksum mismatch" >&2; exit 1; }
fi
if [ "$TARGET" = "${PGDATABASE:-}" ] && [ "$CONFIRMED" -ne 1 ]; then
    echo "refusing to overwrite $TARGET without --yes (use --to-database <scratch> for a drill)" >&2
    exit 2
fi
if [ "$CREATE" -eq 1 ]; then
    createdb "$TARGET" || echo "createdb: database may already exist, continuing" >&2
fi
echo "restoring $DUMP into $TARGET on ${PGHOST:-localhost}:${PGPORT:-5432}" >&2
pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$TARGET" "$DUMP"
echo "restore finished; verify with: psql -d $TARGET -c 'SELECT count(*) FROM public.research_event;'" >&2
