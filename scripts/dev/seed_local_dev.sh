#!/usr/bin/env bash
#
# One-command local-dev seed for a fresh database.
#
# It stages the *built* runtime manifest (and any archives that exist next to
# it) into the running backend container, then runs the canonical seeder
# (scripts/dev/seed_research_study.py --fresh-db). The seeder imports the
# release with producer test results,
# pins default-code4me2-agent to it, turns Goose/Codex into BYOA identities, and
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

# The producer owns test results; never invent them at seed time. An untested
# manifest is rejected before anything touches the database.
if ! python3 - "$MANIFEST" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
artifacts = document.get("artifacts") or []


def passed(artifact):
    tests = artifact.get("tests")
    return (
        isinstance(tests, dict)
        and tests.get("self_check") == "PASS"
        and tests.get("acp_initialize") == "PASS"
        and bool(tests.get("ran_at"))
    )


raise SystemExit(0 if artifacts and all(passed(a) for a in artifacts) else 1)
PY
then
    cat >&2 <<EOF
manifest carries no passing producer tests: $MANIFEST

The producer owns the test results. Produce the manifest first (this runs the
packaged executable's --self-check and ACP initialize and writes the ZIP next to
the manifest), then seed with it:

  cd "$REPO_ROOT"
  PYTHONPATH=src python -m research.study.agents.participant_release native \\
      --skip-build --version <version> --platform macos-arm64 \\
      --server-commit "\$(git rev-parse HEAD)" --output dist/release-<version>
  MANIFEST=dist/release-<version>/native-macos-arm64.json scripts/dev/seed_local_dev.sh

Omit --skip-build to build, test and archive the agent from source.
EOF
    exit 1
fi

MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
MANIFEST_NAME="$(basename "$MANIFEST")"

echo "==> staging the runtime archives into container '$CONTAINER'"
docker exec "$CONTAINER" bash -lc "rm -rf '$STAGING' && mkdir -p '$STAGING/code4me-runtime'"
# The source manifest carries the pinned runtime version (and the archive
# basenames); keep its test results and digest identity unchanged.
docker cp "$MANIFEST" "$CONTAINER:$STAGING/code4me-runtime/$MANIFEST_NAME" >/dev/null

shopt -s nullglob
ARCHIVE_COUNT=0
for archive in "$MANIFEST_DIR"/code4me-agent-*.zip; do
    base="$(basename "$archive")"
    echo "    + $base"
    docker cp "$archive" "$CONTAINER:$STAGING/code4me-runtime/$base"
    ARCHIVE_COUNT=$((ARCHIVE_COUNT + 1))
done
shopt -u nullglob
if [[ "$ARCHIVE_COUNT" -eq 0 ]]; then
    echo "no code4me-agent-*.zip archives found next to $MANIFEST" >&2
    exit 1
fi

# Use the producer's tested manifest unchanged; do not manufacture test results.
echo "==> seeding database (verified import -> pin -> publish)"
docker exec -i "$CONTAINER" bash -lc "source activate myenv && cd /app && CODE4ME_DEV_SEED=1 python scripts/dev/seed_research_study.py --manifest '$STAGING/code4me-runtime/$MANIFEST_NAME' --archives-dir '$STAGING/code4me-runtime' \"\$@\"" -- "$@"
