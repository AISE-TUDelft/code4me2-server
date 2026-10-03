# Agent configuration contract (ISSUE-03)

Recorded: 2026-09-19. This note records what the current release enforces and
what remains open, so a study arm's label cannot silently disagree with what runs.

## Enforced today

- **Framework ↔ release distribution mode.** A profile's `framework_version` and
  its pinned release are validated together at profile create/update and again
  when a study freezes its profile selection
  (`research/study/agents/distributions.py::validate_profile_configuration`):
  `code4me2-agent` requires a `PACKAGED` release; `goose`/`codex` require a
  `BYOA_EXTERNAL` release with a declared command/package identity. A mismatch is
  a typed 4xx (`FRAMEWORK_DISTRIBUTION_MISMATCH`) before enrollment.
- **Qualification.** The release must be `QUALIFIED` (derived from conformance
  evidence bound to the exact artifact/platform — ISSUE-10); withdrawn or
  unqualified releases are refused (`RELEASE_NOT_QUALIFIED`/`RELEASE_WITHDRAWN`).
- **Approval-option evidence.** The profile's approval option must be covered by
  the release's passing conformance cases (`APPROVAL_OPTION_UNVERIFIED`).
- **Tools.** The selected tools must belong to the framework's catalogue
  (`TOOLS_NOT_SUPPORTED`).
- **Packaged binaries resolve per release + platform.** The bootstrap pins the
  assigned release's archive for the participant's platform; the plugin installs
  exactly that archive (verified cache or its exact GitHub Release), and the
  digest pin check remains fail-closed (no PATH fallback).
- **BYOA executable configuration (ISSUE-03 Path A).** A qualified BYOA release
  declares `byoa_config`: one binding per governed profile field
  (`model`, `temperature`, `max_steps`, `tools`, `approval_policy`), each with a
  transport (`env` → environment variable, `arg` → argv pair), an optional
  `value_map` (server vocabulary → agent vocabulary) and a list `format`
  (`csv`/`json`). Profile/study creation rejects any profile field the release
  does not translate (`BYOA_CONFIG_UNMAPPED`) and any list binding without a
  list format (`BYOA_CONFIG_FORMAT_INVALID`). The bootstrap manifest projects
  the bindings plus the frozen profile fields; the plugin applies them to the
  BYOA launch (`ByoaConfiguration.kt`: mapped argv + `--agent-env KEY=VALUE`
  overrides) and refuses to launch if a set field has no binding. The proxy
  passes the overrides only to the agent child environment.
- **Provider connection and the research inference gateway.** The provider
  connection itself is never translated: the server-held key stays on the
  server. A Goose release (a *gateway-bound* framework, see
  `INFERENCE_GATEWAY_FRAMEWORKS`) must additionally declare five *runtime*
  bindings the plugin fills at launch — `inference_gateway_host`
  (the research server origin), `inference_gateway_base_path`
  (`api/research/inference/v1/chat/completions`), `inference_gateway_credential`
  (the participant's inference capability; `env` transport only, never argv),
  `provider_kind` (`openai_compatible`, translated through `value_map`, e.g. to
  Goose's `openai`) and `state_dir` (a plugin-owned isolated agent state
  directory). They are never profile fields. A Goose profile pinned to a release
  without them is refused (`INFERENCE_GATEWAY_UNBOUND`) at profile/study
  creation, at recipe import and at bootstrap. The bootstrap manifest then carries
  an `inference_gateway` block with a signed, scoped inference capability
  (audience `inference`, scope `inference:relay`), the agent's model calls go
  through `POST /api/research/inference/v1/chat/completions`, and every call is
  metered against the participant's budget. Codex releases are not gateway-bound:
  they sign in with ChatGPT and are neither relayed nor metered.
- **Tools enforced at the gateway (`gateway` transport).** Goose reads no tool
  selection from its environment: it offers the model every tool of its enabled
  extensions on every call (Goose 1.51: `shell`, `edit`, `write`, `tree`,
  `analyze`, `read_image`, `load`, `load_skill`, `delegate`, `todo__todo_write`,
  `apps__*`, `extensionmanager__*`). A Goose release may therefore bind `tools`
  as `{"field": "tools", "transport": "gateway", "key": "tool_allowlist",
  "format": "json"}`. The plugin sets nothing for it, and the research inference
  gateway enforces the frozen profile's selection on both sides of the call:
  - **request:** the model is offered only the selected tools. An empty
    selection means no tools; a `tool_choice` naming a withheld tool is dropped
    or narrowed, and `parallel_tool_calls` is dropped with the last tool.
  - **response:** a call the model still makes to a withheld tool is removed
    from the body or stream before Goose sees it. The stream is read line by
    line as Goose reads it and filtered by Goose's own assembly rules
    (`agents/tool_call_filter.py`), so a withheld call cannot be rebuilt from
    later pieces; a line the filter cannot judge (unreadable, or shaped so that
    only Goose could read it) is dropped. When a turn's calls are all withheld,
    the participant sees a short notice. Removals and dropped lines are logged.

  Tool names match exactly. A selection that matches none of the offered tools
  (an agent build that renamed them) is logged as a warning. A release that
  does not declare the binding keeps Goose's own tool definitions untouched.

  Only a release that provably launches a gateway-bound agent can declare it:
  its identity must name only Goose and its command (else package) must be
  Goose's own executable. Anything else, a Codex release or a mis-declared one,
  is refused (`BYOA_CONFIG_UNENFORCEABLE`) and never offers `tools` as
  configurable. The plugin counts the binding only when the manifest carries an
  inference gateway.

  The binding is opt-in per release, and it is not in the canonical example:
  profiles written before it default to `tools_json = "[]"`, which would become
  no-tools arms. Plugins older than this contract refuse such arms, failing
  closed, so ship the plugin first.

## Explicit seams

- The participant plugin bundles no agent: `PackagedAgentInstaller` installs
  the assigned release's archive for the platform from the verified cache or its
  exact GitHub Release (SHA-256 pinned by the bootstrap), or the activation
  blocks (`RUNTIME_UNAVAILABLE` when retryable, otherwise `PREPARATION_FAILED`).
  PATH is never consulted.
- Real Goose/Codex key names are release-owned data (the `byoa_config`
  bindings), not plugin constants. Conformance evidence for a release that
  declares a mapping remains part of release qualification (ISSUE-10/04); the
  plugin-side tests use a recording double to assert the declared mapping is
  applied to argv/env exactly.

## Deliberate decision

The reverted blanket study-creation guard (`feffcd4`/`157337d`) is not
reintroduced. Rejections are per-combination (framework × distribution mode ×
qualification × approval evidence × field coverage), so a legitimately qualified
BYOA study stays creatable while an unexecutable combination fails early with a
typed error.
