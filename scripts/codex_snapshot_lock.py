#!/usr/bin/env python3
"""Secure supervisor for the daily Codex snapshot lock on Darwin."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from codex_reflink_darwin import BackendError, DarwinBackend


LOCK_NAME = "snapshot.lock"
LOCK_FD = 9
EXIT_FATAL = 70
EXIT_BUSY = 75

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0x01000000)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0x00100000)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x00000100)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0x00000004)


class LockSafetyError(RuntimeError):
    """A fail-closed namespace, identity, or access-policy failure."""


class LockBusyError(RuntimeError):
    """The canonical lock inode is held by another snapshot run."""


@dataclass
class StateRoot:
    parent_fd: int
    fd: int
    name: str
    path: str

    def close(self) -> None:
        first_error: Optional[OSError] = None
        for attribute in ("fd", "parent_fd"):
            value = getattr(self, attribute)
            if value < 0:
                continue
            setattr(self, attribute, -1)
            try:
                os.close(value)
            except OSError as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def _absolute_components(path: str) -> List[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise LockSafetyError("state root must be an absolute path")
    if "\x00" in path:
        raise LockSafetyError("state root contains NUL")
    if path == "/" or path.endswith("/"):
        raise LockSafetyError("state root must end in a directory component")
    components = path.split("/")[1:]
    if any(not item or item in (".", "..") or "/" in item for item in components):
        raise LockSafetyError("state root contains an unsafe path component")
    return components


def _directory_flags() -> int:
    return os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC


def _require_directory(fd: int, label: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise LockSafetyError(f"{label} is not a directory")
    return info


def _open_or_create_directory(parent_fd: int, name: str, *, create: bool) -> int:
    flags = _directory_flags()
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise LockSafetyError(f"state directory component is missing: {name!r}")
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise LockSafetyError(
                f"cannot create state directory component {name!r}: {error}"
            ) from error
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise LockSafetyError(
                f"cannot open created state directory component {name!r}: {error}"
            ) from error
        os.fsync(parent_fd)
    except OSError as error:
        raise LockSafetyError(
            f"cannot open state directory component {name!r}: {error}"
        ) from error
    try:
        _require_directory(fd, f"state directory component {name!r}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_state_root(path: str, *, create: bool) -> StateRoot:
    components = _absolute_components(path)
    canonical_path = "/" + "/".join(components)
    try:
        current_fd = os.open("/", _directory_flags())
    except OSError as error:
        raise LockSafetyError(f"cannot open filesystem root: {error}") from error
    try:
        _require_directory(current_fd, "filesystem root")
        for component in components[:-1]:
            next_fd = _open_or_create_directory(current_fd, component, create=create)
            os.close(current_fd)
            current_fd = next_fd
        state_fd = _open_or_create_directory(current_fd, components[-1], create=create)
        return StateRoot(current_fd, state_fd, components[-1], canonical_path)
    except Exception:
        os.close(current_fd)
        raise


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _lstat_at(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise LockSafetyError(f"cannot lstat {name!r}: {error}") from error


def _require_directory_name_matches(root: StateRoot) -> None:
    named = _lstat_at(root.parent_fd, root.name)
    opened = _require_directory(root.fd, "opened state root")
    if not _same_object(named, opened):
        raise LockSafetyError("state root name no longer maps to the opened directory")


def _require_canonical_root_matches(root: StateRoot) -> None:
    """Re-resolve the full nofollow path and bind it to the held state root."""
    _require_directory_name_matches(root)
    reopened = _open_state_root(root.path, create=False)
    try:
        _require_directory_name_matches(reopened)
        if not _same_object(os.fstat(root.fd), os.fstat(reopened.fd)):
            raise LockSafetyError(
                "canonical state-root path no longer maps to the held directory"
            )
    finally:
        reopened.close()


def _require_lock_preconditions(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise LockSafetyError(f"{label} is not a regular file")
    if info.st_nlink != 1:
        raise LockSafetyError(f"{label} has link count {info.st_nlink}, expected 1")
    if info.st_uid != os.geteuid():
        raise LockSafetyError(f"{label} is not owned by the effective user")


def _require_lock_name_matches(state_fd: int, lock_fd: int) -> None:
    named = _lstat_at(state_fd, LOCK_NAME)
    opened = os.fstat(lock_fd)
    _require_lock_preconditions(named, "lock name")
    _require_lock_preconditions(opened, "opened lock")
    if not _same_object(named, opened):
        raise LockSafetyError("snapshot.lock no longer maps to the opened lock inode")


def _run_test_hook(stage: str, state_root: str) -> None:
    hook = os.environ.get("CODEX_SNAPSHOT_LOCK_TEST_HOOK", "")
    if not hook:
        return
    try:
        subprocess.run([hook, stage, state_root, LOCK_NAME], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise LockSafetyError(
            f"snapshot lock test hook failed at {stage}: {error}"
        ) from error


def _harden_state_root(backend: DarwinBackend, root: StateRoot) -> None:
    _require_canonical_root_matches(root)
    backend.harden_private_state_fd(root.fd, is_directory=True)
    _require_canonical_root_matches(root)
    backend.fsync(root.parent_fd)


def _open_lock_leaf(state_fd: int) -> int:
    try:
        existing: Optional[os.stat_result] = os.stat(
            LOCK_NAME, dir_fd=state_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise LockSafetyError(f"cannot inspect snapshot.lock: {error}") from error
    if existing is not None:
        _require_lock_preconditions(existing, "snapshot.lock")

    flags = os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
    try:
        lock_fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=state_fd)
    except OSError as error:
        raise LockSafetyError(f"cannot safely open snapshot.lock: {error}") from error
    try:
        _require_lock_preconditions(os.fstat(lock_fd), "opened snapshot.lock")
        current_flags = fcntl.fcntl(lock_fd, fcntl.F_GETFL)
        fcntl.fcntl(lock_fd, fcntl.F_SETFL, current_flags & ~_O_NONBLOCK)
        return lock_fd
    except Exception:
        os.close(lock_fd)
        raise


def _flock_nonblocking(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise LockBusyError("another snapshot run holds snapshot.lock") from error
        raise LockSafetyError(f"cannot acquire snapshot.lock: {error}") from error


def _harden_lock(backend: DarwinBackend, root: StateRoot, lock_fd: int) -> None:
    _require_canonical_root_matches(root)
    _require_lock_name_matches(root.fd, lock_fd)
    backend.harden_private_state_fd(lock_fd, is_directory=False)
    _require_lock_name_matches(root.fd, lock_fd)
    _require_canonical_root_matches(root)
    backend.fsync(root.fd)


def _prepare_lock(state_root: str) -> Tuple[StateRoot, int, DarwinBackend]:
    if sys.platform != "darwin":
        raise LockSafetyError("snapshot locking is supported only on Darwin")
    backend = DarwinBackend()
    root = _open_state_root(state_root, create=True)
    try:
        _harden_state_root(backend, root)
        _run_test_hook("before_lock_open", state_root)
        lock_fd = _open_lock_leaf(root.fd)
        try:
            _harden_lock(backend, root, lock_fd)
            return root, lock_fd, backend
        except Exception:
            os.close(lock_fd)
            raise
    except Exception:
        root.close()
        raise


def acquire_and_exec(state_root: str, script: str, script_args: Sequence[str]) -> None:
    if not os.path.isabs(script):
        raise LockSafetyError("snapshot script path must be absolute")
    try:
        script_info = os.lstat(script)
    except OSError as error:
        raise LockSafetyError(f"cannot lstat snapshot script: {error}") from error
    if not stat.S_ISREG(script_info.st_mode):
        raise LockSafetyError("snapshot script is not a regular file")

    root, lock_fd, backend = _prepare_lock(state_root)
    inherited_lock = False
    try:
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, lock_fd)
        _run_test_hook("before_flock", state_root)
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, lock_fd)
        _flock_nonblocking(lock_fd)
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, lock_fd)
        _run_test_hook("after_flock", state_root)
        _require_canonical_root_matches(root)
        _harden_lock(backend, root, lock_fd)
        _require_canonical_root_matches(root)
        root.close()

        if lock_fd != LOCK_FD:
            os.dup2(lock_fd, LOCK_FD, inheritable=True)
            os.close(lock_fd)
            lock_fd = -1
        else:
            os.set_inheritable(LOCK_FD, True)
            lock_fd = -1
        inherited_lock = True
        os.execv(script, [script, "--snapshot-lock-held", *script_args])
    except Exception:
        try:
            root.close()
        except OSError:
            pass
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        if inherited_lock:
            try:
                os.close(LOCK_FD)
            except OSError:
                pass
        raise


def verify_inherited_lock(state_root: str, inherited_fd: int) -> None:
    if inherited_fd != LOCK_FD:
        raise LockSafetyError(f"inherited snapshot lock must use fd {LOCK_FD}")
    if sys.platform != "darwin":
        raise LockSafetyError("snapshot locking is supported only on Darwin")
    backend = DarwinBackend()
    root = _open_state_root(state_root, create=False)
    try:
        _harden_state_root(backend, root)
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, inherited_fd)
        _run_test_hook("verify_before_flock", state_root)
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, inherited_fd)
        _flock_nonblocking(inherited_fd)
        _require_canonical_root_matches(root)
        _require_lock_name_matches(root.fd, inherited_fd)
        _run_test_hook("verify_after_flock", state_root)
        _require_canonical_root_matches(root)
        _harden_lock(backend, root, inherited_fd)
        _require_canonical_root_matches(root)
        os.set_inheritable(inherited_fd, True)
    finally:
        root.close()


def _append_log(path: Optional[str], message: str) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(message + "\n")
    except OSError:
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_snapshot_lock.py")
    commands = parser.add_subparsers(dest="command", required=True)

    acquire = commands.add_parser("acquire")
    acquire.add_argument("--state-root", required=True)
    acquire.add_argument("--script", required=True)
    acquire.add_argument("--log")
    acquire.add_argument("script_args", nargs=argparse.REMAINDER)

    verify = commands.add_parser("verify")
    verify.add_argument("--state-root", required=True)
    verify.add_argument("--fd", type=int, default=LOCK_FD)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "acquire":
            script_args = list(args.script_args)
            if script_args[:1] == ["--"]:
                script_args = script_args[1:]
            acquire_and_exec(args.state_root, args.script, script_args)
            raise AssertionError("exec unexpectedly returned")
        verify_inherited_lock(args.state_root, args.fd)
        return 0
    except LockBusyError as error:
        message = f"Snapshot already running; lock unavailable: {error}"
        _append_log(getattr(args, "log", None), message)
        print(message, file=sys.stderr)
        return EXIT_BUSY
    except (LockSafetyError, BackendError, OSError) as error:
        message = f"Snapshot lock safety failure: {error}"
        _append_log(getattr(args, "log", None), message)
        print(message, file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
