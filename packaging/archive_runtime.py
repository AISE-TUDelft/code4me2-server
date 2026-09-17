#!/usr/bin/env python3
"""Create the flat, symlink-free runtime ZIP consumed by the IDE plugin."""

from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path


def collect_tree(root: Path) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    root_real = root.resolve()

    def visit(
        directory: Path,
        archive_prefix: Path,
        ancestors: frozenset[Path],
        external_tree: bool = False,
    ) -> None:
        real_directory = directory.resolve()
        if real_directory in ancestors:
            return
        next_ancestors = ancestors | {real_directory}
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            archive_path = (archive_prefix / child.name).as_posix()
            resolved_child = child.resolve()
            outside_bundle = not resolved_child.is_relative_to(root_real)
            if outside_bundle and not external_tree:
                # PyInstaller's macOS x64 bundle can contain file symlinks into
                # the runner's Python framework. The installer cannot restore
                # symlinks, so materialize the linked framework in the archive.
                if child.is_symlink() and child.is_dir():
                    visit(
                        resolved_child,
                        archive_prefix / child.name,
                        next_ancestors,
                        external_tree=True,
                    )
                    continue
                if child.is_symlink() and child.is_file():
                    entries.setdefault(archive_path, resolved_child)
                    continue
                raise SystemExit(
                    f"runtime bundle contains an unsupported external entry: {child}"
                )
            if child.is_dir():
                visit(child, archive_prefix / child.name, next_ancestors, external_tree)
            elif child.is_file():
                entries.setdefault(archive_path, resolved_child)

    visit(root, Path(), frozenset())
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("dist/code4me2-agent"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"runtime bundle does not exist: {root}")
    executable = "code4me2-agent.exe" if args.platform.startswith("windows") else "code4me2-agent"
    entries = collect_tree(root)
    if executable not in entries:
        raise SystemExit(f"runtime executable is missing at archive root: {executable}")

    output = args.output or Path(f"code4me-agent-{args.platform}.zip")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for archive_path, source in sorted(entries.items()):
            info = zipfile.ZipInfo(archive_path)
            mode = 0o755 if archive_path == executable else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            with source.open("rb") as source_handle, archive.open(info, "w") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
    print(f"archived {output} ({len(entries)} files)")


if __name__ == "__main__":
    main()
