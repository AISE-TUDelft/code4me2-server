"""The workflow steps for the code4me-e2e harness.

Each ``step_*`` function drives the real HTTP API (plus one documented SQL
out-of-band step) and either returns a ``details`` dict or raises
:class:`StepFailure` / :class:`StepBlocked`. The workflow runner wraps them with
timing, persistence and report capture; steps stay plain functions on purpose.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import runtime, stack
from .config import Scenario, redact
from .http import HttpClient, HttpError, redact_text
from .stub_provider import StubProvider

#: Step ids in execution order.
STEP_ORDER = [
    "doctor",
    "create_accounts",
    "grant_roles",
    "enable_researcher",
    "provider_connection",
    "register_release",
    "qualify_release",
    "create_profile",
    "create_study",
    "join_code",
    "plugin_join",
    "bootstrap",
    "acp_prepare",
    "send_message",
    "telemetry",
    "verify",
    "revoke",
]

STEP_DESCRIPTIONS = {
    "doctor": "backend reachable, ACP schema ready, DB tables present, login",
    "create_accounts": "create admin/researcher/participant accounts and log in",
    "grant_roles": "promote the admin account via SQL (no admin-grant endpoint exists)",
    "enable_researcher": "admin enables the researcher account (can_research)",
    "provider_connection": "admin creates the stub provider connection",
    "register_release": "admin imports the producer manifest and its verified archive",
    "qualify_release": "the imported release is usable because its producer tests passed",
    "create_profile": "researcher creates an agent profile pinned to the release",
    "create_study": "researcher creates a study with a fixed agent profile",
    "join_code": "researcher reads the study's join code",
    "plugin_join": "participant resolves the website join code and accepts consent",
    "bootstrap": "participant fetches and independently verifies the signed manifest",
    "acp_prepare": "participant acquires session/project, mints an ACP grant, exchanges it",
    "send_message": "agent creates a managed run and relays one inference turn",
    "telemetry": "agent uploads a canonical telemetry batch",
    "verify": "admin reads the batch receipt and the stored event row",
    "revoke": "researcher revokes the enrollment; bootstrap access is refused",
}


class StepFailure(Exception):
    """A step failed an assertion. Carries report-ready details."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None, fix_hint: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.fix_hint = fix_hint


class StepBlocked(StepFailure):
    """A prerequisite step did not complete, so this step cannot run."""


@dataclass
class StepResult:
    id: str
    status: str
    duration_ms: int
    details: Dict[str, Any] = field(default_factory=dict)
    fix_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "details": redact(self.details),
            "fix_hint": self.fix_hint,
        }


class Ctx:
    """Per-run mutable context shared by steps."""

    def __init__(self, scenario: Scenario, state: Dict[str, Any], run_dir: Path, *, fresh_login: bool = False):
        self.scenario = scenario
        self.state = state
        self.run_dir = run_dir
        self.clients: Dict[str, HttpClient] = {}
        self.exchange_log: List[Dict[str, Any]] = []
        self.stub: Optional[StubProvider] = None
        self.fresh_login = fresh_login
        self.findings: List[Dict[str, str]] = list(state.get("findings") or [])

    # -- HTTP --------------------------------------------------------------

    def client(self, role: str) -> HttpClient:
        if role not in self.clients:
            client = HttpClient(
                self.scenario.base_url or "",
                label=role,
                timeout=float(self.scenario.timeouts.step_seconds),
                on_record=self.exchange_log.append,
            )
            cookies = self.state.get("cookies", {}).get(role) or {}
            client.set_cookies(cookies)
            self.clients[role] = client
            if self.fresh_login and role in self.state.get("accounts", {}):
                # Signing in through the IDE or fixture invalidates an earlier
                # login. Resume with fresh credentials, not a stale cookie jar.
                account = getattr(self.scenario, role)
                auth = client.post("/api/user/authenticate", {"email": account.email, "password": account.password})
                if auth.status != 200:
                    raise StepFailure(f"Resuming {role} authentication failed (HTTP {auth.status})")
        return self.clients[role]

    def snapshot_cookies(self) -> None:
        """Persist the role cookie jars into state (needed for ``--from``)."""
        self.state.setdefault("cookies", {})
        for role, client in self.clients.items():
            self.state["cookies"][role] = client.export_cookies()

    def records_since(self, index: int, limit: int = 6) -> List[Dict[str, Any]]:
        return self.exchange_log[index:][-limit:]

    # -- stub provider -----------------------------------------------------

    def ensure_stub(self) -> int:
        if self.stub is None:
            planned = int(self.state.get("stub_port") or self.scenario.stack.stub_port)
            self.stub = StubProvider(token=self.state.get("stub_token"))
            port = self.stub.start(planned)
            self.state["stub_port"] = port
            self.state["stub_token"] = self.stub.token
        return int(self.stub.port or 0)

    @property
    def stub_token(self) -> str:
        return str(self.state.get("stub_token") or "")

    # -- state helpers -----------------------------------------------------

    def require(self, *keys: str) -> None:
        missing = [key for key in keys if not self.state.get(key)]
        if missing:
            raise StepBlocked(
                "missing prerequisite state: " + ", ".join(missing),
                details={"missing_state": missing},
                fix_hint="run the earlier steps first (or `run` without --from/--only)",
            )

    def get(self, key: str) -> Any:
        return self.state.get(key)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expect(resp, statuses, step: str, message: str, *, fix_hint: str = "") -> Any:
    if resp.status not in statuses:
        raise StepFailure(
            f"{message} (HTTP {resp.status})",
            details={
                "step": step,
                "http_status": resp.status,
                "response": resp.json if resp.json is not None else redact_text(resp.text)[:2000],
            },
            fix_hint=_server_detail_hint(resp, fix_hint),
        )
    return resp.json


def _server_detail_hint(resp, fallback: str) -> str:
    """Prefer the server's own typed validation codes over a generic guess.

    A FastAPI ``{"detail": [{code, field, message}, ...]}`` body says exactly why
    the request was refused; surfacing it keeps the report actionable instead of
    blaming a prerequisite that did pass.
    """
    payload = resp.json if isinstance(resp.json, dict) else None
    detail = (payload or {}).get("detail")
    entries = detail if isinstance(detail, list) else ([detail] if isinstance(detail, dict) else [])
    codes = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code") or entry.get("type")
        field = entry.get("field")
        text = entry.get("message")
        if code:
            codes.append(f"{code}" + (f" at {field}" if field else "") + (f": {text}" if text else ""))
    if codes:
        return "server refused the request: " + "; ".join(codes[:4])
    return fallback


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. doctor
# ---------------------------------------------------------------------------


def step_doctor(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    anon = ctx.client("anon")
    details: Dict[str, Any] = {"base_url": scenario.base_url}

    # Liveness.
    try:
        ping = anon.head("/api/ping")
    except HttpError as error:
        raise StepFailure(
            f"backend is not reachable at {scenario.base_url}",
            details={"error": str(error)},
            fix_hint="start the disposable stack: `python3 -m code4me_e2e stack up`",
        )
    details["ping_status"] = ping.status
    if ping.status != 200:
        raise StepFailure(
            f"/api/ping returned HTTP {ping.status}",
            fix_hint="the backend process is up but unhealthy; check `stack up` logs",
        )

    # Readiness.
    status, payload = stack.capabilities(scenario)
    details["capabilities"] = payload if not isinstance(payload, str) else redact_text(payload)
    if status != 200:
        if status == 503 and isinstance(payload, dict):
            detail = payload.get("detail")
            raise StepFailure(
                "the backend's managed-agent schema is not ready (schema drift)",
                details={"status": status, "detail": detail, "capabilities": payload},
                fix_hint=(
                    "This backend points at a database whose alembic revision is not the "
                    f"code head ({detail!r}). The harness must provision its own clean DB: "
                    "run `python3 -m code4me_e2e stack down` then `stack up` (fresh volume), "
                    "or re-point --base-url at the harness stack. Do not reuse a drifted dev DB."
                ),
            )
        raise StepFailure(
            f"/api/acp/capabilities returned HTTP {status}",
            details={"status": status, "body": redact_text(str(payload))[:2000]},
            fix_hint="verify the backend started and migrations ran",
        )
    if not payload.get("schema_ready"):
        raise StepFailure(
            "capabilities reported schema_ready=false",
            details={"capabilities": payload},
            fix_hint="run `stack down && stack up` so the disposable DB is migrated to head",
        )

    # DB-level checks (best effort: only meaningful for the harness's own stack).
    ok, out = stack.try_psql(
        scenario,
        "SELECT to_regclass('public.provider_connection') IS NOT NULL, "
        
        "to_regclass('public.agent_profile') IS NOT NULL, "
        "(SELECT count(*) FROM config), "
        "(SELECT count(*) FROM information_schema.columns "
        " WHERE table_name='user' AND column_name='can_research'), "
        "(SELECT count(*) FROM information_schema.columns "
        " WHERE table_name='study' AND column_name='is_research');",
    )
    if not ok:
        details["db_probe"] = "skipped (no local db service for this project)"
    else:
        parts = [p.strip() for p in out.split("|")]
        details["db_probe"] = {
            "provider_connection": parts[0] == "t",
            "agent_profile": parts[1] == "t",
            "config_rows": int(parts[2]) if parts[2].isdigit() else 0,
            "user_can_research": parts[3] == "1",
            "study_is_research": parts[4] == "1",
        }
        expected = details["db_probe"]
        if not (
            expected["provider_connection"]
            and expected["agent_profile"]
            and expected["config_rows"] >= 1
            and expected["user_can_research"]
            and expected["study_is_research"]
        ):
            raise StepFailure(
                "the disposable database is missing expected tables/columns",
                details={"db_probe": expected},
                fix_hint=(
                    "the volume is half-initialised or schema-drifted: run "
                    "`python3 -m code4me_e2e stack down` then `stack up`"
                ),
            )

    # Login check per account, only when the account already exists.
    logins: Dict[str, Any] = {}
    for role in ("admin", "researcher", "participant"):
        account = getattr(scenario, role)
        if ok:
            exists = stack.psql(
                scenario,
                "SELECT count(*) FROM \"user\" WHERE email='"
                + account.email.replace("'", "")
                + "';",
            ).strip()
        else:
            exists = "unknown"
        if exists == "0":
            logins[role] = "account not created yet (run will create it)"
            continue
        client = ctx.client(role)
        resp = client.post(
            "/api/user/authenticate",
            {"email": account.email, "password": account.password},
        )
        if resp.status != 200:
            raise StepFailure(
                f"{role} login failed (HTTP {resp.status})",
                details={"role": role, "http_status": resp.status},
                fix_hint=(
                    "check the account email/password in the scenario"
                    if ok
                    else (
                        "this base-url is not the harness's own stack, so its accounts "
                        "are unknown; point --base-url at the harness stack "
                        "(http://localhost:<stack.backend_port>) or supply credentials "
                        "that exist on this backend"
                    )
                ),
            )
        logins[role] = "ok"
        ctx.state[f"{role}_user_id"] = (resp.json or {}).get("user", {}).get("user_id")
    ctx.snapshot_cookies()
    details["logins"] = logins
    return details


# ---------------------------------------------------------------------------
# 2. create_accounts
# ---------------------------------------------------------------------------


def step_create_accounts(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    config_id = ctx.get("config_id")
    if not config_id:
        try:
            config_id = int(stack.psql(scenario, "SELECT config_id FROM config ORDER BY config_id LIMIT 1;"))
        except (stack.StackError, ValueError) as error:
            raise StepFailure(
                "could not read a config_id from the database",
                details={"error": str(error)},
                fix_hint="the disposable DB has no config row; run `stack down && stack up`",
            )
        ctx.state["config_id"] = config_id

    results: Dict[str, Any] = {}
    for role in ("admin", "researcher", "participant"):
        account = getattr(scenario, role)
        client = ctx.client(role)
        created = client.post(
            "/api/user/create",
            {
                "email": account.email,
                "name": account.name,
                "password": account.password,
                "config_id": int(config_id),
            },
        )
        if created.status not in (201, 409):
            raise StepFailure(
                f"creating the {role} account failed (HTTP {created.status})",
                details={
                    "role": role,
                    "http_status": created.status,
                    "response": created.json,
                },
                fix_hint=(
                    "if this is HTTP 500 the backend could not contact the Celery broker; "
                    "the harness stack points CELERY_BROKER_* at its own redis"
                ),
            )
        auth = client.post(
            "/api/user/authenticate",
            {"email": account.email, "password": account.password},
        )
        if auth.status != 200:
            raise StepFailure(
                f"logging in as {role} failed (HTTP {auth.status})",
                details={"role": role, "http_status": auth.status, "response": auth.json},
                fix_hint="check the account was created (HTTP 409 means it already existed)",
            )
        user_id = ((auth.json or {}).get("user") or {}).get("user_id")
        if not user_id:
            raise StepFailure(
                f"{role} login returned no user_id",
                details={"response": auth.json},
                fix_hint="unexpected server response shape",
            )
        ctx.state[f"{role}_user_id"] = user_id
        results[role] = {"created": created.status == 201, "user_id": user_id}
    ctx.snapshot_cookies()
    return {"config_id": config_id, "accounts": results}


# ---------------------------------------------------------------------------
# 3. grant_roles
# ---------------------------------------------------------------------------


def step_grant_roles(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("admin_user_id")
    email = scenario.admin.email.replace("'", "")
    try:
        stack.psql(scenario, f"UPDATE \"user\" SET is_admin=true WHERE email='{email}';")
    except stack.StackError as error:
        raise StepFailure(
            "could not promote the admin account via SQL",
            details={"error": str(error)},
            fix_hint="`docker compose exec db psql` failed; verify the stack is up",
        )
    # Re-login so the session reflects the new privilege (defensive; the token
    # only caches user_id, but this mirrors the plugin's login flow).
    admin = ctx.client("admin")
    auth = admin.post(
        "/api/user/authenticate",
        {"email": scenario.admin.email, "password": scenario.admin.password},
    )
    _expect(auth, (200,), "grant_roles", "admin re-login failed")
    ctx.snapshot_cookies()
    ctx.findings.append(
        {
            "id": "NO_ADMIN_GRANT_ENDPOINT",
            "severity": "info",
            "message": (
                "There is no admin-granting API. The harness promotes the admin "
                "account with an out-of-band SQL UPDATE (the only such step)."
            ),
            "location": "code4me2-server/src/database/init.sql (is_admin default false)",
        }
    )
    ctx.state["findings"] = ctx.findings
    return {"granted_admin": scenario.admin.email, "method": "SQL UPDATE \"user\" SET is_admin=true"}


# ---------------------------------------------------------------------------
# 4. enable_researcher
# ---------------------------------------------------------------------------


def step_enable_researcher(ctx: Ctx) -> Dict[str, Any]:
    ctx.require("researcher_user_id")
    admin = ctx.client("admin")
    resp = admin.put(
        f"/api/research/researchers/{ctx.get('researcher_user_id')}",
        {"can_research": True},
    )
    payload = _expect(
        resp,
        (200,),
        "enable_researcher",
        "could not enable the researcher account",
        fix_hint="the admin account must be promoted first (run grant_roles)",
    )
    return {"researcher": payload.get("user")}


# ---------------------------------------------------------------------------
# 5. provider_connection
# ---------------------------------------------------------------------------


def step_provider_connection(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("researcher_user_id")
    port = ctx.ensure_stub()
    admin = ctx.client("admin")
    label = "e2e-stub-" + ctx.run_dir.name
    # Metered arms (Goose, built-in) fail closed without a price for the
    # model, so the stub connection prices it (USD per million tokens).
    body = {
        "label": label,
        "base_url": f"http://host.docker.internal:{port}/v1",
        "secret_ref": scenario.stack.stub_secret_env,
        "models": [scenario.agent.model],
        "is_active": True,
        "model_prices": {
            scenario.agent.model: {"input_usd_per_million": "1", "output_usd_per_million": "4"}
        },
    }
    resp = admin.post("/api/research/provider-connections", body)
    connection: Optional[dict] = None
    if resp.status == 201:
        connection = (resp.json or {}).get("connection")
    elif resp.status == 409:
        listing = admin.get("/api/research/provider-connections")
        _expect(listing, (200,), "provider_connection", "could not list provider connections")
        for item in (listing.json or {}).get("connections", []):
            if item.get("label") == label:
                connection = item
                break
        if connection is None:
            raise StepFailure("provider connection label conflict but not found in list")
        # An earlier run may have created the connection unpriced: (re)price it.
        priced = admin.put(f"/api/research/provider-connections/{connection.get('connection_id')}", body)
        if priced.status == 200:
            connection = (priced.json or {}).get("connection") or connection
    else:
        _expect(resp, (201,), "provider_connection", "creating the provider connection failed")
    connection_id = (connection or {}).get("connection_id")
    if not connection_id:
        raise StepFailure("provider connection returned no connection_id", details={"response": resp.json})
    ctx.state["connection_id"] = connection_id

    return {
        "connection_id": connection_id,
        "base_url": body["base_url"],
        "stub_port": port,
    }


# ---------------------------------------------------------------------------
# 6. register_release
# ---------------------------------------------------------------------------


def step_register_release(ctx: Ctx) -> Dict[str, Any]:
    """Import the producer manifest and its archive through the byte-verifying API.

    The producer (``participant_release native``) built the archive and ran the
    packaged executable's ``--self-check`` and ACP ``initialize``, so the manifest
    already carries the platform test results. The harness imports exactly that
    document; it never invents a digest or a test verdict.
    """
    ctx.require("admin_user_id")
    admin = ctx.client("admin")
    manifest_path, archive_path = runtime.agent_release(ctx.run_dir)
    document = manifest_path.read_text(encoding="utf-8")
    artifact = (json.loads(document).get("artifacts") or [{}])[0]
    resp = admin.post_multipart(
        "/api/research/agents/releases/import",
        fields={"manifest": document},
        files=[("archives", archive_path.name, archive_path.read_bytes())],
    )
    payload = _expect(
        resp,
        (200, 201),
        "register_release",
        "importing the tested release failed",
        fix_hint=(
            "the manifest must declare the uploaded archive by basename and its "
            "sha256/size must match the bytes; passing producer tests are required"
        ),
    )
    release = (payload or {}).get("release") or {}
    release_id = release.get("release_id")
    if not release_id:
        raise StepFailure("release import returned no release_id", details={"response": payload})
    ctx.state["release_id"] = release_id
    # Downstream steps (and the profile they create) must pin the imported
    # release, not the synthetic scenario defaults.
    ctx.scenario.agent.release_id = release_id
    if artifact.get("sha256"):
        ctx.scenario.agent.artifact_digest = artifact["sha256"]
    return {
        "release_id": release_id,
        "created": (payload or {}).get("created"),
        "artifact_digest": artifact.get("sha256"),
        "verified_artifacts": len((payload or {}).get("verified_artifacts") or []),
    }


# ---------------------------------------------------------------------------
# 7. qualify_release
# ---------------------------------------------------------------------------


def step_qualify_release(ctx: Ctx) -> Dict[str, Any]:
    """Usability is derived from the imported producer tests; only verify it."""
    ctx.require("release_id")
    admin = ctx.client("admin")
    release_id = ctx.get("release_id")
    fetched = admin.get(f"/api/research/agents/releases/{release_id}")
    payload = _expect(fetched, (200,), "qualify_release", "fetching the release failed")
    derived = ((payload or {}).get("release") or {}).get("status")
    if derived != "QUALIFIED":
        raise StepFailure(
            f"release derived status is {derived!r}, expected 'QUALIFIED'",
            details={"release": (payload or {}).get("release")},
            fix_hint=(
                "the imported manifest must carry passing self_check and "
                "acp_initialize results for the scenario platform"
            ),
        )
    return {"release_id": release_id, "derived_status": derived}


# ---------------------------------------------------------------------------
# 8. create_profile
# ---------------------------------------------------------------------------


def step_create_profile(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("researcher_user_id", "connection_id", "release_id")
    researcher = ctx.client("researcher")
    name = scenario.agent.profile_name
    body = {
        "name": name,
        "model": scenario.agent.model,
        "framework_version": scenario.agent.framework_version,
        "connection_id": ctx.get("connection_id"),
        "release_id": ctx.get("release_id"),
        "tools_json": json.dumps(scenario.agent.tools),
        "approval_policy": scenario.agent.approval_policy,
        "max_steps": scenario.agent.max_steps,
        "is_active": True,
    }
    resp = researcher.post("/api/agent/profiles", body)
    profile: Optional[dict] = None
    if resp.status == 201:
        profile = (resp.json or {}).get("profile")
    elif resp.status == 409:
        listing = researcher.get("/api/agent/profiles")
        for item in (listing.json or {}).get("profiles", []):
            if item.get("name") == name:
                profile = item
                break
    else:
        _expect(resp, (201,), "create_profile", "creating the agent profile failed")
    profile_id = (profile or {}).get("profile_id")
    if not profile_id:
        raise StepFailure("agent profile returned no profile_id", details={"response": resp.json})
    ctx.state["profile_id"] = profile_id
    return {"profile_id": profile_id, "created": resp.status == 201, "owner_user_id": (profile or {}).get("owner_user_id")}


# ---------------------------------------------------------------------------
# 9. create_study
# ---------------------------------------------------------------------------


def step_create_study(ctx: Ctx) -> Dict[str, Any]:
    """Create the current study configuration (draft/revision API was removed)."""
    scenario = ctx.scenario
    ctx.require("profile_id")
    now = datetime.now(timezone.utc)
    response = ctx.client("researcher").post("/api/research/studies", {
        "name": scenario.study.name,
        "description": scenario.study.description,
        "profile_ids": [ctx.get("profile_id")],
        # Required for metered arms (the stub model is priced by the
        # provider_connection step); ignored for a Codex-only selection.
        "default_budget_usd": "25",
        "starts_at": scenario.study.start_at or (now - timedelta(hours=1)).isoformat(),
        "ends_at": scenario.study.end_at or (now + timedelta(days=365)).isoformat(),
        "telemetry_policy": {"allowed_field_classes": ["STRUCTURAL", "METRICS"]},
        "session_policy": {"idle_timeout_seconds": 900, "resume_grace_seconds": 300, "heartbeat_seconds": 5},
    })
    payload = _expect(response, (201,), "create_study", "creating the study failed")
    study = payload.get("study") or {}
    if not study.get("study_id") or not study.get("join_code"):
        raise StepFailure("study response must contain study_id and join_code")
    ctx.state["study_id"] = study["study_id"]
    ctx.state["join_code"] = study["join_code"]
    return {"study_id": study["study_id"], "status": study.get("research_status")}


def step_join_code(ctx: Ctx) -> Dict[str, Any]:
    ctx.require("study_id")
    response = ctx.client("researcher").get(f"/api/research/studies/{ctx.get('study_id')}")
    payload = _expect(response, (200,), "join_code", "reading the study join code failed")
    code = (payload.get("study") or {}).get("join_code")
    if not code:
        raise StepFailure("study has no join code")
    ctx.state["join_code"] = code
    return {"study_id": ctx.get("study_id"), "join_code_present": True}


def step_plugin_join(ctx: Ctx) -> Dict[str, Any]:
    ctx.require("join_code", "participant_user_id")
    participant = ctx.client("participant")
    code = ctx.get("join_code")

    resolved = participant.get(f"/api/research/join/{code}")
    resolved_payload = _expect(
        resolved, (200,), "plugin_join", "resolving the join code failed (mirrors ResearchJoinCodeResolver)"
    )
    me = participant.get("/api/research/participants/me")
    _expect(me, (200,), "plugin_join", "fetching /api/research/participants/me failed")

    redeem = participant.post("/api/research/join", {"join_code": code, "accept_consent": True})
    redeem_payload = _expect(
        redeem,
        (200, 201),
        "plugin_join",
        "redeeming the join code failed (mirrors ResearchJoinCodeResolver.redeem)",
        fix_hint="the participant must not already be enrolled in a different live study",
    )
    enrollment_id = (redeem_payload or {}).get("enrollment_id")
    if not enrollment_id:
        raise StepFailure("join response had no enrollment_id", details={"response": redeem_payload})
    ctx.state["enrollment_id"] = enrollment_id

    status_resp = participant.get("/api/research/participants/me")
    enrollments = (status_resp.json or {}).get("enrollments", [])
    match = next((e for e in enrollments if e.get("enrollment_id") == enrollment_id), None)
    if not match or match.get("status") != "ACTIVE":
        raise StepFailure(
            "enrollment is not ACTIVE after redeeming the join code",
            details={"enrollment": match, "enrollments": enrollments},
            fix_hint="consent acceptance must activate the enrollment",
        )
    ctx.state["revocation_epoch_before"] = match.get("revocation_epoch")
    return {
        "study_id": (resolved_payload or {}).get("study", {}).get("study_id"),
        "enrollment_id": enrollment_id,
        "status": match.get("status"),
    }


# ---------------------------------------------------------------------------
# 12. bootstrap
# ---------------------------------------------------------------------------


def _verify_manifest_signature(manifest: Dict[str, Any], secret: str) -> Dict[str, Any]:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_digest", "signature")
    }
    digest = _canonical_hash(payload)
    signature = hmac.new(
        secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "recomputed_digest": digest,
        "digest_matches": digest == manifest.get("manifest_digest"),
        "recomputed_signature": signature,
        "signature_matches": hmac.compare_digest(signature, str(manifest.get("signature") or "")),
    }


def step_bootstrap(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("enrollment_id", "participant_user_id")
    participant = ctx.client("participant")
    environment = {
        "os": scenario.platform.os,
        "arch": scenario.platform.arch,
        "ide_build": scenario.platform.ide_build,
        "plugin_version": scenario.platform.plugin_version,
        "host_kind": scenario.platform.host_kind,
    }
    resp = participant.post(
        "/api/research/bootstrap/research-sessions",
        {
            "enrollment_id": ctx.get("enrollment_id"),
            # Opaque execution context (phase 03): required for per-context
            # session idempotency; mirrors the IntelliJ plugin's project context.
            "context_id": "e2e-bootstrap-context",
            "environment": environment,
        },
    )
    payload = _expect(
        resp,
        (201,),
        "bootstrap",
        "fetching the signed bootstrap manifest failed (mirrors HttpBootstrapTransport)",
        fix_hint=(
            "bootstrap requires an ACTIVE enrollment, a live ACTIVE study, a "
            "QUALIFIED release and a platform artifact for the configured platform"
        ),
    )
    manifest = (payload or {}).get("manifest")
    if not isinstance(manifest, dict):
        raise StepFailure("bootstrap response had no manifest object", details={"response": payload})

    manifest_digest = manifest.get("manifest_digest") or (payload or {}).get("manifest_digest")
    signature = manifest.get("signature") or (payload or {}).get("signature")
    capability = manifest.get("session_capability") or {}
    checks: Dict[str, Any] = {
        "manifest_digest_is_sha256": bool(
            isinstance(manifest_digest, str)
            and len(manifest_digest) == 64
            and all(ch in "0123456789abcdef" for ch in manifest_digest)
        ),
        "signature_present": bool(signature),
        "capability_scope": capability.get("scope"),
        "capability_has_telemetry_write": "telemetry:write" in (capability.get("scope") or []),
    }
    if not checks["manifest_digest_is_sha256"]:
        raise StepFailure(
            "manifest_digest is not a 64-char sha256 hex string",
            details={"manifest_digest": manifest_digest},
        )
    if not checks["signature_present"]:
        raise StepFailure("manifest signature is missing")
    if not checks["capability_has_telemetry_write"]:
        raise StepFailure(
            "session capability does not include the telemetry:write scope",
            details={"scope": capability.get("scope")},
        )

    verification = _verify_manifest_signature(manifest, scenario.stack.bootstrap_signing_secret)
    checks.update(verification)
    if not verification["digest_matches"]:
        raise StepFailure(
            "recomputed manifest digest does not match the server's",
            details={"verification": verification},
            fix_hint="canonical JSON rules differ from research/canonical.py",
        )
    if not verification["signature_matches"]:
        raise StepFailure(
            "manifest HMAC signature does not verify with the configured signing secret",
            details={"verification": verification},
            fix_hint="check stack.bootstrap_signing_secret matches the backend env",
        )

    research_session = manifest.get("research_session") or {}
    ctx.state["research_session_id"] = research_session.get("research_session_id")
    ctx.state["session_capability"] = capability
    ctx.state["manifest_digest"] = manifest_digest
    return {
        "research_session_id": ctx.get("research_session_id"),
        "assignment": manifest.get("assignment"),
        "agent_release": manifest.get("agent_release"),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 13. acp_prepare
# ---------------------------------------------------------------------------


def step_acp_prepare(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("participant_user_id")
    participant = ctx.client("participant")

    acquire = participant.get("/api/session/acquire")
    _expect(acquire, (200,), "acp_prepare", "session acquire failed")
    ctx.snapshot_cookies()

    project = participant.post("/api/project/create", {"project_name": "code4me-e2e"})
    project_payload = _expect(project, (201,), "acp_prepare", "creating the project failed")
    project_id = (project_payload or {}).get("project_token")
    if not project_id:
        raise StepFailure("project creation returned no project_token", details={"response": project_payload})
    ctx.state["project_id"] = project_id
    ctx.snapshot_cookies()

    activate = participant.put("/api/project/activate", {"project_id": project_id})
    _expect(activate, (200,), "acp_prepare", "activating the project failed")

    workspace = "/tmp/code4me-e2e-workspace"
    grant_resp = participant.post(
        "/api/acp/grant",
        {
            "project_id": project_id,
            "workspace": workspace,
            "launch_id": "e2e-launch-1",
            "path_format": "posix",
            "managed_protocol_version": "1",
        },
    )
    grant_payload = _expect(
        grant_resp,
        (201,),
        "acp_prepare",
        "minting an ACP launch grant failed",
        fix_hint="the plugin must hold an active session and project association",
    )
    grant = (grant_payload or {}).get("grant")
    if not grant:
        raise StepFailure("grant response had no grant", details={"response": grant_payload})

    exchange = participant.post("/api/acp/session/exchange", {"grant": grant})
    exchange_payload = _expect(
        exchange, (200,), "acp_prepare", "exchanging the ACP grant failed"
    )
    acp_token = (exchange_payload or {}).get("acp_token")
    if not acp_token:
        raise StepFailure("exchange response had no acp_token", details={"response": exchange_payload})
    ctx.state["acp_token"] = acp_token

    readiness = participant.get("/api/acp/readiness")
    ready_payload = _expect(
        readiness,
        (200,),
        "acp_prepare",
        "ACP readiness probe failed",
        fix_hint=(
            "readiness resolves the participant's sticky assignment: needs an ACTIVE "
            "enrollment, a live study and an active profile"
        ),
    )
    return {
        "project_id": project_id,
        "workspace": (grant_payload or {}).get("workspace"),
        "readiness": ready_payload,
    }


# ---------------------------------------------------------------------------
# 14. send_message
# ---------------------------------------------------------------------------


def step_send_message(ctx: Ctx) -> Dict[str, Any]:
    scenario = ctx.scenario
    ctx.require("acp_token", "research_session_id")
    participant = ctx.client("participant")
    bearer = ctx.get("acp_token")
    run_id = str(uuid.uuid4())
    session_id = ctx.get("research_session_id")
    ctx.state["run_id"] = run_id

    # Name the research session explicitly: another client of the same account
    # (the plugin layer's Kotlin fixture) can hold an active session too, and the
    # server never guesses between them (RESEARCH_CONTEXT_AMBIGUOUS).
    created = participant.post(
        "/api/acp/runs",
        {"run_id": run_id, "session_id": session_id, "research_session_id": session_id},
        bearer=bearer,
    )
    created_payload = _expect(
        created,
        (200, 201),
        "send_message",
        "creating the managed run failed",
        fix_hint=(
            "the ACP scope user must resolve an active study agent profile "
            "(check /api/acp/readiness)"
        ),
    )

    # A resumed run (`--from send_message`) is a fresh process: the in-process
    # stub server from `provider_connection` is gone, so start it again on the
    # persisted port before the relay tries to reach it.
    ctx.ensure_stub()
    expected = scenario.message.expected_substring or ctx.stub_token
    if not expected:
        raise StepFailure(
            "no expected answer token is configured",
            fix_hint="start the stub provider (provider_connection step) or set message.expected_substring",
        )
    inference = participant.post(
        "/api/acp/inference",
        {
            "run_id": run_id,
            "session_id": session_id,
            "request": {"messages": [{"role": "user", "content": scenario.message.prompt}]},
        },
        bearer=bearer,
    )
    _expect(
        inference,
        (200,),
        "send_message",
        "the managed inference relay failed",
        fix_hint=(
            "the provider connection must be active and its secret_ref env var present; "
            "the stub must be reachable at host.docker.internal:<stub_port>"
        ),
    )
    if expected not in (inference.text or ""):
        raise StepFailure(
            f"assistant answer did not contain the expected token {expected!r}",
            details={
                "expected_substring": expected,
                "response": inference.json,
                "stub_requests": (ctx.stub.requests if ctx.stub else []),
            },
            fix_hint="the inference relay did not reach the stub provider, or the response shape changed",
        )
    # Exact request construction at the relay boundary: the provider must receive
    # a non-streaming Chat Completions request carrying the participant messages
    # and the policy model, with no client-supplied override. The stub records
    # only shape metadata, never prompt content.
    forwarded = list(ctx.stub.requests) if ctx.stub else []
    last = forwarded[-1] if forwarded else {}
    if not forwarded or last.get("message_count", 0) < 1 or last.get("stream") or not last.get("model"):
        raise StepFailure(
            "the relay forwarded an unexpected completion request",
            details={"forwarded": forwarded},
            fix_hint=(
                "the managed relay must forward the participant messages non-streaming "
                "with the policy model and no client-side temperature/model override"
            ),
        )
    return {
        "run_id": run_id,
        "session_id": session_id,
        "policy": (created_payload or {}).get("policy"),
        "answer_contains_expected": True,
        "forwarded_request": {
            "message_count": last.get("message_count"),
            "model": last.get("model"),
            "stream": last.get("stream"),
        },
    }


# ---------------------------------------------------------------------------
# 15. telemetry
# ---------------------------------------------------------------------------


def _canonical_event(ctx: Ctx) -> Dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "event_type": "tool.started",
        "source": "acp",
        "study_id": ctx.get("study_id"),
        "enrollment_id": ctx.get("enrollment_id"),
        "research_session_id": ctx.get("research_session_id"),
        "agent_run_id": ctx.get("run_id"),
        "occurred_at": _now_iso(),
        "monotonic_ns": None,
        "emitter_id": "code4me-e2e-harness",
        "emitter_sequence": 1,
        "correlations": {},
        "lifecycle_state": "started",
        "payload": {"status": "started"},
        "metrics": {},
        "privacy": {},
        "provenance": {
            "source": "acp",
            "normalizer_version": "e2e-harness-v1",
            "fidelity": "normalized",
        },
        "coverage": {"state": "AVAILABLE"},
        "unknown_event_type": None,
        "unknown_source": None,
        "unknown_lifecycle_state": None,
    }


def step_telemetry(ctx: Ctx) -> Dict[str, Any]:
    ctx.require(
        "study_id",
        "enrollment_id",
        "research_session_id",
        "session_capability",
    )
    participant = ctx.client("participant")
    event = _canonical_event(ctx)
    batch_id = str(uuid.uuid4())
    body = {
        "batch_id": batch_id,
        "protocol_version": "1",
        "telemetry_schema_version": "1",
        "session_capability": ctx.get("session_capability"),
        "events": [event],
        "client_instance_id": "code4me-e2e-harness",
    }
    resp = participant.post("/api/research/telemetry/batches", body)
    payload = _expect(
        resp,
        (200,),
        "telemetry",
        "telemetry batch submission failed",
        fix_hint="the session capability must be bound to this enrollment/session",
    )
    accepted_ids = [item.get("event_id") for item in (payload or {}).get("accepted", [])]
    rejected = (payload or {}).get("rejected", [])
    retryable = (payload or {}).get("retryable", [])
    if event["event_id"] not in accepted_ids or rejected or retryable:
        raise StepFailure(
            "telemetry event was not accepted",
            details={"response": payload},
            fix_hint="check the event context ids and capability scope (telemetry:write)",
        )
    ctx.state["batch_id"] = batch_id
    ctx.state["telemetry_receipt_id"] = (payload or {}).get("receipt_id")
    ctx.state["telemetry_event_id"] = event["event_id"]
    return {
        "batch_id": batch_id,
        "receipt_id": ctx.get("telemetry_receipt_id"),
        "event_id": event["event_id"],
        "accepted": accepted_ids,
    }


# ---------------------------------------------------------------------------
# 16. verify
# ---------------------------------------------------------------------------


def step_verify(ctx: Ctx) -> Dict[str, Any]:
    ctx.require("telemetry_receipt_id", "telemetry_event_id")
    admin = ctx.client("admin")
    resp = admin.get(
        f"/api/research/telemetry/receipts/{ctx.get('telemetry_receipt_id')}"
    )
    payload = _expect(
        resp,
        (200,),
        "verify",
        "reading the telemetry batch receipt failed",
    )
    stored = None
    try:
        stored = stack.psql(
            ctx.scenario,
            "SELECT count(*) FROM research_event WHERE event_id='"
            + str(ctx.get("telemetry_event_id"))
            + "';",
        )
    except stack.StackError as error:
        stored = f"probe failed: {error}"
    if stored != "1":
        raise StepFailure(
            "the telemetry event was not persisted in research_event",
            details={"research_event_count": stored, "receipt": payload},
            fix_hint="ingestion committed the receipt but not the event; inspect backend logs",
        )
    return {
        "receipt_id": ctx.get("telemetry_receipt_id"),
        "research_event_rows": stored,
        "accepted": ((payload or {}).get("receipt") or {}).get("accepted"),
    }


# ---------------------------------------------------------------------------
# 17. revoke
# ---------------------------------------------------------------------------


def step_revoke(ctx: Ctx) -> Dict[str, Any]:
    ctx.require("enrollment_id", "study_id")
    response = ctx.client("researcher").post(
        f"/api/research/studies/{ctx.get('study_id')}/enrollments/{ctx.get('enrollment_id')}/revoke", {})
    payload = _expect(response, (200,), "revoke", "revoking the enrollment failed")
    status = ctx.client("participant").get("/api/research/participants/me")
    own = _expect(status, (200,), "revoke", "reading revoked enrollment failed")
    match = next((e for e in own.get("enrollments", []) if e.get("enrollment_id") == ctx.get("enrollment_id")), {})
    if not payload.get("revoked") or match.get("status") != "REVOKED":
        raise StepFailure("enrollment must become REVOKED", details={"enrollment": match})
    # A previously issued capability must no longer bootstrap this enrollment.
    refused = ctx.client("participant").post("/api/research/bootstrap/research-sessions", {
        "enrollment_id": ctx.get("enrollment_id"), "context_id": "e2e-after-revoke",
        "environment": {"os": ctx.scenario.platform.os, "arch": ctx.scenario.platform.arch},
    })
    _expect(refused, (403, 409, 410), "revoke", "revoked enrollment was still usable")
    return {"status": match["status"], "bootstrap_status": refused.status}


#: Registry the workflow runner uses.
STEPS = {
    "doctor": step_doctor,
    "create_accounts": step_create_accounts,
    "grant_roles": step_grant_roles,
    "enable_researcher": step_enable_researcher,
    "provider_connection": step_provider_connection,
    "register_release": step_register_release,
    "qualify_release": step_qualify_release,
    "create_profile": step_create_profile,
    "create_study": step_create_study,
    "join_code": step_join_code,
    "plugin_join": step_plugin_join,
    "bootstrap": step_bootstrap,
    "acp_prepare": step_acp_prepare,
    "send_message": step_send_message,
    "telemetry": step_telemetry,
    "verify": step_verify,
    "revoke": step_revoke,
}
