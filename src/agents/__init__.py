"""Server-side agent subsystem.

Layout:

* ``provider``     — resolves the generic OpenAI-compatible upstream from a profile
* ``normalize``    — wire-format normalization (Chat Completions / Responses API)
* ``tools``        — catalogue of selectable tool names, per runtime
* ``telemetry``    — the per-inference-call record written by the proxy path
* ``event_writer`` — persistence for proxy-observed events
* ``ingest``       — translation + persistence for runtime-self-reported events
* ``inference``    — the authed relay behind POST /api/agent/inference
* ``registry``     — sticky, server-authoritative A/B profile assignment
* ``lifecycle``    — aggregating a finished task's events into totals

Note this package is a *relay and telemetry sink*, not an agent runtime. The
built-in runtime lives in the separate ``code4me2_agent`` package.
"""
