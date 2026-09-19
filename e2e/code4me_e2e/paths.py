"""Single source of truth for workspace and repository locations.

The harness is version-controlled inside the server repository
(``code4me2-server/e2e``) but always needs the two application checkouts as
siblings of a workspace root::

    <workspace>/
        code4me2/            # IntelliJ plugin
        code4me2-server/     # backend + this harness

``CODE4ME_E2E_WORKSPACE`` overrides the lookup, which is what CI uses after it
checks both repositories out side by side.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory holding the harness itself (compose file, cache, run artifacts).
E2E_DIR: Path = Path(__file__).resolve().parent.parent


def _find_workspace_root() -> Path:
    override = os.environ.get("CODE4ME_E2E_WORKSPACE")
    if override:
        root = Path(override).expanduser().resolve()
        if not (root / "code4me2").is_dir() or not (root / "code4me2-server").is_dir():
            raise RuntimeError(
                f"CODE4ME_E2E_WORKSPACE={root} must contain code4me2/ and code4me2-server/"
            )
        return root
    for candidate in (E2E_DIR.parent, *E2E_DIR.parents):
        if (candidate / "code4me2").is_dir() and (candidate / "code4me2-server").is_dir():
            return candidate
    raise RuntimeError(
        "Could not locate the workspace root (a directory containing code4me2/ and "
        "code4me2-server/). Set CODE4ME_E2E_WORKSPACE to override the lookup."
    )


WORKSPACE_ROOT: Path = _find_workspace_root()
SERVER_DIR: Path = WORKSPACE_ROOT / "code4me2-server"
PLUGIN_DIR: Path = WORKSPACE_ROOT / "code4me2"


def _browser_dir() -> Path:
    """The browser scripts: vendored next to the harness, else the legacy root."""
    vendored = E2E_DIR / "browser"
    if (vendored / "scenarios.py").is_file():
        return vendored
    legacy = WORKSPACE_ROOT / "task07-browser"
    if (legacy / "scenarios.py").is_file():
        return legacy
    return vendored


BROWSER_DIR: Path = _browser_dir()
