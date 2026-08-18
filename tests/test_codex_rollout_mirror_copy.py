#!/usr/bin/env python3
"""Tests for the fail-closed single-rollout mirror copy helper."""

from __future__ import annotations

import ctypes
import dis
import errno
import fcntl
import gc
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import weakref
from typing import Any, Callable, Dict, Iterable, Optional
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_rollout_mirror_copy.py"
BACKEND_PATH = REPO_ROOT / "scripts" / "codex_reflink_darwin.py"
ROLLOUT_NAME = "rollout-2026-08-17T12-00-00-11111111-1111-4111-8111-111111111111.jsonl"


def load_module(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_helper_module(name: str) -> Any:
    if "codex_reflink_darwin" not in sys.modules:
        load_module("codex_reflink_darwin", BACKEND_PATH)
    return load_module(name, SCRIPT_PATH)


class MirrorCopyImportTests(unittest.TestCase):
    def test_helper_import_is_side_effect_free(self) -> None:
        module = load_helper_module("codex_rollout_mirror_copy_import")
        self.assertTrue(callable(module.sync_one))
        self.assertTrue(callable(module.main))


@unittest.skipUnless(sys.platform == "darwin", "requires Darwin filesystem APIs")
class DarwinMirrorCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.source_parent = self.root / "source"
        self.destination_parent = self.root / "mirror"
        self.source_parent.mkdir(mode=0o700)
        self.destination_parent.mkdir(mode=0o700)
        self.source = self.source_parent / ROLLOUT_NAME
        self.destination = self.destination_parent / ROLLOUT_NAME
        self.helper = load_helper_module(f"codex_rollout_mirror_copy_{id(self)}")
        self.backend_module = sys.modules["codex_reflink_darwin"]

    def write_source(self, content: bytes, *, mode: int = 0o600) -> None:
        self.source.write_bytes(content)
        os.chmod(self.source, mode)
        os.utime(self.source, ns=(1_700_000_000_000_000_000,) * 2)

    def write_destination(self, content: bytes = b"old\n") -> os.stat_result:
        self.destination.write_bytes(content)
        os.chmod(self.destination, 0o600)
        return self.destination.stat()

    def mutate_source_same_inode(
        self,
        mutation: str,
        *,
        rewrite: Optional[bytes] = None,
        append: bytes = b"appended",
        truncate_size: Optional[int] = None,
    ) -> None:
        before = self.source.stat()
        with self.source.open("r+b") as output:
            if mutation == "truncate":
                target_size = (
                    max(1, before.st_size // 2)
                    if truncate_size is None
                    else truncate_size
                )
                output.truncate(target_size)
            elif mutation == "rewrite":
                if rewrite is None or len(rewrite) != before.st_size:
                    raise AssertionError("same-size rewrite fixture has wrong size")
                output.seek(0)
                output.write(rewrite)
            elif mutation == "append":
                output.seek(0, os.SEEK_END)
                output.write(append)
            else:
                raise AssertionError(f"unknown source mutation: {mutation}")
            output.flush()
            os.fsync(output.fileno())
        self.assertEqual(self.source.stat().st_ino, before.st_ino)

    def add_extended_acl(self, path: pathlib.Path) -> None:
        result = subprocess.run(
            ["/bin/chmod", "+a", "everyone deny write", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.skipTest(f"cannot create ACL fixture: {result.stderr}")

    def clear_extended_acl(self, path: pathlib.Path) -> None:
        subprocess.run(
            ["/bin/chmod", "-N", str(path)],
            check=True,
            capture_output=True,
        )

    @staticmethod
    def remove_path(path: pathlib.Path) -> None:
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)

    def sync(
        self,
        *,
        action_hook: Optional[Callable[[str], None]] = None,
        backend_factory: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"action_hook": action_hook}
        if backend_factory is not None:
            kwargs["backend_factory"] = backend_factory
        receipt = self.helper.sync_one(
            str(self.source.absolute()),
            str(self.destination.absolute()),
            **kwargs,
        )
        result = receipt.to_dict()
        self.assertIsInstance(result, dict)
        return result

    def validate_receipt(
        self,
        payload: Dict[str, Any],
        exit_status: int,
        *,
        raw_input: Optional[bytes] = None,
        source: Optional[str] = None,
        destination: Optional[str] = None,
    ) -> subprocess.CompletedProcess[bytes]:
        data = raw_input
        if data is None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "validate-receipt",
                "--source",
                source if source is not None else str(self.source.absolute()),
                "--destination",
                (
                    destination
                    if destination is not None
                    else str(self.destination.absolute())
                ),
                "--exit-status",
                str(exit_status),
            ],
            input=data,
            capture_output=True,
            timeout=10,
        )

    def run_transaction_via_main(
        self,
        transaction: Any,
    ) -> tuple[int, Dict[str, Any], bytes, str]:
        def injected_sync(
            source: str,
            destination: str,
            *,
            action_hook: Optional[Callable[[str], None]] = None,
        ) -> Any:
            self.assertIsNone(action_hook)
            return transaction.sync_one(source, destination)

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with mock.patch.object(self.helper, "sync_one", side_effect=injected_sync):
                with mock.patch.object(self.helper, "_cli_hook", return_value=None):
                    with mock.patch.object(self.helper.sys, "stdout", stdout):
                        with mock.patch.object(self.helper.sys, "stderr", stderr):
                            exit_status = self.helper.main(
                                (
                                    "sync-one",
                                    "--source",
                                    str(self.source.absolute()),
                                    "--destination",
                                    str(self.destination.absolute()),
                                    "--json",
                                )
                            )
        except BaseException as exc:
            self.fail(
                f"sync-one CLI leaked {type(exc).__name__} instead of one JSON receipt"
            )
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        raw = lines[0].encode("utf-8")
        receipt = json.loads(raw)
        self.assertIsInstance(receipt, dict)
        return exit_status, receipt, raw, stderr.getvalue()

    def run_raw_transaction_via_main(
        self,
        transaction: Any,
    ) -> tuple[BaseException, str, str]:
        def injected_sync(
            source: str,
            destination: str,
            *,
            action_hook: Optional[Callable[[str], None]] = None,
        ) -> Any:
            self.assertIsNone(action_hook)
            return transaction.sync_one(source, destination)

        stdout = io.StringIO()
        stderr = io.StringIO()
        escaped: Optional[BaseException] = None
        with mock.patch.object(self.helper, "sync_one", side_effect=injected_sync):
            with mock.patch.object(self.helper, "_cli_hook", return_value=None):
                with mock.patch.object(self.helper.sys, "stdout", stdout):
                    with mock.patch.object(self.helper.sys, "stderr", stderr):
                        try:
                            self.helper.main(
                                (
                                    "sync-one",
                                    "--source",
                                    str(self.source.absolute()),
                                    "--destination",
                                    str(self.destination.absolute()),
                                    "--json",
                                )
                            )
                        except BaseException as exc:
                            escaped = exc
                        else:
                            self.fail("raw BaseException unexpectedly became a receipt")
        self.assertIsNotNone(escaped)
        return escaped, stdout.getvalue(), stderr.getvalue()

    def interrupt_on_traced_local_handoff(
        self,
        target_code: Any,
        predicate: Callable[[Any], bool],
        operation: Callable[[], Any],
        primary: BaseException,
    ) -> Dict[str, Any]:
        previous_trace = sys.gettrace()
        captured: Dict[str, Any] = {}

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code is target_code
                and not captured
                and predicate(frame)
            ):
                captured.update(frame.f_locals)
                captured["__line__"] = frame.f_lineno
                sys.settrace(None)
                raise primary
            return trace

        escaped: Optional[BaseException] = None
        sys.settrace(trace)
        try:
            try:
                operation()
            except BaseException as exc:
                escaped = exc
            else:
                self.fail("trace interruption was not delivered")
        finally:
            sys.settrace(previous_trace)
        self.assertTrue(captured)
        self.assertIs(escaped, primary)
        traceback_cursor = escaped.__traceback__
        saw_trace_frame = False
        while traceback_cursor is not None:
            if traceback_cursor.tb_frame.f_code is trace.__code__:
                saw_trace_frame = True
                break
            traceback_cursor = traceback_cursor.tb_next
        self.assertTrue(saw_trace_frame)
        return captured

    def interrupt_handler_preserving_primary(
        self,
        target_code: Any,
        predicate: Callable[[Any], bool],
        operation: Callable[[], Any],
        secondary: BaseException,
        *,
        expected_primary: Optional[BaseException] = None,
    ) -> Dict[str, Any]:
        previous_trace = sys.gettrace()
        captured: Dict[str, Any] = {}

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code is target_code
                and not captured
                and predicate(frame)
            ):
                handler_primary = expected_primary
                if handler_primary is None:
                    handler_primary = frame.f_locals.get("primary")
                    if not isinstance(handler_primary, BaseException):
                        handler_primary = frame.f_locals.get("exc")
                if not isinstance(handler_primary, BaseException):
                    raise AssertionError("handler primary was not visible")
                captured.update(frame.f_locals)
                captured["__primary__"] = handler_primary
                captured["__traceback__"] = handler_primary.__traceback__
                captured["__line__"] = frame.f_lineno
                sys.settrace(None)
                raise secondary
            return trace

        escaped: Optional[BaseException] = None
        sys.settrace(trace)
        try:
            try:
                operation()
            except BaseException as exc:
                escaped = exc
            else:
                self.fail("handler interruption was not delivered")
        finally:
            sys.settrace(previous_trace)
        self.assertTrue(captured)
        self.assertIs(escaped, captured["__primary__"])
        original_traceback = captured["__traceback__"]
        if original_traceback is not None:
            cursor = escaped.__traceback__
            preserved = False
            while cursor is not None:
                if cursor is original_traceback:
                    preserved = True
                    break
                cursor = cursor.tb_next
            self.assertTrue(preserved)
        return captured

    @staticmethod
    def observe_fd_owner_close(owner: Any) -> Dict[str, Any]:
        state = owner._state
        real_close = state.close
        observation = {"active_closes": 0, "fd": None}

        def recording_close(
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            if state.fd >= 0:
                observation["active_closes"] += 1
                observation["fd"] = state.fd
            real_close(
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        state.close = recording_close
        return observation

    @staticmethod
    def observe_acl_owner_close(owner: Any) -> Dict[str, Any]:
        state = owner._state
        real_close = state.close
        observation = {"active_closes": 0, "pointer": None}

        def recording_close(*, primary_error: Optional[BaseException] = None) -> None:
            if state.active:
                observation["active_closes"] += 1
                observation["pointer"] = state.pointer
            real_close(primary_error=primary_error)

        state.close = recording_close
        return observation

    @staticmethod
    def open_fd_set(limit: int = 512) -> set[int]:
        descriptors = set()
        for descriptor in range(limit):
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                descriptors.add(descriptor)
        return descriptors

    @staticmethod
    def source_line_number(
        code: Any, stripped_source: str, *, occurrence: int = 1
    ) -> int:
        lines = pathlib.Path(code.co_filename).read_text(encoding="utf-8").splitlines()
        executable_lines = sorted(
            {
                line
                for _offset, line in dis.findlinestarts(code)
                if isinstance(line, int)
            }
        )
        matches = [
            line_number
            for line_number in executable_lines
            if line_number <= len(lines)
            and lines[line_number - 1].strip() == stripped_source
        ]
        if occurrence < 1 or len(matches) < occurrence:
            raise AssertionError(
                f"expected occurrence {occurrence} of {stripped_source!r} "
                f"in {code.co_name}, got {matches}"
            )
        return matches[occurrence - 1]

    def updated_receipt(self) -> Dict[str, Any]:
        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(b"validator\n")
        receipt = self.sync()
        self.assertEqual(receipt["outcome"], "updated")
        return receipt

    def assert_deferred(self, receipt: Dict[str, Any]) -> None:
        self.assertEqual(receipt["outcome"], "deferred")
        self.assertFalse(receipt["destination_mutated"])
        self.assertIsInstance(receipt["reason"], str)
        self.assertIsInstance(receipt["detail"], str)
        self.assertEqual(receipt["new_size"], receipt["old_size"])
        self.assertEqual(receipt["new_identity"], receipt["old_identity"])

    def stage_paths(self) -> list[pathlib.Path]:
        return [
            child
            for child in self.destination_parent.iterdir()
            if child != self.destination
        ]

    def assert_no_stage_names(self) -> None:
        self.assertEqual(self.stage_paths(), [])

    def destination_snapshot(self) -> tuple[int, bytes]:
        info = self.destination.stat()
        return (info.st_ino, self.destination.read_bytes())

    def assert_destination_snapshot(self, expected: tuple[int, bytes]) -> None:
        self.assertEqual(self.destination_snapshot(), expected)

    def make_injected_clone_backend(
        self,
        errno_value: int,
        *,
        create_partial_child: bool = False,
        clone_reason: str = "clone_failed",
        ordinary_copy_error: Optional[BaseException] = None,
    ) -> Any:
        backend_module = self.backend_module

        class InjectedCloneBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fallback_calls = 0

            def strict_clone(
                inner_self,
                source_fd: int,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
                writable: bool = False,
            ) -> int:
                del source_fd, writable
                inner_self._authorize_state(authorize_state, "create_clone")
                if create_partial_child:
                    child_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        os.write(child_fd, b"partial")
                    finally:
                        os.close(child_fd)
                raise backend_module.BackendError(
                    clone_reason,
                    "injected fclonefileat failure",
                    errno_value,
                )

            def ordinary_copy_to_absent(inner_self, *args: Any, **kwargs: Any) -> int:
                inner_self.fallback_calls += 1
                if ordinary_copy_error is not None:
                    raise ordinary_copy_error
                return super().ordinary_copy_to_absent(*args, **kwargs)

        return InjectedCloneBackend()

    def make_publish_order_backend(
        self, *, fail_published_fullsync: bool = False
    ) -> Any:
        backend_module = self.backend_module

        class PublishOrderBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.events = []
                inner_self.after_rename = False
                inner_self.after_published_fullsync = False
                rename = inner_self._renameatx_np

                def wrapped_rename(*args: Any) -> int:
                    inner_self.events.append("rename")
                    result = rename(*args)
                    if result == 0:
                        inner_self.after_rename = True
                    return result

                inner_self._renameatx_np = wrapped_rename

            def fsync(inner_self, fd: int) -> None:
                if inner_self.after_rename:
                    inner_self.events.append("parent_fsync")
                super().fsync(fd)

            def full_fsync(inner_self, fd: int) -> None:
                if not inner_self.after_rename:
                    inner_self.events.append("candidate_fullfsync")
                    super().full_fsync(fd)
                    return
                inner_self.events.append("published_fullfsync")
                if fail_published_fullsync:
                    raise backend_module.BackendError(
                        "full_fsync_failed",
                        "injected post-rename published inode flush failure",
                        errno.EIO,
                    )
                super().full_fsync(fd)
                inner_self.after_published_fullsync = True

            def require_identity_at(
                inner_self,
                parent_fd: int,
                name: str,
                expected: Any,
            ) -> Any:
                result = super().require_identity_at(parent_fd, name, expected)
                if inner_self.after_published_fullsync:
                    inner_self.events.append("final_identity_postcheck")
                return result

            def _require_name_absent(inner_self, parent_fd: int, name: str) -> None:
                super()._require_name_absent(parent_fd, name)
                if inner_self.after_published_fullsync:
                    inner_self.events.append("stage_absence_postcheck")

        return PublishOrderBackend()

    def sync_with_final_source_prefix_hook(
        self,
        branch: str,
        backend: Any,
        terminal_hook: Callable[[int], None],
    ) -> tuple[Dict[str, Any], int]:
        final_validation = False
        terminal_calls = 0

        def action_hook(action: str) -> None:
            nonlocal final_validation, terminal_calls
            if branch != "updated" and action == "authorize_cleanup_stage":
                final_validation = True
                return
            if action != "before_source_prefix_return" or not final_validation:
                return
            terminal_calls += 1
            terminal_hook(terminal_calls)

        if branch != "updated":
            receipt = self.sync(
                action_hook=action_hook,
                backend_factory=lambda: backend,
            )
            return receipt, terminal_calls

        real_publish = backend.publish_staged_name

        def publish_with_final_validator(*args: Any, **kwargs: Any) -> Any:
            validator = kwargs["validate_after_authorization"]

            def marked_validator() -> Any:
                nonlocal final_validation
                final_validation = True
                try:
                    return validator()
                finally:
                    final_validation = False

            kwargs["validate_after_authorization"] = marked_validator
            return real_publish(*args, **kwargs)

        with mock.patch.object(
            backend,
            "publish_staged_name",
            side_effect=publish_with_final_validator,
        ):
            receipt = self.sync(
                action_hook=action_hook,
                backend_factory=lambda: backend,
            )
        return receipt, terminal_calls

    def test_create_stage_failure_does_not_remove_authorizer_replacement(self) -> None:
        backend_module = self.backend_module

        class FailFirstFsyncBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fail_next_fsync = True

            def fsync(inner_self, fd: int) -> None:
                if inner_self.fail_next_fsync:
                    inner_self.fail_next_fsync = False
                    raise backend_module.BackendError(
                        "injected_stage_setup_failure",
                        "injected stage setup fsync failure",
                        errno.EIO,
                    )
                super().fsync(fd)

        backend = FailFirstFsyncBackend()
        stage = self.destination_parent / "create-failure-stage"
        held = self.destination_parent / "held-created-stage"
        sentinel = stage / "sentinel"
        parent_fd, name = backend.open_absolute_parent(str(stage))
        injected = False

        def replace_before_remove(action: str) -> None:
            nonlocal injected
            if action != "remove_stage" or injected:
                return
            injected = True
            os.replace(stage, held)
            stage.mkdir(mode=0o700)
            sentinel.write_bytes(b"replacement\n")

        try:
            with self.assertRaises(backend_module.BackendError) as caught:
                backend.create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=replace_before_remove,
                )
        finally:
            os.close(parent_fd)

        self.assertTrue(injected)
        self.assertEqual(caught.exception.reason, "injected_stage_setup_failure")
        self.assertTrue(
            any(
                "identity-bound cleanup failed" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )
        self.assertEqual(sentinel.read_bytes(), b"replacement\n")
        self.assertTrue(held.is_dir())
        self.assertEqual(list(held.iterdir()), [])

    def test_stage_setup_primary_and_cleanup_authorizer_errors_are_aggregated(
        self,
    ) -> None:
        backend_module = self.backend_module
        primary = backend_module.BackendError(
            "injected_stage_setup_primary",
            "injected stage setup primary failure",
            errno.EIO,
        )

        class FailFirstFsyncBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fail_next_fsync = True

            def fsync(inner_self, fd: int) -> None:
                if inner_self.fail_next_fsync:
                    inner_self.fail_next_fsync = False
                    raise primary
                super().fsync(fd)

        backend = FailFirstFsyncBackend()
        stage = self.destination_parent / "aggregate-stage-errors"
        parent_fd, name = backend.open_absolute_parent(str(stage))
        cleanup_authorizer_calls = 0

        def fail_cleanup_authorization(action: str) -> None:
            nonlocal cleanup_authorizer_calls
            if action != "remove_stage":
                return
            cleanup_authorizer_calls += 1
            raise backend_module.BackendError(
                "injected_cleanup_authorizer",
                "injected cleanup authorization failure",
                errno.EACCES,
            )

        try:
            with self.assertRaises(backend_module.BackendError) as caught:
                backend.create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=fail_cleanup_authorization,
                )
        finally:
            os.close(parent_fd)

        self.assertEqual(cleanup_authorizer_calls, 1)
        self.assertIs(caught.exception, primary)
        self.assertEqual(caught.exception.reason, "injected_stage_setup_primary")
        self.assertEqual(
            caught.exception.detail, "injected stage setup primary failure"
        )
        self.assertEqual(caught.exception.errno_value, errno.EIO)
        cleanup_diagnostic = getattr(caught.exception, "cleanup_diagnostic", "")
        self.assertIn("identity-bound cleanup failed", cleanup_diagnostic)
        self.assertIn("injected cleanup authorization failure", cleanup_diagnostic)
        self.assertTrue(
            any(
                "injected cleanup authorization failure" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )
        self.assertTrue(stage.is_dir())
        self.assertEqual(list(stage.iterdir()), [])

    def test_stage_setup_baseexception_primary_identity_and_traceback_win(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CleanupBomb(BaseException):
            pass

        cases = (
            (
                "pre_authorizer",
                KeyboardInterrupt("keyboard primary"),
                SystemExit(81),
            ),
            (
                "authorizer",
                SystemExit(73),
                CleanupBomb("authorizer cleanup bomb"),
            ),
            (
                "post_authorizer",
                KeyboardInterrupt("post primary"),
                CleanupBomb("post-authorizer cleanup bomb"),
            ),
        )
        for phase, primary, cleanup_error in cases:
            with self.subTest(phase=phase, primary=type(primary).__name__):
                stage = self.destination_parent / f"baseexception-stage-{phase}"
                original_args = primary.args
                original_code = getattr(primary, "code", None)

                class InjectedBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.primary_started = False
                        inner_self.cleanup_injected = False
                        inner_self.post_authorized = False
                        inner_self.traceback_at_cleanup = None
                        inner_self.cleanup_parent_fd = -1
                        inner_self.cleanup_parent_fsyncs = 0

                    def fsync(inner_self, fd: int) -> None:
                        if not inner_self.primary_started:
                            inner_self.primary_started = True
                            raise primary
                        if fd == inner_self.cleanup_parent_fd and not stage.exists():
                            inner_self.cleanup_parent_fsyncs += 1
                        super().fsync(fd)

                    def validate_stage_container(inner_self, fd: int) -> Any:
                        should_fail = inner_self.primary_started and (
                            (phase == "pre_authorizer")
                            or (
                                phase == "post_authorizer"
                                and inner_self.post_authorized
                            )
                        )
                        if should_fail and not inner_self.cleanup_injected:
                            inner_self.cleanup_injected = True
                            inner_self.traceback_at_cleanup = primary.__traceback__
                            raise cleanup_error
                        return super().validate_stage_container(fd)

                backend = InjectedBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage))
                backend.cleanup_parent_fd = parent_fd
                cleanup_authorization_calls = 0

                def authorize(action: str) -> None:
                    nonlocal cleanup_authorization_calls
                    if action != "remove_stage":
                        return
                    cleanup_authorization_calls += 1
                    if phase == "authorizer":
                        backend.cleanup_injected = True
                        backend.traceback_at_cleanup = primary.__traceback__
                        raise cleanup_error
                    if phase == "post_authorizer":
                        backend.post_authorized = True

                try:
                    actual = None
                    try:
                        backend.create_private_stage_parent(
                            parent_fd,
                            name,
                            authorize_state=authorize,
                        )
                    except type(primary) as escaped:
                        actual = escaped
                    except BaseException as escaped:
                        self.fail(
                            "cleanup replaced primary with "
                            f"{type(escaped).__name__}: {escaped}"
                        )
                    else:
                        self.fail("stage setup unexpectedly succeeded")
                finally:
                    os.close(parent_fd)

                self.assertTrue(backend.cleanup_injected)
                self.assertIs(actual, primary)
                self.assertEqual(actual.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(actual.code, original_code)
                self.assertIsNotNone(backend.traceback_at_cleanup)
                traceback_cursor = actual.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is backend.traceback_at_cleanup:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)
                cleanup_diagnostic = getattr(
                    actual,
                    "cleanup_diagnostic",
                    "",
                )
                self.assertIn("identity-bound cleanup failed", cleanup_diagnostic)
                self.assertIn(str(cleanup_error), cleanup_diagnostic)
                self.assertTrue(
                    any(
                        str(cleanup_error) in note
                        for note in getattr(actual, "__notes__", ())
                    )
                )
                self.assertEqual(cleanup_authorization_calls, 1)
                if phase == "authorizer":
                    self.assertTrue(stage.is_dir())
                    self.assertEqual(list(stage.iterdir()), [])
                    self.assertEqual(backend.cleanup_parent_fsyncs, 0)
                else:
                    self.assertFalse(stage.exists())
                    self.assertGreaterEqual(backend.cleanup_parent_fsyncs, 1)

    def test_stage_open_interrupts_preserve_primary_cleanup_and_fd_ownership(
        self,
    ) -> None:
        backend_module = self.backend_module

        class StageOpenBomb(BaseException):
            pass

        class StageCleanupBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("stage open keyboard interrupt"),
            lambda: SystemExit(93),
            lambda: StageOpenBomb("stage open custom BaseException"),
        )
        for window in ("open-call", "after-open"):
            for cleanup_fails in (False, True):
                for make_primary in primary_factories:
                    primary = make_primary()
                    with self.subTest(
                        window=window,
                        cleanup_fails=cleanup_fails,
                        primary=type(primary).__name__,
                    ):
                        stage = self.destination_parent / (
                            f"stage-open-{window}-{cleanup_fails}-"
                            f"{type(primary).__name__}"
                        )
                        cleanup_marker = (
                            f"stage-open-cleanup-{window}-"
                            f"{type(primary).__name__}-marker"
                        )
                        cleanup_error = StageCleanupBomb(
                            cleanup_marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))
                        )

                        class StageOpenFailureBackend(backend_module.DarwinBackend):
                            def __init__(inner_self) -> None:
                                super().__init__()
                                inner_self.opened_fd = -1
                                inner_self.opened_identity = None
                                inner_self.identity_injected = False
                                inner_self.unopened_cleanup_called = False
                                inner_self.cleanup_expected = None
                                inner_self.cleanup_result = "not-called"
                                inner_self.traceback_at_cleanup = None

                            def identity(inner_self, fd: int) -> Any:
                                if (
                                    window == "after-open"
                                    and fd == inner_self.opened_fd
                                    and not inner_self.identity_injected
                                ):
                                    inner_self.identity_injected = True
                                    raise primary
                                return super().identity(fd)

                            def _cleanup_unopened_stage(
                                inner_self,
                                parent_fd: int,
                                name: str,
                                expected: Any,
                                expected_container: Any,
                                *,
                                authorize_state: Callable[[str], None],
                            ) -> Optional[str]:
                                inner_self.unopened_cleanup_called = True
                                inner_self.cleanup_expected = expected
                                inner_self.traceback_at_cleanup = primary.__traceback__
                                result = super()._cleanup_unopened_stage(
                                    parent_fd,
                                    name,
                                    expected,
                                    expected_container,
                                    authorize_state=authorize_state,
                                )
                                inner_self.cleanup_result = result
                                return result

                        backend = StageOpenFailureBackend()
                        parent_fd, name = backend.open_absolute_parent(str(stage))
                        actions = []

                        def authorize(action: str) -> None:
                            actions.append(action)
                            if action != "remove_stage":
                                return
                            if backend.traceback_at_cleanup is None:
                                backend.traceback_at_cleanup = primary.__traceback__
                            if cleanup_fails:
                                raise cleanup_error

                        real_open = os.open

                        def inject_stage_open(
                            path: str,
                            flags: int,
                            mode: int = 0o777,
                            *,
                            dir_fd: Optional[int] = None,
                        ) -> int:
                            self.assertEqual(path, name)
                            self.assertEqual(dir_fd, parent_fd)
                            if window == "open-call":
                                raise primary
                            fd = real_open(
                                path,
                                flags,
                                mode,
                                dir_fd=dir_fd,
                            )
                            backend.opened_fd = fd
                            info = os.fstat(fd)
                            backend.opened_identity = (info.st_dev, info.st_ino)
                            return fd

                        original_args = primary.args
                        original_code = getattr(primary, "code", None)
                        actual = None
                        try:
                            with mock.patch.object(
                                backend_module.os,
                                "open",
                                side_effect=inject_stage_open,
                            ):
                                try:
                                    backend.create_private_stage_parent(
                                        parent_fd,
                                        name,
                                        authorize_state=authorize,
                                    )
                                except type(primary) as escaped:
                                    actual = escaped
                                except BaseException as escaped:
                                    self.fail(
                                        "stage-open cleanup replaced primary with "
                                        f"{type(escaped).__name__}"
                                    )
                                else:
                                    self.fail(
                                        "stage-open interruption unexpectedly succeeded"
                                    )
                        finally:
                            os.close(parent_fd)

                        self.assertIs(actual, primary)
                        self.assertEqual(actual.args, original_args)
                        if isinstance(primary, SystemExit):
                            self.assertEqual(actual.code, original_code)
                        self.assertEqual(actions, ["create_stage", "remove_stage"])
                        self.assertIsNotNone(backend.traceback_at_cleanup)
                        traceback_cursor = actual.__traceback__
                        preserved_traceback = False
                        while traceback_cursor is not None:
                            if traceback_cursor is backend.traceback_at_cleanup:
                                preserved_traceback = True
                                break
                            traceback_cursor = traceback_cursor.tb_next
                        self.assertTrue(preserved_traceback)

                        self.assertTrue(backend.unopened_cleanup_called)
                        self.assertIsNotNone(backend.cleanup_expected)
                        if window == "open-call":
                            self.assertEqual(backend.opened_fd, -1)
                        else:
                            self.assertTrue(backend.identity_injected)
                            self.assertGreaterEqual(backend.opened_fd, 0)
                            with self.assertRaises(OSError) as closed:
                                os.fstat(backend.opened_fd)
                            self.assertEqual(closed.exception.errno, errno.EBADF)

                        if not cleanup_fails:
                            self.assertIsNone(backend.cleanup_result)
                            self.assertFalse(stage.exists())
                            self.assertEqual(
                                getattr(primary, "cleanup_diagnostics", ()),
                                (),
                            )
                            continue

                        self.assertIsInstance(backend.cleanup_result, str)
                        self.assertIn(cleanup_marker, backend.cleanup_result)
                        self.assertTrue(stage.is_dir())
                        stage_info = stage.stat()
                        if backend.opened_identity is not None:
                            self.assertEqual(
                                (stage_info.st_dev, stage_info.st_ino),
                                backend.opened_identity,
                            )
                        self.assertEqual(stat.S_IMODE(stage_info.st_mode), 0o700)
                        self.assertEqual(stage_info.st_uid, os.geteuid())
                        self.assertEqual(list(stage.iterdir()), [])
                        diagnostic = getattr(primary, "cleanup_diagnostic", "")
                        self.assertIn("identity-bound cleanup failed", diagnostic)
                        self.assertIn(cleanup_marker, diagnostic)
                        self.assertIn(self.helper._TRUNCATED_MARKER, diagnostic)
                        self.assertLessEqual(
                            len(diagnostic.encode("utf-8")),
                            self.helper._DIAGNOSTIC_LIMIT,
                        )
                        stage.rmdir()

    def test_stage_open_cleanup_raw_failure_cannot_replace_primary(self) -> None:
        backend_module = self.backend_module

        class StageOpenBomb(BaseException):
            pass

        class StageCleanupBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("stage open keyboard interrupt"),
            lambda: SystemExit(94),
            lambda: StageOpenBomb("stage open custom BaseException"),
            lambda: RuntimeError("stage open ordinary exception"),
        )
        for make_primary in primary_factories:
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                stage = self.destination_parent / (
                    "stage-open-raw-cleanup-" + type(primary).__name__
                )
                cleanup_marker = (
                    "stage-open-raw-cleanup-marker-" + type(primary).__name__
                )
                cleanup_error = StageCleanupBomb(
                    cleanup_marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))
                )

                class RawCleanupFailureBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.cleanup_expected = None
                        inner_self.traceback_at_cleanup = None

                    def _cleanup_unopened_stage(
                        inner_self,
                        parent_fd: int,
                        name: str,
                        expected: Any,
                        expected_container: Any,
                        *,
                        authorize_state: Callable[[str], None],
                    ) -> Optional[str]:
                        del parent_fd, name, expected_container, authorize_state
                        inner_self.cleanup_expected = expected
                        inner_self.traceback_at_cleanup = primary.__traceback__
                        raise cleanup_error

                backend = RawCleanupFailureBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage))
                actions = []

                def authorize(action: str) -> None:
                    actions.append(action)

                def inject_stage_open(
                    path: str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: Optional[int] = None,
                ) -> int:
                    del flags, mode
                    self.assertEqual(path, name)
                    self.assertEqual(dir_fd, parent_fd)
                    raise primary

                original_args = primary.args
                original_code = getattr(primary, "code", None)
                actual = None
                try:
                    with mock.patch.object(
                        backend_module.os,
                        "open",
                        side_effect=inject_stage_open,
                    ):
                        try:
                            backend.create_private_stage_parent(
                                parent_fd,
                                name,
                                authorize_state=authorize,
                            )
                        except BaseException as escaped:
                            actual = escaped
                        else:
                            self.fail("stage-open interruption unexpectedly succeeded")
                finally:
                    os.close(parent_fd)

                self.assertIs(actual, primary)
                self.assertEqual(actual.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(actual.code, original_code)
                self.assertEqual(actions, ["create_stage"])
                self.assertIsNotNone(backend.cleanup_expected)
                self.assertIsNotNone(backend.traceback_at_cleanup)
                traceback_cursor = actual.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is backend.traceback_at_cleanup:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)

                self.assertTrue(stage.is_dir())
                stage_info = stage.stat()
                self.assertEqual(
                    (stage_info.st_dev, stage_info.st_ino),
                    (
                        backend.cleanup_expected.dev,
                        backend.cleanup_expected.ino,
                    ),
                )
                self.assertEqual(stat.S_IMODE(stage_info.st_mode), 0o700)
                self.assertEqual(stage_info.st_uid, os.geteuid())
                self.assertEqual(list(stage.iterdir()), [])
                diagnostic = getattr(primary, "cleanup_diagnostic", "")
                self.assertIn("identity-bound cleanup failed", diagnostic)
                self.assertIn(cleanup_marker, diagnostic)
                self.assertIn(self.helper._TRUNCATED_MARKER, diagnostic)
                self.assertLessEqual(
                    len(diagnostic.encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                self.assertTrue(
                    any(
                        cleanup_marker in note
                        for note in getattr(primary, "__notes__", ())
                    )
                )
                stage.rmdir()

    def test_absolute_stage_binding_primary_survives_identity_bound_cleanup(
        self,
    ) -> None:
        backend_module = self.backend_module

        class BindingBomb(BaseException):
            pass

        class CleanupBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("absolute binding keyboard interrupt"),
            lambda: SystemExit(89),
            lambda: BindingBomb("absolute binding custom BaseException"),
            lambda: Exception("absolute binding ordinary exception"),
        )
        for cleanup_fails in (False, True):
            for make_primary in primary_factories:
                primary = make_primary()
                with self.subTest(
                    cleanup_fails=cleanup_fails,
                    primary=type(primary).__name__,
                ):
                    stage = self.destination_parent / (
                        "absolute-binding-stage-"
                        f"{cleanup_fails}-{type(primary).__name__}"
                    )
                    cleanup_marker = (
                        f"absolute-binding-cleanup-marker-{type(primary).__name__}"
                    )
                    cleanup_error = CleanupBomb(
                        cleanup_marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))
                    )

                    class BindingFailureBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            inner_self.created_fd = -1
                            inner_self.parent_fd = -1
                            inner_self.created_identity = None
                            inner_self.cleanup_expected = None
                            inner_self.cleanup_container = None
                            inner_self.cleanup_result = "not-called"
                            inner_self.traceback_at_cleanup = None

                        def create_private_stage_parent(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            *,
                            authorize_state: Callable[[str], None],
                        ) -> Any:
                            result = super().create_private_stage_parent(
                                parent_fd,
                                name,
                                authorize_state=authorize_state,
                            )
                            inner_self.parent_fd = parent_fd
                            inner_self.created_fd = result[0]
                            inner_self.created_identity = result[1]
                            return result

                        def _require_stage_container_mapping(
                            inner_self,
                            stage_path: str,
                            held_fd: int,
                            expected: Any,
                        ) -> None:
                            del stage_path, held_fd, expected
                            raise primary

                        def _cleanup_unopened_stage(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            expected: Any,
                            expected_container: Any,
                            *,
                            authorize_state: Callable[[str], None],
                        ) -> Optional[str]:
                            inner_self.traceback_at_cleanup = primary.__traceback__
                            inner_self.cleanup_expected = expected
                            inner_self.cleanup_container = expected_container
                            result = super()._cleanup_unopened_stage(
                                parent_fd,
                                name,
                                expected,
                                expected_container,
                                authorize_state=authorize_state,
                            )
                            inner_self.cleanup_result = result
                            return result

                    backend = BindingFailureBackend()
                    actions = []

                    def authorize(action: str) -> None:
                        actions.append(action)
                        if cleanup_fails and action == "remove_stage":
                            raise cleanup_error

                    original_args = primary.args
                    original_code = getattr(primary, "code", None)
                    actual = None
                    try:
                        backend.create_private_stage(
                            str(stage),
                            authorize_state=authorize,
                        )
                    except type(primary) as escaped:
                        actual = escaped
                    except BaseException as escaped:
                        self.fail(
                            "absolute binding cleanup replaced primary with "
                            f"{type(escaped).__name__}"
                        )
                    else:
                        self.fail("absolute binding failure unexpectedly succeeded")

                    self.assertIs(actual, primary)
                    self.assertEqual(actual.args, original_args)
                    if isinstance(primary, SystemExit):
                        self.assertEqual(actual.code, original_code)
                    self.assertEqual(actions, ["create_stage", "remove_stage"])
                    self.assertIs(
                        backend.cleanup_expected,
                        backend.created_identity,
                    )
                    self.assertIsNotNone(backend.cleanup_container)
                    self.assertIsNotNone(backend.traceback_at_cleanup)
                    traceback_cursor = actual.__traceback__
                    preserved_traceback = False
                    while traceback_cursor is not None:
                        if traceback_cursor is backend.traceback_at_cleanup:
                            preserved_traceback = True
                            break
                        traceback_cursor = traceback_cursor.tb_next
                    self.assertTrue(preserved_traceback)
                    for fd in (backend.created_fd, backend.parent_fd):
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)

                    if not cleanup_fails:
                        self.assertIsNone(backend.cleanup_result)
                        self.assertFalse(stage.exists())
                        self.assertEqual(
                            getattr(primary, "cleanup_diagnostics", ()),
                            (),
                        )
                        continue

                    self.assertIsInstance(backend.cleanup_result, str)
                    self.assertIn(cleanup_marker, backend.cleanup_result)
                    self.assertTrue(stage.is_dir())
                    stage_info = stage.stat()
                    self.assertEqual(
                        (stage_info.st_dev, stage_info.st_ino),
                        (
                            backend.created_identity.dev,
                            backend.created_identity.ino,
                        ),
                    )
                    self.assertEqual(stat.S_IMODE(stage_info.st_mode), 0o700)
                    self.assertEqual(stage_info.st_uid, os.geteuid())
                    self.assertEqual(list(stage.iterdir()), [])
                    diagnostic = getattr(primary, "cleanup_diagnostic", "")
                    self.assertIn("identity-bound cleanup failed", diagnostic)
                    self.assertIn(
                        "absolute stage binding cleanup failed",
                        diagnostic,
                    )
                    self.assertIn(cleanup_marker, diagnostic)
                    self.assertIn(self.helper._TRUNCATED_MARKER, diagnostic)
                    self.assertLessEqual(
                        len(diagnostic.encode("utf-8")),
                        self.helper._DIAGNOSTIC_LIMIT,
                    )

    def test_unopened_stage_cleanup_does_not_remove_authorizer_replacement(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        stage = self.destination_parent / "unopened-stage"
        held = self.destination_parent / "held-unopened-stage"
        sentinel = stage / "sentinel"
        stage.mkdir(mode=0o700)
        parent_fd, name = backend.open_absolute_parent(str(stage))
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            expected = backend.identity(stage_fd)
        finally:
            os.close(stage_fd)
        expected_container = backend.validate_stage_container(parent_fd)
        injected = False

        def replace_before_remove(action: str) -> None:
            nonlocal injected
            if action != "remove_stage" or injected:
                return
            injected = True
            os.replace(stage, held)
            stage.mkdir(mode=0o700)
            sentinel.write_bytes(b"replacement\n")

        try:
            cleanup_error = backend._cleanup_unopened_stage(
                parent_fd,
                name,
                expected,
                expected_container,
                authorize_state=replace_before_remove,
            )
        finally:
            os.close(parent_fd)

        self.assertTrue(injected)
        self.assertIsInstance(cleanup_error, str)
        self.assertIn("changed after cleanup authorization", cleanup_error)
        self.assertEqual(sentinel.read_bytes(), b"replacement\n")
        self.assertTrue(held.is_dir())

    def test_unopened_stage_cleanup_authorizer_error_is_returned_and_preserved(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        stage = self.destination_parent / "unopened-authorizer-error-stage"
        stage.mkdir(mode=0o700)
        before = stage.stat()
        parent_fd, name = backend.open_absolute_parent(str(stage))
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            expected = backend.identity(stage_fd)
        finally:
            os.close(stage_fd)
        expected_container = backend.validate_stage_container(parent_fd)
        cleanup_authorizer_called = False

        def fail_cleanup_authorization(action: str) -> None:
            nonlocal cleanup_authorizer_called
            self.assertEqual(action, "remove_stage")
            cleanup_authorizer_called = True
            raise backend_module.BackendError(
                "injected_cleanup_authorizer",
                "injected unopened-stage cleanup authorization failure",
                errno.EACCES,
            )

        try:
            cleanup_error = backend._cleanup_unopened_stage(
                parent_fd,
                name,
                expected,
                expected_container,
                authorize_state=fail_cleanup_authorization,
            )
        finally:
            os.close(parent_fd)

        self.assertTrue(cleanup_authorizer_called)
        self.assertIsInstance(cleanup_error, str)
        self.assertIn("injected_cleanup_authorizer", cleanup_error)
        self.assertIn(
            "injected unopened-stage cleanup authorization failure",
            cleanup_error,
        )
        self.assertEqual(stage.stat().st_ino, before.st_ino)
        self.assertEqual(list(stage.iterdir()), [])

    def test_unopened_stage_cleanup_contains_baseexception_from_every_phase(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CleanupBomb(BaseException):
            pass

        cases = (
            ("pre_authorizer", KeyboardInterrupt("pre cleanup interrupt")),
            ("authorizer", SystemExit(88)),
            ("post_authorizer", CleanupBomb("post cleanup bomb")),
        )
        for phase, injected_error in cases:
            with self.subTest(phase=phase):
                backend = backend_module.DarwinBackend()
                stage = self.destination_parent / f"unopened-baseexception-{phase}"
                stage.mkdir(mode=0o700)
                before = stage.stat()
                parent_fd, name = backend.open_absolute_parent(str(stage))
                stage_fd = os.open(
                    stage,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                try:
                    expected = backend.identity(stage_fd)
                finally:
                    os.close(stage_fd)
                expected_container = backend.validate_stage_container(parent_fd)
                original_validate = backend.validate_stage_container
                authorized = False
                injected = False

                def validate(fd: int) -> Any:
                    nonlocal injected
                    should_fail = phase == "pre_authorizer" or (
                        phase == "post_authorizer" and authorized
                    )
                    if should_fail and not injected:
                        injected = True
                        raise injected_error
                    return original_validate(fd)

                def authorize(action: str) -> None:
                    nonlocal authorized, injected
                    self.assertEqual(action, "remove_stage")
                    authorized = True
                    if phase == "authorizer":
                        injected = True
                        raise injected_error

                try:
                    with mock.patch.object(
                        backend,
                        "validate_stage_container",
                        side_effect=validate,
                    ):
                        try:
                            cleanup_error = backend._cleanup_unopened_stage(
                                parent_fd,
                                name,
                                expected,
                                expected_container,
                                authorize_state=authorize,
                            )
                        except BaseException as escaped:
                            self.fail(
                                "unopened-stage cleanup leaked "
                                f"{type(escaped).__name__}: {escaped}"
                            )
                finally:
                    os.close(parent_fd)

                self.assertTrue(injected)
                self.assertIsInstance(cleanup_error, str)
                self.assertIn(type(injected_error).__name__, cleanup_error)
                self.assertEqual(stage.stat().st_ino, before.st_ino)
                self.assertEqual(list(stage.iterdir()), [])

    def test_remove_empty_stage_does_not_remove_authorizer_replacement(self) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        stage = self.destination_parent / "recorded-empty-stage"
        held = self.destination_parent / "held-recorded-empty-stage"
        sentinel = stage / "sentinel"
        stage.mkdir(mode=0o700)
        parent_fd, _name = backend.open_absolute_parent(str(stage))
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            expected = backend.identity(stage_fd)
        finally:
            os.close(stage_fd)
        expected_container = backend.validate_stage_container(parent_fd)
        os.close(parent_fd)
        injected = False

        def replace_before_remove(action: str) -> None:
            nonlocal injected
            if action != "remove_stage" or injected:
                return
            injected = True
            os.replace(stage, held)
            stage.mkdir(mode=0o700)
            sentinel.write_bytes(b"replacement\n")

        with self.assertRaises(backend_module.BackendError):
            backend.remove_empty_private_stage(
                str(stage),
                expected,
                expected_container=expected_container,
                authorize_state=replace_before_remove,
            )

        self.assertTrue(injected)
        self.assertEqual(sentinel.read_bytes(), b"replacement\n")
        self.assertTrue(held.is_dir())

    def test_backend_close_fds_drains_baseexceptions_and_preserves_contract(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CloseBomb(BaseException):
            pass

        descriptors = (("first", 501), ("second", 502), ("third", 503))

        def exercise(
            *,
            primary: Optional[BaseException],
            durable: bool,
        ) -> tuple[list[int], Optional[Any]]:
            calls = []

            def close(fd: int) -> None:
                calls.append(fd)
                if fd == 501:
                    raise KeyboardInterrupt("first close interrupt")
                if fd == 502:
                    raise CloseBomb("second close bomb")

            caught = None
            with mock.patch.object(backend_module.os, "close", side_effect=close):
                if primary is None and not durable:
                    with self.assertRaises(backend_module.BackendError) as context:
                        backend_module.DarwinBackend._close_fds(
                            descriptors,
                            durable_namespace_complete=durable,
                        )
                    caught = context.exception
                else:
                    backend_module.DarwinBackend._close_fds(
                        descriptors,
                        primary_error=primary,
                        durable_namespace_complete=durable,
                    )
            return calls, caught

        primary = SystemExit(91)
        calls, caught = exercise(primary=primary, durable=False)
        self.assertEqual(calls, [501, 502, 503])
        self.assertIsNone(caught)
        self.assertEqual(primary.code, 91)

        calls, caught = exercise(primary=None, durable=False)
        self.assertEqual(calls, [501, 502, 503])
        self.assertIsInstance(caught, backend_module.BackendError)
        self.assertEqual(caught.reason, "close_failed")
        self.assertIn("KeyboardInterrupt", caught.detail)
        self.assertIn("CloseBomb", caught.detail)

        calls, caught = exercise(primary=None, durable=True)
        self.assertEqual(calls, [501, 502, 503])
        self.assertIsNone(caught)

    def test_helper_close_all_drains_baseexceptions_and_preserves_contract(
        self,
    ) -> None:
        class CloseBomb(BaseException):
            pass

        def exercise(
            *,
            primary: Optional[BaseException],
            stage_removed: bool,
        ) -> tuple[list[int], Optional[str], Any]:
            transaction = self.helper.MirrorSync(self.backend_module.DarwinBackend())
            transaction.candidate_fd = 601
            transaction.stage_fd = 602
            transaction.destination_fd = 603
            transaction.stage_removed = stage_removed
            calls = []

            def close(fd: int) -> None:
                calls.append(fd)
                if fd == 601:
                    raise KeyboardInterrupt("candidate close interrupt")
                if fd == 602:
                    raise CloseBomb("stage close bomb")

            with mock.patch.object(self.helper.os, "close", side_effect=close):
                result = transaction._close_all(primary_error=primary)
            return calls, result, transaction

        primary = KeyboardInterrupt("helper primary")
        calls, result, transaction = exercise(
            primary=primary,
            stage_removed=False,
        )
        self.assertEqual(calls, [601, 602, 603])
        self.assertIsInstance(result, str)
        self.assertIn("KeyboardInterrupt", result)
        self.assertIn("CloseBomb", result)
        self.assertEqual(primary.args, ("helper primary",))
        primary_cleanup = getattr(primary, "cleanup_diagnostic", "")
        self.assertIn("KeyboardInterrupt", primary_cleanup)
        self.assertIn("CloseBomb", primary_cleanup)
        self.assertTrue(
            any(
                "KeyboardInterrupt" in note and "CloseBomb" in note
                for note in getattr(primary, "__notes__", ())
            )
        )
        self.assertEqual(transaction.candidate_fd, -1)
        self.assertEqual(transaction.stage_fd, -1)
        self.assertEqual(transaction.destination_fd, -1)

        calls, result, _transaction = exercise(
            primary=None,
            stage_removed=False,
        )
        self.assertEqual(calls, [601, 602, 603])
        self.assertIsInstance(result, str)
        self.assertIn("KeyboardInterrupt", result)
        self.assertIn("CloseBomb", result)

        calls, result, _transaction = exercise(
            primary=None,
            stage_removed=True,
        )
        self.assertEqual(calls, [601, 602, 603])
        self.assertIsNone(result)

    def test_hostile_exception_type_name_cannot_stop_fd_or_acl_drain(self) -> None:
        backend_module = self.backend_module

        class NameLookupBomb(BaseException):
            pass

        class SecondaryDrainBomb(BaseException):
            pass

        def preserve_primary(
            primary: BaseException,
            cleanup: Callable[[BaseException], None],
        ) -> tuple[BaseException, Any]:
            actual = None
            traceback_at_cleanup = None
            try:
                raise primary
            except BaseException as caught:
                traceback_at_cleanup = caught.__traceback__
                cleanup(caught)
                try:
                    raise
                except BaseException as escaped:
                    actual = escaped
            return actual, traceback_at_cleanup

        for name_error in (
            KeyboardInterrupt("hostile type name interrupt"),
            NameLookupBomb("hostile type name custom BaseException"),
        ):

            class HostileNameMeta(type):
                def __getattribute__(cls, name: str) -> Any:
                    if name == "__name__":
                        raise type.__getattribute__(cls, "name_error")
                    return type.__getattribute__(cls, name)

            hostile_type = HostileNameMeta(
                "HostileCleanup",
                (BaseException,),
                {"name_error": name_error},
            )
            hostile_cleanup = hostile_type("hostile cleanup detail")

            backend_close_primary = backend_module.BackendError(
                "backend_close_primary",
                "backend close primary detail",
                errno.EIO,
            )
            backend_close_calls = []

            def backend_close(fd: int) -> None:
                backend_close_calls.append(fd)
                if fd == 901:
                    raise hostile_cleanup
                if fd == 902:
                    raise SecondaryDrainBomb("backend close secondary marker")

            def drain_backend_fds(primary: BaseException) -> None:
                with mock.patch.object(
                    backend_module.os,
                    "close",
                    side_effect=backend_close,
                ):
                    backend_module.DarwinBackend._close_fds(
                        (("first", 901), ("second", 902), ("third", 903)),
                        primary_error=primary,
                    )

            actual, original_traceback = preserve_primary(
                backend_close_primary,
                drain_backend_fds,
            )
            self.assertIs(actual, backend_close_primary)
            traceback_cursor = actual.__traceback__
            preserved_traceback = False
            while traceback_cursor is not None:
                if traceback_cursor is original_traceback:
                    preserved_traceback = True
                    break
                traceback_cursor = traceback_cursor.tb_next
            self.assertTrue(preserved_traceback)
            self.assertEqual(backend_close_calls, [901, 902, 903])
            backend_close_diagnostic = getattr(
                backend_close_primary,
                "cleanup_diagnostic",
                "",
            )
            self.assertIn("<unprintable-exception>", backend_close_diagnostic)
            self.assertIn("backend close secondary marker", backend_close_diagnostic)

            backend = backend_module.DarwinBackend()
            acl_primary = backend_module.BackendError(
                "acl_drain_primary",
                "ACL drain primary detail",
                errno.EIO,
            )
            acl_calls = []

            def free_acl(pointer: ctypes.c_void_p) -> int:
                acl_calls.append(pointer.value)
                if pointer.value == 911:
                    raise hostile_cleanup
                if pointer.value == 912:
                    raise SecondaryDrainBomb("ACL secondary marker")
                return 0

            def drain_acls(primary: BaseException) -> None:
                with mock.patch.object(
                    backend,
                    "_acl_free",
                    side_effect=free_acl,
                ):
                    backend._free_acls(
                        (
                            ("first ACL", ctypes.c_void_p(911)),
                            ("second ACL", ctypes.c_void_p(912)),
                            ("third ACL", ctypes.c_void_p(913)),
                        ),
                        primary_error=primary,
                    )

            actual, _acl_traceback = preserve_primary(acl_primary, drain_acls)
            self.assertIs(actual, acl_primary)
            self.assertEqual(acl_calls, [911, 912, 913])
            acl_diagnostic = getattr(acl_primary, "cleanup_diagnostic", "")
            self.assertIn("<unprintable-exception>", acl_diagnostic)
            self.assertIn("ACL secondary marker", acl_diagnostic)

            helper_primary = backend_module.BackendError(
                "helper_close_primary",
                "helper close primary detail",
                errno.EIO,
            )
            transaction = self.helper.MirrorSync(backend_module.DarwinBackend())
            transaction.candidate_fd = 921
            transaction.stage_fd = 922
            transaction.destination_fd = 923
            helper_close_calls = []
            helper_close_result = None

            def helper_close(fd: int) -> None:
                helper_close_calls.append(fd)
                if fd == 921:
                    raise hostile_cleanup
                if fd == 922:
                    raise SecondaryDrainBomb("helper close secondary marker")

            def drain_helper_fds(primary: BaseException) -> None:
                nonlocal helper_close_result
                with mock.patch.object(
                    self.helper.os,
                    "close",
                    side_effect=helper_close,
                ):
                    helper_close_result = transaction._close_all(primary_error=primary)

            actual, _helper_traceback = preserve_primary(
                helper_primary,
                drain_helper_fds,
            )
            self.assertIs(actual, helper_primary)
            self.assertEqual(helper_close_calls, [921, 922, 923])
            self.assertIsInstance(helper_close_result, str)
            self.assertIn("<unprintable-exception>", helper_close_result)
            self.assertIn("helper close secondary marker", helper_close_result)
            helper_diagnostic = getattr(
                helper_primary,
                "cleanup_diagnostic",
                "",
            )
            self.assertIn("<unprintable-exception>", helper_diagnostic)
            self.assertIn("helper close secondary marker", helper_diagnostic)
            self.assertTrue(
                all(
                    getattr(transaction, name) == -1
                    for name in (
                        "candidate_fd",
                        "stage_fd",
                        "destination_fd",
                    )
                )
            )

    def test_bound_transaction_primary_close_failures_preserve_and_reach_receipt(
        self,
    ) -> None:
        backend_module = self.backend_module

        class TransactionPrimary(BaseException):
            pass

        class TransactionCloseBomb(BaseException):
            pass

        first_marker = "transaction-close-marker-one"
        second_marker = "transaction-close-marker-two"

        def exercise(
            primary: BaseException,
            invoke: Callable[[], None],
            transaction_getter: Callable[[], Any],
            expected_fds: list[int],
        ) -> None:
            close_calls = []
            traceback_at_cleanup = None

            def fail_closes(fd: int) -> None:
                nonlocal traceback_at_cleanup
                close_calls.append(fd)
                if traceback_at_cleanup is None:
                    traceback_at_cleanup = primary.__traceback__
                if fd == expected_fds[0]:
                    raise OSError(first_marker)
                if fd == expected_fds[2]:
                    raise TransactionCloseBomb(second_marker)

            original_args = primary.args
            original_code = getattr(primary, "code", None)
            actual = None
            with mock.patch.object(
                backend_module.os,
                "close",
                side_effect=fail_closes,
            ):
                try:
                    invoke()
                except type(primary) as escaped:
                    actual = escaped
                except BaseException as escaped:
                    self.fail(
                        "transaction close replaced primary with "
                        f"{type(escaped).__name__}"
                    )
                else:
                    self.fail("transaction primary unexpectedly disappeared")

            self.assertIs(actual, primary)
            self.assertEqual(actual.args, original_args)
            if isinstance(primary, SystemExit):
                self.assertEqual(actual.code, original_code)
            self.assertEqual(close_calls, expected_fds)
            self.assertIsNotNone(traceback_at_cleanup)
            traceback_cursor = actual.__traceback__
            preserved_traceback = False
            while traceback_cursor is not None:
                if traceback_cursor is traceback_at_cleanup:
                    preserved_traceback = True
                    break
                traceback_cursor = traceback_cursor.tb_next
            self.assertTrue(preserved_traceback)
            transaction = transaction_getter()
            self.assertIsNotNone(transaction)
            self.assertTrue(transaction._closed)
            self.assertTrue(
                all(
                    getattr(transaction, name) == -1
                    for name in (
                        "clone_fd",
                        "original_fd",
                        "source_fd",
                        "temporary_parent_fd",
                        "destination_parent_fd",
                        "source_parent_fd",
                    )
                )
            )
            diagnostic = getattr(primary, "cleanup_diagnostic", "")
            self.assertIn("close_failed", diagnostic)
            self.assertIn(first_marker, diagnostic)
            self.assertIn(second_marker, diagnostic)
            self.assertLessEqual(
                len(diagnostic.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            outer = backend_module.BackendError(
                "transaction_outer",
                "transaction outer detail",
                errno.EIO,
            )
            outer.__cause__ = primary
            cause_parts = self.helper._cleanup_diagnostic_parts(outer)
            self.assertTrue(any(first_marker in part for part in cause_parts))
            self.assertTrue(any(second_marker in part for part in cause_parts))

        regular = stat.S_IFREG | 0o600
        directory = stat.S_IFDIR | 0o700

        def identity(ino: int, *, mode: int = regular) -> Any:
            return backend_module.FileIdentity(
                1,
                ino,
                mode,
                1,
                10,
                os.geteuid(),
                os.getegid(),
                1_700_000_000_000_000_000,
                1_700_000_000_000_000_000,
            )

        bind_primary = KeyboardInterrupt("transaction bind primary")
        bind_backend = backend_module.DarwinBackend()
        bind_transaction = backend_module.BoundTransaction.__new__(
            backend_module.BoundTransaction
        )
        bind_transaction._initialize(
            bind_backend,
            "/synthetic/source",
            "/synthetic/destination",
            "/synthetic/stage/temporary",
        )
        source_parent = identity(101, mode=directory)
        destination_parent = identity(102, mode=directory)
        temporary_parent = identity(103, mode=directory)
        source_identity = identity(104)
        original_identity = identity(105)

        def invoke_bind() -> None:
            with mock.patch.object(
                bind_transaction,
                "_open_source_parent",
                return_value=(101, "source"),
            ):
                with mock.patch.object(
                    bind_backend,
                    "open_absolute_parent",
                    side_effect=((102, "destination"), (103, "temporary")),
                ):
                    with mock.patch.object(
                        bind_transaction,
                        "_source_parent_identity",
                        return_value=source_parent,
                    ):
                        with mock.patch.object(
                            bind_backend,
                            "validate_stage_container",
                            return_value=destination_parent,
                        ):
                            with mock.patch.object(
                                bind_backend,
                                "validate_private_stage_parent",
                                return_value=temporary_parent,
                            ):
                                with mock.patch.object(
                                    bind_transaction,
                                    "_open_source_leaf",
                                    return_value=104,
                                ):
                                    with mock.patch.object(
                                        bind_transaction,
                                        "_open_bound_leaf",
                                        return_value=105,
                                    ):
                                        with mock.patch.object(
                                            bind_backend,
                                            "identity",
                                            side_effect=(
                                                source_identity,
                                                original_identity,
                                            ),
                                        ):
                                            with mock.patch.object(
                                                bind_backend,
                                                "require_exclusive_writer",
                                                side_effect=bind_primary,
                                            ):
                                                bind_transaction._bind(source_parent)

        exercise(
            bind_primary,
            invoke_bind,
            lambda: bind_transaction,
            [105, 104, 103, 102, 101],
        )

        recover_primary = SystemExit(79)

        class RecoveryFailureTransaction(backend_module.BoundTransaction):
            captured = None

            def _bind_recovery(inner_self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs
                inner_self.source_parent_fd = 201
                inner_self.destination_parent_fd = 202
                inner_self.temporary_parent_fd = 203
                inner_self.source_fd = 204
                inner_self.original_fd = 205
                inner_self.clone_fd = 206
                RecoveryFailureTransaction.captured = inner_self
                raise recover_primary

        recover_backend = backend_module.DarwinBackend()

        def invoke_recover() -> None:
            RecoveryFailureTransaction.recover(
                recover_backend,
                None,
                "/synthetic/destination",
                "/synthetic/stage/temporary",
                identity(205),
                identity(206),
            )

        exercise(
            recover_primary,
            invoke_recover,
            lambda: RecoveryFailureTransaction.captured,
            [206, 205, 204, 203, 202, 201],
        )

        body_primary = TransactionPrimary("transaction context body primary")
        context_backend = backend_module.DarwinBackend()
        context_transaction = backend_module.BoundTransaction.__new__(
            backend_module.BoundTransaction
        )
        context_transaction._initialize(
            context_backend,
            None,
            "/synthetic/destination",
            "/synthetic/stage/temporary",
        )
        context_transaction.source_parent_fd = 301
        context_transaction.destination_parent_fd = 302
        context_transaction.temporary_parent_fd = 303
        context_transaction.source_fd = 304
        context_transaction.original_fd = 305
        context_transaction.clone_fd = 306

        def invoke_context() -> None:
            with context_transaction:
                raise body_primary

        exercise(
            body_primary,
            invoke_context,
            lambda: context_transaction,
            [306, 305, 304, 303, 302, 301],
        )

        receipt_outer = backend_module.BackendError(
            "transaction_receipt_outer",
            "transaction receipt outer detail",
            errno.EIO,
        )
        receipt_outer.__cause__ = body_primary

        class TransactionCauseBackend(backend_module.DarwinBackend):
            def snapshot_policy(inner_self, fd: int) -> Any:
                del fd
                raise receipt_outer

        self.write_source(b"transaction-cleanup-receipt\n")
        before_destination = self.write_destination()
        receipt_transaction = self.helper.MirrorSync(TransactionCauseBackend())
        exit_status, receipt, raw, stderr = self.run_transaction_via_main(
            receipt_transaction
        )
        self.assertEqual(exit_status, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], receipt_outer.reason)
        self.assertIn(first_marker, receipt["detail"])
        self.assertIn(second_marker, receipt["detail"])
        self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")

    def test_bound_transaction_close_without_primary_still_reports_failure(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        transaction = backend_module.BoundTransaction.__new__(
            backend_module.BoundTransaction
        )
        transaction._initialize(
            backend,
            None,
            "/synthetic/destination",
            "/synthetic/stage/temporary",
        )
        transaction.source_parent_fd = 401
        transaction.destination_parent_fd = 402
        transaction.temporary_parent_fd = 403
        transaction.source_fd = 404
        transaction.original_fd = 405
        transaction.clone_fd = 406
        close_calls = []

        def fail_closes(fd: int) -> None:
            close_calls.append(fd)
            if fd in (406, 403):
                raise OSError(f"no-primary-close-marker-{fd}")

        with mock.patch.object(
            backend_module.os,
            "close",
            side_effect=fail_closes,
        ):
            with self.assertRaises(backend_module.BackendError) as caught:
                transaction.close()

        self.assertEqual(caught.exception.reason, "close_failed")
        self.assertIn("no-primary-close-marker-406", caught.exception.detail)
        self.assertIn("no-primary-close-marker-403", caught.exception.detail)
        self.assertEqual(close_calls, [406, 405, 404, 403, 402, 401])
        self.assertTrue(transaction._closed)
        self.assertTrue(
            all(
                getattr(transaction, name) == -1
                for name in (
                    "clone_fd",
                    "original_fd",
                    "source_fd",
                    "temporary_parent_fd",
                    "destination_parent_fd",
                    "source_parent_fd",
                )
            )
        )

    def test_helper_receipt_includes_attached_backend_cleanup_diagnostic(
        self,
    ) -> None:
        backend_module = self.backend_module

        class DiagnosticBackend(backend_module.DarwinBackend):
            def snapshot_policy(inner_self, fd: int) -> Any:
                del fd
                error = backend_module.BackendError(
                    "injected_primary",
                    "injected primary detail",
                    errno.EIO,
                )
                error.cleanup_diagnostic = (
                    "identity-bound cleanup failed (injected cleanup detail)"
                )
                raise error

        self.write_source(b"diagnostic\n")
        before = self.write_destination()

        receipt = self.sync(backend_factory=DiagnosticBackend)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "injected_primary")
        self.assertIn("injected primary detail", receipt["detail"])
        self.assertIn("injected cleanup detail", receipt["detail"])
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        validated = self.validate_receipt(receipt, 2)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_generic_stage_setup_cleanup_diagnostic_is_bounded_in_receipt(
        self,
    ) -> None:
        backend_module = self.backend_module
        primary = RuntimeError("injected generic stage setup primary")
        cleanup_marker = "injected generic setup cleanup marker: "
        cleanup_detail = cleanup_marker + ("x" * (self.helper._RECEIPT_LIMIT * 2))

        class GenericSetupFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fail_next_fsync = True

            def fsync(inner_self, fd: int) -> None:
                if inner_self.fail_next_fsync:
                    inner_self.fail_next_fsync = False
                    raise primary
                super().fsync(fd)

        self.write_source(b"generic-setup-primary\n")
        before_destination = self.write_destination()
        authorize_calls = 0

        def fail_cleanup_authorization(action: str) -> None:
            nonlocal authorize_calls
            if action != "authorize_create_stage":
                return
            authorize_calls += 1
            if authorize_calls == 2:
                raise RuntimeError(cleanup_detail)

        receipt = self.sync(
            action_hook=fail_cleanup_authorization,
            backend_factory=GenericSetupFailureBackend,
        )

        self.assertEqual(authorize_calls, 2)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "unexpected_error")
        self.assertIn("injected generic stage setup primary", receipt["detail"])
        self.assertIn(cleanup_marker, receipt["detail"])
        self.assertTrue(receipt["detail"].endswith(self.helper._TRUNCATED_MARKER))
        self.assertEqual(receipt["detail"].count(self.helper._TRUNCATED_MARKER), 1)
        self.assertEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        encoded = json.dumps(receipt, sort_keys=True).encode("utf-8")
        self.assertLessEqual(len(encoded), self.helper._RECEIPT_LIMIT)
        validated = self.validate_receipt(receipt, 2, raw_input=encoded)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        retained_stages = [
            child
            for child in self.destination_parent.iterdir()
            if child.name.startswith(f".{ROLLOUT_NAME}.codex-stage.")
        ]
        self.assertEqual(len(retained_stages), 1)
        self.assertEqual(list(retained_stages[0].iterdir()), [])

    def test_deferred_with_attached_cleanup_diagnostic_is_fatal(self) -> None:
        transaction = self.helper.MirrorSync(self.backend_module.DarwinBackend())
        primary = self.helper.DeferredSync(
            "source_unstable",
            "injected deferred primary",
        )
        primary.cleanup_diagnostic = "injected deferred cleanup diagnostic"

        with mock.patch.object(transaction, "_execute", side_effect=primary):
            receipt = transaction.sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            ).to_dict()

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "stage_cleanup_failed")
        self.assertIn("injected deferred primary", receipt["detail"])
        self.assertIn("injected deferred cleanup diagnostic", receipt["detail"])
        validated = self.validate_receipt(receipt, 2)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_top_level_primary_survives_baseexception_stage_cleanup(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CleanupBomb(BaseException):
            pass

        cleanup_errors = (
            KeyboardInterrupt("cleanup keyboard interrupt"),
            SystemExit(87),
            CleanupBomb("cleanup custom baseexception"),
        )
        for index, cleanup_error in enumerate(cleanup_errors):
            with self.subTest(cleanup=type(cleanup_error).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"top-level-primary-{index}\n".encode())
                before_destination = self.write_destination()
                primary = backend_module.BackendError(
                    "injected_top_level_primary",
                    "injected top-level primary detail",
                    errno.EIO,
                )
                original_args = primary.args
                owned_fds = []
                close_calls = []
                close_call_start = 0
                traceback_at_cleanup = None
                before_cleanup_calls = 0
                transaction: Any = None

                def inject(action: str) -> None:
                    nonlocal close_call_start, traceback_at_cleanup
                    nonlocal before_cleanup_calls, owned_fds
                    if action == "after_clone":
                        owned_fds = [
                            transaction.candidate_fd,
                            transaction.stage_fd,
                            transaction.destination_fd,
                            transaction.destination_parent_fd,
                            transaction.source_fd,
                            transaction.source_parent_fd,
                        ]
                        self.assertTrue(all(fd >= 0 for fd in owned_fds))
                        close_call_start = len(close_calls)
                        raise primary
                    if action == "before_cleanup":
                        before_cleanup_calls += 1
                        traceback_at_cleanup = primary.__traceback__
                        raise cleanup_error

                transaction = self.helper.MirrorSync(
                    backend_module.DarwinBackend(),
                    action_hook=inject,
                )
                real_close = os.close

                def record_close(fd: int) -> None:
                    close_calls.append(fd)
                    real_close(fd)

                with mock.patch.object(
                    self.helper.os,
                    "close",
                    side_effect=record_close,
                ):
                    receipt = transaction.sync_one(
                        str(self.source.absolute()),
                        str(self.destination.absolute()),
                    ).to_dict()

                self.assertEqual(before_cleanup_calls, 1)
                self.assertIsNotNone(traceback_at_cleanup)
                traceback_cursor = primary.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is traceback_at_cleanup:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)
                self.assertEqual(primary.args, original_args)
                cleanup_diagnostic = getattr(primary, "cleanup_diagnostic", "")
                self.assertIn(type(cleanup_error).__name__, cleanup_diagnostic)
                self.assertIn(str(cleanup_error), cleanup_diagnostic)
                self.assertTrue(
                    any(
                        type(cleanup_error).__name__ in note
                        and str(cleanup_error) in note
                        for note in getattr(primary, "__notes__", ())
                    )
                )
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], primary.reason)
                self.assertIn(primary.detail, receipt["detail"])
                self.assertIn(type(cleanup_error).__name__, receipt["detail"])
                self.assertIn(str(cleanup_error), receipt["detail"])
                validated = self.validate_receipt(receipt, 2)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(
                    close_calls[close_call_start:],
                    owned_fds,
                )
                self.assertTrue(
                    all(
                        getattr(transaction, name) == -1
                        for name in (
                            "candidate_fd",
                            "stage_fd",
                            "destination_fd",
                            "destination_parent_fd",
                            "source_fd",
                            "source_parent_fd",
                        )
                    )
                )
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                retained_stage = pathlib.Path(transaction.stage_path)
                self.assertTrue(retained_stage.is_dir())
                self.assertEqual(
                    [child.name for child in retained_stage.iterdir()],
                    ["candidate.jsonl"],
                )

    def test_final_close_failure_updates_fatal_receipt_and_drains_all_fds(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CloseBomb(BaseException):
            pass

        self.write_source(b"final-close-primary\n")
        before_destination = self.write_destination()
        primary = backend_module.BackendError(
            "injected_final_close_primary",
            "injected final close primary detail",
            errno.EIO,
        )
        original_args = primary.args
        cleanup_marker = "injected final close cleanup marker: "
        close_error = CloseBomb(
            cleanup_marker + ("z" * (self.helper._DIAGNOSTIC_LIMIT * 2))
        )
        transaction: Any = None
        final_fds = []
        failing_fd = -1
        close_calls = []
        operation_close_start = 0
        traceback_at_close = None
        injected = False

        def fail_operation(action: str) -> None:
            nonlocal failing_fd, final_fds, operation_close_start
            if action != "after_clone":
                return
            final_fds = [
                transaction.stage_fd,
                transaction.destination_fd,
                transaction.destination_parent_fd,
                transaction.source_fd,
                transaction.source_parent_fd,
            ]
            self.assertTrue(all(fd >= 0 for fd in final_fds))
            failing_fd = transaction.destination_fd
            operation_close_start = len(close_calls)
            raise primary

        transaction = self.helper.MirrorSync(
            backend_module.DarwinBackend(),
            action_hook=fail_operation,
        )
        real_close = os.close

        def record_close(fd: int) -> None:
            nonlocal injected, traceback_at_close
            close_calls.append(fd)
            real_close(fd)
            if fd == failing_fd and not injected:
                injected = True
                traceback_at_close = primary.__traceback__
                raise close_error

        def injected_sync(
            source: str,
            destination: str,
            *,
            action_hook: Optional[Callable[[str], None]] = None,
        ) -> Any:
            self.assertIsNone(action_hook)
            with mock.patch.object(
                self.helper.os,
                "close",
                side_effect=record_close,
            ):
                return transaction.sync_one(source, destination)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(self.helper, "sync_one", side_effect=injected_sync):
            with mock.patch.object(self.helper, "_cli_hook", return_value=None):
                with mock.patch.object(self.helper.sys, "stdout", stdout):
                    with mock.patch.object(self.helper.sys, "stderr", stderr):
                        exit_status = self.helper.main(
                            (
                                "sync-one",
                                "--source",
                                str(self.source.absolute()),
                                "--destination",
                                str(self.destination.absolute()),
                                "--json",
                            )
                        )

        self.assertEqual(exit_status, 2)
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        receipt = json.loads(lines[0])
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertIn(primary.detail, receipt["detail"])
        self.assertIn(type(close_error).__name__, receipt["detail"])
        self.assertIn(cleanup_marker, receipt["detail"])
        self.assertIn(self.helper._TRUNCATED_MARKER, receipt["detail"])
        self.assertLessEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        validated = self.validate_receipt(
            receipt,
            2,
            raw_input=lines[0].encode("utf-8"),
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)

        self.assertTrue(injected)
        self.assertIsNotNone(traceback_at_close)
        traceback_cursor = primary.__traceback__
        preserved_traceback = False
        while traceback_cursor is not None:
            if traceback_cursor is traceback_at_close:
                preserved_traceback = True
                break
            traceback_cursor = traceback_cursor.tb_next
        self.assertTrue(preserved_traceback)
        self.assertEqual(primary.args, original_args)
        primary_cleanup = getattr(primary, "cleanup_diagnostic", "")
        self.assertIn(type(close_error).__name__, primary_cleanup)
        self.assertIn(cleanup_marker, primary_cleanup)
        self.assertTrue(primary_cleanup.endswith(self.helper._TRUNCATED_MARKER))
        self.assertTrue(
            any(
                type(close_error).__name__ in note and cleanup_marker in note
                for note in getattr(primary, "__notes__", ())
            )
        )
        self.assertEqual(close_calls[-len(final_fds) :], final_fds)
        self.assertEqual(close_calls[operation_close_start:].count(failing_fd), 1)
        self.assertTrue(
            all(
                getattr(transaction, name) == -1
                for name in (
                    "candidate_fd",
                    "stage_fd",
                    "destination_fd",
                    "destination_parent_fd",
                    "source_fd",
                    "source_parent_fd",
                )
            )
        )
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_raw_baseexception_survives_stage_and_final_close_failures(
        self,
    ) -> None:
        class RawPrimary(BaseException):
            pass

        class CleanupBomb(BaseException):
            pass

        class CloseBomb(BaseException):
            pass

        primary_errors = (
            KeyboardInterrupt("raw keyboard primary"),
            SystemExit(89),
            RawPrimary("raw custom primary"),
        )
        for index, primary in enumerate(primary_errors):
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"raw-primary-{index}\n".encode())
                before_destination = self.write_destination()
                cleanup_error = CleanupBomb(
                    f"identity cleanup after {type(primary).__name__}"
                )
                close_error = CloseBomb(f"final close after {type(primary).__name__}")
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                transaction: Any = None
                owned_fds = []
                failing_fd = -1
                close_calls = []
                operation_close_start = 0
                traceback_at_cleanup = None
                close_injected = False
                before_cleanup_calls = 0

                def inject(action: str) -> None:
                    nonlocal failing_fd, operation_close_start
                    nonlocal traceback_at_cleanup, before_cleanup_calls
                    nonlocal owned_fds
                    if action == "after_clone":
                        owned_fds = [
                            transaction.candidate_fd,
                            transaction.stage_fd,
                            transaction.destination_fd,
                            transaction.destination_parent_fd,
                            transaction.source_fd,
                            transaction.source_parent_fd,
                        ]
                        self.assertTrue(all(fd >= 0 for fd in owned_fds))
                        failing_fd = transaction.destination_fd
                        operation_close_start = len(close_calls)
                        raise primary
                    if action == "before_cleanup":
                        before_cleanup_calls += 1
                        traceback_at_cleanup = primary.__traceback__
                        raise cleanup_error

                transaction = self.helper.MirrorSync(
                    self.backend_module.DarwinBackend(),
                    action_hook=inject,
                )
                real_close = os.close

                def record_close(fd: int) -> None:
                    nonlocal close_injected
                    close_calls.append(fd)
                    real_close(fd)
                    if fd == failing_fd and not close_injected:
                        close_injected = True
                        raise close_error

                actual = None
                with mock.patch.object(
                    self.helper.os,
                    "close",
                    side_effect=record_close,
                ):
                    try:
                        transaction.sync_one(
                            str(self.source.absolute()),
                            str(self.destination.absolute()),
                        )
                    except type(primary) as escaped:
                        actual = escaped
                    except BaseException as escaped:
                        self.fail(
                            "cleanup replaced raw primary with "
                            f"{type(escaped).__name__}: {escaped}"
                        )
                    else:
                        self.fail("raw BaseException unexpectedly became a receipt")

                self.assertIs(actual, primary)
                self.assertEqual(actual.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(actual.code, original_code)
                self.assertEqual(before_cleanup_calls, 1)
                self.assertIsNotNone(traceback_at_cleanup)
                traceback_cursor = actual.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is traceback_at_cleanup:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)
                diagnostic = getattr(actual, "cleanup_diagnostic", "")
                self.assertIn(type(cleanup_error).__name__, diagnostic)
                self.assertIn(str(cleanup_error), diagnostic)
                self.assertIn(type(close_error).__name__, diagnostic)
                self.assertIn(str(close_error), diagnostic)
                self.assertLessEqual(
                    len(diagnostic.encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                notes = getattr(actual, "__notes__", ())
                self.assertTrue(
                    any(type(cleanup_error).__name__ in note for note in notes)
                )
                self.assertTrue(
                    any(type(close_error).__name__ in note for note in notes)
                )
                self.assertTrue(close_injected)
                self.assertEqual(
                    close_calls[operation_close_start:],
                    owned_fds,
                )
                self.assertTrue(
                    all(
                        getattr(transaction, name) == -1
                        for name in (
                            "candidate_fd",
                            "stage_fd",
                            "destination_fd",
                            "destination_parent_fd",
                            "source_fd",
                            "source_parent_fd",
                        )
                    )
                )
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                retained_stage = pathlib.Path(transaction.stage_path)
                self.assertTrue(retained_stage.is_dir())
                self.assertEqual(
                    [child.name for child in retained_stage.iterdir()],
                    ["candidate.jsonl"],
                )

    def test_bounded_receipt_combines_primary_acl_stage_and_fd_diagnostics(
        self,
    ) -> None:
        backend_module = self.backend_module

        class AclCleanupBomb(BaseException):
            pass

        class StageCleanupBomb(BaseException):
            pass

        class FdCleanupBomb(BaseException):
            pass

        primary_marker = "primary-bounded-combine-marker"
        acl_marker = "acl-bounded-combine-marker"
        stage_marker = "stage-bounded-combine-marker"
        fd_marker = "fd-bounded-combine-marker"
        primary_detail = primary_marker + (
            "p" * (self.helper._DIAGNOSTIC_LIMIT - len(primary_marker))
        )
        primary = backend_module.BackendError(
            "injected_bounded_combine_primary",
            primary_detail,
            errno.EIO,
        )
        acl_error = AclCleanupBomb(
            acl_marker + ("a" * (self.helper._DIAGNOSTIC_LIMIT * 2))
        )
        stage_error = StageCleanupBomb(stage_marker)
        fd_error = FdCleanupBomb(fd_marker)
        backend = backend_module.DarwinBackend()
        transaction: Any = None
        failing_fd = -1
        fd_failure_injected = False
        acl_failure_injected = False
        acl_failure_armed = False
        stage_failure_injected = False

        self.write_source(b"bounded-combine\n")
        before_destination = self.write_destination()

        def inject(action: str) -> None:
            nonlocal failing_fd, acl_failure_armed, stage_failure_injected
            if action == "after_clone":
                failing_fd = transaction.destination_fd
                try:
                    raise primary
                except BaseException as exc:
                    acl_failure_armed = True
                    backend._free_acls(
                        (("injected ACL finalizer", ctypes.c_void_p(505)),),
                        primary_error=exc,
                    )
                    raise
            if action == "before_cleanup":
                stage_failure_injected = True
                raise stage_error

        transaction = self.helper.MirrorSync(backend, action_hook=inject)
        real_close = os.close
        real_acl_free = backend._acl_free

        def fail_armed_acl_free(pointer: ctypes.c_void_p) -> int:
            nonlocal acl_failure_armed, acl_failure_injected
            if acl_failure_armed and not acl_failure_injected:
                acl_failure_armed = False
                acl_failure_injected = True
                raise acl_error
            return real_acl_free(pointer)

        def record_close(fd: int) -> None:
            nonlocal fd_failure_injected
            real_close(fd)
            if fd == failing_fd and not fd_failure_injected:
                fd_failure_injected = True
                raise fd_error

        with mock.patch.object(
            backend,
            "_acl_free",
            side_effect=fail_armed_acl_free,
        ):
            with mock.patch.object(
                self.helper.os,
                "close",
                side_effect=record_close,
            ):
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()

        self.assertTrue(acl_failure_injected)
        self.assertTrue(stage_failure_injected)
        self.assertTrue(fd_failure_injected)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        for marker in (primary_marker, acl_marker, stage_marker, fd_marker):
            self.assertIn(marker, receipt["detail"])
        self.assertIn(self.helper._TRUNCATED_MARKER, receipt["detail"])
        self.assertLessEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        encoded = json.dumps(receipt, sort_keys=True).encode("utf-8")
        self.assertLessEqual(len(encoded), self.helper._RECEIPT_LIMIT)
        validated = self.validate_receipt(receipt, 2, raw_input=encoded)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        retained_stage = pathlib.Path(transaction.stage_path)
        self.assertTrue(retained_stage.is_dir())
        self.assertEqual(
            [child.name for child in retained_stage.iterdir()],
            ["candidate.jsonl"],
        )

    def test_cleanup_segment_capacity_keeps_primary_and_latest_categories(
        self,
    ) -> None:
        backend_module = self.backend_module
        primary_marker = "capacity-primary-marker"
        cleanup_markers = [
            f"capacity-cleanup-category-{index}-marker" for index in range(8)
        ]
        for attacher in ("helper", "backend"):
            observed_details = []
            for run in range(2):
                with self.subTest(attacher=attacher, run=run):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    self.write_source(f"capacity-{attacher}-{run}\n".encode())
                    before_destination = self.write_destination()
                    primary = backend_module.BackendError(
                        "capacity_primary",
                        primary_marker + ("p" * (self.helper._DIAGNOSTIC_LIMIT * 2)),
                        errno.EIO,
                    )
                    backend = backend_module.DarwinBackend()

                    def inject(action: str) -> None:
                        if action != "after_clone":
                            return
                        for marker in cleanup_markers:
                            if attacher == "helper":
                                self.helper._attach_cleanup_diagnostic(
                                    primary,
                                    "c" * (self.helper._DIAGNOSTIC_LIMIT * 2),
                                    context=marker,
                                )
                            else:
                                backend._attach_cleanup_diagnostic(
                                    primary,
                                    backend_module.BackendError(
                                        "capacity_cleanup",
                                        marker
                                        + ("c" * (self.helper._DIAGNOSTIC_LIMIT * 2)),
                                    ),
                                )
                        raise primary

                    transaction = self.helper.MirrorSync(
                        backend,
                        action_hook=inject,
                    )
                    exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                        transaction
                    )

                    self.assertEqual(exit_status, 2)
                    self.assertEqual(stderr, "")
                    self.assertEqual(receipt["outcome"], "fatal")
                    self.assertEqual(receipt["reason"], primary.reason)
                    self.assertIn(primary_marker, receipt["detail"])
                    self.assertNotIn(cleanup_markers[0], receipt["detail"])
                    for marker in cleanup_markers[1:]:
                        self.assertIn(marker, receipt["detail"])
                    self.assertIn(
                        self.helper._TRUNCATED_MARKER,
                        receipt["detail"],
                    )
                    self.assertLessEqual(
                        len(receipt["detail"].encode("utf-8")),
                        self.helper._DIAGNOSTIC_LIMIT,
                    )
                    self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
                    validated = self.validate_receipt(receipt, 2, raw_input=raw)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assertEqual(
                        self.destination.stat().st_ino,
                        before_destination.st_ino,
                    )
                    self.assertEqual(self.destination.read_bytes(), b"old\n")
                    self.assert_no_stage_names()
                    observed_details.append(receipt["detail"])

            self.assertEqual(observed_details[0], observed_details[1])

    def test_unprintable_primary_and_cleanup_errors_cannot_break_cleanup(
        self,
    ) -> None:
        class GetterBomb(BaseException):
            pass

        class PrimaryStringBomb(Exception):
            def __init__(inner_self, string_error: BaseException) -> None:
                inner_self.string_error = string_error
                inner_self.cleanup_getter_calls = 0
                super().__init__(type(string_error).__name__)

            def __str__(inner_self) -> str:
                raise inner_self.string_error

            @property
            def cleanup_diagnostics(inner_self) -> Any:
                inner_self.cleanup_getter_calls += 1
                raise GetterBomb("cleanup_diagnostics getter failed")

        class AclCleanupFailure(BaseException):
            def __str__(inner_self) -> str:
                raise KeyboardInterrupt("ACL cleanup __str__ failed")

        class StageCleanupFailure(BaseException):
            def __str__(inner_self) -> str:
                raise SystemExit(101)

        class FdCleanupFailure(BaseException):
            def __str__(inner_self) -> str:
                raise GetterBomb("FD cleanup __str__ failed")

        primary_string_errors = (
            lambda: KeyboardInterrupt("primary __str__ keyboard interrupt"),
            lambda: SystemExit(103),
            lambda: GetterBomb("primary __str__ custom baseexception"),
        )
        for index, make_string_error in enumerate(primary_string_errors):
            string_error = make_string_error()
            with self.subTest(string_error=type(string_error).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"unprintable-primary-{index}\n".encode())
                before_destination = self.write_destination()
                primary = PrimaryStringBomb(string_error)
                original_args = primary.args
                acl_error = AclCleanupFailure()
                stage_error = StageCleanupFailure()
                fd_error = FdCleanupFailure()
                backend = self.backend_module.DarwinBackend()
                transaction: Any = None
                owned_fds = []
                close_calls = []
                close_call_start = 0
                failing_fd = -1
                acl_armed = False
                acl_attempts = []
                stage_attempts = 0
                fd_injected = False

                def inject(action: str) -> None:
                    nonlocal acl_armed, close_call_start, failing_fd
                    nonlocal owned_fds, stage_attempts
                    if action == "after_clone":
                        owned_fds = [
                            transaction.candidate_fd,
                            transaction.stage_fd,
                            transaction.destination_fd,
                            transaction.destination_parent_fd,
                            transaction.source_fd,
                            transaction.source_parent_fd,
                        ]
                        self.assertTrue(all(fd >= 0 for fd in owned_fds))
                        failing_fd = transaction.destination_fd
                        close_call_start = len(close_calls)
                        acl_armed = True
                        try:
                            backend._free_acls(
                                (
                                    ("first hostile ACL", ctypes.c_void_p(606)),
                                    ("second hostile ACL", ctypes.c_void_p(707)),
                                ),
                                primary_error=primary,
                            )
                        finally:
                            acl_armed = False
                        raise primary
                    if action == "before_cleanup":
                        stage_attempts += 1
                        raise stage_error

                transaction = self.helper.MirrorSync(backend, action_hook=inject)
                real_acl_free = backend._acl_free
                real_close = os.close

                def fail_hostile_acl(pointer: ctypes.c_void_p) -> int:
                    if acl_armed:
                        acl_attempts.append(pointer.value)
                        raise acl_error
                    return real_acl_free(pointer)

                def fail_one_final_close(fd: int) -> None:
                    nonlocal fd_injected
                    close_calls.append(fd)
                    real_close(fd)
                    if fd == failing_fd and not fd_injected:
                        fd_injected = True
                        raise fd_error

                with mock.patch.object(
                    backend,
                    "_acl_free",
                    side_effect=fail_hostile_acl,
                ):
                    with mock.patch.object(
                        self.helper.os,
                        "close",
                        side_effect=fail_one_final_close,
                    ):
                        exit_status, receipt, raw, stderr = (
                            self.run_transaction_via_main(transaction)
                        )

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], "unexpected_error")
                self.assertIn("PrimaryStringBomb: <unprintable>", receipt["detail"])
                self.assertIn("FdCleanupFailure: <unprintable>", receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(primary.args, original_args)
                self.assertGreater(primary.cleanup_getter_calls, 0)
                self.assertEqual(acl_attempts, [606, 707])
                self.assertEqual(stage_attempts, 1)
                self.assertTrue(fd_injected)
                self.assertEqual(close_calls[close_call_start:], owned_fds)
                self.assertTrue(
                    all(
                        getattr(transaction, name) == -1
                        for name in (
                            "candidate_fd",
                            "stage_fd",
                            "destination_fd",
                            "destination_parent_fd",
                            "source_fd",
                            "source_parent_fd",
                        )
                    )
                )
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                retained_stage = pathlib.Path(transaction.stage_path)
                self.assertTrue(retained_stage.is_dir())
                self.assertEqual(
                    [child.name for child in retained_stage.iterdir()],
                    ["candidate.jsonl"],
                )

    def test_hostile_str_subclasses_cannot_escape_diagnostic_normalization(
        self,
    ) -> None:
        backend_module = self.backend_module

        class EncodeBomb(BaseException):
            pass

        class HostileText(str):
            def __new__(
                cls,
                value: str,
                behavior: str,
            ) -> "HostileText":
                instance = super().__new__(cls, value)
                instance.behavior = behavior
                return instance

            def encode(
                inner_self,
                encoding: str = "utf-8",
                errors: str = "strict",
            ) -> bytes:
                del encoding, errors
                if inner_self.behavior == "keyboard":
                    raise KeyboardInterrupt("hostile str encode interrupt")
                if inner_self.behavior == "custom":
                    raise EncodeBomb("hostile str encode custom BaseException")
                return b"spoofed-hostile-encode-bytes"

        for behavior in ("keyboard", "custom", "spoofed-bytes"):
            with self.subTest(behavior=behavior):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"hostile-str-{behavior}\n".encode())
                before_destination = self.write_destination()
                detail_marker = f"hostile-detail-{behavior}-marker"
                cleanup_marker = f"hostile-cleanup-{behavior}-marker"
                note_marker = f"hostile-note-{behavior}-marker"
                primary = backend_module.BackendError(
                    "hostile_diagnostic_primary",
                    HostileText(detail_marker, behavior),
                    errno.EIO,
                )
                primary.cleanup_diagnostics = (HostileText(cleanup_marker, behavior),)
                hostile_note = HostileText(note_marker, behavior)
                add_note = getattr(BaseException, "add_note", None)
                if callable(add_note):
                    add_note(primary, hostile_note)
                else:
                    primary.__notes__ = [hostile_note]
                backend = backend_module.DarwinBackend()
                transaction: Any = None
                owned_fds = []
                close_calls = []
                close_call_start = 0

                def inject(action: str) -> None:
                    nonlocal close_call_start, owned_fds
                    if action != "after_clone":
                        return
                    owned_fds = [
                        transaction.candidate_fd,
                        transaction.stage_fd,
                        transaction.destination_fd,
                        transaction.destination_parent_fd,
                        transaction.source_fd,
                        transaction.source_parent_fd,
                    ]
                    self.assertTrue(all(fd >= 0 for fd in owned_fds))
                    close_call_start = len(close_calls)
                    raise primary

                transaction = self.helper.MirrorSync(backend, action_hook=inject)
                real_close = os.close

                def record_close(fd: int) -> None:
                    close_calls.append(fd)
                    real_close(fd)

                with mock.patch.object(
                    self.helper.os,
                    "close",
                    side_effect=record_close,
                ):
                    exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                        transaction
                    )

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], primary.reason)
                self.assertIn(detail_marker, receipt["detail"])
                self.assertIn(cleanup_marker, receipt["detail"])
                self.assertIn(note_marker, receipt["detail"])
                self.assertNotIn("spoofed-hostile-encode-bytes", receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                cleanup_close_calls = close_calls[close_call_start:]
                self.assertGreaterEqual(len(cleanup_close_calls), len(owned_fds))
                self.assertEqual(cleanup_close_calls[-len(owned_fds) :], owned_fds)
                self.assertTrue(
                    all(
                        getattr(transaction, name) == -1
                        for name in (
                            "candidate_fd",
                            "stage_fd",
                            "destination_fd",
                            "destination_parent_fd",
                            "source_fd",
                            "source_parent_fd",
                        )
                    )
                )
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_acl_cleanup_diagnostic_survives_deferred_and_backend_wrappers(
        self,
    ) -> None:
        backend_module = self.backend_module

        class AclCleanupFailure(BaseException):
            pass

        for wrapper in ("deferred", "backend"):
            with self.subTest(wrapper=wrapper):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"acl-cause-{wrapper}\n".encode())
                before_destination = self.write_destination()
                marker = f"acl-{wrapper}-inner-finalizer-marker"
                cleanup_error = AclCleanupFailure(marker)
                inner = backend_module.BackendError(
                    "acl_unstable",
                    f"inner ACL snapshot primary for {wrapper}",
                    errno.EIO,
                )

                class WrappedAclSnapshotBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.injected = False

                    def snapshot_policy(inner_self, fd: int) -> Any:
                        if inner_self.injected:
                            return super().snapshot_policy(fd)
                        inner_self.injected = True
                        try:
                            raise inner
                        except BaseException as primary:
                            inner_self._free_acls(
                                (("wrapped snapshot ACL", ctypes.c_void_p(808)),),
                                primary_error=primary,
                            )
                            if wrapper == "backend":
                                raise backend_module.BackendError(
                                    "snapshot_reclassified",
                                    "ACL snapshot failure was reclassified",
                                    errno.EIO,
                                ) from primary
                            raise

                backend = WrappedAclSnapshotBackend()
                with mock.patch.object(
                    backend,
                    "_acl_free",
                    side_effect=cleanup_error,
                ):
                    transaction = self.helper.MirrorSync(backend)
                    exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                        transaction
                    )

                self.assertTrue(backend.injected)
                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(
                    receipt["reason"],
                    (
                        "stage_cleanup_failed"
                        if wrapper == "deferred"
                        else "snapshot_reclassified"
                    ),
                )
                self.assertIn(marker, receipt["detail"])
                self.assertIn(type(cleanup_error).__name__, receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_cleanup_diagnostic_cause_cycles_and_deep_chains_are_bounded(
        self,
    ) -> None:
        backend_module = self.backend_module

        for shape in ("cycle", "deep"):
            with self.subTest(shape=shape):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"cause-{shape}\n".encode())
                before_destination = self.write_destination()
                duplicate_marker = f"{shape}-deduplicated-cleanup-marker"
                node_count = 4 if shape == "cycle" else 32
                nodes = [
                    backend_module.BackendError(
                        f"{shape}_cause_{index}",
                        f"{shape} cause detail {index}",
                        errno.EIO,
                    )
                    for index in range(node_count)
                ]
                for index, node in enumerate(nodes):
                    marker = (
                        duplicate_marker
                        if index < 2
                        else f"{shape}-cleanup-marker-{index}"
                    )
                    node.cleanup_diagnostics = (
                        marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2)),
                    )
                for current, cause in zip(nodes, nodes[1:]):
                    current.__cause__ = cause
                if shape == "cycle":
                    nodes[-1].__cause__ = nodes[1]
                primary = nodes[0]

                class CauseChainBackend(backend_module.DarwinBackend):
                    def snapshot_policy(inner_self, fd: int) -> Any:
                        del fd
                        raise primary

                transaction = self.helper.MirrorSync(CauseChainBackend())
                exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                    transaction
                )

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], primary.reason)
                self.assertIn(primary.detail, receipt["detail"])
                self.assertEqual(receipt["detail"].count(duplicate_marker), 1)
                self.assertIn(self.helper._TRUNCATED_MARKER, receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_cleanup_cause_capacity_keeps_outermost_latest_categories(self) -> None:
        backend_module = self.backend_module
        primary_marker = "cause-capacity-primary-marker"
        cleanup_markers = [
            f"cause-capacity-cleanup-{index}-marker" for index in range(9)
        ]
        nodes = [
            backend_module.BackendError(
                f"cause_capacity_{index}",
                primary_marker if index == 0 else f"inner cause {index}",
                errno.EIO,
            )
            for index in range(len(cleanup_markers))
        ]
        for node, marker in zip(nodes, cleanup_markers):
            node.cleanup_diagnostics = (
                marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2)),
            )
        for current, cause in zip(nodes, nodes[1:]):
            current.__cause__ = cause
        primary = nodes[0]

        class CauseCapacityBackend(backend_module.DarwinBackend):
            def snapshot_policy(inner_self, fd: int) -> Any:
                del fd
                raise primary

        self.write_source(b"cause-capacity\n")
        before_destination = self.write_destination()
        transaction = self.helper.MirrorSync(CauseCapacityBackend())
        exit_status, receipt, raw, stderr = self.run_transaction_via_main(transaction)

        self.assertEqual(exit_status, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertIn(primary_marker, receipt["detail"])
        for marker in cleanup_markers[:7]:
            self.assertIn(marker, receipt["detail"])
        for marker in cleanup_markers[7:]:
            self.assertNotIn(marker, receipt["detail"])
        self.assertIn(self.helper._TRUNCATED_MARKER, receipt["detail"])
        self.assertLessEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_cleanup_cause_lookup_short_circuits_hostile_secondary_getters(
        self,
    ) -> None:
        backend_module = self.backend_module

        class GetterBomb(BaseException):
            pass

        class HostileChainError(backend_module.BackendError):
            def __init__(
                inner_self,
                *,
                injected_cause: Optional[BaseException],
                cause_error: Optional[BaseException] = None,
            ) -> None:
                super().__init__(
                    "hostile_chain_primary",
                    "hostile chain primary detail",
                    errno.EIO,
                )
                inner_self.injected_cause = injected_cause
                inner_self.cause_error = cause_error
                inner_self.context_reads = 0
                inner_self.suppress_reads = 0

            def __getattribute__(inner_self, name: str) -> Any:
                if name == "__cause__":
                    cause_error = object.__getattribute__(
                        inner_self,
                        "cause_error",
                    )
                    if cause_error is not None:
                        raise cause_error
                    return object.__getattribute__(
                        inner_self,
                        "injected_cause",
                    )
                if name == "__context__":
                    inner_self.context_reads += 1
                    raise GetterBomb("hostile context getter was accessed")
                if name == "__suppress_context__":
                    inner_self.suppress_reads += 1
                    raise GetterBomb("hostile suppress getter was accessed")
                return object.__getattribute__(inner_self, name)

        cases = []
        inner = backend_module.BackendError(
            "inner_cause",
            "inner cause detail",
            errno.EIO,
        )
        inner.cleanup_diagnostics = ("valid-cause-inner-cleanup-marker",)
        cases.append(("valid-cause", HostileChainError(injected_cause=inner)))
        cases.extend(
            (
                (
                    "keyboard-cause-getter",
                    HostileChainError(
                        injected_cause=None,
                        cause_error=KeyboardInterrupt("hostile cause getter"),
                    ),
                ),
                (
                    "custom-cause-getter",
                    HostileChainError(
                        injected_cause=None,
                        cause_error=GetterBomb("custom hostile cause getter"),
                    ),
                ),
            )
        )

        for case, primary in cases:
            with self.subTest(case=case):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(f"hostile-cause-{case}\n".encode())
                before_destination = self.write_destination()
                primary.cleanup_diagnostics = (f"{case}-outer-cleanup-marker",)

                class HostileCauseBackend(backend_module.DarwinBackend):
                    def snapshot_policy(inner_self, fd: int) -> Any:
                        del fd
                        raise primary

                transaction = self.helper.MirrorSync(HostileCauseBackend())
                exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                    transaction
                )

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], primary.reason)
                self.assertIn(primary.detail, receipt["detail"])
                self.assertIn(
                    f"{case}-outer-cleanup-marker",
                    receipt["detail"],
                )
                if case == "valid-cause":
                    self.assertIn(
                        "valid-cause-inner-cleanup-marker",
                        receipt["detail"],
                    )
                self.assertEqual(primary.context_reads, 0)
                self.assertEqual(primary.suppress_reads, 0)
                self.assertLessEqual(len(raw), self.helper._RECEIPT_LIMIT)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_strict_clone_updates_existing_destination_with_provenance(self) -> None:
        content = b'{"one":1}\n{"two":2}\n'
        self.write_source(content)
        before = self.write_destination()

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "updated")
        self.assertEqual(receipt["method"], "reflink")
        self.assertTrue(receipt["destination_mutated"])
        self.assertIsNone(receipt["reason"])
        self.assertIsNone(receipt["detail"])
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertNotEqual(self.destination.stat().st_ino, before.st_ino)
        self.assert_no_stage_names()

    def test_absent_destination_is_created_and_updated_destination_is_replaced(
        self,
    ) -> None:
        self.write_source(b"first\n")
        first = self.sync()
        first_inode = self.destination.stat().st_ino
        self.assertEqual(first["outcome"], "updated")
        self.assertEqual(first["method"], "reflink")

        self.write_source(b"second\n")
        second = self.sync()

        self.assertEqual(second["outcome"], "updated")
        self.assertEqual(second["method"], "reflink")
        self.assertNotEqual(self.destination.stat().st_ino, first_inode)
        self.assertEqual(self.destination.read_bytes(), b"second\n")

    def test_unchanged_destination_keeps_inode(self) -> None:
        content = b"stable\n"
        self.write_source(content)
        before = self.write_destination(content)
        os.utime(
            self.destination,
            ns=(self.source.stat().st_atime_ns, self.source.stat().st_mtime_ns),
        )
        before = self.destination.stat()

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "unchanged")
        self.assertFalse(receipt["destination_mutated"])
        self.assertEqual(receipt["new_size"], receipt["old_size"])
        self.assertEqual(receipt["new_identity"], receipt["old_identity"])
        self.assertIsNone(receipt["reason"])
        self.assertIsNone(receipt["detail"])
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), content)
        self.assert_no_stage_names()

    def test_no_complete_line_source_replacement_during_cleanup_is_deferred(
        self,
    ) -> None:
        self.write_source(b"no-complete-line")
        before = self.write_destination()
        held = self.root / "held-no-line-source"
        injected = False

        def replace_source(action: str) -> None:
            nonlocal injected
            if action != "before_cleanup" or injected:
                return
            injected = True
            os.replace(self.source, held)
            self.source.write_bytes(b"replacement\n")

        receipt = self.sync(action_hook=replace_source)

        self.assertTrue(injected)
        self.assert_deferred(receipt)
        self.assertEqual(held.read_bytes(), b"no-complete-line")
        self.assertEqual(self.source.read_bytes(), b"replacement\n")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_no_complete_line_same_inode_content_races_are_deferred(self) -> None:
        original = b"0123456789abcdef"
        rewritten = b"fedcba9876543210"
        hook_stages = (
            "after_clone",
            "before_cleanup",
            "authorize_cleanup_stage",
        )
        for hook_stage in hook_stages:
            for mutation in ("truncate", "rewrite"):
                for destination_exists in (False, True):
                    with self.subTest(
                        hook_stage=hook_stage,
                        mutation=mutation,
                        destination_exists=destination_exists,
                    ):
                        self.remove_path(self.source)
                        self.remove_path(self.destination)
                        self.write_source(original)
                        before_destination = None
                        if destination_exists:
                            self.write_destination()
                            before_destination = self.destination_snapshot()
                        injected = False

                        def mutate(action: str) -> None:
                            nonlocal injected
                            if action != hook_stage or injected:
                                return
                            injected = True
                            self.mutate_source_same_inode(
                                mutation,
                                rewrite=rewritten,
                            )

                        receipt = self.sync(action_hook=mutate)

                        self.assertTrue(injected)
                        self.assert_deferred(receipt)
                        self.assertEqual(receipt["reason"], "source_unstable")
                        validated = self.validate_receipt(receipt, 75)
                        self.assertEqual(validated.returncode, 0, validated.stderr)
                        if before_destination is None:
                            self.assertFalse(self.destination.exists())
                        else:
                            self.assert_destination_snapshot(before_destination)
                        self.assert_no_stage_names()

    def test_no_complete_line_same_inode_append_remains_successful(self) -> None:
        original = b"0123456789abcdef"
        appended = b"-appended-without-newline"
        hook_stages = (
            "after_clone",
            "before_cleanup",
            "authorize_cleanup_stage",
        )
        for hook_stage in hook_stages:
            for destination_exists in (False, True):
                with self.subTest(
                    hook_stage=hook_stage,
                    destination_exists=destination_exists,
                ):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    self.write_source(original)
                    before_destination = None
                    if destination_exists:
                        self.write_destination()
                        before_destination = self.destination_snapshot()
                    injected = False

                    def append_source(action: str) -> None:
                        nonlocal injected
                        if action != hook_stage or injected:
                            return
                        injected = True
                        self.mutate_source_same_inode(
                            "append",
                            append=appended,
                        )

                    receipt = self.sync(action_hook=append_source)

                    self.assertTrue(injected)
                    self.assertEqual(
                        receipt["outcome"],
                        "no-complete-line",
                        receipt,
                    )
                    self.assertFalse(receipt["destination_mutated"])
                    self.assertEqual(self.source.read_bytes(), original + appended)
                    self.assertEqual(receipt["source_size"], len(original + appended))
                    validated = self.validate_receipt(receipt, 0)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    if before_destination is None:
                        self.assertFalse(self.destination.exists())
                    else:
                        self.assert_destination_snapshot(before_destination)
                    self.assert_no_stage_names()

    def test_unchanged_destination_replacement_during_cleanup_is_not_success(
        self,
    ) -> None:
        content = b"unchanged-cleanup\n"
        self.write_source(content)
        self.write_destination(content)
        os.utime(
            self.destination,
            ns=(self.source.stat().st_atime_ns, self.source.stat().st_mtime_ns),
        )
        before = self.destination.stat()
        held = self.root / "held-unchanged-destination"
        injected = False

        def replace_destination(action: str) -> None:
            nonlocal injected
            if action != "before_cleanup" or injected:
                return
            injected = True
            os.replace(self.destination, held)
            self.destination.write_bytes(b"replacement\n")

        receipt = self.sync(action_hook=replace_destination)

        self.assertTrue(injected)
        self.assert_deferred(receipt)
        self.assertEqual(held.stat().st_ino, before.st_ino)
        self.assertEqual(held.read_bytes(), content)
        self.assertEqual(self.destination.read_bytes(), b"replacement\n")
        self.assert_no_stage_names()

    def test_unchanged_cleanup_window_revalidates_same_inode_source_content(
        self,
    ) -> None:
        original = b"unchanged-cleanup-window\n"
        rewritten = b"X" * (len(original) - 1) + b"\n"
        for hook_stage in ("before_cleanup", "authorize_cleanup_stage"):
            for mutation in ("truncate", "rewrite", "append"):
                with self.subTest(hook_stage=hook_stage, mutation=mutation):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    self.write_source(original)
                    self.write_destination(original)
                    os.utime(
                        self.destination,
                        ns=(
                            self.source.stat().st_atime_ns,
                            self.source.stat().st_mtime_ns,
                        ),
                    )
                    before_destination = self.destination_snapshot()
                    injected = False

                    def mutate(action: str) -> None:
                        nonlocal injected
                        if action != hook_stage or injected:
                            return
                        injected = True
                        self.mutate_source_same_inode(
                            mutation,
                            rewrite=rewritten,
                            append=b"appended-after-compare",
                        )

                    receipt = self.sync(action_hook=mutate)

                    self.assertTrue(injected)
                    if mutation == "append":
                        self.assertEqual(receipt["outcome"], "unchanged", receipt)
                        self.assertFalse(receipt["destination_mutated"])
                        self.assertEqual(
                            receipt["source_size"],
                            self.source.stat().st_size,
                        )
                        expected_exit = 0
                    else:
                        self.assert_deferred(receipt)
                        self.assertEqual(receipt["reason"], "source_unstable")
                        expected_exit = 75
                    validated = self.validate_receipt(receipt, expected_exit)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assert_destination_snapshot(before_destination)
                    self.assert_no_stage_names()

    def test_partial_clone_size_sets_source_high_water_before_truncation(self) -> None:
        initial = b"x"
        expanded = b"xyz\nTAIL"
        publish_size = len(b"xyz\n")
        self.write_source(initial)
        before_destination = self.write_destination()
        expanded_mtime_ns = 0
        before_clone_injected = False
        after_clone_injected = False

        def mutate_source(action: str) -> None:
            nonlocal after_clone_injected, before_clone_injected
            nonlocal expanded_mtime_ns
            if action == "before_clone":
                before_clone_injected = True
                self.mutate_source_same_inode(
                    "append",
                    append=expanded[len(initial) :],
                )
                expanded_mtime_ns = self.source.stat().st_mtime_ns
                self.assertEqual(self.source.read_bytes(), expanded)
                return
            if action != "after_clone":
                return
            after_clone_injected = True
            self.mutate_source_same_inode(
                "truncate",
                truncate_size=publish_size,
            )
            current = self.source.stat()
            os.utime(
                self.source,
                ns=(current.st_atime_ns, expanded_mtime_ns),
            )
            self.assertEqual(self.source.read_bytes(), expanded[:publish_size])

        receipt = self.sync(action_hook=mutate_source)

        self.assertTrue(before_clone_injected)
        self.assertTrue(after_clone_injected)
        self.assert_deferred(receipt)
        self.assertEqual(receipt["reason"], "source_unstable")
        self.assertEqual(receipt["method"], "reflink")
        self.assertGreaterEqual(receipt["source_size"], len(expanded))
        validated = self.validate_receipt(receipt, 75)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(
            self.destination.stat().st_ino,
            before_destination.st_ino,
        )
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

        self.remove_path(self.source)
        self.remove_path(self.destination)
        partial_source = b"complete\npartial-tail"
        self.write_source(partial_source)
        positive = self.sync()
        self.assertEqual(positive["outcome"], "updated")
        self.assertTrue(positive["partial"])
        self.assertGreater(positive["source_size"], positive["publish_size"])
        validated = self.validate_receipt(positive, 0)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_source_size_high_water_rejects_late_suffix_shrink(self) -> None:
        appended = b"ABCDEFGHIJKLMNOP"
        retained_suffix = appended[:5]
        cases = (
            ("no-complete-line", False),
            ("no-complete-line", True),
            ("unchanged", True),
            ("updated", False),
            ("updated", True),
        )
        for branch, destination_exists in cases:
            with self.subTest(
                branch=branch,
                destination_exists=destination_exists,
            ):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                candidate = (
                    b"candidate-prefix"
                    if branch == "no-complete-line"
                    else b"candidate-prefix\n"
                )
                self.write_source(candidate)
                before_destination = None
                if branch == "unchanged":
                    self.write_destination(candidate)
                    os.utime(
                        self.destination,
                        ns=(
                            self.source.stat().st_atime_ns,
                            self.source.stat().st_mtime_ns,
                        ),
                    )
                    before_destination = self.destination_snapshot()
                elif destination_exists:
                    self.write_destination()
                    before_destination = self.destination_snapshot()

                append_done = False
                shrink_done = False

                def append_source() -> None:
                    nonlocal append_done
                    self.assertFalse(append_done)
                    append_done = True
                    self.mutate_source_same_inode(
                        "append",
                        append=appended,
                    )

                def shrink_source() -> None:
                    nonlocal shrink_done
                    self.assertTrue(append_done)
                    self.assertFalse(shrink_done)
                    shrink_done = True
                    self.mutate_source_same_inode(
                        "truncate",
                        truncate_size=len(candidate) + len(retained_suffix),
                    )

                def action_hook(action: str) -> None:
                    if branch == "updated":
                        if action == "authorize_publish" and not append_done:
                            append_source()
                        return
                    if action == "before_cleanup" and not append_done:
                        append_source()
                    elif action == "authorize_cleanup_stage" and not shrink_done:
                        shrink_source()

                backend_factory = None
                if branch == "updated":
                    backend_module = self.backend_module

                    class BetweenPublishValidationsBackend(
                        backend_module.DarwinBackend
                    ):
                        def publish_staged_name(
                            inner_self,
                            *args: Any,
                            **kwargs: Any,
                        ) -> Any:
                            validator = kwargs["validate_after_authorization"]

                            def shrink_then_validate() -> Any:
                                shrink_source()
                                return validator()

                            kwargs["validate_after_authorization"] = (
                                shrink_then_validate
                            )
                            return super().publish_staged_name(*args, **kwargs)

                    backend_factory = BetweenPublishValidationsBackend

                receipt = self.sync(
                    action_hook=action_hook,
                    backend_factory=backend_factory,
                )

                self.assertTrue(append_done)
                self.assertTrue(shrink_done)
                self.assertEqual(
                    self.source.read_bytes(),
                    candidate + retained_suffix,
                )
                self.assert_deferred(receipt)
                self.assertEqual(receipt["reason"], "source_unstable")
                validated = self.validate_receipt(receipt, 75)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                if before_destination is None:
                    self.assertFalse(self.destination.exists())
                else:
                    self.assert_destination_snapshot(before_destination)
                self.assert_no_stage_names()

    def test_source_size_high_water_allows_continued_growth(self) -> None:
        first_append = b"ABCDEFGHIJKLMNOP"
        second_append = b"QRSTU"
        for branch in ("no-complete-line", "unchanged", "updated"):
            with self.subTest(branch=branch):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                candidate = (
                    b"candidate-prefix"
                    if branch == "no-complete-line"
                    else b"candidate-prefix\n"
                )
                self.write_source(candidate)
                if branch == "unchanged":
                    self.write_destination(candidate)
                    os.utime(
                        self.destination,
                        ns=(
                            self.source.stat().st_atime_ns,
                            self.source.stat().st_mtime_ns,
                        ),
                    )
                else:
                    self.write_destination()
                before_destination = self.destination_snapshot()
                first_done = False
                second_done = False

                def append_first() -> None:
                    nonlocal first_done
                    self.assertFalse(first_done)
                    first_done = True
                    self.mutate_source_same_inode(
                        "append",
                        append=first_append,
                    )

                def append_second() -> None:
                    nonlocal second_done
                    self.assertTrue(first_done)
                    self.assertFalse(second_done)
                    second_done = True
                    self.mutate_source_same_inode(
                        "append",
                        append=second_append,
                    )

                def action_hook(action: str) -> None:
                    if branch == "updated":
                        if action == "authorize_publish" and not first_done:
                            append_first()
                        return
                    if action == "before_cleanup" and not first_done:
                        append_first()
                    elif action == "authorize_cleanup_stage" and not second_done:
                        append_second()

                backend_factory = None
                if branch == "updated":
                    backend_module = self.backend_module

                    class BetweenPublishValidationsBackend(
                        backend_module.DarwinBackend
                    ):
                        def publish_staged_name(
                            inner_self,
                            *args: Any,
                            **kwargs: Any,
                        ) -> Any:
                            validator = kwargs["validate_after_authorization"]

                            def grow_then_validate() -> Any:
                                append_second()
                                return validator()

                            kwargs["validate_after_authorization"] = grow_then_validate
                            return super().publish_staged_name(*args, **kwargs)

                    backend_factory = BetweenPublishValidationsBackend

                receipt = self.sync(
                    action_hook=action_hook,
                    backend_factory=backend_factory,
                )

                self.assertTrue(first_done)
                self.assertTrue(second_done)
                self.assertEqual(
                    self.source.read_bytes(),
                    candidate + first_append + second_append,
                )
                self.assertEqual(receipt["outcome"], branch, receipt)
                self.assertEqual(receipt["source_size"], self.source.stat().st_size)
                validated = self.validate_receipt(receipt, 0)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                if branch == "updated":
                    self.assertEqual(self.destination.read_bytes(), candidate)
                else:
                    self.assert_destination_snapshot(before_destination)
                self.assert_no_stage_names()

    def test_source_prefix_entry_high_water_is_sticky_before_prefix_reads(
        self,
    ) -> None:
        backend_module = self.backend_module
        candidate = b"entry-sticky-prefix\n"
        first_suffix = b"ABCDEFGHIJKLMNOP"
        retained_suffix = first_suffix[:5]
        rewritten_suffix = b"ponmlkjihgfedcba"
        regrowth = b"-regrow-beyond-prior-high-water"
        high_water = len(candidate + first_suffix)

        for mutation in ("shrink", "rewrite"):
            for destination_exists in (False, True):
                with self.subTest(
                    mutation=mutation,
                    destination_exists=destination_exists,
                ):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    self.write_source(candidate)
                    source_inode = self.source.stat().st_ino
                    before_destination = None
                    if destination_exists:
                        self.write_destination()
                        before_destination = self.destination_snapshot()

                    state = {
                        "first_growth": False,
                        "held_identity_calls": 0,
                        "entry_mutated": False,
                        "pread_growth_armed": False,
                        "pread_regrew": False,
                        "terminal_calls": 0,
                    }
                    transaction: Any = None

                    class EntryStickyBackend(backend_module.DarwinBackend):
                        def identity(inner_self, fd: int) -> Any:
                            if (
                                transaction is None
                                or fd != transaction.source_fd
                                or not state["first_growth"]
                            ):
                                return super().identity(fd)
                            state["held_identity_calls"] += 1
                            if state["held_identity_calls"] != 2:
                                return super().identity(fd)

                            self.assertFalse(state["entry_mutated"])
                            state["entry_mutated"] = True
                            if mutation == "shrink":
                                self.mutate_source_same_inode(
                                    "truncate",
                                    truncate_size=len(candidate + retained_suffix),
                                )
                            else:
                                before_rewrite = self.source.stat()
                                self.mutate_source_same_inode(
                                    "rewrite",
                                    rewrite=candidate + rewritten_suffix,
                                )
                                current = self.source.stat()
                                os.utime(
                                    self.source,
                                    ns=(
                                        current.st_atime_ns,
                                        before_rewrite.st_mtime_ns + 1_000_000,
                                    ),
                                )
                            entry = super().identity(fd)
                            if mutation == "shrink":
                                self.assertLess(entry.size, high_water)
                                self.assertGreaterEqual(entry.size, len(candidate))
                            else:
                                self.assertEqual(entry.size, high_water)
                            state["pread_growth_armed"] = True
                            return entry

                    backend = EntryStickyBackend()

                    def action_hook(action: str) -> None:
                        if action != "before_source_prefix_return":
                            return
                        state["terminal_calls"] += 1
                        if state["first_growth"]:
                            return
                        state["first_growth"] = True
                        self.mutate_source_same_inode(
                            "append",
                            append=first_suffix,
                        )
                        self.assertEqual(self.source.stat().st_size, high_water)

                    transaction = self.helper.MirrorSync(
                        backend,
                        action_hook=action_hook,
                    )
                    real_pread = os.pread

                    def regrow_on_source_prefix_read(
                        fd: int,
                        size: int,
                        offset: int,
                    ) -> bytes:
                        if state["pread_growth_armed"] and fd == transaction.source_fd:
                            state["pread_growth_armed"] = False
                            state["pread_regrew"] = True
                            self.mutate_source_same_inode(
                                "append",
                                append=regrowth,
                            )
                            self.assertGreater(
                                self.source.stat().st_size,
                                high_water,
                            )
                        return real_pread(fd, size, offset)

                    with mock.patch.object(
                        self.helper.os,
                        "pread",
                        side_effect=regrow_on_source_prefix_read,
                    ):
                        exit_status, receipt, raw, stderr = (
                            self.run_transaction_via_main(transaction)
                        )

                    self.assertEqual(exit_status, 75)
                    self.assertEqual(stderr, "")
                    self.assertTrue(state["first_growth"])
                    self.assertTrue(state["entry_mutated"])
                    self.assertTrue(state["pread_growth_armed"])
                    self.assertFalse(state["pread_regrew"])
                    self.assertEqual(state["terminal_calls"], 1)
                    self.assertEqual(self.source.stat().st_ino, source_inode)
                    self.assert_deferred(receipt)
                    self.assertEqual(receipt["reason"], "source_unstable")
                    self.assertEqual(receipt["method"], "reflink")
                    self.assertGreaterEqual(receipt["source_size"], high_water)
                    validated = self.validate_receipt(receipt, 75, raw_input=raw)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    if before_destination is None:
                        self.assertFalse(self.destination.exists())
                    else:
                        self.assert_destination_snapshot(before_destination)
                    self.assert_no_stage_names()

        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(candidate)
        self.write_destination()
        source_inode = self.source.stat().st_ino
        terminal_calls = 0

        def append_once(action: str) -> None:
            nonlocal terminal_calls
            if action != "before_source_prefix_return":
                return
            terminal_calls += 1
            if terminal_calls == 1:
                self.mutate_source_same_inode(
                    "append",
                    append=first_suffix,
                )

        receipt = self.sync(action_hook=append_once)

        self.assertGreaterEqual(terminal_calls, 2)
        self.assertEqual(receipt["outcome"], "updated", receipt)
        self.assertEqual(receipt["source_size"], high_water)
        self.assertEqual(self.source.stat().st_ino, source_inode)
        self.assertEqual(self.source.read_bytes(), candidate + first_suffix)
        self.assertEqual(self.destination.read_bytes(), candidate)
        validated = self.validate_receipt(receipt, 0)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assert_no_stage_names()

    def test_source_prefix_terminal_boundary_revalidates_source_generation(
        self,
    ) -> None:
        candidate = b"candidate-prefix\n"
        appended = b"ABCDEFGHIJKLMNOP"
        rewritten_suffix = b"ponmlkjihgfedcba"
        growth = b"QRSTU"
        for mutation in ("shrink", "rewrite", "growth"):
            for destination_exists in (False, True):
                with self.subTest(
                    mutation=mutation,
                    destination_exists=destination_exists,
                ):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    self.write_source(candidate)
                    before_destination = None
                    if destination_exists:
                        self.write_destination()
                        before_destination = self.destination_snapshot()
                    append_done = False
                    terminal_mutation_done = False

                    def mutate(action: str) -> None:
                        nonlocal append_done, terminal_mutation_done
                        if action == "after_clone" and not append_done:
                            append_done = True
                            self.mutate_source_same_inode(
                                "append",
                                append=appended,
                            )
                            return
                        if (
                            action != "before_source_prefix_return"
                            or terminal_mutation_done
                        ):
                            return
                        self.assertTrue(append_done)
                        terminal_mutation_done = True
                        before_terminal = self.source.stat()
                        if mutation == "shrink":
                            self.mutate_source_same_inode(
                                "truncate",
                                truncate_size=len(candidate) + 5,
                            )
                        elif mutation == "rewrite":
                            self.mutate_source_same_inode(
                                "rewrite",
                                rewrite=candidate + rewritten_suffix,
                            )
                            current = self.source.stat()
                            os.utime(
                                self.source,
                                ns=(
                                    current.st_atime_ns,
                                    before_terminal.st_mtime_ns + 1_000_000,
                                ),
                            )
                        else:
                            self.mutate_source_same_inode(
                                "append",
                                append=growth,
                            )

                    receipt = self.sync(action_hook=mutate)

                    self.assertTrue(append_done)
                    self.assertTrue(terminal_mutation_done)
                    if mutation == "growth":
                        self.assertEqual(receipt["outcome"], "updated", receipt)
                        self.assertEqual(
                            self.source.read_bytes(),
                            candidate + appended + growth,
                        )
                        self.assertEqual(
                            receipt["source_size"],
                            self.source.stat().st_size,
                        )
                        self.assertEqual(self.destination.read_bytes(), candidate)
                        validated = self.validate_receipt(receipt, 0)
                    else:
                        self.assert_deferred(receipt)
                        self.assertEqual(receipt["reason"], "source_unstable")
                        validated = self.validate_receipt(receipt, 75)
                        if before_destination is None:
                            self.assertFalse(self.destination.exists())
                        else:
                            self.assert_destination_snapshot(before_destination)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assert_no_stage_names()

    def test_source_prefix_cross_validation_rejects_same_size_suffix_rewrite(
        self,
    ) -> None:
        candidate = b"candidate-prefix\n"
        appended = b"ABCDEFGHIJKLMNOP"
        rewritten_suffix = b"ponmlkjihgfedcba"
        for destination_exists in (False, True):
            with self.subTest(destination_exists=destination_exists):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(candidate)
                before_destination = None
                if destination_exists:
                    self.write_destination()
                    before_destination = self.destination_snapshot()
                append_done = False
                rewrite_done = False

                def mutate(action: str) -> None:
                    nonlocal append_done, rewrite_done
                    if action == "after_clone" and not append_done:
                        append_done = True
                        self.mutate_source_same_inode(
                            "append",
                            append=appended,
                        )
                        return
                    if action != "before_publish" or rewrite_done:
                        return
                    self.assertTrue(append_done)
                    rewrite_done = True
                    before_rewrite = self.source.stat()
                    self.mutate_source_same_inode(
                        "rewrite",
                        rewrite=candidate + rewritten_suffix,
                    )
                    current = self.source.stat()
                    os.utime(
                        self.source,
                        ns=(
                            current.st_atime_ns,
                            before_rewrite.st_mtime_ns + 1_000_000,
                        ),
                    )

                receipt = self.sync(action_hook=mutate)

                self.assertTrue(append_done)
                self.assertTrue(rewrite_done)
                self.assertEqual(
                    self.source.read_bytes(),
                    candidate + rewritten_suffix,
                )
                self.assert_deferred(receipt)
                self.assertEqual(receipt["reason"], "source_unstable")
                validated = self.validate_receipt(receipt, 75)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                if before_destination is None:
                    self.assertFalse(self.destination.exists())
                else:
                    self.assert_destination_snapshot(before_destination)
                self.assert_no_stage_names()

    def test_clone_terminal_growth_cannot_hide_prefix_or_policy_change(self) -> None:
        for branch in ("no-complete-line", "unchanged", "updated"):
            for mutation in ("prefix_rewrite", "policy_change"):
                with self.subTest(branch=branch, mutation=mutation):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    candidate = (
                        b"candidate-prefix"
                        if branch == "no-complete-line"
                        else b"candidate-prefix\n"
                    )
                    self.write_source(candidate)
                    if branch == "unchanged":
                        self.write_destination(candidate)
                        os.utime(
                            self.destination,
                            ns=(
                                self.source.stat().st_atime_ns,
                                self.source.stat().st_mtime_ns,
                            ),
                        )
                    else:
                        self.write_destination()
                    before_destination = self.destination_snapshot()
                    source_inode = self.source.stat().st_ino

                    def mutate_on_terminal(call: int) -> None:
                        self.assertEqual(call, 1)
                        if mutation == "policy_change":
                            os.chmod(self.source, 0o640)
                        with self.source.open("r+b") as output:
                            if mutation == "prefix_rewrite":
                                output.seek(0)
                                output.write(b"X")
                            output.seek(0, os.SEEK_END)
                            output.write(b"-terminal-growth")
                            output.flush()
                            os.fsync(output.fileno())
                        self.assertEqual(self.source.stat().st_ino, source_inode)

                    receipt, terminal_calls = self.sync_with_final_source_prefix_hook(
                        branch,
                        self.backend_module.DarwinBackend(),
                        mutate_on_terminal,
                    )

                    self.assertEqual(terminal_calls, 1)
                    self.assert_deferred(receipt)
                    self.assertEqual(receipt["reason"], "source_unstable")
                    self.assertEqual(receipt["method"], "reflink")
                    validated = self.validate_receipt(receipt, 75)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assert_destination_snapshot(before_destination)
                    self.assert_no_stage_names()

    def test_clone_terminal_append_requires_stable_full_revalidation(self) -> None:
        appended = b"-terminal-append"
        for branch in ("no-complete-line", "unchanged", "updated"):
            with self.subTest(branch=branch):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                candidate = (
                    b"candidate-prefix"
                    if branch == "no-complete-line"
                    else b"candidate-prefix\n"
                )
                self.write_source(candidate)
                if branch == "unchanged":
                    self.write_destination(candidate)
                    os.utime(
                        self.destination,
                        ns=(
                            self.source.stat().st_atime_ns,
                            self.source.stat().st_mtime_ns,
                        ),
                    )
                else:
                    self.write_destination()
                before_destination = self.destination_snapshot()
                stable_observations = []

                def append_once_then_observe_stable(call: int) -> None:
                    if call == 1:
                        self.mutate_source_same_inode("append", append=appended)
                        return
                    stable_observations.append(self.source.stat().st_size)

                receipt, terminal_calls = self.sync_with_final_source_prefix_hook(
                    branch,
                    self.backend_module.DarwinBackend(),
                    append_once_then_observe_stable,
                )

                self.assertEqual(terminal_calls, 2)
                self.assertEqual(stable_observations, [len(candidate + appended)])
                self.assertEqual(receipt["outcome"], branch, receipt)
                self.assertEqual(receipt["method"], "reflink")
                self.assertEqual(receipt["source_size"], stable_observations[-1])
                self.assertEqual(receipt["source_size"], self.source.stat().st_size)
                validated = self.validate_receipt(receipt, 0)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                if branch == "updated":
                    self.assertEqual(self.destination.read_bytes(), candidate)
                else:
                    self.assert_destination_snapshot(before_destination)
                self.assert_no_stage_names()

    def test_clone_terminal_continuous_append_hits_bounded_attempt_limit(
        self,
    ) -> None:
        attempt_limit = 3
        for branch in ("no-complete-line", "unchanged", "updated"):
            with self.subTest(branch=branch):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                candidate = (
                    b"candidate-prefix"
                    if branch == "no-complete-line"
                    else b"candidate-prefix\n"
                )
                self.write_source(candidate)
                if branch == "unchanged":
                    self.write_destination(candidate)
                    os.utime(
                        self.destination,
                        ns=(
                            self.source.stat().st_atime_ns,
                            self.source.stat().st_mtime_ns,
                        ),
                    )
                else:
                    self.write_destination()
                before_destination = self.destination_snapshot()

                def keep_appending(call: int) -> None:
                    self.mutate_source_same_inode(
                        "append",
                        append=f"-{call}".encode(),
                    )

                with mock.patch.object(
                    self.helper,
                    "_SOURCE_PREFIX_MAX_ATTEMPTS",
                    attempt_limit,
                ):
                    receipt, terminal_calls = self.sync_with_final_source_prefix_hook(
                        branch,
                        self.backend_module.DarwinBackend(),
                        keep_appending,
                    )

                self.assertEqual(terminal_calls, attempt_limit)
                self.assert_deferred(receipt)
                self.assertEqual(receipt["reason"], "source_unstable")
                self.assertEqual(receipt["method"], "reflink")
                validated = self.validate_receipt(receipt, 75)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assert_destination_snapshot(before_destination)
                self.assert_no_stage_names()

    def test_fallback_terminal_source_mutation_is_always_deferred(self) -> None:
        for branch in ("no-complete-line", "unchanged", "updated"):
            for mutation in ("growth", "mtime", "policy"):
                with self.subTest(branch=branch, mutation=mutation):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    candidate = (
                        b"candidate-prefix"
                        if branch == "no-complete-line"
                        else b"candidate-prefix\n"
                    )
                    self.write_source(candidate)
                    if branch == "unchanged":
                        self.write_destination(candidate)
                        os.utime(
                            self.destination,
                            ns=(
                                self.source.stat().st_atime_ns,
                                self.source.stat().st_mtime_ns,
                            ),
                        )
                    else:
                        self.write_destination()
                    before_destination = self.destination_snapshot()
                    backend = self.make_injected_clone_backend(errno.ENOTSUP)

                    def mutate_on_terminal(call: int) -> None:
                        self.assertEqual(call, 1)
                        if mutation == "growth":
                            self.mutate_source_same_inode(
                                "append",
                                append=b"-fallback-growth",
                            )
                        elif mutation == "mtime":
                            current = self.source.stat()
                            os.utime(
                                self.source,
                                ns=(
                                    current.st_atime_ns,
                                    current.st_mtime_ns + 1_000_000,
                                ),
                            )
                        else:
                            os.chmod(self.source, 0o640)

                    receipt, terminal_calls = self.sync_with_final_source_prefix_hook(
                        branch,
                        backend,
                        mutate_on_terminal,
                    )

                    self.assertEqual(terminal_calls, 1)
                    self.assertEqual(backend.fallback_calls, 1)
                    self.assert_deferred(receipt)
                    self.assertEqual(receipt["reason"], "source_unstable")
                    self.assertEqual(receipt["method"], "copy")
                    validated = self.validate_receipt(receipt, 75)
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assert_destination_snapshot(before_destination)
                    self.assert_no_stage_names()

    def test_fallback_terminal_aba_restore_cannot_reduce_source_high_water(
        self,
    ) -> None:
        backend_module = self.backend_module
        original = b"fallback-aba-source"
        appended = b"-observed-growth"
        self.write_source(original)
        before_source = self.source.stat()
        before_destination = self.write_destination()
        test_case = self

        class FallbackAbaBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fallback_calls = 0
                inner_self.restore_armed = False
                inner_self.restore_calls = 0

            def strict_clone(
                inner_self,
                source_fd: int,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
                writable: bool = False,
            ) -> int:
                del source_fd, writable
                inner_self._authorize_state(authorize_state, "create_clone")
                raise backend_module.BackendError(
                    "clone_failed",
                    "injected ENOTSUP for fallback ABA",
                    errno.ENOTSUP,
                )

            def ordinary_copy_to_absent(
                inner_self,
                *args: Any,
                **kwargs: Any,
            ) -> int:
                inner_self.fallback_calls += 1
                return super().ordinary_copy_to_absent(*args, **kwargs)

            def require_snapshot(
                inner_self,
                fd: int,
                expected: Any,
                subject: str,
                **kwargs: Any,
            ) -> Any:
                if (
                    subject == "ordinary-copy source at terminal boundary"
                    and inner_self.restore_armed
                ):
                    inner_self.restore_armed = False
                    inner_self.restore_calls += 1
                    test_case.assertEqual(
                        test_case.source.read_bytes(),
                        original + appended,
                    )
                    with test_case.source.open("r+b") as output:
                        output.seek(0)
                        output.write(original)
                        output.truncate(len(original))
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(test_case.source, stat.S_IMODE(before_source.st_mode))
                    current = test_case.source.stat()
                    os.utime(
                        test_case.source,
                        ns=(current.st_atime_ns, before_source.st_mtime_ns),
                    )
                return super().require_snapshot(
                    fd,
                    expected,
                    subject,
                    **kwargs,
                )

        backend = FallbackAbaBackend()

        def grow_then_arm_restore(call: int) -> None:
            self.assertEqual(call, 1)
            self.mutate_source_same_inode("append", append=appended)
            backend.restore_armed = True

        receipt, terminal_calls = self.sync_with_final_source_prefix_hook(
            "no-complete-line",
            backend,
            grow_then_arm_restore,
        )

        self.assertEqual(terminal_calls, 1)
        self.assertEqual(backend.fallback_calls, 1)
        self.assertEqual(backend.restore_calls, 1)
        self.assertFalse(backend.restore_armed)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.source.stat().st_size, before_source.st_size)
        self.assertEqual(self.source.stat().st_mtime_ns, before_source.st_mtime_ns)
        self.assertEqual(
            stat.S_IMODE(self.source.stat().st_mode),
            stat.S_IMODE(before_source.st_mode),
        )
        self.assertNotEqual(self.source.stat().st_ctime_ns, before_source.st_ctime_ns)
        self.assert_deferred(receipt)
        self.assertEqual(receipt["reason"], "source_unstable")
        self.assertEqual(receipt["method"], "copy")
        self.assertEqual(receipt["source_size"], len(original + appended))
        validated = self.validate_receipt(receipt, 75)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_unchanged_parent_replacement_during_cleanup_preserves_stage_evidence(
        self,
    ) -> None:
        content = b"unchanged-parent-cleanup\n"
        self.write_source(content)
        self.write_destination(content)
        os.utime(
            self.destination,
            ns=(self.source.stat().st_atime_ns, self.source.stat().st_mtime_ns),
        )
        displaced = self.root / "mirror-cleanup-displaced"
        sentinel = self.destination_parent / "replacement-sentinel"
        injected = False

        def replace_parent(action: str) -> None:
            nonlocal injected
            if action != "before_cleanup" or injected:
                return
            injected = True
            os.replace(self.destination_parent, displaced)
            self.destination_parent.mkdir(mode=0o700)
            sentinel.write_bytes(b"sentinel\n")

        receipt = self.sync(action_hook=replace_parent)

        self.assertTrue(injected)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertFalse(receipt["destination_mutated"])
        self.assertEqual((displaced / ROLLOUT_NAME).read_bytes(), content)
        self.assertEqual(sentinel.read_bytes(), b"sentinel\n")
        retained_stages = [
            child
            for child in displaced.iterdir()
            if child.name.startswith(f".{ROLLOUT_NAME}.codex-stage.")
        ]
        self.assertEqual(len(retained_stages), 1)
        self.assertTrue((retained_stages[0] / "candidate.jsonl").is_file())

    def test_complete_partial_and_no_complete_line(self) -> None:
        cases = (
            (b"one\ntwo\n", b"one\ntwo\n", "updated"),
            (b"one\ntwo", b"one\n", "updated"),
            (b"no-newline", b"old\n", "no-complete-line"),
        )
        for index, (source, expected, outcome) in enumerate(cases):
            with self.subTest(source=source):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(source)
                before = self.write_destination()
                receipt = self.sync()
                self.assertEqual(receipt["outcome"], outcome)
                self.assertEqual(self.destination.read_bytes(), expected)
                if outcome == "no-complete-line":
                    self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                    self.assertEqual(receipt.get("method"), "reflink")
                    self.assertEqual(receipt["new_size"], receipt["old_size"])
                    self.assertEqual(receipt["new_identity"], receipt["old_identity"])
                self.assert_no_stage_names()
                self.assertGreaterEqual(index, 0)

    def test_newline_scanner_boundaries_long_line_and_crlf(self) -> None:
        cases = (
            (b"a" * 65_534 + b"\npartial", b"a" * 65_534 + b"\n"),
            (b"a" * 65_535 + b"\npartial", b"a" * 65_535 + b"\n"),
            (b"a" * 65_536 + b"\npartial", b"a" * 65_536 + b"\n"),
            (b"a" * (3 * 65_536 + 17) + b"\nend", b"a" * (3 * 65_536 + 17) + b"\n"),
            (b"windows\r\npartial\r", b"windows\r\n"),
        )
        for source, expected in cases:
            with self.subTest(source_size=len(source), expected_size=len(expected)):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(source)
                receipt = self.sync()
                self.assertEqual(receipt["outcome"], "updated")
                self.assertEqual(self.destination.read_bytes(), expected)
                self.assertTrue(self.destination.read_bytes().endswith(b"\n"))
                self.assert_no_stage_names()

    def test_partial_publish_restores_mode_mtime_xattrs_and_empty_acl(self) -> None:
        self.write_source(b"complete\npartial", mode=0o640)
        expected_mtime = self.source.stat().st_mtime_ns
        attribute = "com.openai.codex-mirror-copy"
        set_attribute = subprocess.run(
            ["/usr/bin/xattr", "-w", attribute, "preserved", str(self.source)],
            capture_output=True,
            text=True,
        )
        if set_attribute.returncode != 0:
            self.skipTest(f"xattrs unavailable: {set_attribute.stderr}")

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "updated")
        self.assertEqual(self.destination.read_bytes(), b"complete\n")
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o640)
        self.assertEqual(self.destination.stat().st_mtime_ns, expected_mtime)
        value = subprocess.run(
            ["/usr/bin/xattr", "-p", attribute, str(self.destination)],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(value.rstrip(b"\n"), b"preserved")
        acl = subprocess.run(
            ["/bin/ls", "-lde", str(self.destination)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(len(acl.rstrip("\n").splitlines()), 1)

    def test_source_with_group_write_or_extended_acl_is_rejected(self) -> None:
        for unsafe_kind in ("mode", "acl"):
            with self.subTest(unsafe_kind=unsafe_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"unsafe\n")
                before = self.write_destination()
                if unsafe_kind == "mode":
                    os.chmod(self.source, 0o620)
                else:
                    self.add_extended_acl(self.source)
                receipt = self.sync()
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_acl_entry_calibration_and_destination_container_gate(self) -> None:
        self.write_source(b"acl-gate\n")
        before = self.write_destination()
        backend = self.backend_module.DarwinBackend()
        clean_fd = os.open(self.destination_parent, os.O_RDONLY)
        try:
            self.assertFalse(backend._acl_has_entries(clean_fd))
        finally:
            os.close(clean_fd)
        self.add_extended_acl(self.destination_parent)
        directory_fd = os.open(self.destination_parent, os.O_RDONLY)
        try:
            self.assertTrue(backend._acl_has_entries(directory_fd))
        finally:
            os.close(directory_fd)

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "unsafe_stage_container")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")

    def test_existing_destination_extended_acl_is_rejected(self) -> None:
        self.write_source(b"new\n")
        before = self.write_destination()
        self.add_extended_acl(self.destination)

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "exclusive_writer_required")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")

    def test_private_stage_extended_acl_after_creation_fails_closed(self) -> None:
        self.write_source(b"new\n")
        before = self.write_destination()

        def add_stage_acl(action: str) -> None:
            if action != "after_stage_create":
                return
            stages = self.stage_paths()
            self.assertEqual(len(stages), 1)
            self.add_extended_acl(stages[0])

        receipt = self.sync(action_hook=add_stage_acl)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertFalse(receipt["destination_mutated"])
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")

    def test_acl_free_drain_preserves_primary_and_aggregates_without_one(self) -> None:
        backend = self.backend_module.DarwinBackend()
        pointers = (
            ctypes.c_void_p(101),
            ctypes.c_void_p(202),
            ctypes.c_void_p(303),
        )
        attempts = []

        def fail_free(pointer: ctypes.c_void_p) -> int:
            attempts.append(pointer.value)
            ctypes.set_errno(errno.EIO)
            return -1

        primary = self.backend_module.BackendError(
            "sentinel_primary", "primary body failure"
        )

        def body_with_primary() -> None:
            try:
                raise primary
            except BaseException as exc:
                backend._free_acls(
                    (("first", pointers[0]), ("second", pointers[1])),
                    primary_error=exc,
                )
                raise

        with mock.patch.object(backend, "_acl_free", side_effect=fail_free):
            with self.assertRaises(self.backend_module.BackendError) as caught:
                body_with_primary()
        self.assertIs(caught.exception, primary)
        self.assertEqual(caught.exception.reason, "sentinel_primary")
        self.assertEqual(attempts, [101, 202])

        attempts.clear()
        with mock.patch.object(backend, "_acl_free", side_effect=fail_free):
            with self.assertRaises(self.backend_module.BackendError) as caught:
                backend._free_acls((("first", pointers[0]), ("second", pointers[1])))
        self.assertEqual(caught.exception.reason, "acl_free_failed")
        self.assertEqual(attempts, [101, 202])
        self.assertIn("first", caught.exception.detail)
        self.assertIn("second", caught.exception.detail)

        class FreeBomb(BaseException):
            pass

        primary_errors = (
            KeyboardInterrupt("ACL free keyboard interrupt"),
            SystemExit(93),
            FreeBomb(
                "ACL free custom marker: " + ("q" * (self.helper._DIAGNOSTIC_LIMIT * 2))
            ),
        )
        for free_error in primary_errors:
            with self.subTest(free_error=type(free_error).__name__):
                attempts.clear()
                primary = self.backend_module.BackendError(
                    "sentinel_primary",
                    "primary body failure",
                )
                original_args = primary.args
                traceback_at_cleanup = None

                def raise_first(pointer: ctypes.c_void_p) -> int:
                    attempts.append(pointer.value)
                    if len(attempts) == 1:
                        raise free_error
                    return 0

                def body_with_baseexception_cleanup() -> None:
                    nonlocal traceback_at_cleanup
                    try:
                        raise primary
                    except BaseException as exc:
                        traceback_at_cleanup = exc.__traceback__
                        backend._free_acls(
                            (
                                ("first", pointers[0]),
                                ("second", pointers[1]),
                                ("third", pointers[2]),
                            ),
                            primary_error=exc,
                        )
                        raise

                actual = None
                with mock.patch.object(
                    backend,
                    "_acl_free",
                    side_effect=raise_first,
                ):
                    try:
                        body_with_baseexception_cleanup()
                    except self.backend_module.BackendError as escaped:
                        actual = escaped
                    except BaseException as escaped:
                        self.fail(
                            "ACL cleanup replaced primary with "
                            f"{type(escaped).__name__}: {escaped}"
                        )
                    else:
                        self.fail("ACL primary unexpectedly disappeared")

                self.assertIs(actual, primary)
                self.assertEqual(actual.args, original_args)
                self.assertEqual(attempts, [101, 202, 303])
                self.assertIsNotNone(traceback_at_cleanup)
                traceback_cursor = actual.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is traceback_at_cleanup:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)
                cleanup_diagnostic = getattr(actual, "cleanup_diagnostic", "")
                self.assertIn(type(free_error).__name__, cleanup_diagnostic)
                self.assertLessEqual(
                    len(cleanup_diagnostic.encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                self.assertTrue(
                    any(
                        type(free_error).__name__ in note
                        for note in getattr(actual, "__notes__", ())
                    )
                )
                if isinstance(free_error, FreeBomb):
                    self.assertIn("ACL free custom marker", cleanup_diagnostic)
                    self.assertTrue(
                        cleanup_diagnostic.endswith(self.helper._TRUNCATED_MARKER)
                    )

        attempts.clear()

        def raise_multiple(pointer: ctypes.c_void_p) -> int:
            attempts.append(pointer.value)
            if pointer.value == 101:
                raise KeyboardInterrupt("aggregate ACL keyboard interrupt")
            if pointer.value == 303:
                raise FreeBomb("aggregate ACL custom baseexception")
            return 0

        with mock.patch.object(backend, "_acl_free", side_effect=raise_multiple):
            with self.assertRaises(self.backend_module.BackendError) as caught:
                backend._free_acls(
                    (
                        ("first", pointers[0]),
                        ("second", pointers[1]),
                        ("third", pointers[2]),
                    )
                )
        self.assertEqual(attempts, [101, 202, 303])
        self.assertEqual(caught.exception.reason, "acl_free_failed")
        self.assertIn("KeyboardInterrupt", caught.exception.detail)
        self.assertIn("FreeBomb", caught.exception.detail)

    def test_sync_receipt_includes_acl_finalizer_cleanup_diagnostic(self) -> None:
        backend_module = self.backend_module

        class AclCleanupBomb(BaseException):
            pass

        primary = backend_module.BackendError(
            "injected_acl_primary",
            "injected ACL body primary",
            errno.EIO,
        )
        cleanup_marker = "injected ACL finalizer cleanup marker: "
        cleanup_error = AclCleanupBomb(
            cleanup_marker + ("r" * (self.helper._DIAGNOSTIC_LIMIT * 2))
        )

        class AclFinalizerFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.injected = False

            def snapshot_policy(inner_self, fd: int) -> Any:
                if inner_self.injected:
                    return super().snapshot_policy(fd)
                inner_self.injected = True
                try:
                    raise primary
                except BaseException as exc:
                    inner_self._free_acls(
                        (("injected snapshot ACL", ctypes.c_void_p(404)),),
                        primary_error=exc,
                    )
                    raise

        backend = AclFinalizerFailureBackend()
        self.write_source(b"acl-finalizer-receipt\n")
        before_destination = self.write_destination()

        with mock.patch.object(backend, "_acl_free", side_effect=cleanup_error):
            receipt = self.sync(backend_factory=lambda: backend)

        self.assertTrue(backend.injected)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertIn(primary.detail, receipt["detail"])
        self.assertIn(type(cleanup_error).__name__, receipt["detail"])
        self.assertIn(cleanup_marker, receipt["detail"])
        self.assertTrue(receipt["detail"].endswith(self.helper._TRUNCATED_MARKER))
        self.assertLessEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        self.assertIn(
            type(cleanup_error).__name__,
            getattr(primary, "cleanup_diagnostic", ""),
        )
        validated = self.validate_receipt(receipt, 2)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_acl_bytes_primary_error_is_not_masked_by_free_failure(self) -> None:
        backend = self.backend_module.DarwinBackend()
        pointer = ctypes.c_void_p(303)
        attempts = []
        primary = self.backend_module.BackendError(
            "sentinel_primary", "ACL body failure"
        )

        def fail_free(acl: ctypes.c_void_p) -> int:
            attempts.append(acl.value)
            ctypes.set_errno(errno.EIO)
            return -1

        with mock.patch.object(backend, "_get_acl", return_value=pointer):
            with mock.patch.object(backend, "_acl_external", side_effect=primary):
                with mock.patch.object(backend, "_acl_free", side_effect=fail_free):
                    with self.assertRaises(self.backend_module.BackendError) as caught:
                        backend._acl_bytes_once(99)

        self.assertIs(caught.exception, primary)
        self.assertEqual(caught.exception.reason, "sentinel_primary")
        self.assertEqual(attempts, [303])

    def test_source_acl_or_xattr_snapshot_instability_is_deferred(self) -> None:
        for reason, method_name in (
            ("acl_unstable", "_snapshot_acl"),
            ("xattr_unstable", "_snapshot_xattrs"),
        ):
            with self.subTest(reason=reason):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"unstable-policy\n")
                before = self.write_destination()
                backend = self.backend_module.DarwinBackend()
                error = self.backend_module.BackendError(reason, "injected instability")
                with mock.patch.object(backend, method_name, side_effect=error):
                    receipt = self.sync(backend_factory=lambda: backend)

                self.assert_deferred(receipt)
                self.assertEqual(receipt["reason"], "source_unstable")
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_private_candidate_snapshot_instability_is_fatal_and_cleaned(self) -> None:
        backend_module = self.backend_module
        for reason in ("acl_unstable", "xattr_unstable"):
            with self.subTest(reason=reason):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"candidate-policy\n")
                before = self.write_destination()

                class CandidateUnstableBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.injected_candidate_fd = -1

                    def strict_clone(inner_self, *args: Any, **kwargs: Any) -> int:
                        candidate_fd = super().strict_clone(*args, **kwargs)
                        inner_self.injected_candidate_fd = candidate_fd
                        return candidate_fd

                    def snapshot_file(inner_self, fd: int) -> Any:
                        if fd == inner_self.injected_candidate_fd:
                            raise backend_module.BackendError(
                                reason, "injected private candidate instability"
                            )
                        return super().snapshot_file(fd)

                backend = CandidateUnstableBackend()
                receipt = self.sync(backend_factory=lambda: backend)

                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], reason)
                self.assertFalse(receipt["destination_mutated"])
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_initial_source_symlink_fifo_and_directory_do_not_block_or_publish(
        self,
    ) -> None:
        for source_kind in ("symlink", "fifo", "directory"):
            with self.subTest(source_kind=source_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                before = self.write_destination()
                sentinel = self.root / f"sentinel-{source_kind}"
                sentinel.write_bytes(b"sentinel\n")
                if source_kind == "symlink":
                    self.source.symlink_to(sentinel)
                elif source_kind == "fifo":
                    os.mkfifo(self.source, 0o600)
                else:
                    self.source.mkdir(mode=0o700)
                receipt = self.sync()
                self.assert_deferred(receipt)
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assertEqual(sentinel.read_bytes(), b"sentinel\n")

    def test_source_parent_access_policy_matrix(self) -> None:
        outside = self.root / "outside-sentinel"
        outside.write_bytes(b"outside\n")
        unsafe_flag = getattr(stat, "UF_IMMUTABLE", 0x00000002)
        for policy_kind in ("world_write", "group_write", "acl", "flags"):
            with self.subTest(policy_kind=policy_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"parent-policy\n")
                before = self.write_destination()
                try:
                    if policy_kind == "world_write":
                        os.chmod(self.source_parent, 0o777)
                    elif policy_kind == "group_write":
                        os.chmod(self.source_parent, 0o775)
                    elif policy_kind == "acl":
                        self.add_extended_acl(self.source_parent)
                    else:
                        os.chflags(self.source_parent, unsafe_flag)

                    receipt = self.sync()

                    self.assertEqual(receipt["outcome"], "fatal")
                    self.assertFalse(receipt["destination_mutated"])
                    self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                    self.assertEqual(self.destination.read_bytes(), b"old\n")
                    self.assertEqual(outside.read_bytes(), b"outside\n")
                    self.assert_no_stage_names()
                finally:
                    os.chflags(self.source_parent, 0)
                    self.clear_extended_acl(self.source_parent)
                    os.chmod(self.source_parent, 0o700)

        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(b"safe-parent\n")
        self.write_destination()
        os.chmod(self.source_parent, 0o755)

        receipt = self.sync()

        self.assertEqual(receipt["outcome"], "updated")
        self.assertEqual(receipt["method"], "reflink")
        self.assertEqual(self.destination.read_bytes(), b"safe-parent\n")
        self.assertEqual(outside.read_bytes(), b"outside\n")
        self.assert_no_stage_names()

    def test_hardlinked_source_or_destination_is_fatal(self) -> None:
        for hardlink_kind in ("source", "destination"):
            with self.subTest(hardlink_kind=hardlink_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"new\n")
                before = self.write_destination()
                alias = self.root / f"{hardlink_kind}-alias"
                target = self.source if hardlink_kind == "source" else self.destination
                os.link(target, alias)

                receipt = self.sync()

                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], "unsafe_link_count")
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()
                alias.unlink()

    def test_source_regular_leaf_replacement_is_deferred(self) -> None:
        original = b"original\n"
        self.write_source(original)
        before = self.write_destination()
        held = self.root / "held-source"

        def replace_source(action: str) -> None:
            if action == "after_source_open":
                os.replace(self.source, held)
                self.source.write_bytes(b"replacement\n")

        receipt = self.sync(action_hook=replace_source)

        self.assert_deferred(receipt)
        self.assertEqual(held.read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), b"replacement\n")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")

    def test_source_replacement_with_symlink_fifo_or_directory_is_deferred(
        self,
    ) -> None:
        for replacement_kind in ("symlink", "fifo", "directory"):
            with self.subTest(replacement_kind=replacement_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"original\n")
                before = self.write_destination()
                held = self.root / f"held-{replacement_kind}"
                sentinel = self.root / f"replacement-sentinel-{replacement_kind}"
                sentinel.write_bytes(b"sentinel\n")

                def replace_source(action: str) -> None:
                    if action != "after_source_open":
                        return
                    os.replace(self.source, held)
                    if replacement_kind == "symlink":
                        self.source.symlink_to(sentinel)
                    elif replacement_kind == "fifo":
                        os.mkfifo(self.source, 0o600)
                    else:
                        self.source.mkdir(mode=0o700)

                receipt = self.sync(action_hook=replace_source)
                self.assert_deferred(receipt)
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assertEqual(sentinel.read_bytes(), b"sentinel\n")
                if self.source.is_dir() and not self.source.is_symlink():
                    self.source.rmdir()
                else:
                    self.source.unlink()
                held.unlink()

    def test_source_parent_replacement_is_deferred(self) -> None:
        self.write_source(b"original\n")
        before = self.write_destination()
        displaced = self.root / "source-displaced"

        def replace_parent(action: str) -> None:
            if action == "after_source_open":
                os.replace(self.source_parent, displaced)
                self.source_parent.mkdir(mode=0o700)
                self.source.write_bytes(b"replacement\n")

        receipt = self.sync(action_hook=replace_parent)

        self.assert_deferred(receipt)
        self.assert_destination_snapshot((before.st_ino, b"old\n"))
        self.assertEqual((displaced / ROLLOUT_NAME).read_bytes(), b"original\n")

    def test_strict_clone_allows_source_append_but_publishes_staged_prefix(
        self,
    ) -> None:
        original = b"first\npartial"
        self.write_source(original)

        def append_after_clone(action: str) -> None:
            if action == "after_clone":
                with self.source.open("ab") as output:
                    output.write(b"-continued\nnew\n")

        receipt = self.sync(action_hook=append_after_clone)

        self.assertEqual(receipt["outcome"], "updated")
        self.assertEqual(receipt["method"], "reflink")
        self.assertEqual(self.destination.read_bytes(), b"first\n")
        self.assertEqual(self.source.read_bytes(), original + b"-continued\nnew\n")

    def test_source_access_policy_change_after_clone_is_deferred(self) -> None:
        self.write_source(b"policy\n")
        before = self.write_destination()

        def change_policy(action: str) -> None:
            if action == "after_clone":
                os.chmod(self.source, 0o640)

        receipt = self.sync(action_hook=change_policy)

        self.assert_deferred(receipt)
        self.assertEqual(receipt["reason"], "source_unstable")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_source_truncate_or_same_size_rewrite_after_clone_is_deferred(self) -> None:
        for mutation in ("truncate", "rewrite"):
            with self.subTest(mutation=mutation):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"first\nsecond\n")
                before = self.write_destination()

                def mutate(action: str) -> None:
                    if action != "after_clone":
                        return
                    if mutation == "truncate":
                        with self.source.open("r+b") as output:
                            output.truncate(3)
                    else:
                        self.source.write_bytes(b"other\nvalue!\n")

                receipt = self.sync(action_hook=mutate)
                self.assert_deferred(receipt)
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_compatibility_clone_errors_use_fcopyfile(self) -> None:
        compatibility_errnos = {errno.ENOTSUP, errno.EXDEV}
        if errno.EOPNOTSUPP not in compatibility_errnos:
            compatibility_errnos.add(errno.EOPNOTSUPP)
        for errno_value in sorted(compatibility_errnos):
            with self.subTest(errno_value=errno_value):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"fallback\n")
                backend = self.make_injected_clone_backend(errno_value)
                receipt = self.sync(backend_factory=lambda: backend)
                self.assertEqual(receipt["outcome"], "updated")
                self.assertEqual(receipt["method"], "copy")
                self.assertEqual(backend.fallback_calls, 1)
                self.assertEqual(self.destination.read_bytes(), b"fallback\n")
                self.assert_no_stage_names()

    def test_noncompatibility_clone_errors_are_fatal_without_fallback(self) -> None:
        for errno_value in (errno.EINVAL, errno.EIO, errno.EACCES):
            with self.subTest(errno_value=errno_value):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"fatal\n")
                before = self.write_destination()
                backend = self.make_injected_clone_backend(errno_value)
                receipt = self.sync(backend_factory=lambda: backend)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(backend.fallback_calls, 0)
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_partial_clone_child_blocks_fallback_and_is_cleaned(self) -> None:
        self.write_source(b"partial-child\n")
        before = self.write_destination()
        backend = self.make_injected_clone_backend(
            errno.ENOTSUP, create_partial_child=True
        )

        receipt = self.sync(backend_factory=lambda: backend)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(backend.fallback_calls, 0)
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_clone_content_invalid_is_deferred_and_cleaned(self) -> None:
        self.write_source(b"clone-content-race\n")
        before = self.write_destination()
        backend = self.make_injected_clone_backend(
            errno.EIO,
            clone_reason="clone_content_invalid",
        )

        receipt = self.sync(backend_factory=lambda: backend)

        self.assert_deferred(receipt)
        self.assertEqual(receipt["reason"], "source_unstable")
        self.assertEqual(backend.fallback_calls, 0)
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_ordinary_copy_policy_instability_is_deferred_and_cleaned(self) -> None:
        self.write_source(b"copy-policy-race\n")
        before = self.write_destination()
        copy_error = self.backend_module.BackendError(
            "policy_unstable", "injected ordinary-copy policy race"
        )
        backend = self.make_injected_clone_backend(
            errno.ENOTSUP,
            ordinary_copy_error=copy_error,
        )

        receipt = self.sync(backend_factory=lambda: backend)

        self.assert_deferred(receipt)
        self.assertEqual(receipt["reason"], "source_unstable")
        self.assertEqual(backend.fallback_calls, 1)
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_fcopyfile_fallback_rejects_source_append(self) -> None:
        self.write_source(b"fallback\n")
        before = self.write_destination()
        backend = self.make_injected_clone_backend(errno.ENOTSUP)

        def append_after_copy(action: str) -> None:
            if action == "after_clone":
                with self.source.open("ab") as output:
                    output.write(b"appended\n")

        receipt = self.sync(
            backend_factory=lambda: backend,
            action_hook=append_after_copy,
        )

        self.assert_deferred(receipt)
        self.assertEqual(backend.fallback_calls, 1)
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_destination_leaf_replacement_before_publish_fails_closed(self) -> None:
        self.write_source(b"new\n")
        before = self.write_destination()
        held = self.root / "held-destination"

        def replace_destination(action: str) -> None:
            if action == "before_publish":
                os.replace(self.destination, held)
                self.destination.write_bytes(b"attacker\n")

        receipt = self.sync(action_hook=replace_destination)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(held.stat().st_ino, before.st_ino)
        self.assertEqual(held.read_bytes(), b"old\n")
        self.assertEqual(self.destination.read_bytes(), b"attacker\n")

    def test_destination_typed_replacement_before_publish_fails_closed(self) -> None:
        for replacement_kind in ("symlink", "fifo", "directory"):
            with self.subTest(replacement_kind=replacement_kind):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                self.write_source(b"new\n")
                before = self.write_destination()
                held = self.root / f"held-destination-{replacement_kind}"
                sentinel = self.root / f"destination-sentinel-{replacement_kind}"
                sentinel.write_bytes(b"sentinel\n")

                def replace_destination(action: str) -> None:
                    if action != "before_publish":
                        return
                    os.replace(self.destination, held)
                    if replacement_kind == "symlink":
                        self.destination.symlink_to(sentinel)
                    elif replacement_kind == "fifo":
                        os.mkfifo(self.destination, 0o600)
                    else:
                        self.destination.mkdir(mode=0o700)

                receipt = self.sync(action_hook=replace_destination)

                self.assertEqual(receipt["outcome"], "fatal")
                self.assertFalse(receipt["destination_mutated"])
                self.assertEqual(held.stat().st_ino, before.st_ino)
                self.assertEqual(held.read_bytes(), b"old\n")
                self.assertEqual(sentinel.read_bytes(), b"sentinel\n")
                self.remove_path(self.destination)
                held.unlink()

    def test_absent_destination_created_before_publish_is_not_replaced(self) -> None:
        self.write_source(b"new\n")

        def create_destination(action: str) -> None:
            if action == "before_publish":
                self.destination.write_bytes(b"attacker\n")

        receipt = self.sync(action_hook=create_destination)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(self.destination.read_bytes(), b"attacker\n")

    def test_destination_parent_replacement_before_publish_fails_closed(self) -> None:
        self.write_source(b"new\n")
        self.write_destination()
        displaced = self.root / "mirror-displaced"

        def replace_parent(action: str) -> None:
            if action == "before_publish":
                os.replace(self.destination_parent, displaced)
                self.destination_parent.mkdir(mode=0o700)

        receipt = self.sync(action_hook=replace_parent)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual((displaced / ROLLOUT_NAME).read_bytes(), b"old\n")
        self.assertFalse(self.destination.exists())

    def test_publish_syscall_failure_with_before_orientation_cleans_stage(self) -> None:
        backend_module = self.backend_module

        class RenameFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self._renameatx_np = inner_self.fail_rename

            @staticmethod
            def fail_rename(*_args: Any) -> int:
                ctypes.set_errno(errno.EIO)
                return -1

        self.write_source(b"new\n")
        before = self.write_destination()
        backend = RenameFailureBackend()

        receipt = self.sync(backend_factory=lambda: backend)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "publish_ambiguous")
        self.assertFalse(receipt["destination_mutated"])
        self.assertEqual(receipt["new_identity"], receipt["old_identity"])
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assert_no_stage_names()

    def test_publish_ordinary_exceptions_are_oriented_before_cleanup(self) -> None:
        backend_module = self.backend_module

        for phase in ("pre_rename", "parent_fsync", "full_fsync", "postcondition"):
            with self.subTest(phase=phase):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                content = f"ordinary-publish-{phase}\n".encode()
                self.write_source(content)
                before_destination = self.write_destination()
                primary = RuntimeError(f"ordinary publish {phase} marker")
                transaction: Any = None

                class OrdinaryPublishBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.after_rename = False
                        inner_self.published_fullsync_done = False
                        inner_self.injected = False
                        rename = inner_self._renameatx_np

                        def wrapped_rename(*args: Any) -> int:
                            if phase == "pre_rename":
                                inner_self.injected = True
                                raise primary
                            result = rename(*args)
                            if result == 0:
                                inner_self.after_rename = True
                            return result

                        inner_self._renameatx_np = wrapped_rename

                    def publish_staged_name(
                        inner_self,
                        *args: Any,
                        **kwargs: Any,
                    ) -> Any:
                        self.assertTrue(transaction.publish_attempted)
                        return super().publish_staged_name(*args, **kwargs)

                    def fsync(inner_self, fd: int) -> None:
                        if (
                            phase == "parent_fsync"
                            and inner_self.after_rename
                            and not inner_self.injected
                        ):
                            inner_self.injected = True
                            raise primary
                        super().fsync(fd)

                    def full_fsync(inner_self, fd: int) -> None:
                        if (
                            phase == "full_fsync"
                            and inner_self.after_rename
                            and not inner_self.injected
                        ):
                            inner_self.injected = True
                            raise primary
                        super().full_fsync(fd)
                        if inner_self.after_rename:
                            inner_self.published_fullsync_done = True

                    def require_identity_at(
                        inner_self,
                        parent_fd: int,
                        name: str,
                        expected: Any,
                    ) -> Any:
                        if (
                            phase == "postcondition"
                            and inner_self.published_fullsync_done
                            and not inner_self.injected
                        ):
                            inner_self.injected = True
                            raise primary
                        return super().require_identity_at(
                            parent_fd,
                            name,
                            expected,
                        )

                backend = OrdinaryPublishBackend()
                transaction = self.helper.MirrorSync(backend)
                exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                    transaction
                )

                self.assertTrue(backend.injected)
                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], "unexpected_error")
                expected_primary_detail = getattr(primary, "detail", str(primary))
                self.assertIn(expected_primary_detail, receipt["detail"])
                if phase == "pre_rename":
                    self.assertFalse(receipt["destination_mutated"])
                    self.assertEqual(receipt["new_size"], receipt["old_size"])
                    self.assertEqual(
                        receipt["new_identity"],
                        receipt["old_identity"],
                    )
                    self.assertEqual(
                        self.destination.stat().st_ino,
                        before_destination.st_ino,
                    )
                    self.assertEqual(self.destination.read_bytes(), b"old\n")
                else:
                    self.assertTrue(receipt["destination_mutated"])
                    self.assertEqual(
                        receipt["new_size"],
                        receipt["publish_size"],
                    )
                    self.assertEqual(
                        receipt["new_identity"],
                        {
                            "dev": self.destination.stat().st_dev,
                            "ino": self.destination.stat().st_ino,
                        },
                    )
                    self.assertNotEqual(
                        self.destination.stat().st_ino,
                        before_destination.st_ino,
                    )
                    self.assertEqual(self.destination.read_bytes(), content)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assert_no_stage_names()

        self.remove_path(self.source)
        self.remove_path(self.destination)
        content = b"ordinary-publish-unknown\n"
        self.write_source(content)
        before_destination = self.write_destination()
        primary = RuntimeError("ordinary publish unknown marker")
        probe_error = KeyboardInterrupt("ordinary orientation probe interrupt")
        transaction = None

        class PublishedOrdinaryBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                rename = inner_self._renameatx_np

                def publish_then_fail(*args: Any) -> int:
                    result = rename(*args)
                    if result == 0:
                        raise primary
                    return result

                inner_self._renameatx_np = publish_then_fail

            def publish_staged_name(
                inner_self,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                self.assertTrue(transaction.publish_attempted)
                return super().publish_staged_name(*args, **kwargs)

        class ProbeFailureTransaction(self.helper.MirrorSync):
            def _prove_pre_publish_orientation(inner_self) -> bool:
                raise probe_error

            def _adopt_published_orientation_if_proved(inner_self) -> bool:
                raise probe_error

        transaction = ProbeFailureTransaction(PublishedOrdinaryBackend())
        exit_status, receipt, raw, stderr = self.run_transaction_via_main(transaction)

        self.assertEqual(exit_status, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "unexpected_error")
        self.assertIsNone(receipt["destination_mutated"])
        self.assertIsNone(receipt["new_size"])
        self.assertIsNone(receipt["new_identity"])
        self.assertIn(str(primary), receipt["detail"])
        self.assertIn("publish orientation probe failed", receipt["detail"])
        self.assertIn(type(probe_error).__name__, receipt["detail"])
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertNotEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), content)
        retained_stage = pathlib.Path(transaction.stage_path)
        self.assertTrue(retained_stage.is_dir())
        self.assertEqual(list(retained_stage.iterdir()), [])
        retained_stage.rmdir()

    def test_raw_publish_attempt_assignment_failure_is_oriented_before_cleanup(
        self,
    ) -> None:
        backend_module = self.backend_module

        class AssignmentBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("publish assignment keyboard interrupt"),
            lambda: SystemExit(103),
            lambda: AssignmentBomb("publish assignment custom BaseException"),
        )
        for make_primary in primary_factories:
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                content = f"assignment-window-{type(primary).__name__}\n".encode()
                self.write_source(content)
                before_destination = self.write_destination()

                class NeverCalledPublishBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.publish_calls = 0

                    def publish_staged_name(
                        inner_self,
                        *args: Any,
                        **kwargs: Any,
                    ) -> Any:
                        inner_self.publish_calls += 1
                        return super().publish_staged_name(*args, **kwargs)

                class AssignmentFailureTransaction(self.helper.MirrorSync):
                    def __init__(inner_self, backend: Any) -> None:
                        super().__init__(backend)
                        inner_self.inject_assignment_failure = True
                        inner_self.prove_results = []
                        inner_self.traceback_at_probe = None

                    def __setattr__(
                        inner_self,
                        name: str,
                        value: Any,
                    ) -> None:
                        super().__setattr__(name, value)
                        if (
                            name == "publish_attempted"
                            and value is True
                            and getattr(
                                inner_self,
                                "inject_assignment_failure",
                                False,
                            )
                        ):
                            inner_self.inject_assignment_failure = False
                            raise primary

                    def _prove_pre_publish_orientation(inner_self) -> bool:
                        inner_self.traceback_at_probe = primary.__traceback__
                        result = super()._prove_pre_publish_orientation()
                        inner_self.prove_results.append(result)
                        return result

                backend = NeverCalledPublishBackend()
                transaction = AssignmentFailureTransaction(backend)
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                escaped, stdout, stderr = self.run_raw_transaction_via_main(transaction)

                self.assertIs(escaped, primary)
                self.assertEqual(escaped.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(escaped.code, original_code)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")
                self.assertEqual(backend.publish_calls, 0)
                self.assertEqual(transaction.prove_results, [True])
                self.assertFalse(transaction.publish_attempted)
                self.assertIsNone(transaction.published_identity)
                self.assertIsNotNone(transaction.traceback_at_probe)
                traceback_cursor = escaped.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is transaction.traceback_at_probe:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assertTrue(transaction.stage_removed)
                self.assertFalse(pathlib.Path(transaction.stage_path).exists())
                self.assert_no_stage_names()
                for attribute in (
                    "source_parent_fd",
                    "source_fd",
                    "destination_parent_fd",
                    "destination_fd",
                    "stage_fd",
                    "candidate_fd",
                ):
                    self.assertEqual(getattr(transaction, attribute), -1)

    def test_raw_publish_unverified_orientation_retains_stage_evidence(
        self,
    ) -> None:
        backend_module = self.backend_module

        class UnverifiedPublishBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("unverified publish keyboard interrupt"),
            lambda: SystemExit(104),
            lambda: UnverifiedPublishBomb("unverified publish custom BaseException"),
        )
        for make_primary in primary_factories:
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                content = f"unverified-publish-{type(primary).__name__}\n".encode()
                self.write_source(content)
                before_destination = self.write_destination()

                class RawPublishFailureBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.publish_calls = 0

                    def publish_staged_name(
                        inner_self,
                        *_args: Any,
                        **_kwargs: Any,
                    ) -> Any:
                        inner_self.publish_calls += 1
                        raise primary

                class UnverifiedOrientationTransaction(self.helper.MirrorSync):
                    def __init__(inner_self, backend: Any) -> None:
                        super().__init__(backend)
                        inner_self.prove_results = []
                        inner_self.adopt_results = []
                        inner_self.traceback_at_probe = None

                    def _prove_pre_publish_orientation(inner_self) -> bool:
                        inner_self.traceback_at_probe = primary.__traceback__
                        inner_self.prove_results.append(False)
                        return False

                    def _adopt_published_orientation_if_proved(
                        inner_self,
                    ) -> bool:
                        inner_self.adopt_results.append(False)
                        return False

                backend = RawPublishFailureBackend()
                transaction = UnverifiedOrientationTransaction(backend)
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                escaped, stdout, stderr = self.run_raw_transaction_via_main(transaction)

                self.assertIs(escaped, primary)
                self.assertEqual(escaped.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(escaped.code, original_code)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")
                self.assertEqual(backend.publish_calls, 1)
                self.assertEqual(transaction.prove_results, [False])
                self.assertEqual(transaction.adopt_results, [False])
                self.assertTrue(transaction.publish_attempted)
                self.assertIsNone(transaction.published_identity)
                self.assertFalse(transaction.stage_removed)
                self.assertIsNotNone(transaction.traceback_at_probe)
                traceback_cursor = escaped.__traceback__
                preserved_traceback = False
                while traceback_cursor is not None:
                    if traceback_cursor is transaction.traceback_at_probe:
                        preserved_traceback = True
                        break
                    traceback_cursor = traceback_cursor.tb_next
                self.assertTrue(preserved_traceback)

                diagnostics = getattr(primary, "cleanup_diagnostics", ())
                self.assertTrue(
                    any(
                        diagnostic == "cleanup skipped; orientation unverified"
                        for diagnostic in diagnostics
                    )
                )
                self.assertTrue(
                    any(
                        note == "cleanup skipped; orientation unverified"
                        for note in getattr(primary, "__notes__", ())
                    )
                )
                self.assertTrue(
                    all(
                        len(diagnostic.encode("utf-8")) <= self.helper._DIAGNOSTIC_LIMIT
                        for diagnostic in diagnostics
                    )
                )
                self.assertEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                stage_path = pathlib.Path(transaction.stage_path)
                self.assertTrue(stage_path.is_dir())
                stage_stat = stage_path.stat()
                self.assertEqual(
                    (stage_stat.st_dev, stage_stat.st_ino),
                    (
                        transaction.stage_identity.dev,
                        transaction.stage_identity.ino,
                    ),
                )
                candidate_path = stage_path / "candidate.jsonl"
                self.assertTrue(candidate_path.is_file())
                candidate_stat = candidate_path.stat()
                self.assertEqual(
                    (candidate_stat.st_dev, candidate_stat.st_ino),
                    (
                        transaction.candidate_identity.dev,
                        transaction.candidate_identity.ino,
                    ),
                )
                self.assertEqual(candidate_path.read_bytes(), content)
                for attribute in (
                    "source_parent_fd",
                    "source_fd",
                    "destination_parent_fd",
                    "destination_fd",
                    "stage_fd",
                    "candidate_fd",
                ):
                    self.assertEqual(getattr(transaction, attribute), -1)

    def test_raw_publish_baseexceptions_are_oriented_cleaned_and_preserved(
        self,
    ) -> None:
        backend_module = self.backend_module

        class RawPublishBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("raw publish keyboard interrupt"),
            lambda: SystemExit(96),
            lambda: RawPublishBomb("raw publish custom BaseException"),
        )
        phases = (
            "authorize",
            "pre_rename",
            "post_rename",
            "parent_fsync",
            "full_fsync",
            "postcondition",
        )
        for phase in phases:
            for make_primary in primary_factories:
                primary = make_primary()
                with self.subTest(phase=phase, primary=type(primary).__name__):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    content = f"raw-publish-{phase}-{type(primary).__name__}\n".encode()
                    self.write_source(content)
                    before_destination = self.write_destination()
                    transaction: Any = None

                    class RawPublishBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            inner_self.after_rename = False
                            inner_self.published_fullsync_done = False
                            inner_self.injected = False
                            rename = inner_self._renameatx_np

                            def wrapped_rename(*args: Any) -> int:
                                if phase == "pre_rename":
                                    inner_self.injected = True
                                    raise primary
                                result = rename(*args)
                                if result == 0:
                                    inner_self.after_rename = True
                                    if phase == "post_rename":
                                        inner_self.injected = True
                                        raise primary
                                return result

                            inner_self._renameatx_np = wrapped_rename

                        def publish_staged_name(
                            inner_self,
                            *args: Any,
                            **kwargs: Any,
                        ) -> Any:
                            self.assertTrue(transaction.publish_attempted)
                            return super().publish_staged_name(*args, **kwargs)

                        def fsync(inner_self, fd: int) -> None:
                            if (
                                phase == "parent_fsync"
                                and inner_self.after_rename
                                and not inner_self.injected
                            ):
                                inner_self.injected = True
                                raise primary
                            super().fsync(fd)

                        def full_fsync(inner_self, fd: int) -> None:
                            if (
                                phase == "full_fsync"
                                and inner_self.after_rename
                                and not inner_self.injected
                            ):
                                inner_self.injected = True
                                raise primary
                            super().full_fsync(fd)
                            if inner_self.after_rename:
                                inner_self.published_fullsync_done = True

                        def require_identity_at(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            expected: Any,
                        ) -> Any:
                            if (
                                phase == "postcondition"
                                and inner_self.published_fullsync_done
                                and not inner_self.injected
                            ):
                                inner_self.injected = True
                                raise primary
                            return super().require_identity_at(
                                parent_fd,
                                name,
                                expected,
                            )

                    class RecordingTransaction(self.helper.MirrorSync):
                        def __init__(inner_self, backend: Any, **kwargs: Any) -> None:
                            super().__init__(backend, **kwargs)
                            inner_self.prove_results = []
                            inner_self.adopt_results = []
                            inner_self.traceback_at_orientation = None

                        def _prove_pre_publish_orientation(inner_self) -> bool:
                            inner_self.traceback_at_orientation = primary.__traceback__
                            result = super()._prove_pre_publish_orientation()
                            inner_self.prove_results.append(result)
                            return result

                        def _adopt_published_orientation_if_proved(
                            inner_self,
                        ) -> bool:
                            inner_self.traceback_at_orientation = primary.__traceback__
                            result = super()._adopt_published_orientation_if_proved()
                            inner_self.adopt_results.append(result)
                            return result

                    actions = []

                    def action_hook(action: str) -> None:
                        actions.append(action)
                        if phase == "authorize" and action == "authorize_publish":
                            backend.injected = True
                            raise primary

                    backend = RawPublishBackend()
                    transaction = RecordingTransaction(
                        backend,
                        action_hook=action_hook,
                    )
                    original_args = primary.args
                    original_code = getattr(primary, "code", None)
                    escaped, stdout, stderr = self.run_raw_transaction_via_main(
                        transaction
                    )

                    self.assertTrue(backend.injected)
                    self.assertIs(escaped, primary)
                    self.assertEqual(escaped.args, original_args)
                    if isinstance(primary, SystemExit):
                        self.assertEqual(escaped.code, original_code)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "")
                    self.assertIsNotNone(transaction.traceback_at_orientation)
                    traceback_cursor = escaped.__traceback__
                    preserved_traceback = False
                    while traceback_cursor is not None:
                        if traceback_cursor is transaction.traceback_at_orientation:
                            preserved_traceback = True
                            break
                        traceback_cursor = traceback_cursor.tb_next
                    self.assertTrue(preserved_traceback)
                    self.assertEqual(
                        getattr(primary, "cleanup_diagnostics", ()),
                        (),
                    )
                    for attribute in (
                        "source_parent_fd",
                        "source_fd",
                        "destination_parent_fd",
                        "destination_fd",
                        "stage_fd",
                        "candidate_fd",
                    ):
                        self.assertEqual(getattr(transaction, attribute), -1)

                    if phase in {"authorize", "pre_rename"}:
                        self.assertEqual(transaction.prove_results, [True])
                        self.assertEqual(transaction.adopt_results, [])
                        self.assertFalse(transaction.publish_attempted)
                        self.assertIsNone(transaction.published_identity)
                        self.assertEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), b"old\n")
                        self.assertIn("authorize_cleanup_child", actions)
                    else:
                        self.assertEqual(transaction.prove_results, [False])
                        self.assertEqual(transaction.adopt_results, [True])
                        self.assertTrue(transaction.publish_attempted)
                        self.assertIsNotNone(transaction.published_identity)
                        self.assertEqual(
                            (
                                self.destination.stat().st_dev,
                                self.destination.stat().st_ino,
                            ),
                            (
                                transaction.candidate_identity.dev,
                                transaction.candidate_identity.ino,
                            ),
                        )
                        self.assertNotEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), content)
                        self.assertNotIn("authorize_cleanup_child", actions)
                    self.assertIn("authorize_cleanup_stage", actions)
                    self.assertTrue(transaction.stage_removed)
                    self.assertFalse(pathlib.Path(transaction.stage_path).exists())

    def test_raw_publish_orientation_probe_failures_do_not_mask_primary(
        self,
    ) -> None:
        backend_module = self.backend_module

        class RawPublishBomb(BaseException):
            pass

        class RawProbeBomb(BaseException):
            pass

        pairs = (
            (
                lambda: KeyboardInterrupt("raw publish primary keyboard interrupt"),
                lambda: SystemExit(98),
            ),
            (
                lambda: SystemExit(99),
                lambda: RawProbeBomb(
                    "raw probe custom marker"
                    + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))
                ),
            ),
            (
                lambda: RawPublishBomb("raw publish custom primary"),
                lambda: KeyboardInterrupt("raw probe keyboard interrupt"),
            ),
        )
        for orientation in ("before", "published"):
            for make_primary, make_secondary in pairs:
                primary = make_primary()
                secondary = make_secondary()
                with self.subTest(
                    orientation=orientation,
                    primary=type(primary).__name__,
                    secondary=type(secondary).__name__,
                ):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    content = (
                        f"raw-probe-{orientation}-{type(primary).__name__}\n".encode()
                    )
                    self.write_source(content)
                    before_destination = self.write_destination()

                    class RawPublishPrimaryBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            rename = inner_self._renameatx_np

                            def inject_primary(*args: Any) -> int:
                                if orientation == "before":
                                    raise primary
                                result = rename(*args)
                                if result == 0:
                                    raise primary
                                return result

                            inner_self._renameatx_np = inject_primary

                    class ProbeFailureTransaction(self.helper.MirrorSync):
                        def __init__(inner_self, backend: Any) -> None:
                            super().__init__(backend)
                            inner_self.traceback_at_probe = None

                        def _prove_pre_publish_orientation(inner_self) -> bool:
                            if orientation == "before":
                                inner_self.traceback_at_probe = primary.__traceback__
                                raise secondary
                            return super()._prove_pre_publish_orientation()

                        def _adopt_published_orientation_if_proved(
                            inner_self,
                        ) -> bool:
                            inner_self.traceback_at_probe = primary.__traceback__
                            raise secondary

                    transaction = ProbeFailureTransaction(RawPublishPrimaryBackend())
                    original_args = primary.args
                    original_code = getattr(primary, "code", None)
                    escaped, stdout, stderr = self.run_raw_transaction_via_main(
                        transaction
                    )

                    self.assertIs(escaped, primary)
                    self.assertEqual(escaped.args, original_args)
                    if isinstance(primary, SystemExit):
                        self.assertEqual(escaped.code, original_code)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "")
                    self.assertTrue(transaction.publish_attempted)
                    self.assertIsNone(transaction.published_identity)
                    self.assertFalse(transaction.stage_removed)
                    self.assertIsNotNone(transaction.traceback_at_probe)
                    traceback_cursor = escaped.__traceback__
                    preserved_traceback = False
                    while traceback_cursor is not None:
                        if traceback_cursor is transaction.traceback_at_probe:
                            preserved_traceback = True
                            break
                        traceback_cursor = traceback_cursor.tb_next
                    self.assertTrue(preserved_traceback)
                    for attribute in (
                        "source_parent_fd",
                        "source_fd",
                        "destination_parent_fd",
                        "destination_fd",
                        "stage_fd",
                        "candidate_fd",
                    ):
                        self.assertEqual(getattr(transaction, attribute), -1)

                    diagnostics = getattr(primary, "cleanup_diagnostics", ())
                    self.assertTrue(
                        any(
                            "publish orientation probe failed" in diagnostic
                            and type(secondary).__name__ in diagnostic
                            for diagnostic in diagnostics
                        )
                    )
                    self.assertTrue(
                        all(
                            len(diagnostic.encode("utf-8"))
                            <= self.helper._DIAGNOSTIC_LIMIT
                            for diagnostic in diagnostics
                        )
                    )
                    if isinstance(secondary, RawProbeBomb):
                        self.assertTrue(
                            any(
                                self.helper._TRUNCATED_MARKER in diagnostic
                                for diagnostic in diagnostics
                            )
                        )

                    stage_path = pathlib.Path(transaction.stage_path)
                    self.assertTrue(stage_path.is_dir())
                    stage_stat = stage_path.stat()
                    self.assertEqual(
                        (stage_stat.st_dev, stage_stat.st_ino),
                        (
                            transaction.stage_identity.dev,
                            transaction.stage_identity.ino,
                        ),
                    )
                    if orientation == "before":
                        self.assertEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), b"old\n")
                        candidate_path = stage_path / "candidate.jsonl"
                        self.assertTrue(candidate_path.is_file())
                        candidate_stat = candidate_path.stat()
                        self.assertEqual(
                            (candidate_stat.st_dev, candidate_stat.st_ino),
                            (
                                transaction.candidate_identity.dev,
                                transaction.candidate_identity.ino,
                            ),
                        )
                        self.assertEqual(candidate_path.read_bytes(), content)
                    else:
                        self.assertEqual(
                            (
                                self.destination.stat().st_dev,
                                self.destination.stat().st_ino,
                            ),
                            (
                                transaction.candidate_identity.dev,
                                transaction.candidate_identity.ino,
                            ),
                        )
                        self.assertNotEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), content)
                        self.assertEqual(list(stage_path.iterdir()), [])

    def test_publish_orientation_probe_baseexceptions_preserve_primary_and_evidence(
        self,
    ) -> None:
        backend_module = self.backend_module

        class ProbeBomb(BaseException):
            pass

        secondary_errors = (
            lambda: KeyboardInterrupt("orientation probe keyboard interrupt"),
            lambda: SystemExit(97),
            lambda: ProbeBomb("orientation probe custom baseexception"),
        )
        for orientation in ("before", "published"):
            for make_secondary in secondary_errors:
                secondary = make_secondary()
                with self.subTest(
                    orientation=orientation,
                    secondary=type(secondary).__name__,
                ):
                    self.remove_path(self.source)
                    self.remove_path(self.destination)
                    content = (
                        f"probe-{orientation}-{type(secondary).__name__}\n".encode()
                    )
                    self.write_source(content)
                    before_destination = self.write_destination()
                    reason = (
                        "publish_ambiguous"
                        if orientation == "before"
                        else "publish_postcondition_unverified"
                    )
                    primary = backend_module.BackendError(
                        reason,
                        f"injected {orientation} publish primary",
                        errno.EIO,
                    )
                    original_args = primary.args

                    class PublishPrimaryBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            real_rename = inner_self._renameatx_np

                            def inject_primary(*args: Any) -> int:
                                if orientation == "before":
                                    raise primary
                                result = real_rename(*args)
                                if result == 0:
                                    raise primary
                                return result

                            inner_self._renameatx_np = inject_primary

                    probe_traceback = None

                    class ProbeFailureTransaction(self.helper.MirrorSync):
                        def _prove_pre_publish_orientation(inner_self) -> bool:
                            nonlocal probe_traceback
                            if orientation == "before":
                                probe_traceback = primary.__traceback__
                                raise secondary
                            return super()._prove_pre_publish_orientation()

                        def _adopt_published_orientation_if_proved(
                            inner_self,
                        ) -> bool:
                            nonlocal probe_traceback
                            if orientation == "published":
                                probe_traceback = primary.__traceback__
                                raise secondary
                            return super()._adopt_published_orientation_if_proved()

                    transaction = ProbeFailureTransaction(PublishPrimaryBackend())

                    exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                        transaction
                    )

                    self.assertEqual(exit_status, 2)
                    self.assertEqual(stderr, "")
                    self.assertEqual(receipt["outcome"], "fatal")
                    self.assertEqual(receipt["reason"], primary.reason)
                    self.assertIsNone(receipt["destination_mutated"])
                    self.assertIsNone(receipt["new_size"])
                    self.assertIsNone(receipt["new_identity"])
                    self.assertIn(primary.detail, receipt["detail"])
                    self.assertIn("publish orientation probe failed", receipt["detail"])
                    self.assertIn(type(secondary).__name__, receipt["detail"])
                    self.assertIn(str(secondary), receipt["detail"])
                    validated = self.validate_receipt(receipt, 2, raw_input=raw)
                    self.assertEqual(validated.returncode, 0, validated.stderr)

                    self.assertEqual(primary.args, original_args)
                    self.assertIsNotNone(probe_traceback)
                    traceback_cursor = primary.__traceback__
                    preserved_traceback = False
                    while traceback_cursor is not None:
                        if traceback_cursor is probe_traceback:
                            preserved_traceback = True
                            break
                        traceback_cursor = traceback_cursor.tb_next
                    self.assertTrue(preserved_traceback)
                    primary_diagnostics = getattr(
                        primary,
                        "cleanup_diagnostics",
                        (),
                    )
                    self.assertTrue(
                        any(
                            "publish orientation probe failed" in diagnostic
                            and type(secondary).__name__ in diagnostic
                            for diagnostic in primary_diagnostics
                        )
                    )

                    stage_path = pathlib.Path(transaction.stage_path)
                    self.assertTrue(stage_path.is_dir())
                    stage_stat = stage_path.stat()
                    self.assertEqual(
                        (stage_stat.st_dev, stage_stat.st_ino),
                        (
                            transaction.stage_identity.dev,
                            transaction.stage_identity.ino,
                        ),
                    )
                    if orientation == "before":
                        self.assertEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), b"old\n")
                        candidate_path = stage_path / "candidate.jsonl"
                        self.assertTrue(candidate_path.is_file())
                        candidate_stat = candidate_path.stat()
                        self.assertEqual(
                            (candidate_stat.st_dev, candidate_stat.st_ino),
                            (
                                transaction.candidate_identity.dev,
                                transaction.candidate_identity.ino,
                            ),
                        )
                        self.assertEqual(candidate_path.read_bytes(), content)
                    else:
                        self.assertNotEqual(
                            self.destination.stat().st_ino,
                            before_destination.st_ino,
                        )
                        self.assertEqual(self.destination.read_bytes(), content)
                        self.assertEqual(
                            (
                                self.destination.stat().st_dev,
                                self.destination.stat().st_ino,
                            ),
                            (
                                transaction.candidate_identity.dev,
                                transaction.candidate_identity.ino,
                            ),
                        )
                        self.assertEqual(list(stage_path.iterdir()), [])

    def test_published_orientation_adoption_assignment_is_receipt_atomic(
        self,
    ) -> None:
        backend_module = self.backend_module

        class AdoptionAssignmentBomb(BaseException):
            pass

        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                content = f"adoption-assignment-{phase}\n".encode()
                self.write_source(content)
                before_destination = self.write_destination()
                primary = backend_module.BackendError(
                    "publish_postcondition_unverified",
                    f"injected adoption assignment {phase} primary",
                    errno.EIO,
                )
                assignment_error = AdoptionAssignmentBomb(
                    f"adoption assignment {phase} interrupt"
                )

                class PublishedPrimaryBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        rename = inner_self._renameatx_np

                        def publish_then_fail(*args: Any) -> int:
                            result = rename(*args)
                            if result == 0:
                                raise primary
                            return result

                        inner_self._renameatx_np = publish_then_fail

                class AssignmentFailureTransaction(self.helper.MirrorSync):
                    def __init__(inner_self, backend: Any) -> None:
                        inner_self.assignment_armed = False
                        super().__init__(backend)

                    def __setattr__(
                        inner_self,
                        name: str,
                        value: Any,
                    ) -> None:
                        if name == "published_identity" and getattr(
                            inner_self,
                            "assignment_armed",
                            False,
                        ):
                            if phase == "before":
                                raise assignment_error
                            super().__setattr__(name, value)
                            raise assignment_error
                        super().__setattr__(name, value)

                    def _adopt_published_orientation_if_proved(
                        inner_self,
                    ) -> bool:
                        inner_self.assignment_armed = True
                        try:
                            return super()._adopt_published_orientation_if_proved()
                        finally:
                            inner_self.assignment_armed = False

                transaction = AssignmentFailureTransaction(PublishedPrimaryBackend())
                exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                    transaction
                )

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr, "")
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], primary.reason)
                self.assertIn(type(assignment_error).__name__, receipt["detail"])
                self.assertNotEqual(
                    (
                        receipt["destination_mutated"],
                        receipt["new_identity"],
                    ),
                    (True, None),
                )
                if phase == "before":
                    self.assertIsNone(receipt["destination_mutated"])
                    self.assertIsNone(receipt["new_size"])
                    self.assertIsNone(receipt["new_identity"])
                else:
                    self.assertTrue(receipt["destination_mutated"])
                    self.assertEqual(receipt["new_size"], len(content))
                    self.assertEqual(
                        receipt["new_identity"],
                        {
                            "dev": self.destination.stat().st_dev,
                            "ino": self.destination.stat().st_ino,
                        },
                    )
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertNotEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), content)
                stage_path = pathlib.Path(transaction.stage_path)
                if phase == "before":
                    self.assertTrue(stage_path.is_dir())
                    self.assertEqual(list(stage_path.iterdir()), [])
                    stage_path.rmdir()
                else:
                    self.assert_no_stage_names()

    def test_postcondition_failure_with_published_orientation_cleans_stage(
        self,
    ) -> None:
        backend_module = self.backend_module

        class PostconditionFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                rename = inner_self._renameatx_np
                inner_self.inject_postcondition_failure = False

                def wrapped_rename(*args: Any) -> int:
                    result = rename(*args)
                    if result == 0:
                        inner_self.inject_postcondition_failure = True
                    return result

                inner_self._renameatx_np = wrapped_rename

            def require_identity_at(
                inner_self,
                parent_fd: int,
                name: str,
                expected: Any,
            ) -> Any:
                if inner_self.inject_postcondition_failure:
                    inner_self.inject_postcondition_failure = False
                    raise backend_module.BackendError(
                        "injected_postcondition",
                        "injected one-shot published identity failure",
                    )
                return super().require_identity_at(parent_fd, name, expected)

        self.write_source(b"published-despite-postcheck\n")
        before = self.write_destination()
        backend = PostconditionFailureBackend()

        receipt = self.sync(backend_factory=lambda: backend)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "publish_postcondition_unverified")
        self.assertTrue(receipt["destination_mutated"])
        self.assertNotEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(
            self.destination.read_bytes(), b"published-despite-postcheck\n"
        )
        self.assert_no_stage_names()

    def test_publish_durability_order(self) -> None:
        self.write_source(b"durable-order\n")
        self.write_destination()
        backend = self.make_publish_order_backend()

        receipt = self.sync(backend_factory=lambda: backend)

        self.assertEqual(receipt["outcome"], "updated")
        events = backend.events
        candidate_flush = events.index("candidate_fullfsync")
        rename = events.index("rename")
        parent_flushes = [
            index for index, event in enumerate(events) if event == "parent_fsync"
        ]
        published_flush = events.index("published_fullfsync")
        final_check = events.index("final_identity_postcheck")
        stage_check = events.index("stage_absence_postcheck")
        self.assertLess(candidate_flush, rename)
        self.assertGreaterEqual(len(parent_flushes), 2)
        self.assertGreaterEqual(
            sum(rename < index < published_flush for index in parent_flushes),
            2,
        )
        self.assertLess(published_flush, final_check)
        self.assertLess(published_flush, stage_check)
        self.assertEqual(self.destination.read_bytes(), b"durable-order\n")
        self.assert_no_stage_names()

    def test_published_inode_fullfsync_failure_is_fatal_but_committed(self) -> None:
        self.write_source(b"published-before-flush-error\n")
        before = self.write_destination()
        outside = self.root / "flush-failure-sentinel"
        outside.write_bytes(b"sentinel\n")
        backend = self.make_publish_order_backend(fail_published_fullsync=True)

        receipt = self.sync(backend_factory=lambda: backend)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "publish_postcondition_unverified")
        self.assertTrue(receipt["destination_mutated"])
        self.assertNotEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(
            receipt["new_identity"],
            {
                "dev": self.destination.stat().st_dev,
                "ino": self.destination.stat().st_ino,
            },
        )
        self.assertEqual(
            self.destination.read_bytes(), b"published-before-flush-error\n"
        )
        self.assertEqual(outside.read_bytes(), b"sentinel\n")
        self.assertIn("published_fullfsync", backend.events)
        self.assert_no_stage_names()

    def test_published_fd_cleanup_diagnostic_survives_publish_wrapper(self) -> None:
        backend_module = self.backend_module

        class PublishedCloseFailure(BaseException):
            pass

        marker = "published-fd-close-cause-marker"
        close_error = PublishedCloseFailure(marker)

        class FullSyncAndCloseFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.after_rename = False
                inner_self.published_fd = -1
                inner_self.close_armed = False
                inner_self.close_injected = False
                rename = inner_self._renameatx_np

                def wrapped_rename(*args: Any) -> int:
                    result = rename(*args)
                    if result == 0:
                        inner_self.after_rename = True
                    return result

                inner_self._renameatx_np = wrapped_rename

            def full_fsync(inner_self, fd: int) -> None:
                if not inner_self.after_rename:
                    super().full_fsync(fd)
                    return
                inner_self.published_fd = fd
                inner_self.close_armed = True
                raise backend_module.BackendError(
                    "full_fsync_failed",
                    "injected published inode full sync failure",
                    errno.EIO,
                )

        self.write_source(b"published-close-cause\n")
        before_destination = self.write_destination()
        backend = FullSyncAndCloseFailureBackend()
        transaction = self.helper.MirrorSync(backend)
        real_close = os.close

        def close_with_published_failure(fd: int) -> None:
            real_close(fd)
            if (
                backend.close_armed
                and not backend.close_injected
                and fd == backend.published_fd
            ):
                backend.close_injected = True
                backend.close_armed = False
                raise close_error

        with mock.patch.object(
            backend_module.os,
            "close",
            side_effect=close_with_published_failure,
        ):
            exit_status, receipt, raw, stderr = self.run_transaction_via_main(
                transaction
            )

        self.assertTrue(backend.close_injected)
        self.assertEqual(exit_status, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], "publish_postcondition_unverified")
        self.assertTrue(receipt["destination_mutated"])
        self.assertIn("full sync failure", receipt["detail"])
        self.assertIn(type(close_error).__name__, receipt["detail"])
        self.assertIn(marker, receipt["detail"])
        self.assertLessEqual(
            len(receipt["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertNotEqual(
            self.destination.stat().st_ino,
            before_destination.st_ino,
        )
        self.assertEqual(self.destination.read_bytes(), b"published-close-cause\n")
        self.assert_no_stage_names()

    def test_stage_leaf_replacement_before_publish_fails_closed(self) -> None:
        self.write_source(b"new\n")
        before = self.write_destination()
        held = self.root / "held-stage-child"

        def replace_stage_child(action: str) -> None:
            if action != "before_publish":
                return
            stage_dirs = self.stage_paths()
            self.assertEqual(len(stage_dirs), 1)
            children = list(stage_dirs[0].iterdir())
            self.assertEqual(len(children), 1)
            os.replace(children[0], held)
            children[0].write_bytes(b"attacker-stage\n")

        receipt = self.sync(action_hook=replace_stage_child)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assertNotEqual(held.read_bytes(), b"attacker-stage\n")

    def test_stage_directory_replacement_fails_closed_without_touching_sentinel(
        self,
    ) -> None:
        self.write_source(b"new\n")
        before = self.write_destination()
        sentinel_dir = self.root / "sentinel-dir"
        sentinel_dir.mkdir(mode=0o700)
        sentinel = sentinel_dir / "sentinel"
        sentinel.write_bytes(b"sentinel\n")
        held_stage = self.root / "held-stage"

        def replace_stage(action: str) -> None:
            if action != "after_stage_create":
                return
            stage_dirs = self.stage_paths()
            self.assertEqual(len(stage_dirs), 1)
            stage = stage_dirs[0]
            os.replace(stage, held_stage)
            stage.symlink_to(sentinel_dir, target_is_directory=True)

        receipt = self.sync(action_hook=replace_stage)

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertEqual(self.destination.read_bytes(), b"old\n")
        self.assertEqual(sentinel.read_bytes(), b"sentinel\n")
        self.assertEqual(list(held_stage.iterdir()), [])

    def test_validate_receipt_partial_semantics_and_ambiguous_fatal_matrix(
        self,
    ) -> None:
        receipts = []
        self.write_source(b"full-record\n")
        self.write_destination()
        full_updated = self.sync()
        full_unchanged = self.sync()
        receipts.extend((full_updated, full_unchanged))

        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(b"complete-record\npartial-tail")
        self.write_destination()
        partial_updated = self.sync()
        partial_unchanged = self.sync()
        receipts.extend((partial_updated, partial_unchanged))

        expected = (
            ("updated", False, "captured-source-policy"),
            ("unchanged", False, "captured-source-policy"),
            ("updated", True, "captured-pre-truncate-source-policy"),
            ("unchanged", True, "captured-pre-truncate-source-policy"),
        )
        for receipt, (outcome, partial, mtime_semantics) in zip(
            receipts,
            expected,
        ):
            with self.subTest(
                outcome=outcome,
                partial=partial,
                valid=True,
            ):
                self.assertEqual(receipt["outcome"], outcome)
                self.assertIs(receipt["partial"], partial)
                self.assertEqual(receipt["mtime_semantics"], mtime_semantics)
                validated = self.validate_receipt(receipt, 0)
                self.assertEqual(validated.returncode, 0, validated.stderr)

            invalid = dict(receipt)
            invalid["mtime_semantics"] = (
                "captured-source-policy"
                if partial
                else "captured-pre-truncate-source-policy"
            )
            with self.subTest(
                outcome=outcome,
                partial=partial,
                valid=False,
            ):
                rejected = self.validate_receipt(invalid, 0)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(
                    b"receipt partial and mtime semantics disagree",
                    rejected.stderr,
                )
            if partial:
                equal_source_size = dict(receipt)
                equal_source_size["source_size"] = receipt["publish_size"]
                with self.subTest(
                    outcome=outcome,
                    partial=partial,
                    source_size_equals_publish=True,
                ):
                    rejected = self.validate_receipt(equal_source_size, 0)
                    self.assertEqual(rejected.returncode, 2)

        self.assertIsNotNone(full_updated["old_identity"])
        self.assertNotEqual(
            full_updated["new_identity"],
            full_updated["old_identity"],
        )
        updated_claims_old_identity = dict(full_updated)
        updated_claims_old_identity["new_identity"] = full_updated["old_identity"]
        rejected = self.validate_receipt(updated_claims_old_identity, 0)
        self.assertEqual(rejected.returncode, 2)

        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(b"absent-destination\n")
        absent_updated = self.sync()
        self.assertEqual(absent_updated["outcome"], "updated")
        self.assertIsNone(absent_updated["old_size"])
        self.assertIsNone(absent_updated["old_identity"])
        self.assertIsNotNone(absent_updated["new_identity"])
        accepted = self.validate_receipt(absent_updated, 0)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for unchanged in (full_unchanged, partial_unchanged):
            with self.subTest(
                partial=unchanged["partial"],
                unchanged_sizes_valid=True,
            ):
                self.assertEqual(unchanged["old_size"], unchanged["new_size"])
                self.assertEqual(
                    unchanged["new_size"],
                    unchanged["publish_size"],
                )
                self.assertEqual(
                    unchanged["old_identity"],
                    unchanged["new_identity"],
                )
                accepted = self.validate_receipt(unchanged, 0)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)

            invalid_payloads = []
            publish_mismatch = dict(unchanged)
            publish_mismatch["publish_size"] = unchanged["publish_size"] - 1
            invalid_payloads.append(("publish_size", publish_mismatch))
            old_size_mismatch = dict(unchanged)
            old_size_mismatch["old_size"] = unchanged["old_size"] - 1
            invalid_payloads.append(("old_size", old_size_mismatch))
            new_size_mismatch = dict(unchanged)
            new_size_mismatch["new_size"] = unchanged["new_size"] - 1
            invalid_payloads.append(("new_size", new_size_mismatch))
            identity_mismatch = dict(unchanged)
            identity_mismatch["new_identity"] = {
                **unchanged["new_identity"],
                "ino": unchanged["new_identity"]["ino"] + 1,
            }
            invalid_payloads.append(("identity", identity_mismatch))
            for mismatch, payload in invalid_payloads:
                with self.subTest(
                    partial=unchanged["partial"],
                    mismatch=mismatch,
                ):
                    rejected = self.validate_receipt(payload, 0)
                    self.assertEqual(rejected.returncode, 2)

        self.remove_path(self.source)
        self.remove_path(self.destination)
        self.write_source(b"ambiguous-fatal\n")
        self.write_destination()
        fatal_primary = self.backend_module.BackendError(
            "injected_ambiguous_validator_fatal",
            "injected fatal after destination binding",
            errno.EIO,
        )

        def fail_after_destination_bind(action: str) -> None:
            if action == "after_destination_bind":
                raise fatal_primary

        fatal = self.sync(action_hook=fail_after_destination_bind)
        self.assertEqual(fatal["outcome"], "fatal")
        self.assertIsNotNone(fatal["old_size"])
        self.assertIsNotNone(fatal["old_identity"])
        ambiguous = dict(fatal)
        ambiguous.update(
            destination_mutated=None,
            new_size=None,
            new_identity=None,
        )
        accepted = self.validate_receipt(ambiguous, 2)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        invalid_ambiguous = dict(ambiguous)
        invalid_ambiguous.update(
            new_size=fatal["old_size"],
            new_identity=fatal["old_identity"],
        )
        rejected = self.validate_receipt(invalid_ambiguous, 2)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(
            b"ambiguous fatal receipt cannot claim new destination evidence",
            rejected.stderr,
        )

        self.remove_path(self.source)
        self.remove_path(self.destination)
        mutated_fatal_source = b"mutated-fatal-complete\npartial-tail"
        self.write_source(mutated_fatal_source)
        self.write_destination()
        fatal_backend = self.make_publish_order_backend(fail_published_fullsync=True)
        mutated_fatal = self.sync(backend_factory=lambda: fatal_backend)
        self.assertEqual(mutated_fatal["outcome"], "fatal")
        self.assertTrue(mutated_fatal["destination_mutated"])
        self.assertEqual(
            mutated_fatal["new_size"],
            mutated_fatal["publish_size"],
        )
        self.assertGreater(mutated_fatal["publish_size"], 0)
        self.assertGreater(
            mutated_fatal["source_size"],
            mutated_fatal["publish_size"],
        )
        self.assertIn(mutated_fatal["method"], {"reflink", "copy"})
        self.assertTrue(mutated_fatal["partial"])
        self.assertEqual(
            mutated_fatal["mtime_semantics"],
            "captured-pre-truncate-source-policy",
        )
        accepted = self.validate_receipt(mutated_fatal, 2)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        invalid_mutated_fatal = []
        new_size_mismatch = dict(mutated_fatal)
        new_size_mismatch["new_size"] = mutated_fatal["publish_size"] - 1
        invalid_mutated_fatal.append(("new_size", new_size_mismatch))
        new_identity_is_old = dict(mutated_fatal)
        new_identity_is_old["new_identity"] = mutated_fatal["old_identity"]
        invalid_mutated_fatal.append(("new_identity_is_old", new_identity_is_old))
        zero_publish = dict(mutated_fatal)
        zero_publish["publish_size"] = 0
        zero_publish["new_size"] = 0
        invalid_mutated_fatal.append(("zero_publish", zero_publish))
        for field in ("method", "partial", "mtime_semantics", "source_size"):
            missing = dict(mutated_fatal)
            missing[field] = None
            invalid_mutated_fatal.append((f"missing_{field}", missing))
        partial_without_tail = dict(mutated_fatal)
        partial_without_tail["source_size"] = mutated_fatal["publish_size"]
        invalid_mutated_fatal.append(("partial_without_tail", partial_without_tail))
        for mismatch, payload in invalid_mutated_fatal:
            with self.subTest(mutated_fatal_mismatch=mismatch):
                rejected = self.validate_receipt(payload, 2)
                self.assertEqual(rejected.returncode, 2)

    def test_validate_receipt_accepts_every_outcome_and_exit_mapping(self) -> None:
        updated = self.updated_receipt()
        unchanged = self.sync()
        self.assertEqual(unchanged["outcome"], "unchanged")

        self.write_source(b"no-complete-line")
        no_complete = self.sync()
        self.assertEqual(no_complete["outcome"], "no-complete-line")

        held = self.root / "validator-held-source"

        def replace_source(action: str) -> None:
            if action == "after_source_open":
                os.replace(self.source, held)
                self.source.write_bytes(b"replacement\n")

        deferred = self.sync(action_hook=replace_source)
        self.assertEqual(deferred["outcome"], "deferred")
        self.remove_path(self.source)
        os.replace(held, self.source)
        os.chmod(self.source, 0o622)
        fatal = self.sync()
        self.assertEqual(fatal["outcome"], "fatal")

        cases = (
            (updated, 0),
            (unchanged, 0),
            (no_complete, 0),
            (deferred, 75),
            (fatal, 2),
        )
        for receipt, exit_status in cases:
            with self.subTest(outcome=receipt["outcome"]):
                completed = self.validate_receipt(receipt, exit_status)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                fields = completed.stdout.decode("utf-8").rstrip("\n").split("\t")
                self.assertEqual(fields[0], receipt["outcome"])
                self.assertEqual(len(fields), 4)

                wrong_exit = 2 if exit_status != 2 else 0
                rejected = self.validate_receipt(receipt, wrong_exit)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b"outcome does not match exit status", rejected.stderr)

    def test_validate_receipt_rejects_trailing_or_oversized_input(self) -> None:
        receipt = self.updated_receipt()
        encoded = json.dumps(receipt, sort_keys=True).encode("utf-8")
        cases = (
            (encoded + b"\n{}", b"trailing data"),
            (b" " * (64 * 1024 + 1), b"exceeds 64 KiB"),
        )
        for raw_input, message in cases:
            with self.subTest(message=message):
                completed = self.validate_receipt(
                    receipt,
                    0,
                    raw_input=raw_input,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn(message, completed.stderr)

    def test_validate_receipt_rejects_path_or_schema_mismatch(self) -> None:
        receipt = self.updated_receipt()
        mismatched_path = self.validate_receipt(
            receipt,
            0,
            source=str(self.source) + ".different",
        )
        self.assertEqual(mismatched_path.returncode, 2)
        self.assertIn(b"paths do not match", mismatched_path.stderr)

        invalid_payloads = []
        unknown = dict(receipt)
        unknown["outcome"] = "unknown"
        invalid_payloads.append(unknown)
        missing = dict(receipt)
        missing.pop("detail")
        invalid_payloads.append(missing)
        extra = dict(receipt)
        extra["extra"] = None
        invalid_payloads.append(extra)
        for payload in invalid_payloads:
            with self.subTest(keys=sorted(payload), outcome=payload.get("outcome")):
                completed = self.validate_receipt(payload, 0)
                self.assertEqual(completed.returncode, 2)

    def test_validate_receipt_rejects_nonmutation_evidence_mismatch(self) -> None:
        self.updated_receipt()
        unchanged = self.sync()
        self.assertEqual(unchanged["outcome"], "unchanged")
        mismatches = []
        size_mismatch = json.loads(json.dumps(unchanged))
        size_mismatch["new_size"] = size_mismatch["old_size"] + 1
        mismatches.append(size_mismatch)
        identity_mismatch = json.loads(json.dumps(unchanged))
        identity_mismatch["new_identity"]["ino"] += 1
        mismatches.append(identity_mismatch)
        for payload in mismatches:
            completed = self.validate_receipt(payload, 0)
            self.assertEqual(completed.returncode, 2)
            self.assertIn(b"must preserve destination evidence", completed.stderr)

    def test_validate_receipt_rejects_reason_contract_violations(self) -> None:
        success = self.updated_receipt()
        success["reason"] = "impossible_success_reason"
        completed = self.validate_receipt(success, 0)
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"successful receipt cannot", completed.stderr)

        os.chmod(self.source, 0o622)
        failure = self.sync()
        self.assertEqual(failure["outcome"], "fatal")
        for missing_field in ("reason", "detail"):
            payload = dict(failure)
            payload[missing_field] = None
            with self.subTest(missing_field=missing_field):
                rejected = self.validate_receipt(payload, 2)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b"must include reason and detail", rejected.stderr)

    def test_validate_receipt_enforces_utf8_detail_byte_limit(self) -> None:
        self.write_source(b"validator detail byte limit\n")
        self.write_destination(b"validator detail old\n")
        os.chmod(self.source, 0o622)
        producer = self.sync()
        self.assertEqual(producer["outcome"], "fatal")
        producer_raw = (json.dumps(producer, sort_keys=True) + "\n").encode()
        self.assertLess(len(producer_raw), 64 * 1024)
        accepted_producer = self.validate_receipt(
            producer,
            2,
            raw_input=producer_raw,
        )
        self.assertEqual(
            accepted_producer.returncode,
            0,
            accepted_producer.stderr,
        )

        exact_limit = dict(producer)
        exact_limit["detail"] = "界" * 1365 + "a"
        self.assertEqual(
            len(exact_limit["detail"].encode("utf-8")),
            self.helper._DIAGNOSTIC_LIMIT,
        )
        exact_raw = (json.dumps(exact_limit, sort_keys=True) + "\n").encode()
        self.assertLess(len(exact_raw), 64 * 1024)
        accepted_limit = self.validate_receipt(
            exact_limit,
            2,
            raw_input=exact_raw,
        )
        self.assertEqual(accepted_limit.returncode, 0, accepted_limit.stderr)

        over_limit_cases = (
            ("ascii", "a" * (self.helper._DIAGNOSTIC_LIMIT + 1)),
            ("multibyte", "界" * 1366),
        )
        for case, detail in over_limit_cases:
            with self.subTest(case=case):
                self.assertGreater(
                    len(detail.encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                payload = dict(producer)
                payload["detail"] = detail
                raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
                self.assertLess(len(raw), 64 * 1024)
                rejected = self.validate_receipt(payload, 2, raw_input=raw)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b"detail", rejected.stderr)
                self.assertIn(b"4 KiB", rejected.stderr)

    def test_validate_receipt_rejects_incomplete_outcome_evidence(self) -> None:
        updated = self.updated_receipt()
        unchanged = self.sync()
        self.assertEqual(unchanged["outcome"], "unchanged")
        self.write_source(b"no-complete-line")
        no_complete = self.sync()
        self.assertEqual(no_complete["outcome"], "no-complete-line")

        invalid_payloads = []
        for field, value in (
            ("publish_size", 0),
            ("source_size", 0),
            ("partial", None),
            ("mtime_semantics", None),
            ("new_identity", None),
        ):
            payload = json.loads(json.dumps(updated))
            payload[field] = value
            invalid_payloads.append(payload)

        missing_existing_identity = json.loads(json.dumps(unchanged))
        missing_existing_identity["old_identity"] = None
        invalid_payloads.append(missing_existing_identity)

        malformed_no_complete = json.loads(json.dumps(no_complete))
        malformed_no_complete["partial"] = False
        invalid_payloads.append(malformed_no_complete)

        for payload in invalid_payloads:
            with self.subTest(
                outcome=payload["outcome"],
                publish_size=payload["publish_size"],
                partial=payload["partial"],
            ):
                completed = self.validate_receipt(payload, 0)
                self.assertEqual(completed.returncode, 2, completed.stdout)

    def test_sync_one_backend_factory_failures_are_bounded_receipts(self) -> None:
        backend_module = self.backend_module

        class StartupStringBomb(Exception):
            def __str__(self) -> str:
                raise KeyboardInterrupt("startup __str__ interrupt")

        long_marker = "startup-ordinary-marker-"
        backend_marker = "startup-backend-marker-"
        cases = (
            (
                RuntimeError(long_marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))),
                "unexpected_error",
                f"RuntimeError: {long_marker}",
                True,
            ),
            (
                StartupStringBomb(),
                "unexpected_error",
                "StartupStringBomb: <unprintable>",
                False,
            ),
            (
                backend_module.BackendError(
                    "startup_backend_failed",
                    backend_marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2)),
                    errno.EIO,
                ),
                "startup_backend_failed",
                backend_marker,
                True,
            ),
        )
        for primary, reason, detail_marker, truncated in cases:
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.destination)
                before = self.write_destination()
                factory = mock.Mock(side_effect=primary)
                receipt = self.helper.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                    backend_factory=factory,
                ).to_dict()

                factory.assert_called_once_with()
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], reason)
                self.assertFalse(receipt["destination_mutated"])
                self.assertIn(detail_marker, receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                if truncated:
                    self.assertIn(
                        self.helper._TRUNCATED_MARKER,
                        receipt["detail"],
                    )
                for field in (
                    "old_size",
                    "new_size",
                    "publish_size",
                    "source_size",
                    "method",
                    "partial",
                    "mtime_semantics",
                    "old_identity",
                    "new_identity",
                ):
                    self.assertIsNone(receipt[field])
                raw = json.dumps(receipt, sort_keys=True).encode("utf-8")
                self.assertLess(len(raw), self.helper._RECEIPT_LIMIT)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_startup_backenderror_reasons_are_safe_bounded_and_validated(
        self,
    ) -> None:
        backend_module = self.backend_module

        class HostileReason(str):
            def encode(self, *_args: Any, **_kwargs: Any) -> bytes:
                raise KeyboardInterrupt("hostile reason encode interrupt")

        valid_reason = "startup_backend_failed"
        hostile_reason = HostileReason("Hostile Reason Marker")
        long_reason = "r" * (self.helper._DIAGNOSTIC_LIMIT * 2)
        factory_cases = (
            (valid_reason, valid_reason, False, valid_reason),
            (
                hostile_reason,
                "backend_initialization_failed",
                False,
                str(hostile_reason),
            ),
            (
                long_reason,
                "backend_initialization_failed",
                True,
                long_reason[:64],
            ),
        )
        real_sync_one = self.helper.sync_one
        argv = (
            "sync-one",
            "--source",
            str(self.source.absolute()),
            "--destination",
            str(self.destination.absolute()),
            "--json",
        )
        for original_reason, expected_reason, truncated, reason_marker in factory_cases:
            with self.subTest(
                boundary="backend_factory",
                expected_reason=expected_reason,
                truncated=truncated,
            ):
                detail_marker = f"backend-factory-detail-{expected_reason}"
                primary = backend_module.BackendError(
                    original_reason,
                    detail_marker,
                    errno.EIO,
                )
                factory = mock.Mock(side_effect=primary)

                def injected_sync(
                    source: str,
                    destination: str,
                    *,
                    action_hook: Optional[Callable[[str], None]] = None,
                ) -> Any:
                    self.assertIsNone(action_hook)
                    return real_sync_one(
                        source,
                        destination,
                        backend_factory=factory,
                    )

                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(self.helper, "_cli_hook", return_value=None):
                    with mock.patch.object(
                        self.helper,
                        "sync_one",
                        side_effect=injected_sync,
                    ):
                        with mock.patch.object(self.helper.sys, "stdout", stdout):
                            with mock.patch.object(self.helper.sys, "stderr", stderr):
                                exit_status = self.helper.main(argv)

                factory.assert_called_once_with()
                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr.getvalue(), "")
                output = stdout.getvalue()
                self.assertEqual(output.count("\n"), 1)
                self.assertTrue(output.endswith("\n"))
                raw = output.rstrip("\n").encode("utf-8")
                self.assertLess(len(raw), self.helper._RECEIPT_LIMIT)
                receipt = json.loads(raw)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], expected_reason)
                self.assertIn(detail_marker, receipt["detail"])
                if expected_reason != original_reason:
                    self.assertIn("invalid startup reason", receipt["detail"])
                    self.assertIn(reason_marker, receipt["detail"])
                    self.assertNotEqual(receipt["reason"], str(original_reason))
                if truncated:
                    self.assertIn(
                        self.helper._TRUNCATED_MARKER,
                        receipt["detail"],
                    )
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)

        boundary_cases = (
            ("cli_hook", hostile_reason, "hook-invalid-reason-detail"),
            ("sync_one", long_reason, "sync-invalid-reason-detail"),
        )
        for surface, original_reason, detail_marker in boundary_cases:
            with self.subTest(boundary=surface):
                primary = backend_module.BackendError(
                    original_reason,
                    detail_marker,
                    errno.EIO,
                )
                hook = mock.Mock(side_effect=primary if surface == "cli_hook" else None)
                sync = mock.Mock(side_effect=primary if surface == "sync_one" else None)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(self.helper, "_cli_hook", hook):
                    with mock.patch.object(self.helper, "sync_one", sync):
                        with mock.patch.object(self.helper.sys, "stdout", stdout):
                            with mock.patch.object(self.helper.sys, "stderr", stderr):
                                exit_status = self.helper.main(argv)

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr.getvalue(), "")
                output = stdout.getvalue()
                self.assertEqual(output.count("\n"), 1)
                raw = output.rstrip("\n").encode("utf-8")
                self.assertLess(len(raw), self.helper._RECEIPT_LIMIT)
                receipt = json.loads(raw)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], "unexpected_error")
                self.assertIn("invalid startup reason", receipt["detail"])
                self.assertIn(detail_marker, receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                if surface == "cli_hook":
                    sync.assert_not_called()
                else:
                    sync.assert_called_once()

    def test_sync_one_backend_factory_raw_baseexceptions_propagate(self) -> None:
        class StartupBomb(BaseException):
            pass

        factories = (
            lambda: KeyboardInterrupt("startup keyboard interrupt"),
            lambda: SystemExit(101),
            lambda: StartupBomb("startup custom BaseException"),
        )
        for make_primary in factories:
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.destination)
                before = self.write_destination()
                stdout = io.StringIO()
                stderr = io.StringIO()
                escaped = None
                with mock.patch.object(
                    self.helper,
                    "_startup_fatal_receipt",
                    wraps=self.helper._startup_fatal_receipt,
                ) as startup_receipt:
                    with mock.patch.object(self.helper.sys, "stdout", stdout):
                        with mock.patch.object(self.helper.sys, "stderr", stderr):
                            try:
                                self.helper.sync_one(
                                    str(self.source.absolute()),
                                    str(self.destination.absolute()),
                                    backend_factory=mock.Mock(side_effect=primary),
                                )
                            except BaseException as exc:
                                escaped = exc
                            else:
                                self.fail(
                                    "raw backend factory failure became a receipt"
                                )
                self.assertIs(escaped, primary)
                if isinstance(primary, SystemExit):
                    self.assertEqual(escaped.code, 101)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                startup_receipt.assert_not_called()
                self.assertEqual(self.destination.stat().st_ino, before.st_ino)
                self.assertEqual(self.destination.read_bytes(), b"old\n")
                self.assert_no_stage_names()

    def test_main_startup_exceptions_emit_one_bounded_fatal_receipt(self) -> None:
        argv = (
            "sync-one",
            "--source",
            str(self.source.absolute()),
            "--destination",
            str(self.destination.absolute()),
            "--json",
        )
        for surface in ("cli_hook", "sync_one"):
            with self.subTest(surface=surface):
                marker = f"main-{surface}-ordinary-marker-"
                primary = RuntimeError(
                    marker + ("x" * (self.helper._DIAGNOSTIC_LIMIT * 2))
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                hook = mock.Mock(side_effect=primary if surface == "cli_hook" else None)
                sync = mock.Mock(side_effect=primary if surface == "sync_one" else None)
                with mock.patch.object(self.helper, "_cli_hook", hook):
                    with mock.patch.object(self.helper, "sync_one", sync):
                        with mock.patch.object(self.helper.sys, "stdout", stdout):
                            with mock.patch.object(self.helper.sys, "stderr", stderr):
                                exit_status = self.helper.main(argv)

                self.assertEqual(exit_status, 2)
                self.assertEqual(stderr.getvalue(), "")
                output = stdout.getvalue()
                self.assertTrue(output.endswith("\n"))
                self.assertEqual(output.count("\n"), 1)
                raw = output.rstrip("\n").encode("utf-8")
                receipt = json.loads(raw)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], "unexpected_error")
                self.assertFalse(receipt["destination_mutated"])
                self.assertIn(marker, receipt["detail"])
                self.assertIn(self.helper._TRUNCATED_MARKER, receipt["detail"])
                self.assertLessEqual(
                    len(receipt["detail"].encode("utf-8")),
                    self.helper._DIAGNOSTIC_LIMIT,
                )
                self.assertLess(len(raw), self.helper._RECEIPT_LIMIT)
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                hook.assert_called_once_with(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                )
                if surface == "cli_hook":
                    sync.assert_not_called()
                else:
                    sync.assert_called_once()

    def test_main_raw_baseexceptions_propagate_and_argparse_remains_outside_boundary(
        self,
    ) -> None:
        class MainStartupBomb(BaseException):
            pass

        argv = (
            "sync-one",
            "--source",
            str(self.source.absolute()),
            "--destination",
            str(self.destination.absolute()),
            "--json",
        )
        primary_factories = (
            lambda: KeyboardInterrupt("main startup keyboard interrupt"),
            lambda: SystemExit(102),
            lambda: MainStartupBomb("main startup custom BaseException"),
        )
        for surface in ("cli_hook", "sync_one"):
            for make_primary in primary_factories:
                primary = make_primary()
                with self.subTest(
                    surface=surface,
                    primary=type(primary).__name__,
                ):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    hook = mock.Mock(
                        side_effect=primary if surface == "cli_hook" else None
                    )
                    sync = mock.Mock(
                        side_effect=primary if surface == "sync_one" else None
                    )
                    escaped = None
                    with mock.patch.object(
                        self.helper,
                        "_startup_fatal_receipt",
                        wraps=self.helper._startup_fatal_receipt,
                    ) as startup_receipt:
                        with mock.patch.object(self.helper, "_cli_hook", hook):
                            with mock.patch.object(self.helper, "sync_one", sync):
                                with mock.patch.object(
                                    self.helper.sys,
                                    "stdout",
                                    stdout,
                                ):
                                    with mock.patch.object(
                                        self.helper.sys,
                                        "stderr",
                                        stderr,
                                    ):
                                        try:
                                            self.helper.main(argv)
                                        except BaseException as exc:
                                            escaped = exc
                                        else:
                                            self.fail(
                                                "raw main failure became a receipt"
                                            )
                    self.assertIs(escaped, primary)
                    if isinstance(primary, SystemExit):
                        self.assertEqual(escaped.code, 102)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "")
                    startup_receipt.assert_not_called()
                    if surface == "cli_hook":
                        sync.assert_not_called()
                    else:
                        sync.assert_called_once()

        stdout = io.StringIO()
        stderr = io.StringIO()
        hook = mock.Mock()
        sync = mock.Mock()
        escaped = None
        with mock.patch.object(self.helper, "_cli_hook", hook):
            with mock.patch.object(self.helper, "sync_one", sync):
                with mock.patch.object(self.helper.sys, "stdout", stdout):
                    with mock.patch.object(self.helper.sys, "stderr", stderr):
                        try:
                            self.helper.main(("sync-one",))
                        except SystemExit as exc:
                            escaped = exc
                        else:
                            self.fail("argparse accepted missing required arguments")
        self.assertIsNotNone(escaped)
        self.assertEqual(escaped.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn("--source", stderr.getvalue())
        self.assertIn("--destination", stderr.getvalue())
        hook.assert_not_called()
        sync.assert_not_called()

    def test_cli_emits_one_json_line_for_success_deferred_and_fatal(self) -> None:
        cases: Iterable[tuple[str, Callable[[], None], int, str]] = (
            ("success", lambda: self.write_source(b"ok\n"), 0, "updated"),
            (
                "deferred",
                lambda: (self.remove_path(self.source), os.mkfifo(self.source)),
                75,
                "deferred",
            ),
            (
                "fatal",
                lambda: (
                    self.remove_path(self.source),
                    self.write_source(b"unsafe\n"),
                    os.chmod(self.source, 0o622),
                ),
                2,
                "fatal",
            ),
        )
        for name, prepare, expected_exit, expected_outcome in cases:
            with self.subTest(name=name):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                prepare()
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT_PATH),
                        "sync-one",
                        "--source",
                        str(self.source),
                        "--destination",
                        str(self.destination),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, expected_exit, completed.stderr)
                lines = completed.stdout.splitlines()
                self.assertEqual(len(lines), 1, completed.stdout)
                receipt = json.loads(lines[0])
                self.assertIsInstance(receipt, dict)
                self.assertEqual(
                    set(receipt), set(self.helper.SyncReceipt.__dataclass_fields__)
                )
                self.assertEqual(receipt["outcome"], expected_outcome)

    def test_trace_interruptions_after_native_fd_handoffs_close_once(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"native-fd-handoff\n")
        parent_fd, name = backend.open_absolute_parent(str(self.source))

        class NativeFDHandoffBomb(BaseException):
            pass

        target_code = self.backend_module._OwnedFD.__enter__.__code__
        acquired_line = self.source_line_number(target_code, "if self.closed:")
        native_probe = backend._native_fd_owner(
            "native acquire-code probe",
            lambda: -1,
            reason="probe_failed",
            operation="trace-code probe",
        )
        native_acquire_code = native_probe._acquire.__code__
        operations = (("open_leaf", lambda: backend.open_leaf(parent_fd, name)),)
        try:
            for label, operation in operations:
                with self.subTest(operation=label):
                    primary = NativeFDHandoffBomb(f"{label} handoff interrupt")
                    observed: Dict[str, Any] = {}

                    def after_native_open(frame: Any) -> bool:
                        target = frame.f_locals.get("self")
                        if (
                            target is None
                            or target.closed
                            or frame.f_lineno != acquired_line
                            or target._acquire.__code__ is not native_acquire_code
                            or target._subject != f"leaf {name!r}"
                        ):
                            return False
                        observed["owner"] = target
                        observed["fd"] = target.fileno()
                        observed["close"] = self.observe_fd_owner_close(target)
                        return True

                    self.interrupt_on_traced_local_handoff(
                        target_code,
                        after_native_open,
                        operation,
                        primary,
                    )

                    owner = observed["owner"]
                    handed_off_fd = observed["fd"]
                    close_observation = observed["close"]
                    self.assertTrue(owner.closed)
                    self.assertEqual(close_observation["active_closes"], 1)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(handed_off_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    owner.close()
                    owner.close()
                    self.assertEqual(close_observation["active_closes"], 1)

            wrapper_cases = (
                (
                    "open_leaf_wrapper",
                    self.backend_module.DarwinBackend._handoff_fd_owner.__code__,
                    "owner.__enter__(); return owner.disarm()  # noqa: E702",
                    lambda frame: frame.f_locals.get("owner") is not None,
                    lambda: backend.open_leaf(parent_fd, name),
                    False,
                ),
                (
                    "identity_result",
                    self.backend_module.DarwinBackend.identity_at.__code__,
                    "owner.close()",
                    lambda frame: "result" in frame.f_locals,
                    lambda: backend.identity_at(parent_fd, name),
                    True,
                ),
            )
            for (
                label,
                code,
                line_text,
                local_ready,
                operation,
                acquired,
            ) in wrapper_cases:
                with self.subTest(operation=label):
                    primary = NativeFDHandoffBomb(f"{label} local interrupt")
                    observed: Dict[str, Any] = {}
                    target_line = self.source_line_number(code, line_text)

                    def after_wrapper_store(frame: Any) -> bool:
                        owner = frame.f_locals.get("owner")
                        if (
                            owner is None
                            or frame.f_lineno != target_line
                            or not local_ready(frame)
                            or owner.closed is acquired
                        ):
                            return False
                        observed["owner"] = owner
                        observed["close"] = self.observe_fd_owner_close(owner)
                        if acquired:
                            observed["fd"] = owner.fileno()
                        return True

                    self.interrupt_on_traced_local_handoff(
                        code,
                        after_wrapper_store,
                        operation,
                        primary,
                    )
                    owner = observed["owner"]
                    self.assertTrue(owner.closed)
                    self.assertEqual(
                        observed["close"]["active_closes"],
                        1 if acquired else 0,
                    )
                    if acquired:
                        with self.assertRaises(OSError) as closed:
                            os.fstat(observed["fd"])
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                    owner.close()
                    self.assertEqual(
                        observed["close"]["active_closes"],
                        1 if acquired else 0,
                    )

            raw_fd = backend.open_leaf(parent_fd, name)
            self.assertEqual(os.fstat(raw_fd).st_ino, self.source.stat().st_ino)
            os.close(raw_fd)

            normal_owner = backend._open_leaf_owned(parent_fd, name)
            with normal_owner:
                normal_observation = self.observe_fd_owner_close(normal_owner)
                normal_fd = normal_owner.fileno()
            self.assertFalse(normal_owner.closed)
            self.assertEqual(os.fstat(normal_fd).st_ino, self.source.stat().st_ino)
            normal_owner.close()
            normal_owner.close()
            self.assertEqual(normal_observation["active_closes"], 1)
        finally:
            native_probe.close()
            os.close(parent_fd)

    def test_absolute_parent_override_owned_handoff_is_compatible_and_closed_once(
        self,
    ) -> None:
        backend_module = self.backend_module
        self.write_source(b"absolute-parent-override\n")

        class OverrideParentBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.override_calls = 0
                inner_self.returned_fds = []
                inner_self.return_leaf = None

            def open_absolute_parent(inner_self, path: str) -> tuple[int, str]:
                inner_self.override_calls += 1
                fd, leaf = super().open_absolute_parent(path)
                inner_self.returned_fds.append(fd)
                return (fd, inner_self.return_leaf or leaf)

        backend = OverrideParentBackend()
        owner, leaf = backend._open_absolute_parent_owned(str(self.source))
        self.assertEqual(leaf, self.source.name)
        with owner:
            normal_close = self.observe_fd_owner_close(owner)
            normal_fd = owner.fileno()
        self.assertEqual(backend.override_calls, 1)
        self.assertFalse(owner.closed)
        self.assertEqual(os.fstat(normal_fd).st_ino, self.source_parent.stat().st_ino)
        owner.close()
        owner.close()
        self.assertEqual(normal_close["active_closes"], 1)

        transaction = self.helper.MirrorSync(backend)
        transaction.source_path = str(self.source.absolute())
        transaction.destination_path = str(self.destination.absolute())
        transaction._validate_paths()
        transaction._bind_source()
        calls_before_reopen = backend.override_calls
        transaction._require_source_mapping()
        self.assertEqual(backend.override_calls, calls_before_reopen + 1)
        transaction._close_all(primary_error=None)

        class ParentOverrideBomb(BaseException):
            pass

        interrupted_owner, interrupted_leaf = backend._open_absolute_parent_owned(
            str(self.source)
        )
        self.assertEqual(interrupted_leaf, self.source.name)
        primary = ParentOverrideBomb("absolute parent tuple handoff interrupt")
        observed: Dict[str, Any] = {}
        acquired_line = self.source_line_number(
            self.backend_module._OwnedFD.__enter__.__code__, "if self.closed:"
        )

        def after_override_return(frame: Any) -> bool:
            target = frame.f_locals.get("self")
            if (
                target is not interrupted_owner
                or target.closed
                or frame.f_lineno != acquired_line
                or target.fileno() != backend.returned_fds[-1]
            ):
                return False
            observed["fd"] = target.fileno()
            observed["close"] = self.observe_fd_owner_close(target)
            return True

        self.interrupt_on_traced_local_handoff(
            self.backend_module._OwnedFD.__enter__.__code__,
            after_override_return,
            interrupted_owner.__enter__,
            primary,
        )
        self.assertTrue(interrupted_owner.closed)
        self.assertEqual(observed["close"]["active_closes"], 1)
        with self.assertRaises(OSError) as closed:
            os.fstat(observed["fd"])
        self.assertEqual(closed.exception.errno, errno.EBADF)
        interrupted_owner.close(primary_error=primary)
        self.assertEqual(observed["close"]["active_closes"], 1)

        mismatch_backend = OverrideParentBackend()
        mismatch_backend.return_leaf = "different.jsonl"
        mismatch_owner, expected_leaf = mismatch_backend._open_absolute_parent_owned(
            str(self.source)
        )
        mismatch_close = self.observe_fd_owner_close(mismatch_owner)
        with self.assertRaises(backend_module.BackendError) as mismatch:
            mismatch_owner.__enter__()
        self.assertEqual(mismatch.exception.reason, "invalid_path")
        self.assertEqual(expected_leaf, self.source.name)
        self.assertEqual(mismatch_backend.override_calls, 1)
        self.assertTrue(mismatch_owner.closed)
        self.assertEqual(mismatch_close["active_closes"], 1)
        mismatch_fd = mismatch_backend.returned_fds[-1]
        with self.assertRaises(OSError) as closed:
            os.fstat(mismatch_fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_malformed_absolute_parent_override_closes_recognizable_fd_once(
        self,
    ) -> None:
        backend_module = self.backend_module
        self.write_source(b"malformed-absolute-parent\n")

        class HostileIterable:
            def __init__(inner_self) -> None:
                inner_self.protocol_calls = 0

            def __iter__(inner_self) -> Any:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile parent result was iterated")

            def __len__(inner_self) -> int:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile parent result length was read")

            def __getitem__(inner_self, _index: int) -> Any:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile parent result was indexed")

        for shape in ("extra", "short", "hostile"):
            with self.subTest(shape=shape):

                class MalformedParentBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.override_calls = 0
                        inner_self.raw_fd = -1
                        inner_self.hostile = HostileIterable()

                    def open_absolute_parent(inner_self, path: str) -> Any:
                        inner_self.override_calls += 1
                        if shape == "hostile":
                            return inner_self.hostile
                        fd, leaf = super().open_absolute_parent(path)
                        inner_self.raw_fd = fd
                        if shape == "short":
                            return (fd,)
                        return (fd, leaf, "unexpected-extra-field")

                backend = MalformedParentBackend()
                owner, expected_leaf = backend._open_absolute_parent_owned(
                    str(self.source)
                )
                close_observation = self.observe_fd_owner_close(owner)
                escaped: Optional[BaseException] = None
                try:
                    owner.__enter__()
                except BaseException as exc:
                    escaped = exc
                else:
                    self.fail("malformed absolute-parent override was accepted")

                self.assertIsInstance(escaped, backend_module.BackendError)
                self.assertEqual(escaped.reason, "invalid_path")
                self.assertEqual(expected_leaf, self.source.name)
                self.assertEqual(backend.override_calls, 1)
                self.assertTrue(owner.closed)
                if shape == "hostile":
                    self.assertEqual(backend.hostile.protocol_calls, 0)
                    self.assertEqual(close_observation["active_closes"], 0)
                else:
                    self.assertEqual(close_observation["active_closes"], 1)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.raw_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                owner.close(primary_error=escaped)
                self.assertEqual(
                    close_observation["active_closes"],
                    0 if shape == "hostile" else 1,
                )

    def test_trace_interruption_closes_helper_reopen_owner_once(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"helper-reopen-handoff\n")
        transaction = self.helper.MirrorSync(backend)
        transaction.source_path = str(self.source.absolute())
        transaction.destination_path = str(self.destination.absolute())
        transaction._validate_paths()
        transaction._bind_source()

        class HelperReopenBomb(BaseException):
            pass

        primary = HelperReopenBomb("helper reopen interrupt")
        observed: Dict[str, Any] = {}
        reopen_line = self.source_line_number(
            self.helper.MirrorSync._require_source_mapping.__code__, "entered = True"
        )

        def after_reopen_enter(frame: Any) -> bool:
            owner = frame.f_locals.get("reopened_owner")
            if (
                owner is None
                or owner.closed
                or frame.f_lineno != reopen_line
                or frame.f_locals.get("entered") is not False
            ):
                return False
            observed["owner"] = owner
            observed["fd"] = owner.fileno()
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                self.helper.MirrorSync._require_source_mapping.__code__,
                after_reopen_enter,
                transaction._require_source_mapping,
                primary,
            )
            owner = observed["owner"]
            reopened_fd = observed["fd"]
            close_observation = observed["close"]
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(reopened_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            owner.close()
            self.assertEqual(close_observation["active_closes"], 1)
        finally:
            transaction._close_all(primary_error=primary)

    def test_trace_interruption_after_stage_open_cleans_exact_owner(self) -> None:
        backend = self.backend_module.DarwinBackend()
        stage_path = self.destination_parent / "trace-stage-open"
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        actions = []

        class StageTraceBomb(BaseException):
            pass

        primary = StageTraceBomb("stage fd handoff interrupt")
        stage_owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        acquired_line = self.source_line_number(
            self.backend_module._OwnedFD.__enter__.__code__, "if self.closed:"
        )
        native_probe = backend._native_fd_owner(
            "stage native acquire-code probe",
            lambda: -1,
            reason="probe_failed",
            operation="trace-code probe",
        )
        native_acquire_code = native_probe._acquire.__code__
        observed: Dict[str, Any] = {}

        def after_stage_open(frame: Any) -> bool:
            target = frame.f_locals.get("self")
            if (
                target is None
                or target.closed
                or type(target) is not self.backend_module._OwnedFD
                or target is stage_owner
                or target._acquire.__code__ is not native_acquire_code
                or target._subject != f"private stage {name!r}"
                or frame.f_lineno != acquired_line
            ):
                return False
            observed["owner"] = target
            observed["fd"] = target.fileno()
            observed["close"] = self.observe_fd_owner_close(target)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                self.backend_module._OwnedFD.__enter__.__code__,
                after_stage_open,
                stage_owner.__enter__,
                primary,
            )
            internal_owner = observed["owner"]
            opened_fd = observed["fd"]
            close_observation = observed["close"]
            self.assertTrue(internal_owner.closed)
            self.assertTrue(stage_owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            with self.assertRaises(OSError) as closed:
                os.fstat(opened_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            internal_owner.close()
            stage_owner.close()
            self.assertEqual(close_observation["active_closes"], 1)

            normal_path = self.destination_parent / "trace-stage-normal"
            normal_owner = backend._create_private_stage_parent_owned(
                parent_fd,
                normal_path.name,
                authorize_state=lambda _action: None,
            )
            with normal_owner:
                normal_identity = normal_owner.identity()
                normal_observation = self.observe_fd_owner_close(normal_owner)
                normal_fd = normal_owner.fileno()
            self.assertFalse(normal_owner.closed)
            self.assertEqual(os.fstat(normal_fd).st_ino, normal_identity.ino)
            normal_owner.close()
            normal_owner.close()
            self.assertEqual(normal_observation["active_closes"], 1)
            normal_path.rmdir()
        finally:
            native_probe.close()
            stage_owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_trace_interruption_after_stage_mkdir_cleans_created_namespace(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        stage_path = self.destination_parent / "trace-stage-mkdir"
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        actions = []

        class StageMkdirBomb(BaseException):
            pass

        primary = StageMkdirBomb("stage mkdir local interrupt")
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        created_stat_line = self.source_line_number(
            owner._acquire.__code__,
            "created_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)",
        )

        def after_namespace_store(frame: Any) -> bool:
            target = frame.f_locals.get("target")
            return (
                target is owner
                and frame.f_lineno == created_stat_line
                and target._namespace_created is True
                and target._stage_identity is None
            )

        try:
            self.interrupt_on_traced_local_handoff(
                owner._acquire.__code__,
                after_namespace_store,
                owner.__enter__,
                primary,
            )
            self.assertTrue(owner.closed)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            owner.close(primary_error=primary)
            self.assertFalse(stage_path.exists())
        finally:
            owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_stage_parent_public_return_interruption_cleans_identity_once(self) -> None:
        backend = self.backend_module.DarwinBackend()
        stage_path = self.destination_parent / "stage-parent-public-return-trace"
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        actions = []
        observed: Dict[str, Any] = {}

        class StageParentReturnBomb(BaseException):
            pass

        primary = StageParentReturnBomb("stage-parent public return interruption")
        handoff_code = backend._handoff_stage_owner.__code__
        public_return_line = self.source_line_number(
            handoff_code,
            "owner.__enter__(); identity = owner.identity(); return (owner.disarm(), identity)  # noqa: E702",
        )

        def before_public_disarm_return(frame: Any) -> bool:
            owner = frame.f_locals.get("owner")
            if (
                frame.f_locals.get("self") is not backend
                or frame.f_lineno != public_return_line
                or owner is None
                or not owner.closed
                or owner._namespace_created
                or stage_path.exists()
            ):
                return False
            observed["owner"] = owner
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                handoff_code,
                before_public_disarm_return,
                lambda: backend.create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=actions.append,
                ),
                primary,
            )
            owner = observed["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(observed["close"]["active_closes"], 0)
            self.assertFalse(owner._namespace_created)
            self.assertFalse(owner._namespace_cleanup_attempted)
            self.assertFalse(owner._namespace_cleanup_complete)
            self.assertEqual(actions, [])
            self.assertFalse(stage_path.exists())
            owner.close(primary_error=primary)
            self.assertEqual(observed["close"]["active_closes"], 0)
        finally:
            owner = observed.get("owner")
            if owner is not None:
                owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_private_stage_success_handoff_interruption_cleans_stage_and_parent(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        stage_path = self.destination_parent / "private-stage-success-handoff"
        actions = []
        observed: Dict[str, Any] = {}

        class PrivateStageReturnBomb(BaseException):
            pass

        primary = PrivateStageReturnBomb("private stage success handoff interruption")
        durable_commit_line = self.source_line_number(
            backend.create_private_stage.__code__,
            "durable_namespace_complete = True",
        )

        def before_durable_success_return(frame: Any) -> bool:
            stage_owner = frame.f_locals.get("stage_owner")
            parent_owner = frame.f_locals.get("parent_owner")
            identity = frame.f_locals.get("identity")
            if (
                frame.f_locals.get("self") is not backend
                or frame.f_lineno != durable_commit_line
                or stage_owner is None
                or parent_owner is None
                or stage_owner.closed
                or parent_owner.closed
                or not isinstance(identity, self.backend_module.FileIdentity)
                or frame.f_locals.get("durable_namespace_complete") is not False
                or not stage_path.is_dir()
            ):
                return False
            observed["stage_owner"] = stage_owner
            observed["parent_owner"] = parent_owner
            observed["stage_fd"] = stage_owner.fileno()
            observed["parent_fd"] = parent_owner.fileno()
            observed["stage_close"] = self.observe_fd_owner_close(stage_owner)
            observed["parent_close"] = self.observe_fd_owner_close(parent_owner)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                backend.create_private_stage.__code__,
                before_durable_success_return,
                lambda: backend.create_private_stage(
                    str(stage_path), authorize_state=actions.append
                ),
                primary,
            )
            self.assertTrue(observed["stage_owner"].closed)
            self.assertTrue(observed["parent_owner"].closed)
            self.assertEqual(observed["stage_close"]["active_closes"], 1)
            self.assertEqual(observed["parent_close"]["active_closes"], 1)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            for fd_key in ("stage_fd", "parent_fd"):
                with self.assertRaises(OSError) as closed:
                    os.fstat(observed[fd_key])
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            stage_owner = observed.get("stage_owner")
            parent_owner = observed.get("parent_owner")
            if stage_owner is not None:
                stage_owner.close(primary_error=primary)
            if parent_owner is not None:
                parent_owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()

    def test_stage_public_override_acquired_owner_interruption_cleans_once(
        self,
    ) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "stage-public-return-trace"
        actions = []

        class StagePublicReturnBomb(BaseException):
            pass

        class OverrideBackend(backend_module.DarwinBackend):
            def create_private_stage_parent(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
            ) -> Any:
                return super().create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=authorize_state,
                )

        backend = OverrideBackend()
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        close_observation = self.observe_fd_owner_close(owner)
        primary = StagePublicReturnBomb("stage public acquired-owner interruption")
        acquired_line = self.source_line_number(
            self.backend_module._OwnedFD.__enter__.__code__, "if self.closed:"
        )

        def after_public_override_acquisition(frame: Any) -> bool:
            target = frame.f_locals.get("self")
            return (
                target is owner
                and not target.closed
                and target._namespace_created
                and frame.f_lineno == acquired_line
            )

        try:
            self.interrupt_on_traced_local_handoff(
                self.backend_module._OwnedFD.__enter__.__code__,
                after_public_override_acquisition,
                owner.__enter__,
                primary,
            )
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertTrue(owner._namespace_cleanup_attempted)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            owner.close(primary_error=primary)
            self.assertEqual(close_observation["active_closes"], 1)
        finally:
            owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_overridden_stage_raw_handoff_interruption_cleans_once(self) -> None:
        backend_module = self.backend_module

        class OverrideHandoffBomb(BaseException):
            pass

        class CleanupBomb(BaseException):
            pass

        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails):
                stage_path = self.destination_parent / (
                    f"trace-stage-override-{cleanup_fails}"
                )
                primary = OverrideHandoffBomb(
                    f"overridden stage handoff interrupt {cleanup_fails}"
                )
                cleanup_marker = f"override-cleanup-marker-{cleanup_fails}"
                actions = []

                class OverrideBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.override_calls = 0

                    def create_private_stage_parent(
                        inner_self,
                        parent_fd: int,
                        name: str,
                        *,
                        authorize_state: Callable[[str], None],
                    ) -> Any:
                        inner_self.override_calls += 1
                        return super().create_private_stage_parent(
                            parent_fd,
                            name,
                            authorize_state=authorize_state,
                        )

                backend = OverrideBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage_path))

                def authorize(action: str) -> None:
                    actions.append(action)
                    if cleanup_fails and action == "remove_stage":
                        raise CleanupBomb(cleanup_marker)

                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    name,
                    authorize_state=authorize,
                )
                close_observation = self.observe_fd_owner_close(owner)
                validated_tuple_line = self.source_line_number(
                    owner._acquire.__code__,
                    "if type(handed_off) is not tuple or len(handed_off) != 2:",
                )

                def after_override_tuple_handoff(frame: Any) -> bool:
                    target = frame.f_locals.get("target")
                    identity = frame.f_locals.get("identity")
                    return (
                        target is owner
                        and frame.f_lineno == validated_tuple_line
                        and target._namespace_created is True
                        and identity is not None
                        and frame.f_locals.get("handed_off_fd", -1) >= 0
                        and not target.closed
                        and target.fileno() == frame.f_locals.get("handed_off_fd", -1)
                    )

                try:
                    captured = self.interrupt_on_traced_local_handoff(
                        owner._acquire.__code__,
                        after_override_tuple_handoff,
                        owner.__enter__,
                        primary,
                    )
                    opened_fd = captured["handed_off_fd"]
                    self.assertEqual(backend.override_calls, 1)
                    self.assertTrue(owner.closed)
                    self.assertTrue(owner._namespace_cleanup_attempted)
                    self.assertEqual(close_observation["active_closes"], 1)
                    self.assertEqual(actions, ["create_stage", "remove_stage"])
                    with self.assertRaises(OSError) as closed:
                        os.fstat(opened_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)

                    if cleanup_fails:
                        self.assertFalse(owner._namespace_cleanup_complete)
                        self.assertTrue(stage_path.is_dir())
                        diagnostic = getattr(primary, "cleanup_diagnostic", "")
                        self.assertIn(cleanup_marker, diagnostic)
                        owner.close(primary_error=primary)
                        self.assertEqual(actions, ["create_stage", "remove_stage"])
                        self.assertTrue(stage_path.is_dir())
                    else:
                        self.assertTrue(owner._namespace_cleanup_complete)
                        self.assertFalse(stage_path.exists())
                        owner.close(primary_error=primary)
                        self.assertEqual(actions, ["create_stage", "remove_stage"])
                        self.assertFalse(stage_path.exists())
                    self.assertEqual(close_observation["active_closes"], 1)
                finally:
                    owner.close(primary_error=primary)
                    if stage_path.exists():
                        stage_path.rmdir()
                    os.close(parent_fd)

    def test_malformed_stage_override_closes_and_cleans_recognizable_result(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CleanupBomb(BaseException):
            pass

        class HostileIterable:
            def __init__(inner_self) -> None:
                inner_self.protocol_calls = 0

            def __iter__(inner_self) -> Any:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile stage result was iterated")

            def __len__(inner_self) -> int:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile stage result length was read")

            def __getitem__(inner_self, _index: int) -> Any:
                inner_self.protocol_calls += 1
                raise AssertionError("hostile stage result was indexed")

        cases = (
            ("extra", False),
            ("extra", True),
            ("short", False),
            ("hostile", False),
        )
        for shape, cleanup_fails in cases:
            with self.subTest(shape=shape, cleanup_fails=cleanup_fails):
                stage_path = self.destination_parent / (
                    f"malformed-stage-{shape}-{cleanup_fails}"
                )
                cleanup_marker = f"malformed-stage-cleanup-{shape}"
                actions = []

                class MalformedStageBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.override_calls = 0
                        inner_self.raw_fd = -1
                        inner_self.returned_identity = None
                        inner_self.hostile = HostileIterable()

                    def create_private_stage_parent(
                        inner_self,
                        parent_fd: int,
                        name: str,
                        *,
                        authorize_state: Callable[[str], None],
                    ) -> Any:
                        inner_self.override_calls += 1
                        if shape == "hostile":
                            return inner_self.hostile
                        fd, identity = super().create_private_stage_parent(
                            parent_fd,
                            name,
                            authorize_state=authorize_state,
                        )
                        inner_self.raw_fd = fd
                        inner_self.returned_identity = identity
                        if shape == "short":
                            return (fd,)
                        return (fd, identity, "unexpected-extra-field")

                backend = MalformedStageBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage_path))

                def authorize(action: str) -> None:
                    actions.append(action)
                    if cleanup_fails and action == "remove_stage":
                        raise CleanupBomb(cleanup_marker)

                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    name,
                    authorize_state=authorize,
                )
                close_observation = self.observe_fd_owner_close(owner)
                escaped: Optional[BaseException] = None
                try:
                    owner.__enter__()
                except BaseException as exc:
                    escaped = exc
                else:
                    self.fail("malformed stage override was accepted")

                try:
                    self.assertIsInstance(escaped, backend_module.BackendError)
                    self.assertEqual(escaped.reason, "invalid_stage_result")
                    self.assertEqual(backend.override_calls, 1)
                    self.assertTrue(owner.closed)
                    if shape == "hostile":
                        self.assertEqual(backend.hostile.protocol_calls, 0)
                        self.assertFalse(owner._namespace_created)
                        self.assertFalse(owner._namespace_cleanup_attempted)
                        self.assertEqual(close_observation["active_closes"], 0)
                        self.assertEqual(actions, [])
                        self.assertFalse(stage_path.exists())
                    else:
                        self.assertTrue(owner._namespace_created)
                        self.assertTrue(owner._namespace_cleanup_attempted)
                        self.assertEqual(close_observation["active_closes"], 1)
                        with self.assertRaises(OSError) as closed:
                            os.fstat(backend.raw_fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)

                        if shape == "short":
                            self.assertFalse(owner._namespace_cleanup_complete)
                            self.assertEqual(actions, ["create_stage"])
                            self.assertTrue(stage_path.is_dir())
                            self.assertIn(
                                "identity was not handed off",
                                getattr(escaped, "cleanup_diagnostic", ""),
                            )
                        elif cleanup_fails:
                            self.assertFalse(owner._namespace_cleanup_complete)
                            self.assertEqual(actions, ["create_stage", "remove_stage"])
                            self.assertTrue(stage_path.is_dir())
                            self.assertIn(
                                cleanup_marker,
                                getattr(escaped, "cleanup_diagnostic", ""),
                            )
                        else:
                            self.assertTrue(owner._namespace_cleanup_complete)
                            self.assertEqual(actions, ["create_stage", "remove_stage"])
                            self.assertFalse(stage_path.exists())

                    owner.close(primary_error=escaped)
                    self.assertEqual(
                        close_observation["active_closes"],
                        0 if shape == "hostile" else 1,
                    )
                    if shape != "hostile":
                        expected_actions = (
                            ["create_stage"]
                            if shape == "short"
                            else ["create_stage", "remove_stage"]
                        )
                        self.assertEqual(actions, expected_actions)
                finally:
                    owner.close(primary_error=escaped)
                    if stage_path.exists():
                        stage_path.rmdir()
                    os.close(parent_fd)

    def test_core_stage_cleanup_handler_interruption_preserves_primary_and_cleans(
        self,
    ) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "core-stage-handler-interrupt"

        class CoreStagePrimary(BaseException):
            pass

        class CleanupHandlerBomb(BaseException):
            pass

        primary = CoreStagePrimary("core stage natural validation failure")
        secondary = CleanupHandlerBomb("core cleanup handler interruption")

        class CoreFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fail_stage_validation = False

            def validate_private_stage_parent(inner_self, fd: int) -> Any:
                if inner_self.fail_stage_validation:
                    inner_self.fail_stage_validation = False
                    raise primary
                return super().validate_private_stage_parent(fd)

        backend = CoreFailureBackend()
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        actions = []
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        backend.fail_stage_validation = True
        baseline_fds = self.open_fd_set()
        cleanup_handler_line = self.source_line_number(
            owner._acquire.__code__, "target._cleanup_created_namespace(exc)"
        )

        def at_cleanup_handler_entry(frame: Any) -> bool:
            return (
                frame.f_locals.get("target") is owner
                and frame.f_lineno == cleanup_handler_line
                and frame.f_locals.get("exc") is primary
                and stage_path.is_dir()
                and actions == ["create_stage"]
            )

        try:
            self.interrupt_handler_preserving_primary(
                owner._acquire.__code__,
                at_cleanup_handler_entry,
                owner.__enter__,
                secondary,
            )
            self.assertEqual(self.open_fd_set(), baseline_fds)
            self.assertTrue(owner.closed)
            self.assertTrue(owner._namespace_cleanup_attempted)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            diagnostic = getattr(primary, "cleanup_diagnostic", "")
            self.assertIn("core cleanup handler interruption", diagnostic)
            owner.close(primary_error=primary)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
        finally:
            owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_stage_cleanup_callback_entry_interruption_retries_and_completes(
        self,
    ) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "stage-cleanup-callback-interrupt"

        class StageValidationPrimary(BaseException):
            pass

        class CleanupCallbackBomb(BaseException):
            pass

        primary = StageValidationPrimary("stage validation primary")
        secondary = CleanupCallbackBomb("cleanup callback entry interruption")

        class CallbackFailureBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.fail_stage_validation = False
                inner_self.opened_fd = -1

            def validate_private_stage_parent(inner_self, fd: int) -> Any:
                if inner_self.fail_stage_validation:
                    inner_self.fail_stage_validation = False
                    inner_self.opened_fd = fd
                    raise primary
                return super().validate_private_stage_parent(fd)

        backend = CallbackFailureBackend()
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        actions = []
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        cleanup_calls = 0
        real_configure_cleanup = owner._configure_namespace_cleanup

        def install_counted_cleanup(cleanup: Callable[[], Optional[str]]) -> None:
            def counted_cleanup() -> Optional[str]:
                nonlocal cleanup_calls
                cleanup_calls += 1
                return cleanup()

            real_configure_cleanup(counted_cleanup)

        owner._configure_namespace_cleanup = install_counted_cleanup
        cleanup_code = next(
            constant
            for constant in owner._acquire.__code__.co_consts
            if getattr(constant, "co_name", None) == "cleanup_created_stage"
        )
        backend.fail_stage_validation = True
        cleanup_callback_line = self.source_line_number(
            cleanup_code,
            "target._namespace_cleanup_attempted = True; current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)  # noqa: E702",
        )
        close_calls = []
        real_close = backend_module.os.close

        def record_stage_close(fd: int) -> None:
            if fd == backend.opened_fd:
                close_calls.append(fd)
            real_close(fd)

        def before_cleanup_callback_attempt(frame: Any) -> bool:
            target = frame.f_locals.get("target")
            return (
                target is owner
                and frame.f_lineno == cleanup_callback_line
                and not target._namespace_cleanup_attempted
                and stage_path.is_dir()
                and actions == ["create_stage"]
            )

        baseline_fds = self.open_fd_set()
        try:
            with mock.patch.object(
                backend_module.os, "close", side_effect=record_stage_close
            ):
                self.interrupt_handler_preserving_primary(
                    cleanup_code,
                    before_cleanup_callback_attempt,
                    owner.__enter__,
                    secondary,
                    expected_primary=primary,
                )
            self.assertEqual(self.open_fd_set(), baseline_fds)
            self.assertTrue(owner.closed)
            self.assertTrue(owner._namespace_cleanup_attempted)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(cleanup_calls, 2)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertEqual(close_calls, [backend.opened_fd])
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.opened_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertFalse(stage_path.exists())
            self.assertIn(
                "cleanup callback entry interruption",
                getattr(primary, "cleanup_diagnostic", ""),
            )
            owner.close(primary_error=primary)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
        finally:
            owner.close(primary_error=primary)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_malformed_stage_salvage_handler_interruption_keeps_primary_and_owner(
        self,
    ) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "malformed-salvage-interrupt"

        class SalvageHandlerBomb(BaseException):
            pass

        secondary = SalvageHandlerBomb("malformed salvage handler interruption")
        actions = []

        class MalformedOverrideBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.raw_fd = -1

            def create_private_stage_parent(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
            ) -> Any:
                fd, identity = super().create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=authorize_state,
                )
                inner_self.raw_fd = fd
                return (fd, identity, "unexpected-extra-field")

        backend = MalformedOverrideBackend()
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        close_observation = self.observe_fd_owner_close(owner)
        baseline_fds = self.open_fd_set()
        salvage_handler_line = self.source_line_number(
            owner._acquire.__code__,
            "if type(handed_off) is tuple and len(handed_off) > 0:",
            occurrence=2,
        )

        def at_salvage_handler_entry(frame: Any) -> bool:
            handler_primary = frame.f_locals.get("primary")
            handed_off = frame.f_locals.get("handed_off")
            return (
                frame.f_locals.get("target") is owner
                and frame.f_lineno == salvage_handler_line
                and isinstance(handler_primary, backend_module.BackendError)
                and handler_primary.reason == "invalid_stage_result"
                and type(handed_off) is tuple
                and len(handed_off) == 3
                and stage_path.is_dir()
                and actions == ["create_stage"]
            )

        try:
            captured = self.interrupt_handler_preserving_primary(
                owner._acquire.__code__,
                at_salvage_handler_entry,
                owner.__enter__,
                secondary,
            )
            primary = captured["__primary__"]
            self.assertEqual(primary.reason, "invalid_stage_result")
            self.assertEqual(self.open_fd_set(), baseline_fds)
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertTrue(owner._namespace_cleanup_attempted)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            self.assertIn(
                "malformed salvage handler interruption",
                getattr(primary, "cleanup_diagnostic", ""),
            )
            owner.close(primary_error=primary)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
        finally:
            owner.close()
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_publish_reopen_raw_override_handoff_closes_once(self) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "publish-reopen-stage"
        stage_path.mkdir(mode=0o700)
        candidate = stage_path / "candidate.jsonl"
        candidate.write_bytes(b"published-owner-handoff\n")
        candidate.chmod(0o600)

        class PublishReopenBomb(BaseException):
            pass

        class PublishOverrideBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.inject_enabled = False
                inner_self.inject_now = False
                inner_self.destination_parent_fd = -1
                inner_self.destination_name = ""
                inner_self.destination_opens = 0
                inner_self.raw_fd = -1

            def open_leaf(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                writable: bool = False,
            ) -> int:
                fd = super().open_leaf(parent_fd, name, writable=writable)
                if (
                    inner_self.inject_enabled
                    and parent_fd == inner_self.destination_parent_fd
                    and name == inner_self.destination_name
                ):
                    inner_self.destination_opens += 1
                    if inner_self.destination_opens == 2:
                        inner_self.inject_now = True
                        inner_self.raw_fd = fd
                return fd

        backend = PublishOverrideBackend()
        stage_fd = backend.open_absolute_dir(str(stage_path))
        destination_parent_fd = backend.open_absolute_dir(str(self.destination_parent))
        primary = PublishReopenBomb("published reopen raw handoff interrupt")
        observed: Dict[str, Any] = {}
        actions = []
        acquired_line = self.source_line_number(
            self.backend_module._OwnedFD.__enter__.__code__, "if self.closed:"
        )
        try:
            stage_expected = backend.identity_at(stage_fd, candidate.name)
            backend.destination_parent_fd = destination_parent_fd
            backend.destination_name = self.destination.name
            backend.inject_enabled = True

            def after_published_raw_return(frame: Any) -> bool:
                target = frame.f_locals.get("self")
                if (
                    not backend.inject_now
                    or target is None
                    or target.closed
                    or target.fileno() != backend.raw_fd
                    or frame.f_lineno != acquired_line
                ):
                    return False
                observed["owner"] = target
                observed["close"] = self.observe_fd_owner_close(target)
                return True

            baseline_fds = self.open_fd_set()
            self.interrupt_on_traced_local_handoff(
                self.backend_module._OwnedFD.__enter__.__code__,
                after_published_raw_return,
                lambda: backend.publish_staged_name(
                    stage_fd,
                    candidate.name,
                    stage_expected,
                    destination_parent_fd,
                    self.destination.name,
                    None,
                    authorize_namespace=actions.append,
                    validate_after_authorization=lambda: None,
                ),
                primary,
            )
            self.assertEqual(self.open_fd_set(), baseline_fds)
            self.assertEqual(backend.destination_opens, 2)
            self.assertEqual(actions, ["publish"])
            self.assertTrue(observed["owner"].closed)
            self.assertEqual(observed["close"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertEqual(
                self.destination.read_bytes(), b"published-owner-handoff\n"
            )
            self.assertEqual(self.destination.stat().st_ino, stage_expected.ino)
            self.assertFalse(candidate.exists())
            self.assertEqual(list(stage_path.iterdir()), [])
            observed["owner"].close(primary_error=primary)
            self.assertEqual(observed["close"]["active_closes"], 1)
        finally:
            os.close(stage_fd)
            os.close(destination_parent_fd)
            if candidate.exists():
                candidate.unlink()
            if stage_path.exists():
                stage_path.rmdir()

    def test_intent_raw_override_handoffs_close_once_without_mutation(self) -> None:
        backend_module = self.backend_module

        class IntentHandoffBomb(BaseException):
            pass

        def snapshot_expectation_for_path(backend: Any, path: pathlib.Path) -> Any:
            parent_fd, name = backend.open_absolute_parent(str(path))
            try:
                fd = backend.open_leaf(parent_fd, name)
                try:
                    return backend.snapshot_expectation(backend.snapshot_file(fd))
                finally:
                    os.close(fd)
            finally:
                os.close(parent_fd)

        for injection_site in ("final-parent", "original", "clone"):
            with self.subTest(injection_site=injection_site):
                final_path = self.destination_parent / (
                    f"intent-survivor-{injection_site}.jsonl"
                )
                final_path.write_bytes(b"durable-original\n")
                final_path.chmod(0o600)
                stage_path = self.destination_parent / (
                    f".codex-reflink-repair-{injection_site.encode().hex():0<32}"
                )

                class IntentOverrideBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.inject_enabled = False
                        inner_self.inject_now = False
                        inner_self.raw_fd = -1
                        inner_self.target_returns = 0

                    def open_absolute_parent(inner_self, path: str) -> tuple[int, str]:
                        fd, leaf = super().open_absolute_parent(path)
                        if (
                            inner_self.inject_enabled
                            and injection_site == "final-parent"
                            and path == str(final_path)
                        ):
                            inner_self.target_returns += 1
                            inner_self.inject_now = True
                            inner_self.raw_fd = fd
                        return (fd, leaf)

                    def open_leaf(
                        inner_self,
                        parent_fd: int,
                        name: str,
                        *,
                        writable: bool = False,
                    ) -> int:
                        fd = super().open_leaf(parent_fd, name, writable=writable)
                        target_name = (
                            final_path.name if injection_site == "original" else "clone"
                        )
                        if (
                            inner_self.inject_enabled
                            and injection_site in {"original", "clone"}
                            and name == target_name
                        ):
                            inner_self.target_returns += 1
                            inner_self.inject_now = True
                            inner_self.raw_fd = fd
                        return fd

                backend = IntentOverrideBackend()
                original_expectation = snapshot_expectation_for_path(
                    backend, final_path
                )
                container_fd = backend.open_absolute_dir(str(self.destination_parent))
                try:
                    container_identity = backend.validate_stage_container(container_fd)
                finally:
                    os.close(container_fd)

                expected_stage = None
                expected_clone = None
                expected_snapshot = None
                expected_size = 0
                expected_sha256 = "0" * 64
                clone_path = stage_path / "clone"
                if injection_site == "clone":
                    stage_path.mkdir(mode=0o700)
                    clone_path.write_bytes(b"durable-clone\n")
                    clone_path.chmod(0o600)
                    stage_fd = backend.open_absolute_dir(str(stage_path))
                    try:
                        expected_stage = backend.validate_private_stage_parent(stage_fd)
                        clone_fd = backend.open_leaf(stage_fd, clone_path.name)
                        try:
                            clone_snapshot = backend.snapshot_file(clone_fd)
                        finally:
                            os.close(clone_fd)
                    finally:
                        os.close(stage_fd)
                    expected_clone = clone_snapshot.identity
                    expected_snapshot = backend.snapshot_expectation(clone_snapshot)
                    expected_size = clone_snapshot.identity.size
                    expected_sha256 = clone_snapshot.sha256

                primary = IntentHandoffBomb(
                    f"INTENT {injection_site} raw handoff interrupt"
                )
                observed: Dict[str, Any] = {}
                actions = []
                backend.inject_enabled = True
                acquired_line = self.source_line_number(
                    self.backend_module._OwnedFD.__enter__.__code__, "if self.closed:"
                )

                def after_intent_raw_return(frame: Any) -> bool:
                    target = frame.f_locals.get("self")
                    if (
                        not backend.inject_now
                        or target is None
                        or target.closed
                        or target.fileno() != backend.raw_fd
                        or frame.f_lineno != acquired_line
                    ):
                        return False
                    observed["owner"] = target
                    observed["close"] = self.observe_fd_owner_close(target)
                    return True

                baseline_fds = self.open_fd_set()
                try:
                    self.interrupt_on_traced_local_handoff(
                        self.backend_module._OwnedFD.__enter__.__code__,
                        after_intent_raw_return,
                        lambda: backend.cleanup_intent_stage(
                            str(stage_path),
                            final_path=str(final_path),
                            expected_container=container_identity,
                            expected_original=original_expectation,
                            expected_stage=expected_stage,
                            allow_clone=True,
                            expected_clone=expected_clone,
                            expected_snapshot=expected_snapshot,
                            expected_size=expected_size,
                            expected_sha256=expected_sha256,
                            authorize_state=actions.append,
                        ),
                        primary,
                    )
                    self.assertEqual(self.open_fd_set(), baseline_fds)
                    self.assertEqual(backend.target_returns, 1)
                    self.assertEqual(actions, [])
                    self.assertTrue(observed["owner"].closed)
                    self.assertEqual(observed["close"]["active_closes"], 1)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.raw_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertEqual(final_path.read_bytes(), b"durable-original\n")
                    if injection_site == "clone":
                        self.assertTrue(stage_path.is_dir())
                        self.assertEqual(clone_path.read_bytes(), b"durable-clone\n")
                    else:
                        self.assertFalse(stage_path.exists())
                    observed["owner"].close(primary_error=primary)
                    self.assertEqual(observed["close"]["active_closes"], 1)
                finally:
                    if clone_path.exists():
                        clone_path.unlink()
                    if stage_path.exists():
                        stage_path.rmdir()
                    final_path.unlink(missing_ok=True)

    def test_helper_leaf_acquires_honor_raw_and_owned_dispatch_overrides(self) -> None:
        backend_module = self.backend_module

        class DispatchBomb(BaseException):
            pass

        for override_kind in ("raw", "owned"):
            for site in ("source", "destination", "published", "partial"):
                with self.subTest(override_kind=override_kind, site=site):
                    primary = DispatchBomb(f"{override_kind} {site} dispatch interrupt")

                    class DispatchBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            inner_self.armed = False
                            inner_self.calls = []
                            inner_self.raw_fd = -1

                        def open_leaf(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            *,
                            writable: bool = False,
                        ) -> int:
                            fd = super().open_leaf(parent_fd, name, writable=writable)
                            if override_kind == "raw" and inner_self.armed:
                                inner_self.calls.append((parent_fd, name, writable))
                                inner_self.raw_fd = fd
                            return fd

                        def _open_leaf_owned(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            *,
                            writable: bool = False,
                        ) -> Any:
                            if override_kind == "owned" and inner_self.armed:
                                inner_self.calls.append((parent_fd, name, writable))
                                raise primary
                            return super()._open_leaf_owned(
                                parent_fd, name, writable=writable
                            )

                    backend = DispatchBackend()
                    transaction = self.helper.MirrorSync(backend)
                    transaction.source_path = str(self.source.absolute())
                    transaction.destination_path = str(self.destination.absolute())
                    self.write_source(b"helper-dispatch-source\n")
                    candidate_path: Optional[pathlib.Path] = None

                    if site in {"destination", "published"}:
                        self.write_destination(b"helper-dispatch-destination\n")
                    if site == "source":
                        operation = transaction._bind_source
                    elif site == "destination":
                        operation = transaction._bind_destination
                    elif site == "published":
                        transaction._bind_source()
                        transaction._bind_destination()
                        self.assertIsNotNone(transaction.destination_snapshot)
                        self.assertIsNotNone(transaction.destination_identity)
                        transaction.candidate_expectation = (
                            backend.snapshot_expectation(
                                transaction.destination_snapshot
                            )
                        )
                        expected = transaction.destination_identity

                        def require_updated_result_binding() -> None:
                            transaction._require_updated_result_binding(expected)

                        operation = require_updated_result_binding
                    else:
                        candidate_path = self.destination_parent / (
                            f"partial-dispatch-{override_kind}.jsonl"
                        )
                        candidate_path.write_bytes(b"partial-dispatch\n")
                        candidate_path.chmod(0o600)
                        stage_owner, candidate_name = (
                            backend._open_absolute_parent_owned(str(candidate_path))
                        )
                        with stage_owner:
                            transaction._install_fd_owner("_stage_owner", stage_owner)
                        transaction.candidate_name = candidate_name
                        operation = transaction._bind_partial_candidate_if_present

                    backend.calls.clear()
                    backend.armed = True
                    acquired_line = self.source_line_number(
                        self.backend_module._OwnedFD.__enter__.__code__,
                        "if self.closed:",
                    )
                    observed: Dict[str, Any] = {}
                    try:
                        if override_kind == "raw":

                            def after_raw_override_return(frame: Any) -> bool:
                                target = frame.f_locals.get("self")
                                if (
                                    not backend.calls
                                    or target is None
                                    or target.closed
                                    or target.fileno() != backend.raw_fd
                                    or frame.f_lineno != acquired_line
                                ):
                                    return False
                                observed["owner"] = target
                                observed["close"] = self.observe_fd_owner_close(target)
                                return True

                            self.interrupt_on_traced_local_handoff(
                                self.backend_module._OwnedFD.__enter__.__code__,
                                after_raw_override_return,
                                operation,
                                primary,
                            )
                            self.assertTrue(observed["owner"].closed)
                            self.assertEqual(observed["close"]["active_closes"], 1)
                            with self.assertRaises(OSError) as closed:
                                os.fstat(backend.raw_fd)
                            self.assertEqual(closed.exception.errno, errno.EBADF)
                            observed["owner"].close(primary_error=primary)
                            self.assertEqual(observed["close"]["active_closes"], 1)
                        else:
                            escaped: Optional[BaseException] = None
                            try:
                                operation()
                            except BaseException as exc:
                                escaped = exc
                            else:
                                self.fail("owned leaf override was bypassed")
                            self.assertIs(escaped, primary)

                        self.assertEqual(len(backend.calls), 1)
                        parent_fd, name, writable = backend.calls[0]
                        self.assertFalse(writable)
                        if site == "source":
                            self.assertEqual(parent_fd, transaction.source_parent_fd)
                            self.assertEqual(name, transaction.source_name)
                        elif site in {"destination", "published"}:
                            self.assertEqual(
                                parent_fd, transaction.destination_parent_fd
                            )
                            self.assertEqual(name, transaction.destination_name)
                        else:
                            self.assertEqual(parent_fd, transaction.stage_fd)
                            self.assertEqual(name, transaction.candidate_name)
                    finally:
                        transaction._close_all(primary_error=primary)
                        if candidate_path is not None:
                            candidate_path.unlink(missing_ok=True)

    def test_compat_fd_slot_rejects_live_owner_replacement_before_adoption(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"compat-slot-old-owner\n")
        parent_fd, name = backend.open_absolute_parent(str(self.source))
        try:
            old_fd = backend.open_leaf(parent_fd, name)
            new_fd = backend.open_leaf(parent_fd, name)
        finally:
            os.close(parent_fd)

        transaction = self.helper.MirrorSync(backend)
        transaction.source_fd = old_fd
        old_owner = transaction._source_owner
        self.assertIsNotNone(old_owner)
        old_close = self.observe_fd_owner_close(old_owner)
        try:
            with mock.patch.object(
                backend, "_adopt_fd", wraps=backend._adopt_fd
            ) as adopt:
                with self.assertRaises(self.backend_module.BackendError) as caught:
                    transaction.source_fd = new_fd
            self.assertEqual(caught.exception.reason, "fd_already_owned")
            adopt.assert_not_called()
            self.assertIs(transaction._source_owner, old_owner)
            self.assertEqual(transaction.source_fd, old_fd)
            self.assertEqual(os.fstat(old_fd).st_ino, self.source.stat().st_ino)
            self.assertEqual(os.fstat(new_fd).st_ino, self.source.stat().st_ino)
            self.assertEqual(old_close["active_closes"], 0)

            os.close(new_fd)
            with self.assertRaises(OSError) as closed:
                os.fstat(new_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            transaction._close_all(primary_error=None)
            transaction._close_all(primary_error=None)
            self.assertTrue(old_owner.closed)
            self.assertEqual(old_close["active_closes"], 1)
        finally:
            if not old_owner.closed:
                transaction._close_all(primary_error=None)
            try:
                os.close(new_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

    def test_compat_fd_slot_raw_adoption_interruptions_close_once(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"compat-slot-raw-adoption\n")

        class CompatSlotAdoptionBomb(BaseException):
            pass

        boundaries = (
            (
                "descriptor",
                self.helper._CompatFDSlot.__set__.__code__,
                "_adoption_guard = 1",
            ),
            (
                "factory",
                self.backend_module.DarwinBackend._adopt_fd.__code__,
                "_adoption_guard = 1",
            ),
            (
                "state-store",
                self.backend_module._OwnedFD._adopt.__code__,
                "self._state.fd = fd",
            ),
        )
        for boundary, target_code, marker in boundaries:
            with self.subTest(boundary=boundary):
                transaction = self.helper.MirrorSync(backend)
                raw_fd = os.open(self.source, os.O_RDONLY)
                primary = CompatSlotAdoptionBomb(
                    f"compat slot {boundary} adoption interruption"
                )
                target_line = self.source_line_number(target_code, marker)
                close_calls: list[int] = []
                real_close = os.close

                def recording_close(fd: int) -> None:
                    if fd == raw_fd:
                        close_calls.append(fd)
                    real_close(fd)

                def at_adoption_boundary(frame: Any) -> bool:
                    if frame.f_lineno != target_line:
                        return False
                    if boundary == "descriptor":
                        return (
                            frame.f_locals.get("instance") is transaction
                            and frame.f_locals.get("value") == raw_fd
                            and frame.f_locals.get("pending_fd") == [raw_fd]
                            and frame.f_locals.get("replacement") is None
                            and transaction._source_owner is None
                        )
                    if boundary == "factory":
                        return (
                            frame.f_locals.get("self") is backend
                            and frame.f_locals.get("fd") == raw_fd
                            and frame.f_locals.get("receipt") == [raw_fd]
                            and frame.f_locals.get("owner") is None
                        )
                    owner = frame.f_locals.get("self")
                    return (
                        frame.f_locals.get("fd") == raw_fd
                        and owner is not None
                        and owner.closed
                    )

                try:
                    with mock.patch.object(
                        self.backend_module.os,
                        "close",
                        side_effect=recording_close,
                    ):
                        self.interrupt_on_traced_local_handoff(
                            target_code,
                            at_adoption_boundary,
                            lambda: setattr(transaction, "source_fd", raw_fd),
                            primary,
                        )
                        self.assertIsNone(transaction._source_owner)
                        self.assertEqual(transaction.source_fd, -1)
                        self.assertEqual(close_calls, [raw_fd])
                        with self.assertRaises(OSError) as closed:
                            os.fstat(raw_fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                        transaction._close_all(primary_error=primary)
                        transaction._close_all(primary_error=primary)
                        self.assertEqual(close_calls, [raw_fd])
                finally:
                    transaction._close_all(primary_error=primary)
                    try:
                        real_close(raw_fd)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise

        transaction = self.helper.MirrorSync(backend)
        raw_fd = os.open(self.source, os.O_RDONLY)
        real_close = os.close
        close_calls = []

        def recording_normal_close(fd: int) -> None:
            if fd == raw_fd:
                close_calls.append(fd)
            real_close(fd)

        try:
            with mock.patch.object(
                self.backend_module.os,
                "close",
                side_effect=recording_normal_close,
            ):
                transaction.source_fd = raw_fd
                owner = transaction._source_owner
                self.assertIsNotNone(owner)
                self.assertFalse(owner.closed)
                self.assertEqual(transaction.source_fd, raw_fd)
                self.assertEqual(close_calls, [])
                transaction._close_all(primary_error=None)
                transaction._close_all(primary_error=None)
                self.assertTrue(owner.closed)
                self.assertIsNone(transaction._source_owner)
                self.assertEqual(transaction.source_fd, -1)
                self.assertEqual(close_calls, [raw_fd])
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            transaction._close_all(primary_error=None)
            try:
                real_close(raw_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

    def test_bound_owner_alias_rejection_does_not_close_existing_fd(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"bound-owner-alias\n")
        raw_fd = os.open(self.source, os.O_RDONLY)
        transaction = object.__new__(self.backend_module.BoundTransaction)
        transaction._initialize(
            backend,
            None,
            str(self.destination),
            str(self.destination_parent / "alias-stage"),
        )
        existing = backend._adopt_fd(raw_fd, "existing alias owner")
        with existing:
            transaction._install_fd_owner("_source_owner", existing)
        existing_close = self.observe_fd_owner_close(existing)
        duplicate = backend._adopt_fd(raw_fd, "duplicate alias owner")
        try:
            with duplicate:
                with self.assertRaises(self.backend_module.BackendError) as caught:
                    transaction._install_fd_owner("_original_owner", duplicate)
            self.assertEqual(caught.exception.reason, "fd_alias_rejected")
            self.assertTrue(duplicate.closed)
            self.assertIs(transaction._source_owner, existing)
            self.assertFalse(existing.closed)
            self.assertEqual(existing.fileno(), raw_fd)
            self.assertEqual(os.fstat(raw_fd).st_ino, self.source.stat().st_ino)
            self.assertEqual(existing_close["active_closes"], 0)
            transaction.close()
            transaction.close()
            self.assertTrue(existing.closed)
            self.assertEqual(existing_close["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            duplicate.close()
            transaction.close()
            try:
                os.close(raw_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

    def test_bound_alias_disarm_interruption_preserves_existing_fd(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"bound-owner-alias-trace\n")
        raw_fd = os.open(self.source, os.O_RDONLY)
        transaction = object.__new__(self.backend_module.BoundTransaction)
        transaction._initialize(
            backend,
            None,
            str(self.destination),
            str(self.destination_parent / "alias-trace-stage"),
        )
        existing = backend._adopt_fd(raw_fd, "existing trace alias owner")
        with existing:
            transaction._install_fd_owner("_source_owner", existing)
        existing_close = self.observe_fd_owner_close(existing)
        duplicate = backend._adopt_fd(raw_fd, "duplicate trace alias owner")

        class AliasDisarmBomb(BaseException):
            pass

        primary = AliasDisarmBomb("alias disarm return interruption")
        alias_rejection_line = self.source_line_number(
            transaction._install_fd_owner.__code__,
            "raise BackendError(",
            occurrence=2,
        )

        def after_duplicate_disarm(frame: Any) -> bool:
            return (
                frame.f_locals.get("self") is transaction
                and frame.f_lineno == alias_rejection_line
                and frame.f_locals.get("owner") is duplicate
                and duplicate.closed
                and transaction._source_owner is existing
                and not existing.closed
            )

        try:
            with duplicate:
                self.interrupt_on_traced_local_handoff(
                    transaction._install_fd_owner.__code__,
                    after_duplicate_disarm,
                    lambda: transaction._install_fd_owner("_original_owner", duplicate),
                    primary,
                )
            self.assertTrue(duplicate.closed)
            self.assertIs(transaction._source_owner, existing)
            self.assertFalse(existing.closed)
            self.assertEqual(existing.fileno(), raw_fd)
            self.assertEqual(os.fstat(raw_fd).st_ino, self.source.stat().st_ino)
            self.assertEqual(existing_close["active_closes"], 0)
            transaction.close(primary_error=primary)
            transaction.close(primary_error=primary)
            self.assertTrue(existing.closed)
            self.assertEqual(existing_close["active_closes"], 1)
        finally:
            duplicate.close(primary_error=primary)
            transaction.close(primary_error=primary)
            try:
                os.close(raw_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

    def test_adopt_fd_return_line_interruption_closes_handoff_once(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"adopt-return-line\n")
        raw_fd = os.open(self.source, os.O_RDONLY)

        class AdoptReturnBomb(BaseException):
            pass

        primary = AdoptReturnBomb("adopt fd return-line interruption")
        observed: Dict[str, Any] = {}
        adopt_return_line = self.source_line_number(
            backend._adopt_fd.__code__, "return owner"
        )

        def after_adoption_before_return(frame: Any) -> bool:
            owner = frame.f_locals.get("owner")
            if (
                owner is None
                or owner.closed
                or owner.fileno() != raw_fd
                or frame.f_lineno != adopt_return_line
            ):
                return False
            observed["owner"] = owner
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                backend._adopt_fd.__code__,
                after_adoption_before_return,
                lambda: backend._adopt_fd(raw_fd, "adopt return-line owner"),
                primary,
            )
            owner = observed["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(observed["close"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            owner.close(primary_error=primary)
            self.assertEqual(observed["close"]["active_closes"], 1)
        finally:
            try:
                os.close(raw_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

    def test_owned_fd_lifecycle_caller_trace_preserves_primary_and_drains_once(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"owned-fd-lifecycle-trace\n")

        class NaturalFDPrimary(BaseException):
            pass

        class FDCallerBomb(BaseException):
            pass

        enter_code = self.backend_module._OwnedFD.__enter__.__code__
        exit_code = self.backend_module._OwnedFD.__exit__.__code__
        enter_close_line = self.source_line_number(
            enter_code, "self._handle_acquisition_failure(primary)"
        )
        exit_close_line = self.source_line_number(
            exit_code, "self._handle_exception(primary_error)"
        )

        for boundary in ("enter", "exit"):
            with self.subTest(boundary=boundary):
                raw_fd = os.open(self.source, os.O_RDONLY)
                primary = NaturalFDPrimary(f"fd {boundary} natural primary")
                secondary = FDCallerBomb(f"fd {boundary} caller interruption")

                if boundary == "enter":

                    def acquire(target: Any) -> None:
                        target._adopt(raw_fd)
                        raise primary

                    owner = self.backend_module._OwnedFD(
                        backend, "fd enter caller trace", acquire
                    )
                    target_code = enter_code
                    target_line = enter_close_line
                    operation = owner.__enter__

                    def at_close_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary") is primary
                            and frame.f_lineno == target_line
                        )

                else:
                    owner = backend._adopt_fd(raw_fd, "fd exit caller trace")
                    target_code = exit_code
                    target_line = exit_close_line

                    def operation() -> None:
                        with owner:
                            raise primary

                    def at_close_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary_error") is primary
                            and frame.f_lineno == target_line
                        )

                close_observation = self.observe_fd_owner_close(owner)
                try:
                    self.interrupt_handler_preserving_primary(
                        target_code,
                        at_close_call,
                        operation,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(owner.closed)
                    self.assertEqual(close_observation["active_closes"], 1)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(raw_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertIn(
                        "caller interruption",
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                    owner.close(primary_error=primary)
                    self.assertEqual(close_observation["active_closes"], 1)
                finally:
                    owner.close(primary_error=primary)
                    try:
                        os.close(raw_fd)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise

    def test_owned_acl_lifecycle_caller_trace_preserves_primary_and_frees_once(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()

        class NaturalACLPrimary(BaseException):
            pass

        class ACLCallerBomb(BaseException):
            pass

        enter_code = self.backend_module._OwnedACL.__enter__.__code__
        exit_code = self.backend_module._OwnedACL.__exit__.__code__
        enter_close_line = self.source_line_number(
            enter_code, "self._handle_acquisition_failure(primary)"
        )
        exit_close_line = self.source_line_number(
            exit_code, "self._handle_exception(primary_error)"
        )

        for index, boundary in enumerate(("enter", "exit"), start=1):
            with self.subTest(boundary=boundary):
                pointer_value = 3_000 + index
                pointer = ctypes.c_void_p(pointer_value)
                primary = NaturalACLPrimary(f"ACL {boundary} natural primary")
                secondary = ACLCallerBomb(f"ACL {boundary} caller interruption")
                free_calls = []

                def record_free(freed_pointer: Any) -> int:
                    free_calls.append(freed_pointer.value)
                    return 0

                if boundary == "enter":

                    def acquire(target: Any) -> None:
                        target._adopt(pointer)
                        raise primary

                    owner = self.backend_module._OwnedACL(
                        backend, "ACL enter caller trace", acquire
                    )
                    target_code = enter_code
                    target_line = enter_close_line
                    operation = owner.__enter__

                    def at_free_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary") is primary
                            and frame.f_lineno == target_line
                        )

                else:

                    def acquire(target: Any) -> None:
                        target._adopt(pointer)

                    owner = self.backend_module._OwnedACL(
                        backend, "ACL exit caller trace", acquire
                    )
                    target_code = exit_code
                    target_line = exit_close_line

                    def operation() -> None:
                        with owner:
                            raise primary

                    def at_free_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("exc_value") is primary
                            and frame.f_lineno == target_line
                        )

                close_observation = self.observe_acl_owner_close(owner)
                with mock.patch.object(backend, "_acl_free", side_effect=record_free):
                    try:
                        self.interrupt_handler_preserving_primary(
                            target_code,
                            at_free_call,
                            operation,
                            secondary,
                            expected_primary=primary,
                        )
                        self.assertTrue(owner.closed)
                        self.assertEqual(close_observation["active_closes"], 1)
                        self.assertEqual(free_calls, [pointer_value])
                        self.assertIn(
                            "caller interruption",
                            getattr(primary, "cleanup_diagnostic", ""),
                        )
                        owner.close(primary_error=primary)
                        self.assertEqual(close_observation["active_closes"], 1)
                        self.assertEqual(free_calls, [pointer_value])
                    finally:
                        owner.close(primary_error=primary)

    def test_empty_acl_disarm_has_no_python_visible_post_store_window(self) -> None:
        backend = self.backend_module.DarwinBackend()
        pointer_value = 3_200
        free_calls = []

        def record_free(pointer: Any) -> int:
            free_calls.append(pointer.value)
            return 0

        handoff_code = backend._handoff_acl_owner.__code__
        self.assertNotIn("pointer", handoff_code.co_varnames)
        self.source_line_number(
            handoff_code,
            "owner.__enter__(); return owner.disarm()  # noqa: E702",
        )
        with mock.patch.object(backend, "_acl_init", return_value=pointer_value):
            with mock.patch.object(backend, "_acl_free", side_effect=record_free):
                pointer = backend._empty_acl()
                self.assertIsInstance(pointer, ctypes.c_void_p)
                self.assertEqual(pointer.value, pointer_value)
                backend._free_acl(pointer)
        self.assertEqual(free_calls, [pointer_value])

    def test_public_raw_handoffs_do_not_cross_normal_owner_exit(self) -> None:
        backend_module = self.backend_module
        self.write_source(b"public raw handoff no normal exit\n")

        class PostDisarmExitBomb(BaseException):
            pass

        class CapturingBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.handoff_owners: Dict[str, Any] = {}
                inner_self.handoff_values: Dict[str, Any] = {}

            def _capture_disarm(inner_self, label: str, owner: Any) -> Any:
                inner_self.handoff_owners[label] = owner
                real_disarm = owner.disarm

                def recording_disarm() -> Any:
                    value = real_disarm()
                    inner_self.handoff_values[label] = value
                    return value

                owner.disarm = recording_disarm
                return owner

            def _open_leaf_owned(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                writable: bool = False,
            ) -> Any:
                owner = super()._open_leaf_owned(
                    parent_fd,
                    name,
                    writable=writable,
                )
                return inner_self._capture_disarm("fd", owner)

            def _empty_acl_owned(inner_self) -> Any:
                owner = super()._empty_acl_owned()
                return inner_self._capture_disarm("acl", owner)

            def _create_private_stage_parent_core_owned(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
            ) -> Any:
                owner = super()._create_private_stage_parent_core_owned(
                    parent_fd,
                    name,
                    authorize_state=authorize_state,
                )
                return inner_self._capture_disarm("stage", owner)

        fd_exit_code = backend_module._OwnedFD.__exit__.__code__
        acl_exit_code = backend_module._OwnedACL.__exit__.__code__
        fd_exit_line = self.source_line_number(fd_exit_code, "if exc_type is None:")
        acl_exit_line = self.source_line_number(
            acl_exit_code,
            "if exc_type is None:",
        )

        for case in ("fd", "acl", "stage"):
            with self.subTest(case=case):
                backend = CapturingBackend()
                parent_fd = -1
                result: Any = None
                stage_path = self.destination_parent / f"raw-handoff-{case}"
                actions: list[str] = []
                free_calls: list[int] = []
                expected_container = None
                pointer_value = 3_503
                if case == "fd":
                    parent_fd, name = backend.open_absolute_parent(str(self.source))

                    def operation() -> Any:
                        return backend.open_leaf(parent_fd, name)

                    target_code = fd_exit_code
                    target_line = fd_exit_line
                elif case == "acl":
                    operation = backend._empty_acl
                    target_code = acl_exit_code
                    target_line = acl_exit_line
                else:
                    parent_fd, name = backend.open_absolute_parent(str(stage_path))
                    expected_container = backend.validate_stage_container(parent_fd)

                    def operation() -> Any:
                        return backend.create_private_stage_parent(
                            parent_fd,
                            name,
                            authorize_state=actions.append,
                        )

                    target_code = fd_exit_code
                    target_line = fd_exit_line

                primary = PostDisarmExitBomb(
                    f"{case} post-disarm normal-exit interruption"
                )
                captured: Dict[str, Any] = {}
                previous_trace = sys.gettrace()

                def trace(frame: Any, event: str, _arg: Any) -> Any:
                    expected_owner = backend.handoff_owners.get(case)
                    if (
                        event == "line"
                        and frame.f_code is target_code
                        and frame.f_lineno == target_line
                        and expected_owner is not None
                        and frame.f_locals.get("self") is expected_owner
                        and frame.f_locals.get("exc_type") is None
                        and expected_owner.closed
                        and case in backend.handoff_values
                        and not captured
                    ):
                        captured["line"] = frame.f_lineno
                        sys.settrace(None)
                        raise primary
                    return trace

                escaped: Optional[BaseException] = None

                def record_free(pointer: Any) -> int:
                    free_calls.append(pointer.value)
                    return 0

                try:
                    sys.settrace(trace)
                    try:
                        if case == "acl":
                            with mock.patch.object(
                                backend,
                                "_acl_init",
                                return_value=pointer_value,
                            ):
                                result = operation()
                        else:
                            result = operation()
                    except BaseException as error:
                        escaped = error
                    finally:
                        sys.settrace(previous_trace)

                    self.assertEqual(captured, {})
                    self.assertIsNone(
                        escaped,
                        f"{case} raw handoff crossed normal owner exit",
                    )
                    owner = backend.handoff_owners[case]
                    self.assertTrue(owner.closed)
                    if case == "fd":
                        self.assertIsInstance(result, int)
                        self.assertEqual(
                            os.fstat(result).st_ino,
                            self.source.stat().st_ino,
                        )
                        os.close(result)
                        with self.assertRaises(OSError) as closed:
                            os.fstat(result)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                        result = None
                    elif case == "acl":
                        self.assertIsInstance(result, ctypes.c_void_p)
                        self.assertEqual(result.value, pointer_value)
                        with mock.patch.object(
                            backend,
                            "_acl_free",
                            side_effect=record_free,
                        ):
                            backend._free_acl(result)
                        self.assertEqual(free_calls, [pointer_value])
                        result = None
                    else:
                        stage_fd, identity = result
                        self.assertTrue(stage_path.is_dir())
                        self.assertTrue(stat.S_ISDIR(os.fstat(stage_fd).st_mode))
                        os.close(stage_fd)
                        with self.assertRaises(OSError) as closed:
                            os.fstat(stage_fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                        backend.remove_empty_private_stage(
                            str(stage_path),
                            identity,
                            expected_container=expected_container,
                            authorize_state=actions.append,
                        )
                        self.assertEqual(actions, ["create_stage", "remove_stage"])
                        self.assertFalse(stage_path.exists())
                        result = None
                finally:
                    sys.settrace(previous_trace)
                    handed_off = backend.handoff_values.get(case)
                    if case == "fd" and isinstance(handed_off, int):
                        try:
                            os.close(handed_off)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise
                    elif case == "acl" and isinstance(
                        handed_off,
                        ctypes.c_void_p,
                    ):
                        if not free_calls:
                            with mock.patch.object(
                                backend,
                                "_acl_free",
                                side_effect=record_free,
                            ):
                                backend._free_acl(handed_off)
                    elif case == "stage" and isinstance(handed_off, int):
                        try:
                            os.close(handed_off)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise
                    if stage_path.exists():
                        stage_path.rmdir()
                    if parent_fd >= 0:
                        os.close(parent_fd)

    def test_owned_transaction_lifecycle_caller_trace_preserves_primary_and_drains(
        self,
    ) -> None:
        class NaturalTransactionPrimary(BaseException):
            pass

        class TransactionCallerBomb(BaseException):
            pass

        enter_code = self.backend_module._OwnedTransaction.__enter__.__code__
        exit_code = self.backend_module._OwnedTransaction.__exit__.__code__
        enter_close_line = self.source_line_number(
            enter_code, "self._handle_acquisition_failure(primary)"
        )
        exit_close_line = self.source_line_number(
            exit_code, "self._handle_exception(primary_error)"
        )

        for boundary in ("enter", "exit"):
            with self.subTest(boundary=boundary):
                transaction = object.__new__(self.backend_module.BoundTransaction)
                transaction._closed = False
                close_calls = []

                def close(*, primary_error: Optional[BaseException] = None) -> None:
                    close_calls.append(primary_error)
                    transaction._closed = True

                transaction.close = close
                primary = NaturalTransactionPrimary(
                    f"transaction {boundary} natural primary"
                )
                secondary = TransactionCallerBomb(
                    f"transaction {boundary} caller interruption"
                )

                if boundary == "enter":

                    def acquire(target: Any) -> None:
                        target._adopt(transaction)
                        raise primary

                    owner = self.backend_module._OwnedTransaction(acquire)
                    target_code = enter_code
                    target_line = enter_close_line
                    operation = owner.__enter__

                    def at_close_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary") is primary
                            and frame.f_lineno == target_line
                        )

                else:

                    def acquire(target: Any) -> None:
                        target._adopt(transaction)

                    owner = self.backend_module._OwnedTransaction(acquire)
                    target_code = exit_code
                    target_line = exit_close_line

                    def operation() -> None:
                        with owner:
                            raise primary

                    def at_close_call(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary_error") is primary
                            and frame.f_lineno == target_line
                        )

                try:
                    self.interrupt_handler_preserving_primary(
                        target_code,
                        at_close_call,
                        operation,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(owner.closed)
                    self.assertTrue(transaction._closed)
                    self.assertEqual(close_calls, [primary])
                    self.assertIn(
                        "caller interruption",
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                    owner.close(primary_error=primary)
                    self.assertEqual(close_calls, [primary])
                finally:
                    owner.close(primary_error=primary)

    def test_owned_transaction_outer_cleanup_boundaries_retry_and_preserve_primary(
        self,
    ) -> None:
        class NaturalTransactionPrimary(BaseException):
            pass

        class TransactionBoundaryBomb(BaseException):
            pass

        for boundary in ("enter-handler", "exit-handler"):
            with self.subTest(boundary=boundary):
                transaction = object.__new__(self.backend_module.BoundTransaction)
                transaction._closed = False
                close_calls = []

                def close(*, primary_error: Optional[BaseException] = None) -> None:
                    close_calls.append(primary_error)
                    transaction._closed = True

                transaction.close = close
                primary = NaturalTransactionPrimary(
                    f"transaction {boundary} natural primary"
                )
                secondary = TransactionBoundaryBomb(
                    f"transaction {boundary} interruption"
                )

                if boundary == "enter-handler":

                    def acquire(target: Any) -> None:
                        target._adopt(transaction)
                        raise primary

                    owner = self.backend_module._OwnedTransaction(acquire)
                    target_code = owner.__enter__.__code__
                    target_line = self.source_line_number(
                        target_code, "self._handle_acquisition_failure(primary)"
                    )
                    operation = owner.__enter__

                    def at_unprotected_boundary(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary") is primary
                            and frame.f_lineno == target_line
                        )

                else:

                    def acquire(target: Any) -> None:
                        target._adopt(transaction)

                    owner = self.backend_module._OwnedTransaction(acquire)
                    target_code = owner.__exit__.__code__
                    target_line = self.source_line_number(
                        target_code, "self._handle_exception(primary_error)"
                    )

                    def operation() -> None:
                        with owner:
                            raise primary

                    def at_unprotected_boundary(frame: Any) -> bool:
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary_error") is primary
                            and frame.f_lineno == target_line
                        )

                try:
                    self.interrupt_handler_preserving_primary(
                        target_code,
                        at_unprotected_boundary,
                        operation,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(owner.closed)
                    self.assertTrue(transaction._closed)
                    self.assertEqual(close_calls, [primary])
                    diagnostic = getattr(primary, "cleanup_diagnostic", "")
                    self.assertIn("interruption", diagnostic)
                    self.assertLessEqual(
                        len(diagnostic.encode("utf-8")),
                        self.helper._DIAGNOSTIC_LIMIT,
                    )
                    owner.close(primary_error=primary)
                    self.assertEqual(close_calls, [primary])
                finally:
                    owner.close(primary_error=primary)

    def test_bound_transaction_handlers_retry_first_close_interruption_and_drain(
        self,
    ) -> None:
        backend_module = self.backend_module
        self.write_source(b"bound-handler-close-interruption\n")

        class BoundHandlerPrimary(BaseException):
            pass

        class BoundCloseBoundaryBomb(BaseException):
            pass

        def install_descriptors(transaction: Any, descriptors: list[int]) -> None:
            for attribute, descriptor in zip(
                (
                    "source_parent_fd",
                    "destination_parent_fd",
                    "temporary_parent_fd",
                    "source_fd",
                    "original_fd",
                    "clone_fd",
                ),
                descriptors,
            ):
                setattr(transaction, attribute, descriptor)

        expected_parent = backend_module.FileIdentity(
            1,
            900,
            stat.S_IFDIR | 0o700,
            1,
            0,
            os.geteuid(),
            os.getegid(),
            1_700_000_000_000_000_000,
            1_700_000_000_000_000_000,
        )
        expected_original = backend_module.FileIdentity(
            1,
            901,
            stat.S_IFREG | 0o600,
            1,
            1,
            os.geteuid(),
            os.getegid(),
            1_700_000_000_000_000_000,
            1_700_000_000_000_000_000,
        )
        expected_clone = backend_module.FileIdentity(
            1,
            902,
            stat.S_IFREG | 0o600,
            1,
            1,
            os.geteuid(),
            os.getegid(),
            1_700_000_000_000_000_000,
            1_700_000_000_000_000_000,
        )

        for boundary in ("bind", "recover", "exit"):
            with self.subTest(boundary=boundary):
                backend = backend_module.DarwinBackend()
                raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(6)]
                primary = BoundHandlerPrimary(f"bound {boundary} natural primary")
                secondary = BoundCloseBoundaryBomb(
                    f"bound {boundary} first close interruption"
                )
                holder: Dict[str, Any] = {}

                if boundary == "bind":
                    transaction = object.__new__(backend_module.BoundTransaction)
                    transaction._initialize(
                        backend,
                        str(self.source),
                        str(self.destination),
                        str(self.destination_parent / "bound-bind-stage"),
                    )
                    install_descriptors(transaction, raw_fds)
                    holder["transaction"] = transaction

                    def operation() -> None:
                        with mock.patch.object(
                            transaction,
                            "_dispatch_source_parent_owned",
                            side_effect=primary,
                        ):
                            transaction._bind(expected_parent)

                elif boundary == "recover":

                    class RecoveryFailureTransaction(backend_module.BoundTransaction):
                        def _bind_recovery(
                            inner_self, *args: Any, **kwargs: Any
                        ) -> None:
                            del args, kwargs
                            install_descriptors(inner_self, raw_fds)
                            holder["transaction"] = inner_self
                            raise primary

                    def operation() -> None:
                        RecoveryFailureTransaction.recover(
                            backend,
                            None,
                            str(self.destination),
                            str(self.destination_parent / "bound-recover-stage"),
                            expected_original,
                            expected_clone,
                        )

                else:
                    transaction = object.__new__(backend_module.BoundTransaction)
                    transaction._initialize(
                        backend,
                        None,
                        str(self.destination),
                        str(self.destination_parent / "bound-exit-stage"),
                    )
                    install_descriptors(transaction, raw_fds)
                    holder["transaction"] = transaction

                    def operation() -> None:
                        with transaction:
                            raise primary

                close_calls = []
                real_close = backend_module.os.close

                def record_close(fd: int) -> None:
                    close_calls.append(fd)
                    real_close(fd)

                cleanup_line = self.source_line_number(
                    backend_module.BoundTransaction._drain_after_error.__code__,
                    "_cleanup_attempt = _attempt + 1",
                )

                def at_first_close_entry(frame: Any) -> bool:
                    return (
                        frame.f_locals.get("self") is holder.get("transaction")
                        and frame.f_locals.get("primary_error") is primary
                        and frame.f_locals.get("_attempt") == 0
                        and frame.f_lineno == cleanup_line
                    )

                with mock.patch.object(
                    backend_module.os, "close", side_effect=record_close
                ):
                    try:
                        self.interrupt_handler_preserving_primary(
                            backend_module.BoundTransaction._drain_after_error.__code__,
                            at_first_close_entry,
                            operation,
                            secondary,
                            expected_primary=primary,
                        )
                        transaction = holder["transaction"]
                        self.assertTrue(transaction._closed)
                        self.assertCountEqual(close_calls, raw_fds)
                        self.assertEqual(len(close_calls), len(raw_fds))
                        self.assertTrue(
                            all(
                                getattr(transaction, attribute) == -1
                                for attribute in (
                                    "source_parent_fd",
                                    "destination_parent_fd",
                                    "temporary_parent_fd",
                                    "source_fd",
                                    "original_fd",
                                    "clone_fd",
                                )
                            )
                        )
                        diagnostic = getattr(primary, "cleanup_diagnostic", "")
                        self.assertIn("first close interruption", diagnostic)
                        self.assertLessEqual(
                            len(diagnostic.encode("utf-8")),
                            self.helper._DIAGNOSTIC_LIMIT,
                        )
                    finally:
                        transaction = holder.get("transaction")
                        if transaction is not None:
                            transaction.close(primary_error=primary)
                for fd in raw_fds:
                    try:
                        os.close(fd)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise

    def test_absolute_directory_owner_cleanup_retries_and_drains_reverse_chain(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()

        class DirectoryValidationPrimary(BaseException):
            pass

        class DirectoryCleanupBomb(BaseException):
            pass

        primary = DirectoryValidationPrimary("absolute directory natural primary")
        secondary = DirectoryCleanupBomb("absolute directory cleanup interruption")
        owner = backend._open_absolute_dir_owned(str(self.source_parent))
        opened_fds = []
        close_calls = []
        require_calls = 0
        real_open = self.backend_module.os.open
        real_close = self.backend_module.os.close
        real_require = backend._require_directory

        def record_open(*args: Any, **kwargs: Any) -> int:
            fd = real_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        def record_close(fd: int) -> None:
            close_calls.append(fd)
            real_close(fd)

        def fail_second_directory_check(fd: int, subject: str) -> None:
            nonlocal require_calls
            require_calls += 1
            if require_calls == 2:
                raise primary
            real_require(fd, subject)

        def at_first_cleanup(frame: Any) -> bool:
            cleanup_owner = frame.f_locals.get("self")
            return (
                cleanup_owner is not None
                and cleanup_owner._backend is backend
                and not cleanup_owner.closed
                and frame.f_locals.get("primary_error") is primary
                and frame.f_locals.get("_attempt") == 0
                and frame.f_lineno == cleanup_line
            )

        cleanup_line = self.source_line_number(
            self.backend_module._OwnedFD._drain.__code__,
            "_cleanup_attempt = _attempt + 1",
        )

        try:
            with mock.patch.object(
                self.backend_module.os, "open", side_effect=record_open
            ):
                with mock.patch.object(
                    self.backend_module.os, "close", side_effect=record_close
                ):
                    with mock.patch.object(
                        backend,
                        "_require_directory",
                        side_effect=fail_second_directory_check,
                    ):
                        self.interrupt_handler_preserving_primary(
                            self.backend_module._OwnedFD._drain.__code__,
                            at_first_cleanup,
                            owner.__enter__,
                            secondary,
                            expected_primary=primary,
                        )
            self.assertGreaterEqual(len(opened_fds), 2)
            self.assertEqual(close_calls, list(reversed(opened_fds)))
            self.assertTrue(owner.closed)
            diagnostic = getattr(primary, "cleanup_diagnostic", "")
            self.assertIn("cleanup interruption", diagnostic)
        finally:
            owner.close(primary_error=primary)
            for fd in opened_fds:
                try:
                    real_close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_publish_final_owner_cleanup_retries_and_preserves_durable_mapping(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        stage_path = self.destination_parent / "publish-cleanup-stage"
        stage_path.mkdir(mode=0o700)
        candidate = stage_path / "candidate.jsonl"
        candidate.write_bytes(b"publish-cleanup-interruption\n")
        candidate.chmod(0o600)

        class PublishDurabilityPrimary(BaseException):
            pass

        class PublishCleanupBomb(BaseException):
            pass

        primary = PublishDurabilityPrimary("published full-fsync natural primary")
        secondary = PublishCleanupBomb("published owner cleanup interruption")
        stage_fd = backend.open_absolute_dir(str(stage_path))
        destination_parent_fd = backend.open_absolute_dir(str(self.destination_parent))
        stage_expected = backend.identity_at(stage_fd, candidate.name)
        observed: Dict[str, Any] = {}
        actions = []
        cleanup_line = self.source_line_number(
            self.backend_module._OwnedFD._drain.__code__,
            "_cleanup_attempt = _attempt + 1",
        )

        def at_published_owner_cleanup(frame: Any) -> bool:
            owner = frame.f_locals.get("self")
            if (
                owner is None
                or frame.f_locals.get("primary_error") is not primary
                or frame.f_locals.get("_attempt") != 0
                or frame.f_lineno != cleanup_line
            ):
                return False
            observed["owner"] = owner
            observed["fd"] = owner.fileno()
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        try:
            with mock.patch.object(backend, "full_fsync", side_effect=primary):
                self.interrupt_handler_preserving_primary(
                    self.backend_module._OwnedFD._drain.__code__,
                    at_published_owner_cleanup,
                    lambda: backend.publish_staged_name(
                        stage_fd,
                        candidate.name,
                        stage_expected,
                        destination_parent_fd,
                        self.destination.name,
                        None,
                        authorize_namespace=actions.append,
                        validate_after_authorization=lambda: None,
                    ),
                    secondary,
                    expected_primary=primary,
                )
            owner = observed["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(observed["close"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(observed["fd"])
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertEqual(actions, ["publish"])
            self.assertFalse(candidate.exists())
            self.assertEqual(
                self.destination.read_bytes(), b"publish-cleanup-interruption\n"
            )
            self.assertEqual(self.destination.stat().st_ino, stage_expected.ino)
            self.assertIn(
                "cleanup interruption",
                getattr(primary, "cleanup_diagnostic", ""),
            )
        finally:
            owner = observed.get("owner")
            if owner is not None:
                owner.close(primary_error=primary, durable_namespace_complete=True)
            os.close(stage_fd)
            os.close(destination_parent_fd)
            if candidate.exists():
                candidate.unlink()
            if stage_path.exists():
                stage_path.rmdir()

    def test_clone_policy_acl_cleanup_retries_and_drains_both_owners(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"ACL calibration cleanup\n")
        original_fd = os.open(self.source, os.O_RDONLY)
        clone_fd = os.open(self.source, os.O_RDWR)
        info = os.fstat(clone_fd)
        expected = self.backend_module.FilePolicy(
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            int(getattr(info, "st_flags", 0)),
            info.st_mtime_ns,
            (),
            b"",
        )

        class CalibrationPrimary(BaseException):
            pass

        class ACLCleanupBomb(BaseException):
            pass

        primary = CalibrationPrimary("ACL calibration natural primary")
        secondary = ACLCleanupBomb("ACL calibration cleanup interruption")
        pointer_value = 3_300

        def acquire_live(target: Any) -> None:
            target._adopt(None)

        def acquire_apply(target: Any) -> None:
            target._adopt(ctypes.c_void_p(pointer_value))

        live_owner = self.backend_module._OwnedACL(
            backend, "live calibration ACL", acquire_live
        )
        apply_owner = self.backend_module._OwnedACL(
            backend, "empty calibration ACL", acquire_apply
        )
        live_close = self.observe_acl_owner_close(live_owner)
        apply_close = self.observe_acl_owner_close(apply_owner)
        free_calls = []

        def record_free(pointer: Any) -> int:
            free_calls.append(pointer.value)
            return 0

        def at_apply_cleanup(frame: Any) -> bool:
            return (
                frame.f_locals.get("owner") is apply_owner
                and frame.f_locals.get("primary_error") is primary
                and frame.f_locals.get("attempts") == 0
                and frame.f_lineno == cleanup_line
            )

        cleanup_line = self.source_line_number(
            self.backend_module.DarwinBackend._close_acl_owners.__code__,
            "attempts += 1",
        )

        try:
            with mock.patch.object(
                backend,
                "require_exclusive_writer_policy",
                return_value=None,
            ):
                with mock.patch.object(
                    backend, "snapshot_policy", side_effect=(expected, expected)
                ):
                    with mock.patch.object(
                        backend, "_get_acl_owned", return_value=live_owner
                    ):
                        with mock.patch.object(
                            backend, "_empty_acl_owned", return_value=apply_owner
                        ):
                            with mock.patch.object(
                                backend, "_replace_xattrs", side_effect=primary
                            ):
                                with mock.patch.object(
                                    backend, "_acl_free", side_effect=record_free
                                ):
                                    self.interrupt_handler_preserving_primary(
                                        self.backend_module.DarwinBackend._close_acl_owners.__code__,
                                        at_apply_cleanup,
                                        lambda: backend.calibrate_clone_policy(
                                            original_fd, clone_fd, expected
                                        ),
                                        secondary,
                                        expected_primary=primary,
                                    )
            self.assertTrue(apply_owner.closed)
            self.assertTrue(live_owner.closed)
            self.assertEqual(apply_close["active_closes"], 1)
            self.assertEqual(live_close["active_closes"], 1)
            self.assertEqual(free_calls, [pointer_value])
            self.assertIn(
                "cleanup interruption",
                getattr(primary, "cleanup_diagnostic", ""),
            )
        finally:
            with mock.patch.object(backend, "_acl_free", side_effect=record_free):
                apply_owner.close(primary_error=primary)
                live_owner.close(primary_error=primary)
            os.close(original_fd)
            os.close(clone_fd)

    def test_resource_state_destructors_provide_ordinary_best_effort_cleanup(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"state-destructor-best-effort\n")

        raw_fd = os.open(self.source, os.O_RDONLY)
        fd_state = self.backend_module._FDState(backend, "FD state destructor", raw_fd)
        fd_close_calls = []
        real_close = os.close

        def record_close(fd: int) -> None:
            fd_close_calls.append(fd)
            real_close(fd)

        try:
            with mock.patch.object(
                self.backend_module.os, "close", side_effect=record_close
            ):
                fd_state.__del__()
            self.assertEqual(fd_state.fd, -1)
            self.assertEqual(fd_close_calls, [raw_fd])
        finally:
            fd_state.close()
            try:
                os.close(raw_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise

        pointer_value = 3_100
        pointer = ctypes.c_void_p(pointer_value)
        acl_state = self.backend_module._ACLState(
            backend, "ACL state destructor", pointer, True
        )
        acl_free_calls = []

        def record_free(freed_pointer: Any) -> int:
            acl_free_calls.append(freed_pointer.value)
            return 0

        with mock.patch.object(backend, "_acl_free", side_effect=record_free):
            try:
                acl_state.__del__()
                self.assertFalse(acl_state.active)
                self.assertIsNone(acl_state.pointer)
                self.assertEqual(acl_free_calls, [pointer_value])
            finally:
                acl_state.close()

        transaction = object.__new__(self.backend_module.BoundTransaction)
        transaction._closed = False
        close_calls = []

        def close(*, primary_error: Optional[BaseException] = None) -> None:
            close_calls.append(primary_error)
            transaction._closed = True

        transaction.close = close
        state = self.backend_module._TransactionState(transaction)

        try:
            state.__del__()
            self.assertIsNone(state.transaction)
            self.assertEqual(close_calls, [None])
        finally:
            state.close()

    def test_compat_and_bound_fd_slot_store_interruptions_keep_one_owner(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"descriptor-slot-trace\n")
        parent_fd, name = backend.open_absolute_parent(str(self.source))

        class SlotStoreBomb(BaseException):
            pass

        class TraceSlotStoreMixin:
            def __setattr__(target, attribute: str, value: Any) -> None:
                object.__setattr__(target, attribute, value)
                return None

        class TraceMirrorSync(TraceSlotStoreMixin, self.helper.MirrorSync):
            pass

        class TraceBoundTransaction(
            TraceSlotStoreMixin, self.backend_module.BoundTransaction
        ):
            pass

        inner_store_code = TraceSlotStoreMixin.__setattr__.__code__
        inner_store_line = self.source_line_number(inner_store_code, "return None")

        for slot_kind in ("helper", "bound"):
            for boundary in ("before-store", "after-store"):
                with self.subTest(slot_kind=slot_kind, boundary=boundary):
                    if slot_kind == "helper":
                        instance = TraceMirrorSync(backend)
                        owner_attribute = "_source_owner"
                        descriptor_code = self.helper._CompatFDSlot.__set__.__code__
                    else:
                        instance = object.__new__(TraceBoundTransaction)
                        instance._initialize(
                            backend,
                            None,
                            str(self.destination.absolute()),
                            str(self.destination_parent / f"bound-slot-{boundary}"),
                        )
                        owner_attribute = "_source_owner"
                        descriptor_code = (
                            self.backend_module._OwnedFDSlot.__set__.__code__
                        )
                    previous_owner = getattr(instance, owner_attribute)
                    raw_fd = backend.open_leaf(parent_fd, name)
                    primary = SlotStoreBomb(f"{slot_kind} {boundary} interruption")
                    observed: Dict[str, Any] = {}

                    def assign_then_cross_boundary() -> None:
                        instance.source_fd = raw_fd
                        after_assignment = instance.source_fd
                        self.assertEqual(after_assignment, raw_fd)

                    if boundary == "before-store":

                        def before_owner_slot_store(frame: Any) -> bool:
                            replacement = frame.f_locals.get("replacement")
                            if (
                                frame.f_locals.get("instance") is not instance
                                or replacement is None
                                or replacement.closed
                                or getattr(instance, owner_attribute) is replacement
                            ):
                                return False
                            observed["owner"] = replacement
                            observed["close"] = self.observe_fd_owner_close(replacement)
                            return True

                        target_code = descriptor_code
                        predicate = before_owner_slot_store
                    else:

                        def after_owner_slot_store(frame: Any) -> bool:
                            replacement = frame.f_locals.get("value")
                            if (
                                frame.f_lineno != inner_store_line
                                or frame.f_locals.get("target") is not instance
                                or frame.f_locals.get("attribute") != owner_attribute
                                or replacement is None
                                or replacement is previous_owner
                                or getattr(instance, owner_attribute) is not replacement
                                or replacement.closed
                                or replacement.fileno() != raw_fd
                                or replacement._retain_if_registered is None
                            ):
                                return False
                            observed["owner"] = replacement
                            observed["registration"] = replacement._retain_if_registered
                            observed["close"] = self.observe_fd_owner_close(replacement)
                            return True

                        target_code = inner_store_code
                        predicate = after_owner_slot_store
                    try:
                        self.interrupt_on_traced_local_handoff(
                            target_code,
                            predicate,
                            assign_then_cross_boundary,
                            primary,
                        )
                        owner = observed["owner"]
                        self.assertIsNone(owner._retain_if_registered)
                        if boundary == "before-store":
                            self.assertIs(
                                getattr(instance, owner_attribute), previous_owner
                            )
                            self.assertEqual(instance.source_fd, -1)
                            self.assertTrue(owner.closed)
                            self.assertEqual(observed["close"]["active_closes"], 1)
                            with self.assertRaises(OSError) as closed:
                                os.fstat(raw_fd)
                            self.assertEqual(closed.exception.errno, errno.EBADF)
                            owner.close(primary_error=primary)
                            self.assertEqual(observed["close"]["active_closes"], 1)
                        else:
                            self.assertTrue(observed["registration"]())
                            self.assertIs(getattr(instance, owner_attribute), owner)
                            self.assertEqual(instance.source_fd, raw_fd)
                            self.assertFalse(owner.closed)
                            self.assertTrue(owner._retained_on_exception)
                            self.assertEqual(observed["close"]["active_closes"], 0)
                            if slot_kind == "helper":
                                instance._close_all(primary_error=primary)
                                instance._close_all(primary_error=primary)
                            else:
                                instance.close(primary_error=primary)
                                instance.close(primary_error=primary)
                            self.assertTrue(owner.closed)
                            self.assertEqual(observed["close"]["active_closes"], 1)
                    finally:
                        if slot_kind == "helper":
                            instance._close_all(primary_error=primary)
                        else:
                            instance.close(primary_error=primary)
                        try:
                            os.close(raw_fd)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise
        os.close(parent_fd)

    def test_trace_interruptions_after_acl_handoffs_free_once(self) -> None:
        backend = self.backend_module.DarwinBackend()

        class ACLHandoffBomb(BaseException):
            pass

        target_code = self.backend_module._OwnedACL.__enter__.__code__
        acquired_line = self.source_line_number(target_code, "if self.closed:")
        operations = (
            ("get", lambda: backend._get_acl(99)),
            ("snapshot", lambda: backend._snapshot_acl(99)),
        )
        for index, (label, operation) in enumerate(operations, start=1):
            with self.subTest(operation=label):
                pointer_value = 700 + index
                primary = ACLHandoffBomb(f"{label} ACL handoff interrupt")
                observed: Dict[str, Any] = {}
                free_calls = []

                def after_acl_get(frame: Any) -> bool:
                    target = frame.f_locals.get("self")
                    pointer = (
                        None if target is None or target.closed else target.pointer()
                    )
                    if (
                        target is None
                        or target.closed
                        or pointer is None
                        or pointer.value != pointer_value
                        or frame.f_lineno != acquired_line
                    ):
                        return False
                    observed["owner"] = target
                    observed["close"] = self.observe_acl_owner_close(target)
                    return True

                def record_free(pointer: ctypes.c_void_p) -> int:
                    free_calls.append(pointer.value)
                    return 0

                with mock.patch.object(
                    backend, "_acl_get_fd_np", return_value=pointer_value
                ):
                    with mock.patch.object(
                        backend, "_acl_free", side_effect=record_free
                    ):
                        self.interrupt_on_traced_local_handoff(
                            target_code,
                            after_acl_get,
                            operation,
                            primary,
                        )

                owner = observed["owner"]
                close_observation = observed["close"]
                self.assertTrue(owner.closed)
                self.assertEqual(free_calls, [pointer_value])
                self.assertEqual(close_observation["active_closes"], 1)
                owner.close()
                owner.close()
                self.assertEqual(close_observation["active_closes"], 1)

        snapshot_pointer = 750
        snapshot_primary = ACLHandoffBomb("snapshot ACL local interrupt")
        snapshot_observed: Dict[str, Any] = {}
        snapshot_free_calls = []
        snapshot_acl_line = self.source_line_number(
            self.backend_module.DarwinBackend._acl_bytes_once.__code__,
            "if acl is None:",
        )

        def after_snapshot_acl_store(frame: Any) -> bool:
            owner = frame.f_locals.get("owner")
            acl = frame.f_locals.get("acl")
            if (
                owner is None
                or owner.closed
                or frame.f_lineno != snapshot_acl_line
                or not isinstance(acl, ctypes.c_void_p)
                or acl.value != snapshot_pointer
            ):
                return False
            snapshot_observed["owner"] = owner
            snapshot_observed["close"] = self.observe_acl_owner_close(owner)
            return True

        def record_snapshot_free(pointer: ctypes.c_void_p) -> int:
            snapshot_free_calls.append(pointer.value)
            return 0

        with mock.patch.object(
            backend, "_acl_get_fd_np", return_value=snapshot_pointer
        ):
            with mock.patch.object(
                backend, "_acl_free", side_effect=record_snapshot_free
            ):
                self.interrupt_on_traced_local_handoff(
                    self.backend_module.DarwinBackend._acl_bytes_once.__code__,
                    after_snapshot_acl_store,
                    lambda: backend._snapshot_acl(99),
                    snapshot_primary,
                )
        snapshot_owner = snapshot_observed["owner"]
        self.assertTrue(snapshot_owner.closed)
        self.assertEqual(snapshot_free_calls, [snapshot_pointer])
        self.assertEqual(snapshot_observed["close"]["active_closes"], 1)
        snapshot_owner.close()
        self.assertEqual(snapshot_observed["close"]["active_closes"], 1)

        normal_free_calls = []

        def record_normal_free(pointer: ctypes.c_void_p) -> int:
            normal_free_calls.append(pointer.value)
            return 0

        with mock.patch.object(backend, "_acl_get_fd_np", return_value=799):
            with mock.patch.object(
                backend, "_acl_free", side_effect=record_normal_free
            ):
                raw_pointer = backend._get_acl(99)
                self.assertEqual(raw_pointer.value, 799)
                self.assertEqual(normal_free_calls, [])
                backend._free_acl(raw_pointer)
        self.assertEqual(normal_free_calls, [799])

    def test_trace_interruption_during_partial_candidate_owner_install_closes_once(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"partial-owner-source\n")
        transaction = self.helper.MirrorSync(backend)
        transaction.source_path = str(self.source.absolute())
        transaction.destination_path = str(self.destination.absolute())
        transaction._validate_paths()
        transaction._bind_source()
        transaction._bind_destination()
        transaction._create_stage()
        candidate_path = (
            pathlib.Path(transaction.stage_path) / transaction.candidate_name
        )
        candidate_path.write_bytes(b"partial-candidate\n")
        os.chmod(candidate_path, 0o600)

        class CandidateInstallBomb(BaseException):
            pass

        primary = CandidateInstallBomb("candidate owner install interrupt")
        observed: Dict[str, Any] = {}
        candidate_identity_line = self.source_line_number(
            self.helper.MirrorSync._bind_partial_candidate_if_present.__code__,
            "self.candidate_identity = self.backend.identity(self._candidate_fd())",
            occurrence=2,
        )

        def after_owner_install(frame: Any) -> bool:
            owner = frame.f_locals.get("owner")
            target = frame.f_locals.get("self")
            if (
                owner is None
                or owner.closed
                or frame.f_lineno != candidate_identity_line
                or target is not transaction
                or frame.f_locals.get("entered") is not True
                or target._candidate_owner is not owner
                or owner._retain_if_registered is None
                or target.candidate_identity is not None
            ):
                return False
            observed["owner"] = owner
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                self.helper.MirrorSync._bind_partial_candidate_if_present.__code__,
                after_owner_install,
                transaction._bind_partial_candidate_if_present,
                primary,
            )
            owner = observed["owner"]
            open_after_interrupt = not owner.closed
            registered_after_interrupt = transaction._candidate_owner is owner
            close_count_after_interrupt = observed["close"]["active_closes"]
            cleanup_error = transaction._cleanup_stage(primary_error=primary)
            transaction._close_all(primary_error=primary)

            self.assertTrue(open_after_interrupt)
            self.assertTrue(registered_after_interrupt)
            self.assertIsNone(owner._retain_if_registered)
            self.assertEqual(close_count_after_interrupt, 0)
            self.assertEqual(observed["close"]["active_closes"], 1)
            self.assertEqual(transaction.candidate_fd, -1)
            self.assertIsNone(cleanup_error)
            self.assertTrue(transaction.stage_removed)
            self.assertFalse(pathlib.Path(transaction.stage_path).exists())
            self.assertFalse(self.destination.exists())
        finally:
            if not transaction.stage_removed:
                transaction._cleanup_stage(primary_error=primary)
            transaction._close_all(primary_error=primary)

        normal_parent_fd, normal_name = backend.open_absolute_parent(str(self.source))
        try:
            normal_fd = backend.open_leaf(normal_parent_fd, normal_name)
        finally:
            os.close(normal_parent_fd)
        normal_owner = backend._adopt_fd(normal_fd, "normal transaction handoff")
        normal_transaction = self.helper.MirrorSync(backend)
        with normal_owner:
            normal_transaction._install_fd_owner("_candidate_owner", normal_owner)
            normal_observation = self.observe_fd_owner_close(normal_owner)
        self.assertFalse(normal_owner.closed)
        self.assertIsNone(normal_owner._retain_if_registered)
        self.assertEqual(normal_transaction.candidate_fd, normal_fd)
        normal_transaction._close_all(primary_error=None)
        normal_transaction._close_all(primary_error=None)
        self.assertTrue(normal_owner.closed)
        self.assertEqual(normal_observation["active_closes"], 1)

    def test_stage_owner_slot_interruption_retains_identity_bound_cleanup(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"stage-owner-slot\n")
        transaction = self.helper.MirrorSync(backend)

        class StageOwnerSlotBomb(BaseException):
            pass

        primary = StageOwnerSlotBomb("stage owner slot interrupt")
        observed: Dict[str, Any] = {}
        stage_identity_line = self.source_line_number(
            self.helper.MirrorSync._create_stage.__code__,
            "self.stage_identity = stage_owner.identity()",
        )

        def after_stage_slot_store(frame: Any) -> bool:
            owner = frame.f_locals.get("stage_owner")
            target = frame.f_locals.get("self")
            if (
                owner is None
                or owner.closed
                or frame.f_lineno != stage_identity_line
                or target is not transaction
                or target._stage_owner is not owner
                or owner._retain_if_registered is None
                or target.stage_identity is not None
            ):
                return False
            observed["owner"] = owner
            observed["identity"] = owner.identity()
            observed["close"] = self.observe_fd_owner_close(owner)
            return True

        self.interrupt_on_traced_local_handoff(
            self.helper.MirrorSync._create_stage.__code__,
            after_stage_slot_store,
            lambda: transaction.sync_one(
                str(self.source.absolute()), str(self.destination.absolute())
            ),
            primary,
        )

        owner = observed["owner"]
        self.assertTrue(owner.closed)
        self.assertIsNone(owner._retain_if_registered)
        self.assertEqual(observed["close"]["active_closes"], 1)
        self.assertTrue(transaction.stage_removed)
        self.assertFalse(pathlib.Path(transaction.stage_path).exists())
        self.assertEqual(self.stage_paths(), [])
        self.assertFalse(self.destination.exists())
        transaction._close_all(primary_error=primary)
        self.assertEqual(observed["close"]["active_closes"], 1)

    def test_registered_owner_predicate_failure_is_bounded_and_unregistered_finalizes(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"owner-predicate\n")

        class OwnerBodyBomb(BaseException):
            pass

        class OwnerPredicateBomb(BaseException):
            pass

        parent_fd, name = backend.open_absolute_parent(str(self.source))
        try:
            transaction = self.helper.MirrorSync(backend)
            registered_owner = backend._open_leaf_owned(parent_fd, name)
            registered_primary = OwnerBodyBomb("registered owner primary")
            registered_secondary = OwnerPredicateBomb("registered predicate secondary")
            registered_close = None
            escaped = None
            try:
                with registered_owner:
                    registered_close = self.observe_fd_owner_close(registered_owner)
                    transaction._candidate_owner = registered_owner

                    def registered_predicate() -> bool:
                        raise registered_secondary

                    registered_owner.retain_if_registered(registered_predicate)
                    raise registered_primary
            except BaseException as exc:
                escaped = exc

            self.assertIs(escaped, registered_primary)
            self.assertFalse(registered_owner.closed)
            self.assertIsNone(registered_owner._retain_if_registered)
            self.assertEqual(registered_close["active_closes"], 0)
            self.assertIn(
                type(registered_secondary).__name__,
                getattr(registered_primary, "cleanup_diagnostic", ""),
            )
            transaction._close_all(primary_error=registered_primary)
            self.assertTrue(registered_owner.closed)
            self.assertEqual(registered_close["active_closes"], 1)

            close_calls = []
            real_close = os.close

            def record_close(fd: int) -> None:
                close_calls.append(fd)
                real_close(fd)

            unregistered_primary = OwnerBodyBomb("unregistered owner primary")
            unregistered_secondary = OwnerPredicateBomb(
                "unregistered predicate secondary"
            )
            unregistered_observed: Dict[str, Any] = {}

            def run_unregistered_owner() -> None:
                owner = backend._open_leaf_owned(parent_fd, name)
                with owner:
                    unregistered_observed["reference"] = weakref.ref(owner)
                    unregistered_observed["fd"] = owner.fileno()

                    def unregistered_predicate() -> bool:
                        raise unregistered_secondary

                    owner.retain_if_registered(unregistered_predicate)
                    raise unregistered_primary

            unregistered_reference = None
            unregistered_fd = -1
            with mock.patch.object(
                self.backend_module.os, "close", side_effect=record_close
            ):
                try:
                    run_unregistered_owner()
                except BaseException as exc:
                    self.assertIs(exc, unregistered_primary)
                    unregistered_reference = unregistered_observed["reference"]
                    unregistered_fd = unregistered_observed["fd"]
                    self.assertIsNone(unregistered_reference()._retain_if_registered)
                    self.assertIn(
                        type(unregistered_secondary).__name__,
                        getattr(unregistered_primary, "cleanup_diagnostic", ""),
                    )
                    for error in (unregistered_primary, unregistered_secondary):
                        error.__traceback__ = None
                        error.__context__ = None
                        error.__cause__ = None
                gc.collect()

            self.assertIsNone(unregistered_reference())
            self.assertEqual(close_calls, [unregistered_fd])
            with self.assertRaises(OSError) as closed:
                os.fstat(unregistered_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            os.close(parent_fd)

    def test_close_all_retries_trace_interruptions_and_drains_each_fd_once(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"close-all-trace\n")

        class CloseDrainBomb(BaseException):
            pass

        caller_guard_line = self.source_line_number(
            self.helper.MirrorSync._close_all_pass.__code__,
            "_owner_close_attempt = 1",
        )

        state_close_line = self.source_line_number(
            self.backend_module._FDState.close.__code__,
            "self.fd = -1; os.close(fd)  # noqa: E702",
        )

        for boundary in ("caller-guard", "state-operation"):
            with self.subTest(boundary=boundary):
                transaction = self.helper.MirrorSync(backend)
                raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(3)]
                owners = [
                    backend._adopt_fd(fd, f"{boundary} close owner {index}")
                    for index, fd in enumerate(raw_fds)
                ]
                for attribute, owner in zip(
                    ("_candidate_owner", "_stage_owner", "_source_owner"),
                    owners,
                ):
                    transaction._install_fd_owner(attribute, owner)

                primary = RuntimeError(f"{boundary} operation primary")
                secondary = CloseDrainBomb(f"{boundary} close trace interrupt")
                if boundary == "caller-guard":
                    target_code = self.helper.MirrorSync._close_all_pass.__code__
                    expected_target = transaction
                else:
                    target_code = self.backend_module._FDState.close.__code__
                    expected_target = owners[0]._state
                previous_trace = sys.gettrace()
                injected = False
                close_calls = []
                real_close = os.close

                def trace(frame: Any, event: str, _arg: Any) -> Any:
                    nonlocal injected
                    at_boundary = frame.f_locals.get("self") is expected_target
                    if boundary == "caller-guard":
                        at_boundary = (
                            at_boundary
                            and frame.f_lineno == caller_guard_line
                            and frame.f_locals.get("owner") is owners[0]
                            and frame.f_locals.get("retry") is False
                        )
                    else:
                        at_boundary = (
                            at_boundary
                            and frame.f_lineno == state_close_line
                            and frame.f_locals.get("fd") == raw_fds[0]
                        )
                    if (
                        event == "line"
                        and frame.f_code is target_code
                        and not injected
                        and at_boundary
                    ):
                        injected = True
                        sys.settrace(None)
                        raise secondary
                    return trace

                def record_close(fd: int) -> None:
                    close_calls.append(fd)
                    real_close(fd)

                sys.settrace(trace)
                try:
                    with mock.patch.object(
                        self.backend_module.os, "close", side_effect=record_close
                    ):
                        detail = transaction._close_all(primary_error=primary)
                finally:
                    sys.settrace(previous_trace)

                self.assertTrue(injected)
                self.assertIsInstance(detail, str)
                self.assertIn(type(secondary).__name__, detail)
                self.assertIn(
                    type(secondary).__name__,
                    getattr(primary, "cleanup_diagnostic", ""),
                )
                self.assertCountEqual(close_calls, raw_fds)
                self.assertEqual(len(close_calls), len(raw_fds))
                for fd in raw_fds:
                    with self.assertRaises(OSError) as closed:
                        os.fstat(fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                self.assertTrue(all(owner.closed for owner in owners))
                self.assertEqual(transaction.candidate_fd, -1)
                self.assertEqual(transaction.stage_fd, -1)
                self.assertEqual(transaction.source_fd, -1)
                self.assertIsNone(transaction._close_all(primary_error=primary))

    def test_owned_transaction_handoff_closes_once_and_normal_owner_survives(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        parent_fd, _name = backend.open_absolute_parent(str(self.source))
        source_parent_identity = backend.identity(parent_fd)

        class TransactionHandoffBomb(BaseException):
            pass

        def fake_transaction() -> tuple[Any, list[Optional[BaseException]]]:
            transaction = object.__new__(self.backend_module.BoundTransaction)
            transaction._closed = False
            close_calls: list[Optional[BaseException]] = []

            def close(*, primary_error: Optional[BaseException] = None) -> None:
                close_calls.append(primary_error)
                transaction._closed = True

            transaction.close = close
            return transaction, close_calls

        interrupted_transaction, interrupted_close_calls = fake_transaction()
        primary = TransactionHandoffBomb("transaction handoff interrupt")
        interrupted_owner = None
        acquired_line = self.source_line_number(
            self.backend_module._OwnedTransaction.__enter__.__code__,
            "if self.closed:",
        )
        try:
            with mock.patch.object(
                backend, "bind_transaction", return_value=interrupted_transaction
            ):
                interrupted_owner = backend.bind_transaction_owned(
                    str(self.source),
                    str(self.destination),
                    str(self.destination_parent / "temporary"),
                    source_parent_expected=source_parent_identity,
                )

                def after_transaction_return(frame: Any) -> bool:
                    return (
                        frame.f_locals.get("self") is interrupted_owner
                        and frame.f_lineno == acquired_line
                        and not interrupted_owner.closed
                        and interrupted_owner.transaction() is interrupted_transaction
                    )

                self.interrupt_on_traced_local_handoff(
                    self.backend_module._OwnedTransaction.__enter__.__code__,
                    after_transaction_return,
                    interrupted_owner.__enter__,
                    primary,
                )
            self.assertTrue(interrupted_owner.closed)
            self.assertEqual(interrupted_close_calls, [primary])
            interrupted_owner.close(primary_error=primary)
            self.assertEqual(interrupted_close_calls, [primary])

            normal_transaction, normal_close_calls = fake_transaction()
            with mock.patch.object(
                backend, "bind_transaction", return_value=normal_transaction
            ):
                normal_owner = backend.bind_transaction_owned(
                    str(self.source),
                    str(self.destination),
                    str(self.destination_parent / "temporary-normal"),
                    source_parent_expected=source_parent_identity,
                )
                with normal_owner:
                    self.assertIs(normal_owner.transaction(), normal_transaction)
            self.assertFalse(normal_owner.closed)
            self.assertIs(normal_owner.transaction(), normal_transaction)
            normal_owner.close()
            normal_owner.close()
            self.assertTrue(normal_owner.closed)
            self.assertEqual(normal_close_calls, [None])
        finally:
            if interrupted_owner is not None:
                interrupted_owner.close(primary_error=primary)
            os.close(parent_fd)

    def test_bound_transaction_owner_slot_trace_retains_until_close(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"bound-owner-slot\n")
        parent_fd, name = backend.open_absolute_parent(str(self.source))
        transaction = object.__new__(self.backend_module.BoundTransaction)
        transaction._initialize(
            backend,
            None,
            str(self.destination.absolute()),
            str(self.destination_parent / "bound-owner-temporary"),
        )
        owner = backend._open_leaf_owned(parent_fd, name)

        class BoundOwnerSlotBomb(BaseException):
            pass

        primary = BoundOwnerSlotBomb("bound transaction owner slot interrupt")
        observed: Dict[str, Any] = {}

        def install_owner() -> None:
            with owner:
                transaction._install_fd_owner("_source_owner", owner)
                installed = True
                self.assertTrue(installed)

        installed_line = self.source_line_number(
            install_owner.__code__, "installed = True"
        )

        def after_bound_slot_store(frame: Any) -> bool:
            if (
                frame.f_locals.get("installed") is not None
                or frame.f_lineno != installed_line
                or transaction._source_owner is not owner
                or owner.closed
                or owner._retain_if_registered is None
            ):
                return False
            observed["close"] = self.observe_fd_owner_close(owner)
            observed["fd"] = owner.fileno()
            return True

        try:
            self.interrupt_on_traced_local_handoff(
                install_owner.__code__, after_bound_slot_store, install_owner, primary
            )
            self.assertFalse(owner.closed)
            self.assertIs(transaction._source_owner, owner)
            self.assertIsNone(owner._retain_if_registered)
            self.assertEqual(observed["close"]["active_closes"], 0)
            transaction.close(primary_error=primary)
            transaction.close(primary_error=primary)
            self.assertTrue(owner.closed)
            self.assertTrue(transaction._closed)
            self.assertEqual(observed["close"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(observed["fd"])
            self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            transaction.close(primary_error=primary)
            os.close(parent_fd)

    def test_bound_transaction_whole_pass_interruptions_preserve_primary_and_drain(
        self,
    ) -> None:
        backend_module = self.backend_module

        class BoundCloseBodyPrimary(BaseException):
            pass

        class BoundClosePassBomb(BaseException):
            pass

        next_owner_line = self.source_line_number(
            backend_module.BoundTransaction._close_owner_pass.__code__,
            "owner = getattr(self, owner_attribute)",
        )
        finalization_line = self.source_line_number(
            backend_module.BoundTransaction.close.__code__,
            "_cleanup_dispatch = 4",
        )

        for boundary in ("next-owner", "finalization"):
            with self.subTest(boundary=boundary):
                backend = backend_module.DarwinBackend()
                transaction = object.__new__(backend_module.BoundTransaction)
                transaction._initialize(
                    backend,
                    None,
                    "/synthetic/destination",
                    "/synthetic/stage/temporary",
                )
                raw_fds = [os.open(os.devnull, os.O_RDONLY) for _index in range(3)]
                owners = [
                    backend._adopt_fd(fd, f"{boundary} bound owner {index}")
                    for index, fd in enumerate(raw_fds)
                ]
                observations = [self.observe_fd_owner_close(owner) for owner in owners]
                for attribute, owner in zip(
                    ("_clone_owner", "_original_owner", "_source_owner"),
                    owners,
                ):
                    transaction._install_fd_owner(attribute, owner)

                primary = BoundCloseBodyPrimary(f"{boundary} body primary")
                secondary = BoundClosePassBomb(f"{boundary} whole-pass interruption")

                def at_bound_close_boundary(frame: Any) -> bool:
                    if frame.f_locals.get("self") is not transaction:
                        return False
                    if boundary == "finalization":
                        return (
                            frame.f_lineno == finalization_line
                            and frame.f_locals.get("primary_error") is primary
                            and all(owner.closed for owner in owners)
                        )
                    return (
                        frame.f_lineno == next_owner_line
                        and frame.f_locals.get("primary_error") is primary
                        and frame.f_locals.get("owner_attribute") == "_original_owner"
                        and owners[0].closed
                        and not owners[1].closed
                        and not owners[2].closed
                    )

                def fail_with_cleanup() -> None:
                    try:
                        raise primary
                    finally:
                        transaction.close(primary_error=primary)

                try:
                    target_code = (
                        backend_module.BoundTransaction.close.__code__
                        if boundary == "finalization"
                        else backend_module.BoundTransaction._close_owner_pass.__code__
                    )
                    self.interrupt_handler_preserving_primary(
                        target_code,
                        at_bound_close_boundary,
                        fail_with_cleanup,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(transaction._closed)
                    self.assertTrue(all(owner.closed for owner in owners))
                    self.assertEqual(
                        [observation["active_closes"] for observation in observations],
                        [1, 1, 1],
                    )
                    for fd in raw_fds:
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    transaction.close(primary_error=primary)
                    for fd in raw_fds:
                        try:
                            os.close(fd)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise

    def test_cli_hook_noise_cannot_corrupt_single_json_output(self) -> None:
        self.write_source(b"cli-hook\n")
        hook = self.root / "noisy-hook.sh"
        hook.write_text(
            "#!/bin/sh\n"
            "printf 'hook stdout noise\\n'\n"
            "printf 'hook stderr noise\\n' >&2\n",
            encoding="utf-8",
        )
        os.chmod(hook, 0o700)
        environment = os.environ.copy()
        environment["CODEX_ROLLOUT_MIRROR_TEST_HOOK"] = str(hook)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "sync-one",
                "--source",
                str(self.source),
                "--destination",
                str(self.destination),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1, completed.stdout)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt["outcome"], "updated")

    def test_close_all_whole_pass_interruptions_preserve_primary_and_drain(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"close-all-whole-pass\n")

        class CloseAllBodyPrimary(BaseException):
            pass

        class CloseAllPassBomb(BaseException):
            pass

        close_loop_line = self.source_line_number(
            self.helper.MirrorSync._close_all_pass.__code__,
            "for _attribute, label, owner in entries:",
        )
        postprocess_line = self.source_line_number(
            self.helper.MirrorSync._close_all_pass.__code__,
            "for attribute, label, owner in entries:",
        )
        finalization_line = self.source_line_number(
            self.helper.MirrorSync._close_all.__code__,
            "_cleanup_dispatch = 3",
        )

        for boundary in ("next-owner", "postprocess", "finalization"):
            with self.subTest(boundary=boundary):
                transaction = self.helper.MirrorSync(backend)
                raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(3)]
                owners = [
                    backend._adopt_fd(fd, f"{boundary} owner {index}")
                    for index, fd in enumerate(raw_fds)
                ]
                observations = [self.observe_fd_owner_close(owner) for owner in owners]
                for attribute, owner in zip(
                    ("_candidate_owner", "_stage_owner", "_source_owner"),
                    owners,
                ):
                    transaction._install_fd_owner(attribute, owner)

                primary = CloseAllBodyPrimary(f"{boundary} body primary")
                secondary = CloseAllPassBomb(
                    f"{boundary} whole-pass cleanup interruption"
                )

                def at_whole_pass_boundary(frame: Any) -> bool:
                    if frame.f_locals.get("self") is not transaction:
                        return False
                    if boundary == "finalization":
                        return (
                            frame.f_lineno == finalization_line
                            and frame.f_locals.get("primary_error") is primary
                            and all(owner.closed for owner in owners)
                            and transaction._candidate_owner is None
                            and transaction._stage_owner is None
                            and transaction._source_owner is None
                        )
                    if frame.f_locals.get("retry") is not False:
                        return False
                    if boundary == "next-owner":
                        return (
                            frame.f_lineno == close_loop_line
                            and owners[0].closed
                            and not owners[1].closed
                            and not owners[2].closed
                        )
                    return (
                        frame.f_lineno == postprocess_line
                        and all(owner.closed for owner in owners)
                        and transaction._candidate_owner is owners[0]
                    )

                def fail_with_cleanup() -> None:
                    try:
                        raise primary
                    finally:
                        transaction._close_all(primary_error=primary)

                try:
                    target_code = (
                        self.helper.MirrorSync._close_all.__code__
                        if boundary == "finalization"
                        else self.helper.MirrorSync._close_all_pass.__code__
                    )
                    self.interrupt_handler_preserving_primary(
                        target_code,
                        at_whole_pass_boundary,
                        fail_with_cleanup,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(all(owner.closed for owner in owners))
                    self.assertEqual(
                        [observation["active_closes"] for observation in observations],
                        [1, 1, 1],
                    )
                    for fd in raw_fds:
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertEqual(transaction.candidate_fd, -1)
                    self.assertEqual(transaction.stage_fd, -1)
                    self.assertEqual(transaction.source_fd, -1)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    transaction._close_all(primary_error=primary)
                    for fd in raw_fds:
                        try:
                            os.close(fd)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise

    def test_direct_stage_cleanup_fd_owner_loop_interruptions_drain_all(
        self,
    ) -> None:
        backend_module = self.backend_module

        class StageCleanupBodyPrimary(BaseException):
            pass

        class StageCleanupLoopBomb(BaseException):
            pass

        owner_guard_line = self.source_line_number(
            backend_module.DarwinBackend._close_fd_owner_pass.__code__,
            "if owner is None:",
        )

        def snapshot_expectation(backend: Any, path: pathlib.Path) -> Any:
            parent_fd, name = backend.open_absolute_parent(str(path))
            try:
                file_fd = backend.open_leaf(parent_fd, name)
                try:
                    return backend.snapshot_expectation(backend.snapshot_file(file_fd))
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent_fd)

        for caller in ("remove", "intent"):
            with self.subTest(caller=caller):
                backend = backend_module.DarwinBackend()
                primary = StageCleanupBodyPrimary(f"{caller} body primary")
                secondary = StageCleanupLoopBomb(
                    f"{caller} next-owner cleanup interruption"
                )
                actions: list[str] = []
                stage = self.destination_parent / (
                    "recorded-loop-stage"
                    if caller == "remove"
                    else f".codex-reflink-repair-{17:032x}"
                )
                stage.mkdir(mode=0o700)
                final_path = self.destination_parent / "intent-loop-original.jsonl"
                final_path.write_bytes(b"intent loop original\n")
                final_path.chmod(0o600)

                parent_fd, _name = backend.open_absolute_parent(str(stage))
                stage_fd = backend.open_absolute_dir(str(stage))
                try:
                    expected_stage = backend.validate_private_stage_parent(stage_fd)
                    expected_container = backend.validate_stage_container(parent_fd)
                finally:
                    os.close(stage_fd)
                    os.close(parent_fd)
                expected_original = snapshot_expectation(backend, final_path)
                before_stage = stage.stat()
                baseline_fds = self.open_fd_set()
                live_owners: list[Any] = []
                observations: list[Dict[str, Any]] = []
                cleanup_dispatches = 0
                real_close_owners = backend_module.DarwinBackend._close_fd_owners
                real_close_pass = backend_module.DarwinBackend._close_fd_owner_pass

                def record_owner_pass(
                    descriptors: Any,
                    **kwargs: Any,
                ) -> Any:
                    nonlocal cleanup_dispatches, live_owners, observations
                    cleanup_dispatches += 1
                    current_live = [
                        owner for _subject, owner in descriptors if owner is not None
                    ]
                    if not live_owners:
                        live_owners = current_live
                        observations = [
                            self.observe_fd_owner_close(owner) for owner in live_owners
                        ]
                    return real_close_pass(descriptors, **kwargs)

                def authorize(action: str) -> None:
                    actions.append(action)
                    expected_action = (
                        "remove_stage" if caller == "remove" else "intent_remove_stage"
                    )
                    self.assertEqual(action, expected_action)
                    raise primary

                def operation() -> None:
                    if caller == "remove":
                        backend.remove_empty_private_stage(
                            str(stage),
                            expected_stage,
                            expected_container=expected_container,
                            authorize_state=authorize,
                        )
                        return
                    backend.cleanup_intent_stage(
                        str(stage),
                        final_path=str(final_path),
                        expected_container=expected_container,
                        expected_original=expected_original,
                        expected_stage=None,
                        allow_clone=False,
                        expected_clone=None,
                        expected_snapshot=None,
                        expected_size=None,
                        expected_sha256=None,
                        authorize_state=authorize,
                    )

                def after_first_live_owner(frame: Any) -> bool:
                    descriptors = frame.f_locals.get("descriptors", ())
                    current_live = [
                        owner for _subject, owner in descriptors if owner is not None
                    ]
                    return (
                        frame.f_lineno == owner_guard_line
                        and frame.f_locals.get("primary_error") is primary
                        and len(current_live) >= 2
                        and frame.f_locals.get("owner") is current_live[1]
                        and current_live[0].closed
                        and not current_live[1].closed
                    )

                try:
                    with mock.patch.object(
                        backend_module.DarwinBackend,
                        "_close_fd_owner_pass",
                        side_effect=record_owner_pass,
                    ):
                        self.interrupt_handler_preserving_primary(
                            real_close_pass.__code__,
                            after_first_live_owner,
                            operation,
                            secondary,
                            expected_primary=primary,
                        )
                    self.assertGreaterEqual(cleanup_dispatches, 2)
                    self.assertGreaterEqual(len(live_owners), 2)
                    self.assertTrue(all(owner.closed for owner in live_owners))
                    self.assertEqual(
                        [observation["active_closes"] for observation in observations],
                        [1] * len(observations),
                    )
                    self.assertEqual(self.open_fd_set(), baseline_fds)
                    self.assertEqual(stage.stat().st_ino, before_stage.st_ino)
                    self.assertEqual(list(stage.iterdir()), [])
                    self.assertEqual(
                        actions,
                        [
                            "remove_stage"
                            if caller == "remove"
                            else "intent_remove_stage"
                        ],
                    )
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    if live_owners:
                        real_close_owners(
                            tuple(
                                (f"test cleanup {index}", owner)
                                for index, owner in enumerate(live_owners)
                            ),
                            primary_error=primary,
                        )
                    if stage.exists():
                        stage.rmdir()
                    final_path.unlink(missing_ok=True)

    def test_sync_one_finally_dispatch_interruption_retries_and_rebuilds_receipt(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        self.write_source(b"sync finally dispatch\n")
        self.write_destination(b"existing destination\n")

        class FinallyDispatchBomb(BaseException):
            pass

        primary = backend_module.BackendError(
            "finally_body_failed",
            "sync body failed before final FD cleanup",
            errno.EIO,
        )
        secondary = FinallyDispatchBomb("sync finally first dispatch interruption")

        class FinallyTransaction(self.helper.MirrorSync):
            def __init__(inner_self) -> None:
                super().__init__(backend)
                inner_self.close_calls: list[Optional[BaseException]] = []

            def _execute(inner_self) -> Any:
                raise primary

            def _close_all(
                inner_self,
                *,
                primary_error: Optional[BaseException],
            ) -> Optional[str]:
                inner_self.close_calls.append(primary_error)
                return super()._close_all(primary_error=primary_error)

        transaction = FinallyTransaction()
        raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(3)]
        owners = [
            backend._adopt_fd(fd, f"sync finally owner {index}")
            for index, fd in enumerate(raw_fds)
        ]
        observations = [self.observe_fd_owner_close(owner) for owner in owners]
        for attribute, owner in zip(
            ("_candidate_owner", "_destination_owner", "_source_owner"),
            owners,
        ):
            transaction._install_fd_owner(attribute, owner)

        dispatch_line = self.source_line_number(
            self.helper.MirrorSync.sync_one.__code__,
            "_cleanup_dispatch = 1",
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code is self.helper.MirrorSync.sync_one.__code__
                and frame.f_lineno == dispatch_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is primary
                and not captured
            ):
                captured["traceback"] = primary.__traceback__
                captured["result"] = frame.f_locals.get("result")
                sys.settrace(None)
                raise secondary
            return trace

        receipt = None
        sys.settrace(trace)
        try:
            receipt = transaction.sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            )
        finally:
            sys.settrace(previous_trace)

        try:
            self.assertTrue(captured)
            self.assertIsNotNone(captured["traceback"])
            self.assertEqual(transaction.close_calls, [primary])
            self.assertTrue(all(owner.closed for owner in owners))
            self.assertEqual(
                [observation["active_closes"] for observation in observations],
                [1, 1, 1],
            )
            for fd in raw_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            cursor = primary.__traceback__
            while cursor is not None and cursor is not captured["traceback"]:
                cursor = cursor.tb_next
            self.assertIs(cursor, captured["traceback"])
            self.assertIn(
                type(secondary).__name__,
                getattr(primary, "cleanup_diagnostic", ""),
            )
            payload = receipt.to_dict()
            self.assertEqual(payload["outcome"], "fatal")
            self.assertEqual(payload["reason"], primary.reason)
            self.assertIn(type(secondary).__name__, payload["detail"])
            raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(payload, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            transaction._close_all(primary_error=primary)
            for fd in raw_fds:
                try:
                    os.close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_stage_cleanup_caller_dispatch_interruptions_preserve_primary(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CallerBodyPrimary(BaseException):
            pass

        class CallerDispatchBomb(BaseException):
            pass

        def snapshot_expectation(backend: Any, path: pathlib.Path) -> Any:
            parent_fd, name = backend.open_absolute_parent(str(path))
            try:
                file_fd = backend.open_leaf(parent_fd, name)
                try:
                    return backend.snapshot_expectation(backend.snapshot_file(file_fd))
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent_fd)

        for caller in ("remove", "intent"):
            with self.subTest(caller=caller):
                backend = backend_module.DarwinBackend()
                primary = CallerBodyPrimary(f"{caller} caller body primary")
                secondary = CallerDispatchBomb(
                    f"{caller} caller cleanup dispatch interruption"
                )
                actions: list[str] = []
                stage = self.destination_parent / (
                    "recorded-caller-dispatch-stage"
                    if caller == "remove"
                    else f".codex-reflink-repair-{23:032x}"
                )
                stage.mkdir(mode=0o700)
                final_path = (
                    self.destination_parent / "intent-caller-dispatch-original.jsonl"
                )
                final_path.write_bytes(b"intent caller dispatch original\n")
                final_path.chmod(0o600)

                parent_fd, _name = backend.open_absolute_parent(str(stage))
                stage_fd = backend.open_absolute_dir(str(stage))
                try:
                    expected_stage = backend.validate_private_stage_parent(stage_fd)
                    expected_container = backend.validate_stage_container(parent_fd)
                finally:
                    os.close(stage_fd)
                    os.close(parent_fd)
                expected_original = snapshot_expectation(backend, final_path)
                before_stage = stage.stat()
                baseline_fds = self.open_fd_set()

                def authorize(action: str) -> None:
                    actions.append(action)
                    expected_action = (
                        "remove_stage" if caller == "remove" else "intent_remove_stage"
                    )
                    self.assertEqual(action, expected_action)
                    raise primary

                if caller == "remove":
                    caller_code = (
                        backend_module.DarwinBackend.remove_empty_private_stage.__code__
                    )

                    def operation() -> None:
                        backend.remove_empty_private_stage(
                            str(stage),
                            expected_stage,
                            expected_container=expected_container,
                            authorize_state=authorize,
                        )

                else:
                    caller_code = (
                        backend_module.DarwinBackend.cleanup_intent_stage.__code__
                    )

                    def operation() -> None:
                        backend.cleanup_intent_stage(
                            str(stage),
                            final_path=str(final_path),
                            expected_container=expected_container,
                            expected_original=expected_original,
                            expected_stage=None,
                            allow_clone=False,
                            expected_clone=None,
                            expected_snapshot=None,
                            expected_size=None,
                            expected_sha256=None,
                            authorize_state=authorize,
                        )

                dispatch_line = self.source_line_number(
                    caller_code,
                    "self._close_fd_owners(",
                )
                captured_owners: list[Any] = []
                observations: list[Dict[str, Any]] = []

                def at_caller_cleanup_dispatch(frame: Any) -> bool:
                    nonlocal captured_owners, observations
                    if (
                        frame.f_lineno != dispatch_line
                        or frame.f_locals.get("primary_error") is not primary
                    ):
                        return False
                    names = (
                        ("stage_owner", "parent_owner")
                        if caller == "remove"
                        else (
                            "original_owner",
                            "final_parent_owner",
                            "clone_owner",
                            "stage_owner",
                            "parent_owner",
                        )
                    )
                    captured_owners = [
                        frame.f_locals[name]
                        for name in names
                        if frame.f_locals.get(name) is not None
                    ]
                    if len(captured_owners) < 2 or any(
                        owner.closed for owner in captured_owners
                    ):
                        return False
                    observations = [
                        self.observe_fd_owner_close(owner) for owner in captured_owners
                    ]
                    return True

                try:
                    self.interrupt_handler_preserving_primary(
                        caller_code,
                        at_caller_cleanup_dispatch,
                        operation,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertGreaterEqual(len(captured_owners), 2)
                    self.assertTrue(all(owner.closed for owner in captured_owners))
                    self.assertEqual(
                        [observation["active_closes"] for observation in observations],
                        [1] * len(observations),
                    )
                    self.assertEqual(self.open_fd_set(), baseline_fds)
                    self.assertEqual(stage.stat().st_ino, before_stage.st_ino)
                    self.assertEqual(list(stage.iterdir()), [])
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    if captured_owners:
                        backend._close_fd_owners(
                            tuple(
                                (f"caller test cleanup {index}", owner)
                                for index, owner in enumerate(captured_owners)
                            ),
                            primary_error=primary,
                        )
                    if stage.exists():
                        stage.rmdir()
                    final_path.unlink(missing_ok=True)

    def test_fd_owner_finalization_interruption_preserves_body_primary(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        self.write_source(b"fd-owner-finalization\n")

        class FinalizationBodyPrimary(BaseException):
            pass

        class FinalizationBomb(BaseException):
            pass

        dispatch_line = self.source_line_number(
            backend_module.DarwinBackend._close_fd_owners.__code__,
            "_cleanup_dispatch = 4",
        )
        assignment_line = self.source_line_number(
            backend_module.DarwinBackend._close_fd_owners.__code__,
            "close_failure: Optional[BackendError] = None",
        )

        for boundary in ("dispatch", "assignment"):
            with self.subTest(boundary=boundary):
                raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(2)]
                owners = [
                    backend._adopt_fd(fd, f"{boundary} finalization owner {index}")
                    for index, fd in enumerate(raw_fds)
                ]
                observations = [self.observe_fd_owner_close(owner) for owner in owners]
                descriptors = tuple(
                    (f"{boundary} finalization owner {index}", owner)
                    for index, owner in enumerate(owners)
                )
                primary = FinalizationBodyPrimary(
                    f"{boundary} finalization body primary"
                )
                secondary = FinalizationBomb(
                    f"{boundary} finalization cleanup interruption"
                )

                def at_finalization_guard(frame: Any) -> bool:
                    expected_line = (
                        dispatch_line if boundary == "dispatch" else assignment_line
                    )
                    return (
                        frame.f_lineno == expected_line
                        and frame.f_locals.get("primary_error") is primary
                        and all(owner.closed for owner in owners)
                        and (
                            boundary == "dispatch"
                            or frame.f_locals.get("_cleanup_dispatch") == 4
                        )
                    )

                def fail_with_cleanup() -> None:
                    try:
                        raise primary
                    finally:
                        backend._close_fd_owners(
                            descriptors,
                            primary_error=primary,
                        )

                try:
                    self.interrupt_handler_preserving_primary(
                        backend_module.DarwinBackend._close_fd_owners.__code__,
                        at_finalization_guard,
                        fail_with_cleanup,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertTrue(all(owner.closed for owner in owners))
                    self.assertEqual(
                        [observation["active_closes"] for observation in observations],
                        [1, 1],
                    )
                    for fd in raw_fds:
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    backend._close_fd_owners(descriptors, primary_error=primary)
                    for fd in raw_fds:
                        try:
                            os.close(fd)
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise

    def test_stage_cleanup_after_identity_probe_interrupt_retries_and_removes(
        self,
    ) -> None:
        backend_module = self.backend_module

        class CoreStagePrimary(BaseException):
            pass

        class StageRemoveBomb(BaseException):
            pass

        for implementation in ("core", "override"):
            with self.subTest(implementation=implementation):
                stage_path = self.destination_parent / (
                    f"stage-remove-interruption-{implementation}"
                )
                core_primary = CoreStagePrimary("core stage validation primary")
                secondary = StageRemoveBomb(
                    f"{implementation} stage remove interruption"
                )

                if implementation == "core":

                    class StageBackend(backend_module.DarwinBackend):
                        def __init__(inner_self) -> None:
                            super().__init__()
                            inner_self.fail_validation = False

                        def validate_private_stage_parent(inner_self, fd: int) -> Any:
                            if inner_self.fail_validation:
                                inner_self.fail_validation = False
                                raise core_primary
                            return super().validate_private_stage_parent(fd)

                else:

                    class StageBackend(backend_module.DarwinBackend):
                        def create_private_stage_parent(
                            inner_self,
                            parent_fd: int,
                            name: str,
                            *,
                            authorize_state: Callable[[str], None],
                        ) -> Any:
                            fd, identity = super().create_private_stage_parent(
                                parent_fd,
                                name,
                                authorize_state=authorize_state,
                            )
                            return (fd, identity, "unexpected-extra-field")

                backend = StageBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage_path))
                actions: list[str] = []
                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    name,
                    authorize_state=actions.append,
                )
                cleanup_calls = 0
                real_configure_cleanup = owner._configure_namespace_cleanup

                def install_counted_cleanup(
                    cleanup: Callable[[], Optional[str]],
                ) -> None:
                    def counted_cleanup() -> Optional[str]:
                        nonlocal cleanup_calls
                        cleanup_calls += 1
                        return cleanup()

                    real_configure_cleanup(counted_cleanup)

                owner._configure_namespace_cleanup = install_counted_cleanup
                cleanup_code = next(
                    constant
                    for constant in owner._acquire.__code__.co_consts
                    if getattr(constant, "co_name", None) == "cleanup_created_stage"
                )
                after_identity_probe_line = self.source_line_number(
                    cleanup_code,
                    "expected = target._stage_identity",
                )
                if implementation == "core":
                    backend.fail_validation = True

                captured: Dict[str, Any] = {}
                previous_trace = sys.gettrace()

                def trace(frame: Any, event: str, _arg: Any) -> Any:
                    if event != "line":
                        return trace
                    if (
                        frame.f_code
                        is backend_module._OwnedStageFD._cleanup_created_namespace.__code__
                        and frame.f_locals.get("self") is owner
                        and isinstance(
                            frame.f_locals.get("primary_error"), BaseException
                        )
                        and "primary" not in captured
                    ):
                        captured["primary"] = frame.f_locals["primary_error"]
                        captured["traceback"] = captured["primary"].__traceback__
                    if (
                        frame.f_code is cleanup_code
                        and frame.f_lineno == after_identity_probe_line
                        and frame.f_locals.get("target") is owner
                        and owner._namespace_cleanup_attempted
                        and not captured.get("injected", False)
                    ):
                        captured["injected"] = True
                        sys.settrace(None)
                        raise secondary
                    return trace

                escaped: Optional[BaseException] = None
                sys.settrace(trace)
                try:
                    try:
                        owner.__enter__()
                    except BaseException as exc:
                        escaped = exc
                    else:
                        self.fail("stage cleanup interruption was not delivered")
                finally:
                    sys.settrace(previous_trace)

                try:
                    self.assertTrue(captured.get("injected"))
                    self.assertIs(escaped, captured.get("primary"))
                    if implementation == "core":
                        self.assertIs(escaped, core_primary)
                    else:
                        self.assertIsInstance(escaped, backend_module.BackendError)
                        self.assertEqual(escaped.reason, "invalid_stage_result")
                    original_traceback = captured.get("traceback")
                    if original_traceback is not None:
                        cursor = escaped.__traceback__
                        while cursor is not None and cursor is not original_traceback:
                            cursor = cursor.tb_next
                        self.assertIs(cursor, original_traceback)
                    self.assertEqual(cleanup_calls, 2)
                    self.assertTrue(owner.closed)
                    self.assertTrue(owner._namespace_cleanup_complete)
                    self.assertEqual(actions, ["create_stage", "remove_stage"])
                    self.assertFalse(stage_path.exists())
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(escaped, "cleanup_diagnostic", ""),
                    )
                finally:
                    owner.close(primary_error=escaped)
                    if stage_path.exists():
                        stage_path.rmdir()
                    os.close(parent_fd)

    def test_absolute_parent_invalid_result_handler_interruption_keeps_primary(
        self,
    ) -> None:
        backend_module = self.backend_module
        self.write_source(b"absolute invalid handler\n")

        class AbsoluteHandlerBomb(BaseException):
            pass

        class InvalidAbsoluteParentBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.raw_fd = -1

            def open_absolute_parent(inner_self, path: str) -> Any:
                fd, leaf = super().open_absolute_parent(path)
                inner_self.raw_fd = fd
                return (fd, leaf, "unexpected-extra-field")

        backend = InvalidAbsoluteParentBackend()
        owner, expected_leaf = backend._open_absolute_parent_owned(str(self.source))
        self.assertEqual(expected_leaf, self.source.name)
        close_observation = self.observe_fd_owner_close(owner)
        secondary = AbsoluteHandlerBomb("absolute invalid handler interruption")
        handler_line = self.source_line_number(
            owner._acquire.__code__,
            "_handler_attempt = 1",
        )

        def at_natural_invalid_result_handler(frame: Any) -> bool:
            primary = frame.f_locals.get("primary")
            return (
                frame.f_locals.get("target") is owner
                and frame.f_lineno == handler_line
                and isinstance(primary, backend_module.BackendError)
                and primary.reason == "invalid_path"
                and not owner.closed
                and owner.fileno() == backend.raw_fd
            )

        try:
            captured = self.interrupt_handler_preserving_primary(
                owner._acquire.__code__,
                at_natural_invalid_result_handler,
                owner.__enter__,
                secondary,
            )
            primary = captured["__primary__"]
            self.assertIsInstance(primary, backend_module.BackendError)
            self.assertEqual(primary.reason, "invalid_path")
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertIn(
                type(secondary).__name__,
                getattr(primary, "cleanup_diagnostic", ""),
            )
        finally:
            primary_error = locals().get("primary")
            owner.close(
                primary_error=(
                    primary_error if isinstance(primary_error, BaseException) else None
                )
            )

    def test_absolute_parent_invalid_result_prehandler_interruption_keeps_primary(
        self,
    ) -> None:
        backend_module = self.backend_module
        self.write_source(b"absolute invalid prehandler\n")

        class AbsolutePrehandlerBomb(BaseException):
            pass

        class InvalidAbsoluteParentBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.raw_fd = -1

            def open_absolute_parent(inner_self, path: str) -> Any:
                fd, leaf = super().open_absolute_parent(path)
                inner_self.raw_fd = fd
                return (fd, leaf, "unexpected-extra-field")

        backend = InvalidAbsoluteParentBackend()
        owner, expected_leaf = backend._open_absolute_parent_owned(str(self.source))
        self.assertEqual(expected_leaf, self.source.name)
        close_observation = self.observe_fd_owner_close(owner)
        secondary = AbsolutePrehandlerBomb("absolute invalid prehandler interruption")
        prehandler_line = self.source_line_number(
            owner._acquire.__code__,
            "primary_traceback = primary.__traceback__",
        )

        def before_natural_invalid_result_guard(frame: Any) -> bool:
            primary = frame.f_locals.get("primary")
            return (
                frame.f_locals.get("target") is owner
                and frame.f_lineno == prehandler_line
                and isinstance(primary, backend_module.BackendError)
                and primary.reason == "invalid_path"
                and not owner.closed
                and owner.fileno() == backend.raw_fd
            )

        try:
            captured = self.interrupt_handler_preserving_primary(
                owner._acquire.__code__,
                before_natural_invalid_result_guard,
                owner.__enter__,
                secondary,
            )
            primary = captured["__primary__"]
            self.assertIsInstance(primary, backend_module.BackendError)
            self.assertEqual(primary.reason, "invalid_path")
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertIn(
                type(secondary).__name__,
                getattr(primary, "cleanup_diagnostic", ""),
            )
        finally:
            primary_error = locals().get("primary")
            owner.close(
                primary_error=(
                    primary_error if isinstance(primary_error, BaseException) else None
                )
            )

    def test_unopened_stage_rmdir_and_parent_fsync_interruptions_retry_to_durable_absence(
        self,
    ) -> None:
        backend_module = self.backend_module

        class StageValidationPrimary(BaseException):
            pass

        class StageCleanupBoundaryBomb(BaseException):
            pass

        for boundary, source_line in (
            ("rmdir", "os.rmdir(name, dir_fd=parent_fd)"),
            ("parent_fsync", "self.fsync(parent_fd)"),
        ):
            with self.subTest(boundary=boundary):
                stage_path = self.destination_parent / (
                    f"unopened-stage-cleanup-{boundary}"
                )
                primary = StageValidationPrimary(f"{boundary} stage validation primary")
                secondary = StageCleanupBoundaryBomb(
                    f"{boundary} stage cleanup interruption"
                )

                class StageCleanupBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.fail_validation = False
                        inner_self.cleanup_parent_fd = -1
                        inner_self.cleanup_authorized = False
                        inner_self.cleanup_parent_fsyncs = 0
                        inner_self.failed_stage_fd = -1

                    def validate_private_stage_parent(inner_self, fd: int) -> Any:
                        if inner_self.fail_validation:
                            inner_self.fail_validation = False
                            inner_self.failed_stage_fd = fd
                            raise primary
                        return super().validate_private_stage_parent(fd)

                    def fsync(inner_self, fd: int) -> None:
                        if (
                            inner_self.cleanup_authorized
                            and fd == inner_self.cleanup_parent_fd
                        ):
                            inner_self.cleanup_parent_fsyncs += 1
                        super().fsync(fd)

                backend = StageCleanupBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage_path))
                backend.cleanup_parent_fd = parent_fd
                actions: list[str] = []

                def authorize(action: str) -> None:
                    actions.append(action)
                    if action == "remove_stage":
                        backend.cleanup_authorized = True

                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    name,
                    authorize_state=authorize,
                )
                cleanup_calls = 0
                stage_close_calls: list[int] = []
                real_close = backend_module.os.close
                real_configure_cleanup = owner._configure_namespace_cleanup

                def record_stage_close(fd: int) -> None:
                    if fd == backend.failed_stage_fd:
                        stage_close_calls.append(fd)
                    real_close(fd)

                def install_counted_cleanup(
                    cleanup: Callable[[], Optional[str]],
                ) -> None:
                    def counted_cleanup() -> Optional[str]:
                        nonlocal cleanup_calls
                        cleanup_calls += 1
                        return cleanup()

                    real_configure_cleanup(counted_cleanup)

                owner._configure_namespace_cleanup = install_counted_cleanup
                cleanup_code = (
                    backend_module.DarwinBackend._cleanup_unopened_stage.__code__
                )
                boundary_line = self.source_line_number(cleanup_code, source_line)
                backend.fail_validation = True

                def at_cleanup_boundary(frame: Any) -> bool:
                    if (
                        frame.f_locals.get("self") is not backend
                        or frame.f_locals.get("parent_fd") != parent_fd
                        or frame.f_locals.get("name") != name
                        or frame.f_lineno != boundary_line
                        or cleanup_calls != 1
                        or not owner._namespace_cleanup_attempted
                    ):
                        return False
                    if boundary == "rmdir":
                        return stage_path.is_dir()
                    return not stage_path.exists()

                try:
                    with mock.patch.object(
                        backend_module.os,
                        "close",
                        side_effect=record_stage_close,
                    ):
                        self.interrupt_handler_preserving_primary(
                            cleanup_code,
                            at_cleanup_boundary,
                            owner.__enter__,
                            secondary,
                            expected_primary=primary,
                        )
                    self.assertEqual(cleanup_calls, 2)
                    self.assertTrue(owner._namespace_cleanup_attempted)
                    self.assertTrue(owner._namespace_cleanup_complete)
                    self.assertFalse(stage_path.exists())
                    self.assertGreaterEqual(backend.cleanup_parent_fsyncs, 1)
                    self.assertEqual(actions[0], "create_stage")
                    self.assertIn("remove_stage", actions)
                    self.assertTrue(owner.closed)
                    self.assertGreaterEqual(backend.failed_stage_fd, 0)
                    self.assertEqual(stage_close_calls, [backend.failed_stage_fd])
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.failed_stage_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                    owner.close(primary_error=primary)
                    self.assertEqual(stage_close_calls, [backend.failed_stage_fd])
                finally:
                    owner.close(primary_error=primary)
                    if stage_path.exists():
                        stage_path.rmdir()
                    os.close(parent_fd)

    def test_stage_cleanup_validation_interruptions_retry_with_one_authorization(
        self,
    ) -> None:
        backend_module = self.backend_module

        class StageValidationPrimary(BaseException):
            pass

        class StageCleanupValidationBomb(BaseException):
            pass

        cleanup_code = backend_module.DarwinBackend._cleanup_unopened_stage.__code__
        for boundary, occurrence in (
            ("pre_authorization", 1),
            ("post_authorization", 2),
        ):
            with self.subTest(boundary=boundary):
                stage_path = self.destination_parent / (
                    f"stage-cleanup-validation-{boundary}"
                )
                primary = StageValidationPrimary(
                    f"{boundary} stage setup validation primary"
                )
                secondary = StageCleanupValidationBomb(
                    f"{boundary} cleanup validation interruption"
                )

                class ValidationCleanupBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.fail_stage_validation = False
                        inner_self.failed_stage_fd = -1
                        inner_self.cleanup_parent_fd = -1
                        inner_self.cleanup_started = False
                        inner_self.cleanup_parent_fsyncs = 0

                    def validate_private_stage_parent(inner_self, fd: int) -> Any:
                        if inner_self.fail_stage_validation:
                            inner_self.fail_stage_validation = False
                            inner_self.failed_stage_fd = fd
                            raise primary
                        return super().validate_private_stage_parent(fd)

                    def fsync(inner_self, fd: int) -> None:
                        if (
                            inner_self.cleanup_started
                            and fd == inner_self.cleanup_parent_fd
                            and not stage_path.exists()
                        ):
                            inner_self.cleanup_parent_fsyncs += 1
                        super().fsync(fd)

                backend = ValidationCleanupBackend()
                parent_fd, name = backend.open_absolute_parent(str(stage_path))
                backend.cleanup_parent_fd = parent_fd
                actions: list[str] = []
                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    name,
                    authorize_state=actions.append,
                )
                cleanup_calls = 0
                real_configure_cleanup = owner._configure_namespace_cleanup

                def install_counted_cleanup(
                    cleanup: Callable[[], Optional[str]],
                ) -> None:
                    def counted_cleanup() -> Optional[str]:
                        nonlocal cleanup_calls
                        cleanup_calls += 1
                        backend.cleanup_started = True
                        return cleanup()

                    real_configure_cleanup(counted_cleanup)

                owner._configure_namespace_cleanup = install_counted_cleanup
                boundary_line = self.source_line_number(
                    cleanup_code,
                    "current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)",
                    occurrence=occurrence,
                )
                backend.fail_stage_validation = True

                def at_validation_boundary(frame: Any) -> bool:
                    if (
                        frame.f_locals.get("self") is not backend
                        or frame.f_lineno != boundary_line
                        or frame.f_locals.get("parent_fd") != parent_fd
                        or frame.f_locals.get("name") != name
                        or cleanup_calls != 1
                        or owner._namespace_cleanup_attempts != 1
                        or not owner._namespace_cleanup_attempted
                        or not stage_path.is_dir()
                    ):
                        return False
                    if boundary == "pre_authorization":
                        return (
                            actions == ["create_stage"]
                            and not owner._namespace_cleanup_authorization_attempted
                            and not owner._namespace_cleanup_authorized
                        )
                    return (
                        actions == ["create_stage", "remove_stage"]
                        and owner._namespace_cleanup_authorization_attempted
                        and owner._namespace_cleanup_authorized
                    )

                try:
                    self.interrupt_handler_preserving_primary(
                        cleanup_code,
                        at_validation_boundary,
                        owner.__enter__,
                        secondary,
                        expected_primary=primary,
                    )
                    self.assertEqual(cleanup_calls, 2)
                    self.assertEqual(actions, ["create_stage", "remove_stage"])
                    self.assertTrue(owner._namespace_cleanup_authorization_attempted)
                    self.assertTrue(owner._namespace_cleanup_authorized)
                    self.assertTrue(owner._namespace_cleanup_attempted)
                    self.assertTrue(owner._namespace_cleanup_complete)
                    self.assertEqual(owner._namespace_cleanup_attempts, 2)
                    self.assertFalse(stage_path.exists())
                    self.assertGreaterEqual(backend.cleanup_parent_fsyncs, 1)
                    self.assertTrue(owner.closed)
                    self.assertGreaterEqual(backend.failed_stage_fd, 0)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.failed_stage_fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    owner.close(primary_error=primary)
                    if stage_path.exists():
                        stage_path.rmdir()
                    os.close(parent_fd)

    def test_overridden_stage_explicit_backend_error_from_none_preserves_primary_and_identity_cleanup(
        self,
    ) -> None:
        backend_module = self.backend_module
        stage_path = self.destination_parent / "stage-explicit-from-none"
        actions: list[str] = []

        class HiddenStageContext(RuntimeError):
            pass

        hidden = HiddenStageContext("hidden-stage-context-marker")
        explicit = backend_module.BackendError(
            "explicit_stage_override_failure",
            "explicit-stage-error-marker",
            errno.EIO,
        )
        try:
            raise hidden
        except HiddenStageContext:
            try:
                raise explicit from None
            except backend_module.BackendError as captured_explicit:
                self.assertIs(captured_explicit, explicit)
        origin_traceback = explicit.__traceback__
        self.assertIsNotNone(origin_traceback)
        self.assertIs(explicit.__context__, hidden)
        self.assertIsNone(explicit.__cause__)
        self.assertTrue(explicit.__suppress_context__)

        class OverrideBackend(backend_module.DarwinBackend):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.override_calls = 0
                inner_self.cleanup_expectations: list[Any] = []

            def create_private_stage_parent(
                inner_self,
                parent_fd: int,
                name: str,
                *,
                authorize_state: Callable[[str], None],
            ) -> Any:
                inner_self.override_calls += 1
                return super().create_private_stage_parent(
                    parent_fd,
                    name,
                    authorize_state=authorize_state,
                )

            def _cleanup_unopened_stage(
                inner_self,
                parent_fd: int,
                name: str,
                expected: Any,
                expected_container: Any,
                *,
                authorize_state: Callable[[str], None],
            ) -> Optional[str]:
                inner_self.cleanup_expectations.append((expected, expected_container))
                return super()._cleanup_unopened_stage(
                    parent_fd,
                    name,
                    expected,
                    expected_container,
                    authorize_state=authorize_state,
                )

        backend = OverrideBackend()
        parent_fd, name = backend.open_absolute_parent(str(stage_path))
        owner = backend._create_private_stage_parent_owned(
            parent_fd,
            name,
            authorize_state=actions.append,
        )
        close_observation = self.observe_fd_owner_close(owner)
        handoff_line = self.source_line_number(
            owner._acquire.__code__,
            "if type(handed_off) is not tuple or len(handed_off) != 2:",
        )

        def after_override_handoff(frame: Any) -> bool:
            handed_off = frame.f_locals.get("handed_off")
            identity = frame.f_locals.get("identity")
            handed_off_fd = frame.f_locals.get("handed_off_fd")
            return (
                frame.f_locals.get("target") is owner
                and frame.f_lineno == handoff_line
                and type(handed_off) is tuple
                and len(handed_off) == 2
                and isinstance(identity, backend_module.FileIdentity)
                and isinstance(handed_off_fd, int)
                and handed_off_fd >= 0
                and owner._namespace_created
                and not owner.closed
                and owner.fileno() == handed_off_fd
                and owner.identity() is identity
            )

        try:
            captured = self.interrupt_on_traced_local_handoff(
                owner._acquire.__code__,
                after_override_handoff,
                owner.__enter__,
                explicit,
            )
            self.assertIs(type(explicit), backend_module.BackendError)
            self.assertEqual(explicit.reason, "explicit_stage_override_failure")
            self.assertEqual(explicit.detail, "explicit-stage-error-marker")
            self.assertEqual(explicit.errno_value, errno.EIO)
            traceback_cursor = explicit.__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not origin_traceback
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, origin_traceback)
            diagnostic = getattr(explicit, "cleanup_diagnostic", "")
            self.assertNotIn("hidden-stage-context-marker", diagnostic)
            self.assertEqual(backend.override_calls, 1)
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertTrue(owner._namespace_cleanup_attempted)
            self.assertTrue(owner._namespace_cleanup_complete)
            self.assertEqual(actions, ["create_stage", "remove_stage"])
            self.assertFalse(stage_path.exists())
            self.assertEqual(len(backend.cleanup_expectations), 1)
            cleanup_expected, cleanup_container = backend.cleanup_expectations[0]
            self.assertIs(cleanup_expected, captured["identity"])
            self.assertIs(cleanup_expected, owner.identity())
            self.assertTrue(
                cleanup_container.is_same_object(
                    backend.validate_stage_container(parent_fd)
                )
            )
            owner.close(primary_error=explicit)
            self.assertEqual(close_observation["active_closes"], 1)
        finally:
            owner.close(primary_error=explicit)
            if stage_path.exists():
                stage_path.rmdir()
            os.close(parent_fd)

    def test_acl_normal_with_retained_owner_bridge_drains_without_destructor(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"ACL retained-owner bridge\n")
        original_fd = os.open(self.source, os.O_RDONLY)
        clone_fd = os.open(self.source, os.O_RDWR)
        info = os.fstat(clone_fd)
        expected = self.backend_module.FilePolicy(
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            int(getattr(info, "st_flags", 0)),
            info.st_mtime_ns,
            (),
            b"",
        )

        class ACLBridgePrimary(BaseException):
            pass

        primary = ACLBridgePrimary("ACL normal-with retained-owner interruption")
        pointer_value = 3_401
        pointer = ctypes.c_void_p(pointer_value)

        def acquire_live(target: Any) -> None:
            target._adopt(pointer)

        owner = self.backend_module._OwnedACL(
            backend,
            "ACL normal-with retained owner",
            acquire_live,
        )
        close_observation = self.observe_acl_owner_close(owner)
        free_calls: list[int] = []

        def record_free(freed_pointer: Any) -> int:
            free_calls.append(freed_pointer.value)
            return 0

        bridge_code = self.backend_module.DarwinBackend.calibrate_clone_policy.__code__
        bridge_line = self.source_line_number(
            bridge_code,
            "apply_acl_owner = live_acl_owner",
        )

        def after_normal_with(frame: Any) -> bool:
            live_acl = frame.f_locals.get("live_acl")
            return (
                frame.f_locals.get("self") is backend
                and frame.f_lineno == bridge_line
                and frame.f_locals.get("live_acl_owner") is owner
                and isinstance(live_acl, ctypes.c_void_p)
                and live_acl.value == pointer_value
                and not owner.closed
            )

        try:
            with mock.patch.object(
                backend,
                "require_exclusive_writer_policy",
                return_value=None,
            ):
                with mock.patch.object(
                    backend,
                    "snapshot_policy",
                    side_effect=(expected, expected),
                ):
                    with mock.patch.object(
                        backend,
                        "_get_acl_owned",
                        return_value=owner,
                    ):
                        with mock.patch.object(
                            backend,
                            "_acl_free",
                            side_effect=record_free,
                        ):
                            self.interrupt_on_traced_local_handoff(
                                bridge_code,
                                after_normal_with,
                                lambda: backend.calibrate_clone_policy(
                                    original_fd,
                                    clone_fd,
                                    expected,
                                ),
                                primary,
                            )
            self.assertTrue(owner.closed)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertEqual(free_calls, [pointer_value])
            with mock.patch.object(
                backend,
                "_acl_free",
                side_effect=record_free,
            ):
                owner.close(primary_error=primary)
            self.assertEqual(close_observation["active_closes"], 1)
            self.assertEqual(free_calls, [pointer_value])
        finally:
            with mock.patch.object(
                backend,
                "_acl_free",
                side_effect=record_free,
            ):
                owner.close(primary_error=primary)
            os.close(original_fd)
            os.close(clone_fd)

    def test_stage_parent_normal_with_retained_owner_bridges_drain_create_and_remove(
        self,
    ) -> None:
        backend_module = self.backend_module

        class FDWithBridgePrimary(BaseException):
            pass

        for caller in ("create", "remove"):
            with self.subTest(caller=caller):
                path = self.destination_parent / f"retained-parent-{caller}"
                expected = None
                expected_container = None
                before_identity = None
                if caller == "remove":
                    path.mkdir(mode=0o700)
                    setup_backend = backend_module.DarwinBackend()
                    parent_fd, _name = setup_backend.open_absolute_parent(str(path))
                    stage_fd = setup_backend.open_absolute_dir(str(path))
                    try:
                        expected = setup_backend.validate_private_stage_parent(stage_fd)
                        expected_container = setup_backend.validate_stage_container(
                            parent_fd
                        )
                    finally:
                        os.close(stage_fd)
                        os.close(parent_fd)
                    before_identity = path.stat()

                class RetainedParentBackend(backend_module.DarwinBackend):
                    def __init__(inner_self) -> None:
                        super().__init__()
                        inner_self.parent_owner = None

                    def _open_absolute_parent_owned(
                        inner_self, opened_path: str
                    ) -> Any:
                        owner, name = super()._open_absolute_parent_owned(opened_path)
                        inner_self.parent_owner = owner
                        return owner, name

                backend = RetainedParentBackend()
                actions: list[str] = []
                primary = FDWithBridgePrimary(
                    f"{caller} normal-with retained-parent interruption"
                )
                if caller == "create":
                    caller_code = (
                        backend_module.DarwinBackend.create_private_stage.__code__
                    )
                    bridge_line = self.source_line_number(
                        caller_code,
                        "container_identity = self.validate_stage_container(parent_fd)",
                    )

                    def operation() -> None:
                        backend.create_private_stage(
                            str(path),
                            authorize_state=actions.append,
                        )

                else:
                    caller_code = (
                        backend_module.DarwinBackend.remove_empty_private_stage.__code__
                    )
                    bridge_line = self.source_line_number(
                        caller_code,
                        "self._require_stage_container_mapping(path, parent_fd, expected_container)",
                    )

                    def operation() -> None:
                        backend.remove_empty_private_stage(
                            str(path),
                            expected,
                            expected_container=expected_container,
                            authorize_state=actions.append,
                        )

                parent_fd_at_boundary = -1
                parent_close_at_boundary = None

                def after_parent_normal_with(frame: Any) -> bool:
                    nonlocal parent_close_at_boundary, parent_fd_at_boundary
                    owner = backend.parent_owner
                    matches = (
                        frame.f_locals.get("self") is backend
                        and frame.f_lineno == bridge_line
                        and owner is not None
                        and frame.f_locals.get("parent_owner") is owner
                        and frame.f_locals.get("parent_fd") == owner.fileno()
                        and not owner.closed
                    )
                    if matches:
                        parent_fd_at_boundary = owner.fileno()
                        parent_close_at_boundary = self.observe_fd_owner_close(owner)
                    return matches

                try:
                    self.interrupt_on_traced_local_handoff(
                        caller_code,
                        after_parent_normal_with,
                        operation,
                        primary,
                    )
                    owner = backend.parent_owner
                    self.assertIsNotNone(owner)
                    self.assertTrue(owner.closed)
                    self.assertGreaterEqual(parent_fd_at_boundary, 0)
                    self.assertIsNotNone(parent_close_at_boundary)
                    self.assertEqual(parent_close_at_boundary["active_closes"], 1)
                    self.assertEqual(
                        parent_close_at_boundary["fd"], parent_fd_at_boundary
                    )
                    with self.assertRaises(OSError) as closed:
                        os.fstat(parent_fd_at_boundary)
                    self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertEqual(actions, [])
                    if caller == "create":
                        self.assertFalse(path.exists())
                    else:
                        after_identity = path.stat()
                        self.assertEqual(
                            (after_identity.st_dev, after_identity.st_ino),
                            (before_identity.st_dev, before_identity.st_ino),
                        )
                    owner.close(primary_error=primary)
                    self.assertEqual(parent_close_at_boundary["active_closes"], 1)
                finally:
                    owner = backend.parent_owner
                    if owner is not None:
                        owner.close(primary_error=primary)
                    if path.exists():
                        path.rmdir()

    def test_close_all_error_append_interruption_keeps_actionable_close_failure(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        self.write_source(b"close error append boundary\n")

        body_primary = backend_module.BackendError(
            "close_append_body_primary",
            "close append body detail",
            errno.EIO,
        )
        close_failure = OSError(
            errno.EIO,
            "candidate close failed before descriptor dispatch",
        )

        class CloseErrorAppendBomb(BaseException):
            pass

        secondary = CloseErrorAppendBomb("close-error append line interruption")

        class CloseErrorTransaction(self.helper.MirrorSync):
            def __init__(inner_self) -> None:
                super().__init__(backend)
                inner_self.body_traceback = None

            def _execute(inner_self) -> Any:
                try:
                    raise body_primary
                except backend_module.BackendError as active_primary:
                    inner_self.body_traceback = active_primary.__traceback__
                    raise

        transaction = CloseErrorTransaction()
        raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(3)]
        owners = [
            backend._adopt_fd(fd, f"close error append owner {index}")
            for index, fd in enumerate(raw_fds)
        ]
        close_observations = [self.observe_fd_owner_close(owner) for owner in owners]
        for attribute, owner in zip(
            ("_candidate_owner", "_destination_owner", "_source_owner"),
            owners,
        ):
            transaction._install_fd_owner(attribute, owner)
        failing_owner = owners[0]
        real_owner_close = failing_owner.close
        close_attempts = 0

        def close_then_fail(
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            nonlocal close_attempts
            close_attempts += 1
            if close_attempts == 1:
                raise close_failure
            real_owner_close(
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        failing_owner.close = close_then_fail
        append_code = self.helper.MirrorSync._close_all_pass.__code__
        append_line = self.source_line_number(
            append_code,
            'suffix = " retry" if retry else ""',
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code is append_code
                and frame.f_lineno == append_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("owner") is failing_owner
                and frame.f_locals.get("label") == "candidate_fd"
                and frame.f_locals.get("retry") is False
                and frame.f_locals.get("exc") is close_failure
                and not failing_owner.closed
                and frame.f_locals.get("errors") == []
                and not captured
            ):
                captured["line"] = frame.f_lineno
                captured["body_traceback"] = body_primary.__traceback__
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)
                failing_owner.close = real_owner_close

            self.assertTrue(captured)
            self.assertEqual(close_attempts, 2)
            self.assertTrue(all(owner.closed for owner in owners))
            self.assertEqual(
                [observation["active_closes"] for observation in close_observations],
                [1, 1, 1],
            )
            self.assertIsNone(transaction._candidate_owner)
            self.assertIsNone(transaction._destination_owner)
            self.assertIsNone(transaction._source_owner)
            for raw_fd in raw_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], body_primary.reason)
            self.assertIn(body_primary.detail, receipt["detail"])
            self.assertIn("candidate_fd", receipt["detail"])
            self.assertIn(str(close_failure), receipt["detail"])
            self.assertIn(type(secondary).__name__, receipt["detail"])
            diagnostic = getattr(body_primary, "cleanup_diagnostic", "")
            self.assertIn("candidate_fd", diagnostic)
            self.assertIn(str(close_failure), diagnostic)
            self.assertIn(type(secondary).__name__, diagnostic)
            traceback_cursor = body_primary.__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not transaction.body_traceback
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, transaction.body_traceback)
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            failing_owner.close = real_owner_close
            transaction._close_all(primary_error=body_primary)
            for raw_fd in raw_fds:
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_post_publish_backend_diagnostic_interruption_keeps_primary_receipt(
        self,
    ) -> None:
        backend_module = self.backend_module
        backend = backend_module.DarwinBackend()
        published_content = b"published diagnostic source\n"
        self.write_source(published_content)
        before_destination = self.write_destination(b"published diagnostic old\n")

        primary = backend_module.BackendError(
            "published_backend_failed",
            "published backend primary detail",
            errno.EIO,
        )

        class DiagnosticAssemblyBomb(BaseException):
            pass

        secondary = DiagnosticAssemblyBomb(
            "post-publish diagnostic assembly interruption"
        )

        primary_traceback = None

        def fail_after_publish(stage: str) -> None:
            nonlocal primary_traceback
            if stage == "after_publish":
                try:
                    raise primary
                except backend_module.BackendError as active_primary:
                    primary_traceback = active_primary.__traceback__
                    raise

        transaction = self.helper.MirrorSync(
            backend,
            action_hook=fail_after_publish,
        )
        assembly_line = self.source_line_number(
            self.helper.MirrorSync._backend_error_result.__code__,
            "_combine_diagnostics(",
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code
                is self.helper.MirrorSync._backend_error_result.__code__
                and frame.f_lineno == assembly_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary") is primary
                and not captured
            ):
                captured["line"] = frame.f_lineno
                sys.settrace(None)
                raise secondary
            return trace

        sys.settrace(trace)
        try:
            receipt = transaction.sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            ).to_dict()
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(captured)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertTrue(receipt["destination_mutated"])
        self.assertEqual(receipt["new_size"], receipt["publish_size"])
        self.assertIn(primary.detail, receipt["detail"])
        self.assertIn(type(secondary).__name__, receipt["detail"])
        self.assertIn(
            type(secondary).__name__,
            getattr(primary, "cleanup_diagnostic", ""),
        )
        self.assertIsNotNone(primary_traceback)
        traceback_cursor = primary.__traceback__
        while (
            traceback_cursor is not None and traceback_cursor is not primary_traceback
        ):
            traceback_cursor = traceback_cursor.tb_next
        self.assertIs(traceback_cursor, primary_traceback)
        self.assertNotEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), published_content)
        self.assertEqual(
            receipt["new_identity"],
            {
                "dev": self.destination.stat().st_dev,
                "ino": self.destination.stat().st_ino,
            },
        )
        self.assert_no_stage_names()
        raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_sync_final_receipt_rebuild_interruption_keeps_primary_and_diagnostics(
        self,
    ) -> None:
        backend_module = self.backend_module
        primary = backend_module.BackendError(
            "final_receipt_primary",
            "final receipt primary detail",
            errno.EIO,
        )
        close_marker = "final receipt owned-FD close marker"

        class FinalReceiptBomb(BaseException):
            pass

        secondary = FinalReceiptBomb("final receipt rebuild interruption")

        class FinalReceiptTransaction(self.helper.MirrorSync):
            def __init__(inner_self) -> None:
                super().__init__(backend_module.DarwinBackend())
                inner_self.primary_traceback = None

            def _execute(inner_self) -> Any:
                try:
                    raise primary
                except backend_module.BackendError as active_primary:
                    inner_self.primary_traceback = active_primary.__traceback__
                    raise

            def _close_all(
                inner_self,
                *,
                primary_error: Optional[BaseException],
            ) -> Optional[str]:
                self.assertIs(primary_error, primary)
                return close_marker

        transaction = FinalReceiptTransaction()
        rebuild_line = self.source_line_number(
            self.helper.MirrorSync._finalize_close_result.__code__,
            "if primary_error is not None and result is not None:",
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (
                event == "line"
                and frame.f_code
                is self.helper.MirrorSync._finalize_close_result.__code__
                and frame.f_lineno == rebuild_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is primary
                and frame.f_locals.get("close_error") == close_marker
                and not captured
            ):
                captured["line"] = frame.f_lineno
                sys.settrace(None)
                raise secondary
            return trace

        sys.settrace(trace)
        try:
            receipt = transaction.sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            ).to_dict()
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(captured)
        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertFalse(receipt["destination_mutated"])
        self.assertIn(primary.detail, receipt["detail"])
        self.assertIn(close_marker, receipt["detail"])
        self.assertIn(type(secondary).__name__, receipt["detail"])
        self.assertIn(
            type(secondary).__name__,
            getattr(primary, "cleanup_diagnostic", ""),
        )
        traceback_cursor = primary.__traceback__
        while (
            traceback_cursor is not None
            and traceback_cursor is not transaction.primary_traceback
        ):
            traceback_cursor = traceback_cursor.tb_next
        self.assertIs(traceback_cursor, transaction.primary_traceback)
        raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_cleanup_stage_without_primary_propagates_raw_baseexceptions(self) -> None:
        backend = self.backend_module.DarwinBackend()

        class CleanupStageBomb(BaseException):
            pass

        class CleanupStageSecondary(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("cleanup stage keyboard interrupt"),
            lambda: SystemExit(117),
            lambda: CleanupStageBomb("cleanup stage custom BaseException"),
        )
        for make_primary in primary_factories:
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                secondary = CleanupStageSecondary(
                    f"cleanup retry secondary for {type(primary).__name__}"
                )
                transaction = self.helper.MirrorSync(backend)
                raw_fd = os.open(self.destination_parent, os.O_RDONLY)
                owner = backend._adopt_fd(raw_fd, "cleanup stage raw primary")
                transaction._install_fd_owner("_stage_owner", owner)
                captured_traceback = None
                escaped: Optional[BaseException] = None

                cleanup_hook = mock.Mock(side_effect=(primary, secondary))

                def invoke_cleanup() -> None:
                    nonlocal captured_traceback
                    with mock.patch.object(
                        transaction,
                        "_run_before_cleanup_hook",
                        cleanup_hook,
                    ):
                        try:
                            transaction._cleanup_stage(primary_error=None)
                        except BaseException as active_primary:
                            captured_traceback = active_primary.__traceback__
                            raise

                try:
                    try:
                        invoke_cleanup()
                    except BaseException as error:
                        escaped = error
                    else:
                        self.fail("raw cleanup BaseException was converted to a string")
                    self.assertIs(escaped, primary)
                    self.assertEqual(escaped.args, original_args)
                    if isinstance(primary, SystemExit):
                        self.assertEqual(escaped.code, original_code)
                    traceback_cursor = escaped.__traceback__
                    while (
                        traceback_cursor is not None
                        and traceback_cursor is not captured_traceback
                    ):
                        traceback_cursor = traceback_cursor.tb_next
                    self.assertIs(traceback_cursor, captured_traceback)
                    self.assertEqual(cleanup_hook.call_count, 2)
                    self.assertIn(
                        type(secondary).__name__,
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                    self.assertIn(
                        str(secondary),
                        getattr(primary, "cleanup_diagnostic", ""),
                    )
                finally:
                    transaction._close_all(primary_error=primary)
                    try:
                        os.close(raw_fd)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise

    def test_deferred_after_known_publish_keeps_reason_and_mutated_evidence(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"known published deferred\n"
        self.write_source(published_content)
        before_destination = self.write_destination(b"known published old\n")
        primary = self.helper.DeferredSync(
            "published_source_deferred",
            "known published deferred detail",
        )

        def fail_after_publish(stage: str) -> None:
            if stage == "after_publish":
                raise primary

        receipt = (
            self.helper.MirrorSync(
                backend,
                action_hook=fail_after_publish,
            )
            .sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            )
            .to_dict()
        )

        self.assertEqual(receipt["outcome"], "fatal")
        self.assertEqual(receipt["reason"], primary.reason)
        self.assertEqual(receipt["detail"], primary.detail)
        self.assertTrue(receipt["destination_mutated"])
        self.assertEqual(receipt["new_size"], receipt["publish_size"])
        self.assertIsNotNone(receipt["new_identity"])
        self.assertNotEqual(receipt["new_identity"], receipt["old_identity"])
        self.assertNotEqual(self.destination.stat().st_ino, before_destination.st_ino)
        self.assertEqual(self.destination.read_bytes(), published_content)
        self.assertEqual(
            receipt["new_identity"],
            {
                "dev": self.destination.stat().st_dev,
                "ino": self.destination.stat().st_ino,
            },
        )
        self.assert_no_stage_names()
        raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        validated = self.validate_receipt(receipt, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_sync_handler_cleanup_dispatch_interruptions_retry_known_publish_receipts(
        self,
    ) -> None:
        backend_module = self.backend_module

        class HandlerCleanupDispatchBomb(BaseException):
            pass

        cases = (
            (
                "deferred",
                lambda: self.helper.DeferredSync(
                    "published_handler_deferred",
                    "published handler deferred detail",
                ),
                self.helper.MirrorSync._deferred_result.__code__,
                "published_handler_deferred",
            ),
            (
                "exception",
                lambda: RuntimeError("published handler ordinary detail"),
                self.helper.MirrorSync._unexpected_error_result.__code__,
                "unexpected_error",
            ),
        )
        for case_index, (
            case,
            make_primary,
            cleanup_code,
            expected_reason,
        ) in enumerate(cases):
            with self.subTest(case=case):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                published_content = f"published handler {case_index}\n".encode()
                self.write_source(published_content)
                before_destination = self.write_destination(
                    f"published handler old {case_index}\n".encode()
                )
                backend = backend_module.DarwinBackend()
                primary = make_primary()
                secondary = HandlerCleanupDispatchBomb(
                    f"{case} cleanup dispatch interruption"
                )
                actions: list[str] = []
                hook_traceback = None

                def fail_after_publish(stage: str) -> None:
                    nonlocal hook_traceback
                    actions.append(stage)
                    if stage == "after_publish":
                        try:
                            raise primary
                        except BaseException as active_primary:
                            hook_traceback = active_primary.__traceback__
                            raise

                transaction = self.helper.MirrorSync(
                    backend,
                    action_hook=fail_after_publish,
                )
                dispatch_line = self.source_line_number(
                    cleanup_code,
                    "self._cleanup_stage(primary_error=primary)",
                )
                captured: Dict[str, Any] = {}
                previous_trace = sys.gettrace()

                def trace(frame: Any, event: str, _arg: Any) -> Any:
                    if (
                        event == "line"
                        and frame.f_code is cleanup_code
                        and frame.f_lineno == dispatch_line
                        and frame.f_locals.get("self") is transaction
                        and frame.f_locals.get("primary") is primary
                        and transaction.publish_attempted
                        and transaction.published_identity is not None
                        and not transaction.stage_removed
                        and pathlib.Path(transaction.stage_path).is_dir()
                        and not captured
                    ):
                        captured["line"] = frame.f_lineno
                        captured["primary_traceback"] = primary.__traceback__
                        sys.settrace(None)
                        raise secondary
                    return trace

                escaped: Optional[BaseException] = None
                receipt: Optional[Dict[str, Any]] = None
                sys.settrace(trace)
                try:
                    try:
                        receipt = transaction.sync_one(
                            str(self.source.absolute()),
                            str(self.destination.absolute()),
                        ).to_dict()
                    except BaseException as error:
                        escaped = error
                finally:
                    sys.settrace(previous_trace)

                self.assertTrue(captured)
                self.assertIsNone(
                    escaped,
                    f"handler cleanup dispatch leaked {type(escaped).__name__}",
                )
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], expected_reason)
                self.assertTrue(receipt["destination_mutated"])
                self.assertEqual(receipt["new_size"], receipt["publish_size"])
                expected_primary_detail = getattr(primary, "detail", str(primary))
                self.assertIn(expected_primary_detail, receipt["detail"])
                self.assertIn(type(secondary).__name__, receipt["detail"])
                self.assertIn(
                    type(secondary).__name__,
                    getattr(primary, "cleanup_diagnostic", ""),
                )
                self.assertIsNotNone(hook_traceback)
                traceback_cursor = primary.__traceback__
                while (
                    traceback_cursor is not None
                    and traceback_cursor is not hook_traceback
                ):
                    traceback_cursor = traceback_cursor.tb_next
                self.assertIs(traceback_cursor, hook_traceback)
                self.assertNotEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), published_content)
                self.assertEqual(
                    receipt["new_identity"],
                    {
                        "dev": self.destination.stat().st_dev,
                        "ino": self.destination.stat().st_ino,
                    },
                )
                self.assertIn("authorize_cleanup_stage", actions)
                self.assertTrue(transaction.stage_removed)
                self.assert_no_stage_names()
                self.assertEqual(
                    (
                        transaction.source_parent_fd,
                        transaction.source_fd,
                        transaction.destination_parent_fd,
                        transaction.destination_fd,
                        transaction.stage_fd,
                        transaction.candidate_fd,
                    ),
                    (-1, -1, -1, -1, -1, -1),
                )
                raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_raw_handler_cleanup_dispatch_interruption_preserves_primary_and_stage(
        self,
    ) -> None:
        backend_module = self.backend_module

        class RawHandlerCleanupDispatchBomb(BaseException):
            pass

        primary_factories = (
            lambda: KeyboardInterrupt("raw handler cleanup keyboard interrupt"),
            lambda: SystemExit(129),
        )
        handler_code = self.helper.MirrorSync.sync_one.__code__
        dispatch_line = self.source_line_number(
            handler_code,
            "self._cleanup_stage(primary_error=exc)",
            occurrence=1,
        )
        for case_index, make_primary in enumerate(primary_factories):
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                published_content = f"raw handler published {case_index}\n".encode()
                self.write_source(published_content)
                before_destination = self.write_destination(
                    f"raw handler old {case_index}\n".encode()
                )
                actions: list[str] = []
                hook_traceback = None
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                secondary = RawHandlerCleanupDispatchBomb(
                    f"raw {type(primary).__name__} cleanup dispatch interruption"
                )

                def fail_after_publish(stage: str) -> None:
                    nonlocal hook_traceback
                    actions.append(stage)
                    if stage == "after_publish":
                        try:
                            raise primary
                        except BaseException as active_primary:
                            hook_traceback = active_primary.__traceback__
                            raise

                transaction = self.helper.MirrorSync(
                    backend_module.DarwinBackend(),
                    action_hook=fail_after_publish,
                )

                def at_cleanup_dispatch(frame: Any) -> bool:
                    return (
                        frame.f_lineno == dispatch_line
                        and frame.f_locals.get("self") is transaction
                        and frame.f_locals.get("exc") is primary
                        and frame.f_locals.get("primary_error") is primary
                        and transaction.publish_attempted
                        and transaction.published_identity is not None
                        and not transaction.stage_removed
                        and pathlib.Path(transaction.stage_path).is_dir()
                    )

                self.interrupt_handler_preserving_primary(
                    handler_code,
                    at_cleanup_dispatch,
                    lambda: transaction.sync_one(
                        str(self.source.absolute()),
                        str(self.destination.absolute()),
                    ),
                    secondary,
                    expected_primary=primary,
                )
                self.assertEqual(primary.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(primary.code, original_code)
                self.assertIsNotNone(hook_traceback)
                traceback_cursor = primary.__traceback__
                while (
                    traceback_cursor is not None
                    and traceback_cursor is not hook_traceback
                ):
                    traceback_cursor = traceback_cursor.tb_next
                self.assertIs(traceback_cursor, hook_traceback)
                self.assertIn(
                    type(secondary).__name__,
                    getattr(primary, "cleanup_diagnostic", ""),
                )
                self.assertNotEqual(
                    self.destination.stat().st_ino,
                    before_destination.st_ino,
                )
                self.assertEqual(self.destination.read_bytes(), published_content)
                self.assertIn("authorize_cleanup_stage", actions)
                self.assertTrue(transaction.stage_removed)
                self.assert_no_stage_names()
                self.assertEqual(
                    (
                        transaction.source_parent_fd,
                        transaction.source_fd,
                        transaction.destination_parent_fd,
                        transaction.destination_fd,
                        transaction.stage_fd,
                        transaction.candidate_fd,
                    ),
                    (-1, -1, -1, -1, -1, -1),
                )

    def test_sync_handler_bookkeeping_interruptions_preserve_published_receipts(
        self,
    ) -> None:
        backend_module = self.backend_module

        class HandlerBookkeepingBomb(BaseException):
            pass

        cases = (
            (
                "deferred",
                lambda: self.helper.DeferredSync(
                    "published_bookkeeping_deferred",
                    "published bookkeeping deferred detail",
                ),
                1,
                "published_bookkeeping_deferred",
            ),
            (
                "exception",
                lambda: RuntimeError("published bookkeeping ordinary detail"),
                5,
                "unexpected_error",
            ),
        )
        handler_code = self.helper.MirrorSync.sync_one.__code__
        for case_index, (case, make_primary, occurrence, expected_reason) in enumerate(
            cases
        ):
            with self.subTest(case=case):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                published_content = f"published bookkeeping {case_index}\n".encode()
                self.write_source(published_content)
                self.write_destination(f"bookkeeping old {case_index}\n".encode())
                primary = make_primary()
                secondary = HandlerBookkeepingBomb(
                    f"{case} handler bookkeeping interruption"
                )
                actions: list[str] = []
                hook_traceback = None

                def fail_after_publish(stage: str) -> None:
                    nonlocal hook_traceback
                    actions.append(stage)
                    if stage == "after_publish":
                        try:
                            raise primary
                        except BaseException as active_primary:
                            hook_traceback = active_primary.__traceback__
                            raise

                transaction = self.helper.MirrorSync(
                    backend_module.DarwinBackend(),
                    action_hook=fail_after_publish,
                )
                bookkeeping_line = self.source_line_number(
                    handler_code,
                    "primary_error = exc",
                    occurrence=occurrence,
                )
                captured: Dict[str, Any] = {}
                previous_trace = sys.gettrace()

                def trace(frame: Any, event: str, _arg: Any) -> Any:
                    if (
                        event == "line"
                        and frame.f_code is handler_code
                        and frame.f_lineno == bookkeeping_line
                        and frame.f_locals.get("self") is transaction
                        and frame.f_locals.get("exc") is primary
                        and frame.f_locals.get("primary_error") is None
                        and transaction.publish_attempted
                        and transaction.published_identity is not None
                        and not transaction.stage_removed
                        and pathlib.Path(transaction.stage_path).is_dir()
                        and not captured
                    ):
                        captured["line"] = frame.f_lineno
                        captured["traceback"] = primary.__traceback__
                        sys.settrace(None)
                        raise secondary
                    return trace

                receipt: Optional[Dict[str, Any]] = None
                escaped: Optional[BaseException] = None
                sys.settrace(trace)
                try:
                    try:
                        receipt = transaction.sync_one(
                            str(self.source.absolute()),
                            str(self.destination.absolute()),
                        ).to_dict()
                    except BaseException as error:
                        escaped = error
                finally:
                    sys.settrace(previous_trace)

                self.assertTrue(captured)
                self.assertIsNone(escaped)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt["outcome"], "fatal")
                self.assertEqual(receipt["reason"], expected_reason)
                self.assertTrue(receipt["destination_mutated"])
                self.assertEqual(receipt["new_size"], receipt["publish_size"])
                self.assertEqual(self.destination.read_bytes(), published_content)
                expected_primary_detail = getattr(primary, "detail", str(primary))
                self.assertIn(expected_primary_detail, receipt["detail"])
                self.assertIn(type(secondary).__name__, receipt["detail"])
                self.assertIn(
                    type(secondary).__name__,
                    getattr(primary, "cleanup_diagnostic", ""),
                )
                self.assertIsNotNone(hook_traceback)
                traceback_cursor = primary.__traceback__
                while (
                    traceback_cursor is not None
                    and traceback_cursor is not hook_traceback
                ):
                    traceback_cursor = traceback_cursor.tb_next
                self.assertIs(traceback_cursor, hook_traceback)
                self.assertIn("authorize_cleanup_stage", actions)
                self.assertTrue(transaction.stage_removed)
                self.assert_no_stage_names()
                self.assertEqual(
                    (
                        transaction.source_parent_fd,
                        transaction.source_fd,
                        transaction.destination_parent_fd,
                        transaction.destination_fd,
                        transaction.stage_fd,
                        transaction.candidate_fd,
                    ),
                    (-1, -1, -1, -1, -1, -1),
                )
                raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
                validated = self.validate_receipt(receipt, 2, raw_input=raw)
                self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_raw_handler_bookkeeping_interruptions_preserve_primary_and_stage(
        self,
    ) -> None:
        backend_module = self.backend_module

        class RawHandlerBookkeepingBomb(BaseException):
            pass

        handler_code = self.helper.MirrorSync.sync_one.__code__
        bookkeeping_line = self.source_line_number(
            handler_code,
            "primary_error = exc",
            occurrence=7,
        )
        for case_index, make_primary in enumerate(
            (
                lambda: KeyboardInterrupt("raw bookkeeping keyboard interrupt"),
                lambda: SystemExit(137),
            )
        ):
            primary = make_primary()
            with self.subTest(primary=type(primary).__name__):
                self.remove_path(self.source)
                self.remove_path(self.destination)
                published_content = f"raw bookkeeping {case_index}\n".encode()
                self.write_source(published_content)
                self.write_destination(f"raw bookkeeping old {case_index}\n".encode())
                original_args = primary.args
                original_code = getattr(primary, "code", None)
                secondary = RawHandlerBookkeepingBomb(
                    f"raw {type(primary).__name__} bookkeeping interruption"
                )
                actions: list[str] = []
                hook_traceback = None

                def fail_after_publish(stage: str) -> None:
                    nonlocal hook_traceback
                    actions.append(stage)
                    if stage == "after_publish":
                        try:
                            raise primary
                        except BaseException as active_primary:
                            hook_traceback = active_primary.__traceback__
                            raise

                transaction = self.helper.MirrorSync(
                    backend_module.DarwinBackend(),
                    action_hook=fail_after_publish,
                )

                def at_bookkeeping(frame: Any) -> bool:
                    return (
                        frame.f_lineno == bookkeeping_line
                        and frame.f_locals.get("self") is transaction
                        and frame.f_locals.get("exc") is primary
                        and frame.f_locals.get("primary_error") is None
                        and transaction.publish_attempted
                        and transaction.published_identity is not None
                        and not transaction.stage_removed
                        and pathlib.Path(transaction.stage_path).is_dir()
                    )

                self.interrupt_handler_preserving_primary(
                    handler_code,
                    at_bookkeeping,
                    lambda: transaction.sync_one(
                        str(self.source.absolute()),
                        str(self.destination.absolute()),
                    ),
                    secondary,
                    expected_primary=primary,
                )
                self.assertEqual(primary.args, original_args)
                if isinstance(primary, SystemExit):
                    self.assertEqual(primary.code, original_code)
                self.assertIsNotNone(hook_traceback)
                traceback_cursor = primary.__traceback__
                while (
                    traceback_cursor is not None
                    and traceback_cursor is not hook_traceback
                ):
                    traceback_cursor = traceback_cursor.tb_next
                self.assertIs(traceback_cursor, hook_traceback)
                self.assertIn(
                    type(secondary).__name__,
                    getattr(primary, "cleanup_diagnostic", ""),
                )
                self.assertEqual(self.destination.read_bytes(), published_content)
                self.assertIn("authorize_cleanup_stage", actions)
                self.assertTrue(transaction.stage_removed)
                self.assert_no_stage_names()
                self.assertEqual(
                    (
                        transaction.source_parent_fd,
                        transaction.source_fd,
                        transaction.destination_parent_fd,
                        transaction.destination_fd,
                        transaction.stage_fd,
                        transaction.candidate_fd,
                    ),
                    (-1, -1, -1, -1, -1, -1),
                )

    def test_close_all_retry_guard_interruption_still_drains_and_reports_failure(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"close retry guard boundary\n")
        transaction = self.helper.MirrorSync(backend)
        transaction.stage_removed = True
        body_primary = RuntimeError("close retry guard body primary")
        first_close_failure = OSError(
            errno.EIO,
            "candidate first close failure",
        )

        class RetryGuardBomb(BaseException):
            pass

        secondary = RetryGuardBomb("retry close guard interruption")
        raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(2)]
        owners = [
            backend._adopt_fd(fd, f"retry guard owner {index}")
            for index, fd in enumerate(raw_fds)
        ]
        close_observations = [self.observe_fd_owner_close(owner) for owner in owners]
        transaction._install_fd_owner("_candidate_owner", owners[0])
        transaction._install_fd_owner("_source_owner", owners[1])
        failing_owner = owners[0]
        real_owner_close = failing_owner.close
        close_attempts = 0
        close_detail: Optional[str] = None
        body_traceback = None

        def fail_first_close(
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            nonlocal close_attempts
            close_attempts += 1
            if close_attempts == 1:
                raise first_close_failure
            real_owner_close(
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        failing_owner.close = fail_first_close
        pass_code = self.helper.MirrorSync._close_all_pass.__code__
        retry_guard_line = self.source_line_number(
            pass_code,
            "_owner_close_attempt = 1",
        )

        def at_retry_guard(frame: Any) -> bool:
            errors = frame.f_locals.get("errors")
            active_failure = frame.f_locals.get("active_failure")
            return (
                frame.f_lineno == retry_guard_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("retry") is True
                and frame.f_locals.get("_attribute") == "_candidate_owner"
                and frame.f_locals.get("label") == "candidate_fd"
                and frame.f_locals.get("owner") is failing_owner
                and close_attempts == 1
                and not failing_owner.closed
                and isinstance(errors, list)
                and any(
                    "candidate_fd" in error and str(first_close_failure) in error
                    for error in errors
                )
                and isinstance(active_failure, dict)
                and active_failure.get("label") == "candidate_fd"
                and active_failure.get("owner") is failing_owner
                and active_failure.get("retry") is True
            )

        def close_while_body_primary_is_active() -> None:
            nonlocal body_traceback, close_detail
            try:
                raise body_primary
            except RuntimeError as active_primary:
                body_traceback = active_primary.__traceback__
                close_detail = transaction._close_all(primary_error=body_primary)
                raise

        try:
            self.interrupt_handler_preserving_primary(
                pass_code,
                at_retry_guard,
                close_while_body_primary_is_active,
                secondary,
                expected_primary=body_primary,
            )
            self.assertIsNotNone(body_traceback)
            self.assertEqual(close_attempts, 2)
            self.assertIsNotNone(close_detail)
            self.assertIn("candidate_fd", close_detail)
            self.assertIn(str(first_close_failure), close_detail)
            self.assertIn(type(secondary).__name__, close_detail)
            self.assertIn(
                type(secondary).__name__,
                getattr(body_primary, "cleanup_diagnostic", ""),
            )
            self.assertTrue(all(owner.closed for owner in owners))
            self.assertEqual(
                [observation["active_closes"] for observation in close_observations],
                [1, 1],
            )
            self.assertIsNone(transaction._candidate_owner)
            self.assertIsNone(transaction._source_owner)
            self.assertEqual(transaction.candidate_fd, -1)
            self.assertEqual(transaction.source_fd, -1)
            for raw_fd in raw_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            sys.settrace(None)
            failing_owner.close = real_owner_close
            transaction._close_all(primary_error=body_primary)
            for raw_fd in raw_fds:
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_successful_publish_with_open_owner_becomes_fatal_and_drains(self) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"open owner verification publish\n"
        self.write_source(published_content)
        self.write_destination(b"open owner verification old\n")
        transaction = self.helper.MirrorSync(backend)
        source_owner = None
        source_raw_fd = -1
        source_observation = None
        real_source_close = None
        close_attempts = 0

        def retain_source_until_authoritative_drain(stage: str) -> None:
            nonlocal close_attempts
            nonlocal real_source_close
            nonlocal source_observation
            nonlocal source_owner
            nonlocal source_raw_fd
            if stage != "after_publish":
                return
            source_owner = transaction._source_owner
            self.assertIsNotNone(source_owner)
            source_raw_fd = source_owner.fileno()
            source_observation = self.observe_fd_owner_close(source_owner)
            real_source_close = source_owner.close

            def return_while_open(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                nonlocal close_attempts
                close_attempts += 1
                if close_attempts <= 2:
                    return
                real_source_close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )

            source_owner.close = return_while_open

        transaction.action_hook = retain_source_until_authoritative_drain
        try:
            receipt = transaction.sync_one(
                str(self.source.absolute()),
                str(self.destination.absolute()),
            ).to_dict()

            self.assertEqual(close_attempts, 3)
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assertIn("source_fd", receipt["detail"])
            self.assertIn("owner remained open after close retry", receipt["detail"])
            self.assertIsNotNone(source_owner)
            self.assertTrue(source_owner.closed)
            self.assertIsNone(transaction._source_owner)
            self.assertEqual(transaction.source_fd, -1)
            self.assertIsNotNone(source_observation)
            self.assertEqual(source_observation["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(source_raw_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertTrue(transaction.stage_removed)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            if source_owner is not None and real_source_close is not None:
                source_owner.close = real_source_close
            transaction._close_all(primary_error=None)
            if source_raw_fd >= 0:
                try:
                    os.close(source_raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_published_result_durable_close_interruption_is_fatal_and_drained(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"published durable close interruption\n"
        self.write_source(published_content)
        before_destination = self.write_destination(b"published close old\n")
        transaction = self.helper.MirrorSync(backend)

        class PublishedCloseBomb(BaseException):
            pass

        primary = PublishedCloseBomb("published-close-state-transfer-marker")
        close_line = self.source_line_number(
            self.backend_module._FDState.close.__code__,
            "self.fd = -1; os.close(fd)  # noqa: E702",
        )
        real_dispatch = backend._dispatch_open_leaf_owned
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def capture_published_owner(
            parent_fd: int,
            name: str,
            *,
            writable: bool = False,
        ) -> Any:
            owner = real_dispatch(parent_fd, name, writable=writable)
            if (
                transaction.stage_removed
                and parent_fd == transaction.destination_parent_fd
                and name == transaction.destination_name
                and "owner" not in captured
            ):
                real_owner_close = owner.close
                captured["owner"] = owner
                captured["close_attempts"] = 0
                captured["real_owner_close"] = real_owner_close

                def record_owner_close(
                    *,
                    primary_error: Optional[BaseException] = None,
                    durable_namespace_complete: bool = False,
                ) -> None:
                    captured["close_attempts"] += 1
                    real_owner_close(
                        primary_error=primary_error,
                        durable_namespace_complete=durable_namespace_complete,
                    )

                owner.close = record_owner_close
            return owner

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            owner = captured.get("owner")
            if (
                event == "line"
                and frame.f_code is self.backend_module._FDState.close.__code__
                and frame.f_lineno == close_line
                and owner is not None
                and frame.f_locals.get("self") is owner._state
                and frame.f_locals.get("durable_namespace_complete") is True
                and isinstance(
                    frame.f_locals.get("primary_error"),
                    self.backend_module.BackendError,
                )
                and "interrupted" not in captured
            ):
                captured["interrupted"] = True
                captured["fd"] = frame.f_locals["fd"]
                captured["close_receipt"] = frame.f_locals["primary_error"]
                captured["close_observation"] = self.observe_fd_owner_close(owner)
                sys.settrace(None)
                raise primary
            return trace

        try:
            with mock.patch.object(
                backend,
                "_dispatch_open_leaf_owned",
                side_effect=capture_published_owner,
            ):
                sys.settrace(trace)
                try:
                    receipt = transaction.sync_one(
                        str(self.source.absolute()),
                        str(self.destination.absolute()),
                    ).to_dict()
                finally:
                    sys.settrace(previous_trace)

            self.assertTrue(captured.get("interrupted"), captured)
            published_owner = captured["owner"]
            published_fd = captured["fd"]
            self.assertTrue(published_owner.closed)
            self.assertEqual(captured["close_attempts"], 2)
            self.assertEqual(captured["close_observation"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(published_fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            close_receipt = captured["close_receipt"]
            close_diagnostic = getattr(close_receipt, "cleanup_diagnostic", "")
            self.assertIn(type(primary).__name__, close_diagnostic)
            self.assertIn("published-close-state-transfer-marker", close_diagnostic)
            self.assertLessEqual(
                len(close_diagnostic.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )

            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertIsNotNone(receipt["new_identity"])
            self.assertNotEqual(receipt["new_identity"], receipt["old_identity"])
            self.assertIn(type(primary).__name__, receipt["detail"])
            self.assertIn("published-close-state-transfer-marker", receipt["detail"])
            self.assertIn("required a recovery close", receipt["detail"])
            self.assertNotEqual(
                self.destination.stat().st_ino,
                before_destination.st_ino,
            )
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assertEqual(
                receipt["new_identity"],
                {
                    "dev": self.destination.stat().st_dev,
                    "ino": self.destination.stat().st_ino,
                },
            )
            self.assertTrue(transaction.stage_removed)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            self.assertLess(len(raw), 64 * 1024)
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            owner = captured.get("owner")
            if owner is not None:
                owner.close = captured["real_owner_close"]
                owner.close(primary_error=primary)
            transaction._close_all(primary_error=primary)

    def test_bounded_multi_owner_close_diagnostics_survive_finalization(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"bounded multi-owner cleanup diagnostics\n"
        self.write_source(published_content)
        self.write_destination(b"bounded multi-owner old\n")
        transaction = self.helper.MirrorSync(backend)

        class OwnerCloseFailure(OSError):
            pass

        class OwnerFinalizationBomb(BaseException):
            pass

        labels = ("destination_fd", "destination_parent_fd", "source_fd")
        markers = (
            "destination-owner-first-marker",
            "destination-parent-second-marker",
            "source-owner-third-marker",
        )
        failures = (
            OwnerCloseFailure(
                errno.EIO,
                f"{markers[0]}-" + "x" * (self.helper._DIAGNOSTIC_LIMIT * 2),
            ),
            OwnerCloseFailure(errno.EBUSY, markers[1]),
            OwnerCloseFailure(errno.EBADF, markers[2]),
        )
        secondary = OwnerFinalizationBomb("owner-finalization-async-marker")
        owner_attributes = (
            "_destination_owner",
            "_destination_parent_owner",
            "_source_owner",
        )
        owners: Dict[str, Any] = {}
        real_closes: Dict[str, Callable[..., None]] = {}
        close_observations: Dict[str, Dict[str, Any]] = {}
        close_attempts = {label: 0 for label in labels}
        raw_fds: Dict[str, int] = {}
        first_failure_traceback = None
        wrapped = False

        def wrap_close(
            label: str,
            owner: Any,
            failure: BaseException,
        ) -> Callable[..., None]:
            real_owner_close = owner.close

            def fail_once(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                nonlocal first_failure_traceback
                close_attempts[label] += 1
                if close_attempts[label] == 1:
                    try:
                        raise failure
                    except BaseException as active_failure:
                        if label == labels[0]:
                            first_failure_traceback = active_failure.__traceback__
                        raise
                real_owner_close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )

            return fail_once

        def install_close_failures(stage: str) -> None:
            nonlocal wrapped
            if stage != "after_publish" or wrapped:
                return
            wrapped = True
            for attribute, label, failure in zip(
                owner_attributes,
                labels,
                failures,
            ):
                owner = getattr(transaction, attribute)
                self.assertIsNotNone(owner)
                owners[label] = owner
                raw_fds[label] = owner.fileno()
                close_observations[label] = self.observe_fd_owner_close(owner)
                real_closes[label] = owner.close
                owner.close = wrap_close(label, owner, failure)

        transaction.action_hook = install_close_failures
        close_code = self.helper.MirrorSync._close_all.__code__
        finalization_line = self.source_line_number(
            close_code,
            "_cleanup_dispatch = 4",
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            errors = frame.f_locals.get("errors")
            if (
                event == "line"
                and frame.f_code is close_code
                and frame.f_lineno == finalization_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is None
                and all(close_attempts[label] == 2 for label in labels)
                and all(owner.closed for owner in owners.values())
                and isinstance(errors, list)
                and all(
                    any(label in detail and marker in detail for detail in errors)
                    for label, marker in zip(labels, markers)
                )
                and not captured
            ):
                captured["line"] = frame.f_lineno
                captured["errors"] = tuple(errors)
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)
                for label, owner in owners.items():
                    owner.close = real_closes[label]

            self.assertTrue(captured)
            self.assertIsNotNone(first_failure_traceback)
            traceback_cursor = failures[0].__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not first_failure_traceback
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, first_failure_traceback)
            self.assertEqual(
                [close_attempts[label] for label in labels],
                [2, 2, 2],
            )
            self.assertTrue(all(owner.closed for owner in owners.values()))
            self.assertEqual(
                [close_observations[label]["active_closes"] for label in labels],
                [1, 1, 1],
            )
            for label in labels:
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fds[label])
                self.assertEqual(closed.exception.errno, errno.EBADF)

            diagnostic = getattr(failures[0], "cleanup_diagnostic", "")
            self.assertIn(type(secondary).__name__, diagnostic)
            self.assertIn("owner-finalization-async-marker", diagnostic)
            for label, marker in zip(labels, markers):
                self.assertIn(label, diagnostic)
                self.assertIn(marker, diagnostic)
                self.assertIn(label, receipt["detail"])
                self.assertIn(marker, receipt["detail"])
            self.assertIn(self.helper._TRUNCATED_MARKER, diagnostic)
            self.assertLessEqual(
                len(diagnostic.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertLessEqual(
                len(receipt["detail"].encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(self.destination.read_bytes(), published_content)
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            self.assertLess(len(raw), 64 * 1024)
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            for label, owner in owners.items():
                owner.close = real_closes[label]
            transaction._close_all(primary_error=failures[0])
            for raw_fd in raw_fds.values():
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_owner_recovery_flag_survives_lossy_nineteen_segment_detail(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"structured owner recovery survives truncation\n"
        self.write_source(published_content)
        self.write_destination(b"structured owner recovery old\n")

        class OwnerPassFailure(Exception):
            pass

        class RecoveryPostprocessBomb(BaseException):
            pass

        labels = (
            "candidate_fd",
            "stage_fd",
            "destination_fd",
            "destination_parent_fd",
            "source_fd",
            "source_parent_fd",
        )
        owner_attributes = (
            "_candidate_owner",
            "_stage_owner",
            "_destination_owner",
            "_destination_parent_owner",
            "_source_owner",
            "_source_parent_owner",
        )
        retained_after_retry = {
            "destination_fd",
            "destination_parent_fd",
            "source_parent_fd",
        }
        close_attempts = {label: 0 for label in labels}
        owners: Dict[str, Any] = {}
        real_closes: Dict[str, Callable[..., None]] = {}
        close_observations: Dict[str, Dict[str, Any]] = {}
        raw_fds: Dict[str, int] = {}
        captured: Dict[str, Any] = {}
        transaction: Any = None

        def wrap_close(label: str, owner: Any) -> Callable[..., None]:
            real_owner_close = owner.close

            def fail_for_pass(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                close_attempts[label] += 1
                attempt = close_attempts[label]
                failure = OwnerPassFailure(
                    f"{label}-pass-{attempt}-marker-"
                    + "x" * (self.helper._DIAGNOSTIC_LIMIT * 2)
                )
                if attempt == 1 or (attempt == 2 and label in retained_after_retry):
                    raise failure
                real_owner_close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
                raise failure

            return fail_for_pass

        class RecoveryTransaction(self.helper.MirrorSync):
            def _execute(inner_self: Any) -> Any:
                result = super()._execute()
                if inner_self._candidate_owner is None:
                    raw_fd = os.open(self.source, os.O_RDONLY)
                    candidate_owner = backend._adopt_fd(
                        raw_fd,
                        "synthetic candidate recovery owner",
                    )
                    with candidate_owner:
                        inner_self._install_fd_owner(
                            "_candidate_owner",
                            candidate_owner,
                        )
                for attribute, label in zip(owner_attributes, labels):
                    owner = getattr(inner_self, attribute)
                    self.assertIsNotNone(owner)
                    owners[label] = owner
                    raw_fds[label] = owner.fileno()
                    close_observations[label] = self.observe_fd_owner_close(owner)
                    real_closes[label] = owner.close
                    owner.close = wrap_close(label, owner)
                return result

            def _finalize_close_result(
                inner_self: Any,
                result: Any,
                **kwargs: Any,
            ) -> Any:
                recovery_marker = "owner remained open after close retry"
                original_parts = tuple(inner_self._owner_close_diagnostics)
                filtered_parts = tuple(
                    part for part in original_parts if recovery_marker not in part
                )
                captured["original_close_error"] = kwargs.get("close_error")
                captured["owner_recovery_required"] = (
                    inner_self._owner_recovery_required
                )
                captured["filtered_parts"] = filtered_parts
                kwargs["close_error"] = self.helper._combine_owner_diagnostics(
                    *filtered_parts
                )
                captured["close_error"] = kwargs["close_error"]
                inner_self._owner_close_diagnostics[:] = filtered_parts
                try:
                    return super()._finalize_close_result(result, **kwargs)
                finally:
                    inner_self._owner_close_diagnostics[:] = original_parts

        transaction = RecoveryTransaction(backend)
        pass_code = self.helper.MirrorSync._close_all_pass.__code__
        postprocess_line = self.source_line_number(
            pass_code,
            "for attribute, label, owner in entries:",
        )
        secondary = RecoveryPostprocessBomb(
            "third-pass-postprocess-marker-" + "y" * (self.helper._DIAGNOSTIC_LIMIT * 2)
        )
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            errors = frame.f_locals.get("errors")
            if (
                event == "line"
                and frame.f_code is pass_code
                and frame.f_lineno == postprocess_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("retry") is True
                and isinstance(errors, list)
                and len(errors) == 18
                and all(owner.closed for owner in owners.values())
                and all(
                    close_attempts[label] == (3 if label in retained_after_retry else 2)
                    for label in labels
                )
                and "trace_line" not in captured
            ):
                captured["trace_line"] = frame.f_lineno
                captured["errors_before_interruption"] = tuple(errors)
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)
                for label, owner in owners.items():
                    owner.close = real_closes[label]

            self.assertIn("trace_line", captured)
            self.assertEqual(len(captured["errors_before_interruption"]), 18)
            self.assertTrue(captured["owner_recovery_required"])
            self.assertIn(
                "owner remained open after close retry",
                captured["original_close_error"],
            )
            close_error = captured.get("close_error")
            self.assertIsInstance(close_error, str)
            self.assertNotIn(
                "owner remained open after close retry",
                close_error,
            )
            self.assertIn(self.helper._TRUNCATED_MARKER, close_error)
            self.assertLessEqual(
                len(close_error.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(
                [close_attempts[label] for label in labels],
                [2, 2, 3, 3, 2, 3],
            )
            self.assertTrue(all(owner.closed for owner in owners.values()))
            self.assertEqual(
                [close_observations[label]["active_closes"] for label in labels],
                [1, 1, 1, 1, 1, 1],
            )
            for raw_fd in raw_fds.values():
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)

            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assertTrue(transaction.stage_removed)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            self.assertLess(len(raw), 64 * 1024)
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            for label, owner in owners.items():
                owner.close = real_closes[label]
            if transaction is not None:
                transaction._close_all(primary_error=None)
            for raw_fd in raw_fds.values():
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_thirty_owner_diagnostics_retain_slot_pass_prefixes_and_markers(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"thirty owner diagnostics\n")
        transaction = self.helper.MirrorSync(backend)
        transaction.stage_removed = True

        class OwnerLabelFailure(Exception):
            pass

        owner_attributes = (
            "_candidate_owner",
            "_stage_owner",
            "_destination_owner",
            "_destination_parent_owner",
            "_source_owner",
            "_source_parent_owner",
        )
        labels = (
            "candidate_fd",
            "stage_fd",
            "destination_fd",
            "destination_parent_fd",
            "source_fd",
            "source_parent_fd",
        )
        raw_fds = [os.open(self.source, os.O_RDONLY) for _label in labels]
        owners = [
            backend._adopt_fd(raw_fd, f"thirty-segment {label}")
            for raw_fd, label in zip(raw_fds, labels)
        ]
        real_closes = [owner.close for owner in owners]
        close_attempts = {label: 0 for label in labels}

        def wrap_close(label: str) -> Callable[..., None]:
            def fail_while_open(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                del primary_error, durable_namespace_complete
                close_attempts[label] += 1
                attempt = close_attempts[label]
                raise OwnerLabelFailure(
                    f"{label}-pass-{attempt}-marker-"
                    + "z" * (self.helper._DIAGNOSTIC_LIMIT * 2)
                )

            return fail_while_open

        for attribute, label, owner in zip(owner_attributes, labels, owners):
            transaction._install_fd_owner(attribute, owner)
            owner.close = wrap_close(label)

        try:
            detail = transaction._close_all(primary_error=None)
            self.assertIsInstance(detail, str)
            self.assertEqual(
                [close_attempts[label] for label in labels],
                [3, 3, 3, 3, 3, 3],
            )
            self.assertEqual(len(detail.split("; ")), 30)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertIn(self.helper._TRUNCATED_MARKER, detail)
            for label in labels:
                self.assertIn(
                    f"{label}: OwnerLabelFailure: {label}-pass-1-marker",
                    detail,
                )
                for attempt in (2, 3):
                    self.assertIn(
                        f"{label} retry: OwnerLabelFailure: "
                        f"{label}-pass-{attempt}-marker",
                        detail,
                    )
                self.assertGreaterEqual(
                    detail.count(f"{label}: owner remained open after close retry"),
                    2,
                )
        finally:
            for owner, real_close in zip(owners, real_closes):
                owner.close = real_close
            transaction._close_all(primary_error=None)
            for raw_fd in raw_fds:
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_hostile_owner_delimiters_cannot_create_synthetic_segments(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"hostile owner diagnostic delimiters\n")
        transaction = self.helper.MirrorSync(backend)
        transaction.stage_removed = True

        class HostileOwnerFailure(Exception):
            pass

        owner_attributes = (
            "_candidate_owner",
            "_stage_owner",
            "_destination_owner",
            "_destination_parent_owner",
            "_source_owner",
            "_source_parent_owner",
        )
        labels = (
            "candidate_fd",
            "stage_fd",
            "destination_fd",
            "destination_parent_fd",
            "source_fd",
            "source_parent_fd",
        )
        hostile_body = "".join(
            (
                f"; diagnostic: d{index}"
                f"; source_fd retry: s{index}"
                f"; destination_parent_fd: p{index}"
                f"; : e{index}"
            )
            for index in range(64)
        )
        raw_fds = [os.open(self.source, os.O_RDONLY) for _label in labels]
        owners = [
            backend._adopt_fd(raw_fd, f"hostile-segment {label}")
            for raw_fd, label in zip(raw_fds, labels)
        ]
        real_closes = [owner.close for owner in owners]
        close_attempts = {label: 0 for label in labels}
        expected_labels = []
        expected_markers = []
        for attempt in (1, 2, 3):
            suffix = "" if attempt == 1 else " retry"
            for label_index, label in enumerate(labels):
                expected_labels.append(f"{label}{suffix}")
                expected_markers.append(f"h{attempt}{label_index}")
            if attempt > 1:
                for label in labels:
                    expected_labels.append(label)
                    expected_markers.append("owner remained open after close retry")
        self.assertEqual(len(expected_labels), 30)

        def wrap_close(label: str, label_index: int) -> Callable[..., None]:
            def fail_while_open(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                del primary_error, durable_namespace_complete
                close_attempts[label] += 1
                attempt = close_attempts[label]
                marker = f"h{attempt}{label_index}"
                suffix = hostile_body if attempt == 1 and label_index == 0 else ""
                raise HostileOwnerFailure(
                    marker + suffix + "-" + "界" * (self.helper._DIAGNOSTIC_LIMIT * 2)
                )

            return fail_while_open

        for label_index, (attribute, label, owner) in enumerate(
            zip(owner_attributes, labels, owners)
        ):
            transaction._install_fd_owner(attribute, owner)
            owner.close = wrap_close(label, label_index)

        try:
            detail = transaction._close_all(primary_error=None)
            self.assertIsInstance(detail, str)
            segments = tuple(transaction._owner_close_diagnostics)
            self.assertEqual(len(segments), 30)
            self.assertTrue(
                all(
                    isinstance(segment, self.helper._DiagnosticSegment)
                    for segment in segments
                )
            )
            self.assertEqual(
                tuple(segment.label for segment in segments),
                tuple(expected_labels),
            )
            for segment, marker in zip(segments, expected_markers):
                self.assertIn(marker, segment.message)
            self.assertIn("; diagnostic: d0", segments[0].message)
            self.assertIn(
                "; source_fd retry: s0",
                segments[0].message,
            )

            self.assertEqual(
                detail,
                self.helper._combine_owner_diagnostics(*segments),
            )
            rendered_parts = detail.split("; ")
            self.assertEqual(len(rendered_parts), 30)
            for part, label, marker in zip(
                rendered_parts,
                expected_labels,
                expected_markers,
            ):
                self.assertTrue(part.startswith(f"{label}: "), part)
                self.assertIn(marker, part)
            self.assertIn("\\x3b diagnostic: d0", detail)
            self.assertIn("\\x3b source_fd retry: s0", detail)
            self.assertNotIn("; diagnostic: d0", detail)
            self.assertNotIn("; source_fd retry: s0", detail)
            self.assertIn(self.helper._TRUNCATED_MARKER, detail)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
        finally:
            for owner, real_close in zip(owners, real_closes):
                owner.close = real_close
            transaction._close_all(primary_error=None)
            for raw_fd in raw_fds:
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_two_close_batches_do_not_recollect_rendered_owner_notes(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"two structured owner cleanup batches\n"
        self.write_source(published_content)
        self.write_destination(b"two owner batches old\n")

        class OwnerBatchFailure(Exception):
            pass

        class FirstBatchFinalizationBomb(BaseException):
            pass

        labels = (
            "candidate_fd",
            "stage_fd",
            "destination_fd",
            "destination_parent_fd",
            "source_fd",
            "source_parent_fd",
        )
        owner_attributes = (
            "_candidate_owner",
            "_stage_owner",
            "_destination_owner",
            "_destination_parent_owner",
            "_source_owner",
            "_source_parent_owner",
        )
        close_attempts = {label: 0 for label in labels}
        owners: Dict[str, Any] = {}
        real_closes: Dict[str, Callable[..., None]] = {}
        raw_fds: Dict[str, int] = {}
        captured: Dict[str, Any] = {}
        first_failure: Optional[BaseException] = None
        first_failure_traceback = None
        transaction: Any = None

        expected_owner_labels = []
        expected_owner_markers = []
        for attempt in range(1, 7):
            batch_attempt = ((attempt - 1) % 3) + 1
            suffix = "" if batch_attempt == 1 else " retry"
            for label_index, label in enumerate(labels):
                expected_owner_labels.append(f"{label}{suffix}")
                expected_owner_markers.append(f"b{attempt}{label_index}")
            if batch_attempt > 1:
                for label in labels:
                    expected_owner_labels.append(label)
                    expected_owner_markers.append(
                        "owner remained open after close retry"
                    )
        self.assertEqual(len(expected_owner_labels), 60)

        def wrap_close(
            label: str,
            label_index: int,
        ) -> Callable[..., None]:
            def fail_while_open(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                nonlocal first_failure
                nonlocal first_failure_traceback
                del primary_error, durable_namespace_complete
                close_attempts[label] += 1
                attempt = close_attempts[label]
                failure = OwnerBatchFailure(
                    f"b{attempt}{label_index}-"
                    + "x" * (self.helper._DIAGNOSTIC_LIMIT * 2)
                )
                try:
                    raise failure
                except OwnerBatchFailure as active_failure:
                    if attempt == 1 and label_index == 0:
                        first_failure = active_failure
                        first_failure_traceback = active_failure.__traceback__
                    raise

            return fail_while_open

        class TwoBatchTransaction(self.helper.MirrorSync):
            def _execute(inner_self: Any) -> Any:
                result = super()._execute()
                if inner_self._candidate_owner is None:
                    raw_fd = os.open(self.source, os.O_RDONLY)
                    candidate_owner = backend._adopt_fd(
                        raw_fd,
                        "synthetic two-batch candidate owner",
                    )
                    with candidate_owner:
                        inner_self._install_fd_owner(
                            "_candidate_owner",
                            candidate_owner,
                        )
                for label_index, (attribute, label) in enumerate(
                    zip(owner_attributes, labels)
                ):
                    owner = getattr(inner_self, attribute)
                    self.assertIsNotNone(owner)
                    owners[label] = owner
                    raw_fds[label] = owner.fileno()
                    real_closes[label] = owner.close
                    owner.close = wrap_close(label, label_index)
                return result

            def _finalize_close_result(
                inner_self: Any,
                result: Any,
                **kwargs: Any,
            ) -> Any:
                primary_error = kwargs.get("primary_error")
                captured["primary_error"] = primary_error
                captured["owner_segments"] = tuple(inner_self._owner_close_diagnostics)
                captured["primary_owner_segments"] = (
                    self.helper._direct_owner_cleanup_diagnostics(primary_error)
                )
                captured["generic_parts"] = self.helper._cleanup_diagnostic_parts(
                    primary_error
                )
                try:
                    captured["primary_notes"] = tuple(
                        getattr(primary_error, "__notes__", ())
                    )
                except BaseException:
                    captured["primary_notes"] = ()
                return super()._finalize_close_result(result, **kwargs)

        transaction = TwoBatchTransaction(backend)
        close_code = self.helper.MirrorSync._close_all.__code__
        finalization_line = self.source_line_number(
            close_code,
            "_cleanup_dispatch = 4",
        )
        secondary = FirstBatchFinalizationBomb("first-owner-batch-finalization-marker")
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            errors = frame.f_locals.get("errors")
            if (
                event == "line"
                and frame.f_code is close_code
                and frame.f_lineno == finalization_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is None
                and isinstance(errors, list)
                and len(errors) == 30
                and all(close_attempts[label] == 3 for label in labels)
                and all(not owner.closed for owner in owners.values())
                and "trace_line" not in captured
            ):
                captured["trace_line"] = frame.f_lineno
                captured["first_batch"] = tuple(errors)
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)
                for label, owner in owners.items():
                    owner.close = real_closes[label]

            self.assertIn("trace_line", captured)
            self.assertEqual(len(captured["first_batch"]), 30)
            self.assertIsNotNone(first_failure)
            self.assertIs(captured["primary_error"], first_failure)
            traceback_cursor = first_failure.__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not first_failure_traceback
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, first_failure_traceback)
            self.assertEqual(
                [close_attempts[label] for label in labels],
                [6, 6, 6, 6, 6, 6],
            )

            owner_segments = captured["owner_segments"]
            self.assertEqual(len(owner_segments), 60)
            self.assertTrue(
                all(
                    isinstance(segment, self.helper._DiagnosticSegment)
                    for segment in owner_segments
                )
            )
            self.assertEqual(
                tuple(segment.label for segment in owner_segments),
                tuple(expected_owner_labels),
            )
            for segment, marker in zip(
                owner_segments,
                expected_owner_markers,
            ):
                self.assertIn(marker, segment.message)
            self.assertEqual(
                captured["primary_owner_segments"],
                owner_segments,
            )

            generic_parts = captured["generic_parts"]
            self.assertEqual(len(generic_parts), 1)
            self.assertIn(type(secondary).__name__, generic_parts[0])
            self.assertIn(
                "first-owner-batch-finalization-marker",
                generic_parts[0],
            )
            primary_notes = captured["primary_notes"]
            batch_notes = tuple(
                note
                for note in primary_notes
                if isinstance(note, str)
                and "candidate_fd:" in note
                and "source_parent_fd" in note
            )
            self.assertEqual(len(batch_notes), 2)
            self.assertTrue(all(note not in generic_parts for note in batch_notes))

            detail = receipt["detail"]
            self.assertIsInstance(detail, str)
            rendered_parts = detail.split("; ")
            self.assertEqual(len(rendered_parts), 62)
            self.assertTrue(rendered_parts[0].startswith("operation: "))
            self.assertTrue(rendered_parts[1].startswith("cleanup-note-1: "))
            self.assertIn(type(secondary).__name__, rendered_parts[1])
            self.assertEqual(
                tuple(part.split(": ", 1)[0] for part in rendered_parts[2:]),
                tuple(expected_owner_labels),
            )
            for marker in expected_owner_markers:
                self.assertIn(marker, detail)
            for label in labels:
                self.assertEqual(
                    detail.count(f"{label}: owner remained open after close retry"),
                    4,
                )
            for batch_note in batch_notes:
                self.assertNotIn(batch_note, detail)
            self.assertNotIn("cleanup-summary:", detail)
            self.assertIn(self.helper._TRUNCATED_MARKER, detail)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )

            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assertTrue(transaction.stage_removed)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            self.assertLess(len(raw), 64 * 1024)
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            for label, owner in owners.items():
                owner.close = real_closes[label]
            if transaction is not None:
                transaction._close_all(primary_error=first_failure)
            for raw_fd in raw_fds.values():
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_plain_note_equal_to_rendered_owner_batch_remains_generic(
        self,
    ) -> None:
        transaction = self.helper.MirrorSync(self.backend_module.DarwinBackend())
        transaction.source_path = str(self.source.absolute())
        transaction.destination_path = str(self.destination.absolute())
        primary = self.backend_module.BackendError(
            "plain_note_collision",
            "plain note collision operation marker",
            errno.EIO,
        )
        owner_segment = self.helper._diagnostic_segment(
            "candidate_fd",
            "plain-note-owner-batch-marker",
        )
        transaction._owner_close_diagnostics.append(owner_segment)
        self.helper._attach_owner_cleanup_diagnostics(primary, (owner_segment,))
        owner_notes = tuple(
            note
            for note in getattr(primary, "__notes__", ())
            if isinstance(note, self.helper._RenderedOwnerDiagnostics)
        )
        self.assertEqual(len(owner_notes), 1)
        plain_note = "{}".format(owner_notes[0])
        self.assertIs(type(plain_note), str)
        self.assertEqual(plain_note, owner_notes[0])
        notes = list(getattr(primary, "__notes__", ()))
        notes.append(plain_note)
        setattr(primary, "__notes__", notes)

        generic_parts = self.helper._cleanup_diagnostic_parts(primary)
        self.assertEqual(generic_parts, (plain_note,))
        result = transaction._fatal_receipt(primary.reason, primary.detail)
        receipt = transaction._finalize_close_result(
            result,
            primary_error=primary,
            primary_detail_base=primary.detail,
            close_error=self.helper._RenderedOwnerDiagnostics(plain_note),
        )
        self.assertIsNotNone(receipt)
        payload = receipt.to_dict()
        detail = payload["detail"]
        self.assertIsInstance(detail, str)
        rendered_parts = detail.split("; ")
        self.assertEqual(len(rendered_parts), 3)
        self.assertTrue(rendered_parts[0].startswith("operation: "))
        self.assertTrue(rendered_parts[1].startswith("cleanup-note-1: "))
        self.assertTrue(rendered_parts[2].startswith("candidate_fd: "))
        self.assertEqual(detail.count("plain-note-owner-batch-marker"), 2)
        self.assertNotIn("cleanup-summary:", detail)
        self.assertEqual(payload["outcome"], "fatal")
        self.assertEqual(payload["reason"], primary.reason)
        self.assertFalse(payload["destination_mutated"])
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
        validated = self.validate_receipt(payload, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_plain_close_error_equal_to_owner_render_keeps_cleanup_summary(
        self,
    ) -> None:
        transaction = self.helper.MirrorSync(self.backend_module.DarwinBackend())
        transaction.source_path = str(self.source.absolute())
        transaction.destination_path = str(self.destination.absolute())
        owner_segment = self.helper._diagnostic_segment(
            "candidate_fd",
            "plain-close-error-owner-marker",
        )
        transaction._owner_close_diagnostics.append(owner_segment)
        plain_close_error = "{}".format(
            self.helper._combine_owner_diagnostics(owner_segment)
        )
        self.assertIs(type(plain_close_error), str)
        self.assertEqual(
            plain_close_error,
            self.helper._combine_owner_diagnostics(owner_segment),
        )

        receipt = transaction._finalize_close_result(
            None,
            primary_error=None,
            primary_detail_base=None,
            close_error=plain_close_error,
        )
        self.assertIsNotNone(receipt)
        payload = receipt.to_dict()
        detail = payload["detail"]
        self.assertIsInstance(detail, str)
        rendered_parts = detail.split("; ")
        self.assertEqual(len(rendered_parts), 2)
        self.assertTrue(rendered_parts[0].startswith("candidate_fd: "))
        self.assertTrue(rendered_parts[1].startswith("cleanup-summary: "))
        self.assertEqual(detail.count("plain-close-error-owner-marker"), 2)
        self.assertEqual(payload["outcome"], "fatal")
        self.assertEqual(payload["reason"], "close_failed")
        self.assertFalse(payload["destination_mutated"])
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
        validated = self.validate_receipt(payload, 2, raw_input=raw)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_typed_close_result_survives_receipt_finalization_retry_once(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"typed close result finalization retry\n"
        self.write_source(published_content)
        self.write_destination(b"typed close result old\n")

        class OwnerCloseFailure(OSError):
            pass

        class ReceiptFinalizationBomb(BaseException):
            pass

        close_failure = OwnerCloseFailure(
            errno.EIO,
            "typed-owner-close-marker",
        )
        secondary = ReceiptFinalizationBomb(
            "receipt-finalization-typed-marker",
        )
        captured: Dict[str, Any] = {}
        transaction: Any = None

        class TypedCloseResultTransaction(self.helper.MirrorSync):
            def _execute(inner_self: Any) -> Any:
                result = super()._execute()
                inner_self.stage_removed = False
                raw_fd = os.open(self.source, os.O_RDONLY)
                owner = backend._adopt_fd(
                    raw_fd,
                    "typed close result candidate owner",
                )
                with owner:
                    inner_self._install_fd_owner("_candidate_owner", owner)
                captured["owner"] = owner
                captured["raw_fd"] = raw_fd
                captured["close_observation"] = self.observe_fd_owner_close(owner)
                captured["real_close"] = owner.close
                captured["close_attempts"] = 0

                def fail_once(
                    *,
                    primary_error: Optional[BaseException] = None,
                    durable_namespace_complete: bool = False,
                ) -> None:
                    captured["close_attempts"] += 1
                    if captured["close_attempts"] == 1:
                        raise close_failure
                    captured["real_close"](
                        primary_error=primary_error,
                        durable_namespace_complete=durable_namespace_complete,
                    )

                owner.close = fail_once
                return result

        transaction = TypedCloseResultTransaction(backend)
        finalizer_code = self.helper.MirrorSync._finalize_close_result.__code__
        finalizer_line = self.source_line_number(
            finalizer_code,
            "if primary_error is not None and result is not None:",
        )
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            close_error = frame.f_locals.get("close_error")
            close_parts = frame.f_locals.get("close_parts")
            if (
                event == "line"
                and frame.f_code is finalizer_code
                and frame.f_lineno == finalizer_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is None
                and isinstance(close_error, self.helper._RenderedOwnerDiagnostics)
                and isinstance(close_parts, tuple)
                and len(close_parts) == 1
                and close_parts[0].label == "candidate_fd"
                and "typed-owner-close-marker" in close_parts[0].message
                and captured.get("close_attempts") == 2
                and captured["owner"].closed
                and "trace_line" not in captured
            ):
                captured["trace_line"] = frame.f_lineno
                captured["typed_close_error"] = close_error
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)
                owner = captured.get("owner")
                if owner is not None:
                    owner.close = captured["real_close"]

            self.assertIn("trace_line", captured)
            self.assertIsInstance(
                captured["typed_close_error"],
                self.helper._RenderedOwnerDiagnostics,
            )
            self.assertEqual(captured["close_attempts"], 2)
            owner = captured["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(captured["close_observation"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(captured["raw_fd"])
            self.assertEqual(closed.exception.errno, errno.EBADF)

            detail = receipt["detail"]
            self.assertIsInstance(detail, str)
            rendered_parts = detail.split("; ")
            self.assertEqual(len(rendered_parts), 2)
            self.assertTrue(rendered_parts[0].startswith("operation: "))
            self.assertTrue(rendered_parts[1].startswith("candidate_fd: "))
            self.assertIn(type(secondary).__name__, rendered_parts[0])
            self.assertIn("receipt-finalization-typed-marker", rendered_parts[0])
            self.assertEqual(detail.count("typed-owner-close-marker"), 1)
            self.assertNotIn("cleanup-summary:", detail)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            owner = captured.get("owner")
            if owner is not None:
                owner.close = captured["real_close"]
                owner.close(primary_error=secondary)
            if transaction is not None:
                transaction._close_all(primary_error=secondary)
            raw_fd = captured.get("raw_fd")
            if isinstance(raw_fd, int):
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_plain_close_error_finalization_interruption_is_not_reembedded(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"plain close finalization primary\n")
        primary = self.backend_module.BackendError(
            "plain_finalization_primary",
            "plain finalization primary detail",
            errno.EIO,
        )
        plain_close_error = "plain-close-evidence-marker"

        class PlainFinalizationBomb(BaseException):
            pass

        secondary = PlainFinalizationBomb(
            "plain-finalization-interruption-marker",
        )
        captured: Dict[str, Any] = {"finalizer_primaries": []}
        transaction: Any = None

        class PlainCloseFinalizationTransaction(self.helper.MirrorSync):
            def _execute(inner_self: Any) -> Any:
                raw_fd = os.open(self.source, os.O_RDONLY)
                owner = backend._adopt_fd(
                    raw_fd,
                    "plain close finalization owner",
                )
                with owner:
                    inner_self._install_fd_owner("_candidate_owner", owner)
                captured["owner"] = owner
                captured["raw_fd"] = raw_fd
                captured["close_observation"] = self.observe_fd_owner_close(owner)
                try:
                    raise primary
                except self.backend_module.BackendError as active_primary:
                    captured["primary_traceback"] = active_primary.__traceback__
                    raise

            def _close_all(
                inner_self: Any,
                *,
                primary_error: Optional[BaseException],
            ) -> Optional[str]:
                captured["close_primary"] = primary_error
                captured["super_close_error"] = super()._close_all(
                    primary_error=primary_error
                )
                return plain_close_error

            def _finalize_close_result(
                inner_self: Any,
                result: Any,
                **kwargs: Any,
            ) -> Any:
                captured["finalizer_primaries"].append(kwargs.get("primary_error"))
                return super()._finalize_close_result(result, **kwargs)

        transaction = PlainCloseFinalizationTransaction(backend)
        finalizer_code = self.helper.MirrorSync._finalize_close_result.__code__
        finalizer_line = self.source_line_number(
            finalizer_code,
            "if primary_error is not None and result is not None:",
        )
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            close_parts = frame.f_locals.get("close_parts")
            if (
                event == "line"
                and frame.f_code is finalizer_code
                and frame.f_lineno == finalizer_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is primary
                and frame.f_locals.get("close_error") == plain_close_error
                and type(frame.f_locals.get("close_error")) is str
                and isinstance(close_parts, tuple)
                and len(close_parts) == 1
                and close_parts[0].label == "cleanup-summary"
                and close_parts[0].message == plain_close_error
                and captured["owner"].closed
                and "trace_line" not in captured
            ):
                captured["trace_line"] = frame.f_lineno
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)

            self.assertIn("trace_line", captured)
            self.assertIs(captured["close_primary"], primary)
            self.assertIsNone(captured["super_close_error"])
            self.assertEqual(len(captured["finalizer_primaries"]), 2)
            self.assertIs(captured["finalizer_primaries"][0], primary)
            self.assertIs(captured["finalizer_primaries"][1], primary)
            traceback_cursor = primary.__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not captured["primary_traceback"]
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, captured["primary_traceback"])
            owner = captured["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(captured["close_observation"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(captured["raw_fd"])
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertIsNone(transaction._candidate_owner)

            detail = receipt["detail"]
            self.assertIsInstance(detail, str)
            rendered_parts = detail.split("; ")
            self.assertEqual(len(rendered_parts), 3)
            self.assertTrue(rendered_parts[0].startswith("operation: "))
            self.assertTrue(rendered_parts[1].startswith("cleanup-note-1: "))
            self.assertTrue(rendered_parts[2].startswith("cleanup-summary: "))
            self.assertEqual(
                detail.count("plain-finalization-interruption-marker"),
                1,
            )
            self.assertEqual(detail.count(plain_close_error), 1)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], primary.reason)
            self.assertFalse(receipt["destination_mutated"])
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            owner = captured.get("owner")
            if owner is not None:
                owner.close(primary_error=primary)
            if transaction is not None:
                transaction._close_all(primary_error=primary)
            raw_fd = captured.get("raw_fd")
            if isinstance(raw_fd, int):
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_none_close_error_finalization_uses_static_fatal_fallback(
        self,
    ) -> None:
        backend = self.backend_module.DarwinBackend()
        published_content = b"none close error finalization retry\n"
        self.write_source(published_content)
        self.write_destination(b"none close error old\n")

        class NoneCloseFinalizationBomb(BaseException):
            pass

        secondary = NoneCloseFinalizationBomb(
            "none-close-finalization-interruption-marker",
        )
        captured: Dict[str, Any] = {"finalizer_primaries": []}
        transaction: Any = None

        class NoneCloseFinalizationTransaction(self.helper.MirrorSync):
            def _execute(inner_self: Any) -> Any:
                result = super()._execute()
                raw_fd = os.open(self.source, os.O_RDONLY)
                owner = backend._adopt_fd(
                    raw_fd,
                    "none close finalization owner",
                )
                with owner:
                    inner_self._install_fd_owner("_candidate_owner", owner)
                captured["owner"] = owner
                captured["raw_fd"] = raw_fd
                captured["close_observation"] = self.observe_fd_owner_close(owner)
                return result

            def _finalize_close_result(
                inner_self: Any,
                result: Any,
                **kwargs: Any,
            ) -> Any:
                captured["finalizer_primaries"].append(kwargs.get("primary_error"))
                return super()._finalize_close_result(result, **kwargs)

        transaction = NoneCloseFinalizationTransaction(backend)
        finalizer_code = self.helper.MirrorSync._finalize_close_result.__code__
        finalizer_line = self.source_line_number(
            finalizer_code,
            "if close_error is None and not cleanup_failed:",
        )
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            result = frame.f_locals.get("result")
            if (
                event == "line"
                and frame.f_code is finalizer_code
                and frame.f_lineno == finalizer_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is None
                and frame.f_locals.get("close_error") is None
                and frame.f_locals.get("cleanup_failed") is False
                and getattr(result, "outcome", None) == "updated"
                and captured["owner"].closed
                and transaction._candidate_owner is None
                and "trace_line" not in captured
            ):
                captured["trace_line"] = frame.f_lineno
                sys.settrace(None)
                raise secondary
            return trace

        try:
            sys.settrace(trace)
            try:
                receipt = transaction.sync_one(
                    str(self.source.absolute()),
                    str(self.destination.absolute()),
                ).to_dict()
            finally:
                sys.settrace(previous_trace)

            self.assertIn("trace_line", captured)
            self.assertEqual(len(captured["finalizer_primaries"]), 2)
            self.assertIsNone(captured["finalizer_primaries"][0])
            self.assertIs(captured["finalizer_primaries"][1], secondary)
            traceback_cursor = secondary.__traceback__
            saw_trace_frame = False
            while traceback_cursor is not None:
                if traceback_cursor.tb_frame.f_code is trace.__code__:
                    saw_trace_frame = True
                    break
                traceback_cursor = traceback_cursor.tb_next
            self.assertTrue(saw_trace_frame)
            owner = captured["owner"]
            self.assertTrue(owner.closed)
            self.assertEqual(captured["close_observation"]["active_closes"], 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(captured["raw_fd"])
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertIsNone(transaction._candidate_owner)

            detail = receipt["detail"]
            self.assertIsInstance(detail, str)
            rendered_parts = detail.split("; ")
            self.assertEqual(len(rendered_parts), 1)
            self.assertTrue(rendered_parts[0].startswith("operation: "))
            self.assertEqual(
                detail.count("none-close-finalization-interruption-marker"),
                1,
            )
            self.assertNotIn("cleanup-summary:", detail)
            self.assertNotIn("receipt finalization failed", detail)
            self.assertNotIn("cleanup-note-", detail)
            self.assertLessEqual(
                len(detail.encode("utf-8")),
                self.helper._DIAGNOSTIC_LIMIT,
            )
            self.assertEqual(receipt["outcome"], "fatal")
            self.assertEqual(receipt["reason"], "close_failed")
            self.assertTrue(receipt["destination_mutated"])
            self.assertEqual(receipt["new_size"], receipt["publish_size"])
            self.assertEqual(self.destination.read_bytes(), published_content)
            self.assertTrue(transaction.stage_removed)
            self.assert_no_stage_names()
            raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            validated = self.validate_receipt(receipt, 2, raw_input=raw)
            self.assertEqual(validated.returncode, 0, validated.stderr)
        finally:
            sys.settrace(previous_trace)
            owner = captured.get("owner")
            if owner is not None:
                owner.close(primary_error=secondary)
            if transaction is not None:
                transaction._close_all(primary_error=secondary)
            raw_fd = captured.get("raw_fd")
            if isinstance(raw_fd, int):
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_close_all_no_body_finalization_keeps_first_close_primary(self) -> None:
        backend = self.backend_module.DarwinBackend()
        self.write_source(b"close finalization guard boundary\n")
        transaction = self.helper.MirrorSync(backend)
        raw_fds = [os.open(self.source, os.O_RDONLY) for _index in range(2)]
        owners = [
            backend._adopt_fd(fd, f"finalization guard owner {index}")
            for index, fd in enumerate(raw_fds)
        ]
        close_observations = [self.observe_fd_owner_close(owner) for owner in owners]
        transaction._install_fd_owner("_candidate_owner", owners[0])
        transaction._install_fd_owner("_source_owner", owners[1])

        class FirstCloseFailure(OSError):
            pass

        class SecondCloseFailure(OSError):
            pass

        class FinalizationGuardBomb(BaseException):
            pass

        first_close_failure = FirstCloseFailure(
            errno.EIO,
            "candidate finalization first close failure",
        )
        second_close_failure = SecondCloseFailure(
            errno.EBUSY,
            "source finalization second close failure",
        )
        secondary = FinalizationGuardBomb("close finalization guard interruption")
        failing_owner = owners[0]
        second_failing_owner = owners[1]
        real_owner_close = failing_owner.close
        real_second_owner_close = second_failing_owner.close
        close_attempts = 0
        second_close_attempts = 0
        first_failure_traceback = None

        def fail_first_close(
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            nonlocal close_attempts, first_failure_traceback
            close_attempts += 1
            if close_attempts == 1:
                try:
                    raise first_close_failure
                except FirstCloseFailure as active_failure:
                    first_failure_traceback = active_failure.__traceback__
                    raise
            real_owner_close(
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        def fail_second_close(
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            nonlocal second_close_attempts
            second_close_attempts += 1
            if second_close_attempts == 1:
                raise second_close_failure
            real_second_owner_close(
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        failing_owner.close = fail_first_close
        second_failing_owner.close = fail_second_close
        close_code = self.helper.MirrorSync._close_all.__code__
        finalization_line = self.source_line_number(
            close_code,
            "_cleanup_dispatch = 4",
        )
        captured: Dict[str, Any] = {}
        previous_trace = sys.gettrace()

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            errors = frame.f_locals.get("errors")
            if (
                event == "line"
                and frame.f_code is close_code
                and frame.f_lineno == finalization_line
                and frame.f_locals.get("self") is transaction
                and frame.f_locals.get("primary_error") is None
                and close_attempts == 2
                and second_close_attempts == 2
                and all(owner.closed for owner in owners)
                and transaction._candidate_owner is None
                and transaction._source_owner is None
                and frame.f_locals.get("active_failure") == {}
                and isinstance(errors, list)
                and any(
                    "candidate_fd" in error and str(first_close_failure) in error
                    for error in errors
                )
                and any(
                    "source_fd" in error and str(second_close_failure) in error
                    for error in errors
                )
                and not captured
            ):
                captured["line"] = frame.f_lineno
                captured["errors"] = tuple(errors)
                sys.settrace(None)
                raise secondary
            return trace

        escaped: Optional[BaseException] = None
        try:
            sys.settrace(trace)
            try:
                transaction._close_all(primary_error=None)
            except BaseException as error:
                escaped = error
            finally:
                sys.settrace(previous_trace)
                failing_owner.close = real_owner_close
                second_failing_owner.close = real_second_owner_close

            self.assertTrue(captured)
            self.assertIs(escaped, first_close_failure)
            self.assertEqual(type(escaped), FirstCloseFailure)
            self.assertEqual(escaped.args, first_close_failure.args)
            self.assertIsNotNone(first_failure_traceback)
            traceback_cursor = escaped.__traceback__
            while (
                traceback_cursor is not None
                and traceback_cursor is not first_failure_traceback
            ):
                traceback_cursor = traceback_cursor.tb_next
            self.assertIs(traceback_cursor, first_failure_traceback)
            diagnostic = getattr(first_close_failure, "cleanup_diagnostic", "")
            self.assertIn("candidate_fd", diagnostic)
            self.assertIn(str(first_close_failure), diagnostic)
            self.assertIn("source_fd", diagnostic)
            self.assertIn(str(second_close_failure), diagnostic)
            self.assertIn(type(secondary).__name__, diagnostic)
            self.assertEqual(close_attempts, 2)
            self.assertEqual(second_close_attempts, 2)
            self.assertTrue(all(owner.closed for owner in owners))
            self.assertEqual(
                [observation["active_closes"] for observation in close_observations],
                [1, 1],
            )
            self.assertIsNone(transaction._candidate_owner)
            self.assertIsNone(transaction._source_owner)
            for raw_fd in raw_fds:
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            sys.settrace(previous_trace)
            failing_owner.close = real_owner_close
            second_failing_owner.close = real_second_owner_close
            transaction._close_all(primary_error=first_close_failure)
            for raw_fd in raw_fds:
                try:
                    os.close(raw_fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_validate_receipt_requires_exact_integer_version_one(self) -> None:
        receipt = self.updated_receipt()
        self.assertIs(type(receipt["version"]), int)
        self.assertEqual(receipt["version"], 1)
        accepted = self.validate_receipt(receipt, 0)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for invalid_version in (True, 1.0):
            invalid = dict(receipt)
            invalid["version"] = invalid_version
            with self.subTest(version=repr(invalid_version)):
                rejected = self.validate_receipt(invalid, 0)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b"receipt version", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
