#!/usr/bin/env python3
"""Create checksummed metadata that pins native bundles to a server commit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--server-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    archives = sorted(args.directory.glob("code4me-agent-*.zip"))
    expected = {
        "code4me-agent-macos-arm64.zip",
        "code4me-agent-macos-x64.zip",
        "code4me-agent-windows-x64.zip",
        "code4me-agent-linux-x64.zip",
    }
    names = {archive.name for archive in archives}
    if names != expected:
        raise SystemExit(f"runtime release is incomplete: expected {sorted(expected)}, found {sorted(names)}")
    payload = {
        "manifest_version": 1,
        "managed_protocol_version": "1",
        "runtime_version": args.version,
        "server_commit": args.server_commit,
        "artifacts": [
            {"archive": archive.name, "sha256": sha256(archive)} for archive in archives
        ],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
