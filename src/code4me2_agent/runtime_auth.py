from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, request

logger = logging.getLogger(__name__)


def _log_secrets_enabled() -> bool:
    return os.environ.get("CODE4ME_ACP_LOG_SECRETS", "").lower() in {"1", "true", "yes"}


def _secret_for_log(value: str | None) -> str:
    if not value:
        return "missing"
    if _log_secrets_enabled():
        return value
    return "present"


class AcpAuthorizationFailure(Exception):
    pass


@dataclass(frozen=True)
class AcpRuntimeScope:
    project_id: str
    workspace: str


class AcpBackendAuthorization:
    def __init__(
        self,
        *,
        backend_url: str | None,
        grant: str | None,
        acp_token: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._backend_url = backend_url.rstrip("/") if backend_url else None
        self._grant = grant
        self._acp_token: str | None = acp_token
        self._scope: AcpRuntimeScope | None = None
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )

    @classmethod
    def from_environment(
        cls,
        *,
        workspace_root: str | Path | None = None,
    ) -> "AcpBackendAuthorization":
        backend_url = os.environ.get("CODE4ME_ACP_BACKEND_URL")
        grant = os.environ.get("CODE4ME_ACP_GRANT")
        acp_token = os.environ.get("CODE4ME_ACP_TOKEN")
        if (
            not backend_url or not grant or not acp_token
        ) and workspace_root is not None:
            logger.info(
                "Loading prepared ACP runtime credentials from workspace handoff file."
            )
            fallback_values = _read_runtime_auth_values(
                Path(workspace_root), include_home=True
            )
            backend_url = backend_url or fallback_values.get("CODE4ME_ACP_BACKEND_URL")
            grant = grant or fallback_values.get("CODE4ME_ACP_GRANT")
            acp_token = acp_token or fallback_values.get("CODE4ME_ACP_TOKEN")
        logger.info(
            "Prepared ACP runtime credentials loaded: backend_url=%s grant=%s acp_token=%s workspace_root=%s.",
            backend_url or "missing",
            _secret_for_log(grant),
            _secret_for_log(acp_token),
            Path(workspace_root).resolve() if workspace_root is not None else "missing",
        )
        return cls(
            backend_url=backend_url,
            grant=grant,
            acp_token=acp_token,
            workspace_root=workspace_root,
        )

    @property
    def is_authenticated(self) -> bool:
        return self._acp_token is not None and self._scope is not None

    @property
    def backend_url(self) -> str | None:
        return self._backend_url

    def authenticate(self) -> AcpRuntimeScope:
        self._refresh_prepared_credentials()
        if self._backend_url and self._acp_token:
            try:
                return self.validate()
            except AcpAuthorizationFailure:
                logger.info(
                    "Cached ACP runtime token was not valid; falling back to prepared grant exchange."
                )
        if not self._backend_url or not self._grant:
            logger.warning(
                "ACP authentication failed because prepared credentials are missing: backend_url=%s grant=%s workspace_root=%s.",
                self._backend_url or "missing",
                _secret_for_log(self._grant),
                self._workspace_root or "missing",
            )
            raise AcpAuthorizationFailure(
                "Prepared Code4Me runtime credentials are required."
            )
        logger.info(
            "Exchanging prepared ACP runtime grant with backend_url=%s grant=%s.",
            self._backend_url,
            _secret_for_log(self._grant),
        )
        payload = self._post("/api/acp/session/exchange", {"grant": self._grant})
        self._grant = None
        acp_token = payload.get("acp_token")
        project_id = payload.get("project_id")
        workspace = payload.get("workspace")
        if not acp_token or not project_id or not workspace:
            logger.warning(
                "ACP grant exchange returned an invalid authorization scope."
            )
            raise AcpAuthorizationFailure(
                "ACP grant exchange returned an invalid authorization scope."
            )
        self._acp_token = str(acp_token)
        self._scope = AcpRuntimeScope(
            project_id=str(project_id), workspace=str(workspace)
        )
        self._write_session_cache()
        self.fetch_agent_config()
        logger.info(
            "ACP authentication succeeded for project_id=%s workspace=%s acp_token=%s.",
            project_id,
            workspace,
            _secret_for_log(self._acp_token),
        )
        return self._scope

    def validate(self) -> AcpRuntimeScope:
        if not self._backend_url or not self._acp_token:
            logger.warning(
                "ACP validation failed because authentication state is missing."
            )
            raise AcpAuthorizationFailure("ACP authentication is required.")
        try:
            logger.info(
                "Validating ACP runtime authentication with backend_url=%s acp_token=%s.",
                self._backend_url,
                _secret_for_log(self._acp_token),
            )
            payload = self._post(
                "/api/acp/session/validate-or-refresh",
                {},
                headers={"Authorization": f"Bearer {self._acp_token}"},
            )
        except AcpAuthorizationFailure:
            self._acp_token = None
            self._scope = None
            logger.warning(
                "ACP validation failed; cleared runtime authentication state."
            )
            raise
        project_id = payload.get("project_id")
        workspace = payload.get("workspace")
        if not project_id or not workspace:
            self._acp_token = None
            self._scope = None
            logger.warning("ACP validation returned an invalid authorization scope.")
            raise AcpAuthorizationFailure(
                "ACP validation returned an invalid authorization scope."
            )
        self._scope = AcpRuntimeScope(
            project_id=str(project_id), workspace=str(workspace)
        )
        logger.info(
            "ACP validation succeeded for project_id=%s workspace=%s.",
            project_id,
            workspace,
        )
        return self._scope

    # The assigned agent profile, as handed down by the backend after auth.
    # Held as one object rather than a field per setting so adding a profile
    # setting doesn't require touching this class at all.
    _server_agent_config: "ServerAgentConfig | None" = None

    @property
    def server_agent_config(self) -> "ServerAgentConfig | None":
        """The backend's assigned configuration, or None if it couldn't be fetched."""
        return self._server_agent_config

    # Backwards-compatible accessors, kept so existing callers and tests that
    # read individual settings keep working.
    @property
    def server_commands_allowlist(self) -> list[str] | None:
        return (
            self._server_agent_config.commands_allowlist
            if self._server_agent_config
            else None
        )

    @property
    def server_model(self) -> str | None:
        return self._server_agent_config.model if self._server_agent_config else None

    @property
    def server_max_iterations(self) -> int | None:
        return (
            self._server_agent_config.max_iterations
            if self._server_agent_config
            else None
        )

    @property
    def server_base_url(self) -> str | None:
        return (
            self._server_agent_config.base_url if self._server_agent_config else None
        )

    @property
    def server_api_key_ref(self) -> str | None:
        return (
            self._server_agent_config.api_key_ref
            if self._server_agent_config
            else None
        )

    @property
    def server_store_agent_content(self) -> bool:
        """Whether the server will persist content for this user.

        Advisory only. The server enforces its own decision regardless of what
        this agent sends, so this is used purely to avoid transmitting content
        that would be discarded — never to enable capture.
        """
        return (
            self._server_agent_config.store_agent_content
            if self._server_agent_config
            else True
        )

    def fetch_agent_config(self) -> None:
        """Fetch the assigned agent profile's runtime configuration.

        Called right after a successful grant exchange. A failure here is
        logged and tolerated: the agent then runs on whatever the local config
        file provides, which is a degraded but working state rather than a dead
        one. Note that with the provider defaults removed, a local config that
        specifies no model will fail loudly at first use instead — which is the
        intended outcome, since silently substituting a different model would
        corrupt the experiment.
        """
        if not self._backend_url or not self._acp_token:
            logger.info("Cannot fetch agent config: not authenticated.")
            return
        try:
            payload = self._get(
                "/api/acp/agent-config",
                headers={"Authorization": f"Bearer {self._acp_token}"},
            )
        except AcpAuthorizationFailure:
            logger.info("Failed to fetch server agent config; using local config.")
            return

        from code4me2_agent.config import ServerAgentConfig

        self._server_agent_config = ServerAgentConfig.from_payload(payload)
        config = self._server_agent_config
        logger.info(
            "Server agent config: profile=%s runtime=%s model=%s base_url=%s "
            "api_key_ref=%s tools=%s max_iterations=%s store_content=%s.",
            config.agent_profile,
            config.framework_version,
            config.model,
            config.base_url or "(via backend relay)",
            config.api_key_ref,
            config.tools,
            config.max_iterations,
            config.store_agent_content,
        )

    def telemetry_headers(self) -> dict[str, str]:
        if not self._acp_token:
            logger.warning(
                "Telemetry headers requested before ACP authentication completed."
            )
            raise AcpAuthorizationFailure("ACP authentication is required.")
        return {"Authorization": f"Bearer {self._acp_token}"}

    def authorized_json_request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict:
        if not self._backend_url or not self._acp_token:
            logger.warning(
                "ACP authorized request failed because authentication state is missing."
            )
            raise AcpAuthorizationFailure("ACP authentication is required.")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            f"{self._backend_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self._acp_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        logger.info(
            "Sending ACP authorized request method=%s url=%s payload_keys=%s authorization_header=%s.",
            method,
            http_request.full_url,
            sorted(payload.keys()) if payload else [],
            _secret_for_log(f"Bearer {self._acp_token}"),
        )
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            if exc.code == 404:
                return {}
            logger.warning(
                "ACP authorized request failed with HTTP status %s url=%s.",
                exc.code,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorized request was rejected."
            ) from None
        except error.URLError as exc:
            logger.warning(
                "ACP authorized request failed with URL error: %s url=%s.",
                exc.reason,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorized request was rejected."
            ) from None
        except TimeoutError:
            logger.warning("ACP authorized request timed out.")
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorized request was rejected."
            ) from None
        except ValueError:
            logger.warning("ACP authorized request returned invalid JSON.")
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorized request was rejected."
            ) from None

    def _refresh_prepared_credentials(self) -> None:
        if self._workspace_root is None:
            return
        fallback_values = _read_runtime_auth_values(
            self._workspace_root, include_home=True
        )
        fallback_backend_url = fallback_values.get("CODE4ME_ACP_BACKEND_URL")
        fallback_grant = fallback_values.get("CODE4ME_ACP_GRANT")
        fallback_acp_token = fallback_values.get("CODE4ME_ACP_TOKEN")
        if fallback_backend_url:
            self._backend_url = fallback_backend_url.rstrip("/")
            logger.info("Refreshed ACP backend URL from handoff file.")
        if fallback_grant:
            self._grant = fallback_grant
            logger.info("Refreshed ACP runtime grant from handoff file.")
        if fallback_acp_token:
            self._acp_token = fallback_acp_token
            logger.info("Refreshed ACP runtime token from handoff file.")

    def _write_session_cache(self) -> None:
        if not self._backend_url or not self._acp_token:
            return
        cache_path = Path("~/.code4me/acp-session.env").expanduser()
        workspace_cache_path = (
            self._workspace_root / ".code4me" / "acp-runtime.env"
            if self._workspace_root is not None
            else None
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                "CODE4ME_ACP_BACKEND_URL="
                f"{self._backend_url}\n"
                "CODE4ME_ACP_TOKEN="
                f"{self._acp_token}\n",
                encoding="utf-8",
            )
            os.chmod(cache_path, 0o600)
            logger.info("Cached ACP runtime session token at %s.", cache_path)
        except OSError as exc:
            logger.info(
                "Could not cache ACP runtime session token at %s: %s.", cache_path, exc
            )
        if workspace_cache_path is not None:
            try:
                _write_runtime_auth_values(
                    workspace_cache_path,
                    {
                        "CODE4ME_ACP_BACKEND_URL": self._backend_url,
                        "CODE4ME_ACP_TOKEN": self._acp_token,
                    },
                )
                os.chmod(workspace_cache_path, 0o600)
                logger.info(
                    "Cached ACP runtime session token in workspace handoff at %s.",
                    workspace_cache_path,
                )
            except OSError as exc:
                logger.info(
                    "Could not cache ACP runtime session token in workspace handoff at %s: %s.",
                    workspace_cache_path,
                    exc,
                )

    def _post(
        self,
        path: str,
        payload: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        http_request = request.Request(
            f"{self._backend_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        logger.info(
            "Sending ACP authorization request method=POST url=%s payload_keys=%s authorization_header=%s.",
            http_request.full_url,
            sorted(payload.keys()),
            _secret_for_log(headers.get("Authorization") if headers else None),
        )
        try:
            with request.urlopen(http_request, timeout=10.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            logger.warning(
                "ACP authorization request failed with HTTP status %s url=%s.",
                exc.code,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorization request was rejected."
            ) from None
        except error.URLError as exc:
            logger.warning(
                "ACP authorization request failed with URL error: %s url=%s.",
                exc.reason,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorization request was rejected."
            ) from None
        except TimeoutError:
            logger.warning("ACP authorization request timed out.")
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorization request was rejected."
            ) from None
        except ValueError:
            logger.warning("ACP authorization request returned invalid JSON.")
            raise AcpAuthorizationFailure(
                "Code4Me ACP authorization request was rejected."
            ) from None

    def _get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        http_request = request.Request(
            f"{self._backend_url}{path}",
            headers={"Content-Type": "application/json", **(headers or {})},
            method="GET",
        )
        logger.info(
            "Sending ACP agent-config request method=GET url=%s authorization_header=%s.",
            http_request.full_url,
            _secret_for_log(headers.get("Authorization") if headers else None),
        )
        try:
            with request.urlopen(http_request, timeout=10.0) as response:
                parsed = json.loads(response.read().decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
        except error.HTTPError as exc:
            logger.warning(
                "ACP agent-config request failed with HTTP status %s url=%s.",
                exc.code,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure("Code4Me ACP agent-config request was rejected.") from None
        except error.URLError as exc:
            logger.warning(
                "ACP agent-config request failed with URL error: %s url=%s.",
                exc.reason,
                http_request.full_url,
            )
            raise AcpAuthorizationFailure("Code4Me ACP agent-config request was rejected.") from None
        except TimeoutError:
            logger.warning("ACP agent-config request timed out.")
            raise AcpAuthorizationFailure("Code4Me ACP agent-config request was rejected.") from None
        except ValueError:
            logger.warning("ACP agent-config request returned invalid JSON.")
            raise AcpAuthorizationFailure("Code4Me ACP agent-config request was rejected.") from None


def _read_env_file(file_path: Path) -> dict[str, str]:
    try:
        raw_lines = file_path.read_text().splitlines()
    except OSError:
        logger.info("ACP runtime env file was not available at %s.", file_path)
        return {}
    values: dict[str, str] = {}
    for line in raw_lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"CODE4ME_ACP_BACKEND_URL", "CODE4ME_ACP_GRANT", "CODE4ME_ACP_TOKEN"}:
            values[key] = value.strip()
    return values


def _write_runtime_auth_values(file_path: Path, values: dict[str, str]) -> None:
    existing_values = _read_env_file(file_path)
    merged_values = {**existing_values, **values}
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        f"{key}={value}\n"
        for key, value in merged_values.items()
        if key in {"CODE4ME_ACP_BACKEND_URL", "CODE4ME_ACP_GRANT", "CODE4ME_ACP_TOKEN"}
    )
    file_path.write_text(payload, encoding="utf-8")


def _read_runtime_auth_values(
    workspace_root: Path, *, include_home: bool = False
) -> dict[str, str]:
    candidates = [
        workspace_root.resolve() / ".idea" / "code4me" / "acp-runtime.env",
        workspace_root.resolve() / ".code4me" / "acp-runtime.env",
    ]
    if include_home:
        candidates.extend(
            [
                Path("~/.code4me/acp-runtime.env").expanduser(),
                Path("~/.code4me/acp-session.env").expanduser(),
            ]
        )
    available: list[tuple[float, Path, dict[str, str]]] = []
    for handoff_path in candidates:
        values = _read_env_file(handoff_path)
        if values:
            try:
                modified_at = handoff_path.stat().st_mtime
            except OSError:
                modified_at = 0.0
            available.append((modified_at, handoff_path, values))
            continue
        logger.info("ACP runtime handoff file was not available at %s.", handoff_path)
    if available:
        merged: dict[str, str] = {}
        for _, handoff_path, values in sorted(
            available, key=lambda item: item[0], reverse=True
        ):
            logger.info("Considering ACP runtime handoff file at %s.", handoff_path)
            for key, value in values.items():
                merged.setdefault(key, value)
        return merged
    return {}


def _read_runtime_handoff_file(
    workspace_root: Path, *, include_home: bool = False
) -> tuple[str | None, str | None]:
    values = _read_runtime_auth_values(workspace_root, include_home=include_home)
    return values.get("CODE4ME_ACP_BACKEND_URL"), values.get("CODE4ME_ACP_GRANT")
