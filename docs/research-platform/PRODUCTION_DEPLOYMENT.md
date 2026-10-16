# Production deployment

How to run the Code4Me research platform for a live study. Written for the
operator who deploys the server; the plugin side is in
`code4me2/docs/PARTICIPANT_SETUP.md`. The stack is `docker-compose.prod.yml`;
everything else in the repository root (`docker-compose.yml`,
`docker-compose.dev-arm.yaml`) is a GPU or development stack and must not serve
participants.

## Topology

```
participants (IDE plugin, browser)
        │  https://<PUBLIC_ORIGIN>
        ▼
  TLS edge (cloud LB / Caddy / Traefik / host nginx)   ← terminates TLS, forwards to :80
        │
  website (nginx)  ─ static React build, proxies /api/ → backend:8008
        │
  backend (uvicorn, N workers) ── db (Postgres 16 + pgvector, 127.0.0.1:5432 on the host)
        │                       ── redis (AOF, password)         ── redis-celery (AOF)
        │                       ── celery-worker (classic rows)
  migrate (one-shot, runs before backend)      backup (nightly pg_dump → ./backups)
```

Only the `website` container is published (`HTTP_PORT`, default 80). Postgres is
bound to `127.0.0.1` for `psql`/restores. Redis and the backend are reachable
only inside the compose network, which is why the backend trusts
`X-Forwarded-For` from any peer (`FORWARDED_ALLOW_IPS=*`): the only peer is
nginx, and nginx forwards exactly one hop (`$remote_addr`, never the
client-supplied chain), so a client cannot choose its own rate-limit key.
Behind an external TLS edge `$remote_addr` is the edge: uncomment the
`set_real_ip_from`/`real_ip_header` lines in `nginx.prod.conf` with the edge's
address so nginx recovers the real client first.

### TLS

Terminate TLS in front of the `website` container and forward plain HTTP to
`HTTP_PORT`, preserving `X-Forwarded-Proto: https` (Caddy, Traefik and cloud
load balancers do; for a host nginx add `proxy_set_header X-Forwarded-Proto
$scheme;`). Cookies are issued with `Secure` (`COOKIE_SECURE=true`) and the
participant plugin refuses non-HTTPS origins, so the edge is mandatory.
`nginx.prod.tls.conf.example` shows how to terminate TLS in the container
instead.

## Required configuration

Copy `.env.example` to `.env` and fill the "Production inventory" section. The
compose file refuses to start without:

| Variable | Why |
| --- | --- |
| `BOOTSTRAP_SIGNING_SECRET` | HMAC key for bootstrap manifests and session/inference capabilities. Without it every bootstrap answers 503 and the backend exits at start. |
| `REDIS_PASSWORD` | `requirepass` for the session Redis. |
| `PUBLIC_ORIGIN` | The https origin participants use; becomes `CORS_ALLOWED_ORIGINS` and the website's compiled backend host. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Postgres credentials (also used by the backup service). |

Plus the provider key(s) every active agent profile's `secret_ref` names
(`OPENROUTER_API_KEY`, ...). Provider keys never leave the backend environment.

Never reuse the development `.env`: rotate any secret that was ever committed
or shared, and keep `.env` out of images (`.dockerignore` excludes it; the
containers receive it through `env_file`).

## First deployment

1. `cp .env.example .env`, fill it, then validate the rendering:
   `docker compose --env-file .env -f docker-compose.prod.yml config >/dev/null`.
2. `docker compose --env-file .env -f docker-compose.prod.yml up -d --build`.
   `migrate` runs `migration_manager.py migrate` (initializes a fresh database
   from `init.sql`, then applies every Alembic revision) and the API waits for it.
3. `docker compose -f docker-compose.prod.yml ps` — `backend` must be `healthy`
   (its check is `GET /api/health`, which touches Postgres and Redis) and
   `migrate` `exited (0)`.
4. Smoke the research plane through the public origin:
   - `curl -fsS https://<host>/api/health` → `{"status":"ok",...}`.
   - Sign in on the website, create a study as a researcher, join it as a test
     participant, then in the IDE plugin confirm the status bar reaches
     **Research: active** and `docker compose logs backend | grep "telemetry batch"`
     shows accepted batches. A `SIGNING_SECRET_MISSING` or `CAPABILITY_INVALID`
     here means the secret is not reaching the container.
5. Confirm the first backup file appeared in `BACKUP_DIR` (the service dumps once
   at start, then every `BACKUP_INTERVAL_SECONDS`).
6. Record server commit, Alembic head (`python src/database/migration/migration_manager.py current`),
   plugin tag, runtime tag and checksums per `docs/MANAGED_AGENT_DEPLOYMENT.md`.

## Upgrades

1. Back up (`docker compose -f docker-compose.prod.yml exec backup /bin/bash /ops/backup-postgres.sh`).
2. `git pull` the release commit, `docker compose --env-file .env -f docker-compose.prod.yml build`.
3. `docker compose --env-file .env -f docker-compose.prod.yml up -d` — `migrate`
   runs first; if it fails the old `backend` container keeps serving; fix or
   restore before retrying. Never reset the study database during an upgrade.
4. Watch `docker compose logs -f backend` for the first heartbeats/batches.

Schema deltas for databases created before a revision are listed in
`RELEASES.md` ("Existing data").

## Backups and the restore drill

- The `backup` service writes `code4me-<db>-<UTC>.dump` (pg_dump custom format)
  plus a `.sha256` to `BACKUP_DIR`, prunes after `BACKUP_RETENTION_DAYS` (only
  when the off-host copy succeeded), and runs `BACKUP_OFFHOST_CMD` (e.g.
  `rclone copy "$1" remote:code4me-backups`) after every dump. Configure the
  off-host copy before recruitment: a disk failure must not be able to take the
  study with it. Set `BACKUP_DIR` to a path on a different disk than the
  Postgres volume (e.g. `/var/backups/code4me`); the default `./backups` is
  ignored by git and by the Docker build context, but shares the disk. The
  pgvector image has no `rclone`/`aws`: mount the binary into the `backup`
  service, or run the off-host copy from a host cron on `BACKUP_DIR`.
- Redis persists with AOF on `redis_data`; losing it only logs participants out
  (they sign in again); nothing collected lives there.
- **Drill (do this once before the first participant, then monthly):**
  ```bash
  docker compose -f docker-compose.prod.yml exec backup /bin/bash /ops/backup-postgres.sh
  docker compose -f docker-compose.prod.yml exec backup /bin/bash /ops/restore-postgres.sh \
      /backups/<latest>.dump --to-database code4me_drill --create
  docker compose -f docker-compose.prod.yml exec db psql -U "$DB_USER" -d code4me_drill \
      -c 'SELECT count(*) FROM public.research_event;'
  docker compose -f docker-compose.prod.yml exec db dropdb -U "$DB_USER" code4me_drill
  ```
- **Real restore:** stop `backend` and `celery-worker`, then
  `restore-postgres.sh <dump> --yes` against `PGDATABASE`, then `up -d`.

## Operations

- Health: `GET /api/health` (database + Redis, 503 when either fails);
  `GET /nginx-health` for the edge alone. Point your uptime monitor at both.
- Memory: the compose caps `backend` and `celery-worker` at 2 GiB. With
  `CODE4ME_WEB_WORKERS=2` each worker imports the ML stack; measure RSS on the
  first deploy (`docker stats`) and raise the cap before it turns into an
  OOM-kill loop that looks like "healthy, then gone".
- Logs are bounded (`json-file`, 50 MB × 5 per container); the limiter's
  per-request line is at DEBUG, uvicorn's access log stays on (one line per
  request, no bodies or cookies). `docker compose logs backend | grep -E "telemetry batch|rejected"`
  shows ingestion outcomes without payloads.
- Rate limits key on the real client address (nginx's `X-Forwarded-For`).
  Research-plane paths get a 20000 requests/hour floor per client; tune with
  `MAX_REQUEST_RATE_PER_HOUR_CONFIG`.
- Kill switch: the researcher UI's kill switch pauses collection; participants'
  plugins keep their runtime and resume automatically when it is released.
- Session tail: events of a session that ended (idle timeout, IDE closed) are
  accepted for `TELEMETRY_LATE_EVENT_GRACE_SECONDS` (default 900) after the
  close; the plugin drains its spool on project close and rotates sessions on
  idle without participant action.

## Not covered here

Alerting (silence per participant, disk usage) and the study-health page are
follow-ups (review B-11/§5.6); until they exist, check `/api/health`, disk
usage on the host, and the researcher dashboards daily during the study.
