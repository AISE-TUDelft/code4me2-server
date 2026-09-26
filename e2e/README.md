# Code4Me end-to-end tests

This harness is version-controlled in the server repository at
`code4me2-server/e2e/`. It drives both checkouts, so it expects the workspace
layout:

```
<workspace>/
    code4me2/            # IntelliJ plugin
    code4me2-server/     # backend + this harness
```

From the workspace root:

```bash
./code4me2-server/e2e/test
```

or from this directory:

```bash
./test
```

`CODE4ME_E2E_WORKSPACE` overrides the workspace lookup (CI checks the two
repositories out side by side and sets it when the layout differs).

One command runs everything, and nobody has to prepare the host by hand first.
The gate checks each layer's prerequisites and provisions what the harness can
own: it starts Colima when the Docker daemon is down, builds the backend image
when it is missing or its `requirements.txt` changed, creates the Playwright
venv and its Chromium, installs website dependencies, and builds the plugin's
vendored Codex ACP adapter. It then starts a disposable backend, builds the
current plugin and native runtimes, opens an isolated IntelliJ sandbox, drives
the UI, exercises the registered ACP proxy, and checks the resulting telemetry in
PostgreSQL. It runs the live Kotlin fixture and the HTTP workflow through
enrollment revocation, real Goose and Codex agents over ACP, and the website
suite. No manual IDE startup, account creation, study setup, or model API key is
needed.

On success the default command closes its IDE and removes its Compose stack and
volume. On failure it closes the IDE and keeps the stack and run artifacts for
diagnosis. A nonzero exit code means the gate did not pass.

## For coding agents

- Run the whole gate with `./code4me2-server/e2e/test --json` from the
  workspace root. It prints progress on stderr and a JSON summary on stdout:
  `ok`, every step, `layers` (PASS / FAIL / BLOCKED / NOT_ATTEMPTED with a
  reason) and `prerequisites` (each check with its remediation).
- **Run it outside any command sandbox, in a logged-in desktop session.** The
  IDE layer opens a real IntelliJ window. Started from a sandboxed shell (Claude
  Code's Bash sandbox, Codex's seatbelt), IntelliJ hangs in AWT/Metal start-up
  and robot-server never answers; the harness then fails at `ide_startup` after
  its timeout. In Claude Code, run the command with the sandbox disabled.
- `python3 -m code4me_e2e setup --check` (from `code4me2-server/e2e/`) reports
  every prerequisite without changing anything; `setup` provisions them without
  running tests. Both take `--layer` and `--json`.
- A layer that cannot get its prerequisites is BLOCKED with the exact
  remediation, and the gate fails. Nothing is skipped silently. The other,
  independent layers still run: `agents` and `browser` run even when the IDE
  layer fails.
- With warm caches a full run takes about 3 minutes (2 min 52 s on 2026-09-25:
  IDE layer 51 s, Kotlin fixture 80 s, agents 6 s, browser 23 s). The first run
  takes much longer: Gradle downloads the IDE, PyInstaller builds the agent and
  proxy, and a missing backend image downloads several GB. Use `--layer` for a
  narrower check (table below).
- Only one harness command runs at a time (workspace lock). Do not run other
  Gradle builds in `code4me2/` while the gate runs. The gate stops sandbox IDEs
  left behind by an earlier, interrupted harness run.
- Give the command a long timeout or run it in the background: provisioning on a
  fresh host (image build, IDE download, native builds) can take far longer than
  a warm run. An interrupted gate (Ctrl-C or SIGTERM, e.g. a tool timeout) stops
  the builds and the IDE it started; SIGKILL cannot be cleaned up.

## Choose the checks

| Command | Coverage |
| --- | --- |
| `./test` | every layer below: real IDE + registered ACP proxy, Kotlin fixture, HTTP workflow, real agents, website |
| `./test --layer backend` | 17 HTTP workflow steps (imports the producer-built agent release; no IDE) |
| `./test --layer plugin` | Kotlin IntelliJ fixture + HTTP workflow, no IDE UI |
| `./test --layer agents` | real Goose and Codex over ACP against a local provider; no Docker |
| `./test --layer browser` | `browser/` suite: 40 headless Chromium checks + persisted read models |
| `./test --json` | Same gate, with a JSON summary on stdout |
| `./test --keep-stack` | Preserve the disposable backend after success |
| `./test --no-setup` | Check prerequisites but provision nothing (missing ones block their layers) |

`all` is the default and runs the layers in this order: `ui`, `plugin`,
`backend`, `agents`, `browser`. `plugin` and `backend` depend on the IDE layer
and are NOT_ATTEMPTED when an earlier layer failed. `agents` and `browser` are
independent and always attempted. The report records every layer
(`report.json` → `layers`).

The IDE layer uses the real server, plugin, packaged agent and packaged proxy.
Only the external model provider is replaced by a deterministic local HTTP
provider. The UI test drives eight steps: plugin loading, settings navigation,
sign-in, automatic enrollment discovery, the status surface, ACP registration,
agent preparation and one model response. The harness then checks the entry the
IDE registered in its ACP registry:
- the proxy and the wrapped agent binary are executable files;
- their sha256 and `--version` are recorded;
- the declared `--agent-digest` matches the agent binary;
- the one-time spool capability is present.

It then acts as an ACP client with that exact command, arguments and managed-auth
environment, and requires:
- the provider's deterministic token in the answer;
- exactly one new provider request for that first prompt;
- canonical telemetry scoped to this launch, persisted in the database:
  `interaction.started` ×2, `agent.message.started` ×2 and
  `agent.message.completed` ×1 from this proxy emitter, plus IDE events from this
  run's research session.

The Kotlin fixture independently tests the plugin's login, discovery, signed
bootstrap validation and durable spool upload.

The browser suite covers:
- study creation, consent and enrollment handoff;
- metadata editing and locking;
- the participants, dashboard and analytics tabs, and the participant's My studies page;
- stopping and cloning a study;
- authorization controls.

It must run its exact 40-check inventory once each. It then re-reads the real
API read models:
- edited metadata;
- stopped study retention;
- clone identity and profile selection;
- enrollment and assignment coverage.

The browser layer seeds its own accounts, profile and study under
`runs/<id>/browser-state/`, so it never disturbs the IDE layers' state.

This does not automate JetBrains AI Assistant's chat panel or JetBrains account
sign-in. It tests the registered ACP integration through its public protocol.
The deterministic provider does not test model quality, external provider
availability, or every agent/tool combination.

## Prerequisites: provisioned vs. one-time

`./test` and `setup` check these per layer, in this order:

| Check | Layers | Provisioned by the harness | Otherwise BLOCKED with |
| --- | --- | --- | --- |
| `docker` | ui, plugin, backend, browser | `colima start [profile]` when the daemon is down and the active docker context is a Colima one (never creates a VM or switches contexts) | start Docker Desktop or Colima |
| `backend_image` | same | `docker build -f Dockerfile.cpu` when the image is missing, or its `/app/requirements.txt` differs from the checkout (harness builds carry an `org.code4me.e2e.deps` label) | the build log path |
| `stack_ports` | same | — (ports held by this harness's own compose project are fine) | the owning process; `--set stack.<name>_port=N` |
| `native_python` | ui, plugin, backend, browser | `e2e/.venv`, pinned like the release CI (`packaging/requirements-{runtime,build}.lock` + the editable server), when neither `code4me2-server/.venv` nor an existing `e2e/.venv` imports the build inputs. The first interpreter that does is used | an explicit `CODE4ME_E2E_PYTHON` that cannot import them is BLOCKED, never replaced |
| `java` | ui, plugin | — | install a JDK 17+ (Gradle provisions the plugin's toolchain and IDE) |
| `gui_session` | ui | — | run from a logged-in desktop session (Linux: `DISPLAY`/Xvfb) |
| `stale_ides` | ui | stops sandbox IDEs left by an interrupted harness run (only processes whose home is under `e2e/runs/`) | — |
| `goose` | agents | — | install Goose once or set `CODE4ME_E2E_GOOSE_EXECUTABLE` |
| `node` | agents, browser | — | install Node.js 18+ (22 is verified) |
| `codex_acp` | agents | `npm ci` + `npm run build` of `code4me2/dev/codex-acp-proxy/codex-acp` (gitignored outputs) and a launcher at `e2e/.cache/agents/bin/codex-acp` | the build log; or `CODE4ME_E2E_CODEX_EXECUTABLE` |
| `website_deps` | browser | `npm ci` in `src/website` when `node_modules` is absent | the install log |
| `browser_python` | browser | `e2e/.browser-venv` with Playwright 1.63.0 and its Chromium (browsers go to Playwright's shared per-user cache) | the venv command (an explicit `CODE4ME_E2E_BROWSER_PYTHON` is never replaced) |

Provisioning logs go to `e2e/.cache/setup/`. The first backend image build
downloads several GB. The image tag `code4me2-server-backend:latest` is the one
`docker-compose.dev-arm.yaml` uses: the harness rebuilds it only when its
`requirements.txt` differs from the checkout, which is the image the dev stack
needs too. Set `--set stack.image=<tag>` to keep a separate e2e image. One-time host tools the harness never installs: Docker
(or Colima), a JDK, Node.js, Python 3.10+ and Goose. The harness itself uses only
the standard library; the harness unit tests additionally import the server's
`research.*` package from the server venv.

Each test recreates its backend with current server source mounted read-only and
applies Alembic migrations; ordinary source edits need no image rebuild. The
harness uses synthetic `backend.env`, never the developer's `.env`.

The browser runner builds current website source into its run directory and
pins login and research API traffic to its local proxy. It needs no manually
started web server or previously seeded account.

## Real agents: Goose and Codex

A `goose` or `codex` study arm is participant-installed (`BYOA_EXTERNAL`), so
the harness only launches an agent it can identify. The `agents` layer and the
standalone probe use no Compose stack and no IDE. `agent-probe` is the only
command exempt from the workspace lock.

```bash
cd code4me2-server/e2e
# what the agents layer runs: a real ACP turn against a local deterministic provider
python3 -m code4me_e2e agent-probe --framework goose --local-provider --json
python3 -m code4me_e2e agent-probe --framework codex --local-provider --json
# identify only: path, size, sha256 and --version; no session and no model turn
python3 -m code4me_e2e agent-probe --framework goose --detect-only --json
```

Codex runs through the plugin's vendored ACP adapter
(`code4me2/dev/codex-acp-proxy/codex-acp`, the adapter the plugin ships).
`setup --layer agents` (and the gate) builds it; `agent-probe`, which runs
outside the workspace lock, never builds it and reports `missing_binary` until
it is built instead of falling back to another `codex-acp` on `PATH`. Goose is
the host installation.

The executable is resolved in this order:
1. `--executable` or the `agent.executable` setting;
2. `CODE4ME_E2E_GOOSE_EXECUTABLE` / `CODE4ME_E2E_CODEX_EXECUTABLE`;
3. a release-style `agent.agent_command` / `agent.agent_package`;
4. for Codex, the vendored adapter (and nothing else);
5. for Goose, `PATH`, then the plugin's known per-user install locations.

Each probe records the absolute path, size, sha256 and the first `--version`
line. It runs under an isolated agent home with ambient provider credentials
removed, and exercises `initialize` → `session/new` → one `session/prompt` over
stdio with fail-closed host responses.

With `--local-provider` (and in the `agents` layer), the agent talks to a
loopback provider that answers with a unique token:
- Goose uses Chat Completions;
- the Codex adapter uses the Responses API through `CODEX_PROXY_URL`.

A pass needs both a fresh request at that provider and the token in the answer.

A missing binary, missing authentication, exhausted quota or protocol failure is
a typed `BLOCKED` reason (`missing_binary`, `missing_auth`, `quota`,
`protocol`) with exit code 1. `BLOCKED` is never a pass. `--home DIR` overrides
the isolated agent home, which must be empty. `--run-dir DIR` keeps the probe
transcript and stderr log. Without `--local-provider`, the probe uses the
agent's own provider configuration, which the isolated home does not have, so
expect `missing_auth` at the prompt.

`codex-acp` is launched without a subcommand. The bare `codex` CLI does not
speak ACP on stdio and is refused with a `protocol` block rather than left
hanging; pass `agent.agent_command_args` to use another adapter.

Workflow scenarios always use the packaged `code4me2-agent`. Goose and Codex
releases can only be imported through the release manifest, and their arms are
not run by the managed relay, so `--set agent.framework_version=goose|codex`
is refused with `FRAMEWORK_NOT_IN_WORKFLOW`.

For Goose the probe uses the *gateway shape* a study arm receives: `OPENAI_HOST`
is the provider origin, `OPENAI_BASE_PATH` is the research gateway path
(`api/research/inference/v1/chat/completions`), `OPENAI_API_KEY` is a random
bearer the provider must see back, `GOOSE_PROVIDER=openai` and
`GOOSE_PATH_ROOT` isolates Goose's own state while the isolated home carries a
deliberately *poisoned* `.config/goose/config.yaml` (another provider, a dead
host). A pass proves the pinned Goose honours the env-driven gateway
configuration over its own config. `--quota-exhausted` makes the provider answer
`402 quota_exhausted` (the research gateway's refusal) and passes only when the
probe is `BLOCKED` with reason `quota` after no more than Goose's two calls of a
normal turn (the reply and the session-description call) — no retry storm.
Goose 1.51 reports that refusal over ACP as `credits_exhausted` with its own
generic "add more credits" wording; the study-specific explanation reaches the
participant through the plugin's status banner.

### Unverified boundaries

- **Real provider authentication** is not exercised (the provider is a local
  fixture); the budget refusal *shape* is, through `--quota-exhausted`. A passing
  probe is not evidence that a paid provider works for a participant.
- **Codex model binding:** the adapter's `CODEX_MODEL` is not applied when it
  talks to a custom gateway. The local provider sees Codex's default model name,
  so the probe does not assert the model.
- **JetBrains-hosted chat** (AI Assistant's chat panel and JetBrains account
  sign-in) is not automated. The UI layer drives setup and registration; the
  registered ACP proxy and the real agents are exercised over stdio instead.
- Goose/Codex builds and tool catalogues vary by installation. The probe records
  the exact executable it used (`identity` in `--json`), but cannot certify an
  unmeasured build or a tool combination it did not observe.

## Isolation and repeatability

- **Compose project and ports.** The default project is `code4me-e2e`, with:
  - backend port **28008**;
  - database port **25432**;
  - Redis port **26379**;
  - provider stub port **28999**.

  Database, Redis and backend ports bind to loopback. Project-name overrides must
  start with `code4me-e2e-`, so destructive cleanup cannot select the developer's
  Compose project. Every fresh run, and every layer state directory, creates
  unique account emails.
- **IDE runs.** Each IDE run has a private home under `runs/<id>/ide-home/`,
  including its ACP registry, credentials, project and caches. Credentials are
  kept in memory (PasswordSafe `MEMORY_ONLY`), so the sandbox never reads or
  prompts for the developer's macOS keychain. robot-server uses an ephemeral
  port. Cleanup targets only that run's processes. The harness never edits the
  developer's `~/.jetbrains/acp.json`.
- **Native build cache.** Native builds are cached under
  `code4me2-server/e2e/.cache/`, keyed by source content, dependency lock and
  packaging inputs. The release producer
  (`research.study.agents.participant_release native`) writes the manifest the
  backend imports and the archive staged into the plugin overlay. Release
  versions and checked-in runtime resources are not stamped. After changing the
  host Python environment, remove the harness-owned `.cache/agent/` to force a
  native rebuild.
- **One command at a time.** A workspace lock prevents simultaneous harness
  commands. Do not run unrelated Gradle tests or builds in `code4me2/` while the
  gate runs: Gradle sandbox staging and JUnit output directories are shared.
- **What fails the gate:**
  - missing test output;
  - missing UI steps;
  - skipped tests;
  - failing Gradle commands;
  - missing or duplicated browser checks;
  - blocked prerequisites.

## Reports and debugging

Each run prints its artifact directory,
`code4me2-server/e2e/runs/<timestamp>-<id>/`:

- `report.json`: step status, timing, assertions and failure hints, plus
  `layers` and `prerequisites`.
- `state.json`: resumable synthetic accounts, IDs and credentials (mode 0600).
- `http.jsonl`: HTTP exchanges with secret-shaped fields redacted.
- `ui-test-results.json`, `ui-test.log`, `ui-ide.log`: actual IDE assertions and
  Gradle/IDE output.
- `acp-runtime.log`: managed native agent/proxy diagnostics.
- `plugin-test.log`: the live Kotlin fixture result.
- `native-agents/<framework>/`: real-agent probe transcripts and stderr logs.
- `browser-results.json`, `browser-test.log`, `browser-build.log`,
  `browser-state/`: browser checks and the layer's own seeded state.
- `logs/backend.log`: backend diagnostics captured on failure.

Run directories are private and ignored by Git. Keep `state.json`, IDE homes and
raw process logs local, because they can contain test credentials or synthetic
conversation content. Share a reviewed `report.json` when reporting a failure.

A synthetic admin is promoted through SQL because the API has no admin-creation
endpoint; this is recorded as `NO_ADMIN_GRANT_ENDPOINT`, not a skipped check.
The imported agent release is QUALIFIED because the producer's own self-check
and ACP `initialize` results passed. That is test setup, not production
conformance certification.

For focused debugging, run the module from this directory:

```bash
cd code4me2-server/e2e
python3 -m code4me_e2e setup --check
python3 -m code4me_e2e doctor --json
python3 -m code4me_e2e ui-test --keep-ide --keep-stack
python3 -m code4me_e2e plugin-test --keep-stack
python3 -m code4me_e2e run --from bootstrap --run-dir runs/<id> --keep-stack
python3 -m code4me_e2e step telemetry --state runs/<id>/state.json --json
python3 -m code4me_e2e stack status
python3 -m code4me_e2e stack down
```

`ui-test`, `plugin-test`, `run` and `step` do not provision; run `setup` first
when you use them on a fresh host. The exit code of `run` covers only the steps
that invocation ran; the report (and its `ok`) keeps earlier results of the run. `--keep-ide` is an explicit debugging escape
hatch: it leaves that sandbox open, and the next gate stops it as a leftover.

Resuming a run:
- Resumed HTTP layers authenticate again, because IDE/fixture login invalidates
  older cookies.
- Resume only against the same retained stack and a compatible scenario.
- Use a fresh run after runtime/profile changes or enrollment revocation.
- Stack teardown deletes the disposable database, so saved IDs cannot resume
  against a newly created one.

Scenario overrides work on every test layer:

```bash
./test --scenario scenario.example.json --keep-stack
./test --layer backend --set stack.backend_port=28009
```

The example lists supported settings. Without an explicit `base_url`, it follows
`stack.backend_port`. Mutating commands require this disposable loopback origin;
`doctor --base-url URL` is the read-only exception. The historical step ID
`plugin_join` now exercises the **website** consent API; IntelliJ discovers an
existing enrollment automatically.

## Harness regression tests

These need no Docker, IDE, real agent or network provider:

```bash
cd code4me2-server
PYTHONPATH=e2e .venv/bin/python -m unittest discover -s e2e/tests -v
```

What the tests cover:
- `tests/test_prereqs.py`: every check and provisioning decision, with patched
  commands; nothing is started, built or installed.
- `tests/test_real_agents.py`:
  - discovery and identity;
  - the typed `BLOCKED` reasons and the Codex ACP guard;
  - scoped ACP telemetry, checked against the server's own normalizer;
  - stub receipts and registered-entry identity.

  It uses temporary directories and recorded fake transcripts only.
- `tests/test_suite_coverage.py`: layer gating, the browser inventory, the
  persisted re-reads and browser state isolation.

The normalizer contract check imports the server's editable `research.*`
package. With an interpreter that lacks it, the check is reported as
**skipped** with an explicit reason, never as a pass. The venv command above
runs it. The three `test_classic_provider_e2e` tests are opt-in live checks
against paid OpenRouter (`E2E_CLASSIC_PROVIDER=1`) and are skipped otherwise.

## Continuous integration

`.github/workflows/e2e.yml` runs the harness unit tests and the `backend` or
`browser` layer, on demand and on pushes that touch `e2e/`.
- Both repositories must be checked out side by side; the workflow sets
  `CODE4ME_E2E_WORKSPACE` to the directory holding them.
- Missing prerequisites are provisioned the same way as locally, including
  `e2e/.venv` for the native builds.
- The IDE/UI layer needs a graphical runner and is not part of the default CI
  job.

The browser scripts are vendored at `browser/` next to the harness. A developer
checkout may still keep them at `<workspace>/task07-browser/`; the harness uses
that location as a fallback.
