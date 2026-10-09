"""TLS trust for the agent's own HTTPS calls to the backend.

Inside the packaged (PyInstaller) runtime, Python's compiled-in OpenSSL CA
paths do not exist on the participant's machine, so on macOS a plain
``urlopen`` has an empty trust store and fails every HTTPS backend with
``CERTIFICATE_VERIFY_FAILED``. The bundled certifi roots are added on top of
the platform defaults, which still honour ``SSL_CERT_FILE`` and the system
store wherever Python can read it.

Every backend call also names the agent in ``User-Agent``: the Cloudflare edge
in front of the backend rejects urllib's default ``Python-urllib/3.x`` with
403 (error 1010) before the request reaches the server.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

from code4me2_agent._build_version import RUNTIME_VERSION

USER_AGENT = f"code4me2-agent/{RUNTIME_VERSION}"


@lru_cache(maxsize=1)
def https_context() -> ssl.SSLContext:
    """A verifying client context: platform trust plus the bundled certifi roots."""
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        # Without certifi (or its bundle file) the platform defaults are all
        # there is; verification stays on either way.
        pass
    return context
