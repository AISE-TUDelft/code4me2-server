"""TLS trust for the agent's own HTTPS calls to the backend.

Inside the packaged (PyInstaller) runtime, Python's compiled-in OpenSSL CA
paths do not exist on the participant's machine, so on macOS a plain
``urlopen`` has an empty trust store and fails every HTTPS backend with
``CERTIFICATE_VERIFY_FAILED``. The bundled certifi roots are added on top of
the platform defaults, which still honour ``SSL_CERT_FILE`` and the system
store wherever Python can read it.
"""

from __future__ import annotations

import ssl
from functools import lru_cache


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
