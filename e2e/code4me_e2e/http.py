"""Minimal stdlib HTTP client with a cookie jar and secret redaction.

No third-party dependency: ``urllib.request`` plus a hand-rolled cookie jar for
the three cookies the Code4Me backend uses (``auth_token``, ``session_token``,
``project_token``). HTTP errors never raise: every call returns
``(status, parsed_json, raw_text)`` so a step can assert on a status code.

Every captured exchange is sanitized before it is stored, so the run report and
logs never contain a password, session cookie, launch grant or capability.
"""

from __future__ import annotations

import http.cookies
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import is_secret_key, redact

#: Best-effort redaction of raw (non-JSON) bodies.
_REDACT_PATTERNS = (
    (r"(?i)(Bearer)\s+[A-Za-z0-9._\-]+", r"\1 <redacted>"),
    (r"(?i)(auth_token|session_token|project_token|acp_token|grant)=([^;\s]+)", r"\1=<redacted>"),
)


class HttpError(RuntimeError):
    """The HTTP call could not be completed (DNS, refused, timeout)."""


@dataclass
class HttpResponse:
    status: int
    json: Optional[Any]
    text: str
    headers: Dict[str, str] = field(default_factory=dict)

    def body_contains(self, needle: str) -> bool:
        return needle in self.text


def redact_text(text: str) -> str:
    """Best-effort redaction for a raw body that is not a JSON object."""
    import re

    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


def encode_multipart(
    fields: Dict[str, str], files: List[Tuple[str, str, bytes]]
) -> Tuple[bytes, str]:
    """Encode one multipart/form-data body using only the standard library.

    ``files`` entries are ``(field_name, filename, payload)``. The release import
    endpoint takes the manifest as a text field and each archive as a file field,
    so the harness must speak multipart without adding a dependency.
    """
    boundary = "----code4me-e2e-" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, filename, payload in files:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class HttpClient:
    """A cookie-aware HTTP client for one authenticated role."""

    def __init__(
        self,
        base_url: str,
        *,
        label: str = "client",
        timeout: float = 30.0,
        on_record: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.timeout = timeout
        self.cookies: Dict[str, str] = {}
        self.records: list[Dict[str, Any]] = []
        self._on_record = on_record

    # -- cookie jar --------------------------------------------------------

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        self.cookies.update({k: v for k, v in cookies.items() if v})

    def export_cookies(self) -> Dict[str, str]:
        return dict(self.cookies)

    def _absorb_cookies(self, headers) -> None:
        values = headers.get_all("Set-Cookie") or []
        for value in values:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(value)
            except http.cookies.CookieError:
                continue
            for name, morsel in jar.items():
                self.cookies[name] = morsel.value

    # -- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        record_request: Any = None,
        headers: Optional[Dict[str, str]] = None,
        bearer: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        request_headers = {"Accept": "application/json"}
        data: Optional[bytes] = None
        if body is not None:
            data = body
            if content_type:
                request_headers["Content-Type"] = content_type
        elif json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if bearer:
            request_headers["Authorization"] = f"Bearer {bearer}"
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in self.cookies.items()
            )
        if headers:
            request_headers.update(headers)

        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                status = response.status
                raw = response.read().decode("utf-8", "replace")
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read().decode("utf-8", "replace")
            response_headers = error.headers
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise HttpError(f"{method} {path} failed: {error}") from error
        duration_ms = int((time.monotonic() - started) * 1000)

        self._absorb_cookies(response_headers)

        parsed: Optional[Any] = None
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None

        record = {
            "client": self.label,
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "request": (
                redact(json_body)
                if json_body is not None
                else (redact(record_request) if record_request is not None else None)
            ),
            "response": (
                redact(parsed) if parsed is not None else redact_text(raw)[:4000]
            ),
        }
        if self._on_record is not None:
            self._on_record(record)
        else:
            self.records.append(record)

        return HttpResponse(status, parsed, raw, dict(response_headers.items()))

    # Convenience wrappers -------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> HttpResponse:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs: Any) -> HttpResponse:
        return self.request("PUT", path, json_body=json_body, **kwargs)

    def post_multipart(
        self,
        path: str,
        *,
        fields: Dict[str, str],
        files: List[Tuple[str, str, bytes]],
        **kwargs: Any,
    ) -> HttpResponse:
        """POST multipart/form-data; the record keeps only field/file names."""
        body, content_type = encode_multipart(fields, files)
        return self.request(
            "POST",
            path,
            body=body,
            content_type=content_type,
            record_request={"fields": sorted(fields), "files": [name for _, name, _ in files]},
            **kwargs,
        )

    def head(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("HEAD", path, **kwargs)
