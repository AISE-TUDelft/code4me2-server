# SWE-bench runs for the Code4Me agent

This folder runs the packaged Code4Me agent (`code4me2_agent`) on SWE-bench Verified tasks
and keeps the agent's own telemetry for every task. It does not use the plugin, the IDE or
the ACP transport. A small driver calls the agent core (`EchoAgentCore.handle_prompt`)
directly, inside each task's container.

It is self-contained:
- It has its own `pyproject.toml` and virtual environment.
- Nothing outside this folder imports it.
- The server's test suite does not collect it.

## How a task runs

1. **Runtime.** `code4me-swebench runtime` builds the agent wheel from this checkout. It
   installs the wheel, together with the release's locked dependencies
   (`packaging/requirements-runtime.lock`), on a portable Python 3.13. The result goes into a
   Docker volume named after the content hash of the wheel and the lock file.
   - Why not the native release bundle: it is built on Ubuntu 24.04 and needs a newer glibc
     than the SWE-bench images (Ubuntu 22.04) provide.
2. **Container.** For each task, the official instance image
   (`swebench/sweb.eval.x86_64.<id>`) starts with the runtime mounted read-only at
   `/opt/code4me`.
3. **Agent run.** `driver.py` runs the agent once in `/testbed`, the repository at the task's
   base commit.
   - Commands run with the image's `testbed` conda environment on `PATH`, exactly where the
     grading tests run.
   - The prompt contains the issue text only: no hints and no tests (see `prompt.py`).
   - The `ask_user` tool is removed and approvals are automatic.
4. **Patch.** Everything the agent changed is collected as `git diff --cached --binary`
   against the starting commit. That diff becomes the prediction.
5. **Grading.** The official harness (`swebench.harness.run_evaluation`, swebench 5.0.2)
   grades the prediction in a fresh container.

## Network sandbox

Task containers get no internet. Before 2026-10-09 they ran on Docker's default network, and
both agents used it to fetch the upstream fix: `pip download` of a newer release, GitHub pull
request diffs, `git fetch`. Results from those runs are not valid.

- **Our agent** runs inside the container and needs its model API. The container therefore joins
  the `--internal` network `code4me-swebench-internal`, which has no route out.
  - Its only exit is `code4me-swebench-proxy`, a stdlib CONNECT proxy (`egress_proxy.py`) that
    allows the model host on port 443 and nothing else.
  - The agent and every command it runs get `HTTP(S)_PROXY` pointing at the proxy.
  - Refused attempts are logged (`docker logs code4me-swebench-proxy`). Each task records its own
    in `host.json` as `egress_denied`.
- **mini-swe-agent** calls the model from the host, so its containers run with `--network none`.

## Telemetry

Each task writes `trace.jsonl`. It is the same `code4me.agent.event.v1` event stream that
the agent uploads to `/api/agent/events/ingest` in a study, with raw capture on. It includes:
- every model call: model, finish reason, token usage, provider cache hits, latency and call
  purpose, plus the raw request and response;
- every tool call: arguments, result, status, duration and denials;
- commands with parsed test-runner summaries;
- run start and end, with the stop reason.

`telemetry.json` summarises each trace. `report.json` and `instances.csv` aggregate the run.

`code4me-swebench check [RUN ...]` validates every trace. It checks that:
- each trace has one run and one schema;
- sequence numbers are 1..N and timestamps are in order;
- there is a start and an end;
- every model request is answered: by `agent.model.completed`, by a main-loop failure
  (`agent.adapter.loop_failed`), or by a failed optional side call counted in
  `agent.run.completed` (`metrics.failed_side_calls`);
- every tool call has an outcome;
- parent links resolve;
- token metrics equal the provider's raw usage;
- the provider key does not appear, when it is set in the environment.

Agent builds before 2026-10-09 recorded nothing when a model call failed, so a failure shows up
as one unanswered request.

Traces contain prompts and tool output from public repositories, and they are large. `runs/`
is git-ignored.

The provider key never reaches disk or the trace. Only its environment variable *name* is
written. The value goes to `docker exec -e NAME`, and the agent's command environment
allowlist keeps it away from the commands the model runs.

## Requirements

- Docker that can run `linux/amd64` images. The images are published for amd64 only.
  - x86-64 Linux works natively.
  - On Apple Silicon, use Colima with `vmType: vz` and `rosetta: true` (this machine's setup).
- Disk: about 1–2 GB per task image. Pass `--remove-images` to delete each image after it is
  graded.
- `uv`.
- A provider key in an environment variable. The default is OpenCode Go
  (`https://opencode.ai/zen/go/v1`, key in `OPENCODE_GO_API_KEY`). The agent adds the
  `x-opencode-session` header that OpenCode requires.

## Usage

```bash
cd code4me2-server/benchmarks/swebench
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q                       # unit tests (no Docker, no network)

export OPENCODE_GO_API_KEY=...                       # never commit it
.venv/bin/code4me-swebench runtime                   # once per agent change (~30 s)
.venv/bin/code4me-swebench run --run-name v4flash-mini-s1 --subset verified-mini \
    --model deepseek-v4.1-flash --remove-images
.venv/bin/code4me-swebench report --run-name v4flash-mini-s1
```

`run` behaves as follows:
- It is resumable. Finished tasks are skipped, and a run directory refuses different
  settings.
- It grades each task as soon as the agent finishes. Pass `--no-eval` to skip grading, then
  grade later with `code4me-swebench evaluate`.
- Useful flags:
  - `--limit N` and `--instances ID ...` for smoke tests;
  - `--workers N` for parallel tasks (keep it at 1 on an 8 GB Colima VM);
  - `--max-iterations` (default 250 model calls per task, mini-swe-agent's stock step limit);
  - `--context-tokens` (default 400k: trimming should almost never be needed; see below);
  - `--task-timeout` (default 10800 s, a guard against hangs);
  - `--memory` and `--cpus` per container;
  - `--no-self-review` to switch off the agent's end-of-task self-review.

### Context window

Agent builds before 2026-10-09 broke provider prompt caching on long tasks. Once a single turn
outgrew the memory window, the agent elided "just enough" old tool output on every call. That
changed the request prefix on every call, so almost every later call was a full cache miss.
Their chars/4 estimate was also about 20% (up to 2×) below the provider's count.

The agent now does three things:
- calibrates its estimate from the reported usage;
- elides in one step down to 60% of the budget;
- reports the compaction as integer metrics on the next `agent.model.requested`
  (`context_elided_units`, `context_tokens_before`/`_after`, `token_scale_milli`).

No new telemetry event types are added, because the study counts steps as events (constraint
C1 of the 2026-09-26 tiers run).

Benchmarks also set a 400k window. The run `rebench-v4flash-s1` (100k window, old build) is
kept as evidence of the old behaviour.

### Agent behaviour in benchmark runs

The benchmark config sets the agent's `autonomous` flag. IDE sessions never do, and study
profiles cannot. With it set:
- **Continuation.** A turn that ends announcing more work ("Let me…", "I'll…", a trailing colon,
  but not "Let me know"), or whose answer was cut off by the output limit, is continued.
  - At most two continuations per turn.
  - Each needs a tool run since the previous one.
  - The request that follows carries `continuation_nudge: true`.

The following apply in every mode, including IDE sessions, as of 2026-10-09:
- **Reasoning pass-back.** The model's reasoning goes back as `reasoning_content`, exactly as
  returned.
  - DeepSeek V4/V4.1 models also get `""` on every other assistant message when tools are sent,
    as their API requires.
  - A server that rejects the field gets nothing more after one retry.
- **Verification.** Only commands that run code count as verification before stopping. Inspection
  (`git status/diff`, `ls`, `cat`, `grep`), file moves and package installs do not.
- **Arguments.** Out-of-range size and paging arguments are clamped, and a plain argv string is
  split. Both are reported to the model as `argument_notes`.

### Self-review is off in our main runs

With DeepSeek V4 Flash, the agent's end-of-task self-review takes 54% of task time. The
model reasons for about 19k tokens on a 2k-token review prompt, and 4 of 20 reviews stalled
until the server cut them off after 20 minutes. The review led to further edits in only
3 of 22 tasks.

Main runs therefore pass `--no-self-review`. The setting is recorded in `manifest.json` and in
every task's `agent-config.json`, and results must state it. The paired Verified-20 runs
(`verified20-v4flash-s1` with it on, `verified20-v4flash-noreview-s1` with it off) measure
its effect.

### SWE-rebench

SWE-rebench (`nebius/SWE-rebench-leaderboard`) builds tasks from fresh GitHub issues each month,
so models are less likely to have trained on them. The same `run` command handles it:

```bash
.venv/bin/code4me-swebench --dataset nebius/SWE-rebench-leaderboard --split 2026_03 \
    run --run-name rebench-v4flash-s1 --subset rebench-2026_03-newest-50 --remove-images
```

- **Grading** uses SWE-rebench's own fork of the harness (pinned commit, separate
  `.venv-rebench`, created on first use). The fork is swebench 4.0.3, which conflicts with
  swebench 5.
- **Image layout.** SWE-rebench images keep conda in `/opt/conda`, not `/opt/miniconda3`.
  The runner finds either, and fails the task if there is no `testbed` environment.
- **Leak files.** Some older SWE-rebench images ship `/issue.md` and
  `/swebench_instance.json`. The runner deletes them before the agent starts and records
  this in `host.json`.

### Baseline: mini-swe-agent (optional, not used in the main runs)

`baseline-mini` runs mini-swe-agent (bash-only, version 2.4.6, stock `swebench.yaml`) on the
same tasks with the same model and grader. It writes the same run-directory layout, so
`report` works on it unchanged. It runs in its own `.venv-mini`.

Only the following are overridden:
- the model endpoint (`mini-overlay.yaml`);
- the container platform;
- cost tracking, because litellm has no price for these models.

OpenCode's session header comes from `mini_baseline/opencode_model.py`. Tasks run in batches,
so `--remove-images` can keep disk use bounded.

```bash
.venv/bin/code4me-swebench --dataset nebius/SWE-rebench-leaderboard --split 2026_03 \
    baseline-mini --run-name rebench-mini-v4flash-s1 --subset rebench-2026_03-newest-50 --remove-images
```

Known differences from our runner:
- Both stop at 250 model calls (mini-swe-agent's stock step limit and our default). mini's stock
  $3 cost limit never applies here: litellm has no price for these model ids.
- It submits the diff it writes itself (`git diff -- <files>`), where we stage everything
  (`git add -A`).

### Main runs: the SWE-rebench leaderboard protocol

We compare against the official SWE-rebench leaderboard (swe-rebench.com), not against a baseline of
our own. It publishes results for one fixed scaffold and many models on fixed task windows. Running
our agent with the same model, on the same window and with the same metrics gives a comparison in
which the scaffold is the main difference. `references.json` holds the published numbers, read from
the site on 2026-10-14.

| | Leaderboard | Our runs |
| --- | --- | --- |
| Tasks | a half-month-aligned window; HF split `2026_03` is exactly 2026-03-01→05-15 (110 tasks) | the same window files (`subsets/rebench-2026_03-*`), dataset revision pinned |
| Runs | 5 per model | `--repeats 5` |
| Metrics | mean resolved rate over the 5 runs (every task counts), SEM = sd/√5, pass@5, tokens and cost per problem | `aggregate`, same formulas |
| Steps | no cap (runs reached 492) | 500 model calls |
| Context | 128k; past that the task fails | 128k; our agent compacts instead |
| Command timeout | 300 s | 300 s |
| Sampling | model defaults (V4: temperature 1.0, top_p 1.0) | sent explicitly |
| Network | open (pip and GitHub reachable) | model API only (stricter) |
| Scaffold | in-house minimal ReAct agent | our agent: the thing being compared |

**Which model.** The leaderboard's only DeepSeek Flash entry is DeepSeek-V4 Flash [high]. It scores:
- 38.36 ± 0.97 on the full 2026_03 window (pass@5 57.3, $0.069 and 3.0M tokens per problem);
- 36.09 ± 1.63 on its 46-task March window (2026-03-01→04-01, pass@5 56.5), our planned set.
DeepSeek V4.1 Flash has no leaderboard number. For a same-model comparison, run
`deepseek/deepseek-v4-flash`.

DeepSeek's own API now answers to that name with V4.1, so on OpenRouter V4 Flash comes only from
third-party hosts. Pin one fp8 host, and record which one. Which host the leaderboard used is not
published.

**OpenRouter pinning.** OpenRouter serves one model from many hosts, which differ in:
- precision (fp4, fp8 or full);
- output limit;
- price.

By default it picks a cheap host per request, and it may pick a different one on every call. So:
- `--openrouter-upstream SLUG` sends `provider: {only: [SLUG], allow_fallbacks: false,
  require_parameters: true}`. Every request goes to that host, or it fails. A run with an
  `openrouter.ai` URL and no pin is refused.
- OpenRouter runs also send `reasoning: {effort: "high"}`, `top_p: 1.0` and `temperature: 1.0`.
  Override them with `--extra-body` or `--temperature`.
- All of these fields are recorded in `manifest.json` and in each task's `agent-config.json`.
- `telemetry.json` lists the hosts (`upstreams`) that served each task and the billed `cost_usd`.
  `check` fails a task if any call names another host, or names none at all.

```bash
export OPENROUTER_API_KEY="$(cat ~/.config/code4me/openrouter-key)"   # file is chmod 600
OR=(--base-url https://openrouter.ai/api/v1 --api-key-env OPENROUTER_API_KEY
    --model deepseek/deepseek-v4-flash --openrouter-upstream deepinfra)
RB=(--dataset nebius/SWE-rebench-leaderboard --split 2026_03)

# Pre-flight, a few cents: the key, the pin and DeepSeek's reasoning pass-back through OpenRouter.
.venv/bin/code4me-swebench "${RB[@]}" run --run-name lb-preflight \
    --instances tox-dev__tox-3931 --no-self-review --remove-images "${OR[@]}"
.venv/bin/code4me-swebench check lb-preflight            # upstreams/cost_usd: telemetry.json

# Main: 5 independent runs of the 46-task March window, task by task (each image is pulled once).
# For the full 110-task window use --subset rebench-2026_03-0301-0515.
.venv/bin/code4me-swebench "${RB[@]}" run --run-name lb-v4flash \
    --subset rebench-2026_03-0301-0401 --repeats 5 --no-self-review --remove-images "${OR[@]}"
.venv/bin/code4me-swebench check lb-v4flash-r1 lb-v4flash-r2 lb-v4flash-r3 lb-v4flash-r4 lb-v4flash-r5
.venv/bin/code4me-swebench aggregate --run-name lb-v4flash --reference deepseek-v4-flash-high
```

`aggregate` reports the run's window and every 2026_03 window it fully covers, next to the
reference, with the difference and its z score. A difference counts at |z| ≥ 2:
- 46-task March window: roughly 5 points;
- 64-task window: roughly 6 points;
- full 110-task window: roughly 3 points.

**Cost.** Per task, using our measured token usage (3.9M input tokens, 95% from the cache; 44k
output tokens):

| Host | Per task | 5 × 46 | 5 × 64 | 5 × 110 |
| --- | --- | --- | --- | --- |
| Baidu fp8 | $0.040 | $9 | $13 | $22 |
| StreamLake fp8 | $0.072 | $17 | $23 | $40 |
| DeepInfra fp8 | $0.093 | $21 | $30 | $51 |
| V4.1 Flash, DeepSeek's own API, off-peak | $0.068 | $16 | $22 | $37 |

- The leaderboard's own token use (3.0M per problem) would be about 25% lower.
- Hosts change prices; `report.json` shows what was actually billed.
- **Time:** about 10 minutes per task-run with one worker: 5 × 46 takes about 1.6 days, 5 × 64
  about 2.2 days and 5 × 110 about 3.8 days.

**Caveats.**
- **Unpaired comparison.** The leaderboard publishes aggregates only, no per-task results, for
  this window. The comparison is therefore not paired.
- **Contamination.** V4 Flash (released 2026-04-24) carries the leaderboard's contamination flag
  on every window except 2026-05-01→05-15 (21 tasks).
- **Interface text.** 24 tasks carry an "Interface" section in the issue text. 15 others have one
  in a separate dataset field that our prompt leaves out; whether the leaderboard showed it is
  unknown.
- **Network.** Their sandbox was online and ours is offline. That can cost us tasks that need a
  package install.

**Key cap.** Give the key a credit limit in the OpenRouter dashboard.
- An exhausted key answers 402 or 403. Like 401, 404 (no model or pinned host), rate limits and
  outages, that counts as a provider outage, not a failed task.
- The task is recorded as `infra_error`, three in a row stop the run, and `requeue` releases them.

## Subsets (`subsets/`)

| File | What it is |
| --- | --- |
| `verified-mini.txt` | The public SWE-bench Verified Mini: 50 tasks, django and sphinx only, chosen to track Verified's resolve rate. Comparable with other published Mini numbers, but covers only 2 repositories and skews harder (8 of 50 tasks take an hour or more, against 45 of 500 in the full set). |
| `verified-stratified-20.txt` | 20 tasks with the same stratification (seed 0): the quick check of the setup. |
| `rebench-2026_03-0301-0515.txt` | The SWE-rebench leaderboard window 2026-03-01→05-15: all 110 tasks of split 2026_03 (dataset revision `34d5a588`). The main task set. |
| `rebench-2026_03-0301-0401.txt` | The leaderboard window 2026-03-01→04-01: 46 tasks (V4 Flash: 36.09 ± 1.63). |
| `rebench-2026_03-0401-0515.txt`, `-0415-0515`, `-0501-0515` | Its leaderboard sub-windows: 64, 39 and 21 tasks. Only the last postdates DeepSeek V4's release (2026-04-24). |
| `rebench-2026_03-newest-50.txt` | Earlier runs only: the 50 newest tasks of split 2026_03 (2026-04-10 to 05-12). It is not a leaderboard window, so no official number exists for it. |
| `verified-stratified-50.txt` | 50 tasks drawn from all of Verified, proportional to repository × difficulty (largest remainder, seed 0, `code4me-swebench select`). It covers 10 repositories but needs more image storage. |

## Reporting

- The resolve rate counts every task in the subset. Tasks that were not run or not graded
  count as unresolved.
- `report.json` gives a Wilson 95% interval. At 50 tasks the interval is roughly ±13 points.
- For claims, use the leaderboard protocol above: 5 runs, `aggregate`, and a published reference
  for the same model and window.
- SWE-bench Verified is widely regarded as saturated and partly contaminated. Use it to check
  that the harness works, and add a newer benchmark (SWE-rebench, SWE-bench Pro) for
  capability claims.

## Not covered here

- **Leaderboard windows after 2026-05-15.** The newest window (2026-05-15→07-01, 111 tasks) is
  published only as a Harbor dataset, and most of its tasks are not Python. It is the only window
  with per-run trajectories, which would allow a paired comparison. V4 Flash was not run on it.
- **The ACP and plugin path.** Driving the same agent over ACP (for example with Harbor's
  ACP agent support or the research telemetry proxy) would exercise the transport the study
  uses. It is not implemented.
- **Uploading traces to a Code4Me server.** Ingest is idempotent by event id, so traces could
  be replayed after a run under a benchmark account. It is not implemented.
