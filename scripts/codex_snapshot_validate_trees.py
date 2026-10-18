#!/usr/bin/env python3
"""Validate whole-tree snapshot inputs and keep outputs outside those trees."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional


SOURCE_NAMES = ("sessions", "archived_sessions")


def lstat_or_missing(path: str, label: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"Unable to inspect {label}: {path}: {exc}") from exc


def require_directory(path: str, label: str) -> bool:
    info = lstat_or_missing(path, label)
    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} is not a non-symlink directory: {path}")
    return True


def is_within(path: str, directory: str) -> bool:
    try:
        resolved_path = Path(path).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Unable to resolve snapshot output path: {path}: {exc}") from exc
    try:
        resolved_directory = Path(directory).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Unable to resolve Codex source tree: {directory}: {exc}") from exc

    try:
        resolved_path.relative_to(resolved_directory)
        return True
    except ValueError:
        return False


def validate(codex_root: str, output_paths: tuple[str, ...]) -> None:
    root_exists = require_directory(codex_root, "Snapshot source root")
    source_paths = []
    if root_exists:
        for source_name in SOURCE_NAMES:
            source_path = os.path.join(codex_root, source_name)
            if require_directory(source_path, "Snapshot source path"):
                source_paths.append(source_path)

    for output_path in output_paths:
        for source_path in source_paths:
            if is_within(output_path, source_path):
                raise ValueError(
                    "Snapshot output path is inside Codex source tree: "
                    f"{output_path} (source: {source_path})"
                )


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "Usage: codex_snapshot_validate_trees.py CODEX_ROOT "
            "SNAPSHOT_DIR STAGING_DIR STATE_ROOT",
            file=sys.stderr,
        )
        return 2

    try:
        validate(sys.argv[1], tuple(sys.argv[2:]))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
