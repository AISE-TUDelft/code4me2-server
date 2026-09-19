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

This starts a disposable backend, builds the current plugin and native runtimes,
opens an isolated IntelliJ sandbox, drives the UI, exercises the registered ACP
proxy, and checks the resulting telemetry in PostgreSQL. It then runs the live
Kotlin fixture and HTTP workflow through enrollment revocation. No manual IDE
startup, account creation, study setup, or model API key is needed.

On success the default command closes its IDE and removes its Compose stack and
volume. On failure it closes the IDE and keeps the stack and run artifacts for
diagnosis. A nonzero exit code means the gate did not pass.

## Choose the checks

| Command | Coverage |
| --- | --- |
| `./test` | 19 steps: real IDE + native ACP + Kotlin fixture + HTTP workflow |
| `./test --layer backend` | 17 HTTP workflow steps, no IDE or native builds |
| `./test --layer plugin` | Kotlin IntelliJ fixture + HTTP workflow, no IDE UI |
| `./test --layer browser` | `browser/` suite: 27 headless Chromium checks |
| `./test --json` | Same default gate, with a JSON summary on stdout |
| `./test --keep-stack` | Preserve the disposable backend after success |

`all` is the default layer and includes IDE, plugin fixture, and HTTP checks.
The browser layer is separate because it requires Playwright and Node.js.

The full gate uses the real server, plugin, packaged agent and packaged proxy.
Only the external model provider is replaced by a deterministic local HTTP
provider. The harness acts as an ACP client using the exact proxy command,
arguments and managed-auth environment registered by the IDE. It checks the
response token and requires telemetry from both the IDE and **that proxy
launch** to reach the database.

The seven UI checks cover plugin loading, settings navigation, sign-in,
automatic enrollment discovery, the status surface, ACP registration and agent
preparation. The Kotlin fixture independently tests the plugin's login,
discovery, signed bootstrap validation and durable spool upload. The browser
suite covers study creation, consent, enrollment handoff, metadata locking,
stopping/cloning a study and authorization controls.

This does not automate JetBrains AI Assistant's chat panel or JetBrains account
sign-in. It tests the registered ACP integration through its public protocol.
The deterministic provider does not test model quality, external provider
availability, or every agent/tool combination.

## Host prerequisites (one-time)

- Docker daemon and Docker Compose (`docker compose` or `docker-compose`).
- Python 3.10+ for the harness; it uses only the standard library.
- A Java environment that can run `code4me2/gradlew`. Gradle provisions the IDE
  and its configured toolchain; the initial download can take several minutes.
- A graphical desktop for the IDE layer. On Linux use an X server such as Xvfb.
  The UI driver uses robot-server's in-process API and needs no macOS
  Accessibility permission. This workflow has been verified on Apple Silicon
  macOS; Linux/Xvfb is not verified here.
- The CPU backend image, normally already available from local development:

  ```bash
  docker build -f code4me2-server/Dockerfile.cpu \
    -t code4me2-server-backend:latest code4me2-server
  ```

  The first build downloads substantial dependencies. Rebuild it when server
  dependencies change. Each test recreates its backend with current server
  source mounted read-only and applies Alembic migrations; ordinary source
  edits need no image rebuild. The harness uses synthetic `backend.env`, never
  the developer's `.env`.

For native agent/proxy builds, the default interpreter is
`code4me2-server/.venv/bin/python`. It must contain the agent dependencies,
the editable telemetry proxy package, and PyInstaller. An alternative setup is:

```bash
python3 -m venv code4me2-server/e2e/.venv
code4me2-server/e2e/.venv/bin/python -m pip install -e ./code4me2-server \
  -e ./code4me2/telemetry-acp-proxy pyinstaller
export CODE4ME_E2E_PYTHON="$PWD/code4me2-server/e2e/.venv/bin/python"
./code4me2-server/e2e/test
```

The browser layer additionally needs `node` and `npm` on PATH, plus Python with
Playwright and its Chromium browser. Website dependencies are installed with
`npm ci` if `node_modules` is absent. For example:

```bash
python3 -m venv code4me2-server/e2e/.browser-venv
code4me2-server/e2e/.browser-venv/bin/python -m pip install playwright
code4me2-server/e2e/.browser-venv/bin/python -m playwright install chromium
export CODE4ME_E2E_BROWSER_PYTHON="$PWD/code4me2-server/e2e/.browser-venv/bin/python"
./code4me2-server/e2e/test --layer browser
```

The browser runner builds current website source into its run directory and
pins login and research API traffic to its local proxy. It needs no manually
started web server or previously seeded account.

## Isolation and repeatability

The default Compose project is `code4me-e2e`, with backend port **28008**, database
port **25432**, Redis port **26379** and provider stub port **28999**. Database,
Redis and backend published ports bind to loopback. Project-name overrides must
start with `code4me-e2e-`; destructive cleanup cannot select the developer's
Compose project. Tests create unique account emails for every fresh run.

Each IDE run has a private home under `runs/<id>/ide-home/`, including its ACP
registry, credentials, project and caches. Its robot-server uses an ephemeral
port. Cleanup targets only that run's processes. The harness never edits the
developer's `~/.jetbrains/acp.json`.

Native builds are cached under `code4me2-server/e2e/.cache/` by source content,
dependency lock and packaging inputs. Gradle stages an E2E runtime overlay with
computed checksums; release versions and checked-in runtime resources are not
stamped. When changing the host Python environment, remove the harness-owned
`.cache/` to force native rebuilding.

A workspace lock prevents simultaneous harness commands. Do not run unrelated
Gradle tests/builds in `code4me2/` while the gate runs: Gradle sandbox staging and
JUnit output directories are shared. Missing test output, missing UI steps,
skipped tests and failing Gradle commands all fail the gate.

## Reports and debugging

Each run prints its artifact directory,
`code4me2-server/e2e/runs/<timestamp>-<id>/`:

- `report.json`: step status, timing, assertions and failure hints.
- `state.json`: resumable synthetic accounts, IDs and credentials (mode 0600).
- `http.jsonl`: HTTP exchanges with secret-shaped fields redacted.
- `ui-test-results.json`, `ui-test.log`, `ui-ide.log`: actual IDE assertions and
  Gradle/IDE output.
- `acp-runtime.log`: managed native agent/proxy diagnostics.
- `plugin-test.log`: the live Kotlin fixture result.
- `browser-results.json`, `browser-test.log`, `browser-build.log`: browser checks.
- `logs/backend.log`: backend diagnostics captured on failure.

Run directories are private and ignored by Git. Keep `state.json`, IDE homes,
and raw process logs local: they can contain test credentials or synthetic
conversation content. Share a reviewed `report.json` when reporting a failure.
A synthetic admin is promoted through SQL because the API has no admin-creation
endpoint; this is recorded as `NO_ADMIN_GRANT_ENDPOINT`, not a skipped check.
The E2E release qualification receipt is test setup, not production conformance
certification.

For focused debugging, run the module from this directory:

```bash
cd code4me2-server/e2e
python3 -m code4me_e2e doctor --json
python3 -m code4me_e2e ui-test --keep-ide --keep-stack
python3 -m code4me_e2e plugin-test --keep-stack
python3 -m code4me_e2e run --from bootstrap --run-dir runs/<id> --keep-stack
python3 -m code4me_e2e step telemetry --state runs/<id>/state.json --json
python3 -m code4me_e2e stack status
python3 -m code4me_e2e stack down
```

`--keep-ide` is an explicit debugging escape hatch: it leaves that sandbox open.
Resumed HTTP layers authenticate again because IDE/fixture login invalidates
older cookies. Resume only against the same retained stack and compatible
scenario. Use a fresh run after runtime/profile changes or enrollment
revocation. Stack teardown deletes the disposable database; saved IDs cannot
resume against a newly created database.

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

These require no Docker or IDE:

```bash
cd code4me2-server/e2e
python3 -m unittest discover -s tests -v
```

## Continuous integration

`.github/workflows/e2e.yml` runs the harness unit tests and the `backend` layer
on demand and on a nightly schedule. Both repositories must be checked out side
by side; the workflow sets `CODE4ME_E2E_WORKSPACE` to the directory holding
them. The IDE/UI layer needs a graphical runner and is not part of the default
CI job; select it with the workflow's `layer` input on a self-hosted runner.

The browser scripts are vendored at `browser/` next to the harness. A developer
checkout may still keep them at `<workspace>/task07-browser/`; the harness uses
that location as a fallback.
