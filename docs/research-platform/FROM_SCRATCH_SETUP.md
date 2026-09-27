# From-scratch setup (dev) — end to end

This walks through a working Code4Me research installation on a clean machine:
infrastructure → migrations → admin → researcher → provider → agent → plugin →
study → participant. Each step says what to do and what to expect.

**Repository layout (required):** both repositories live side by side under one
workspace root.

```
<workspace>/
  code4me2/          # IntelliJ plugin (carries the agent inside)
  code4me2-server/   # backend + website + producer + e2e harness
```

**Roles:** *operator/admin* = infrastructure + catalogue + provider; *researcher*
= profiles + studies; *participant* = enters the join code, installs the plugin,
starts the session.

---

## 0. Prerequisites

| Requirement | Note |
|---|---|
| Docker + Docker Compose v2 | for db/redis/backend/website/nginx |
| JDK 21+ | plugin build; the Gradle toolchain downloads JDK 25 itself when needed (foojay) |
| IntelliJ IDEA | to install the plugin and start a session |
| Python 3.12 venv (`code4me2-server/.venv`) | agent builds + e2e harness |
| PyInstaller | `uv pip install -r packaging/requirements-build.lock` (documented build dependency) |

> Use `code4me2-server/.venv`. A workspace-root `.venv` may have a broken pytest
> (`ImportError: Deque`) and a full agent build then fails at its test step.

---

## 1. Environment file (`.env`)

`code4me2-server/.env` (gitignored) holds the required values. The critical ones:

```dotenv
DB_PORT=5433            # host port (same port inside compose)
DB_NAME=code4meV2
DB_USER=postgres
DB_PASSWORD=postgres
SERVER_PORT=8008
OPENROUTER_API_KEY=...  # your provider's key (never written to the database)
```

- Provider keys live **only** here; the database stores the *name* (`secret_ref`)
  and the backend resolves the value from its own environment. Goose and
  built-in study arms spend from this one key through a metered relay; the
  optional `INFERENCE_*` knobs in `.env.example` tune the hold estimate, output
  cap, reservation deadline and the inference capability lifetime.
- `CODE4ME_DEV_RELOAD=1` (dev compose default): the backend restarts on source
  changes, so a long-running stack never serves stale in-memory code.

---

## 2. Bring the stack up

```bash
cd code4me2-server
docker compose -f docker-compose.dev-arm.yaml up -d --build
docker compose -f docker-compose.dev-arm.yaml ps
```

Expected: `db`, `redis`, `redis-celery`, `backend`, `celery-worker`, `website`,
`nginx` → **healthy**.

| Address | What |
|---|---|
| http://localhost:8008/docs | Backend API (OpenAPI) |
| http://localhost:3000 | Website (login/signup/dashboard/join) |
| localhost:5433 | Postgres (db: `code4meV2`) |

> **Data persistence:** Postgres data lives in the `pgdata` named volume.
> `docker compose -f docker-compose.dev-arm.yaml down` removes the containers but
> **keeps** the data. Use `down -v` deliberately when you want a full reset (it
> also drops the `website_node_modules` volume), then rerun the migrations in
> step 3.
>
> Without the volume, `down` would destroy the database: `init.sql` only creates
> the 22 base tables and the research schema arrives via migrations. With no
> schema, login answers `500 Server failed to authenticate the user!` and the
> `user` table is empty.

---

## 3. Migrations

`init.sql` runs automatically only on an **empty volume** (base tables). The
research schema comes from migrations:

```bash
docker exec backend bash -lc "source activate myenv && cd /app && \
  python src/database/migration/migration_manager.py status"
docker exec backend bash -lc "source activate myenv && cd /app && \
  python src/database/migration/migration_manager.py migrate"
```

Expected `status`: `Database: Connected` · `Tables: 45` · `At expected head`.

There is a single revision, `8a0084080b46_consolidated_schema.py`. It **refuses
to run** against a database that still holds the previous research tables — use a
new database/volume (never reset a production database in place).

---

## 4. First admin (one-time)

```bash
curl -X POST http://localhost:8008/api/user/create \
  -H 'Content-Type: application/json' \
  -d '{"config_id":1,"email":"admin@example.com","name":"Admin","password":"<strong-password>"}'
```

```sql
-- one-time, operator-run; there is deliberately no admin-granting endpoint
UPDATE "user" SET is_admin = TRUE WHERE email = 'admin@example.com';
```

Verify: http://localhost:3000/login → sign in with that account → the dashboard
shows the **ADMIN VIEW** badge and the `Accounts / Provider Connections / Agent
Catalogue` tiles.

---

## 5. Researcher account

1. Create an account at `/signup` (participants and researchers use the same path).
2. Grant researcher access as the admin:
   - **UI:** Dashboard → **Accounts** → tick **Researcher** on that row.
   - **API:** `PUT /api/research/researchers/<user_id>` → `{"can_research": true}`

`can_research` is the single researcher authority (there is no per-study role
table). Administrators bypass ownership checks.

---

## 6. Provider connection (+ secret)

**UI:** Dashboard → **Provider Connections**

| Field | Example |
|---|---|
| Label | `prod-openrouter` |
| Base URL | `https://openrouter.ai/api/v1` |
| Secret name (env var) | `OPENROUTER_API_KEY` ← the **name**, not the value |
| Models (one per line) | `cohere/north-mini-code:free` |

After saving, the row must show **"Ready — secret present in deployment"** (the
named env var exists and the backend can read it).

**Model prices (required for Goose and built-in arms):** in the same editor,
enter each model's price in **USD per million tokens** (input, output, and
optionally cached input). A metered arm whose model has no price refuses every
call with `503 price_missing`, and the study form warns before creation. The
connection card shows **"Prices: N of M models"** and a *price missing* chip per
unpriced model.

**API:** `POST /api/research/provider-connections` → `{label, base_url, secret_ref, models, is_active, model_prices?}`
(`model_prices` = `{model: {input_usd_per_million, output_usd_per_million, cached_input_usd_per_million?}}`; omitted = unchanged)

Selection rule: every **active** connection can be selected in any signed-in
user's profile (there is no per-user grant flow). The secret value never enters
the database.

---

## 7. Produce the agent (release)

```bash
cd code4me2-server

# A) Full build: stamp the version → runtime tests → PyInstaller → archive →
#    --version / --self-check / ACP initialize against the packaged executable
PYTHONPATH=src .venv/bin/python -m research.study.agents.participant_release native \
  --version 1.2.3 --platform macos-arm64 \
  --server-commit "$(git rev-parse HEAD)" \
  --output dist/release-1.2.3

# B) Re-test an already built bundle (the version must equal the bundle's)
PYTHONPATH=src .venv/bin/python -m research.study.agents.participant_release native \
  --skip-build --version 1.2.3 --platform macos-arm64 \
  --server-commit "$(git rev-parse HEAD)" --output dist/re-test-1.2.3
```

Rules:
- Run the command **from `code4me2-server`** (that is where `src/research` lives).
- The `--output` directory must **not** exist yet (immutable release rule).
- With `--skip-build`, `--version` must equal the version embedded in the bundle.
- Platforms: `macos-arm64`, `macos-x64`, `linux-x64`, `windows-x64` (`aarch64` is normalised to `arm64`).
- **Failed tests produce no output directory.**

Output:
```
dist/release-1.2.3/code4me-agent-macos-arm64.zip
dist/release-1.2.3/native-macos-arm64.json      ← tests: self_check/acp_initialize PASS
```

> **Keep this ZIP.** Those bytes are what a study pins, and the plugin will carry
> them. Rebuilding the same version can produce a different sha → the pin no
> longer matches.

---

## 8. Import the agent into the catalogue

**UI:** Dashboard → **Agent Catalogue**

- From files: paste `native-macos-arm64.json` into `Runtime manifest JSON` →
  upload the **complete set** of ZIPs the manifest declares via `Agent archive
  files` → **Verify and import**.
- From URLs: **Import from release URLs** → `Manifest URL` + `Archive URLs` (one
  ZIP per line).

**API:**
```http
POST /api/research/agents/releases/import       # multipart: manifest (text) + archives (files)
POST /api/research/agents/releases/import-url   # {manifest_url, archive_urls[]}
```

What the server verifies: the exact declared set (missing/extra/duplicate),
sha256 + size (computed server-side) and the test results. Any failure rejects
the **whole** import.

Result: a `release_id` (e.g. `code4me-agent-1.2.3-9f5f4e5d1c53`) and **QUALIFIED**
status — derived from the platform test results; there is no separate
approval/receipt step.

**Emergency stop:** **Disable release** in the release detail is one-way;
re-importing the same manifest does not re-enable it.

---

## 9. Build and install the plugin with the agent

The plugin's agent identity is **a single recipe**:
`src/main/resources/code4me-runtime/manifest.json` plus the ZIP it declares. There
is no separate agent staging parameter (the old `-PresearchAgent*` options were
removed); the runtime installs that recipe and compares it with the archive
digest pinned by the bootstrap manifest.

**Critical rule:** the ZIP you stage must be **the ZIP that was imported into the
catalogue** — not a fresh build. Every build produces a different sha; with other
bytes the gate still passes but the session fails with
`the bundled agent archive does not match the bootstrap pin`.

```bash
cd code4me2
python3 scripts/build-plugin-with-agent.py ../code4me2-server/dist/release-1.2.3
```

That single command copies the ZIP into `src/main/resources/code4me-runtime/`,
writes the recipe (`code4me-runtime/manifest.json`) **computed** from the release
manifest (no hand-typed digests, no editor), and runs
`verifyResearchRuntimeConsistency` + `buildPlugin`. It prints the plugin ZIP path.

Options:
- `--no-build` → only copy and write the recipe.
- `--platform macos-x64` → stage a platform other than the host.

**Why `native-<platform>.json` is not used verbatim:** the producer manifest and
the plugin recipe differ in exactly two places — the recipe stores the archive
path relative to the resources root (`code4me-runtime/<name>.zip`) and carries a
per-artifact `managed_protocol`. The script bridges those two; it uses
`runtime_version`, `sha256`, `size`, `executable`, `server_commit` and `tests`
verbatim. Hand-editing typically produces the opposite mistake: the recipe
describes one build while the directory holds another → the gate stops with
`Runtime checksum mismatch`.

Output: `code4me2/build/distributions/client-<version>.zip`

**Install:** IntelliJ → **Settings → Plugins → ⚙ → Install Plugin from Disk…** →
ZIP → **Restart IDE**.

The dev backend URL is already `http://localhost:8008` in
`src/main/resources/plugin.conf`.

> The plugin version (`pluginVersion` in `gradle.properties`) and the agent
> version (`--version` at production) are independent axes: the plugin ZIP is
> named after the former, the release/catalogue entries after the latter. The
> runtime compares the agent's **sha256**, never its version string.

---

## 10. Profile → study → participant

**Profile (researcher):** Dashboard → **Agent Profiles**

| Field | Value |
|---|---|
| Profile name | `default-code4me2-agent` |
| framework | `code4me2-agent (built-in)` |
| connection_id | the connection from step 6 |
| model | one of the connection's models |
| release_id | the release created in step 8 (must be QUALIFIED) |
| approval_policy | `Auto-approve` / `Ask per step` / `Suggestion only` |
| tools, max_steps, temperature | from the framework catalogue / limits |

**Study (researcher):** Dashboard → **Research Control Plane** → **New study** →
name/dates → select the profile → **default budget per participant (USD)** →
telemetry policy + session policy → create → **Join code**.

The default budget is required (> 0) whenever a selected profile runs Goose or
the built-in agent (Codex arms sign in with ChatGPT and are not metered). It is
editable at any time in the study's **Settings → Participant budgets** card,
which also applies a new default to the participants still on the old one; the
**Participants** tab shows spent/budget per participant and the drawer's
**Adjust budget** tops up or sets an individual limit (with a reason).
Participants see "Budget remaining" on *My studies*. Every enrollment is born
with a balance at the study default; a participant whose budget is used up gets
`402 quota_exhausted` from the agent until topped up — nothing else changes.

**Participant:**
1. Account at `/signup` (or an existing account).
2. Web: **Join a research study** → code → study preview → **consent**
   (or plugin: **Join a Study** → code).
3. Open the project in IntelliJ → start the session from the Code4Me research
   settings → pick **Code4Me Research Proxy** in AI Chat.
4. The agent is installed from inside the plugin (run with `--managed`) and
   session telemetry flows.

---

## 11. Verification and shortcuts

```bash
# Whole backend flow (disposable stack; real import + session + telemetry + revoke)
cd code4me2-server && ./e2e/test --layer backend

# Admin panel + study + join flow (real browser)
CODE4ME_FLOW_MANIFEST=$PWD/dist/release-1.2.3/native-macos-arm64.json \
  node e2e/browser/admin_flow.js

# Backend + database tests (isolated DB; never touches the dev database)
docker exec postgres createdb -U postgres code4me_release_refactor_test 2>/dev/null || true
docker exec -e TEST_DATABASE_URL=postgresql://postgres:postgres@db:5433/code4me_release_refactor_test \
  backend sh -c 'cd /app && /opt/conda/envs/myenv/bin/python -m pytest tests/backend_tests/research tests/database_tests -q'

# Website
cd src/website && CI=true npm test -- --watchAll=false --runInBand && CI=true npm run build

# Plugin side: recipe↔ZIP gate + test suite
cd ../code4me2 && ./gradlew verifyResearchRuntimeConsistency && ./gradlew test
```

**Dev seed (shortcut):** catalogue + pin + study in one command from a produced manifest:
```bash
MANIFEST=dist/release-1.2.3/native-macos-arm64.json scripts/dev/seed_local_dev.sh
```
(It rejects a manifest without test results and prints the producer command.)

---

## 12. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `No accounts found.` | The running backend may hold stale code | `docker restart backend` (dev reload is on; make sure `CODE4ME_DEV_RELOAD=1`) |
| `ModuleNotFoundError: No module named 'research'` | Command run from the wrong directory | `cd code4me2-server` (that is where `src/research` lives) |
| `packaged executable version does not match the release` | `--skip-build` with a different version | Match the bundle's version, or drop `--skip-build` |
| `TESTS_NOT_PASSED` | The manifest carries no passing platform test | Produce the manifest with the producer command |
| `DIGEST_MISMATCH` / `SIZE_MISMATCH` | The uploaded bytes disagree with the manifest | Upload the ZIP from the same build |
| `ARTIFACT_MISSING` / `UNEXPECTED_ARCHIVE` / `DUPLICATE_ARCHIVE` | Archive set is not exactly what the manifest declares | Upload the full set, one copy each |
| `RELEASE_NOT_QUALIFIED` (profile) | Release has no test results, or was disabled | Import a tested manifest; `DISABLED` cannot be re-enabled |
| `FRAMEWORK_DISTRIBUTION_MISMATCH` | Profile framework does not match the release mode | `code4me2-agent` ↔ PACKAGED; Goose/Codex ↔ BYOA |
| `the bundled agent runtime … does not match the pinned release archive …` | The plugin carries a different ZIP than the study pins | Redo step 9 with the right ZIP |
| `the bundled runtime has no agent for <os>-<arch>` | The recipe does not declare that platform | Step 9: make sure the release manifest contains the platform |
| `unrecognized arguments: --status-file / --agent-run-id / --telemetry-policy-digest` | The packaged proxy bundle is stale (no such CLI flags) | `./gradlew buildResearchProxyBundle`, then rebuild/reinstall the plugin |
| `ARTIFACT_UNAVAILABLE` (bootstrap) | No packaged archive for the participant's platform | Build a plugin that includes that platform |
| `401` from the provider | `secret_ref` env var is empty/wrong | Put the key in `.env`, `docker restart backend` |
| `402 quota_exhausted` in the agent chat | The participant's budget is used up (or the study default is still 0) | Study → Settings → Participant budgets (set/apply the default) or the participant drawer → Adjust budget |
| `503 price_missing` in the agent chat | The arm's model has no price on its provider connection | Provider Connections → enter the model's prices |
| `INFERENCE_GATEWAY_UNBOUND` (bootstrap / profile) | The Goose release does not bind the research inference gateway | Re-import a recipe whose Goose agent declares the runtime bindings (see `docs/participant-release.example.json`) and re-pin the study |
| `BUDGET_REQUIRED` / `BUDGET_PRICE_MISSING` (create study) | A selected Goose/built-in profile needs a default budget / a priced model | Enter the default budget; price the model on its connection |
| Migration "refuses to run" | The database still holds old research tables | Use a new database/volume (no in-place production reset) |

---

## 13. Do not

- Write a provider key into the database, the plugin or a manifest (store the *name* only).
- Reset/drop tables in a production database.
- Distribute the developer `buildPlugin` ZIP to participants (the participant
  release is a separate CI flow).
- Try to revive a `DISABLED` release by re-importing it.
- Delete a produced release ZIP: a study's pin is bound to those bytes and a
  rebuild yields a different sha.

---

## 14. Common paths (summary)

```
Producer:  code4me2-server/dist/release-<v>/{native-<platform>.json, code4me-agent-<platform>.zip}
Catalogue: Dashboard → Agent Catalogue → Verify and import
Provider:  Dashboard → Provider Connections
Profile:   Dashboard → Agent Profiles
Study:     Dashboard → Research Control Plane
Join:      /join (web) or the plugin's "Join a Study"
Plugin:    code4me2/build/distributions/client-<version>.zip
Dev seed:  scripts/dev/seed_local_dev.sh (with MANIFEST=...)
E2E:       ./e2e/test --layer backend · node e2e/browser/admin_flow.js
```
