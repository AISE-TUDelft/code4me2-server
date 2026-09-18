#!/usr/bin/env bash
#
# One-command local-dev seed for a fresh database.
#
# It stages the *built* runtime manifest (and any archives that exist next to
# it) into the running backend container, then runs the canonical seeder
# (scripts/dev/seed_research_study.py --fresh-db). The seeder imports the
# release, records the conformance receipt that qualifies it, pins
# default-code4me2-agent to it, turns Goose/Codex into BYOA identities, and
# publishes one study revision with a working session policy.
#
# No digest or size is ever typed by hand: when an archive is present its real
# sha256/size are computed inside the container and checked against the manifest.
#
# Usage (from code4me2-server/):
#   scripts/dev/seed_local_dev.sh
#   MANIFEST=/path/to/manifest.json scripts/dev/seed_local_dev.sh
#   scripts/dev/seed_local_dev.sh --builtin-study-name "My Study"
#
# Environment overrides:
#   CONTAINER   backend container name            (default: backend)
#   MANIFEST    path to the build manifest JSON   (default: ../code4me2/src/main/resources/code4me-runtime/manifest.json)
#   STAGING     in-container staging directory    (default: /tmp/code4me-runtime-staging)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER="${CONTAINER:-backend}"
MANIFEST="${MANIFEST:-$(cd "$REPO_ROOT/.." && pwd)/code4me2/src/main/resources/code4me-runtime/manifest.json}"
STAGING="${STAGING:-/tmp/code4me-runtime-staging}"

if [[ ! -f "$MANIFEST" ]]; then
    echo "manifest not found: $MANIFEST" >&2
    echo "set MANIFEST=/path/to/manifest.json (or build the runtime first)" >&2
    exit 1
fi

MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
MANIFEST_NAME="$(basename "$MANIFEST")"

echo "==> staging build manifest into container '$CONTAINER'"
docker exec "$CONTAINER" bash -lc "rm -rf '$STAGING' && mkdir -p '$STAGING/code4me-runtime'"
docker cp "$MANIFEST" "$CONTAINER:$STAGING/code4me-runtime/$MANIFEST_NAME"

shopt -s nullglob
for archive in "$MANIFEST_DIR"/code4me-agent-*.zip; do
    echo "    + $(basename "$archive")"
    docker cp "$archive" "$CONTAINER:$STAGING/code4me-runtime/$(basename "$archive")"
done
shopt -u nullglob

echo "==> seeding fresh database (import manifest -> qualify -> pin -> publish)"
docker exec -i "$CONTAINER" bash -lc "source activate myenv && cd /app && CODE4ME_DEV_SEED=1 python scripts/dev/seed_research_study.py --fresh-db --manifest '$STAGING/code4me-runtime/$MANIFEST_NAME' --artifact-root '$STAGING' \"\$@\"" -- "$@"
