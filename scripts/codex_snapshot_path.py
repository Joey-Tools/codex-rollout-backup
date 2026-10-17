#!/usr/bin/env python3
"""Resolve the per-computer default OneDrive snapshot directory."""

from __future__ import annotations

import os
import re
import subprocess
import sys


INVALID_CHARACTERS = set('/\\:*?"<>|')
RESERVED_NAMES = {".lock", "desktop.ini"}
RESERVED_DEVICE_NAME = re.compile(
    r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?$", re.IGNORECASE
)


def read_computer_name() -> str:
    override = os.environ.get("CODEX_BACKUP_COMPUTER_NAME")
    if override is not None:
        return override

    try:
        result = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("timed out reading macOS ComputerName with scutil") from exc
    except OSError as exc:
        raise ValueError(f"Unable to read macOS ComputerName with scutil: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"Unable to read macOS ComputerName with scutil{suffix}")

    output = result.stdout
    if output.endswith(b"\n"):
        output = output[:-1]
        if output.endswith(b"\r"):
            output = output[:-1]
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("macOS ComputerName is not valid UTF-8") from exc


def validate_computer_name(name: str) -> None:
    if not name or name in {".", ".."}:
        raise ValueError("computer name must be a non-empty single directory component")
    if name.startswith(" "):
        raise ValueError("computer name must not begin with a space")
    if name.endswith((".", " ")):
        raise ValueError("computer name must not end in a dot or space")
    if any(char in INVALID_CHARACTERS for char in name):
        raise ValueError("computer name contains a OneDrive-reserved character")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name):
        raise ValueError("computer name contains a control character")
    folded_name = name.casefold()
    if folded_name in RESERVED_NAMES or RESERVED_DEVICE_NAME.fullmatch(name):
        raise ValueError("computer name is reserved by OneDrive or Windows")
    if name.startswith("~$") or "_vti_" in folded_name:
        raise ValueError("computer name contains a OneDrive-reserved name pattern")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: codex_snapshot_path.py HOME", file=sys.stderr)
        return 2

    try:
        computer_name = read_computer_name()
        validate_computer_name(computer_name)
    except ValueError as exc:
        print(f"Snapshot directory resolution failed: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(
        os.path.join(
            sys.argv[1],
            "OneDrive",
            "Backup",
            "dotfiles",
            "codex",
            computer_name,
            "snapshots",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
