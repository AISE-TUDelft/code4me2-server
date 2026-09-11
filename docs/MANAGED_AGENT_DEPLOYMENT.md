# Managed agent deployment

The participant plugin assumes a globally reachable HTTPS backend and managed ACP protocol v1. Provider credentials belong only in the backend environment; participant machines must not receive them.

## Deployment order

1. Back up the study database.
2. Deploy the candidate backend with traffic disabled or readiness failing.
3. Run `python src/database/migration/migration_manager.py migrate` as a one-shot deployment job. In the Compose image, use `/opt/conda/envs/myenv/bin/python src/database/migration/migration_manager.py migrate`; the command now loads the same non-overriding `.env` fallback as the application and exits unsuccessfully unless the database reaches the code's exact Alembic head.
4. Confirm the database is at the expected Alembic head and the managed ACP capabilities endpoint reports ready.
5. Run the managed agent smoke test through the public HTTPS origin.
6. Enable traffic, then distribute the plugin ZIP built for this protocol and backend version.

Do not reset a deployed study database during an upgrade. A failed migration must stop deployment and leave the existing application version serving until the database is restored or the migration is corrected.

## Release checks

- Upgrade a copy of the previous deployed schema and initialize an empty database.
- Confirm the active study contains only runtime profiles certified for the participant release.
- Confirm the backend holds every provider key named by an active profile.
- Verify grant exchange, run creation, inference, telemetry, logout revocation, and expiry recovery.
- Block direct provider access on the participant test machine and confirm inference still succeeds.
- Scan the plugin and runtime artifacts for `.env` files, provider credentials, source-checkout paths, and `local-dev` references.

Record the server commit, Alembic head, plugin commit, runtime version, runtime checksums, IDE version, and tested operating systems with each release.
