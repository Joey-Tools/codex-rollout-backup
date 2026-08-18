#!/usr/bin/env python3
"""Tests for the fail-closed rollout reflink repair utility."""

from __future__ import annotations

import contextlib
import errno
import dataclasses
import fcntl
import importlib.util
import io
import json
import linecache
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_repair_rollout_reflinks.py"
DARWIN_BACKEND_PATH = REPO_ROOT / "scripts" / "codex_reflink_darwin.py"
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"


def load_repair_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "codex_repair_rollout_reflinks", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load repair module: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_module(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def walk_values(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from walk_values(child)


class FakeBackend:
    """Deterministic backend used to prove core control-flow without syscalls."""

    def __init__(self, repair: Any) -> None:
        self.repair = repair
        self.events: List[str] = []
        self.relations: Dict[str, Any] = {}
        self.policy_mismatches: set[str] = set()
        self.mirror_policy_overrides: Dict[str, Mapping[str, Any]] = {}
        self.nlinks: Dict[str, int] = {}
        self.inspect_errors: Dict[str, BaseException] = {}
        self.prepare_errors: Dict[str, BaseException] = {}
        self.prepare_source_parent_identity: Optional[Any] = None
        self.clone_error: Optional[BaseException] = None
        self.clone_errors: List[BaseException] = []
        self.clone_hook: Optional[Any] = None
        self.clone_policy_changed = False
        self.postverify_hook: Optional[Any] = None
        self.postverify_error: Optional[BaseException] = None
        self.swap_back_error: Optional[BaseException] = None
        self.resume_orientation: Optional[Any] = None
        self.resume_snapshot_mutations: List[tuple[str, str]] = []
        self.resume_sources: List[Optional[pathlib.Path]] = []
        self.resume_source_error: Optional[BaseException] = None
        self.intent_stage_disposition = "absent"
        self.intent_stage_error: Optional[BaseException] = None
        self.intent_stage_cleanup_hook: Optional[Any] = None
        self.recovered_intents: List[tuple[Any, pathlib.Path]] = []
        self.last_transaction: Optional[FakeTransaction] = None
        self.unlink_original_hook: Optional[Any] = None
        self.unlink_clone_hook: Optional[Any] = None
        self.abort_before_prepared_error: Optional[BaseException] = None
        self.before_authorize_hooks: Dict[str, Any] = {}
        self.during_authorize_hooks: Dict[str, Any] = {}
        self._next_inode = 100

    def authorize(self, callback: Any, action: str) -> None:
        hook = self.before_authorize_hooks.get(action)
        if hook is not None:
            hook()
        during_hook = self.during_authorize_hooks.get(action)
        if during_hook is None:
            callback(action)
        else:
            real_pread = self.repair.os.pread
            injected = False

            def racing_pread(descriptor: int, count: int, offset: int) -> bytes:
                nonlocal injected
                chunk = real_pread(descriptor, count, offset)
                if chunk and not injected:
                    injected = True
                    during_hook()
                return chunk

            with mock.patch.object(self.repair.os, "pread", side_effect=racing_pread):
                callback(action)
            if not injected:
                raise AssertionError("durable authorization did not read its fence")
        self.events.append(f"authorize:{action}")

    def _rollout_id(self, path: pathlib.Path) -> str:
        rollout_id = self.repair.rollout_id_from_name(path.name)
        if rollout_id is None:
            raise AssertionError(f"fake backend received a non-rollout path: {path}")
        return rollout_id

    def _snapshot(
        self,
        path: pathlib.Path,
        *,
        inode: int,
        policy_salt: str = "",
    ) -> Any:
        content = path.read_bytes()
        rollout_id = self._rollout_id(path)
        return self.repair.FileSnapshot(
            identity=self.repair.FileIdentity(device=7, inode=inode),
            size=len(content),
            mtime_ns=123456789,
            nlink=self.nlinks.get(rollout_id, 1),
            content_sha256=__import__("hashlib").sha256(content).hexdigest(),
            policy=self.repair.PolicyFingerprint(
                uid=os.geteuid(),
                gid=os.getegid(),
                mode=0o600,
                flags=0,
                acl_sha256=self.repair.EMPTY_ACL_SHA256,
                xattrs_sha256=f"xattrs{policy_salt}",
            ),
        )

    def inspect_pair(self, source: pathlib.Path, mirror: pathlib.Path) -> Any:
        rollout_id = self._rollout_id(source)
        self.events.append(f"inspect:{rollout_id}")
        error = self.inspect_errors.get(rollout_id)
        if error is not None:
            raise error
        source_snapshot = self._snapshot(source, inode=self._next_inode)
        self._next_inode += 1
        mirror_snapshot = self._snapshot(
            mirror,
            inode=self._next_inode,
            policy_salt="-mirror" if rollout_id in self.policy_mismatches else "",
        )
        override = self.mirror_policy_overrides.get(rollout_id)
        if override:
            mirror_snapshot = dataclasses.replace(
                mirror_snapshot,
                policy=dataclasses.replace(mirror_snapshot.policy, **override),
            )
        self._next_inode += 1
        relation = self.relations.get(rollout_id)
        if relation is None:
            if source.read_bytes() == mirror.read_bytes():
                relation = self.repair.ContentRelation.EXACT
            elif source.read_bytes().startswith(mirror.read_bytes()):
                relation = self.repair.ContentRelation.MIRROR_COMPLETE_PREFIX
            else:
                relation = self.repair.ContentRelation.DIFFERENT
        return self.repair.PairInspection(
            source=source_snapshot,
            mirror=mirror_snapshot,
            source_parent=self.repair.FileIdentity(device=7, inode=10),
            mirror_parent=self.repair.FileIdentity(device=7, inode=20),
            relation=relation,
        )

    def prepare(
        self,
        source: pathlib.Path,
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        expected: Any,
        authorize_state: Any,
    ) -> Any:
        self.events.append("prepare")
        rollout_id = self._rollout_id(source)
        error = self.prepare_errors.get(rollout_id)
        if error is not None:
            raise error
        if (
            self.prepare_source_parent_identity is not None
            and self.prepare_source_parent_identity != expected.source_parent
        ):
            raise self.repair.UnstablePathError(
                "source parent changed after pair inspection"
            )
        self.authorize(authorize_state, "create_stage")
        transaction = FakeTransaction(self, expected, authorize_state)
        self.last_transaction = transaction
        return transaction

    def resume(
        self,
        source: Optional[pathlib.Path],
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        manifest: Any,
        authorize_state: Any,
    ) -> Any:
        self.events.append("resume")
        self.resume_sources.append(source)
        if source is not None and self.resume_source_error is not None:
            raise self.resume_source_error
        transaction = FakeTransaction.from_manifest(self, manifest, authorize_state)
        if self.resume_orientation is not None:
            transaction.current_orientation = self.resume_orientation
        for target, property_name in self.resume_snapshot_mutations:
            snapshot = {
                "source": transaction.source_live,
                "original": transaction.original_live,
                "clone": transaction.clone_live,
            }[target]
            assert snapshot is not None
            if property_name == "content":
                changed = dataclasses.replace(snapshot, content_sha256="e" * 64)
            else:
                changed = dataclasses.replace(
                    snapshot,
                    policy=dataclasses.replace(
                        snapshot.policy,
                        acl_sha256=f"{snapshot.policy.acl_sha256}-changed",
                    ),
                )
            if target == "source":
                transaction.source_live = changed
            elif target == "original":
                transaction.original_live = changed
            else:
                transaction.clone_live = changed
        self.last_transaction = transaction
        return transaction

    def recover_intent_stage(
        self, intent: Any, temporary: pathlib.Path, authorize_state: Any
    ) -> str:
        self.events.append("recover_intent_stage")
        self.recovered_intents.append((intent, temporary))
        if self.intent_stage_error is not None:
            raise self.intent_stage_error
        if self.intent_stage_disposition == "removed-clone":
            self.authorize(authorize_state, "intent_unlink_clone")
            self.events.append("intent_unlink_clone")
        if self.intent_stage_disposition in {"removed-clone", "removed-empty"}:
            self.authorize(authorize_state, "intent_remove_stage")
            self.events.append("intent_remove_stage")
        if self.intent_stage_cleanup_hook is not None:
            self.intent_stage_cleanup_hook()
        return self.intent_stage_disposition


class FakeTransaction:
    def __init__(
        self, backend: FakeBackend, inspection: Any, authorize_state: Any
    ) -> None:
        self.backend = backend
        self.authorize_state = authorize_state
        self.inspection = inspection
        self.clone_value: Optional[Any] = None
        self.source_live = inspection.source
        self.original_live = inspection.mirror
        self.clone_live: Optional[Any] = None
        self.current_orientation = (
            backend.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING
        )
        self.prepared_durable = False
        self.closed = False

    @classmethod
    def from_manifest(
        cls, backend: FakeBackend, manifest: Any, authorize_state: Any
    ) -> "FakeTransaction":
        inspection = backend.repair.PairInspection(
            source=manifest.source_snapshot,
            mirror=manifest.original_snapshot,
            source_parent=backend.repair.FileIdentity(device=7, inode=10),
            mirror_parent=manifest.final_parent_identity,
            relation=backend.repair.ContentRelation.EXACT,
        )
        transaction = cls(backend, inspection, authorize_state)
        transaction.clone_value = manifest.clone_snapshot
        transaction.clone_live = manifest.clone_snapshot
        transaction.prepared_durable = True
        transaction.current_orientation = (
            backend.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        )
        return transaction

    def clone(self, expected_source: Any, expected_original: Any) -> Any:
        self.backend.authorize(self.authorize_state, "create_clone")
        self.backend.events.append("clone")
        if self.backend.clone_errors:
            raise self.backend.clone_errors.pop(0)
        if self.backend.clone_error is not None:
            raise self.backend.clone_error
        policy = expected_original.policy
        if self.backend.clone_policy_changed:
            policy = dataclasses.replace(
                policy, xattrs_sha256=f"{policy.xattrs_sha256}-changed"
            )
        self.clone_value = self.backend.repair.FileSnapshot(
            identity=self.backend.repair.FileIdentity(
                device=expected_original.identity.device,
                inode=expected_original.identity.inode + 10000,
            ),
            size=expected_original.size,
            mtime_ns=expected_original.mtime_ns,
            nlink=1,
            content_sha256=expected_original.content_sha256,
            policy=policy,
        )
        self.current_orientation = (
            self.backend.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        )
        self.clone_live = self.clone_value
        if self.backend.clone_hook is not None:
            self.backend.clone_hook()
        return self.clone_value

    def mark_prepared(self) -> None:
        self.prepared_durable = True
        self.backend.events.append("mark_prepared")

    def revalidate_before_prepared(
        self, expected_source: Any, expected_original: Any, expected_clone: Any
    ) -> None:
        self.backend.events.append("revalidate_before_prepared")
        if self.source_live != expected_source:
            raise self.backend.repair.UnstablePathError(
                "source protected snapshot changed before PREPARED"
            )
        if self.original_live != expected_original or self.clone_live != expected_clone:
            raise self.backend.repair.UnstablePathError(
                "mirror protected snapshot changed before PREPARED"
            )

    def parent_identities(self) -> Any:
        self.backend.events.append("parent_identities")
        return (
            self.inspection.mirror_parent,
            self.backend.repair.FileIdentity(device=7, inode=30),
        )

    def orientation(self, expected_original: Any, expected_clone: Any) -> Any:
        self.backend.events.append(f"orientation:{self.current_orientation.value}")
        return self.current_orientation

    def swap_forward(self, expected_original: Any, expected_clone: Any) -> None:
        self.backend.authorize(self.authorize_state, "swap_forward")
        self.backend.events.append("swap_forward")
        self.current_orientation = (
            self.backend.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP
        )

    def revalidate_pre_forward(
        self, expected_source: Any, expected_original: Any, expected_clone: Any
    ) -> None:
        self.backend.events.append("revalidate_pre_forward")
        if self.source_live != expected_source:
            raise self.backend.repair.UnstablePathError(
                "source protected snapshot changed before forward swap"
            )
        if self.original_live != expected_original or self.clone_live != expected_clone:
            raise self.backend.repair.SafetyError(
                "mirror protected snapshot changed before forward swap"
            )

    def postverify(self, expected_source: Any, expected_clone: Any) -> Any:
        self.backend.events.append("postverify")
        if self.backend.postverify_hook is not None:
            self.backend.postverify_hook()
        if self.backend.postverify_error is not None:
            raise self.backend.postverify_error
        return expected_clone

    def revalidate_forward(self, expected_original: Any, expected_clone: Any) -> None:
        self.backend.events.append("revalidate_forward")
        if self.original_live != expected_original or self.clone_live != expected_clone:
            raise self.backend.repair.SafetyError(
                "same-inode protected snapshot changed before forward commit"
            )

    def revalidate_before_cleanup(
        self, expected_original: Any, expected_clone: Any
    ) -> None:
        self.backend.events.append("revalidate_before_cleanup")
        if self.original_live != expected_original or self.clone_live != expected_clone:
            raise self.backend.repair.SafetyError(
                "same-inode protected snapshot changed before cleanup"
            )

    def revalidate_committed(self, expected_clone: Any) -> None:
        self.backend.events.append("revalidate_committed")
        if self.clone_live != expected_clone:
            raise self.backend.repair.SafetyError(
                "committed clone protected snapshot changed"
            )

    def revalidate_rolled_back(self, expected_original: Any) -> None:
        self.backend.events.append("revalidate_rolled_back")
        if self.original_live != expected_original:
            raise self.backend.repair.SafetyError(
                "rolled-back original protected snapshot changed"
            )

    def unlink_original(self, expected_original: Any, expected_clone: Any) -> None:
        self.backend.authorize(self.authorize_state, "unlink_original")
        self.backend.events.append("unlink_original")
        self.current_orientation = (
            self.backend.repair.Orientation.CLONE_FINAL_TEMP_MISSING
        )
        if self.backend.unlink_original_hook is not None:
            self.backend.unlink_original_hook(self)

    def swap_back(self, expected_clone: Any, expected_original: Any) -> None:
        self.backend.authorize(self.authorize_state, "swap_back")
        self.backend.events.append("swap_back")
        if self.backend.swap_back_error is not None:
            raise self.backend.swap_back_error
        self.current_orientation = (
            self.backend.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        )

    def unlink_clone(self, expected_clone: Any, expected_original: Any) -> None:
        self.backend.authorize(self.authorize_state, "unlink_clone")
        self.backend.events.append("unlink_clone")
        self.current_orientation = (
            self.backend.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING
        )
        if self.backend.unlink_clone_hook is not None:
            self.backend.unlink_clone_hook(self)

    def sync_namespaces(self) -> None:
        self.backend.events.append("sync_namespaces")

    def cleanup_stage(self) -> None:
        self.backend.authorize(self.authorize_state, "remove_stage")
        self.backend.events.append("cleanup_stage")

    def abort_before_prepared(self) -> None:
        self.backend.events.append("abort_before_prepared")
        if self.backend.abort_before_prepared_error is not None:
            error = self.backend.abort_before_prepared_error
            self.backend.abort_before_prepared_error = None
            self.clone_live = None
            self.current_orientation = (
                self.backend.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING
            )
            self.backend.events.append("unlink_clone")
            raise error
        if self.clone_live is not None:
            self.backend.authorize(self.authorize_state, "unlink_clone")
        self.backend.authorize(self.authorize_state, "remove_stage")

    def close(self) -> None:
        self.backend.events.append("close")
        self.closed = True


class TrackingRawAdapterTransaction:
    """Minimal raw transaction used to test adapter ownership handoff."""

    def __init__(
        self,
        *,
        source_snapshot: Any = None,
        original_snapshot: Any = None,
        source_parent_identity: Any = None,
        destination_parent_identity: Any = None,
        temporary_parent_identity: Any = None,
        stage_path: Optional[pathlib.Path] = None,
        fd_count: int = 5,
    ) -> None:
        self._source_snapshot = source_snapshot
        self._original_snapshot = original_snapshot
        self.source_parent_identity = source_parent_identity
        self.destination_parent_identity = destination_parent_identity
        self.temporary_parent_identity = temporary_parent_identity
        self.stage_path = stage_path
        self.original_fds = tuple(
            os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            for _index in range(fd_count)
        )
        self.live_fds = list(self.original_fds)
        self.close_counts = {descriptor: 0 for descriptor in self.original_fds}
        self.close_primaries: List[Optional[BaseException]] = []
        self.abort_calls = 0

    @property
    def closed(self) -> bool:
        return all(descriptor < 0 for descriptor in self.live_fds)

    def source_snapshot(self) -> Any:
        return self._source_snapshot

    def original_snapshot(self) -> Any:
        return self._original_snapshot

    def abort_before_prepared(self, _expected: Any, *, authorize_state: Any) -> None:
        self.abort_calls += 1
        authorize_state("remove_stage")
        if self.stage_path is not None and self.stage_path.exists():
            self.stage_path.rmdir()

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        if self.closed:
            return
        self.close_primaries.append(primary_error)
        for index, descriptor in enumerate(self.live_fds):
            if descriptor < 0:
                continue
            self.live_fds[index] = -1
            os.close(descriptor)
            self.close_counts[descriptor] += 1


class TrackingAdapterTransactionOwner:
    """Lazy test owner matching the Darwin transaction handoff contract."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._transaction: Optional[TrackingRawAdapterTransaction] = None
        self._entered = False
        self._retain_if_registered: Optional[Any] = None
        self.acquired_transactions: List[TrackingRawAdapterTransaction] = []

    @property
    def closed(self) -> bool:
        return self._transaction is None

    @property
    def acquired(self) -> bool:
        return bool(self.acquired_transactions)

    def transaction(self) -> TrackingRawAdapterTransaction:
        transaction = self._transaction
        if transaction is None:
            raise RuntimeError("tracking transaction owner is closed")
        return transaction

    def retain_if_registered(self, predicate: Any) -> "TrackingAdapterTransactionOwner":
        if not callable(predicate):
            raise TypeError("tracking registration predicate must be callable")
        self._retain_if_registered = predicate
        return self

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        self._transaction = None
        transaction.close(primary_error=primary_error)

    def __enter__(self) -> "TrackingAdapterTransactionOwner":
        if self._entered:
            raise RuntimeError("tracking transaction owner was re-entered")
        self._entered = True
        transaction = self._factory()
        self._transaction = transaction
        self.acquired_transactions.append(transaction)
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, _traceback: Any) -> None:
        predicate = self._retain_if_registered
        self._retain_if_registered = None
        if exc_type is None:
            return
        if predicate is not None and predicate():
            return
        self.close(
            primary_error=(exc_value if isinstance(exc_value, BaseException) else None)
        )


class FilesystemFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.repair = load_repair_module()
        resolved_temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="codex-reflink-repair-test-", dir=str(resolved_temp_root)
        )
        self.root = pathlib.Path(self._temporary_directory.name)
        self.source_root = self.root / "codex"
        self.mirror_root = self.root / "mirror"
        self.state_root = self.root / "state"
        self.source_root.mkdir()
        self.mirror_root.mkdir()
        self.state_root.mkdir()

    def config(self) -> Any:
        return self.repair.Config(
            codex_root=self.source_root,
            mirror_root=self.mirror_root,
            state_root=self.state_root / "reflink-repair",
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def rollout_path(
        self,
        root: pathlib.Path,
        state: str,
        rollout_id: str,
        *,
        leaf: Optional[str] = None,
    ) -> pathlib.Path:
        name = leaf or f"rollout-2026-08-17T00-00-00-{rollout_id}.jsonl"
        path = root / state / "2026" / "08" / "17" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_rollout(
        self,
        root: pathlib.Path,
        state: str,
        rollout_id: str,
        content: bytes = b'{"type":"event"}\n',
        *,
        leaf: Optional[str] = None,
        mode: int = 0o600,
    ) -> pathlib.Path:
        path = self.rollout_path(root, state, rollout_id, leaf=leaf)
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def replace_parent_preserving_leaf(
        self, path: pathlib.Path, *, suffix: str
    ) -> pathlib.Path:
        parent = path.parent
        parent_before = parent.stat()
        leaf_before = path.stat()
        moved = parent.with_name(f"{parent.name}.{suffix}")
        os.replace(parent, moved)
        parent.mkdir(mode=stat.S_IMODE(parent_before.st_mode))
        os.replace(moved / path.name, path)
        leaf_after = path.stat()
        self.assertNotEqual(parent_before.st_ino, parent.stat().st_ino)
        self.assertEqual(
            (leaf_before.st_dev, leaf_before.st_ino),
            (
                leaf_after.st_dev,
                leaf_after.st_ino,
            ),
        )
        self.assertEqual(1, leaf_after.st_nlink)
        return moved

    def remap_private_state_record(
        self,
        config: Any,
        path: pathlib.Path,
        *,
        scope: str,
        suffix: str,
    ) -> pathlib.Path:
        original = path.read_bytes()
        if scope == "leaf":
            moved = path.with_name(f"{path.name}.{suffix}")
            os.replace(path, moved)
        else:
            target = config.state_root if scope == "root" else path.parent
            moved = target.with_name(f"{target.name}.{suffix}")
            os.replace(target, moved)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_bytes(original)
        path.chmod(0o600)
        if scope == "root":
            old_path = moved / path.relative_to(config.state_root)
        elif scope == "child":
            old_path = moved / path.name
        else:
            old_path = moved
        self.assertEqual(original, old_path.read_bytes())
        self.assertNotEqual(old_path.stat().st_ino, path.stat().st_ino)
        return old_path

    def cli(
        self,
        *arguments: str,
        python: str = sys.executable,
        env_overrides: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_ROOT": str(self.source_root),
                "CODEX_MIRROR_ROOT": str(self.mirror_root),
                "CODEX_BACKUP_STATE_ROOT": str(self.state_root),
            }
        )
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [python, str(SCRIPT_PATH), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def json_receipt(
        self, completed: subprocess.CompletedProcess[str]
    ) -> Dict[str, Any]:
        self.assertNotEqual("", completed.stdout.strip(), msg=completed.stderr)
        try:
            receipt = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"stdout is not one JSON receipt: {error}\n"
                f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
            )
        self.assertIsInstance(receipt, dict)
        return receipt

    def results_by_id(self, receipt: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
        return {
            str(result["rollout_id"]): result
            for result in receipt["results"]
            if result.get("rollout_id") is not None
        }

    def assert_trace_interruption(
        self,
        target_code: Any,
        predicate: Any,
        operation: Any,
        *,
        label: str,
        events: Tuple[str, ...] = ("line",),
        unwrap_caught: Optional[Any] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Any:
        primary = KeyboardInterrupt(label)
        previous_trace = sys.gettrace()
        if evidence is None:
            evidence = {}
        evidence.update({"fired": False, "primary": primary})

        def raise_primary() -> None:
            try:
                raise primary
            except BaseException as error:
                evidence["origin_traceback"] = error.__traceback__
                raise

        def tracer(frame: Any, event: str, _argument: Any) -> Any:
            if (
                event in events
                and frame.f_code is target_code
                and not evidence["fired"]
                and predicate(frame, evidence)
            ):
                evidence["fired"] = True
                evidence["event"] = event
                sys.settrace(None)
                raise_primary()
            return tracer

        caught: Optional[BaseException] = None
        sys.settrace(tracer)
        try:
            operation()
        except BaseException as error:
            caught = error
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(evidence["fired"], f"trace boundary did not fire: {label}")
        observed_primary = caught if unwrap_caught is None else unwrap_caught(caught)
        self.assertIs(primary, observed_primary)
        origin_traceback = evidence.get("origin_traceback")
        self.assertIsNotNone(origin_traceback)
        traceback = (
            observed_primary.__traceback__
            if isinstance(observed_primary, BaseException)
            else None
        )
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(origin_traceback, traceback_nodes)
        evidence["caught"] = caught
        return evidence

    def capture_owned_descriptor(
        self,
        evidence: Dict[str, Any],
        owner: Any,
        *,
        descriptor: Optional[int] = None,
    ) -> None:
        captured = owner.fileno() if descriptor is None else descriptor
        metadata = os.fstat(captured)
        evidence["descriptor_owner"] = owner
        evidence["descriptor_state"] = owner._state
        evidence["descriptor"] = captured
        evidence["descriptor_identity"] = (metadata.st_dev, metadata.st_ino)

    def assert_cleanup_trace_preserves_primary(
        self,
        target_code: Any,
        predicate: Any,
        operation: Any,
        body_primary: BaseException,
        *,
        label: str,
        events: Tuple[str, ...] = ("call",),
        evidence: Optional[Dict[str, Any]] = None,
        unwrap_caught: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if evidence is None:
            evidence = {}
        cleanup_interrupt = KeyboardInterrupt(f"{label} cleanup")
        evidence.update(
            {
                "cleanup_fired": False,
                "cleanup_interrupt": cleanup_interrupt,
            }
        )
        previous_trace = sys.gettrace()

        def raise_cleanup_interrupt() -> None:
            try:
                raise cleanup_interrupt
            except BaseException as error:
                evidence["cleanup_origin_traceback"] = error.__traceback__
                raise

        def tracer(frame: Any, event: str, _argument: Any) -> Any:
            if (
                event in events
                and frame.f_code is target_code
                and not evidence["cleanup_fired"]
                and predicate(frame, evidence)
            ):
                evidence["cleanup_fired"] = True
                evidence["cleanup_event"] = event
                sys.settrace(None)
                raise_cleanup_interrupt()
            return tracer

        caught: Optional[BaseException] = None
        sys.settrace(tracer)
        try:
            operation()
        except BaseException as error:
            caught = error
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(
            evidence["cleanup_fired"], f"cleanup trace did not fire: {label}"
        )
        observed_primary = caught if unwrap_caught is None else unwrap_caught(caught)
        self.assertIs(body_primary, observed_primary)
        body_origin = evidence.get("body_origin_traceback")
        self.assertIsNotNone(body_origin)
        body_traceback = (
            observed_primary.__traceback__
            if isinstance(observed_primary, BaseException)
            else None
        )
        body_nodes: List[Any] = []
        while body_traceback is not None:
            body_nodes.append(body_traceback)
            body_traceback = body_traceback.tb_next
        self.assertIn(body_origin, body_nodes)
        cleanup_origin = evidence.get("cleanup_origin_traceback")
        self.assertIsNotNone(cleanup_origin)
        cleanup_traceback = cleanup_interrupt.__traceback__
        cleanup_nodes: List[Any] = []
        while cleanup_traceback is not None:
            cleanup_nodes.append(cleanup_traceback)
            cleanup_traceback = cleanup_traceback.tb_next
        self.assertIn(cleanup_origin, cleanup_nodes)
        evidence["caught"] = caught
        return evidence

    def assert_cleanup_trace_allows_completion(
        self,
        target_code: Any,
        predicate: Any,
        operation: Any,
        *,
        label: str,
        events: Tuple[str, ...] = ("line",),
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if evidence is None:
            evidence = {}
        cleanup_interrupt = KeyboardInterrupt(f"{label} cleanup")
        evidence.update(
            {
                "cleanup_fired": False,
                "cleanup_interrupt": cleanup_interrupt,
            }
        )
        previous_trace = sys.gettrace()

        def raise_cleanup_interrupt() -> None:
            try:
                raise cleanup_interrupt
            except BaseException as error:
                evidence["cleanup_origin_traceback"] = error.__traceback__
                raise

        def tracer(frame: Any, event: str, _argument: Any) -> Any:
            if (
                event in events
                and frame.f_code is target_code
                and not evidence["cleanup_fired"]
                and predicate(frame, evidence)
            ):
                evidence["cleanup_fired"] = True
                evidence["cleanup_event"] = event
                sys.settrace(None)
                raise_cleanup_interrupt()
            return tracer

        caught: Optional[BaseException] = None
        result: Any = None
        sys.settrace(tracer)
        try:
            result = operation()
        except BaseException as error:
            caught = error
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(
            evidence["cleanup_fired"], f"cleanup trace did not fire: {label}"
        )
        self.assertIsNone(
            caught,
            f"cleanup trace replaced the handled primary: {caught!r}",
        )
        body_primary = evidence.get("body_primary")
        body_origin = evidence.get("body_origin_traceback")
        self.assertIsInstance(body_primary, BaseException)
        self.assertIsNotNone(body_origin)
        body_traceback = body_primary.__traceback__
        body_nodes: List[Any] = []
        while body_traceback is not None:
            body_nodes.append(body_traceback)
            body_traceback = body_traceback.tb_next
        self.assertIn(body_origin, body_nodes)
        cleanup_origin = evidence.get("cleanup_origin_traceback")
        self.assertIsNotNone(cleanup_origin)
        cleanup_traceback = cleanup_interrupt.__traceback__
        cleanup_nodes: List[Any] = []
        while cleanup_traceback is not None:
            cleanup_nodes.append(cleanup_traceback)
            cleanup_traceback = cleanup_traceback.tb_next
        self.assertIn(cleanup_origin, cleanup_nodes)
        evidence["result"] = result
        return evidence

    @contextlib.contextmanager
    def record_captured_descriptor_closes(
        self, evidence: Dict[str, Any]
    ) -> Iterator[None]:
        real_state_close = self.repair._DescriptorState.close
        real_close = self.repair.os.close
        active_state: Optional[Any] = None
        evidence.setdefault("state_close_entries", [])
        evidence.setdefault("descriptor_close_calls", [])
        evidence.setdefault("descriptor_reuse", [])

        def recording_state_close(state: Any, *arguments: Any, **keywords: Any) -> Any:
            nonlocal active_state
            target_state = evidence.get("descriptor_state")
            if state is not target_state or state.descriptor < 0:
                return real_state_close(state, *arguments, **keywords)
            evidence["state_close_entries"].append(state.descriptor)
            previous_state = active_state
            active_state = state
            try:
                return real_state_close(state, *arguments, **keywords)
            finally:
                active_state = previous_state

        def recording_close(descriptor: int) -> None:
            if active_state is not None and active_state is evidence.get(
                "descriptor_state"
            ):
                metadata = os.fstat(descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
                if descriptor != evidence.get("descriptor") or identity != evidence.get(
                    "descriptor_identity"
                ):
                    evidence["descriptor_reuse"].append((descriptor, identity))
                evidence["descriptor_close_calls"].append(descriptor)
            real_close(descriptor)

        with (
            mock.patch.object(
                self.repair._DescriptorState,
                "close",
                autospec=True,
                side_effect=recording_state_close,
            ),
            mock.patch.object(self.repair.os, "close", side_effect=recording_close),
        ):
            yield

    def assert_captured_descriptor_closed_once(
        self, evidence: Mapping[str, Any]
    ) -> None:
        owner = evidence["descriptor_owner"]
        state = evidence["descriptor_state"]
        descriptor = evidence["descriptor"]
        self.assertTrue(owner.closed)
        self.assertEqual(-1, state.descriptor)
        self.assertEqual([descriptor], evidence["state_close_entries"])
        self.assertEqual([descriptor], evidence["descriptor_close_calls"])
        self.assertEqual([], evidence["descriptor_reuse"])
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)


class RolloutIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repair = load_repair_module()

    def test_filename_uses_only_the_canonical_uuid_suffix(self) -> None:
        self.assertEqual(
            UUID_A,
            self.repair.rollout_id_from_name(
                f"rollout-2026-08-17T00-00-00-{UUID_A}.jsonl"
            ),
        )
        self.assertIsNone(
            self.repair.rollout_id_from_name(
                f"rollout-2026-08-17T00-00-00-{UUID_A}.jsonl.backup"
            )
        )
        self.assertIsNone(self.repair.rollout_id_from_name("rollout-not-a-uuid.jsonl"))

    def test_rollout_id_normalizes_uppercase_but_rejects_noncanonical_values(
        self,
    ) -> None:
        self.assertEqual(UUID_A, self.repair.canonical_rollout_id(UUID_A.upper()))
        for invalid in ("", UUID_A + "-suffix", "11111111111141118111111111111111"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.repair.canonical_rollout_id(invalid)


class DiscoveryTests(FilesystemFixture):
    def test_pairs_by_uuid_across_live_and_archived_states(self) -> None:
        source = self.write_rollout(self.source_root, "sessions", UUID_A)
        mirror = self.write_rollout(self.mirror_root, "archived_sessions", UUID_A)

        discovery = self.repair.discover_candidates(self.config())

        self.assertEqual([UUID_A], sorted(discovery.candidates))
        candidate = discovery.candidates[UUID_A]
        self.assertEqual(source, candidate.source)
        self.assertEqual(mirror, candidate.mirror)
        self.assertTrue(candidate.source_rel.startswith("sessions/"))
        self.assertTrue(candidate.mirror_rel.startswith("archived_sessions/"))

    def test_duplicates_on_each_side_are_ambiguous_and_never_candidates(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A)
        self.write_rollout(self.source_root, "archived_sessions", UUID_A)
        self.write_rollout(self.mirror_root, "sessions", UUID_A)
        self.write_rollout(self.source_root, "sessions", UUID_B)
        self.write_rollout(self.mirror_root, "sessions", UUID_B)
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_B)

        discovery = self.repair.discover_candidates(self.config())

        self.assertNotIn(UUID_A, discovery.candidates)
        self.assertNotIn(UUID_B, discovery.candidates)
        self.assertEqual(2, len(discovery.source_duplicates[UUID_A]))
        self.assertEqual(2, len(discovery.mirror_duplicates[UUID_B]))

    def test_source_only_mirror_only_and_noncanonical_are_distinct(self) -> None:
        source = self.write_rollout(self.source_root, "sessions", UUID_A)
        mirror = self.write_rollout(self.mirror_root, "archived_sessions", UUID_B)
        malformed = self.source_root / "sessions" / "rollout-not-a-uuid.jsonl"
        malformed.write_bytes(b"{}\n")

        discovery = self.repair.discover_candidates(self.config())

        self.assertEqual(
            str(source.relative_to(self.source_root)), discovery.source_only[UUID_A]
        )
        self.assertEqual(
            str(mirror.relative_to(self.mirror_root)), discovery.mirror_only[UUID_B]
        )
        self.assertEqual(
            "noncanonical-name", discovery.noncanonical[0]["classification"]
        )
        self.assertEqual({}, discovery.candidates)

    def test_top_level_source_and_mirror_root_symlinks_are_scan_errors(self) -> None:
        outside_source = self.root / "outside-source"
        outside_mirror = self.root / "outside-mirror"
        self.write_rollout(outside_source, "sessions", UUID_A, b"same\n")
        self.write_rollout(outside_mirror, "archived_sessions", UUID_A, b"same\n")
        source_link = self.root / "source-link"
        mirror_link = self.root / "mirror-link"
        os.symlink(outside_source, source_link)
        os.symlink(outside_mirror, mirror_link)
        config = dataclasses.replace(
            self.config(), codex_root=source_link, mirror_root=mirror_link
        )

        discovery = self.repair.discover_candidates(config)

        self.assertEqual({}, discovery.candidates)
        self.assertEqual(
            {"source", "mirror"},
            {item["side"] for item in discovery.scan_errors},
        )
        self.assertEqual(
            {str(source_link), str(mirror_link)},
            {item["path"] for item in discovery.scan_errors},
        )

    def test_top_level_root_identity_change_is_a_scan_error_on_both_sides(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"same\n")
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"same\n")
        real_lstat = self.repair.os.lstat
        root_calls = {self.source_root: 0, self.mirror_root: 0}

        def changing_lstat(path: Any) -> os.stat_result:
            result = real_lstat(path)
            candidate = pathlib.Path(path)
            if candidate in root_calls:
                root_calls[candidate] += 1
                if root_calls[candidate] == 2:
                    fields = list(result)
                    fields[1] += 10000
                    return os.stat_result(fields)
            return result

        with mock.patch.object(self.repair.os, "lstat", side_effect=changing_lstat):
            discovery = self.repair.discover_candidates(self.config())

        self.assertEqual({self.source_root: 2, self.mirror_root: 2}, root_calls)
        self.assertEqual(
            {"source", "mirror"},
            {
                item["side"]
                for item in discovery.scan_errors
                if "identity changed" in item["error"]
            },
        )

    def test_state_root_regular_file_is_a_scan_error(self) -> None:
        state_root = self.source_root / "sessions"
        state_root.write_bytes(b"not a directory")

        discovery = self.repair.discover_candidates(self.config())

        self.assertEqual({}, discovery.candidates)
        self.assertEqual(1, len(discovery.scan_errors))
        self.assertEqual("source", discovery.scan_errors[0]["side"])
        self.assertEqual(str(state_root), discovery.scan_errors[0]["path"])


class DurableStateTests(FilesystemFixture):
    @staticmethod
    def open_fd_set(limit: int = 512) -> set[int]:
        descriptors: set[int] = set()
        for descriptor in range(limit):
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                descriptors.add(descriptor)
        return descriptors

    def test_descriptor_state_pre_disarm_trace_second_drains(self) -> None:
        baseline = self.open_fd_set()
        descriptor = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        owner = self.repair._OwnedDescriptor("descriptor close trace")
        owner._adopt(descriptor)
        body_primary = RuntimeError("descriptor close body primary")
        evidence: Dict[str, Any] = {}
        self.capture_owned_descriptor(evidence, owner)
        state_close_code = self.repair._DescriptorState._close_once.__code__

        def pre_disarm_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            source = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            receipt = frame.f_locals.get("receipt")
            if (
                frame.f_locals.get("self") is not owner._state
                or receipt is not owner._state.close_receipt
                or receipt is None
                or receipt.descriptor != descriptor
                or receipt.disposition
                is not self.repair._DescriptorCloseDisposition.OWNED_PENDING
                or receipt.dispatches != 0
                or owner._state.descriptor != descriptor
                or "receipt.attempt = _DescriptorCloseAttempt(" not in source
            ):
                return False
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["close_line"] = frame.f_lineno
            return True

        def operation() -> None:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                owner.close(primary_error=error)
                raise

        closed_before_fallback = False
        state_entries_before_fallback: tuple[int, ...] = ()
        close_calls_before_fallback: tuple[int, ...] = ()
        with self.record_captured_descriptor_closes(evidence):
            try:
                self.assert_cleanup_trace_preserves_primary(
                    state_close_code,
                    pre_disarm_boundary,
                    operation,
                    body_primary,
                    label="descriptor state pre-disarm",
                    events=("line",),
                    evidence=evidence,
                )
                closed_before_fallback = owner.closed
                state_entries_before_fallback = tuple(evidence["state_close_entries"])
                close_calls_before_fallback = tuple(evidence["descriptor_close_calls"])
            finally:
                owner.close(primary_error=body_primary)

        self.assertIsInstance(evidence["close_line"], int)
        self.assertTrue(closed_before_fallback)
        self.assertEqual((descriptor,), state_entries_before_fallback)
        self.assertEqual((descriptor,), close_calls_before_fallback)
        self.assertEqual([], evidence["descriptor_reuse"])
        self.assertEqual(-1, owner._state.descriptor)
        self.assertEqual(baseline, self.open_fd_set())

    def test_descriptor_state_close_receipt_survives_pre_syscall_interrupt(
        self,
    ) -> None:
        baseline = self.open_fd_set()
        descriptor = os.open(
            os.devnull,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        state = self.repair._DescriptorState(
            "descriptor provisional close receipt",
            descriptor,
        )
        evidence: Dict[str, Any] = {}
        physical_closes: List[int] = []
        real_close = self.repair.os.close

        def record_close(candidate: int) -> None:
            if candidate != descriptor:
                real_close(candidate)
                return
            physical_closes.append(candidate)
            real_close(candidate)

        def pre_dispatch_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            receipt = frame.f_locals.get("receipt")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "receipt.attempt = _DescriptorCloseAttempt("
                or frame.f_locals.get("self") is not state
                or receipt is not state.close_receipt
                or receipt is None
                or receipt.descriptor != descriptor
                or receipt.disposition
                is not self.repair._DescriptorCloseDisposition.OWNED_PENDING
                or receipt.dispatches != 0
                or receipt.error is not None
                or state.descriptor != descriptor
            ):
                return False
            captured["receipt"] = receipt
            captured["dispatch_line"] = frame.f_lineno
            return True

        try:
            with (
                mock.patch.object(
                    self.repair.os,
                    "close",
                    side_effect=record_close,
                ),
                mock.patch.object(
                    self.repair._DescriptorState,
                    "__del__",
                    autospec=True,
                ) as destructor,
            ):
                self.assert_trace_interruption(
                    self.repair._DescriptorState._close_once.__code__,
                    pre_dispatch_boundary,
                    state.close,
                    label="descriptor pre-syscall receipt",
                    events=("line",),
                    evidence=evidence,
                )
                self.assertEqual(0, destructor.call_count)

            receipt = state.close_receipt
            self.assertIsInstance(receipt, self.repair._DescriptorCloseReceipt)
            assert receipt is not None
            self.assertIs(receipt, evidence["receipt"])
            self.assertIsInstance(evidence["dispatch_line"], int)
            self.assertEqual(1, receipt.dispatches)
            self.assertTrue(receipt.completed)
            self.assertIs(
                self.repair._DescriptorCloseDisposition.CLOSED_PROVED,
                receipt.disposition,
            )
            self.assertIs(evidence["primary"], receipt.error)
            self.assertEqual(-1, state.descriptor)
            self.assertEqual([descriptor], physical_closes)
            state.close()
            self.assertEqual([descriptor], physical_closes)
            with self.assertRaises(OSError) as closed:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            self.assertEqual(errno.EBADF, closed.exception.errno)
            self.assertEqual(baseline, self.open_fd_set())
        finally:
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                real_close(descriptor)

    def test_descriptor_state_body_primary_keeps_secondary_close_evidence(
        self,
    ) -> None:
        baseline = self.open_fd_set()
        descriptor = os.open(
            os.devnull,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        subject = "descriptor body-primary cleanup"
        state = self.repair._DescriptorState(subject, descriptor)
        body_primary = RuntimeError("descriptor body primary")
        close_error = KeyboardInterrupt("descriptor secondary close failure")
        evidence: Dict[str, Any] = {}
        close_attempts: List[int] = []
        physical_closes: List[int] = []
        real_close = self.repair.os.close

        def close_then_fail(candidate: int) -> None:
            if candidate != descriptor:
                real_close(candidate)
                return
            close_attempts.append(candidate)
            if len(close_attempts) == 1:
                real_close(candidate)
                physical_closes.append(candidate)
                try:
                    raise close_error
                except BaseException:
                    raise
            real_close(candidate)

        caught: Optional[BaseException] = None
        with (
            mock.patch.object(
                self.repair.os,
                "close",
                side_effect=close_then_fail,
            ),
            mock.patch.object(
                self.repair._DescriptorState,
                "__del__",
                autospec=True,
            ) as destructor,
        ):
            try:
                try:
                    raise body_primary
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    state.close(primary_error=error)
                    raise
            except BaseException as error:
                caught = error
            self.assertEqual(0, destructor.call_count)

        self.assertIs(body_primary, caught)
        traceback = caught.__traceback__ if caught is not None else None
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(evidence["body_origin_traceback"], traceback_nodes)
        receipt = state.close_receipt
        self.assertIsInstance(receipt, self.repair._DescriptorCloseReceipt)
        assert receipt is not None
        self.assertEqual(1, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIs(
            self.repair._DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED,
            receipt.disposition,
        )
        self.assertIs(close_error, receipt.error)
        diagnostics = getattr(body_primary, "_cleanup_diagnostics", ())
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIsInstance(diagnostic, self.repair._CleanupDiagnostic)
        self.assertIs(close_error, diagnostic.error)
        self.assertIs(receipt, diagnostic.receipt)
        self.assertIn(subject, diagnostic.detail)
        self.assertIn(str(close_error), diagnostic.detail)
        add_note = getattr(body_primary, "add_note", None)
        if callable(add_note):
            self.assertIn(diagnostic.detail, getattr(body_primary, "__notes__", ()))
        self.assertEqual(-1, state.descriptor)
        self.assertEqual([descriptor], close_attempts)
        self.assertEqual([descriptor], physical_closes)
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_repair_lock_failure_revalidation_is_secondary_to_body_primary(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "lock-failure-revalidation",
        )
        baseline = self.open_fd_set()
        body_primary = KeyboardInterrupt("repair lock body primary")
        verification_error = OSError(
            errno.ESTALE,
            "repair lock failure-path revalidation failed",
        )
        evidence: Dict[str, Any] = {}
        body_active = False
        real_revalidate = self.repair.StateDirectory.revalidate

        def fail_failure_path_revalidation(state: Any) -> None:
            if not body_active:
                real_revalidate(state)
                return
            try:
                raise verification_error
            except BaseException as error:
                evidence["verification_origin_traceback"] = error.__traceback__
                raise

        caught: Optional[BaseException] = None
        with mock.patch.object(
            self.repair.StateDirectory,
            "revalidate",
            autospec=True,
            side_effect=fail_failure_path_revalidation,
        ):
            try:
                with self.repair.repair_lock(config):
                    try:
                        body_active = True
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise
            except BaseException as error:
                caught = error

        self.assertIs(body_primary, caught)
        self.assertIs(type(body_primary), type(caught))
        traceback = caught.__traceback__ if caught is not None else None
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(evidence["body_origin_traceback"], traceback_nodes)
        diagnostics = getattr(body_primary, "_cleanup_diagnostics", ())
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIsInstance(diagnostic, self.repair._CleanupDiagnostic)
        self.assertIs(verification_error, diagnostic.error)
        self.assertIsNone(diagnostic.receipt)
        self.assertIn(
            "repair lock namespace changed while handling failure",
            diagnostic.detail,
        )
        self.assertIn(str(verification_error), diagnostic.detail)
        add_note = getattr(body_primary, "add_note", None)
        if callable(add_note):
            self.assertIn(diagnostic.detail, getattr(body_primary, "__notes__", ()))
        self.assertIsNotNone(evidence["verification_origin_traceback"])
        self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
        self.assertIsNone(self.repair._active_state_root())
        self.assertEqual(baseline, self.open_fd_set())
        with self.repair.repair_lock(config):
            pass

    def test_descriptor_close_does_not_follow_reused_fd_number(self) -> None:
        original_path = self.root / "descriptor-original"
        replacement_path = self.root / "descriptor-replacement"
        original_path.write_bytes(b"original")
        replacement_path.write_bytes(b"replacement")
        baseline = self.open_fd_set()
        descriptor = os.open(
            original_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        replacement_source = os.open(
            replacement_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        original_metadata = os.fstat(descriptor)
        replacement_metadata = os.fstat(replacement_source)
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        replacement_identity = (
            replacement_metadata.st_dev,
            replacement_metadata.st_ino,
        )
        self.assertNotEqual(original_identity, replacement_identity)
        state = self.repair._DescriptorState(
            "descriptor reused-number close",
            descriptor,
        )
        close_error = OSError(errno.EIO, "descriptor close outcome unproved")
        evidence: Dict[str, Any] = {}
        close_attempts: List[int] = []
        physical_closes: List[int] = []
        unexpected_reused_closes: List[int] = []
        real_close = self.repair.os.close

        def close_reuse_then_fail(candidate: int) -> None:
            if candidate != descriptor:
                real_close(candidate)
                return
            close_attempts.append(candidate)
            if len(close_attempts) > 1:
                unexpected_reused_closes.append(candidate)
                raise AssertionError("reused descriptor number was closed again")
            before = os.fstat(candidate)
            self.assertEqual(original_identity, (before.st_dev, before.st_ino))
            real_close(candidate)
            physical_closes.append(candidate)
            os.dup2(replacement_source, candidate, inheritable=False)
            after = os.fstat(candidate)
            self.assertEqual(replacement_identity, (after.st_dev, after.st_ino))
            try:
                raise close_error
            except BaseException as error:
                evidence["origin_traceback"] = error.__traceback__
                raise

        caught: Optional[BaseException] = None
        try:
            with (
                mock.patch.object(
                    self.repair.os,
                    "close",
                    side_effect=close_reuse_then_fail,
                ),
                mock.patch.object(
                    self.repair._DescriptorState,
                    "__del__",
                    autospec=True,
                ) as destructor,
            ):
                try:
                    state.close()
                except BaseException as error:
                    caught = error
                self.assertEqual(0, destructor.call_count)
                self.assertIs(close_error, caught)
                traceback = caught.__traceback__ if caught is not None else None
                traceback_nodes: List[Any] = []
                while traceback is not None:
                    traceback_nodes.append(traceback)
                    traceback = traceback.tb_next
                self.assertIn(evidence["origin_traceback"], traceback_nodes)
                with self.assertRaises(OSError) as repeated:
                    state.close()
                self.assertIs(close_error, repeated.exception)

            receipt = state.close_receipt
            self.assertIsInstance(receipt, self.repair._DescriptorCloseReceipt)
            assert receipt is not None
            self.assertEqual(descriptor, receipt.descriptor)
            self.assertEqual(1, receipt.dispatches)
            self.assertFalse(receipt.completed)
            self.assertIs(
                self.repair._DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED,
                receipt.disposition,
            )
            self.assertIs(close_error, receipt.error)
            self.assertEqual(-1, state.descriptor)
            self.assertEqual([descriptor], close_attempts)
            self.assertEqual([descriptor], physical_closes)
            self.assertEqual([], unexpected_reused_closes)
            reused_metadata = os.fstat(descriptor)
            self.assertEqual(
                replacement_identity,
                (reused_metadata.st_dev, reused_metadata.st_ino),
            )
        finally:
            for candidate in (descriptor, replacement_source):
                try:
                    fcntl.fcntl(candidate, fcntl.F_GETFD)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
                else:
                    real_close(candidate)
        self.assertEqual(baseline, self.open_fd_set())

    def test_repair_lock_body_primary_keeps_unlock_failure_receipt(self) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "lock-body-unlock-secondary",
        )
        baseline = self.open_fd_set()
        body_primary = KeyboardInterrupt("repair lock body primary")
        unlock_error = OSError(errno.EIO, "repair lock unlock outcome unproved")
        evidence: Dict[str, Any] = {}
        unlock_calls: List[int] = []
        descriptor_closes: List[int] = []
        real_flock = self.repair.fcntl.flock
        real_close = self.repair.os.close

        def unlock_then_fail(descriptor: int, operation: int) -> None:
            if operation != self.repair.fcntl.LOCK_UN:
                real_flock(descriptor, operation)
                return
            unlock_calls.append(descriptor)
            evidence["lock_descriptor"] = descriptor
            real_flock(descriptor, operation)
            try:
                raise unlock_error
            except BaseException as error:
                evidence["unlock_origin_traceback"] = error.__traceback__
                raise

        def record_close(descriptor: int) -> None:
            if descriptor == evidence.get("lock_descriptor"):
                descriptor_closes.append(descriptor)
            real_close(descriptor)

        caught: Optional[BaseException] = None
        with (
            mock.patch.object(
                self.repair.fcntl,
                "flock",
                side_effect=unlock_then_fail,
            ),
            mock.patch.object(
                self.repair.os,
                "close",
                side_effect=record_close,
            ),
        ):
            try:
                with self.repair.repair_lock(config):
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise
            except BaseException as error:
                caught = error
            self.assertEqual(1, len(unlock_calls))

        self.assertIs(body_primary, caught)
        traceback = caught.__traceback__ if caught is not None else None
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(evidence["body_origin_traceback"], traceback_nodes)
        diagnostics = getattr(body_primary, "_cleanup_diagnostics", ())
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIsInstance(diagnostic, self.repair._CleanupDiagnostic)
        self.assertIs(unlock_error, diagnostic.error)
        receipt = diagnostic.receipt
        self.assertIsInstance(receipt, self.repair._CleanupActionReceipt)
        self.assertEqual(1, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIs(unlock_error, receipt.error)
        self.assertIn("repair lock release was not proved clean", diagnostic.detail)
        self.assertIn(str(unlock_error), diagnostic.detail)
        add_note = getattr(body_primary, "add_note", None)
        if callable(add_note):
            self.assertIn(diagnostic.detail, getattr(body_primary, "__notes__", ()))
        descriptor = unlock_calls[0]
        self.assertEqual([descriptor], descriptor_closes)
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
        self.assertIsNone(self.repair._active_state_root())
        self.assertEqual(baseline, self.open_fd_set())
        with self.repair.repair_lock(config):
            pass

    def test_repair_lock_revalidation_detail_trace_preserves_body_primary(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "lock-revalidation-detail-trace",
        )
        baseline = self.open_fd_set()
        body_primary = KeyboardInterrupt("repair lock detail body primary")
        verification_error = OSError(
            errno.ESTALE,
            "repair lock detail revalidation failed",
        )
        evidence: Dict[str, Any] = {}
        body_active = False
        real_revalidate = self.repair.StateDirectory.revalidate

        def fail_failure_path_revalidation(state: Any) -> None:
            if not body_active:
                real_revalidate(state)
                return
            raise verification_error

        def diagnostic_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("error") is not body_primary
                or frame.f_locals.get("verification_error") is not verification_error
                or getattr(body_primary, "_cleanup_diagnostics", ())
            ):
                return False
            captured["verification_error"] = verification_error
            captured["diagnostic_line"] = frame.f_lineno
            return True

        def operation() -> None:
            nonlocal body_active
            with self.repair.repair_lock(config):
                try:
                    body_active = True
                    raise body_primary
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    raise

        with mock.patch.object(
            self.repair.StateDirectory,
            "revalidate",
            autospec=True,
            side_effect=fail_failure_path_revalidation,
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._bound_repair_lock.__wrapped__.__code__,
                diagnostic_boundary,
                operation,
                body_primary,
                label="repair lock revalidation diagnostic",
                events=("line",),
                evidence=evidence,
            )

        self.assertIsInstance(evidence["diagnostic_line"], int)
        diagnostics = getattr(body_primary, "_cleanup_diagnostics", ())
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIs(verification_error, diagnostic.error)
        self.assertIsNone(diagnostic.receipt)
        self.assertIn(
            "repair lock namespace changed while handling failure",
            diagnostic.detail,
        )
        self.assertIn(str(verification_error), diagnostic.detail)
        self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
        self.assertIsNone(self.repair._active_state_root())
        self.assertEqual(baseline, self.open_fd_set())
        with self.repair.repair_lock(config):
            pass

    def test_descriptor_close_detail_trace_preserves_body_primary(self) -> None:
        baseline = self.open_fd_set()
        descriptor = os.open(
            os.devnull,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        subject = "descriptor diagnostic trace"
        state = self.repair._DescriptorState(subject, descriptor)
        body_primary = RuntimeError("descriptor diagnostic body primary")
        close_error = OSError(errno.EIO, "descriptor diagnostic close failure")
        evidence: Dict[str, Any] = {}
        close_attempts: List[int] = []
        physical_closes: List[int] = []
        real_close = self.repair.os.close

        def close_then_fail(candidate: int) -> None:
            if candidate != descriptor:
                real_close(candidate)
                return
            close_attempts.append(candidate)
            real_close(candidate)
            physical_closes.append(candidate)
            raise close_error

        def diagnostic_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            receipt = frame.f_locals.get("receipt")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("self") is not state
                or frame.f_locals.get("primary_error") is not body_primary
                or frame.f_locals.get("authoritative_error") is not close_error
                or receipt is not state.close_receipt
                or receipt is None
                or receipt.dispatches != 1
                or receipt.completed
                or receipt.error is not close_error
                or receipt.disposition
                is not self.repair._DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED
                or state.descriptor != -1
                or getattr(body_primary, "_cleanup_diagnostics", ())
            ):
                return False
            captured["receipt"] = receipt
            captured["diagnostic_line"] = frame.f_lineno
            return True

        def operation() -> None:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                state.close(primary_error=error)
                raise

        with (
            mock.patch.object(
                self.repair.os,
                "close",
                side_effect=close_then_fail,
            ),
            mock.patch.object(
                self.repair._DescriptorState,
                "__del__",
                autospec=True,
            ) as destructor,
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._DescriptorState.close.__code__,
                diagnostic_boundary,
                operation,
                body_primary,
                label="descriptor close diagnostic",
                events=("line",),
                evidence=evidence,
            )
            self.assertEqual(0, destructor.call_count)

        receipt = evidence["receipt"]
        self.assertIs(receipt, state.close_receipt)
        self.assertIsInstance(evidence["diagnostic_line"], int)
        self.assertEqual(1, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIs(close_error, receipt.error)
        diagnostics = getattr(body_primary, "_cleanup_diagnostics", ())
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIs(close_error, diagnostic.error)
        self.assertIs(receipt, diagnostic.receipt)
        self.assertIn(subject, diagnostic.detail)
        self.assertIn(str(close_error), diagnostic.detail)
        self.assertEqual(-1, state.descriptor)
        self.assertEqual([descriptor], close_attempts)
        self.assertEqual([descriptor], physical_closes)
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_no_body_close_error_survives_handler_trace_across_owner_drains(
        self,
    ) -> None:
        routes = ("descriptor", "state-directory", "descriptor-drain", "primary-drain")
        for index, route in enumerate(routes):
            with self.subTest(route=route):
                baseline = self.open_fd_set()
                evidence: Dict[str, Any] = {}
                natural_error = OSError(errno.EIO, f"{route} natural close error")
                physical_closes: List[int] = []
                close_dispatches: List[Optional[BaseException]] = []
                real_close = self.repair.os.close
                handle: Optional[Any] = None

                if route == "state-directory":
                    handle = self.repair._open_state_directory(
                        self.root / f"natural-close-{index}", create=True
                    )
                    handle.__enter__()
                    owner = handle._owner
                    self.assertIsInstance(owner, self.repair._OwnedDescriptor)
                    assert owner is not None
                    descriptor = owner.fileno()
                elif route == "primary-drain":
                    descriptor = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    repair_module = self.repair

                    class PrimaryOwner:
                        def __init__(self) -> None:
                            self.closed = False

                        def close(
                            self, *, primary_error: Optional[BaseException] = None
                        ) -> None:
                            close_dispatches.append(primary_error)
                            if self.closed:
                                return
                            self.closed = True
                            repair_module.os.close(descriptor)

                    primary_owner = PrimaryOwner()
                    owner = primary_owner
                else:
                    descriptor = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    owner = self.repair._OwnedDescriptor(f"{route} natural close trace")
                    owner._adopt(descriptor)

                if route != "primary-drain":
                    real_owner_close = owner.close

                    def record_owner_close(
                        *,
                        primary_error: Optional[BaseException] = None,
                        durable_namespace_complete: bool = False,
                    ) -> None:
                        close_dispatches.append(primary_error)
                        real_owner_close(
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )

                    owner_close_patch = mock.patch.object(
                        owner, "close", side_effect=record_owner_close
                    )
                else:
                    owner_close_patch = contextlib.nullcontext()

                def close_then_fail(candidate: int) -> None:
                    if candidate != descriptor or physical_closes:
                        real_close(candidate)
                        return
                    physical_closes.append(candidate)
                    real_close(candidate)
                    try:
                        raise natural_error
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    source = linecache.getline(
                        frame.f_code.co_filename, frame.f_lineno
                    ).strip()
                    if source != "_cleanup_guard = True":
                        return False
                    if route == "descriptor":
                        receipt = frame.f_locals.get("receipt")
                        matched = (
                            frame.f_locals.get("self") is owner._state
                            and frame.f_locals.get("error") is natural_error
                            and frame.f_locals.get("primary_error") is None
                            and receipt is owner._state.close_receipt
                            and receipt is not None
                            and receipt.descriptor == descriptor
                            and receipt.dispatches == 1
                            and not receipt.completed
                            and receipt.error is natural_error
                            and receipt.disposition
                            is self.repair._DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED
                            and owner._state.descriptor == -1
                        )
                    elif route == "state-directory":
                        matched = (
                            frame.f_locals.get("self") is handle
                            and frame.f_locals.get("owner") is owner
                            and frame.f_locals.get("cleanup_error") is natural_error
                            and frame.f_locals.get("primary_error") is None
                            and frame.f_locals.get("retry_dispatched") is False
                            and owner.closed
                            and handle is not None
                            and handle._owner is owner
                        )
                    else:
                        matched = (
                            frame.f_locals.get("error") is natural_error
                            and frame.f_locals.get("primary_error") is None
                            and frame.f_locals.get("owner") is owner
                            and frame.f_locals.get("retry_dispatched") is False
                            and owner.closed
                        )
                    if not matched:
                        return False
                    captured["handler_line"] = frame.f_lineno
                    captured["owner"] = owner
                    captured["descriptor"] = descriptor
                    return True

                if route == "descriptor":
                    target_code = self.repair._DescriptorState.close.__code__

                    def operation() -> None:
                        owner.close()

                elif route == "state-directory":
                    assert handle is not None
                    target_code = self.repair.StateDirectory.close.__code__
                    operation = handle.close
                elif route == "descriptor-drain":
                    target_code = self.repair._drain_descriptor_owners_once.__code__

                    def operation() -> None:
                        self.repair._drain_descriptor_owners((owner,))

                else:
                    target_code = self.repair._drain_primary_closeables_once.__code__

                    def operation() -> None:
                        self.repair._drain_primary_closeables((owner,))

                closed_before_fallback = False
                try:
                    with (
                        mock.patch.object(
                            self.repair.os, "close", side_effect=close_then_fail
                        ),
                        owner_close_patch,
                    ):
                        self.assert_cleanup_trace_preserves_primary(
                            target_code,
                            handler_boundary,
                            operation,
                            natural_error,
                            label=f"{route} natural close handler",
                            events=("line",),
                            evidence=evidence,
                        )
                        closed_before_fallback = bool(owner.closed)
                finally:
                    if route == "state-directory":
                        assert handle is not None
                        handle.close(primary_error=natural_error)
                    elif not owner.closed:
                        if route == "primary-drain":
                            owner.close(primary_error=natural_error)
                        else:
                            owner.close(primary_error=natural_error)

                self.assertIsInstance(evidence["handler_line"], int)
                self.assertTrue(closed_before_fallback)
                self.assertEqual([descriptor], physical_closes)
                expected_dispatches = (
                    [None] if route == "descriptor" else [None, natural_error]
                )
                self.assertEqual(expected_dispatches, close_dispatches)
                if handle is not None:
                    self.assertIsNone(handle._owner)
                with self.assertRaises(OSError) as closed:
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)
                self.assertEqual(errno.EBADF, closed.exception.errno)
                self.assertEqual(baseline, self.open_fd_set())

    def test_multi_owner_drains_keep_first_natural_error_after_loop_trace(
        self,
    ) -> None:
        for route in ("descriptor", "primary"):
            with self.subTest(route=route):
                baseline = self.open_fd_set()
                natural_error = OSError(
                    errno.EIO, f"{route} first owner natural close error"
                )
                evidence: Dict[str, Any] = {}
                descriptors = tuple(
                    os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    for _index in range(3)
                )
                physical_closes: List[int] = []
                dispatches: List[List[Optional[BaseException]]] = [
                    [] for _descriptor in descriptors
                ]
                real_close = self.repair.os.close

                if route == "descriptor":
                    owners = []
                    for index, descriptor in enumerate(descriptors):
                        owner = self.repair._OwnedDescriptor(
                            f"multi-owner natural close {index}"
                        )
                        owner._adopt(descriptor)
                        owners.append(owner)
                    target_code = self.repair._drain_descriptor_owners_once.__code__

                    def operation() -> None:
                        self.repair._drain_descriptor_owners(owners)

                else:
                    repair_module = self.repair

                    class PrimaryOwner:
                        def __init__(self, index: int, descriptor: int) -> None:
                            self.index = index
                            self.descriptor = descriptor
                            self.closed = False

                        def close(
                            self, *, primary_error: Optional[BaseException] = None
                        ) -> None:
                            dispatches[self.index].append(primary_error)
                            if self.closed:
                                return
                            self.closed = True
                            repair_module.os.close(self.descriptor)

                    owners = [
                        PrimaryOwner(index, descriptor)
                        for index, descriptor in enumerate(descriptors)
                    ]
                    target_code = self.repair._drain_primary_closeables_once.__code__

                    def operation() -> None:
                        self.repair._drain_primary_closeables(owners)

                def close_then_fail(candidate: int) -> None:
                    if candidate in descriptors:
                        physical_closes.append(candidate)
                    real_close(candidate)
                    if (
                        candidate == descriptors[0]
                        and physical_closes.count(candidate) == 1
                    ):
                        try:
                            raise natural_error
                        except BaseException as error:
                            evidence["body_origin_traceback"] = error.__traceback__
                            raise

                def loop_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    receipt = frame.f_locals.get("receipt")
                    if (
                        linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "if owner is None:"
                        or frame.f_locals.get("owner") is not owners[1]
                        or frame.f_locals.get("close_error") is not natural_error
                        or frame.f_locals.get("primary_error") is not None
                        or receipt is None
                        or receipt.completed
                        or getattr(receipt, "first_error", None) is not natural_error
                        or not owners[0].closed
                        or any(owner.closed for owner in owners[1:])
                    ):
                        return False
                    captured["owners"] = tuple(owners)
                    captured["receipt"] = receipt
                    captured["loop_line"] = frame.f_lineno
                    return True

                def record_descriptor_owner_close(index: int, owner: Any) -> Any:
                    real_owner_close = owner.close

                    def record(
                        *,
                        primary_error: Optional[BaseException] = None,
                        durable_namespace_complete: bool = False,
                    ) -> None:
                        dispatches[index].append(primary_error)
                        real_owner_close(
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )

                    return record

                closed_before_fallback: Tuple[bool, ...] = ()
                try:
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(
                                self.repair.os,
                                "close",
                                side_effect=close_then_fail,
                            )
                        )
                        if route == "descriptor":
                            for index, owner in enumerate(owners):
                                stack.enter_context(
                                    mock.patch.object(
                                        owner,
                                        "close",
                                        side_effect=record_descriptor_owner_close(
                                            index, owner
                                        ),
                                    )
                                )
                        self.assert_cleanup_trace_preserves_primary(
                            target_code,
                            loop_boundary,
                            operation,
                            natural_error,
                            label=f"{route} multi-owner loop",
                            events=("line",),
                            evidence=evidence,
                        )
                        closed_before_fallback = tuple(owner.closed for owner in owners)
                finally:
                    for owner in owners:
                        if not owner.closed:
                            if route == "descriptor":
                                owner.close(primary_error=natural_error)
                            else:
                                owner.close(primary_error=natural_error)

                self.assertIsInstance(evidence["loop_line"], int)
                self.assertEqual((True, True, True), closed_before_fallback)
                self.assertEqual(sorted(descriptors), sorted(physical_closes))
                for descriptor in descriptors:
                    self.assertEqual(1, physical_closes.count(descriptor))
                    with self.assertRaises(OSError) as closed:
                        fcntl.fcntl(descriptor, fcntl.F_GETFD)
                    self.assertEqual(errno.EBADF, closed.exception.errno)
                for owner_dispatches in dispatches:
                    self.assertNotIn(evidence["cleanup_interrupt"], owner_dispatches)
                self.assertIsNone(dispatches[0][0])
                self.assertTrue(
                    all(
                        primary is natural_error
                        for owner_dispatches in dispatches
                        for primary in owner_dispatches[1:]
                    )
                )
                self.assertTrue(
                    all(
                        any(primary is natural_error for primary in owner_dispatches)
                        for owner_dispatches in dispatches[1:]
                    )
                )
                self.assertEqual(baseline, self.open_fd_set())

    def test_natural_cleanup_errors_survive_handler_preguard_trace(self) -> None:
        for route in ("descriptor", "repair-lock"):
            with self.subTest(route=route):
                baseline = self.open_fd_set()
                natural_error = OSError(
                    errno.EIO, f"{route} handler preguard natural error"
                )
                evidence: Dict[str, Any] = {}
                physical_closes: List[int] = []
                real_close = self.repair.os.close

                if route == "descriptor":
                    descriptor = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    state = self.repair._DescriptorState(
                        "descriptor handler preguard", descriptor
                    )

                    def close_then_fail(candidate: int) -> None:
                        if candidate == descriptor:
                            physical_closes.append(candidate)
                        real_close(candidate)
                        if candidate == descriptor:
                            try:
                                raise natural_error
                            except BaseException as error:
                                evidence["body_origin_traceback"] = error.__traceback__
                                raise

                    def handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                        receipt = frame.f_locals.get("receipt")
                        if (
                            linecache.getline(
                                frame.f_code.co_filename, frame.f_lineno
                            ).strip()
                            != "_cleanup_guard = True"
                            or frame.f_locals.get("self") is not state
                            or frame.f_locals.get("error") is not natural_error
                            or frame.f_locals.get("primary_error") is not None
                            or receipt is not state.close_receipt
                            or receipt is None
                            or receipt.descriptor != descriptor
                            or receipt.dispatches != 1
                            or receipt.completed
                            or receipt.error is not natural_error
                            or receipt.disposition
                            is not self.repair._DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED
                            or state.descriptor != -1
                        ):
                            return False
                        captured["state"] = state
                        captured["descriptor"] = descriptor
                        captured["handler_line"] = frame.f_lineno
                        return True

                    with (
                        mock.patch.object(
                            self.repair.os,
                            "close",
                            side_effect=close_then_fail,
                        ),
                        mock.patch.object(
                            self.repair._DescriptorState,
                            "__del__",
                            autospec=True,
                        ) as destructor,
                    ):
                        self.assert_cleanup_trace_preserves_primary(
                            self.repair._DescriptorState.close.__code__,
                            handler_boundary,
                            state.close,
                            natural_error,
                            label="descriptor handler preguard",
                            events=("line",),
                            evidence=evidence,
                        )
                        self.assertEqual(0, destructor.call_count)
                    self.assertIs(evidence["state"], state)
                    self.assertEqual(-1, state.descriptor)
                    self.assertEqual([descriptor], physical_closes)
                    with self.assertRaises(OSError) as repeated:
                        state.close()
                    self.assertIs(natural_error, repeated.exception)
                else:
                    config = dataclasses.replace(
                        self.config(),
                        state_root=self.state_root / "lock-handler-preguard",
                    )
                    real_flock = self.repair.fcntl.flock
                    unlock_calls: List[int] = []

                    def fail_first_unlock(candidate: int, operation: int) -> None:
                        if operation != self.repair.fcntl.LOCK_UN:
                            real_flock(candidate, operation)
                            return
                        unlock_calls.append(candidate)
                        if len(unlock_calls) == 1:
                            try:
                                raise natural_error
                            except BaseException as error:
                                evidence["body_origin_traceback"] = error.__traceback__
                                raise
                        real_flock(candidate, operation)

                    def record_lock_close(candidate: int) -> None:
                        if candidate == evidence.get("descriptor"):
                            physical_closes.append(candidate)
                        real_close(candidate)

                    def unlock_handler_boundary(
                        frame: Any, captured: Dict[str, Any]
                    ) -> bool:
                        complete_frame = frame.f_back
                        lock_frame = (
                            complete_frame.f_back
                            if complete_frame is not None
                            and complete_frame.f_code
                            is self.repair._complete_cleanup_action.__code__
                            else None
                        )
                        if (
                            lock_frame is None
                            or lock_frame.f_code
                            is not self.repair._bound_repair_lock.__wrapped__.__code__
                        ):
                            return False
                        descriptor_owner = lock_frame.f_locals.get("descriptor_owner")
                        descriptor = lock_frame.f_locals.get("descriptor")
                        receipt = frame.f_locals.get("receipt")
                        if (
                            linecache.getline(
                                frame.f_code.co_filename, frame.f_lineno
                            ).strip()
                            != "_cleanup_guard = True"
                            or frame.f_locals.get("error") is not natural_error
                            or receipt is not lock_frame.f_locals.get("unlock_receipt")
                            or receipt.dispatches != 1
                            or receipt.completed
                            or receipt.error is not None
                            or lock_frame.f_locals.get("primary_error") is not None
                            or lock_frame.f_locals.get("acquired") is not True
                            or not isinstance(
                                descriptor_owner, self.repair._OwnedDescriptor
                            )
                            or descriptor_owner.closed
                            or descriptor != descriptor_owner.fileno()
                            or unlock_calls != [descriptor]
                        ):
                            return False
                        captured["owner"] = descriptor_owner
                        captured["descriptor"] = descriptor
                        captured["handler_line"] = frame.f_lineno
                        return True

                    def run_lock() -> None:
                        with self.repair.repair_lock(config):
                            pass

                    with (
                        mock.patch.object(
                            self.repair.fcntl,
                            "flock",
                            side_effect=fail_first_unlock,
                        ),
                        mock.patch.object(
                            self.repair.os,
                            "close",
                            side_effect=record_lock_close,
                        ),
                    ):
                        self.assert_cleanup_trace_preserves_primary(
                            self.repair._dispatch_cleanup_action.__code__,
                            unlock_handler_boundary,
                            run_lock,
                            natural_error,
                            label="repair lock unlock handler preguard",
                            events=("line",),
                            evidence=evidence,
                        )
                    self.assertTrue(evidence["owner"].closed)
                    self.assertEqual(1, len(unlock_calls))
                    self.assertEqual([evidence["descriptor"]], physical_closes)
                    self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
                    self.assertIsNone(self.repair._active_state_root())
                    with self.repair.repair_lock(config):
                        pass

                self.assertIsInstance(evidence["handler_line"], int)
                descriptor = evidence["descriptor"]
                with self.assertRaises(OSError) as closed:
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)
                self.assertEqual(errno.EBADF, closed.exception.errno)
                self.assertEqual(baseline, self.open_fd_set())

    def test_repair_lock_unlock_error_survives_posthandler_trace(self) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "lock-unlock-posthandler-trace",
        )
        baseline = self.open_fd_set()
        natural_error = OSError(errno.EIO, "repair lock natural unlock error")
        evidence: Dict[str, Any] = {}
        unlock_calls: List[int] = []
        physical_closes: List[int] = []
        real_flock = self.repair.fcntl.flock
        real_close = self.repair.os.close

        def fail_first_unlock(descriptor: int, operation: int) -> None:
            if operation != self.repair.fcntl.LOCK_UN:
                real_flock(descriptor, operation)
                return
            unlock_calls.append(descriptor)
            if len(unlock_calls) == 1:
                try:
                    raise natural_error
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    raise
            real_flock(descriptor, operation)

        def record_close(descriptor: int) -> None:
            if descriptor == evidence.get("descriptor"):
                physical_closes.append(descriptor)
            real_close(descriptor)

        def posthandler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            descriptor_owner = frame.f_locals.get("descriptor_owner")
            descriptor = frame.f_locals.get("descriptor")
            unlock_receipt = frame.f_locals.get("unlock_receipt")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "active_primary = ("
                or frame.f_locals.get("unlock_error") is not natural_error
                or frame.f_locals.get("primary_error") is not None
                or frame.f_locals.get("cleanup_error") is not None
                or frame.f_locals.get("acquired") is not True
                or not isinstance(descriptor_owner, self.repair._OwnedDescriptor)
                or descriptor_owner.closed
                or descriptor != descriptor_owner.fileno()
                or not isinstance(unlock_receipt, self.repair._CleanupActionReceipt)
                or unlock_receipt.dispatches != 1
                or unlock_receipt.completed
                or unlock_receipt.error is not natural_error
                or unlock_calls != [descriptor]
            ):
                return False
            captured["owner"] = descriptor_owner
            captured["descriptor"] = descriptor
            captured["unlock_receipt"] = unlock_receipt
            captured["posthandler_line"] = frame.f_lineno
            return True

        def run_lock() -> None:
            with self.repair.repair_lock(config):
                pass

        with (
            mock.patch.object(
                self.repair.fcntl,
                "flock",
                side_effect=fail_first_unlock,
            ),
            mock.patch.object(
                self.repair.os,
                "close",
                side_effect=record_close,
            ),
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._bound_repair_lock.__wrapped__.__code__,
                posthandler_boundary,
                run_lock,
                natural_error,
                label="repair lock unlock posthandler",
                events=("line",),
                evidence=evidence,
            )

        self.assertIsInstance(evidence["posthandler_line"], int)
        self.assertTrue(evidence["owner"].closed)
        self.assertEqual([evidence["descriptor"]], unlock_calls)
        self.assertEqual(1, evidence["unlock_receipt"].dispatches)
        self.assertFalse(evidence["unlock_receipt"].completed)
        self.assertIs(natural_error, evidence["unlock_receipt"].error)
        self.assertEqual([evidence["descriptor"]], physical_closes)
        self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
        self.assertIsNone(self.repair._active_state_root())
        with self.repair.repair_lock(config):
            pass
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(evidence["descriptor"], fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_repair_lock_descriptor_error_survives_drain_handler_trace(self) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "lock-descriptor-drain-handler-trace",
        )
        baseline = self.open_fd_set()
        natural_error = OSError(errno.EIO, "repair lock descriptor close error")
        evidence: Dict[str, Any] = {}
        physical_closes: List[int] = []
        real_flock = self.repair.fcntl.flock
        real_close = self.repair.os.close

        def record_flock(descriptor: int, operation: int) -> None:
            if operation & self.repair.fcntl.LOCK_EX:
                evidence["descriptor"] = descriptor
            real_flock(descriptor, operation)

        def close_then_fail(descriptor: int) -> None:
            if descriptor != evidence.get("descriptor"):
                real_close(descriptor)
                return
            physical_closes.append(descriptor)
            real_close(descriptor)
            try:
                raise natural_error
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def drain_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            descriptor_owner = frame.f_locals.get("descriptor_owner")
            descriptor = frame.f_locals.get("descriptor")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("error") is not natural_error
                or frame.f_locals.get("cleanup_error") is not None
                or frame.f_locals.get("primary_error") is not None
                or frame.f_locals.get("unlock_error") is not None
                or frame.f_locals.get("active_primary") is not None
                or descriptor != captured.get("descriptor")
                or not isinstance(descriptor_owner, self.repair._OwnedDescriptor)
                or not descriptor_owner.closed
                or physical_closes != [descriptor]
            ):
                return False
            captured["owner"] = descriptor_owner
            captured["drain_handler_line"] = frame.f_lineno
            return True

        def run_lock() -> None:
            with self.repair.repair_lock(config):
                pass

        with (
            mock.patch.object(
                self.repair.fcntl,
                "flock",
                side_effect=record_flock,
            ),
            mock.patch.object(
                self.repair.os,
                "close",
                side_effect=close_then_fail,
            ),
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._bound_repair_lock.__wrapped__.__code__,
                drain_handler_boundary,
                run_lock,
                natural_error,
                label="repair lock descriptor drain handler",
                events=("line",),
                evidence=evidence,
            )

        self.assertIsInstance(evidence["drain_handler_line"], int)
        self.assertTrue(evidence["owner"].closed)
        self.assertEqual([evidence["descriptor"]], physical_closes)
        self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
        self.assertIsNone(self.repair._active_state_root())
        with self.repair.repair_lock(config):
            pass
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(evidence["descriptor"], fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_state_directory_enter_trace_registration_closes_once(self) -> None:
        for registered in (False, True):
            with self.subTest(registered=registered):
                path = self.root / f"state-enter-{registered}"
                baseline = self.open_fd_set()
                handle = self.repair._open_state_directory(path, create=True)
                evidence: Dict[str, Any] = {}

                def registration_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    if frame.f_locals.get("self") is not handle:
                        return False
                    owner = frame.f_locals.get("owner")
                    if (
                        not isinstance(owner, self.repair._OwnedDescriptor)
                        or owner.closed
                        or owner._retain_if_registered is None
                        or "metadata" not in frame.f_locals
                    ):
                        return False
                    is_registered = handle._owner is owner
                    if is_registered != registered:
                        return False
                    if registered and handle.identity is not None:
                        return False
                    self.capture_owned_descriptor(captured, owner)
                    captured["handle"] = handle
                    captured["slot_owner"] = handle._owner
                    captured["line_number"] = frame.f_lineno
                    return True

                with self.record_captured_descriptor_closes(evidence):
                    self.assert_trace_interruption(
                        self.repair.StateDirectory.__enter__.__code__,
                        registration_boundary,
                        lambda: handle.__enter__(),
                        label=f"state directory registration {registered}",
                        evidence=evidence,
                    )

                self.assertEqual(
                    registered,
                    evidence["slot_owner"] is evidence["descriptor_owner"],
                )
                self.assertIsNone(handle._owner)
                self.assertEqual(-1, handle.descriptor)
                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())

        normal = self.repair._open_state_directory(
            self.root / "state-enter-normal", create=True
        )
        with normal as entered:
            descriptor = entered.descriptor
            self.assertGreaterEqual(descriptor, 0)
            self.assertEqual(os.fstat(descriptor).st_ino, normal.identity[1])
        self.assertEqual(-1, normal.descriptor)
        self.assertIsNone(normal._owner)

    def test_state_directory_callers_trace_active_with_closes_immediately(
        self,
    ) -> None:
        cases = (
            "ensure",
            "atomic",
            "read",
            "lock",
            "delete",
            "load-manifests",
            "load-intents",
        )
        payload = {"version": 1, "entries": [UUID_A]}
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"active-with-{index}"
                )
                path = config.queue_path
                if case == "ensure":
                    expected_path = config.state_root / "ensured"

                    def operation() -> None:
                        self.repair._ensure_private_directory(expected_path)

                    target_code = self.repair._ensure_private_directory.__code__
                elif case == "atomic":
                    expected_path = path.parent

                    def operation() -> None:
                        self.repair.atomic_write_json(path, payload)

                    target_code = self.repair.atomic_write_json.__code__
                elif case == "read":
                    path.parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(encoded)
                    path.chmod(0o600)
                    expected_path = path.parent

                    def operation() -> None:
                        self.repair._read_json_record(path)

                    target_code = self.repair._read_json_record.__code__
                elif case == "lock":
                    expected_path = config.state_root

                    def operation() -> None:
                        with self.repair.repair_lock(config):
                            pass

                    target_code = self.repair.repair_lock.__wrapped__.__code__
                elif case == "delete":
                    path = config.manifests_dir / f"{UUID_A}.json"
                    path.parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(b"{}\n")
                    path.chmod(0o600)
                    expected_path = path.parent

                    def operation() -> None:
                        self.repair._delete_state_file(path, label="manifest")

                    target_code = self.repair._delete_state_file.__code__
                elif case == "load-manifests":
                    config.manifests_dir.mkdir(parents=True, mode=0o700)
                    expected_path = config.manifests_dir

                    def operation() -> None:
                        self.repair._load_manifests(config)

                    target_code = self.repair._load_manifests.__code__
                else:
                    config.intents_dir.mkdir(parents=True, mode=0o700)
                    expected_path = config.intents_dir

                    def operation() -> None:
                        self.repair._load_intents(config)

                    target_code = self.repair._load_intents.__code__

                expected_path = expected_path.absolute()
                baseline = self.open_fd_set()
                handles: List[Any] = []
                evidence: Dict[str, Any] = {}
                real_open_state_directory = self.repair._open_state_directory

                def recording_open_state_directory(
                    *arguments: Any, **keywords: Any
                ) -> Any:
                    opened = real_open_state_directory(*arguments, **keywords)
                    if opened.path == expected_path:
                        handles.append(opened)
                    return opened

                def caller_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    if not handles:
                        return False
                    handle = handles[-1]
                    owner = handle._owner
                    if (
                        handle.path != expected_path
                        or not isinstance(owner, self.repair._OwnedDescriptor)
                        or owner.closed
                        or handle.identity is None
                    ):
                        return False
                    self.capture_owned_descriptor(captured, owner)
                    captured["state_directory"] = handle
                    captured["line_number"] = frame.f_lineno
                    return True

                def unwrap_atomic(caught: Any) -> Any:
                    self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
                    self.assertEqual(
                        self.repair.AtomicPublication.NOT_PUBLISHED,
                        caught.publication,
                    )
                    return caught.cause

                with (
                    mock.patch.object(
                        self.repair,
                        "_open_state_directory",
                        side_effect=recording_open_state_directory,
                    ),
                    self.record_captured_descriptor_closes(evidence),
                ):
                    self.assert_trace_interruption(
                        target_code,
                        caller_boundary,
                        operation,
                        label=f"active state directory caller {case}",
                        unwrap_caught=(unwrap_atomic if case == "atomic" else None),
                        evidence=evidence,
                    )

                state_directory = evidence["state_directory"]
                self.assertEqual(
                    -1,
                    state_directory.descriptor,
                    msg=f"{case} trace line {evidence['line_number']}",
                )
                self.assertIsNone(state_directory._owner)
                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())
                if case == "ensure":
                    self.repair._ensure_private_directory(expected_path)
                    self.assertTrue(expected_path.is_dir())
                elif case == "atomic":
                    self.repair.atomic_write_json(path, payload)
                    self.assertEqual(payload, self.repair._read_json(path))
                elif case == "read":
                    record = self.repair._read_json_record(path)
                    self.assertIsNotNone(record)
                    self.assertEqual(payload, record[0])
                elif case == "lock":
                    self.assertIsNone(self.repair._ACTIVE_STATE_ROOT)
                    self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
                    with self.repair.repair_lock(config):
                        pass
                elif case == "delete":
                    self.assertTrue(path.exists())
                    self.repair._delete_state_file(path, label="manifest")
                    self.assertFalse(path.exists())
                elif case == "load-manifests":
                    self.assertEqual([], self.repair._load_manifests(config))
                else:
                    self.assertEqual([], self.repair._load_intents(config))
                self.assertEqual(baseline, self.open_fd_set())

    def test_state_directory_close_and_release_trace_keep_owner_slot(
        self,
    ) -> None:
        for method_name in ("close", "release"):
            with self.subTest(method=method_name):
                path = self.root / f"state-{method_name}-trace"
                baseline = self.open_fd_set()
                handle = self.repair._open_state_directory(path, create=True)
                evidence: Dict[str, Any] = {}

                def owner_slot_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    if frame.f_locals.get("self") is not handle:
                        return False
                    owner = frame.f_locals.get("owner")
                    if (
                        not isinstance(owner, self.repair._OwnedDescriptor)
                        or owner.closed
                        or handle._owner is not owner
                    ):
                        return False
                    self.capture_owned_descriptor(captured, owner)
                    captured["slot_owner"] = handle._owner
                    return True

                def operation() -> None:
                    with handle as entered:
                        getattr(entered, method_name)()

                with self.record_captured_descriptor_closes(evidence):
                    self.assert_trace_interruption(
                        getattr(self.repair.StateDirectory, method_name).__code__,
                        owner_slot_boundary,
                        operation,
                        label=f"state directory {method_name} slot",
                        events=(("return",) if method_name == "release" else ("line",)),
                        evidence=evidence,
                    )

                self.assertIs(evidence["slot_owner"], evidence["descriptor_owner"])
                self.assertIsNone(handle._owner)
                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())

        normal_close = self.repair._open_state_directory(
            self.root / "state-close-normal", create=True
        )
        close_evidence: Dict[str, Any] = {}
        normal_close.__enter__()
        self.capture_owned_descriptor(close_evidence, normal_close._owner)
        with self.record_captured_descriptor_closes(close_evidence):
            normal_close.close()
            normal_close.close()
        self.assertIsNone(normal_close._owner)
        self.assert_captured_descriptor_closed_once(close_evidence)

        normal_release = self.repair._open_state_directory(
            self.root / "state-release-normal", create=True
        )
        normal_release.__enter__()
        release_owner = normal_release._owner
        released = normal_release.release()
        self.assertIs(release_owner, released)
        self.assertIs(release_owner, normal_release._owner)
        released_fd = released.fileno()
        fcntl.fcntl(released_fd, fcntl.F_GETFD)
        released.close()
        self.assertTrue(release_owner.closed)
        self.assertIs(release_owner, normal_release._owner)
        self.assertEqual(-1, normal_release.descriptor)
        self.assertIsNone(normal_release.release())
        normal_release.close()
        self.assertIsNone(normal_release._owner)
        with self.assertRaises(OSError) as closed:
            os.fstat(released_fd)
        self.assertEqual(errno.EBADF, closed.exception.errno)

    def test_state_leaf_raw_handoff_trace_closes_owned_descriptor_once(
        self,
    ) -> None:
        payload = {"version": 1, "entries": [UUID_A]}
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        dummy = self.repair._open_fd_owned(
            os.devnull, os.O_RDONLY, subject="trace code probe"
        )
        acquire_code = dummy._acquire.__code__
        for index, case in enumerate(("probe", "atomic", "read", "lock", "delete")):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"leaf-handoff-{index}"
                )
                path = config.queue_path
                outer_baseline = self.open_fd_set()
                parent_fd = -1
                if case == "probe":
                    path.parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(encoded)
                    path.chmod(0o600)
                    parent_fd = os.open(
                        path.parent,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )

                    def subject_matches(subject: str) -> bool:
                        return subject == f"state publication probe {path.name!r}"

                    def operation() -> None:
                        self.repair._probe_state_leaf(parent_fd, path.name, encoded)

                elif case == "atomic":

                    def subject_matches(subject: str) -> bool:
                        return subject.startswith("atomic temporary ")

                    def operation() -> None:
                        self.repair.atomic_write_json(path, payload)

                elif case == "read":
                    path.parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(encoded)
                    path.chmod(0o600)

                    def subject_matches(subject: str) -> bool:
                        return subject == f"canonical state {path.name!r}"

                    def operation() -> None:
                        self.repair._read_json_record(path)

                elif case == "lock":

                    def subject_matches(subject: str) -> bool:
                        return subject == "repair lock"

                    def operation() -> None:
                        with self.repair.repair_lock(config):
                            pass

                else:
                    path = config.manifests_dir / f"{UUID_A}.json"
                    path.parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(b"{}\n")
                    path.chmod(0o600)

                    def subject_matches(subject: str) -> bool:
                        return subject == "durable manifest"

                    def operation() -> None:
                        self.repair._delete_state_file(path, label="manifest")

                baseline = self.open_fd_set()
                evidence: Dict[str, Any] = {}

                def raw_handoff_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    owner = frame.f_locals.get("target")
                    descriptor = frame.f_locals.get("handed_off", -1)
                    if (
                        not isinstance(owner, self.repair._OwnedDescriptor)
                        or not owner.closed
                        or not isinstance(descriptor, int)
                        or descriptor < 0
                        or not subject_matches(owner._subject)
                    ):
                        return False
                    self.capture_owned_descriptor(
                        captured, owner, descriptor=descriptor
                    )
                    captured["line_number"] = frame.f_lineno
                    return True

                def unwrap_atomic(caught: Any) -> Any:
                    self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
                    return caught.cause

                with self.record_captured_descriptor_closes(evidence):
                    self.assert_trace_interruption(
                        acquire_code,
                        raw_handoff_boundary,
                        operation,
                        label=f"state leaf raw handoff {case}",
                        unwrap_caught=(unwrap_atomic if case == "atomic" else None),
                        evidence=evidence,
                    )

                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())
                if case == "probe":
                    probe = self.repair._probe_state_leaf(parent_fd, path.name, encoded)
                    self.assertIsNotNone(probe)
                    self.assertTrue(probe[1])
                elif case == "atomic":
                    self.repair.atomic_write_json(path, payload)
                    self.assertEqual(payload, self.repair._read_json(path))
                elif case == "read":
                    record = self.repair._read_json_record(path)
                    self.assertIsNotNone(record)
                    self.assertEqual(payload, record[0])
                elif case == "lock":
                    self.assertIsNone(self.repair._ACTIVE_STATE_ROOT)
                    self.assertIsNone(self.repair._ACTIVE_LOCK_DESCRIPTOR)
                    with self.repair.repair_lock(config):
                        pass
                else:
                    self.assertTrue(path.exists())
                    self.repair._delete_state_file(path, label="manifest")
                    self.assertFalse(path.exists())
                self.assertEqual(baseline, self.open_fd_set())
                if parent_fd >= 0:
                    os.close(parent_fd)
                    self.assertEqual(outer_baseline, self.open_fd_set())

    def test_atomic_post_publication_trace_reports_durable(self) -> None:
        payload = {"version": 1, "entries": [UUID_A]}
        for registered in (False, True):
            with self.subTest(receipt_registered=registered):
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"durable-trace-{registered}",
                )
                path = config.queue_path
                baseline = self.open_fd_set()

                def durable_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
                    receipt = frame.f_locals.get("receipt")
                    fence = frame.f_locals.get("fence")
                    if (
                        receipt is None
                        or not isinstance(fence, self.repair.DurableFence)
                        or not frame.f_locals.get("publication_verified", False)
                    ):
                        return False
                    is_registered = (
                        receipt.publication == self.repair.AtomicPublication.DURABLE
                        and receipt.fence is fence
                    )
                    if is_registered != registered:
                        return False
                    evidence["receipt"] = receipt
                    evidence["fence"] = fence
                    evidence["line_number"] = frame.f_lineno
                    return True

                def unwrap_durable(caught: Any) -> Any:
                    self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
                    self.assertEqual(
                        self.repair.AtomicPublication.DURABLE,
                        caught.publication,
                    )
                    return caught.cause

                evidence = self.assert_trace_interruption(
                    self.repair._atomic_write_json_with_parent.__code__,
                    durable_boundary,
                    lambda: self.repair.atomic_write_json(path, payload),
                    label=f"durable publication receipt {registered}",
                    unwrap_caught=unwrap_durable,
                )

                self.assertEqual(
                    self.repair.AtomicPublication.DURABLE,
                    evidence["receipt"].publication,
                )
                self.assertEqual(path.absolute(), evidence["fence"].path)
                self.assertEqual(payload, self.repair._read_json(path))
                self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_error_handler_trace_never_reverts_to_not_published(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "handler-trace-publication"
        )
        path = config.queue_path
        payload = {"version": 1, "entries": [UUID_A]}
        body_primary = KeyboardInterrupt("post-replace primary")
        baseline = self.open_fd_set()
        evidence: Dict[str, Any] = {}
        replaced = False
        body_raised = False
        real_replace = self.repair.os.replace
        real_revalidate = self.repair._revalidate_state_handles

        def recording_replace(*arguments: Any, **keywords: Any) -> None:
            nonlocal replaced
            real_replace(*arguments, **keywords)
            replaced = True

        def interrupt_after_replace(*handles: Any) -> None:
            nonlocal body_raised
            if replaced and not body_raised:
                body_raised = True
                try:
                    raise body_primary
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    raise
            real_revalidate(*handles)

        def handler_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
            receipt = frame.f_locals.get("receipt")
            publication = frame.f_locals.get("publication")
            if (
                frame.f_locals.get("error") is not body_primary
                or receipt is None
                or not isinstance(publication, self.repair.AtomicPublication)
                or publication == self.repair.AtomicPublication.NOT_PUBLISHED
                or receipt.publication == publication
            ):
                return False
            evidence["receipt_before_handler_commit"] = receipt.publication
            evidence["classified_publication"] = publication
            evidence["line_number"] = frame.f_lineno
            return True

        def unwrap_interruption(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
            self.assertNotEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            return caught.cause

        with (
            mock.patch.object(self.repair.os, "replace", side_effect=recording_replace),
            mock.patch.object(
                self.repair,
                "_revalidate_state_handles",
                side_effect=interrupt_after_replace,
            ),
        ):
            evidence = self.assert_cleanup_trace_preserves_primary(
                self.repair._atomic_write_json_with_parent.__code__,
                handler_boundary,
                lambda: self.repair.atomic_write_json(path, payload),
                body_primary,
                label="atomic publication handler interruption",
                events=("line",),
                evidence=evidence,
                unwrap_caught=unwrap_interruption,
            )

        self.assertTrue(replaced)
        self.assertTrue(body_raised)
        self.assertEqual(
            self.repair.AtomicPublication.PUBLISHED_UNSYNCED,
            evidence["classified_publication"],
        )
        self.assertNotEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["receipt_before_handler_commit"],
        )
        self.assertEqual(payload, self.repair._read_json(path))
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_failure_reporting_trace_preserves_natural_primary(self) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "reporting-trace-publication"
        )
        path = config.queue_path
        payload = {"version": 1, "entries": [UUID_A]}
        body_primary = KeyboardInterrupt("post-replace reporting primary")
        baseline = self.open_fd_set()
        evidence: Dict[str, Any] = {}
        replaced = False
        body_raised = False
        real_replace = self.repair.os.replace
        real_revalidate = self.repair._revalidate_state_handles

        def recording_replace(*arguments: Any, **keywords: Any) -> None:
            nonlocal replaced
            real_replace(*arguments, **keywords)
            replaced = True

        def interrupt_after_replace(*handles: Any) -> None:
            nonlocal body_raised
            if replaced and not body_raised:
                body_raised = True
                try:
                    raise body_primary
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    raise
            real_revalidate(*handles)

        def first_failure_report(frame: Any, captured: Dict[str, Any]) -> bool:
            caller = frame.f_back
            if (
                frame.f_locals.get("error") is not body_primary
                or frame.f_locals.get("reporting_error") is not None
                or caller is None
                or caller.f_code
                is not self.repair._atomic_write_json_with_parent.__code__
            ):
                return False
            receipt = caller.f_locals.get("receipt")
            publication = frame.f_locals.get("publication")
            if (
                receipt is None
                or not isinstance(publication, self.repair.AtomicPublication)
                or publication == self.repair.AtomicPublication.NOT_PUBLISHED
                or receipt.publication == self.repair.AtomicPublication.NOT_PUBLISHED
            ):
                return False
            captured["receipt"] = receipt
            captured["publication"] = publication
            return True

        def unwrap_interruption(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
            self.assertNotEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            return caught.cause

        with (
            mock.patch.object(self.repair.os, "replace", side_effect=recording_replace),
            mock.patch.object(
                self.repair,
                "_revalidate_state_handles",
                side_effect=interrupt_after_replace,
            ),
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._atomic_write_failure.__code__,
                first_failure_report,
                lambda: self.repair.atomic_write_json(path, payload),
                body_primary,
                label="atomic failure reporting",
                evidence=evidence,
                unwrap_caught=unwrap_interruption,
            )

        self.assertTrue(replaced)
        self.assertTrue(body_raised)
        self.assertNotEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["receipt"].publication,
        )
        self.assertEqual(payload, self.repair._read_json(path))
        self.assertEqual(baseline, self.open_fd_set())

    def test_descriptor_and_state_cleanup_trace_retries_once(self) -> None:
        for layer in (
            "descriptor-enter",
            "descriptor-exit",
            "closing-descriptor",
            "state-exit",
        ):
            with self.subTest(layer=layer):
                baseline = self.open_fd_set()
                body_primary = RuntimeError(f"{layer} body primary")
                evidence: Dict[str, Any] = {}
                owner: Optional[Any] = None
                state_directory: Optional[Any] = None

                def raise_body_primary() -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                if layer == "descriptor-enter":
                    descriptor = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )

                    def acquire(target: Any) -> None:
                        target._adopt(descriptor)
                        raise_body_primary()

                    owner = self.repair._OwnedDescriptor(layer, acquire)
                    self.capture_owned_descriptor(
                        evidence, owner, descriptor=descriptor
                    )
                    operation = owner.__enter__
                    target_code = self.repair._OwnedDescriptor.close.__code__
                elif layer == "state-exit":
                    state_directory = self.repair._open_state_directory(
                        self.root / "state-cleanup-trace", create=True
                    )

                    def operation() -> None:
                        with state_directory:
                            self.capture_owned_descriptor(
                                evidence, state_directory._owner
                            )
                            raise_body_primary()

                    target_code = self.repair.StateDirectory.close.__code__
                else:
                    owner = self.repair._open_fd_owned(
                        os.devnull, os.O_RDONLY, subject=layer
                    )
                    if layer == "descriptor-exit":

                        def operation() -> None:
                            with owner:
                                self.capture_owned_descriptor(evidence, owner)
                                raise_body_primary()

                    else:

                        def operation() -> None:
                            with self.repair._closing_descriptor(owner):
                                self.capture_owned_descriptor(evidence, owner)
                                raise_body_primary()

                    target_code = self.repair._OwnedDescriptor.close.__code__

                def cleanup_entry(frame: Any, _captured: Dict[str, Any]) -> bool:
                    if frame.f_locals.get("primary_error") is not body_primary:
                        return False
                    if layer == "state-exit":
                        return frame.f_locals.get("self") is state_directory
                    return frame.f_locals.get("self") is owner

                with self.record_captured_descriptor_closes(evidence):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            target_code,
                            cleanup_entry,
                            operation,
                            body_primary,
                            label=layer,
                            evidence=evidence,
                        )
                    finally:
                        if state_directory is not None:
                            state_directory.close(primary_error=body_primary)
                        elif owner is not None:
                            owner.close(primary_error=body_primary)

                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())

    def test_closing_descriptor_enter_handoff_trace_closes_immediately(
        self,
    ) -> None:
        baseline = self.open_fd_set()
        owner = self.repair._open_fd_owned(
            os.devnull,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            subject="closing descriptor enter handoff",
        )
        evidence: Dict[str, Any] = {}

        def active_before_yield(frame: Any, captured: Dict[str, Any]) -> bool:
            if frame.f_locals.get("owner") is not owner or owner.closed:
                return False
            self.capture_owned_descriptor(captured, owner)
            captured["line_number"] = frame.f_lineno
            return True

        with self.record_captured_descriptor_closes(evidence):
            try:
                self.assert_trace_interruption(
                    self.repair._closing_descriptor.__wrapped__.__code__,
                    active_before_yield,
                    lambda: self.repair._closing_descriptor(owner).__enter__(),
                    label="closing descriptor enter handoff",
                    events=("line",),
                    evidence=evidence,
                )
            finally:
                owner.close(primary_error=evidence.get("primary"))

        self.assertIsInstance(evidence["line_number"], int)
        self.assert_captured_descriptor_closed_once(evidence)
        self.assertEqual(baseline, self.open_fd_set())

    def test_descriptor_registration_probe_trace_retries_and_drains(self) -> None:
        for registered in (False, True):
            with self.subTest(registered=registered):
                baseline = self.open_fd_set()
                owner = self.repair._open_fd_owned(
                    os.devnull,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    subject=f"descriptor registration probe {registered}",
                )
                body_primary = RuntimeError(
                    f"descriptor registration {registered} body primary"
                )
                evidence: Dict[str, Any] = {}
                registration_calls: List[bool] = []

                def registration_predicate() -> bool:
                    registration_calls.append(registered)
                    return registered

                def raise_body_primary() -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def operation() -> None:
                    with owner:
                        owner.retain_if_registered(registration_predicate)
                        self.capture_owned_descriptor(evidence, owner)
                        raise_body_primary()

                def first_registration_probe(
                    frame: Any, _captured: Dict[str, Any]
                ) -> bool:
                    probe_caller = frame.f_back
                    exit_caller = (
                        probe_caller.f_back if probe_caller is not None else None
                    )
                    return (
                        not registration_calls
                        and probe_caller is not None
                        and probe_caller.f_code
                        is self.repair._OwnedDescriptor._should_retain_on_exception.__code__
                        and probe_caller.f_locals.get("self") is owner
                        and exit_caller is not None
                        and exit_caller.f_code
                        is self.repair._OwnedDescriptor.__exit__.__code__
                        and exit_caller.f_locals.get("exc_value") is body_primary
                        and exit_caller.f_locals.get("_cleanup_guard") is True
                        and not owner.closed
                    )

                retained_before_fallback = False
                state_closes_before_fallback: tuple[int, ...] = ()
                syscalls_before_fallback: tuple[int, ...] = ()
                with self.record_captured_descriptor_closes(evidence):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            registration_predicate.__code__,
                            first_registration_probe,
                            operation,
                            body_primary,
                            label=f"descriptor registration probe {registered}",
                            evidence=evidence,
                        )
                        retained_before_fallback = not owner.closed
                        state_closes_before_fallback = tuple(
                            evidence["state_close_entries"]
                        )
                        syscalls_before_fallback = tuple(
                            evidence["descriptor_close_calls"]
                        )
                    finally:
                        owner.close(primary_error=body_primary)

                self.assertEqual([registered], registration_calls)
                self.assertEqual(registered, retained_before_fallback)
                if registered:
                    self.assertEqual((), state_closes_before_fallback)
                    self.assertEqual((), syscalls_before_fallback)
                else:
                    descriptor = evidence["descriptor"]
                    self.assertEqual((descriptor,), state_closes_before_fallback)
                    self.assertEqual((descriptor,), syscalls_before_fallback)
                self.assert_captured_descriptor_closed_once(evidence)
                self.assertEqual(baseline, self.open_fd_set())

    def test_state_directory_caller_exit_cleanup_trace_retries_once(self) -> None:
        path = self.root / "state-caller-exit" / "record.json"
        path.parent.mkdir(parents=True, mode=0o700)
        baseline = self.open_fd_set()
        body_primary = RuntimeError("state caller body primary")
        evidence: Dict[str, Any] = {}
        handles: List[Any] = []
        real_open_state_directory = self.repair._open_state_directory

        def recording_open_state_directory(*arguments: Any, **keywords: Any) -> Any:
            handle = real_open_state_directory(*arguments, **keywords)
            handles.append(handle)
            return handle

        def raise_body_primary(*_arguments: Any, **_keywords: Any) -> None:
            handle = handles[-1]
            self.capture_owned_descriptor(evidence, handle._owner)
            evidence["state_directory"] = handle
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def cleanup_entry(frame: Any, _captured: Dict[str, Any]) -> bool:
            return (
                bool(handles)
                and frame.f_locals.get("self") is handles[-1]
                and frame.f_locals.get("primary_error") is body_primary
                and handles[-1]._owner is not None
                and not handles[-1]._owner.closed
            )

        with (
            mock.patch.object(
                self.repair,
                "_open_state_directory",
                side_effect=recording_open_state_directory,
            ),
            mock.patch.object(
                self.repair,
                "_read_json_record_with_parent",
                side_effect=raise_body_primary,
            ),
            self.record_captured_descriptor_closes(evidence),
        ):
            try:
                self.assert_cleanup_trace_preserves_primary(
                    self.repair.StateDirectory.close.__code__,
                    cleanup_entry,
                    lambda: self.repair._read_json_record(path),
                    body_primary,
                    label="state caller exit cleanup",
                    evidence=evidence,
                )
            finally:
                for handle in handles:
                    handle.close(primary_error=body_primary)

        self.assertIsNone(evidence["state_directory"]._owner)
        self.assert_captured_descriptor_closed_once(evidence)
        self.assertEqual(baseline, self.open_fd_set())

    def test_directory_walk_close_failure_drains_next_fd_without_double_close(
        self,
    ) -> None:
        target = self.root / "directory-walk" / "nested"
        target.mkdir(parents=True)
        baseline = self.open_fd_set()
        opened: List[int] = []
        close_calls: List[int] = []
        real_open = self.repair.os.open
        real_close = self.repair.os.close

        def recording_open(*arguments: Any, **keywords: Any) -> int:
            descriptor = real_open(*arguments, **keywords)
            opened.append(descriptor)
            return descriptor

        def fail_first_close(descriptor: int) -> None:
            close_calls.append(descriptor)
            real_close(descriptor)
            if len(close_calls) == 1:
                raise OSError(errno.EIO, "injected directory component close failure")

        with (
            mock.patch.object(self.repair.os, "open", side_effect=recording_open),
            mock.patch.object(self.repair.os, "close", side_effect=fail_first_close),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            with self.repair._open_directory_nofollow(
                target, create=False, private_final=False
            ):
                pass

        self.assertEqual(2, len(opened))
        self.assertCountEqual(opened, close_calls)
        self.assertEqual(len(close_calls), len(set(close_calls)))
        self.assertEqual(baseline, self.open_fd_set())

    def test_directory_walk_trace_handoffs_close_all_owned_descriptors(
        self,
    ) -> None:
        for boundary in ("root-enter-return", "child-before-registration"):
            with self.subTest(boundary=boundary):
                target = self.root / f"directory-walk-{boundary}" / "nested"
                target.mkdir(parents=True)
                walker = self.repair._open_directory_nofollow_owned(
                    target, create=False, private_final=False
                )
                created: List[Any] = []
                captured_owners: List[Any] = []
                captured_fds: Dict[int, tuple[int, int]] = {}
                close_calls: List[int] = []
                baseline = self.open_fd_set()
                real_factory = self.repair._open_fd_owned
                real_close = self.repair.os.close

                def recording_factory(*arguments: Any, **keywords: Any) -> Any:
                    owner = real_factory(*arguments, **keywords)
                    created.append(owner)
                    return owner

                def capture(owners: List[Any], evidence: Dict[str, Any]) -> None:
                    captured_owners.extend(owners)
                    evidence["owners"] = tuple(owners)
                    for owner in owners:
                        descriptor = owner.fileno()
                        metadata = os.fstat(descriptor)
                        captured_fds[descriptor] = (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                    evidence["fds"] = tuple(captured_fds)

                def recording_close(descriptor: int) -> None:
                    if descriptor in captured_fds:
                        metadata = os.fstat(descriptor)
                        self.assertEqual(
                            captured_fds[descriptor],
                            (metadata.st_dev, metadata.st_ino),
                        )
                        close_calls.append(descriptor)
                    real_close(descriptor)

                if boundary == "root-enter-return":

                    def handoff_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
                        owner = frame.f_locals.get("self")
                        if (
                            not created
                            or owner is not created[0]
                            or owner.closed
                            or owner._subject != "filesystem root"
                        ):
                            return False
                        capture([owner], evidence)
                        return True

                    target_code = self.repair._OwnedDescriptor.__enter__.__code__
                    events = ("return",)
                else:

                    def handoff_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
                        current_owner = frame.f_locals.get("current_owner")
                        next_owner = frame.f_locals.get("next_owner")
                        if (
                            not isinstance(current_owner, self.repair._OwnedDescriptor)
                            or not isinstance(next_owner, self.repair._OwnedDescriptor)
                            or current_owner is next_owner
                            or current_owner.closed
                            or next_owner.closed
                            or current_owner._subject != "filesystem root"
                            or frame.f_locals.get("current_fd")
                            != current_owner.fileno()
                            or frame.f_locals.get("next_fd") != next_owner.fileno()
                            or "previous_owner" in frame.f_locals
                        ):
                            return False
                        capture([current_owner, next_owner], evidence)
                        return True

                    target_code = walker._acquire.__code__
                    events = ("line",)

                immediate_closed: tuple[bool, ...] = ()
                immediate_close_calls: tuple[int, ...] = ()
                evidence: Dict[str, Any] = {}
                with (
                    mock.patch.object(
                        self.repair,
                        "_open_fd_owned",
                        side_effect=recording_factory,
                    ),
                    mock.patch.object(
                        self.repair.os, "close", side_effect=recording_close
                    ),
                ):
                    try:
                        evidence = self.assert_trace_interruption(
                            target_code,
                            handoff_boundary,
                            lambda: walker.__enter__(),
                            label=f"directory walk {boundary}",
                            events=events,
                        )
                        immediate_closed = tuple(
                            owner.closed for owner in evidence["owners"]
                        )
                        immediate_close_calls = tuple(close_calls)
                    finally:
                        primary = evidence.get("primary")
                        for owner in created:
                            owner.close(primary_error=primary)
                        walker.close(primary_error=primary)

                self.assertTrue(all(immediate_closed))
                for descriptor in evidence["fds"]:
                    self.assertEqual(
                        1,
                        immediate_close_calls.count(descriptor),
                    )
                self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_close_failure_preserves_publication_and_drains_parent(
        self,
    ) -> None:
        cases = ("temporary-close", "write-interrupt")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"close-drain-{index}"
                )
                path = config.queue_path
                baseline = self.open_fd_set()
                parent_fds: List[int] = []
                temporary_fd: Optional[int] = None
                temporary_identity: Optional[tuple[int, int]] = None
                close_calls: List[int] = []
                closed_identities: List[tuple[int, int]] = []
                failed_close = False
                sentinel = KeyboardInterrupt("injected durable write interrupt")
                real_state_directory_enter = self.repair.StateDirectory.__enter__
                real_open = self.repair.os.open
                real_close = self.repair.os.close
                real_write = self.repair.os.write

                def recording_state_directory_enter(handle: Any) -> Any:
                    entered = real_state_directory_enter(handle)
                    parent_fds.append(handle.descriptor)
                    return entered

                def recording_open(*arguments: Any, **keywords: Any) -> int:
                    nonlocal temporary_fd, temporary_identity
                    descriptor = real_open(*arguments, **keywords)
                    target = arguments[0]
                    if isinstance(target, str) and target.startswith(
                        f".{path.name}.tmp."
                    ):
                        temporary_fd = descriptor
                        metadata = os.fstat(descriptor)
                        temporary_identity = (metadata.st_dev, metadata.st_ino)
                    return descriptor

                def injected_write(descriptor: int, value: bytes) -> int:
                    if case == "write-interrupt" and descriptor == temporary_fd:
                        raise sentinel
                    return real_write(descriptor, value)

                def flaky_close(descriptor: int) -> None:
                    nonlocal failed_close
                    close_calls.append(descriptor)
                    metadata = os.fstat(descriptor)
                    closed_identities.append((metadata.st_dev, metadata.st_ino))
                    real_close(descriptor)
                    if descriptor == temporary_fd and not failed_close:
                        failed_close = True
                        raise OSError(errno.EIO, "injected temporary close failure")

                with (
                    mock.patch.object(
                        self.repair.StateDirectory,
                        "__enter__",
                        autospec=True,
                        side_effect=recording_state_directory_enter,
                    ),
                    mock.patch.object(
                        self.repair.os, "open", side_effect=recording_open
                    ),
                    mock.patch.object(
                        self.repair.os, "write", side_effect=injected_write
                    ),
                    mock.patch.object(self.repair.os, "close", side_effect=flaky_close),
                    self.assertRaises(BaseException) as raised,
                ):
                    self.repair.atomic_write_json(
                        path, {"version": 1, "entries": [UUID_A]}
                    )

                self.assertIsNotNone(temporary_fd)
                self.assertIsNotNone(temporary_identity)
                self.assertTrue(failed_close)
                if case == "write-interrupt":
                    self.assertIsInstance(
                        raised.exception, self.repair.AtomicWriteInterruption
                    )
                    self.assertIs(sentinel, raised.exception.cause)
                else:
                    self.assertIsInstance(
                        raised.exception, self.repair.AtomicWriteError
                    )
                self.assertEqual(
                    self.repair.AtomicPublication.NOT_PUBLISHED,
                    raised.exception.publication,
                )
                self.assertEqual(1, closed_identities.count(temporary_identity))
                self.assertTrue(parent_fds)
                self.assertIn(parent_fds[-1], close_calls)
                self.assertFalse(path.exists())
                self.assertEqual([], list(path.parent.glob(f".{path.name}.tmp.*")))
                self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_durable_success_is_not_reversed_by_parent_close_failure(
        self,
    ) -> None:
        path = self.config().queue_path
        payload = {"version": 1, "entries": [UUID_A]}
        baseline = self.open_fd_set()
        parent_fd: Optional[int] = None
        failed_close = False
        real_state_directory_enter = self.repair.StateDirectory.__enter__
        real_close = self.repair.os.close

        def recording_state_directory_enter(handle: Any) -> Any:
            nonlocal parent_fd
            entered = real_state_directory_enter(handle)
            parent_fd = handle.descriptor
            return entered

        def fail_parent_close(descriptor: int) -> None:
            nonlocal failed_close
            real_close(descriptor)
            if descriptor == parent_fd and not failed_close:
                failed_close = True
                raise OSError(errno.EIO, "injected parent close failure")

        with (
            mock.patch.object(
                self.repair.StateDirectory,
                "__enter__",
                autospec=True,
                side_effect=recording_state_directory_enter,
            ),
            mock.patch.object(self.repair.os, "close", side_effect=fail_parent_close),
        ):
            fence = self.repair.atomic_write_json(path, payload)

        self.assertTrue(failed_close)
        self.assertIsNotNone(parent_fd)
        self.assertEqual(path.absolute(), fence.path)
        self.assertEqual(payload, self.repair._read_json(path))
        self.assertEqual(baseline, self.open_fd_set())

    @unittest.skipUnless(sys.platform == "darwin", "requires Darwin ACL semantics")
    def test_state_directory_and_canonical_file_acls_are_cleared_on_held_fds(
        self,
    ) -> None:
        config = self.config()
        config.state_root.mkdir(parents=True, mode=0o750)
        config.state_root.chmod(0o750)
        subprocess.run(
            [
                "/bin/chmod",
                "+a",
                "everyone allow readattr",
                str(config.state_root),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.repair.atomic_write_json(
            config.queue_path, {"version": 1, "entries": [UUID_A]}
        )

        self.assertEqual(0o700, config.state_root.stat().st_mode & 0o777)
        self.assertNotIn(
            "group:everyone",
            subprocess.check_output(
                ["/bin/ls", "-lde", str(config.state_root)], text=True
            ),
        )
        self.assertEqual(0o600, config.queue_path.stat().st_mode & 0o777)

        config.queue_path.chmod(0o640)
        subprocess.run(
            [
                "/bin/chmod",
                "+a",
                "everyone allow readattr",
                str(config.queue_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(
            {"version": 1, "entries": [UUID_A]},
            self.repair._read_json(config.queue_path),
        )
        self.assertEqual(0o600, config.queue_path.stat().st_mode & 0o777)
        self.assertNotIn(
            "group:everyone",
            subprocess.check_output(
                ["/bin/ls", "-le", str(config.queue_path)], text=True
            ),
        )

    def test_atomic_json_write_is_private_and_failure_preserves_authoritative_bytes(
        self,
    ) -> None:
        path = self.config().queue_path
        self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
        original = path.read_bytes()
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

        with mock.patch.object(
            self.repair.os, "replace", side_effect=OSError("injected replace failure")
        ):
            with self.assertRaises(self.repair.FatalRepairError):
                self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_B]})

        self.assertEqual(original, path.read_bytes())

    def test_atomic_success_binds_canonical_inode_and_exact_payload(self) -> None:
        config = self.config()
        path = config.queue_path
        payload = {"version": 1, "entries": [UUID_A]}
        expected = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        temporary_identity: Optional[tuple[int, int]] = None
        real_open = self.repair.os.open

        def recording_open(
            target: Any, flags: int, mode: int = 0o777, **keywords: Any
        ) -> int:
            nonlocal temporary_identity
            descriptor = real_open(target, flags, mode, **keywords)
            if isinstance(target, str) and target.startswith(f".{path.name}.tmp."):
                metadata = os.fstat(descriptor)
                temporary_identity = (metadata.st_dev, metadata.st_ino)
            return descriptor

        with mock.patch.object(self.repair.os, "open", side_effect=recording_open):
            fence = self.repair.atomic_write_json(path, payload)

        self.assertIsNotNone(temporary_identity)
        metadata = path.stat()
        self.assertEqual(temporary_identity, (metadata.st_dev, metadata.st_ino))
        self.assertEqual(expected, path.read_bytes())
        self.assertEqual(path.absolute(), fence.path)
        self.assertEqual(
            (metadata.st_dev, metadata.st_ino),
            (fence.leaf_identity.device, fence.leaf_identity.inode),
        )
        self.assertEqual(expected, fence.encoded_payload)

    def test_atomic_success_path_replacement_or_payload_mutation_is_ambiguous(
        self,
    ) -> None:
        payload = {"version": 1, "entries": [UUID_A]}
        expected = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        changed = expected.replace(UUID_A.encode(), UUID_B.encode())
        self.assertEqual(len(expected), len(changed))
        for index, case in enumerate(("new-inode", "same-inode-content")):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"atomic-success-{index}",
                )
                path = config.queue_path
                real_replace = self.repair.os.replace
                attacked = False

                def replace_then_attack(
                    source: Any,
                    destination: Any,
                    *arguments: Any,
                    **keywords: Any,
                ) -> Any:
                    nonlocal attacked
                    result = real_replace(source, destination, *arguments, **keywords)
                    if destination == path.name and not attacked:
                        attacked = True
                        if case == "new-inode":
                            replacement = path.with_name(f".{path.name}.attacker")
                            replacement.write_bytes(expected)
                            replacement.chmod(0o600)
                            real_replace(replacement, path)
                        else:
                            with path.open("r+b", buffering=0) as stream:
                                stream.write(changed)
                                stream.flush()
                                os.fsync(stream.fileno())
                    return result

                with (
                    mock.patch.object(
                        self.repair.os, "replace", side_effect=replace_then_attack
                    ),
                    self.assertRaises(self.repair.AtomicWriteError) as raised,
                ):
                    self.repair.atomic_write_json(path, payload)

                self.assertTrue(attacked)
                self.assertEqual(
                    self.repair.AtomicPublication.AMBIGUOUS,
                    raised.exception.publication,
                )

    def test_atomic_pre_replace_failures_clean_identity_bound_temporary(self) -> None:
        cases = tuple(
            (stage, interruption)
            for stage in ("harden", "write", "file-fsync")
            for interruption in (False, True)
        )
        for index, (failure_stage, interruption) in enumerate(cases):
            with self.subTest(stage=failure_stage, interruption=interruption):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"atomic-{index}"
                )
                path = config.queue_path
                self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
                original = path.read_bytes()
                injected: BaseException = (
                    KeyboardInterrupt(f"injected {failure_stage} interrupt")
                    if interruption
                    else OSError(errno.EIO, f"injected {failure_stage} failure")
                )
                real_harden = self.repair._harden_private_state_fd
                real_write = self.repair.os.write
                real_fsync = self.repair.os.fsync

                def harden(descriptor: int, *, is_directory: bool) -> None:
                    if failure_stage == "harden" and not is_directory:
                        raise injected
                    real_harden(descriptor, is_directory=is_directory)

                def write(descriptor: int, value: bytes) -> int:
                    if failure_stage == "write":
                        raise injected
                    return real_write(descriptor, value)

                def fsync(descriptor: int) -> None:
                    metadata = os.fstat(descriptor)
                    if failure_stage == "file-fsync" and stat.S_ISREG(metadata.st_mode):
                        raise injected
                    real_fsync(descriptor)

                with (
                    mock.patch.object(
                        self.repair,
                        "_harden_private_state_fd",
                        side_effect=harden,
                    ),
                    mock.patch.object(self.repair.os, "write", side_effect=write),
                    mock.patch.object(self.repair.os, "fsync", side_effect=fsync),
                    self.assertRaises(BaseException) as raised,
                ):
                    self.repair.atomic_write_json(
                        path, {"version": 1, "entries": [UUID_B]}
                    )

                error = raised.exception
                self.assertIsInstance(
                    error,
                    self.repair.AtomicWriteInterruption
                    if interruption
                    else self.repair.AtomicWriteError,
                )
                self.assertEqual(
                    self.repair.AtomicPublication.NOT_PUBLISHED,
                    error.publication,
                )
                self.assertEqual(original, path.read_bytes())
                self.assertEqual([], list(path.parent.glob(f".{path.name}.tmp.*")))

    def test_atomic_adopt_hook_trace_registers_identity_before_cleanup(self) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "atomic-adopt-hook"
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        evidence: Dict[str, Any] = {}
        unlink_records: List[tuple[str, int, tuple[int, int]]] = []
        replace_calls: List[tuple[Any, ...]] = []
        real_unlink = self.repair.os.unlink
        real_replace = self.repair.os.replace
        capture_code = next(
            value
            for value in self.repair._atomic_write_json_with_parent.__code__.co_consts
            if getattr(value, "co_name", None) == "capture_temporary_identity"
        )
        probe = self.repair._open_fd_owned(
            os.devnull, os.O_RDONLY, subject="atomic adopt hook code probe"
        )
        acquire_code = probe._acquire.__code__

        def record_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append(
                (
                    name,
                    parent_fd,
                    (metadata.st_dev, metadata.st_ino),
                )
            )
            real_unlink(name, *arguments, **keywords)

        def record_replace(*arguments: Any, **keywords: Any) -> None:
            replace_calls.append(arguments)
            real_replace(*arguments, **keywords)

        def adopt_hook_before_receipt(frame: Any, captured: Dict[str, Any]) -> bool:
            caller = frame.f_back
            owner = frame.f_locals.get("owner")
            if (
                caller is None
                or caller.f_code is not acquire_code
                or caller.f_locals.get("target") is not owner
                or not isinstance(owner, self.repair._OwnedDescriptor)
                or owner.closed
                or caller.f_locals.get("handed_off", -1) != owner.fileno()
                or caller.f_locals.get("adopt_hook_complete") is not False
                or frame.f_locals.get("temporary_identity") is not None
                or not owner._subject.startswith(f"atomic temporary '.{path.name}.tmp.")
                or "os.fstat(owner.fileno())"
                not in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            ):
                return False
            atomic_frame = caller.f_back
            while (
                atomic_frame is not None
                and atomic_frame.f_code
                is not self.repair._atomic_write_json_with_parent.__code__
            ):
                atomic_frame = atomic_frame.f_back
            if (
                atomic_frame is None
                or atomic_frame.f_locals.get("descriptor_owner") is not owner
                or atomic_frame.f_locals.get("temporary_identity") is not None
                or atomic_frame.f_locals.get("replace_attempted") is not False
                or atomic_frame.f_locals.get("replace_returned") is not False
            ):
                return False
            receipt = atomic_frame.f_locals.get("receipt")
            if (
                receipt is None
                or receipt.publication
                is not self.repair.AtomicPublication.NOT_PUBLISHED
            ):
                return False
            self.capture_owned_descriptor(captured, owner)
            captured["receipt"] = receipt
            captured["temporary_name"] = atomic_frame.f_locals["temporary_name"]
            captured["parent_fd"] = atomic_frame.f_locals["parent_fd"]
            captured["hook_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteInterruption)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            self.assertIs(caught.cause, caught.__cause__)
            return caught.cause

        with (
            self.record_captured_descriptor_closes(evidence),
            mock.patch.object(self.repair.os, "unlink", side_effect=record_unlink),
            mock.patch.object(self.repair.os, "replace", side_effect=record_replace),
        ):
            self.assert_trace_interruption(
                capture_code,
                adopt_hook_before_receipt,
                lambda: self.repair.atomic_write_json(path, replacement_payload),
                label="atomic adopt hook identity receipt",
                events=("line",),
                unwrap_caught=unwrap_atomic,
                evidence=evidence,
            )

        self.assertIsInstance(evidence["hook_line"], int)
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["receipt"].publication,
        )
        self.assertEqual([], replace_calls)
        self.assertEqual(
            [
                (
                    evidence["temporary_name"],
                    evidence["parent_fd"],
                    evidence["descriptor_identity"],
                )
            ],
            unlink_records,
        )
        self.assertEqual(original, path.read_bytes())
        metadata = path.stat()
        self.assertEqual(original_identity, (metadata.st_dev, metadata.st_ino))
        self.assertEqual([], list(path.parent.glob(f".{path.name}.tmp.*")))
        self.assert_captured_descriptor_closed_once(evidence)
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_temporary_unlink_trace_retries_identity_bound_cleanup(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "atomic-unlink-trace"
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        body_primary = OSError(errno.EIO, "atomic write body primary")
        evidence: Dict[str, Any] = {}
        unlink_records: List[tuple[str, int, tuple[int, int]]] = []
        real_unlink = self.repair.os.unlink

        def fail_write(_descriptor: int, _value: bytes) -> int:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def record_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            real_unlink(name, *arguments, **keywords)

        def unlink_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            source = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            identity = frame.f_locals.get("identity")
            name = frame.f_locals.get("name")
            parent_fd = frame.f_locals.get("parent_fd")
            if (
                "os.unlink(name, dir_fd=parent_fd)" not in source
                or not isinstance(name, str)
                or not name.startswith(f".{path.name}.tmp.")
                or not isinstance(parent_fd, int)
                or not isinstance(identity, tuple)
            ):
                return False
            atomic_frame = frame.f_back
            while (
                atomic_frame is not None
                and atomic_frame.f_code
                is not self.repair._atomic_write_json_with_parent.__code__
            ):
                atomic_frame = atomic_frame.f_back
            if atomic_frame is None:
                return False
            receipt = atomic_frame.f_locals.get("receipt")
            if receipt is None:
                return False
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            captured["temporary_name"] = name
            captured["parent_fd"] = parent_fd
            captured["temporary_identity"] = identity
            captured["observed_identity"] = (metadata.st_dev, metadata.st_ino)
            captured["receipt"] = receipt
            captured["unlink_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteError)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            return caught.__cause__

        with (
            mock.patch.object(self.repair.os, "write", side_effect=fail_write),
            mock.patch.object(self.repair.os, "unlink", side_effect=record_unlink),
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._unlink_atomic_temporary_once.__code__,
                unlink_boundary,
                lambda: self.repair.atomic_write_json(path, replacement_payload),
                body_primary,
                label="atomic identity-bound temporary unlink",
                events=("line",),
                evidence=evidence,
                unwrap_caught=unwrap_atomic,
            )

        self.assertIsInstance(evidence["unlink_line"], int)
        self.assertEqual(evidence["temporary_identity"], evidence["observed_identity"])
        self.assertEqual(
            [
                (
                    evidence["temporary_name"],
                    evidence["parent_fd"],
                    evidence["temporary_identity"],
                )
            ],
            unlink_records,
        )
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["receipt"].publication,
        )
        self.assertEqual([], list(path.parent.glob(f".{path.name}.tmp.*")))
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_replace_failure_post_probe_trace_finally_unlinks_temporary(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "atomic-replace-probe-trace"
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        replace_error = OSError(errno.EIO, "atomic replace returned without mutation")
        evidence: Dict[str, Any] = {}
        replace_calls: List[Tuple[Any, ...]] = []
        unlink_records: List[Tuple[str, int, Tuple[int, int]]] = []
        real_unlink = self.repair.os.unlink

        def fail_replace(*arguments: Any, **_keywords: Any) -> None:
            replace_calls.append(arguments)
            try:
                raise replace_error
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def record_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            real_unlink(name, *arguments, **keywords)

        def post_probe_cleanup_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            receipt = frame.f_locals.get("receipt")
            cleanup_receipt = frame.f_locals.get("temporary_cleanup_receipt")
            temporary_name = frame.f_locals.get("temporary_name")
            parent_fd = frame.f_locals.get("parent_fd")
            temporary_identity = frame.f_locals.get("temporary_identity")
            if (
                source != "candidate = _complete_cleanup_action("
                or frame.f_locals.get("error") is not replace_error
                or frame.f_locals.get("primary_error") is not replace_error
                or frame.f_locals.get("replace_attempted") is not True
                or frame.f_locals.get("replace_returned") is not False
                or frame.f_locals.get("directory_synced") is not False
                or frame.f_locals.get("mapping_error") is not None
                or frame.f_locals.get("publication")
                is not self.repair.AtomicPublication.NOT_PUBLISHED
                or receipt is None
                or receipt.publication
                is not self.repair.AtomicPublication.NOT_PUBLISHED
                or cleanup_receipt is None
                or cleanup_receipt.dispatches != 0
                or cleanup_receipt.completed
                or cleanup_receipt.error is not None
                or not isinstance(temporary_name, str)
                or not temporary_name.startswith(f".{path.name}.tmp.")
                or not isinstance(parent_fd, int)
                or not isinstance(temporary_identity, tuple)
            ):
                return False
            metadata = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            observed_identity = (metadata.st_dev, metadata.st_ino)
            if observed_identity != temporary_identity:
                return False
            if path.read_bytes() != original_bytes:
                return False
            current = path.stat()
            if (current.st_dev, current.st_ino) != original_identity:
                return False
            captured["publication_receipt"] = receipt
            captured["cleanup_receipt"] = cleanup_receipt
            captured["temporary_name"] = temporary_name
            captured["parent_fd"] = parent_fd
            captured["temporary_identity"] = temporary_identity
            captured["cleanup_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteError)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            self.assertIs(replace_error, caught.__cause__)
            return caught.__cause__

        temporary_paths_before_fallback: Tuple[pathlib.Path, ...] = ()
        try:
            with (
                mock.patch.object(self.repair.os, "replace", side_effect=fail_replace),
                mock.patch.object(self.repair.os, "unlink", side_effect=record_unlink),
            ):
                self.assert_cleanup_trace_preserves_primary(
                    self.repair._atomic_write_json_with_parent.__code__,
                    post_probe_cleanup_boundary,
                    lambda: self.repair.atomic_write_json(path, replacement_payload),
                    replace_error,
                    label="atomic replace failure post-probe cleanup",
                    events=("line",),
                    evidence=evidence,
                    unwrap_caught=unwrap_atomic,
                )
                temporary_paths_before_fallback = tuple(
                    path.parent.glob(f".{path.name}.tmp.*")
                )
        finally:
            for temporary_path in path.parent.glob(f".{path.name}.tmp.*"):
                temporary_path.unlink()

        cleanup_receipt = evidence["cleanup_receipt"]
        self.assertIsInstance(evidence["cleanup_line"], int)
        self.assertEqual(1, len(replace_calls))
        self.assertEqual(1, cleanup_receipt.dispatches)
        self.assertTrue(cleanup_receipt.completed)
        self.assertIsNone(cleanup_receipt.error)
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["publication_receipt"].publication,
        )
        self.assertEqual(
            [
                (
                    evidence["temporary_name"],
                    evidence["parent_fd"],
                    evidence["temporary_identity"],
                )
            ],
            unlink_records,
        )
        self.assertEqual((), temporary_paths_before_fallback)
        self.assertIn(
            "publication classification was interrupted", str(evidence["caught"])
        )
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def _assert_atomic_cleanup_error_survives_completion_trace(
        self, boundary: str
    ) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / f"atomic-cleanup-completion-{boundary}",
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        body_primary = OSError(errno.EIO, "atomic body before temporary cleanup")
        cleanup_error = OSError(errno.EIO, "atomic temporary cleanup failed")
        evidence: Dict[str, Any] = {}
        unlink_records: List[Tuple[str, int, Tuple[int, int]]] = []

        def fail_write(_descriptor: int, _value: bytes) -> int:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def fail_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            raise cleanup_error

        def completion_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            receipt = frame.f_locals.get("receipt")
            action = frame.f_locals.get("action")
            if (
                source
                != (
                    "_cleanup_guard = True"
                    if boundary == "handler"
                    else "if receipt.completed:"
                )
                or (
                    boundary == "handler"
                    and frame.f_locals.get("cleanup_error") is not cleanup_error
                )
                or frame.f_locals.get("first_cleanup_error")
                is not (None if boundary == "handler" else cleanup_error)
                or not isinstance(receipt, self.repair._CleanupActionReceipt)
                or receipt.dispatches != 1
                or receipt.completed
                or receipt.error is not cleanup_error
                or not callable(action)
                or getattr(action, "__name__", None) != "cleanup_temporary"
            ):
                return False
            atomic_frame = frame.f_back
            if (
                atomic_frame is None
                or atomic_frame.f_code
                is not self.repair._atomic_write_json_with_parent.__code__
                or atomic_frame.f_locals.get("temporary_cleanup_receipt") is not receipt
                or atomic_frame.f_locals.get("error") is not body_primary
                or atomic_frame.f_locals.get("replace_attempted") is not False
                or atomic_frame.f_locals.get("receipt").publication
                is not self.repair.AtomicPublication.NOT_PUBLISHED
            ):
                return False
            temporary_name = atomic_frame.f_locals.get("temporary_name")
            parent_fd = atomic_frame.f_locals.get("parent_fd")
            temporary_identity = atomic_frame.f_locals.get("temporary_identity")
            if (
                not isinstance(temporary_name, str)
                or not temporary_name.startswith(f".{path.name}.tmp.")
                or not isinstance(parent_fd, int)
                or not isinstance(temporary_identity, tuple)
                or unlink_records
                != [
                    (temporary_name, parent_fd, temporary_identity),
                    (temporary_name, parent_fd, temporary_identity),
                ]
            ):
                return False
            captured["cleanup_receipt"] = receipt
            captured["temporary_name"] = temporary_name
            captured["parent_fd"] = parent_fd
            captured["temporary_identity"] = temporary_identity
            captured["completion_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteError)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            self.assertIs(body_primary, caught.__cause__)
            return caught.__cause__

        temporary_paths_before_fallback: Tuple[pathlib.Path, ...] = ()
        retained_identity_before_fallback: Optional[Tuple[int, int]] = None
        try:
            with (
                mock.patch.object(self.repair.os, "write", side_effect=fail_write),
                mock.patch.object(self.repair.os, "unlink", side_effect=fail_unlink),
            ):
                self.assert_cleanup_trace_preserves_primary(
                    self.repair._complete_cleanup_action.__code__,
                    completion_handler_boundary,
                    lambda: self.repair.atomic_write_json(path, replacement_payload),
                    body_primary,
                    label=f"atomic cleanup completion {boundary}",
                    events=("line",),
                    evidence=evidence,
                    unwrap_caught=unwrap_atomic,
                )
                temporary_paths_before_fallback = tuple(
                    path.parent.glob(f".{path.name}.tmp.*")
                )
                if len(temporary_paths_before_fallback) == 1:
                    retained = temporary_paths_before_fallback[0].stat()
                    retained_identity_before_fallback = (
                        retained.st_dev,
                        retained.st_ino,
                    )
        finally:
            for temporary_path in path.parent.glob(f".{path.name}.tmp.*"):
                temporary_path.unlink()

        cleanup_receipt = evidence["cleanup_receipt"]
        self.assertIsInstance(evidence["completion_line"], int)
        self.assertEqual(1, cleanup_receipt.dispatches)
        self.assertFalse(cleanup_receipt.completed)
        self.assertIs(cleanup_error, cleanup_receipt.error)
        expected_unlink = (
            evidence["temporary_name"],
            evidence["parent_fd"],
            evidence["temporary_identity"],
        )
        self.assertEqual([expected_unlink, expected_unlink], unlink_records)
        self.assertEqual(1, len(temporary_paths_before_fallback))
        self.assertEqual(
            evidence["temporary_identity"],
            retained_identity_before_fallback,
        )
        self.assertIn("temporary cleanup was not proved", str(evidence["caught"]))
        self.assertIn(str(cleanup_error), str(evidence["caught"]))
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_cleanup_error_survives_completion_handler_trace(self) -> None:
        self._assert_atomic_cleanup_error_survives_completion_trace("handler")

    def test_atomic_cleanup_error_survives_completion_return_trace(self) -> None:
        self._assert_atomic_cleanup_error_survives_completion_trace("return")

    def test_atomic_interruption_exposes_temporary_cleanup_failure(self) -> None:
        config = dataclasses.replace(
            self.config(),
            state_root=self.state_root / "atomic-interruption-cleanup-failure",
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        body_primary = KeyboardInterrupt("atomic body interruption")
        cleanup_error = OSError(errno.EIO, "atomic interruption cleanup failed")
        evidence: Dict[str, Any] = {}
        unlink_records: List[Tuple[str, int, Tuple[int, int]]] = []
        completion_results: List[Optional[BaseException]] = []
        real_complete = self.repair._complete_cleanup_action

        def interrupt_write(_descriptor: int, _value: bytes) -> int:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def fail_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            raise cleanup_error

        def record_completion(receipt: Any, action: Any) -> Optional[BaseException]:
            result = real_complete(receipt, action)
            if getattr(action, "__name__", None) == "cleanup_temporary":
                evidence["cleanup_receipt"] = receipt
                completion_results.append(result)
            return result

        temporary_paths_before_fallback: Tuple[pathlib.Path, ...] = ()
        retained_identity_before_fallback: Optional[Tuple[int, int]] = None
        try:
            with (
                mock.patch.object(
                    self.repair.os,
                    "write",
                    side_effect=interrupt_write,
                ),
                mock.patch.object(
                    self.repair.os,
                    "unlink",
                    side_effect=fail_unlink,
                ),
                mock.patch.object(
                    self.repair,
                    "_complete_cleanup_action",
                    side_effect=record_completion,
                ),
            ):
                with self.assertRaises(self.repair.AtomicWriteInterruption) as raised:
                    self.repair.atomic_write_json(path, replacement_payload)
                caught = raised.exception
                temporary_paths_before_fallback = tuple(
                    path.parent.glob(f".{path.name}.tmp.*")
                )
                if len(temporary_paths_before_fallback) == 1:
                    retained = temporary_paths_before_fallback[0].stat()
                    retained_identity_before_fallback = (
                        retained.st_dev,
                        retained.st_ino,
                    )
        finally:
            for temporary_path in path.parent.glob(f".{path.name}.tmp.*"):
                temporary_path.unlink()

        cleanup_receipt = evidence["cleanup_receipt"]
        self.assertIs(body_primary, caught.cause)
        self.assertIs(body_primary, caught.__cause__)
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            caught.publication,
        )
        self.assertEqual(caught.detail, str(caught))
        self.assertIn("temporary cleanup was not proved", caught.detail)
        self.assertIn(str(cleanup_error), caught.detail)
        self.assertIs(cleanup_error, caught.cleanup_error)
        self.assertIs(cleanup_receipt, caught.cleanup_receipt)
        self.assertIs(caught, body_primary._atomic_write_interruption)
        self.assertEqual(1, cleanup_receipt.dispatches)
        self.assertFalse(cleanup_receipt.completed)
        self.assertIs(cleanup_error, cleanup_receipt.error)
        self.assertEqual([cleanup_error, cleanup_error], completion_results)
        self.assertEqual(2, len(unlink_records))
        self.assertEqual([unlink_records[0], unlink_records[0]], unlink_records)
        self.assertEqual(1, len(temporary_paths_before_fallback))
        self.assertEqual(unlink_records[0][0], temporary_paths_before_fallback[0].name)
        self.assertEqual(unlink_records[0][2], retained_identity_before_fallback)
        body_origin = evidence["body_origin_traceback"]
        traceback = body_primary.__traceback__
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(body_origin, traceback_nodes)
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_unlink_second_attempt_trace_retries_outer_receipt(self) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "atomic-unlink-second-trace"
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        body_primary = OSError(errno.EIO, "atomic write before unlink retry")
        first_unlink_error = OSError(
            errno.EIO, "first identity-bound temporary unlink failed"
        )
        evidence: Dict[str, Any] = {}
        unlink_records: List[Tuple[str, int, Tuple[int, int]]] = []
        replace_calls: List[Tuple[Any, ...]] = []
        real_unlink = self.repair.os.unlink

        def fail_write(_descriptor: int, _value: bytes) -> int:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def record_replace(*arguments: Any, **_keywords: Any) -> None:
            replace_calls.append(arguments)

        def fail_first_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            if len(unlink_records) == 1:
                try:
                    raise first_unlink_error
                except BaseException as error:
                    evidence["first_unlink_origin_traceback"] = error.__traceback__
                    raise
            real_unlink(name, *arguments, **keywords)

        def second_attempt_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_unlink_atomic_temporary_once(parent_fd, name, identity)"
                or frame.f_locals.get("first_cleanup_error") is not first_unlink_error
                or frame.f_locals.get("_cleanup_guard") is not True
            ):
                return False
            name = frame.f_locals.get("name")
            parent_fd = frame.f_locals.get("parent_fd")
            identity = frame.f_locals.get("identity")
            if (
                not isinstance(name, str)
                or not name.startswith(f".{path.name}.tmp.")
                or not isinstance(parent_fd, int)
                or not isinstance(identity, tuple)
                or len(unlink_records) != 1
            ):
                return False
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            observed_identity = (metadata.st_dev, metadata.st_ino)
            if observed_identity != identity or unlink_records[0][2] != identity:
                return False
            atomic_frame = frame.f_back
            while (
                atomic_frame is not None
                and atomic_frame.f_code
                is not self.repair._atomic_write_json_with_parent.__code__
            ):
                atomic_frame = atomic_frame.f_back
            if atomic_frame is None:
                return False
            cleanup_receipt = atomic_frame.f_locals.get("temporary_cleanup_receipt")
            publication_receipt = atomic_frame.f_locals.get("receipt")
            if (
                cleanup_receipt is None
                or cleanup_receipt.dispatches != 1
                or cleanup_receipt.completed
                or cleanup_receipt.error is not None
                or publication_receipt is None
                or publication_receipt.publication
                is not self.repair.AtomicPublication.NOT_PUBLISHED
            ):
                return False
            captured["cleanup_receipt"] = cleanup_receipt
            captured["publication_receipt"] = publication_receipt
            captured["temporary_name"] = name
            captured["parent_fd"] = parent_fd
            captured["temporary_identity"] = identity
            captured["retry_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteError)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            self.assertIs(body_primary, caught.__cause__)
            return caught.__cause__

        temporary_paths_before_fallback: Tuple[pathlib.Path, ...] = ()
        try:
            with (
                mock.patch.object(self.repair.os, "write", side_effect=fail_write),
                mock.patch.object(
                    self.repair.os,
                    "replace",
                    side_effect=record_replace,
                ),
                mock.patch.object(
                    self.repair.os,
                    "unlink",
                    side_effect=fail_first_unlink,
                ),
            ):
                self.assert_cleanup_trace_preserves_primary(
                    self.repair._unlink_atomic_temporary.__code__,
                    second_attempt_boundary,
                    lambda: self.repair.atomic_write_json(path, replacement_payload),
                    body_primary,
                    label="atomic unlink second identity attempt",
                    events=("line",),
                    evidence=evidence,
                    unwrap_caught=unwrap_atomic,
                )
                temporary_paths_before_fallback = tuple(
                    path.parent.glob(f".{path.name}.tmp.*")
                )
        finally:
            for temporary_path in path.parent.glob(f".{path.name}.tmp.*"):
                temporary_path.unlink()

        cleanup_receipt = evidence["cleanup_receipt"]
        self.assertIsInstance(evidence["retry_line"], int)
        self.assertEqual([], replace_calls)
        self.assertEqual(2, cleanup_receipt.dispatches)
        self.assertTrue(cleanup_receipt.completed)
        self.assertIsNone(cleanup_receipt.error)
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["publication_receipt"].publication,
        )
        self.assertEqual(2, len(unlink_records))
        self.assertEqual(
            [evidence["temporary_identity"], evidence["temporary_identity"]],
            [record[2] for record in unlink_records],
        )
        self.assertEqual(
            [evidence["temporary_name"], evidence["temporary_name"]],
            [record[0] for record in unlink_records],
        )
        self.assertEqual((), temporary_paths_before_fallback)
        first_unlink_origin = evidence.get("first_unlink_origin_traceback")
        self.assertIsNotNone(first_unlink_origin)
        traceback = first_unlink_error.__traceback__
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(first_unlink_origin, traceback_nodes)
        self.assertNotIn("temporary cleanup was not proved", str(evidence["caught"]))
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def test_atomic_failure_handler_trace_finally_unlinks_bound_temporary(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "atomic-handler-trace"
        )
        path = config.queue_path
        original_payload = {"version": 1, "entries": [UUID_A]}
        replacement_payload = {"version": 1, "entries": [UUID_B]}
        self.repair.atomic_write_json(path, original_payload)
        original_bytes = path.read_bytes()
        original_metadata = path.stat()
        original_identity = (original_metadata.st_dev, original_metadata.st_ino)
        baseline = self.open_fd_set()
        body_primary = OSError(errno.EIO, "atomic failure handler body primary")
        evidence: Dict[str, Any] = {}
        unlink_records: List[tuple[str, int, tuple[int, int]]] = []
        real_unlink = self.repair.os.unlink

        def fail_write(_descriptor: int, _value: bytes) -> int:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def record_unlink(name: str, *arguments: Any, **keywords: Any) -> None:
            parent_fd = keywords.get("dir_fd")
            self.assertIsInstance(parent_fd, int)
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            unlink_records.append((name, parent_fd, (metadata.st_dev, metadata.st_ino)))
            real_unlink(name, *arguments, **keywords)

        def failure_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("error") is not body_primary
                or frame.f_locals.get("primary_error") is not None
                or frame.f_locals.get("replace_attempted") is not False
                or frame.f_locals.get("replace_returned") is not False
            ):
                return False
            temporary_name = frame.f_locals.get("temporary_name")
            parent_fd = frame.f_locals.get("parent_fd")
            temporary_identity = frame.f_locals.get("temporary_identity")
            receipt = frame.f_locals.get("receipt")
            cleanup_receipt = frame.f_locals.get("temporary_cleanup_receipt")
            if (
                not isinstance(temporary_name, str)
                or not temporary_name.startswith(f".{path.name}.tmp.")
                or not isinstance(parent_fd, int)
                or not isinstance(temporary_identity, tuple)
                or receipt is None
                or receipt.publication
                is not self.repair.AtomicPublication.NOT_PUBLISHED
                or cleanup_receipt is None
                or cleanup_receipt.dispatches != 0
                or cleanup_receipt.completed
                or cleanup_receipt.error is not None
            ):
                return False
            metadata = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            observed_identity = (metadata.st_dev, metadata.st_ino)
            if observed_identity != temporary_identity:
                return False
            captured["temporary_name"] = temporary_name
            captured["parent_fd"] = parent_fd
            captured["temporary_identity"] = temporary_identity
            captured["publication_receipt"] = receipt
            captured["cleanup_receipt"] = cleanup_receipt
            captured["handler_line"] = frame.f_lineno
            return True

        def unwrap_atomic(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.AtomicWriteError)
            self.assertEqual(
                self.repair.AtomicPublication.NOT_PUBLISHED,
                caught.publication,
            )
            self.assertIs(body_primary, caught.__cause__)
            return caught.__cause__

        with (
            mock.patch.object(self.repair.os, "write", side_effect=fail_write),
            mock.patch.object(self.repair.os, "unlink", side_effect=record_unlink),
        ):
            self.assert_cleanup_trace_preserves_primary(
                self.repair._atomic_write_json_with_parent.__code__,
                failure_handler_boundary,
                lambda: self.repair.atomic_write_json(path, replacement_payload),
                body_primary,
                label="atomic failure handler identity cleanup",
                events=("line",),
                evidence=evidence,
                unwrap_caught=unwrap_atomic,
            )

        cleanup_receipt = evidence["cleanup_receipt"]
        self.assertIsInstance(evidence["handler_line"], int)
        self.assertEqual(1, cleanup_receipt.dispatches)
        self.assertTrue(cleanup_receipt.completed)
        self.assertIsNone(cleanup_receipt.error)
        self.assertEqual(
            self.repair.AtomicPublication.NOT_PUBLISHED,
            evidence["publication_receipt"].publication,
        )
        self.assertEqual(
            [
                (
                    evidence["temporary_name"],
                    evidence["parent_fd"],
                    evidence["temporary_identity"],
                )
            ],
            unlink_records,
        )
        self.assertIn(
            "publication classification was interrupted",
            str(evidence["caught"]),
        )
        self.assertEqual([], list(path.parent.glob(f".{path.name}.tmp.*")))
        self.assertEqual(original_bytes, path.read_bytes())
        current = path.stat()
        self.assertEqual(original_identity, (current.st_dev, current.st_ino))
        self.assertEqual(baseline, self.open_fd_set())

    def test_corrupt_json_is_fatal_and_left_byte_for_byte_unchanged(self) -> None:
        path = self.config().queue_path
        path.parent.mkdir(parents=True)
        corrupt = b'{"version":1,"entries":['
        path.write_bytes(corrupt)

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair._read_json(path)

        self.assertEqual(corrupt, path.read_bytes())

    def test_state_reader_rejects_unsafe_type_and_link_count(self) -> None:
        state_root = self.config().state_root
        state_root.mkdir(parents=True, mode=0o700)
        valid = b'{"version":1,"entries":[]}\n'

        directory = state_root / "directory.json"
        directory.mkdir(mode=0o700)

        symlink_target = state_root / "symlink-target.json"
        symlink_target.write_bytes(valid)
        symlink_target.chmod(0o600)
        symlink = state_root / "symlink.json"
        os.symlink(symlink_target.name, symlink)

        hardlinked = state_root / "hardlinked.json"
        hardlinked.write_bytes(valid)
        hardlinked.chmod(0o600)
        os.link(hardlinked, state_root / "hardlinked-alias.json")

        for path in (directory, symlink, hardlinked):
            with (
                self.subTest(path=path.name),
                self.assertRaises(self.repair.FatalRepairError),
            ):
                self.repair._read_json(path)

    def test_state_json_read_and_write_are_bounded(self) -> None:
        limit = self.repair.MAX_STATE_JSON_BYTES
        state_root = self.config().state_root
        state_root.mkdir(parents=True, mode=0o700)
        oversized = state_root / "oversized.json"
        original = b" " * (limit + 1)
        oversized.write_bytes(original)
        oversized.chmod(0o600)

        with mock.patch.object(self.repair.json, "loads") as loads:
            with self.assertRaises(self.repair.FatalRepairError):
                self.repair._read_json(oversized)
        loads.assert_not_called()
        self.assertEqual(original, oversized.read_bytes())

        destination = state_root / "oversized-write.json"
        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.atomic_write_json(destination, {"padding": "x" * (limit + 1)})
        self.assertFalse(destination.exists())
        self.assertEqual([], list(state_root.glob(f".{destination.name}.tmp.*")))

    def test_state_json_benign_timestamp_generation_churn_retries_then_accepts(
        self,
    ) -> None:
        for index, kind in enumerate(("mtime", "ctime")):
            with self.subTest(kind=kind):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"benign-{index}"
                )
                path = config.queue_path
                self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
                expected = path.read_bytes()
                before_inode = path.stat().st_ino
                real_pread = self.repair.os.pread
                mutated = False

                def racing_read(descriptor: int, count: int, offset: int) -> bytes:
                    nonlocal mutated
                    chunk = real_pread(descriptor, count, offset)
                    if chunk and not mutated:
                        mutated = True
                        metadata = path.stat()
                        if kind == "mtime":
                            os.utime(
                                path,
                                ns=(
                                    metadata.st_atime_ns,
                                    metadata.st_mtime_ns + 1_000_000_000,
                                ),
                            )
                        else:
                            path.chmod(0o640)
                            path.chmod(0o600)
                    return chunk

                with mock.patch.object(
                    self.repair.os, "pread", side_effect=racing_read
                ):
                    value = self.repair._read_json(path)

                self.assertTrue(mutated)
                self.assertEqual({"version": 1, "entries": [UUID_A]}, value)
                self.assertEqual(before_inode, path.stat().st_ino)
                self.assertEqual(expected, path.read_bytes())
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_state_json_same_inode_content_mutation_during_read_is_fatal(self) -> None:
        path = self.config().queue_path
        self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
        original = path.read_bytes()
        replacement = original.replace(UUID_A.encode(), UUID_B.encode())
        self.assertEqual(len(original), len(replacement))
        real_pread = self.repair.os.pread
        mutated = False

        def racing_read(descriptor: int, count: int, offset: int) -> bytes:
            nonlocal mutated
            chunk = real_pread(descriptor, count, offset)
            if chunk and not mutated:
                mutated = True
                with path.open("r+b", buffering=0) as stream:
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
            return chunk

        with mock.patch.object(self.repair.os, "pread", side_effect=racing_read):
            with self.assertRaisesRegex(
                self.repair.FatalRepairError, "content changed"
            ):
                self.repair._read_json(path)

        self.assertTrue(mutated)
        self.assertEqual(replacement, path.read_bytes())

    def test_state_json_access_policy_mutation_during_read_is_fatal(self) -> None:
        path = self.config().queue_path
        self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
        original = path.read_bytes()
        real_pread = self.repair.os.pread
        mutated = False

        def racing_read(descriptor: int, count: int, offset: int) -> bytes:
            nonlocal mutated
            chunk = real_pread(descriptor, count, offset)
            if chunk and not mutated:
                mutated = True
                path.chmod(0o640)
            return chunk

        with (
            mock.patch.object(self.repair.os, "pread", side_effect=racing_read),
            self.assertRaisesRegex(self.repair.FatalRepairError, "access policy"),
        ):
            self.repair._read_json(path)

        self.assertTrue(mutated)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(0o640, path.stat().st_mode & 0o777)

    def test_state_json_persistent_generation_churn_and_unreadable_are_distinct(
        self,
    ) -> None:
        path = self.config().queue_path
        self.repair.atomic_write_json(path, {"version": 1, "entries": [UUID_A]})
        real_pread = self.repair.os.pread
        churns = 0

        def churning_read(descriptor: int, count: int, offset: int) -> bytes:
            nonlocal churns
            chunk = real_pread(descriptor, count, offset)
            if chunk:
                churns += 1
                metadata = path.stat()
                os.utime(
                    path,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            return chunk

        with (
            mock.patch.object(self.repair.os, "pread", side_effect=churning_read),
            self.assertRaisesRegex(
                self.repair.FatalRepairError, "generation remained unstable"
            ),
        ):
            self.repair._read_json(path)
        self.assertGreaterEqual(churns, self.repair.STATE_READ_ATTEMPTS)

        with (
            mock.patch.object(
                self.repair.os,
                "pread",
                side_effect=OSError(errno.EIO, "injected unreadable state"),
            ),
            self.assertRaisesRegex(self.repair.FatalRepairError, "became unreadable"),
        ):
            self.repair._read_json(path)

    def test_repair_lock_rejects_state_root_replacement_after_lock_acquisition(
        self,
    ) -> None:
        config = self.config()
        moved = config.state_root.with_name("reflink-repair-moved")
        real_flock = self.repair.fcntl.flock
        remapped = False
        entered = False

        def remapping_flock(descriptor: int, operation: int) -> Any:
            nonlocal remapped
            result = real_flock(descriptor, operation)
            if operation & self.repair.fcntl.LOCK_EX and not remapped:
                remapped = True
                os.replace(config.state_root, moved)
                config.state_root.mkdir(mode=0o700)
            return result

        with (
            mock.patch.object(self.repair.fcntl, "flock", side_effect=remapping_flock),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            with self.repair.repair_lock(config):
                entered = True

        self.assertTrue(remapped)
        self.assertFalse(entered)
        self.assertTrue((moved / config.lock_path.name).exists())

    def test_atomic_write_rejects_queue_directory_replacement_after_fd_open(
        self,
    ) -> None:
        config = self.config()
        moved = config.state_root.with_name("queue-parent-moved")
        real_replace = self.repair.os.replace
        remapped = False

        def remapping_replace(
            source: Any, destination: Any, *arguments: Any, **keywords: Any
        ) -> Any:
            nonlocal remapped
            if keywords.get("src_dir_fd") is not None and not remapped:
                remapped = True
                real_replace(config.state_root, moved)
                config.state_root.mkdir(mode=0o700)
            return real_replace(source, destination, *arguments, **keywords)

        with (
            mock.patch.object(self.repair.os, "replace", side_effect=remapping_replace),
            self.assertRaises(self.repair.AtomicWriteError) as raised,
        ):
            self.repair.atomic_write_json(
                config.queue_path, {"version": 1, "entries": [UUID_A]}
            )

        self.assertTrue(remapped)
        self.assertEqual(
            self.repair.AtomicPublication.AMBIGUOUS,
            raised.exception.publication,
        )
        self.assertFalse(config.queue_path.exists())
        self.assertTrue((moved / config.queue_path.name).exists())

    def test_stable_read_rejects_state_parent_replacement_after_fd_open(self) -> None:
        config = self.config()
        self.repair._write_queue(config, [UUID_A])
        moved = config.state_root.with_name("read-parent-moved")
        real_pread = self.repair.os.pread
        remapped = False

        def remapping_read(descriptor: int, count: int, offset: int) -> bytes:
            nonlocal remapped
            chunk = real_pread(descriptor, count, offset)
            if chunk and not remapped:
                remapped = True
                os.replace(config.state_root, moved)
                config.state_root.mkdir(mode=0o700)
            return chunk

        with (
            mock.patch.object(self.repair.os, "pread", side_effect=remapping_read),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            self.repair._load_queue(config)

        self.assertTrue(remapped)
        self.assertFalse(config.queue_path.exists())
        self.assertTrue((moved / config.queue_path.name).exists())

    def test_delete_rejects_manifest_or_intent_parent_replacement_before_unlink(
        self,
    ) -> None:
        for index, label in enumerate(("manifest", "intent")):
            with self.subTest(label=label):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"delete-{index}"
                )
                parent = (
                    config.manifests_dir if label == "manifest" else config.intents_dir
                )
                path = parent / f"{UUID_A}.json"
                self.repair.atomic_write_json(path, {"evidence": label})
                moved = parent.with_name(f"{parent.name}-moved")
                real_stat = self.repair.os.stat
                remapped = False

                def remapping_stat(
                    target: Any, *arguments: Any, **keywords: Any
                ) -> Any:
                    nonlocal remapped
                    if (
                        target == path.name
                        and keywords.get("dir_fd") is not None
                        and not remapped
                    ):
                        remapped = True
                        os.replace(parent, moved)
                        parent.mkdir(mode=0o700)
                    return real_stat(target, *arguments, **keywords)

                with (
                    mock.patch.object(
                        self.repair.os, "stat", side_effect=remapping_stat
                    ),
                    self.assertRaises(self.repair.FatalRepairError),
                ):
                    self.repair._delete_state_file(path, label=label)

                self.assertTrue(remapped)
                self.assertFalse(path.exists())
                self.assertTrue((moved / path.name).exists())

    def test_repair_lock_fails_fast_while_held_and_stale_file_is_not_a_lock(
        self,
    ) -> None:
        config = self.config()
        config.state_root.mkdir(parents=True)
        holder_code = (
            "import fcntl, os, sys, time; "
            "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
            "fcntl.flock(fd, fcntl.LOCK_EX); "
            "print('ready', flush=True); time.sleep(2)"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(config.lock_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual("ready", holder.stdout.readline().strip())
            started = time.monotonic()
            with self.assertRaises(self.repair.FatalRepairError):
                with self.repair.repair_lock(config):
                    self.fail("a second owner acquired the held repair lock")
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            holder.terminate()
            holder.communicate(timeout=5)

        with self.repair.repair_lock(config):
            self.assertTrue(config.lock_path.exists())


class RepairSelectionTests(FilesystemFixture):
    def paired(self, rollout_id: str, content: bytes) -> None:
        self.write_rollout(self.source_root, "sessions", rollout_id, content)
        self.write_rollout(self.mirror_root, "archived_sessions", rollout_id, content)

    def test_default_repair_is_dry_run_and_repeated_ids_do_not_duplicate_work(
        self,
    ) -> None:
        self.paired(UUID_A, b"aaaa")
        self.paired(UUID_B, b"bbbbbb")
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(
            rollout_ids=[UUID_A, UUID_A, UUID_B], apply=False, max_files=1
        )

        results = self.results_by_id(receipt)
        self.assertEqual("dry-run", results[UUID_A]["outcome"])
        self.assertEqual("limit-files", results[UUID_B]["classification"])
        self.assertEqual([f"inspect:{UUID_A}", f"inspect:{UUID_B}"], backend.events)
        self.assertNotIn("prepare", backend.events)
        self.assertFalse(self.config().queue_path.exists())

    def test_max_bytes_accepts_the_exact_boundary_and_never_overshoots(self) -> None:
        self.paired(UUID_A, b"aaaa")
        self.paired(UUID_B, b"bbbbbb")
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(apply=False, max_bytes=4)

        results = self.results_by_id(receipt)
        self.assertEqual("dry-run", results[UUID_A]["outcome"])
        self.assertEqual("limit-bytes", results[UUID_B]["classification"])
        self.assertEqual(1, receipt["summary"]["selected_files"])
        self.assertEqual(4, receipt["summary"]["selected_bytes"])

    def test_apply_limits_mutate_only_the_deterministic_first_candidate(self) -> None:
        self.paired(UUID_A, b"aaaa")
        self.paired(UUID_B, b"bbbb")
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(
            apply=True,
            max_files=1,
            rollout_ids=[UUID_B, UUID_A, UUID_A],
        )

        results = self.results_by_id(receipt)
        self.assertEqual("repaired", results[UUID_A]["outcome"])
        self.assertEqual("limit-files", results[UUID_B]["classification"])
        self.assertEqual(1, backend.events.count("swap_forward"))

    def test_source_policy_difference_does_not_block_exact_content(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"source\n")
        self.write_rollout(self.mirror_root, "sessions", UUID_A, b"mirror\n")
        self.paired(UUID_B, b"same\n")
        self.write_rollout(self.source_root, "sessions", UUID_C, b"complete\npartial")
        self.write_rollout(self.mirror_root, "sessions", UUID_C, b"complete\n")
        backend = FakeBackend(self.repair)
        backend.policy_mismatches.add(UUID_B)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(apply=False)

        results = self.results_by_id(receipt)
        self.assertEqual("content-mismatch", results[UUID_A]["classification"])
        self.assertEqual("eligible", results[UUID_B]["classification"])
        self.assertEqual("dry-run", results[UUID_B]["outcome"])
        self.assertEqual("active-complete-prefix", results[UUID_C]["classification"])
        self.assertEqual("deferred", results[UUID_C]["outcome"])
        self.assertNotIn("prepare", backend.events)

    def test_nonexclusive_mirror_policy_is_terminal_without_transaction_state(
        self,
    ) -> None:
        cases = (
            ("wrong-owner", {"uid": os.geteuid() + 1}),
            ("group-writable", {"mode": 0o620}),
            ("other-writable", {"mode": 0o602}),
            ("extended-acl", {"acl_sha256": "f" * 64}),
            ("unsafe-flags", {"flags": 0x2}),
        )
        for index, (label, override) in enumerate(cases):
            with self.subTest(policy=label):
                self.paired(UUID_A, b"same\n")
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"nonexclusive-{index}",
                )
                backend = FakeBackend(self.repair)
                backend.mirror_policy_overrides[UUID_A] = override
                tool = self.repair.RepairTool(config, backend)

                dry_run = tool.repair(apply=False, rollout_ids=[UUID_A])
                applied = tool.repair(apply=True, rollout_ids=[UUID_A])
                self.repair._write_queue(config, [UUID_A])
                retried = tool.retry(apply=True)

                for receipt in (dry_run, applied, retried):
                    result = self.results_by_id(receipt)[UUID_A]
                    self.assertEqual("unsupported", result["classification"])
                self.assertEqual(
                    "skipped", self.results_by_id(dry_run)[UUID_A]["outcome"]
                )
                self.assertEqual(
                    "skipped", self.results_by_id(applied)[UUID_A]["outcome"]
                )
                self.assertEqual(
                    "terminal", self.results_by_id(retried)[UUID_A]["outcome"]
                )
                self.assertNotIn("prepare", backend.events)
                self.assertNotIn("clone", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                self.assertEqual([], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_intents(config))
                self.assertEqual([], self.repair._load_manifests(config))

    def test_mirror_policy_change_during_apply_never_reaches_swap(self) -> None:
        self.paired(UUID_A, b"same\n")
        backend = FakeBackend(self.repair)
        backend.prepare_errors[UUID_A] = self.repair.UnstablePathError(
            "mirror policy changed after inspection"
        )
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(apply=True, queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertNotIn("swap_forward", backend.events)
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))

    def test_source_parent_replacement_after_inspection_is_deferred_and_queued(
        self,
    ) -> None:
        self.paired(UUID_A, b"same\n")
        backend = FakeBackend(self.repair)
        backend.prepare_source_parent_identity = self.repair.FileIdentity(
            device=7, inode=9999
        )

        receipt = self.repair.RepairTool(self.config(), backend).repair(
            apply=True,
            rollout_ids=[UUID_A],
            queue_unstable=True,
        )

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertIn("prepare", backend.events)
        self.assertNotIn("clone", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_intents(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_source_parent_unreadable_after_inspection_is_fatal_not_deferred(
        self,
    ) -> None:
        self.paired(UUID_A, b"same\n")
        backend = FakeBackend(self.repair)
        backend.prepare_errors[UUID_A] = self.repair.SafetyError(
            "source parent became unreadable"
        )

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(self.config(), backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            )

        self.assertNotIn("clone", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertFalse(self.config().queue_path.exists())

    def test_unreadable_unstable_and_unsupported_inspection_are_distinct(self) -> None:
        for rollout_id in (UUID_A, UUID_B, UUID_C):
            self.paired(rollout_id, b"same\n")
        backend = FakeBackend(self.repair)
        backend.inspect_errors.update(
            {
                UUID_A: self.repair.UnreadablePathError("cannot read"),
                UUID_B: self.repair.UnstablePathError("changed during inspection"),
                UUID_C: self.repair.UnsupportedError("clone unsupported"),
            }
        )
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(apply=False)

        results = self.results_by_id(receipt)
        self.assertEqual("unreadable", results[UUID_A]["classification"])
        self.assertEqual("unstable", results[UUID_B]["classification"])
        self.assertEqual("unsupported", results[UUID_C]["classification"])

    def test_targeted_dry_run_reports_scan_errors_as_incomplete(self) -> None:
        self.paired(UUID_A, b"same\n")
        discovery = self.repair.discover_candidates(self.config())
        discovery.scan_errors.append(
            {
                "side": "source",
                "path": str(self.source_root / "archived_sessions"),
                "error": "injected state-root replacement",
            }
        )
        config = self.config()
        self.repair._write_queue(config, [UUID_A])
        tool = self.repair.RepairTool(config, FakeBackend(self.repair))

        with mock.patch.object(
            self.repair, "discover_candidates", return_value=discovery
        ):
            receipts = (
                tool.inventory([UUID_A]),
                tool.repair(apply=False, rollout_ids=[UUID_A]),
                tool.retry(apply=False),
            )

        for receipt in receipts:
            with self.subTest(command=receipt["command"]):
                self.assertEqual("incomplete", receipt["status"])
                self.assertFalse(receipt["summary"]["complete"])
                self.assertEqual(1, receipt["summary"]["scan_errors"])
                self.assertEqual(1, receipt["summary"]["classifications"]["scan-error"])
                scan_errors = [
                    result
                    for result in receipt["results"]
                    if result["classification"] == "scan-error"
                ]
                self.assertEqual(1, len(scan_errors))
                self.assertEqual("incomplete", scan_errors[0]["outcome"])


class TransactionTests(FilesystemFixture):
    def paired(self, rollout_id: str = UUID_A, content: bytes = b"same\n") -> None:
        self.write_rollout(self.source_root, "sessions", rollout_id, content)
        self.write_rollout(self.mirror_root, "archived_sessions", rollout_id, content)

    def record_manifests(self, events: List[str]) -> Any:
        original = self.repair._write_manifest

        def write(config: Any, manifest: Any) -> Any:
            events.append(f"manifest:{manifest.phase.value}")
            return original(config, manifest)

        return mock.patch.object(self.repair, "_write_manifest", side_effect=write)

    def test_live_transaction_close_failure_preserves_body_primary(self) -> None:
        cases: Sequence[tuple[str, BaseException]] = (
            ("exception", RuntimeError("injected live primary")),
            ("interrupt", KeyboardInterrupt("injected live interrupt")),
        )
        for index, (case, primary) in enumerate(cases):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"live-close-{index}"
                )
                self.paired()
                discovery = self.repair.discover_candidates(config)
                candidate = discovery.candidates[UUID_A]
                backend = FakeBackend(self.repair)
                inspection = backend.inspect_pair(candidate.source, candidate.mirror)
                backend.clone_error = primary
                close_calls: List[Any] = []
                real_close = FakeTransaction.close

                def failing_close(transaction: Any) -> None:
                    close_calls.append(transaction)
                    real_close(transaction)
                    raise OSError(errno.EIO, "injected live transaction close failure")

                with (
                    mock.patch.object(FakeTransaction, "close", new=failing_close),
                    self.assertRaises(BaseException) as raised,
                ):
                    self.repair.RepairTool(config, backend)._repair_one(
                        candidate, inspection, queue_enabled=False
                    )

                self.assertEqual(2, len(close_calls))
                self.assertIs(close_calls[0], close_calls[1])
                self.assertTrue(close_calls[0].closed)
                if case == "interrupt":
                    self.assertIs(primary, raised.exception)
                else:
                    self.assertIsInstance(
                        raised.exception, self.repair.FatalRepairError
                    )
                    self.assertIn("injected live primary", str(raised.exception))
                    self.assertNotIn("transaction close failure", str(raised.exception))

    def test_live_transaction_finally_trace_retries_close_with_primary(
        self,
    ) -> None:
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "live-finally-trace"
        )
        self.paired()
        discovery = self.repair.discover_candidates(config)
        candidate = discovery.candidates[UUID_A]
        backend = FakeBackend(self.repair)
        inspection = backend.inspect_pair(candidate.source, candidate.mirror)
        body_primary = KeyboardInterrupt("live transaction body primary")
        backend.clone_error = body_primary
        evidence: Dict[str, Any] = {}
        close_calls: List[Any] = []
        real_close = FakeTransaction.close

        def recording_close(transaction: Any) -> None:
            close_calls.append(transaction)
            real_close(transaction)

        def cleanup_call_line(frame: Any, captured: Dict[str, Any]) -> bool:
            owned = frame.f_locals.get("transaction")
            if (
                owned is None
                or owned is not backend.last_transaction
                or owned.closed
                or frame.f_locals.get("primary_error") is not body_primary
                or linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "transaction.close()"
            ):
                return False
            captured["transaction"] = owned
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["line_number"] = frame.f_lineno
            return True

        closed_before_fallback = False
        calls_before_fallback: tuple[Any, ...] = ()
        with mock.patch.object(FakeTransaction, "close", new=recording_close):
            try:
                self.assert_cleanup_trace_preserves_primary(
                    self.repair.RepairTool._repair_one.__code__,
                    cleanup_call_line,
                    lambda: self.repair.RepairTool(config, backend)._repair_one(
                        candidate, inspection, queue_enabled=False
                    ),
                    body_primary,
                    label="live transaction finally close",
                    events=("line",),
                    evidence=evidence,
                )
                closed_before_fallback = evidence["transaction"].closed
                calls_before_fallback = tuple(close_calls)
            finally:
                transaction = backend.last_transaction
                if transaction is not None and not transaction.closed:
                    real_close(transaction)

        self.assertIsInstance(evidence["line_number"], int)
        self.assertTrue(closed_before_fallback)
        self.assertEqual((evidence["transaction"],), calls_before_fallback)

    def test_live_transaction_close_failure_fallback_trace_second_drains(
        self,
    ) -> None:
        self.paired()
        for with_body_primary in (False, True):
            with self.subTest(with_body_primary=with_body_primary):
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root
                    / f"live-close-fallback-{with_body_primary}",
                )
                discovery = self.repair.discover_candidates(config)
                candidate = discovery.candidates[UUID_A]
                backend = FakeBackend(self.repair)
                inspection = backend.inspect_pair(candidate.source, candidate.mirror)
                body_primary = KeyboardInterrupt(
                    "live transaction body primary before close fallback"
                )
                if with_body_primary:
                    backend.clone_error = body_primary
                first_close_error = OSError(
                    errno.EIO, "injected first live transaction close failure"
                )
                expected_primary: BaseException = (
                    body_primary if with_body_primary else first_close_error
                )
                evidence: Dict[str, Any] = {}
                close_calls: List[Any] = []
                real_close = FakeTransaction.close

                def fail_first_close(transaction: Any) -> None:
                    close_calls.append(transaction)
                    if len(close_calls) == 1:
                        try:
                            raise first_close_error
                        except BaseException as error:
                            evidence["first_close_origin_traceback"] = (
                                error.__traceback__
                            )
                            if not with_body_primary:
                                evidence["body_origin_traceback"] = error.__traceback__
                            raise
                    real_close(transaction)

                def fallback_preamble(frame: Any, captured: Dict[str, Any]) -> bool:
                    transaction = frame.f_locals.get("transaction")
                    if (
                        transaction is None
                        or transaction is not backend.last_transaction
                        or transaction.closed
                        or frame.f_locals.get("cleanup_error") is not first_close_error
                        or frame.f_locals.get("primary_error")
                        is not (body_primary if with_body_primary else None)
                        or close_calls != [transaction]
                        or linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "_cleanup_guard = True"
                    ):
                        return False
                    if with_body_primary:
                        captured["body_origin_traceback"] = body_primary.__traceback__
                    captured["transaction"] = transaction
                    captured["line_number"] = frame.f_lineno
                    return True

                closed_before_fallback = False
                calls_before_fallback: tuple[Any, ...] = ()
                with mock.patch.object(FakeTransaction, "close", new=fail_first_close):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            self.repair.RepairTool._repair_one.__code__,
                            fallback_preamble,
                            lambda: self.repair.RepairTool(config, backend)._repair_one(
                                candidate,
                                inspection,
                                queue_enabled=False,
                            ),
                            expected_primary,
                            label=(
                                f"live transaction close fallback {with_body_primary}"
                            ),
                            events=("line",),
                            evidence=evidence,
                        )
                        transaction = evidence["transaction"]
                        closed_before_fallback = transaction.closed
                        calls_before_fallback = tuple(close_calls)
                    finally:
                        transaction = backend.last_transaction
                        if transaction is not None and not transaction.closed:
                            real_close(transaction)

                self.assertIsInstance(evidence["line_number"], int)
                self.assertIsNotNone(evidence["first_close_origin_traceback"])
                self.assertTrue(closed_before_fallback)
                self.assertEqual(
                    (evidence["transaction"], evidence["transaction"]),
                    calls_before_fallback,
                )

    def mutate_live_snapshot(
        self, backend: FakeBackend, *, target: str, property_name: str
    ) -> None:
        transaction = backend.last_transaction
        self.assertIsNotNone(transaction)
        assert transaction is not None
        snapshot = {
            "source": transaction.source_live,
            "original": transaction.original_live,
            "clone": transaction.clone_live,
        }[target]
        self.assertIsNotNone(snapshot)
        if property_name == "content":
            changed = dataclasses.replace(snapshot, content_sha256="f" * 64)
        else:
            changed = dataclasses.replace(
                snapshot,
                policy=dataclasses.replace(
                    snapshot.policy,
                    xattrs_sha256=f"{snapshot.policy.xattrs_sha256}-changed",
                ),
            )
        if target == "source":
            transaction.source_live = changed
        elif target == "original":
            transaction.original_live = changed
        else:
            transaction.clone_live = changed

    def test_success_persists_commit_ready_before_unlink_and_done_before_queue_removal(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)
        self.repair._write_queue(self.config(), [UUID_A])
        events = backend.events
        original_write_queue = self.repair._write_queue
        original_delete_manifest = self.repair._delete_manifest

        def write_queue(
            config: Any, rollout_ids: Iterable[str], queue_path: Any = None
        ) -> None:
            values = sorted(rollout_ids)
            events.append(f"queue:{','.join(values)}")
            original_write_queue(config, values, queue_path)

        def delete_manifest(config: Any, rollout_id: str) -> None:
            events.append("manifest:delete")
            original_delete_manifest(config, rollout_id)

        with (
            self.record_manifests(events),
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(
                self.repair, "_delete_manifest", side_effect=delete_manifest
            ),
        ):
            receipt = tool.retry(apply=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("repaired", result["classification"])
        self.assertLess(
            events.index("revalidate_before_prepared"),
            events.index("manifest:PREPARED"),
        )
        self.assertLess(
            events.index("manifest:PREPARED"), events.index("mark_prepared")
        )
        self.assertLess(
            events.index("mark_prepared"), events.index("revalidate_pre_forward")
        )
        self.assertLess(
            events.index("revalidate_pre_forward"), events.index("swap_forward")
        )
        self.assertLess(
            events.index("postverify"), events.index("manifest:COMMIT_READY")
        )
        self.assertLess(
            events.index("manifest:COMMIT_READY"), events.index("unlink_original")
        )
        self.assertLess(
            events.index("unlink_original"), events.index("revalidate_committed")
        )
        self.assertLess(
            events.index("revalidate_committed"), events.index("cleanup_stage")
        )
        self.assertLess(events.index("cleanup_stage"), events.index("manifest:DONE"))
        self.assertLess(events.index("manifest:DONE"), events.index("queue:"))
        self.assertLess(events.index("queue:"), events.index("manifest:delete"))
        self.assertEqual([], self.repair._load_queue(self.config()))

    def test_durable_manifest_mapping_authorizes_every_namespace_mutation(
        self,
    ) -> None:
        cases = (
            ("swap_forward", False, "swap_forward"),
            ("unlink_original", False, "unlink_original"),
            ("remove_stage", False, "cleanup_stage"),
            ("swap_back", True, "swap_back"),
            ("unlink_clone", True, "unlink_clone"),
        )
        for index, (action, rollback, forbidden_event) in enumerate(cases):
            for scope in ("leaf", "child", "root"):
                with self.subTest(action=action, scope=scope):
                    self.paired()
                    config = dataclasses.replace(
                        self.config(),
                        state_root=self.state_root / f"authorize-{index}-{scope}",
                    )
                    backend = FakeBackend(self.repair)
                    if rollback:
                        backend.postverify_error = self.repair.UnstablePathError(
                            "injected postverify rollback"
                        )
                    retained: List[pathlib.Path] = []

                    def remap() -> None:
                        retained.append(
                            self.remap_private_state_record(
                                config,
                                self.repair._manifest_path(config, UUID_A),
                                scope=scope,
                                suffix=f"{action}-moved",
                            )
                        )

                    backend.during_authorize_hooks[action] = remap
                    with self.assertRaises(self.repair.FatalRepairError):
                        self.repair.RepairTool(config, backend).repair(
                            apply=True,
                            rollout_ids=[UUID_A],
                            queue_unstable=True,
                        )

                    self.assertEqual(1, len(retained))
                    self.assertTrue(retained[0].exists())
                    self.assertNotIn(forbidden_event, backend.events)
                    self.assertFalse(config.queue_path.exists())

    def test_commit_ready_revalidates_both_same_inode_snapshots_before_unlink(
        self,
    ) -> None:
        cases = (
            (UUID_A, "original", "content"),
            (UUID_B, "clone", "policy"),
        )
        for rollout_id, target, property_name in cases:
            with self.subTest(target=target, property_name=property_name):
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"commit-{rollout_id}",
                )
                backend = FakeBackend(self.repair)
                tool = self.repair.RepairTool(config, backend)
                original_write = self.repair._write_manifest

                def write_manifest(current_config: Any, manifest: Any) -> Any:
                    result = original_write(current_config, manifest)
                    if manifest.phase == self.repair.Phase.COMMIT_READY:
                        self.mutate_live_snapshot(
                            backend,
                            target=target,
                            property_name=property_name,
                        )
                    return result

                with (
                    mock.patch.object(
                        self.repair, "_write_manifest", side_effect=write_manifest
                    ),
                    self.assertRaises(self.repair.FatalRepairError),
                ):
                    tool.repair(apply=True, rollout_ids=[rollout_id])

                self.assertIn("revalidate_forward", backend.events)
                self.assertNotIn("unlink_original", backend.events)
                manifests = self.repair._load_manifests(config)
                self.assertEqual(1, len(manifests))
                self.assertEqual(self.repair.Phase.COMMIT_READY, manifests[0].phase)

    def test_post_unlink_commit_survivor_mutation_retains_commit_ready(self) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        backend.unlink_original_hook = lambda _transaction: self.mutate_live_snapshot(
            backend, target="clone", property_name="content"
        )

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(config, backend).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        self.assertLess(
            backend.events.index("unlink_original"),
            backend.events.index("revalidate_committed"),
        )
        self.assertNotIn("cleanup_stage", backend.events)
        self.assertFalse(config.queue_path.exists())
        manifests = self.repair._load_manifests(config)
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.COMMIT_READY, manifests[0].phase)

    def test_post_prepared_nonexclusive_policy_drift_is_fatal_and_retained(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        original_write = self.repair._write_manifest

        def write_manifest(current_config: Any, manifest: Any) -> Any:
            fence = original_write(current_config, manifest)
            if manifest.phase == self.repair.Phase.PREPARED:
                transaction = backend.last_transaction
                self.assertIsNotNone(transaction)
                assert transaction is not None
                transaction.original_live = dataclasses.replace(
                    transaction.original_live,
                    policy=dataclasses.replace(
                        transaction.original_live.policy,
                        mode=0o660,
                    ),
                )
            return fence

        with (
            mock.patch.object(
                self.repair, "_write_manifest", side_effect=write_manifest
            ),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            self.repair.RepairTool(config, backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            )

        self.assertIn("mark_prepared", backend.events)
        self.assertIn("revalidate_pre_forward", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertFalse(config.queue_path.exists())
        manifests = self.repair._load_manifests(config)
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.PREPARED, manifests[0].phase)

    def test_live_prepared_revalidates_all_protected_snapshots_before_forward_swap(
        self,
    ) -> None:
        cases = (
            (UUID_A, "source", "content", True),
            (UUID_B, "original", "policy", False),
            (UUID_C, "clone", "content", False),
        )
        for rollout_id, target, property_name, retryable in cases:
            with self.subTest(target=target, property_name=property_name):
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"pre-forward-{rollout_id}",
                )
                backend = FakeBackend(self.repair)
                tool = self.repair.RepairTool(config, backend)
                original_write = self.repair._write_manifest

                def write_manifest(current_config: Any, manifest: Any) -> Any:
                    result = original_write(current_config, manifest)
                    if manifest.phase == self.repair.Phase.PREPARED:
                        self.mutate_live_snapshot(
                            backend,
                            target=target,
                            property_name=property_name,
                        )
                    return result

                with mock.patch.object(
                    self.repair, "_write_manifest", side_effect=write_manifest
                ):
                    if retryable:
                        receipt = tool.repair(
                            apply=True,
                            rollout_ids=[rollout_id],
                            queue_unstable=True,
                        )
                    else:
                        with self.assertRaises(self.repair.FatalRepairError):
                            tool.repair(
                                apply=True,
                                rollout_ids=[rollout_id],
                                queue_unstable=True,
                            )

                self.assertIn("revalidate_pre_forward", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                if retryable:
                    result = self.results_by_id(receipt)[rollout_id]
                    self.assertEqual("unstable", result["classification"])
                    self.assertEqual("deferred", result["outcome"])
                    self.assertEqual([rollout_id], self.repair._load_queue(config))
                    self.assertEqual([], self.repair._load_manifests(config))
                else:
                    self.assertEqual([], self.repair._load_queue(config))
                    manifests = self.repair._load_manifests(config)
                    self.assertEqual(1, len(manifests))
                    self.assertEqual(self.repair.Phase.PREPARED, manifests[0].phase)

    def test_rollback_ready_revalidates_both_same_inode_snapshots_before_swap_back(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.UnstablePathError(
            "injected active source change"
        )
        tool = self.repair.RepairTool(self.config(), backend)
        original_write = self.repair._write_manifest

        def write_manifest(config: Any, manifest: Any) -> Any:
            result = original_write(config, manifest)
            if manifest.phase == self.repair.Phase.ROLLBACK_READY:
                self.mutate_live_snapshot(
                    backend, target="original", property_name="content"
                )
                self.mutate_live_snapshot(
                    backend, target="clone", property_name="policy"
                )
            return result

        with (
            mock.patch.object(
                self.repair, "_write_manifest", side_effect=write_manifest
            ),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        self.assertIn("revalidate_forward", backend.events)
        self.assertNotIn("swap_back", backend.events)
        self.assertNotIn("unlink_clone", backend.events)
        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.ROLLBACK_READY, manifests[0].phase)

    def test_rolled_back_revalidates_both_same_inode_snapshots_before_clone_unlink(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.UnstablePathError(
            "injected active source change"
        )
        tool = self.repair.RepairTool(self.config(), backend)
        original_write = self.repair._write_manifest

        def write_manifest(config: Any, manifest: Any) -> Any:
            result = original_write(config, manifest)
            if manifest.phase == self.repair.Phase.ROLLED_BACK:
                self.mutate_live_snapshot(
                    backend, target="original", property_name="policy"
                )
                self.mutate_live_snapshot(
                    backend, target="clone", property_name="content"
                )
            return result

        with (
            mock.patch.object(
                self.repair, "_write_manifest", side_effect=write_manifest
            ),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        self.assertIn("swap_back", backend.events)
        self.assertIn("revalidate_before_cleanup", backend.events)
        self.assertNotIn("unlink_clone", backend.events)
        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.ROLLED_BACK, manifests[0].phase)

    def test_prepared_not_durable_policy_failure_aborts_stage_without_swap_or_evidence(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.clone_policy_changed = True
        tool = self.repair.RepairTool(self.config(), backend)
        events = backend.events

        with (
            self.record_manifests(events),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        self.assertLess(events.index("clone"), events.index("abort_before_prepared"))
        self.assertLess(events.index("abort_before_prepared"), events.index("close"))
        self.assertNotIn("swap_forward", events)
        self.assertFalse(any(event.startswith("manifest:") for event in events))
        self.assertEqual([], self.repair._load_manifests(self.config()))
        self.assertEqual([], self.repair._load_queue(self.config()))

    def test_preprepared_unstable_cleanup_queues_before_last_intent_is_deleted(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        backend.clone_error = self.repair.UnstablePathError(
            "injected source change before PREPARED"
        )
        events = backend.events
        original_write_queue = self.repair._write_queue
        original_delete_intent = self.repair._delete_intent

        def write_queue(
            current_config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(current_config, rollout_ids, queue_path)

        def delete_intent(current_config: Any, rollout_id: str) -> None:
            events.append("intent:delete")
            original_delete_intent(current_config, rollout_id)

        with (
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(self.repair, "_delete_intent", side_effect=delete_intent),
        ):
            receipt = self.repair.RepairTool(config, backend).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertLess(
            events.index("abort_before_prepared"), events.index("queue:add")
        )
        self.assertLess(events.index("queue:add"), events.index("intent:delete"))
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))

    def test_preprepared_abort_handler_trace_retries_and_preserves_deferred_result(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "preprepared-abort-trace"
        )
        backend = FakeBackend(self.repair)
        body_primary = self.repair.UnstablePathError(
            "source changed before PREPARED abort trace"
        )
        backend.clone_error = body_primary
        evidence: Dict[str, Any] = {}

        def abort_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            transaction = frame.f_locals.get("self")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != 'self.backend.events.append("abort_before_prepared")'
                or transaction is None
                or transaction is not backend.last_transaction
                or "abort_before_prepared" in backend.events
            ):
                return False
            repair_frame = frame.f_back
            while (
                repair_frame is not None
                and repair_frame.f_code
                is not self.repair.RepairTool._repair_one.__code__
            ):
                repair_frame = repair_frame.f_back
            if (
                repair_frame is None
                or repair_frame.f_locals.get("error") is not body_primary
            ):
                return False
            captured["body_primary"] = body_primary
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["transaction"] = transaction
            captured["abort_line"] = frame.f_lineno
            return True

        self.assert_cleanup_trace_allows_completion(
            FakeTransaction.abort_before_prepared.__code__,
            abort_boundary,
            lambda: self.repair.RepairTool(config, backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            ),
            label="preprepared abort handler",
            evidence=evidence,
        )

        result = self.results_by_id(evidence["result"])[UUID_A]
        self.assertIsInstance(evidence["abort_line"], int)
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(1, backend.events.count("abort_before_prepared"))
        self.assertLess(
            backend.events.index("abort_before_prepared"),
            backend.events.index("close"),
        )
        self.assertTrue(evidence["transaction"].closed)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_cleanup_receipt_handler_trace_keeps_abort_failure_fail_closed(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "cleanup-receipt-handler"
        )
        backend = FakeBackend(self.repair)
        body_primary = self.repair.UnstablePathError(
            "source changed after clone before PREPARED"
        )
        natural_error = OSError(errno.EIO, "abort cleanup failed after unlinking clone")
        backend.abort_before_prepared_error = natural_error
        evidence: Dict[str, Any] = {"fired": False}
        cleanup_interrupt = KeyboardInterrupt("cleanup receipt handler interrupt")

        def fail_after_clone() -> None:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        backend.clone_hook = fail_after_clone

        def receipt_error_boundary(frame: Any) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "receipt.error = error"
                or frame.f_locals.get("error") is not natural_error
            ):
                return False
            receipt = frame.f_locals.get("receipt")
            if (
                receipt is None
                or receipt.dispatches != 1
                or receipt.completed
                or receipt.error is not None
            ):
                return False
            repair_frame = frame.f_back
            while (
                repair_frame is not None
                and repair_frame.f_code
                is not self.repair.RepairTool._repair_one.__code__
            ):
                repair_frame = repair_frame.f_back
            if (
                repair_frame is None
                or repair_frame.f_locals.get("preprepared_abort_receipt") is not receipt
                or repair_frame.f_locals.get("error") is not body_primary
            ):
                return False
            evidence["receipt"] = receipt
            evidence["natural_origin_traceback"] = natural_error.__traceback__
            evidence["handler_line"] = frame.f_lineno
            return True

        def raise_cleanup_interrupt() -> None:
            try:
                raise cleanup_interrupt
            except BaseException as error:
                evidence["cleanup_origin_traceback"] = error.__traceback__
                raise

        previous_trace = sys.gettrace()

        def tracer(frame: Any, event: str, _argument: Any) -> Any:
            if (
                event == "line"
                and frame.f_code is self.repair._dispatch_cleanup_action.__code__
                and not evidence["fired"]
                and receipt_error_boundary(frame)
            ):
                evidence["fired"] = True
                sys.settrace(None)
                raise_cleanup_interrupt()
            return tracer

        caught: Optional[BaseException] = None
        sys.settrace(tracer)
        try:
            self.repair.RepairTool(config, backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            )
        except BaseException as error:
            caught = error
        finally:
            sys.settrace(previous_trace)

        self.assertTrue(evidence["fired"])
        self.assertIsInstance(evidence["handler_line"], int)
        self.assertIsInstance(caught, self.repair.FatalRepairError)
        assert isinstance(caught, self.repair.FatalRepairError)
        self.assertIsInstance(caught.__cause__, self.repair.FatalRepairError)
        assert isinstance(caught.__cause__, self.repair.FatalRepairError)
        self.assertIs(body_primary, caught.__cause__.__cause__)
        self.assertIn(str(natural_error), str(caught))
        body_origin = evidence.get("body_origin_traceback")
        self.assertIsNotNone(body_origin)
        body_traceback = body_primary.__traceback__
        body_nodes: List[Any] = []
        while body_traceback is not None:
            body_nodes.append(body_traceback)
            body_traceback = body_traceback.tb_next
        self.assertIn(body_origin, body_nodes)
        natural_origin = evidence.get("natural_origin_traceback")
        self.assertIsNotNone(natural_origin)
        natural_traceback = natural_error.__traceback__
        natural_nodes: List[Any] = []
        while natural_traceback is not None:
            natural_nodes.append(natural_traceback)
            natural_traceback = natural_traceback.tb_next
        self.assertIn(natural_origin, natural_nodes)
        cleanup_origin = evidence.get("cleanup_origin_traceback")
        self.assertIsNotNone(cleanup_origin)
        cleanup_traceback = cleanup_interrupt.__traceback__
        cleanup_nodes: List[Any] = []
        while cleanup_traceback is not None:
            cleanup_nodes.append(cleanup_traceback)
            cleanup_traceback = cleanup_traceback.tb_next
        self.assertIn(cleanup_origin, cleanup_nodes)
        receipt = evidence["receipt"]
        self.assertEqual(1, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIs(natural_error, receipt.error)
        self.assertEqual(1, backend.events.count("abort_before_prepared"))
        self.assertEqual(1, backend.events.count("unlink_clone"))
        self.assertNotIn("swap_forward", backend.events)
        self.assertIsNotNone(backend.last_transaction)
        assert backend.last_transaction is not None
        self.assertTrue(backend.last_transaction.closed)
        intents = self.repair._load_intents(config)
        self.assertEqual(1, len(intents))
        self.assertEqual(self.repair.IntentState.STAGE_BOUND, intents[0].state)
        self.assertEqual([], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_intent_atomic_failure_before_replace_never_creates_stage_or_swaps(
        self,
    ) -> None:
        for index, injected in enumerate(
            (
                OSError(errno.EIO, "injected intent temp write failure"),
                KeyboardInterrupt("injected intent temp write interrupt"),
            )
        ):
            with self.subTest(exception_type=type(injected).__name__):
                rollout_id = (UUID_A, UUID_B)[index]
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"intent-atomic-{index}"
                )
                backend = FakeBackend(self.repair)
                original_atomic_write = self.repair.atomic_write_json

                def fail_intent_write(
                    path: pathlib.Path, payload: Mapping[str, Any]
                ) -> Any:
                    if payload.get("phase") != self.repair.Phase.INTENT.value:
                        return original_atomic_write(path, payload)
                    with mock.patch.object(
                        self.repair.os, "write", side_effect=injected
                    ):
                        return original_atomic_write(path, payload)

                with (
                    mock.patch.object(
                        self.repair,
                        "atomic_write_json",
                        side_effect=fail_intent_write,
                    ),
                    self.assertRaises(BaseException),
                ):
                    self.repair.RepairTool(config, backend).repair(
                        apply=True, rollout_ids=[rollout_id], queue_unstable=True
                    )

                self.assertNotIn("prepare", backend.events)
                self.assertNotIn("clone", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                self.assertEqual([], self.repair._load_manifests(config))
                self.assertEqual([], self.repair._load_intents(config))
                self.assertEqual(
                    [], list(config.intents_dir.glob(f".{rollout_id}.json.tmp.*"))
                )
                self.assertEqual(
                    [], list(self.mirror_root.rglob(".codex-reflink-repair-*"))
                )

    def test_failed_post_unlink_abort_is_not_retried_or_allowed_to_delete_intent(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        backend.abort_before_prepared_error = self.repair.SafetyError(
            "injected original survivor post-unlink revalidation failure"
        )
        original_write_intent = self.repair._write_intent

        def fail_clone_bound_intent(current_config: Any, intent: Any) -> Any:
            if intent.state == self.repair.IntentState.CLONE_BOUND:
                raise self.repair.AtomicWriteError(
                    "injected CLONE_BOUND intent publication failure",
                    publication=self.repair.AtomicPublication.NOT_PUBLISHED,
                )
            return original_write_intent(current_config, intent)

        with (
            mock.patch.object(
                self.repair, "_write_intent", side_effect=fail_clone_bound_intent
            ),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            self.repair.RepairTool(config, backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            )

        self.assertEqual(1, backend.events.count("abort_before_prepared"))
        self.assertEqual(1, backend.events.count("unlink_clone"))
        self.assertNotIn("cleanup_stage", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        intents = self.repair._load_intents(config)
        self.assertEqual(1, len(intents))
        self.assertEqual(self.repair.IntentState.STAGE_BOUND, intents[0].state)
        self.assertFalse(config.queue_path.exists())

    def test_first_prepared_write_failure_before_replace_aborts_stage_for_all_exceptions(
        self,
    ) -> None:
        cases = (
            (
                UUID_A,
                self.repair.AtomicWriteError(
                    "injected failure before canonical replace",
                    publication=self.repair.AtomicPublication.NOT_PUBLISHED,
                ),
            ),
            (
                UUID_B,
                self.repair.AtomicWriteInterruption(
                    KeyboardInterrupt("injected interrupt before replace"),
                    publication=self.repair.AtomicPublication.NOT_PUBLISHED,
                ),
            ),
        )
        original_atomic_write = self.repair.atomic_write_json
        for rollout_id, injected in cases:
            with self.subTest(exception_type=type(injected).__name__):
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"before-replace-{rollout_id}",
                )
                backend = FakeBackend(self.repair)
                tool = self.repair.RepairTool(config, backend)

                def fail_prepared(
                    path: pathlib.Path, payload: Mapping[str, Any]
                ) -> Any:
                    if payload.get("phase") == self.repair.Phase.PREPARED.value:
                        raise injected
                    return original_atomic_write(path, payload)

                with (
                    mock.patch.object(
                        self.repair, "atomic_write_json", side_effect=fail_prepared
                    ),
                    self.assertRaises(BaseException),
                ):
                    tool.repair(apply=True, rollout_ids=[rollout_id])

                self.assertIn("abort_before_prepared", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                self.assertFalse(
                    self.repair._manifest_path(config, rollout_id).exists()
                )
                self.assertEqual([], self.repair._load_manifests(config))

    def test_first_prepared_write_failure_after_replace_retains_canonical_evidence(
        self,
    ) -> None:
        cases = (
            (
                UUID_A,
                self.repair.AtomicWriteError(
                    "injected parent fsync failure after replace",
                    publication=self.repair.AtomicPublication.PUBLISHED_UNSYNCED,
                ),
            ),
            (
                UUID_B,
                self.repair.AtomicWriteInterruption(
                    KeyboardInterrupt("injected interrupt after replace"),
                    publication=self.repair.AtomicPublication.PUBLISHED_UNSYNCED,
                ),
            ),
        )
        original_atomic_write = self.repair.atomic_write_json
        for rollout_id, injected in cases:
            with self.subTest(exception_type=type(injected).__name__):
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"after-replace-{rollout_id}",
                )
                backend = FakeBackend(self.repair)
                tool = self.repair.RepairTool(config, backend)

                def fail_prepared(
                    path: pathlib.Path, payload: Mapping[str, Any]
                ) -> Any:
                    result = original_atomic_write(path, payload)
                    if payload.get("phase") == self.repair.Phase.PREPARED.value:
                        raise injected
                    return result

                with (
                    mock.patch.object(
                        self.repair, "atomic_write_json", side_effect=fail_prepared
                    ),
                    self.assertRaises(BaseException),
                ):
                    tool.repair(apply=True, rollout_ids=[rollout_id])

                self.assertNotIn("abort_before_prepared", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                manifests = self.repair._load_manifests(config)
                self.assertEqual(1, len(manifests))
                self.assertEqual(self.repair.Phase.PREPARED, manifests[0].phase)
                self.assertEqual(rollout_id, manifests[0].rollout_id)

    def test_manifest_parent_replacement_during_atomic_publish_never_reaches_swap(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        moved = config.manifests_dir.with_name("manifests-moved")
        real_replace = self.repair.os.replace
        remapped = False

        def remapping_replace(
            source: Any, destination: Any, *arguments: Any, **keywords: Any
        ) -> Any:
            nonlocal remapped
            destination_fd = keywords.get("dst_dir_fd")
            if (
                destination == f"{UUID_A}.json"
                and destination_fd is not None
                and config.manifests_dir.exists()
                and not remapped
            ):
                held = os.fstat(destination_fd)
                named = config.manifests_dir.stat()
                if (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino):
                    remapped = True
                    real_replace(config.manifests_dir, moved)
                    config.manifests_dir.mkdir(mode=0o700)
            return real_replace(source, destination, *arguments, **keywords)

        with (
            mock.patch.object(self.repair.os, "replace", side_effect=remapping_replace),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            self.repair.RepairTool(config, backend).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        self.assertTrue(remapped)
        self.assertNotIn("swap_forward", backend.events)
        self.assertEqual([], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))
        self.assertTrue((moved / f"{UUID_A}.json").exists())
        self.assertEqual(1, len(self.repair._load_intents(config)))
        self.assertEqual(
            0, len(list(self.mirror_root.rglob(".codex-reflink-repair-*")))
        )

    def test_postverify_failure_durably_orients_and_rolls_back_before_cleanup(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.UnstablePathError(
            "injected postverify instability"
        )
        tool = self.repair.RepairTool(self.config(), backend)
        events = backend.events
        original_write_queue = self.repair._write_queue
        original_delete_manifest = self.repair._delete_manifest

        def write_queue(
            config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(config, rollout_ids, queue_path)

        def delete_manifest(config: Any, rollout_id: str) -> None:
            events.append("manifest:delete")
            original_delete_manifest(config, rollout_id)

        with (
            self.record_manifests(events),
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(
                self.repair, "_delete_manifest", side_effect=delete_manifest
            ),
        ):
            receipt = tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertIn("manifest:ROLLBACK_READY", events)
        self.assertLess(
            events.index("manifest:ROLLBACK_READY"), events.index("swap_back")
        )
        self.assertLess(events.index("swap_back"), events.index("unlink_clone"))
        self.assertLess(
            events.index("unlink_clone"), events.index("revalidate_rolled_back")
        )
        self.assertLess(
            events.index("revalidate_rolled_back"), events.index("cleanup_stage")
        )
        self.assertLess(
            events.index("cleanup_stage"), events.index("manifest:DEFERRED")
        )
        self.assertLess(events.index("manifest:DEFERRED"), events.index("queue:add"))
        self.assertLess(events.index("queue:add"), events.index("manifest:delete"))
        self.assertNotIn("unlink_original", events)
        self.assertEqual([], self.repair._load_manifests(self.config()))
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))

    def test_postverify_rollback_first_line_trace_retries_once_and_defers(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "postverify-rollback-trace"
        )
        backend = FakeBackend(self.repair)
        body_primary = self.repair.UnstablePathError(
            "postverify body primary before rollback trace"
        )
        backend.postverify_error = body_primary
        evidence: Dict[str, Any] = {}

        def rollback_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            transaction = frame.f_locals.get("transaction")
            if (
                "orientation = transaction.orientation("
                not in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
                or transaction is None
                or transaction is not backend.last_transaction
                or frame.f_locals.get("original_error") is not body_primary
                or "swap_back" in backend.events
            ):
                return False
            repair_frame = frame.f_back
            while (
                repair_frame is not None
                and repair_frame.f_code
                is not self.repair.RepairTool._repair_one.__code__
            ):
                repair_frame = repair_frame.f_back
            if repair_frame is None:
                return False
            receipt = repair_frame.f_locals.get("postverify_rollback_receipt")
            if receipt is None or receipt.dispatches != 1:
                return False
            captured["body_primary"] = body_primary
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["transaction"] = transaction
            captured["action_receipt"] = receipt
            captured["rollback_line"] = frame.f_lineno
            return True

        self.assert_cleanup_trace_allows_completion(
            self.repair.RepairTool._rollback_after_postverify.__code__,
            rollback_boundary,
            lambda: self.repair.RepairTool(config, backend).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            ),
            label="live postverify rollback first line",
            evidence=evidence,
        )

        result = self.results_by_id(evidence["result"])[UUID_A]
        receipt = evidence["action_receipt"]
        self.assertIsInstance(evidence["rollback_line"], int)
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(2, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIsInstance(receipt.error, self.repair.SafeRolledBack)
        self.assertEqual(1, backend.events.count("swap_back"))
        self.assertEqual(1, backend.events.count("unlink_clone"))
        self.assertEqual(1, backend.events.count("cleanup_stage"))
        self.assertNotIn("unlink_original", backend.events)
        self.assertTrue(evidence["transaction"].closed)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))
        self.assertEqual([], self.repair._load_intents(config))

    def test_late_cleanup_sync_trace_resumes_from_durable_phase(self) -> None:
        for index, route in enumerate(("defer", "rollback")):
            with self.subTest(route=route):
                rollout_id = (UUID_A, UUID_B)[index]
                self.paired(rollout_id)
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"late-sync-{route}"
                )
                backend = FakeBackend(self.repair)
                body_primary = self.repair.UnstablePathError(
                    f"live {route} body primary before late namespace sync"
                )
                evidence: Dict[str, Any] = {}

                def fail_pre_forward(
                    transaction: Any,
                    _source: Any,
                    _original: Any,
                    _clone: Any,
                ) -> None:
                    transaction.backend.events.append("revalidate_pre_forward")
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def fail_postverify(
                    transaction: Any,
                    _source: Any,
                    _clone: Any,
                ) -> Any:
                    transaction.backend.events.append("postverify")
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                helper = (
                    self.repair.RepairTool._defer_before_forward
                    if route == "defer"
                    else self.repair.RepairTool._rollback_after_postverify
                )
                receipt_name = (
                    "pre_forward_defer_receipt"
                    if route == "defer"
                    else "postverify_rollback_receipt"
                )
                syncs_before = 0 if route == "defer" else 1

                def late_sync_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    transaction = frame.f_locals.get("transaction")
                    current = frame.f_locals.get("current")
                    authorization = frame.f_locals.get("authorization")
                    manifest = frame.f_locals.get("manifest")
                    if (
                        linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "transaction.sync_namespaces()"
                        or frame.f_locals.get("original_error") is not body_primary
                        or transaction is not backend.last_transaction
                        or current is None
                        or current.phase is not self.repair.Phase.ROLLED_BACK
                        or authorization is None
                        or authorization.state is not current
                        or manifest is None
                        or manifest.phase is not self.repair.Phase.PREPARED
                        or transaction.current_orientation
                        is not self.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING
                        or backend.events.count("unlink_clone") != 1
                        or backend.events.count("sync_namespaces") != syncs_before
                    ):
                        return False
                    repair_frame = frame.f_back
                    while (
                        repair_frame is not None
                        and repair_frame.f_code
                        is not self.repair.RepairTool._repair_one.__code__
                    ):
                        repair_frame = repair_frame.f_back
                    if repair_frame is None:
                        return False
                    action_receipt = repair_frame.f_locals.get(receipt_name)
                    if action_receipt is None or action_receipt.dispatches != 1:
                        return False
                    captured["body_primary"] = body_primary
                    captured["body_origin_traceback"] = evidence[
                        "body_origin_traceback"
                    ]
                    captured["transaction"] = transaction
                    captured["action_receipt"] = action_receipt
                    captured["sync_line"] = frame.f_lineno
                    return True

                fault_patch = mock.patch.object(
                    FakeTransaction,
                    ("revalidate_pre_forward" if route == "defer" else "postverify"),
                    new=(fail_pre_forward if route == "defer" else fail_postverify),
                )
                with self.record_manifests(backend.events), fault_patch:
                    self.assert_cleanup_trace_allows_completion(
                        helper.__code__,
                        late_sync_boundary,
                        lambda: self.repair.RepairTool(config, backend).repair(
                            apply=True,
                            rollout_ids=[rollout_id],
                            queue_unstable=True,
                        ),
                        label=f"live {route} late cleanup sync",
                        evidence=evidence,
                    )

                result = self.results_by_id(evidence["result"])[rollout_id]
                action_receipt = evidence["action_receipt"]
                relevant_phases = [
                    event for event in backend.events if event.startswith("manifest:")
                ]
                self.assertIsInstance(evidence["sync_line"], int)
                self.assertEqual("unstable", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertEqual(2, action_receipt.dispatches)
                if route == "defer":
                    self.assertTrue(action_receipt.completed)
                    self.assertIsNone(action_receipt.error)
                    self.assertNotIn("swap_forward", backend.events)
                    self.assertNotIn("swap_back", backend.events)
                    self.assertEqual(1, backend.events.count("sync_namespaces"))
                else:
                    self.assertFalse(action_receipt.completed)
                    self.assertIsInstance(
                        action_receipt.error, self.repair.SafeRolledBack
                    )
                    self.assertEqual(1, backend.events.count("swap_forward"))
                    self.assertEqual(1, backend.events.count("swap_back"))
                    self.assertEqual(2, backend.events.count("sync_namespaces"))
                self.assertEqual(
                    [
                        "manifest:PREPARED",
                        "manifest:ROLLBACK_READY",
                        "manifest:ROLLED_BACK",
                        "manifest:DEFERRED",
                    ],
                    relevant_phases,
                )
                self.assertEqual(1, backend.events.count("unlink_clone"))
                self.assertEqual(1, backend.events.count("cleanup_stage"))
                self.assertNotIn("unlink_original", backend.events)
                self.assertTrue(evidence["transaction"].closed)
                self.assertEqual([rollout_id], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_manifests(config))
                self.assertEqual([], self.repair._load_intents(config))

    def test_post_unlink_rollback_survivor_mutation_retains_rolled_back(self) -> None:
        self.paired()
        config = self.config()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.UnstablePathError(
            "injected source instability"
        )
        backend.unlink_clone_hook = lambda _transaction: self.mutate_live_snapshot(
            backend, target="original", property_name="policy"
        )

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(config, backend).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        self.assertLess(
            backend.events.index("unlink_clone"),
            backend.events.index("revalidate_rolled_back"),
        )
        self.assertNotIn("cleanup_stage", backend.events)
        self.assertFalse(config.queue_path.exists())
        manifests = self.repair._load_manifests(config)
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.ROLLED_BACK, manifests[0].phase)

    def test_postswap_source_path_or_parent_move_is_deferred_after_safe_rollback(
        self,
    ) -> None:
        for index, mutation in enumerate(("pathname", "parent")):
            with self.subTest(mutation=mutation):
                rollout_id = (UUID_A, UUID_B)[index]
                self.paired(rollout_id)
                source = next(
                    path
                    for path in (self.source_root / "sessions").rglob("*.jsonl")
                    if rollout_id in path.name
                )
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"postswap-{mutation}"
                )
                backend = FakeBackend(self.repair)
                backend.postverify_error = self.repair.UnstablePathError(
                    f"injected source {mutation} replacement"
                )
                if mutation == "pathname":
                    moved = source.with_name(f"{source.name}.moved")
                    backend.postverify_hook = lambda: source.replace(moved)
                else:
                    moved_parent = source.parent.with_name(
                        f"{source.parent.name}.moved"
                    )

                    def move_parent() -> None:
                        source.parent.replace(moved_parent)
                        source.parent.mkdir()

                    backend.postverify_hook = move_parent
                events = backend.events

                with self.record_manifests(events):
                    receipt = self.repair.RepairTool(config, backend).repair(
                        apply=True,
                        rollout_ids=[rollout_id],
                        queue_unstable=True,
                    )

                result = self.results_by_id(receipt)[rollout_id]
                self.assertEqual("unstable", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertIn("swap_forward", events)
                self.assertIn("swap_back", events)
                self.assertIn("manifest:DEFERRED", events)
                self.assertNotIn("manifest:FAILED", events)
                self.assertNotIn("unlink_original", events)
                self.assertEqual([rollout_id], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_manifests(config))

    def test_nonretryable_postverify_safety_failure_is_failed_and_permanently_retained(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.SafetyError(
            "injected nonretryable safety failure"
        )
        tool = self.repair.RepairTool(self.config(), backend)
        events = backend.events
        original_write_queue = self.repair._write_queue

        def write_queue(
            config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(config, rollout_ids, queue_path)

        with (
            self.record_manifests(events),
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        self.assertLess(events.index("unlink_clone"), events.index("cleanup_stage"))
        self.assertLess(events.index("cleanup_stage"), events.index("manifest:FAILED"))
        self.assertNotIn("queue:add", events)
        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.FAILED, manifests[0].phase)
        self.assertEqual([], self.repair._load_queue(self.config()))

    def test_unsupported_failure_after_prepared_is_fatal_and_keeps_queue_evidence(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        self.repair._write_queue(config, [UUID_A])
        before = config.queue_path.read_bytes()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.UnsupportedError(
            "injected ENOTSUP after PREPARED"
        )
        tool = self.repair.RepairTool(config, backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.retry(apply=True)

        self.assertEqual(before, config.queue_path.read_bytes())
        manifests = self.repair._load_manifests(config)
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.FAILED, manifests[0].phase)

    def test_rollback_failure_is_fatal_and_retains_durable_orientation_evidence(
        self,
    ) -> None:
        self.paired()
        backend = FakeBackend(self.repair)
        backend.postverify_error = self.repair.SafetyError(
            "injected postverify failure"
        )
        backend.swap_back_error = self.repair.SafetyError("injected rollback failure")
        tool = self.repair.RepairTool(self.config(), backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.ROLLBACK_READY, manifests[0].phase)
        self.assertNotIn("unlink_original", backend.events)
        self.assertNotIn("unlink_clone", backend.events)


class QueueTests(FilesystemFixture):
    def test_apply_and_retry_fail_closed_on_root_scan_error_without_queue_change(
        self,
    ) -> None:
        outside_source = self.root / "outside-source"
        source = self.rollout_path(outside_source, "sessions", UUID_A)
        source.write_bytes(b"same\n")
        source_link = self.root / "source-link"
        os.symlink(outside_source, source_link)
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"same\n")
        config = dataclasses.replace(self.config(), codex_root=source_link)
        self.repair._write_queue(config, [UUID_A])
        before = config.queue_path.read_bytes()
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(config, backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.repair(apply=True, queue_unstable=True)
        self.assertEqual(before, config.queue_path.read_bytes())

        with self.assertRaises(self.repair.FatalRepairError):
            tool.retry(apply=True)
        self.assertEqual(before, config.queue_path.read_bytes())
        self.assertFalse(any(event.startswith("inspect:") for event in backend.events))
        self.assertNotIn("prepare", backend.events)

    def test_retry_root_identity_change_is_fatal_and_preserves_queue(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"same\n")
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"same\n")
        config = self.config()
        self.repair._write_queue(config, [UUID_A])
        before = config.queue_path.read_bytes()
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(config, backend)
        real_lstat = self.repair.os.lstat
        source_root_calls = 0

        def changing_lstat(path: Any) -> os.stat_result:
            nonlocal source_root_calls
            result = real_lstat(path)
            if pathlib.Path(path) == self.source_root:
                source_root_calls += 1
                if source_root_calls == 2:
                    fields = list(result)
                    fields[1] += 10000
                    return os.stat_result(fields)
            return result

        with (
            mock.patch.object(self.repair.os, "lstat", side_effect=changing_lstat),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            tool.retry(apply=True)

        self.assertEqual(2, source_root_calls)
        self.assertEqual(before, config.queue_path.read_bytes())
        self.assertNotIn("prepare", backend.events)

    def test_repair_dry_run_with_queue_unstable_does_not_create_queue(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"complete\npartial")
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"complete\n")
        config = self.config()
        tool = self.repair.RepairTool(config, FakeBackend(self.repair))

        receipt = tool.repair(apply=False, queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("active-complete-prefix", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(0, receipt["summary"]["queue_size"])
        self.assertFalse(receipt["summary"]["queue_enabled"])
        self.assertFalse(config.queue_path.exists())

    def test_queue_disabled_preserves_existing_queue_for_prepared_and_rollback_instability(
        self,
    ) -> None:
        for timing, rollout_id in (("prepare", UUID_A), ("postverify", UUID_B)):
            with self.subTest(timing=timing):
                self.write_rollout(self.source_root, "sessions", rollout_id, b"same\n")
                self.write_rollout(
                    self.mirror_root, "archived_sessions", rollout_id, b"same\n"
                )
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"disabled-{timing}"
                )
                self.repair._write_queue(config, [UUID_C])
                before = config.queue_path.read_bytes()
                backend = FakeBackend(self.repair)
                if timing == "prepare":
                    backend.prepare_errors[rollout_id] = self.repair.UnstablePathError(
                        "injected bind instability"
                    )
                else:
                    backend.postverify_error = self.repair.UnstablePathError(
                        "injected postverify instability"
                    )
                tool = self.repair.RepairTool(config, backend)

                receipt = tool.repair(
                    apply=True,
                    rollout_ids=[rollout_id],
                    queue_unstable=False,
                )

                result = self.results_by_id(receipt)[rollout_id]
                self.assertEqual("unstable", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertFalse(receipt["summary"]["queue_enabled"])
                self.assertEqual(before, config.queue_path.read_bytes())

    def test_repair_dry_run_preserves_existing_queue_bytes(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"complete\npartial")
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"complete\n")
        config = self.config()
        self.repair.atomic_write_json(
            config.queue_path, {"version": 1, "entries": [UUID_B]}
        )
        before = config.queue_path.read_bytes()
        tool = self.repair.RepairTool(config, FakeBackend(self.repair))

        receipt = tool.repair(apply=False, queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("active-complete-prefix", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(1, receipt["summary"]["queue_size"])
        self.assertFalse(receipt["summary"]["queue_enabled"])
        self.assertEqual(before, config.queue_path.read_bytes())

    def test_discovery_deferred_queue_is_durable_before_later_candidate_failure(
        self,
    ) -> None:
        cases = (
            (
                "fatal",
                self.repair.SafetyError("injected later candidate failure"),
                self.repair.FatalRepairError,
            ),
            (
                "interrupt",
                KeyboardInterrupt("injected later candidate interrupt"),
                KeyboardInterrupt,
            ),
        )
        for index, (label, injected, expected_exception) in enumerate(cases):
            with self.subTest(label=label):
                self.write_rollout(
                    self.source_root, "sessions", UUID_A, b"complete\npartial"
                )
                self.write_rollout(
                    self.mirror_root, "archived_sessions", UUID_A, b"complete\n"
                )
                self.write_rollout(self.source_root, "sessions", UUID_B, b"same\n")
                self.write_rollout(
                    self.mirror_root, "archived_sessions", UUID_B, b"same\n"
                )
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"queue-fence-{index}"
                )
                backend = FakeBackend(self.repair)
                backend.inspect_errors[UUID_B] = injected
                events = backend.events
                original_write_queue = self.repair._write_queue

                def write_queue(
                    current_config: Any,
                    rollout_ids: Iterable[str],
                    queue_path: Optional[pathlib.Path] = None,
                ) -> None:
                    events.append("queue:add")
                    original_write_queue(current_config, rollout_ids, queue_path)

                with (
                    mock.patch.object(
                        self.repair, "_write_queue", side_effect=write_queue
                    ),
                    self.assertRaises(expected_exception),
                ):
                    self.repair.RepairTool(config, backend).repair(
                        apply=True, queue_unstable=True
                    )

                self.assertLess(
                    events.index("queue:add"), events.index(f"inspect:{UUID_B}")
                )
                self.assertEqual([UUID_A], self.repair._load_queue(config))

    def test_active_complete_prefix_queues_only_id_and_retry_keeps_it_deferred(
        self,
    ) -> None:
        source = self.write_rollout(
            self.source_root, "sessions", UUID_A, b"complete\npartial"
        )
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"complete\n")
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.repair(apply=True, queue_unstable=True)

        self.assertEqual(
            "active-complete-prefix",
            self.results_by_id(receipt)[UUID_A]["classification"],
        )
        self.assertTrue(receipt["summary"]["queue_enabled"])
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        raw_queue = json.loads(self.config().queue_path.read_text())
        self.assertEqual({"version": 1, "entries": [UUID_A]}, raw_queue)
        self.assertNotIn("sessions", self.config().queue_path.read_text())

        retry = tool.retry(apply=True)
        self.assertTrue(retry["summary"]["queue_enabled"])
        self.assertEqual("deferred", self.results_by_id(retry)[UUID_A]["outcome"])
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))

        archived = self.rollout_path(self.source_root, "archived_sessions", UUID_A)
        archived.parent.mkdir(parents=True, exist_ok=True)
        source.replace(archived)
        archived.write_bytes(b"complete\n")
        retry_backend = FakeBackend(self.repair)
        retry_tool = self.repair.RepairTool(self.config(), retry_backend)
        finished = retry_tool.retry(apply=True)
        self.assertEqual("repaired", self.results_by_id(finished)[UUID_A]["outcome"])
        self.assertEqual([], self.repair._load_queue(self.config()))

    def test_retry_retains_missing_but_removes_terminal_unsupported_pair(self) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_C, b"same\n")
        self.write_rollout(self.mirror_root, "sessions", UUID_C, b"same\n")
        self.repair._write_queue(self.config(), [UUID_B, UUID_C])
        backend = FakeBackend(self.repair)
        backend.inspect_errors[UUID_C] = self.repair.UnsupportedError(
            "strict clone unavailable"
        )
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.retry(apply=True)

        results = self.results_by_id(receipt)
        self.assertEqual("deferred", results[UUID_B]["outcome"])
        self.assertEqual("terminal", results[UUID_C]["outcome"])
        self.assertEqual([UUID_B], self.repair._load_queue(self.config()))

    def test_apply_time_clone_enotsup_and_exdev_are_terminal_after_proved_cleanup(
        self,
    ) -> None:
        for rollout_id in (UUID_A, UUID_B):
            self.write_rollout(self.source_root, "sessions", rollout_id, b"same\n")
            self.write_rollout(
                self.mirror_root, "archived_sessions", rollout_id, b"same\n"
            )
        config = self.config()
        self.repair._write_queue(config, [UUID_A, UUID_B])
        backend = FakeBackend(self.repair)
        backend.clone_errors.extend(
            [
                self.repair.UnsupportedError(
                    f"clone failed: {os.strerror(errno.ENOTSUP)}"
                ),
                self.repair.UnsupportedError(
                    f"clone failed: {os.strerror(errno.EXDEV)}"
                ),
            ]
        )
        tool = self.repair.RepairTool(config, backend)

        receipt = tool.retry(apply=True)

        results = self.results_by_id(receipt)
        for rollout_id in (UUID_A, UUID_B):
            self.assertEqual("unsupported", results[rollout_id]["classification"])
            self.assertEqual("terminal", results[rollout_id]["outcome"])
        self.assertEqual(2, backend.events.count("abort_before_prepared"))
        self.assertNotIn("swap_forward", backend.events)
        self.assertEqual([], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_repair_apply_time_unsupported_is_a_per_candidate_terminal_result(
        self,
    ) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"same\n")
        self.write_rollout(self.mirror_root, "archived_sessions", UUID_A, b"same\n")
        config = self.config()
        backend = FakeBackend(self.repair)
        backend.clone_error = self.repair.UnsupportedError(
            f"clone failed: {os.strerror(errno.ENOTSUP)}"
        )
        tool = self.repair.RepairTool(config, backend)

        receipt = tool.repair(apply=True, rollout_ids=[UUID_A])

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unsupported", result["classification"])
        self.assertEqual("terminal", result["outcome"])
        self.assertIn("abort_before_prepared", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertFalse(config.queue_path.exists())
        self.assertEqual([], self.repair._load_manifests(config))

    def test_noncanonical_or_duplicate_queue_schema_is_fatal_without_rewrite(
        self,
    ) -> None:
        queue_path = self.config().queue_path
        for payload in (
            {"version": 2, "entries": [UUID_A]},
            {"version": 1, "entries": [UUID_A, UUID_A]},
            {"version": 1, "entries": ["not-a-uuid"]},
        ):
            with self.subTest(payload=payload):
                self.repair.atomic_write_json(queue_path, payload)
                before = queue_path.read_bytes()
                with self.assertRaises(self.repair.FatalRepairError):
                    self.repair._load_queue(self.config())
                self.assertEqual(before, queue_path.read_bytes())


class CliTests(FilesystemFixture):
    def environment(self) -> Dict[str, str]:
        return {
            "HOME": str(self.root),
            "CODEX_ROOT": str(self.source_root),
            "CODEX_MIRROR_ROOT": str(self.mirror_root),
            "CODEX_BACKUP_STATE_ROOT": str(self.state_root),
        }

    def invoke(self, arguments: Sequence[str], backend: FakeBackend) -> Any:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = self.repair.main(
            arguments,
            backend=backend,
            environ=self.environment(),
            stdout=stdout,
            stderr=stderr,
        )
        lines = stdout.getvalue().splitlines()
        self.assertEqual(1, len(lines), msg=stdout.getvalue())
        return exit_code, json.loads(lines[0]), stderr.getvalue()

    def paired(self, rollout_id: str) -> None:
        self.write_rollout(self.source_root, "sessions", rollout_id, b"same\n")
        self.write_rollout(self.mirror_root, "archived_sessions", rollout_id, b"same\n")

    def build_manifest(self, config: Any, rollout_id: str, phase: Any) -> Any:
        source = self.rollout_path(self.source_root, "sessions", rollout_id)
        mirror = self.rollout_path(self.mirror_root, "archived_sessions", rollout_id)
        inspection = FakeBackend(self.repair).inspect_pair(source, mirror)
        clone = self.repair.FileSnapshot(
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
            size=inspection.mirror.size,
            mtime_ns=inspection.mirror.mtime_ns,
            nlink=1,
            content_sha256=inspection.mirror.content_sha256,
            policy=inspection.mirror.policy,
        )
        txid = rollout_id.replace("-", "")
        final_rel = str(mirror.relative_to(self.mirror_root))
        temporary_rel = str(
            pathlib.PurePath(final_rel).parent
            / f".codex-reflink-repair-{txid}"
            / "clone"
        )
        return self.repair.RepairManifest(
            rollout_id=rollout_id,
            txid=txid,
            phase=phase,
            source_rel=str(source.relative_to(self.source_root)),
            final_rel=final_rel,
            temporary_rel=temporary_rel,
            source_parent_identity=inspection.source_parent,
            final_parent_identity=inspection.mirror_parent,
            temporary_parent_identity=self.repair.FileIdentity(
                device=7, inode=300 + int(rollout_id[0])
            ),
            source_snapshot=inspection.source,
            original_snapshot=inspection.mirror,
            clone_snapshot=clone,
            created_at_ns=1,
            updated_at_ns=1,
            queue_enabled=True,
        )

    def test_json_dry_run_is_exit_zero_and_deduplicates_repeated_rollout_ids(
        self,
    ) -> None:
        self.write_rollout(self.source_root, "sessions", UUID_A, b"same\n")
        self.write_rollout(self.mirror_root, "sessions", UUID_A, b"same\n")
        backend = FakeBackend(self.repair)

        exit_code, receipt, stderr = self.invoke(
            [
                "repair",
                "--rollout-id",
                UUID_A,
                "--rollout-id",
                UUID_A,
                "--json",
            ],
            backend,
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("ok", receipt["status"])
        self.assertEqual(1, receipt["summary"]["total"])
        self.assertEqual("dry-run", receipt["results"][0]["outcome"])
        self.assertEqual("", stderr)
        self.assertNotIn("prepare", backend.events)

    def test_corrupt_queue_and_invalid_limits_return_one_fatal_json_receipt(
        self,
    ) -> None:
        config = self.repair.Config.from_environment(self.environment())
        self.repair.atomic_write_json(
            config.queue_path, {"version": 99, "entries": [UUID_A]}
        )
        before = config.queue_path.read_bytes()

        exit_code, receipt, stderr = self.invoke(
            ["retry", "--apply", "--json"], FakeBackend(self.repair)
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("fatal", receipt["status"])
        self.assertEqual("fatal", receipt["results"][0]["classification"])
        self.assertNotEqual("", stderr)
        self.assertEqual(before, config.queue_path.read_bytes())

        exit_code, receipt, _ = self.invoke(
            ["repair", "--max-files", "-1", "--json"], FakeBackend(self.repair)
        )
        self.assertEqual(2, exit_code)
        self.assertEqual("fatal", receipt["status"])

    def test_repair_fatal_receipt_preserves_earlier_success_and_counters(self) -> None:
        self.paired(UUID_A)
        self.paired(UUID_B)
        backend = FakeBackend(self.repair)
        backend.prepare_errors[UUID_B] = self.repair.SafetyError(
            "injected second candidate fatal"
        )

        exit_code, receipt, stderr = self.invoke(
            [
                "repair",
                "--apply",
                "--rollout-id",
                UUID_A,
                "--rollout-id",
                UUID_B,
                "--json",
            ],
            backend,
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("fatal", receipt["status"])
        results = self.results_by_id(receipt)
        self.assertEqual("repaired", results[UUID_A]["outcome"])
        self.assertEqual("fatal", results[UUID_B]["classification"])
        self.assertIn("second candidate fatal", results[UUID_B]["detail"])
        self.assertEqual(1, receipt["summary"]["completed_before_fatal"])
        self.assertEqual(2, receipt["summary"]["selected_files"])
        self.assertEqual(1, backend.events.count("swap_forward"))
        self.assertNotEqual("", stderr)

    def test_recover_fatal_receipt_keeps_completed_recovery_and_queue_state(
        self,
    ) -> None:
        self.paired(UUID_A)
        self.paired(UUID_B)
        config = self.repair.Config.from_environment(self.environment())
        self.repair._write_manifest(
            config, self.build_manifest(config, UUID_A, self.repair.Phase.DONE)
        )
        self.repair._write_manifest(
            config,
            self.build_manifest(config, UUID_B, self.repair.Phase.PREPARED),
        )
        self.repair._write_queue(config, [UUID_A, UUID_B])
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.IMPOSSIBLE

        exit_code, receipt, _stderr = self.invoke(
            ["recover", "--apply", "--json"], backend
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("fatal", receipt["status"])
        results = self.results_by_id(receipt)
        self.assertEqual("recovery-done", results[UUID_A]["classification"])
        self.assertEqual("fatal", results[UUID_B]["classification"])
        self.assertEqual(1, receipt["summary"]["completed_before_fatal"])
        self.assertEqual(1, receipt["summary"]["queue_size"])
        self.assertEqual([UUID_B], self.repair._load_queue(config))
        self.assertFalse(self.repair._manifest_path(config, UUID_A).exists())
        self.assertTrue(self.repair._manifest_path(config, UUID_B).exists())

    def test_retry_fatal_receipt_preserves_success_and_authoritative_queue(
        self,
    ) -> None:
        self.paired(UUID_A)
        self.paired(UUID_B)
        config = self.repair.Config.from_environment(self.environment())
        self.repair._write_queue(config, [UUID_A, UUID_B])
        backend = FakeBackend(self.repair)
        backend.prepare_errors[UUID_B] = self.repair.SafetyError("injected retry fatal")

        exit_code, receipt, _stderr = self.invoke(
            ["retry", "--apply", "--json"], backend
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("fatal", receipt["status"])
        results = self.results_by_id(receipt)
        self.assertEqual("repaired", results[UUID_A]["outcome"])
        self.assertEqual("fatal", results[UUID_B]["classification"])
        self.assertEqual(1, receipt["summary"]["completed_before_fatal"])
        self.assertEqual(1, receipt["summary"]["queue_size"])
        self.assertEqual([UUID_B], self.repair._load_queue(config))
        self.assertEqual(1, backend.events.count("swap_forward"))


class RecoveryTests(FilesystemFixture):
    def paired(self, rollout_id: str = UUID_A, content: bytes = b"same\n") -> None:
        self.write_rollout(self.source_root, "sessions", rollout_id, content)
        self.write_rollout(self.mirror_root, "archived_sessions", rollout_id, content)

    def record_manifests(self, events: List[str]) -> Any:
        original = self.repair._write_manifest

        def write(config: Any, manifest: Any) -> Any:
            events.append(f"manifest:{manifest.phase.value}")
            return original(config, manifest)

        return mock.patch.object(self.repair, "_write_manifest", side_effect=write)

    def build_manifest(
        self,
        phase: Any,
        *,
        retryable: bool = False,
        queue_enabled: bool = False,
    ) -> Any:
        source = next((self.source_root / "sessions").rglob("*.jsonl"))
        mirror = next((self.mirror_root / "archived_sessions").rglob("*.jsonl"))
        backend = FakeBackend(self.repair)
        inspection = backend.inspect_pair(source, mirror)
        clone = self.repair.FileSnapshot(
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
            size=inspection.mirror.size,
            mtime_ns=inspection.mirror.mtime_ns,
            nlink=1,
            content_sha256=inspection.mirror.content_sha256,
            policy=inspection.mirror.policy,
        )
        txid = "a" * 32
        final_rel = str(mirror.relative_to(self.mirror_root))
        temporary_rel = str(
            pathlib.PurePath(final_rel).parent
            / f".codex-reflink-repair-{txid}"
            / "clone"
        )
        return self.repair.RepairManifest(
            rollout_id=UUID_A,
            txid=txid,
            phase=phase,
            source_rel=str(source.relative_to(self.source_root)),
            final_rel=final_rel,
            temporary_rel=temporary_rel,
            source_parent_identity=inspection.source_parent,
            final_parent_identity=inspection.mirror_parent,
            temporary_parent_identity=self.repair.FileIdentity(device=7, inode=30),
            source_snapshot=inspection.source,
            original_snapshot=inspection.mirror,
            clone_snapshot=clone,
            created_at_ns=1,
            updated_at_ns=1,
            retryable=retryable,
            queue_enabled=queue_enabled,
        )

    def build_intent(
        self,
        state: Any,
        *,
        queue_enabled: bool = False,
        txid: str = "b" * 32,
    ) -> Any:
        source = next((self.source_root / "sessions").rglob("*.jsonl"))
        mirror = next((self.mirror_root / "archived_sessions").rglob("*.jsonl"))
        backend = FakeBackend(self.repair)
        inspection = backend.inspect_pair(source, mirror)
        clone = self.repair.FileSnapshot(
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
            size=inspection.mirror.size,
            mtime_ns=inspection.mirror.mtime_ns,
            nlink=1,
            content_sha256=inspection.mirror.content_sha256,
            policy=inspection.mirror.policy,
        )
        final_rel = str(mirror.relative_to(self.mirror_root))
        temporary_rel = str(
            pathlib.PurePath(final_rel).parent
            / f".codex-reflink-repair-{txid}"
            / "clone"
        )
        stage_bound = state in {
            self.repair.IntentState.STAGE_BOUND,
            self.repair.IntentState.CLONE_BOUND,
        }
        return self.repair.RepairIntent(
            rollout_id=UUID_A,
            txid=txid,
            state=state,
            source_rel=str(source.relative_to(self.source_root)),
            final_rel=final_rel,
            temporary_rel=temporary_rel,
            source_snapshot=inspection.source,
            original_snapshot=inspection.mirror,
            source_parent_identity=inspection.source_parent,
            final_parent_identity=inspection.mirror_parent,
            temporary_parent_identity=(
                self.repair.FileIdentity(device=7, inode=30) if stage_bound else None
            ),
            clone_snapshot=(
                clone if state == self.repair.IntentState.CLONE_BOUND else None
            ),
            queue_enabled=queue_enabled,
            created_at_ns=1,
            updated_at_ns=1,
        )

    def materialize_intent_stage(self, intent: Any, *, clone: bool = True) -> Any:
        temporary = self.mirror_root / intent.temporary_rel
        temporary.parent.mkdir(mode=0o700)
        temporary.parent.chmod(0o700)
        if clone:
            temporary.write_bytes((self.source_root / intent.source_rel).read_bytes())
            temporary.chmod(0o600)
        return temporary

    def test_recovery_transaction_close_failure_preserves_body_primary(self) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.IMPOSSIBLE
        close_calls: List[Any] = []
        real_close = FakeTransaction.close

        def failing_close(transaction: Any) -> None:
            close_calls.append(transaction)
            real_close(transaction)
            raise OSError(errno.EIO, "injected recovery transaction close failure")

        with (
            mock.patch.object(FakeTransaction, "close", new=failing_close),
            self.assertRaises(self.repair.FatalRepairError) as raised,
        ):
            self.repair.RepairTool(self.config(), backend)._resume_manifest(
                manifest, apply=True
            )

        self.assertEqual(2, len(close_calls))
        self.assertIs(close_calls[0], close_calls[1])
        self.assertTrue(close_calls[0].closed)
        self.assertIn("impossible orientation", str(raised.exception))
        self.assertNotIn("transaction close failure", str(raised.exception))

    def test_recovery_transaction_finally_trace_retries_close_with_primary(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED)
        backend = FakeBackend(self.repair)
        body_primary = KeyboardInterrupt("recovery transaction body primary")
        evidence: Dict[str, Any] = {}
        close_calls: List[Any] = []
        real_close = FakeTransaction.close

        def raise_body_primary(
            _transaction: Any, *_arguments: Any, **_keywords: Any
        ) -> None:
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def recording_close(transaction: Any) -> None:
            close_calls.append(transaction)
            real_close(transaction)

        def cleanup_call_line(frame: Any, captured: Dict[str, Any]) -> bool:
            owned = frame.f_locals.get("transaction")
            if (
                owned is None
                or owned is not backend.last_transaction
                or owned.closed
                or frame.f_locals.get("primary_error") is not body_primary
                or linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "transaction.close()"
            ):
                return False
            captured["transaction"] = owned
            captured["line_number"] = frame.f_lineno
            return True

        closed_before_fallback = False
        calls_before_fallback: tuple[Any, ...] = ()
        with (
            mock.patch.object(FakeTransaction, "orientation", new=raise_body_primary),
            mock.patch.object(FakeTransaction, "close", new=recording_close),
        ):
            try:
                self.assert_cleanup_trace_preserves_primary(
                    self.repair.RepairTool._resume_manifest.__code__,
                    cleanup_call_line,
                    lambda: self.repair.RepairTool(
                        self.config(), backend
                    )._resume_manifest(manifest, apply=True),
                    body_primary,
                    label="recovery transaction finally close",
                    events=("line",),
                    evidence=evidence,
                )
                closed_before_fallback = evidence["transaction"].closed
                calls_before_fallback = tuple(close_calls)
            finally:
                transaction = backend.last_transaction
                if transaction is not None and not transaction.closed:
                    real_close(transaction)

        self.assertIsInstance(evidence["line_number"], int)
        self.assertTrue(closed_before_fallback)
        self.assertEqual((evidence["transaction"],), calls_before_fallback)

    def test_recovery_transaction_close_failure_fallback_trace_second_drains(
        self,
    ) -> None:
        self.paired()
        for with_body_primary in (False, True):
            with self.subTest(with_body_primary=with_body_primary):
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root
                    / f"recovery-close-fallback-{with_body_primary}",
                )
                manifest = self.build_manifest(self.repair.Phase.PREPARED)
                manifest = self.repair._write_manifest(config, manifest)
                backend = FakeBackend(self.repair)
                backend.resume_orientation = (
                    self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
                )
                body_primary = KeyboardInterrupt(
                    "recovery transaction body primary before close fallback"
                )
                first_close_error = OSError(
                    errno.EIO,
                    "injected first recovery transaction close failure",
                )
                expected_primary: BaseException = (
                    body_primary if with_body_primary else first_close_error
                )
                evidence: Dict[str, Any] = {}
                close_calls: List[Any] = []
                real_close = FakeTransaction.close

                def raise_body_primary(
                    _transaction: Any, *_arguments: Any, **_keywords: Any
                ) -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def fail_first_close(transaction: Any) -> None:
                    close_calls.append(transaction)
                    if len(close_calls) == 1:
                        try:
                            raise first_close_error
                        except BaseException as error:
                            evidence["first_close_origin_traceback"] = (
                                error.__traceback__
                            )
                            if not with_body_primary:
                                evidence["body_origin_traceback"] = error.__traceback__
                            raise
                    real_close(transaction)

                def fallback_preamble(frame: Any, captured: Dict[str, Any]) -> bool:
                    transaction = frame.f_locals.get("transaction")
                    if (
                        transaction is None
                        or transaction is not backend.last_transaction
                        or transaction.closed
                        or frame.f_locals.get("cleanup_error") is not first_close_error
                        or frame.f_locals.get("primary_error")
                        is not (body_primary if with_body_primary else None)
                        or close_calls != [transaction]
                        or linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "_cleanup_guard = True"
                    ):
                        return False
                    captured["transaction"] = transaction
                    captured["line_number"] = frame.f_lineno
                    return True

                orientation_patch: Any = (
                    mock.patch.object(
                        FakeTransaction,
                        "orientation",
                        new=raise_body_primary,
                    )
                    if with_body_primary
                    else contextlib.nullcontext()
                )

                def operation() -> Any:
                    return self.repair.RepairTool(config, backend)._resume_manifest(
                        manifest, apply=True
                    )

                closed_before_fallback = False
                calls_before_fallback: tuple[Any, ...] = ()
                with (
                    self.repair.repair_lock(config),
                    orientation_patch,
                    mock.patch.object(FakeTransaction, "close", new=fail_first_close),
                ):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            self.repair.RepairTool._resume_manifest.__code__,
                            fallback_preamble,
                            operation,
                            expected_primary,
                            label=(
                                "recovery transaction close fallback "
                                f"{with_body_primary}"
                            ),
                            events=("line",),
                            evidence=evidence,
                        )
                        transaction = evidence["transaction"]
                        closed_before_fallback = transaction.closed
                        calls_before_fallback = tuple(close_calls)
                    finally:
                        transaction = backend.last_transaction
                        if transaction is not None and not transaction.closed:
                            real_close(transaction)

                self.assertIsInstance(evidence["line_number"], int)
                self.assertIsNotNone(evidence["first_close_origin_traceback"])
                self.assertTrue(closed_before_fallback)
                self.assertEqual(
                    (evidence["transaction"], evidence["transaction"]),
                    calls_before_fallback,
                )

    def test_state_enumeration_cannot_follow_replaced_manifest_or_intent_tree(
        self,
    ) -> None:
        cases = tuple(
            (label, timing, scope)
            for label in ("manifest", "intent")
            for timing in ("list", "read")
            for scope in ("child", "root")
        )
        for index, (label, timing, scope) in enumerate(cases):
            with self.subTest(label=label, timing=timing, scope=scope):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"load-race-{index}"
                )
                self.paired()
                if label == "manifest":
                    value = self.build_manifest(self.repair.Phase.PREPARED)
                    self.repair._write_manifest(config, value)
                    path = self.repair._manifest_path(config, UUID_A)
                    loader = self.repair._load_manifests
                else:
                    value = self.build_intent(self.repair.IntentState.PLANNED)
                    self.repair._write_intent(config, value)
                    path = self.repair._intent_path(config, UUID_A)
                    loader = self.repair._load_intents
                original = path.read_bytes()
                original_inode = path.stat().st_ino
                parent = path.parent
                moved = (
                    config.state_root.with_name(f"{config.state_root.name}.moved")
                    if scope == "root"
                    else parent.with_name(f"{parent.name}.moved")
                )
                remapped = False

                def remap() -> None:
                    nonlocal remapped
                    if remapped:
                        return
                    remapped = True
                    target = config.state_root if scope == "root" else parent
                    os.replace(target, moved)
                    parent.mkdir(parents=True, mode=0o700)
                    path.write_bytes(original)
                    path.chmod(0o600)
                    self.assertNotEqual(original_inode, path.stat().st_ino)

                real_listdir = self.repair.os.listdir
                real_read_record = self.repair._read_json_record

                def list_then_remap(target: Any) -> Any:
                    names = real_listdir(target)
                    if timing == "list":
                        remap()
                    return names

                def remap_then_read(
                    target: pathlib.Path,
                    *,
                    parent: Any = None,
                    harden: bool = True,
                ) -> Any:
                    if timing == "read":
                        remap()
                    return real_read_record(target, parent=parent, harden=harden)

                with (
                    mock.patch.object(
                        self.repair.os, "listdir", side_effect=list_then_remap
                    ),
                    mock.patch.object(
                        self.repair, "_read_json_record", side_effect=remap_then_read
                    ),
                    self.assertRaises(self.repair.FatalRepairError),
                ):
                    loader(config)

                self.assertTrue(remapped)
                self.assertEqual(original, path.read_bytes())
                old_path = (
                    moved / path.relative_to(config.state_root)
                    if scope == "root"
                    else moved / path.name
                )
                self.assertEqual(original, old_path.read_bytes())

    def test_intent_restart_cleans_single_valid_clone_then_continues_repair(
        self,
    ) -> None:
        self.paired(UUID_A)
        self.paired(UUID_B)
        intent = self.build_intent(
            self.repair.IntentState.STAGE_BOUND, queue_enabled=True
        )
        temporary = self.materialize_intent_stage(intent)
        self.repair._write_intent(self.config(), intent)
        backend = FakeBackend(self.repair)
        backend.intent_stage_disposition = "removed-clone"

        def remove_clone_stage() -> None:
            temporary.unlink()
            temporary.parent.rmdir()

        backend.intent_stage_cleanup_hook = remove_clone_stage

        receipt = self.repair.RepairTool(self.config(), backend).repair(
            apply=True, rollout_ids=[UUID_B]
        )

        results = self.results_by_id(receipt)
        self.assertEqual("recovery-intent-cleaned", results[UUID_A]["classification"])
        self.assertEqual("removed-clone", results[UUID_A]["stage"])
        self.assertEqual("repaired", results[UUID_B]["outcome"])
        self.assertLess(
            backend.events.index("recover_intent_stage"),
            backend.events.index("prepare"),
        )
        self.assertFalse(temporary.parent.exists())
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_intents(self.config()))

    def test_intent_recovery_honors_disabled_queue_after_clone_cleanup(self) -> None:
        self.paired()
        intent = self.build_intent(self.repair.IntentState.CLONE_BOUND)
        temporary = self.materialize_intent_stage(intent)
        self.repair._write_intent(self.config(), intent)
        backend = FakeBackend(self.repair)
        backend.intent_stage_disposition = "removed-clone"
        backend.intent_stage_cleanup_hook = lambda: (
            temporary.unlink(),
            temporary.parent.rmdir(),
        )

        receipt = self.repair.RepairTool(self.config(), backend).recover(apply=True)

        self.assertEqual(
            "recovery-intent-cleaned", receipt["results"][0]["classification"]
        )
        self.assertEqual([], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_intents(self.config()))

    def test_intent_dry_run_never_calls_backend_or_mutates_queue_and_evidence(
        self,
    ) -> None:
        self.paired()
        intent = self.build_intent(
            self.repair.IntentState.STAGE_BOUND, queue_enabled=True
        )
        temporary = self.materialize_intent_stage(intent)
        self.repair._write_intent(self.config(), intent)
        before = self.repair._intent_path(self.config(), UUID_A).read_bytes()
        backend = FakeBackend(self.repair)

        receipt = self.repair.RepairTool(self.config(), backend).recover(apply=False)

        result = receipt["results"][0]
        self.assertEqual("recovery-intent-pending", result["classification"])
        self.assertEqual("dry-run", result["outcome"])
        self.assertNotIn("recover_intent_stage", backend.events)
        self.assertEqual([], backend.recovered_intents)
        self.assertEqual(
            before, self.repair._intent_path(self.config(), UUID_A).read_bytes()
        )
        self.assertTrue(temporary.exists())
        self.assertFalse(self.config().queue_path.exists())

    def test_intent_queue_failure_retains_fence_then_absent_restart_queues_first(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        intent = self.build_intent(
            self.repair.IntentState.STAGE_BOUND, queue_enabled=True
        )
        temporary = self.materialize_intent_stage(intent)
        self.repair._write_intent(config, intent)
        backend = FakeBackend(self.repair)
        backend.intent_stage_disposition = "removed-clone"

        def remove_stage() -> None:
            temporary.unlink()
            temporary.parent.rmdir()

        backend.intent_stage_cleanup_hook = remove_stage
        injected = self.repair.AtomicWriteError(
            "injected queue publication failure",
            publication=self.repair.AtomicPublication.NOT_PUBLISHED,
        )
        with (
            mock.patch.object(self.repair, "_write_queue", side_effect=injected),
            self.assertRaises(self.repair.FatalRepairError),
        ):
            self.repair.RepairTool(config, backend).recover(apply=True)

        self.assertFalse(temporary.parent.exists())
        self.assertEqual(1, len(self.repair._load_intents(config)))
        self.assertFalse(config.queue_path.exists())

        restart = FakeBackend(self.repair)
        restart.intent_stage_disposition = "absent"
        events: List[str] = []
        original_write_queue = self.repair._write_queue
        original_delete_intent = self.repair._delete_intent

        def write_queue(
            current_config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(current_config, rollout_ids, queue_path)

        def delete_intent(current_config: Any, rollout_id: str) -> None:
            events.append("intent:delete")
            original_delete_intent(current_config, rollout_id)

        with (
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(self.repair, "_delete_intent", side_effect=delete_intent),
        ):
            receipt = self.repair.RepairTool(config, restart).recover(apply=True)

        result = receipt["results"][0]
        self.assertEqual("recovery-intent-cleaned", result["classification"])
        self.assertEqual("absent", result["stage"])
        self.assertLess(events.index("queue:add"), events.index("intent:delete"))
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))
        self.assertEqual([(intent, temporary)], restart.recovered_intents)

    def test_planned_present_empty_stage_queues_before_intent_delete(self) -> None:
        self.paired()
        config = self.config()
        intent = self.build_intent(self.repair.IntentState.PLANNED, queue_enabled=True)
        temporary = self.materialize_intent_stage(intent, clone=False)
        self.repair._write_intent(config, intent)
        backend = FakeBackend(self.repair)
        backend.intent_stage_disposition = "removed-empty"
        events: List[str] = []
        backend.intent_stage_cleanup_hook = lambda: (
            events.append("stage:removed"),
            temporary.parent.rmdir(),
        )
        original_write_queue = self.repair._write_queue
        original_delete_intent = self.repair._delete_intent

        def write_queue(
            current_config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(current_config, rollout_ids, queue_path)

        def delete_intent(current_config: Any, rollout_id: str) -> None:
            events.append("intent:delete")
            original_delete_intent(current_config, rollout_id)

        with (
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(self.repair, "_delete_intent", side_effect=delete_intent),
        ):
            receipt = self.repair.RepairTool(config, backend).recover(apply=True)

        result = receipt["results"][0]
        self.assertEqual("recovery-intent-cleaned", result["classification"])
        self.assertEqual("removed-empty", result["stage"])
        self.assertLess(events.index("stage:removed"), events.index("queue:add"))
        self.assertLess(events.index("queue:add"), events.index("intent:delete"))
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))
        self.assertFalse(temporary.parent.exists())
        self.assertEqual([(intent, temporary)], backend.recovered_intents)

    def test_planned_unsafe_present_stage_retains_intent_and_evidence(self) -> None:
        cases = ("clone", "extra-child", "unsafe-policy")
        for index, case in enumerate(cases, start=240):
            with self.subTest(case=case):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"planned-{case}"
                )
                self.paired()
                intent = self.build_intent(
                    self.repair.IntentState.PLANNED,
                    queue_enabled=True,
                    txid=f"{index:032x}",
                )
                temporary = self.materialize_intent_stage(intent, clone=case == "clone")
                if case == "extra-child":
                    (temporary.parent / "extra").write_bytes(b"unowned evidence")
                elif case == "unsafe-policy":
                    temporary.parent.chmod(0o777)
                self.repair._write_intent(config, intent)
                before_intent = self.repair._intent_path(config, UUID_A).read_bytes()
                backend = FakeBackend(self.repair)
                backend.intent_stage_error = self.repair.SafetyError(
                    f"unsafe PLANNED stage: {case}"
                )

                with self.assertRaises(self.repair.FatalRepairError):
                    self.repair.RepairTool(config, backend).recover(apply=True)

                self.assertEqual(
                    before_intent,
                    self.repair._intent_path(config, UUID_A).read_bytes(),
                )
                self.assertTrue(temporary.parent.exists())
                if case == "clone":
                    self.assertTrue(temporary.exists())
                if case == "extra-child":
                    self.assertTrue((temporary.parent / "extra").exists())
                self.assertFalse(config.queue_path.exists())
                self.assertEqual([(intent, temporary)], backend.recovered_intents)
                temporary.parent.chmod(0o700)
                if temporary.exists():
                    temporary.unlink()
                extra = temporary.parent / "extra"
                if extra.exists():
                    extra.unlink()
                temporary.parent.rmdir()

    def test_stage_bound_empty_and_clone_bound_absent_recovery_are_idempotent(
        self,
    ) -> None:
        cases = (
            (self.repair.IntentState.PLANNED, "absent", False),
            (self.repair.IntentState.STAGE_BOUND, "removed-empty", True),
            (self.repair.IntentState.CLONE_BOUND, "absent", False),
        )
        for index, (state, disposition, materialize) in enumerate(cases):
            with self.subTest(state=state.value):
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"intent-idempotent-{index}",
                )
                self.paired()
                intent = self.build_intent(state, txid=f"{index + 200:032x}")
                temporary = (
                    self.materialize_intent_stage(intent, clone=False)
                    if materialize
                    else self.mirror_root / intent.temporary_rel
                )
                self.repair._write_intent(config, intent)
                backend = FakeBackend(self.repair)
                backend.intent_stage_disposition = disposition
                if materialize:
                    backend.intent_stage_cleanup_hook = temporary.parent.rmdir

                receipt = self.repair.RepairTool(config, backend).recover(apply=True)

                result = receipt["results"][0]
                self.assertEqual("recovery-intent-cleaned", result["classification"])
                self.assertEqual(disposition, result["stage"])
                self.assertEqual([], self.repair._load_intents(config))
                self.assertFalse(temporary.parent.exists())
                self.assertEqual([(intent, temporary)], backend.recovered_intents)

    def test_intent_recovery_failure_retains_stage_and_durable_evidence(self) -> None:
        reasons = (
            "extra child",
            "symlink clone",
            "wrong owner",
            "wrong type",
            "unsafe link count",
            "clone identity mismatch",
            "stage identity mismatch",
        )
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"intent-fatal-{index}"
                )
                self.paired()
                intent = self.build_intent(
                    self.repair.IntentState.CLONE_BOUND,
                    txid=f"{index + 100:032x}",
                )
                temporary = self.materialize_intent_stage(intent)
                self.repair._write_intent(config, intent)
                before = self.repair._intent_path(config, UUID_A).read_bytes()
                backend = FakeBackend(self.repair)
                backend.intent_stage_error = self.repair.SafetyError(reason)

                with self.assertRaises(self.repair.FatalRepairError):
                    self.repair.RepairTool(config, backend).repair(apply=True)

                self.assertEqual(
                    before, self.repair._intent_path(config, UUID_A).read_bytes()
                )
                self.assertTrue(temporary.exists())
                self.assertNotIn("prepare", backend.events)
                self.assertEqual([], self.repair._load_queue(config))

    def test_intent_clone_cleanup_requires_proven_original_survivor(self) -> None:
        failures = (
            ("missing", self.repair.MissingPathError("final original is missing")),
            ("wrong-inode", self.repair.SafetyError("final inode changed")),
            (
                "content-mutation",
                self.repair.SafetyError("final content changed on the same inode"),
            ),
            (
                "policy-mutation",
                self.repair.SafetyError("final policy changed on the same inode"),
            ),
            (
                "unreadable",
                self.repair.UnreadablePathError("final original is unreadable"),
            ),
        )
        states = (
            self.repair.IntentState.STAGE_BOUND,
            self.repair.IntentState.CLONE_BOUND,
        )
        for state_index, state in enumerate(states):
            for failure_index, (case, error) in enumerate(failures):
                with self.subTest(state=state.value, case=case):
                    config = dataclasses.replace(
                        self.config(),
                        state_root=(
                            self.state_root
                            / f"intent-survivor-{state_index}-{failure_index}"
                        ),
                    )
                    self.paired()
                    intent = self.build_intent(
                        state,
                        queue_enabled=True,
                        txid=f"{300 + state_index * len(failures) + failure_index:032x}",
                    )
                    temporary = self.materialize_intent_stage(intent)
                    self.repair._write_intent(config, intent)
                    self.repair._write_queue(config, [UUID_B])
                    before_intent = self.repair._intent_path(
                        config, UUID_A
                    ).read_bytes()
                    before_queue = config.queue_path.read_bytes()
                    before_clone = temporary.read_bytes()
                    backend = FakeBackend(self.repair)
                    backend.intent_stage_error = error

                    with self.assertRaises(self.repair.FatalRepairError):
                        self.repair.RepairTool(config, backend).recover(apply=True)

                    self.assertEqual(
                        before_intent,
                        self.repair._intent_path(config, UUID_A).read_bytes(),
                    )
                    self.assertEqual(before_queue, config.queue_path.read_bytes())
                    self.assertEqual(before_clone, temporary.read_bytes())
                    self.assertTrue(temporary.parent.exists())
                    self.assertEqual([(intent, temporary)], backend.recovered_intents)
                    temporary.unlink()
                    temporary.parent.rmdir()

    def test_unreferenced_tool_artifacts_are_fatal_and_never_adopted(self) -> None:
        self.paired()
        container = next(
            (self.mirror_root / "archived_sessions").rglob("*.jsonl")
        ).parent
        target = container / "unowned-target"
        target.mkdir()
        for index, artifact_type in enumerate(("directory", "symlink", "regular")):
            with self.subTest(artifact_type=artifact_type):
                artifact = container / f".codex-reflink-repair-{index + 300:032x}"
                if artifact_type == "directory":
                    artifact.mkdir(mode=0o700)
                    (artifact / "clone").write_bytes(b"unowned evidence")
                elif artifact_type == "symlink":
                    os.symlink(target.name, artifact)
                else:
                    artifact.write_bytes(b"unowned evidence")
                backend = FakeBackend(self.repair)

                with self.assertRaises(self.repair.FatalRepairError):
                    self.repair.RepairTool(self.config(), backend).repair(apply=True)

                self.assertTrue(os.path.lexists(artifact))
                self.assertNotIn("recover_intent_stage", backend.events)
                self.assertNotIn("prepare", backend.events)

    def test_recover_prepared_swaps_then_postverifies_before_commit(self) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED)
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-done", receipt["results"][0]["classification"])
        self.assertLess(
            backend.events.index("swap_forward"), backend.events.index("postverify")
        )
        self.assertLess(
            backend.events.index("postverify"), backend.events.index("unlink_original")
        )

    def test_recovered_prepared_revalidates_all_snapshots_before_forward_swap(
        self,
    ) -> None:
        cases = (
            ("source", "policy", True),
            ("original", "content", False),
            ("clone", "policy", False),
        )
        for index, (target, property_name, retryable) in enumerate(cases):
            with self.subTest(target=target, property_name=property_name):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"recover-pre-{index}"
                )
                self.paired()
                manifest = self.build_manifest(
                    self.repair.Phase.PREPARED, queue_enabled=True
                )
                self.repair._write_manifest(config, manifest)
                backend = FakeBackend(self.repair)
                backend.resume_orientation = (
                    self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
                )
                backend.resume_snapshot_mutations.append((target, property_name))
                tool = self.repair.RepairTool(config, backend)

                if retryable:
                    receipt = tool.recover(apply=True)
                else:
                    with self.assertRaises(self.repair.FatalRepairError):
                        tool.recover(apply=True)

                self.assertIn("revalidate_pre_forward", backend.events)
                self.assertNotIn("swap_forward", backend.events)
                if retryable:
                    self.assertEqual(
                        "recovery-deferred",
                        receipt["results"][0]["classification"],
                    )
                    self.assertEqual([UUID_A], self.repair._load_queue(config))
                    self.assertEqual([], self.repair._load_manifests(config))
                else:
                    self.assertEqual([], self.repair._load_queue(config))
                    manifests = self.repair._load_manifests(config)
                    self.assertEqual(1, len(manifests))
                    self.assertEqual(self.repair.Phase.PREPARED, manifests[0].phase)

    def test_recovery_pre_forward_handler_trace_retries_deferred_compensation(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "recovery-handler-trace"
        )
        manifest = self.build_manifest(
            self.repair.Phase.PREPARED,
            queue_enabled=True,
        )
        self.repair._write_manifest(config, manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        body_primary = self.repair.UnstablePathError(
            "source changed during recovery pre-forward validation"
        )
        evidence: Dict[str, Any] = {}

        def fail_pre_forward(
            transaction: Any,
            _source: Any,
            _original: Any,
            _clone: Any,
        ) -> None:
            transaction.backend.events.append("revalidate_pre_forward")
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def recovery_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            transaction = frame.f_locals.get("transaction")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "current = authorization.state"
                or frame.f_locals.get("original_error") is not body_primary
                or transaction is None
                or transaction is not backend.last_transaction
                or "unlink_clone" in backend.events
            ):
                return False
            recovery_frame = frame.f_back
            while (
                recovery_frame is not None
                and recovery_frame.f_code
                is not self.repair.RepairTool._resume_manifest.__code__
            ):
                recovery_frame = recovery_frame.f_back
            if recovery_frame is None:
                return False
            action_receipt = recovery_frame.f_locals.get("recovery_defer_receipt")
            if action_receipt is None or action_receipt.dispatches != 1:
                return False
            captured["body_primary"] = body_primary
            captured["body_origin_traceback"] = evidence["body_origin_traceback"]
            captured["transaction"] = transaction
            captured["action_receipt"] = action_receipt
            captured["handler_line"] = frame.f_lineno
            return True

        with mock.patch.object(
            FakeTransaction,
            "revalidate_pre_forward",
            new=fail_pre_forward,
        ):
            self.assert_cleanup_trace_allows_completion(
                self.repair.RepairTool._defer_before_forward.__code__,
                recovery_handler_boundary,
                lambda: self.repair.RepairTool(config, backend).recover(apply=True),
                label="recovery pre-forward handler",
                evidence=evidence,
            )

        result = self.results_by_id(evidence["result"])[UUID_A]
        self.assertIsInstance(evidence["handler_line"], int)
        self.assertEqual("recovery-deferred", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(2, evidence["action_receipt"].dispatches)
        self.assertTrue(evidence["action_receipt"].completed)
        self.assertIn("unlink_clone", backend.events)
        self.assertIn("cleanup_stage", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertTrue(evidence["transaction"].closed)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_recovery_postverify_rollback_first_line_trace_retries_once(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "recovery-rollback-trace"
        )
        manifest = self.build_manifest(
            self.repair.Phase.PREPARED,
            queue_enabled=True,
        )
        self.repair._write_manifest(config, manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        body_primary = self.repair.UnstablePathError(
            "recovery postverify body primary before rollback trace"
        )
        backend.postverify_error = body_primary
        evidence: Dict[str, Any] = {}

        def rollback_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            transaction = frame.f_locals.get("transaction")
            if (
                "orientation = transaction.orientation("
                not in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
                or transaction is None
                or transaction is not backend.last_transaction
                or frame.f_locals.get("original_error") is not body_primary
                or "swap_back" in backend.events
            ):
                return False
            recovery_frame = frame.f_back
            while (
                recovery_frame is not None
                and recovery_frame.f_code
                is not self.repair.RepairTool._resume_manifest.__code__
            ):
                recovery_frame = recovery_frame.f_back
            if recovery_frame is None:
                return False
            receipt = recovery_frame.f_locals.get("recovery_postverify_receipt")
            if receipt is None or receipt.dispatches != 1:
                return False
            captured["body_primary"] = body_primary
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["transaction"] = transaction
            captured["action_receipt"] = receipt
            captured["rollback_line"] = frame.f_lineno
            return True

        self.assert_cleanup_trace_allows_completion(
            self.repair.RepairTool._rollback_after_postverify.__code__,
            rollback_boundary,
            lambda: self.repair.RepairTool(config, backend).recover(apply=True),
            label="recovery postverify rollback first line",
            evidence=evidence,
        )

        result = self.results_by_id(evidence["result"])[UUID_A]
        receipt = evidence["action_receipt"]
        self.assertIsInstance(evidence["rollback_line"], int)
        self.assertEqual("recovery-deferred", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(2, receipt.dispatches)
        self.assertFalse(receipt.completed)
        self.assertIsInstance(receipt.error, self.repair.SafeRolledBack)
        self.assertEqual(1, backend.events.count("swap_forward"))
        self.assertEqual(1, backend.events.count("swap_back"))
        self.assertEqual(1, backend.events.count("unlink_clone"))
        self.assertEqual(1, backend.events.count("cleanup_stage"))
        self.assertNotIn("unlink_original", backend.events)
        self.assertTrue(evidence["transaction"].closed)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_recovery_late_cleanup_sync_trace_resumes_from_durable_phase(
        self,
    ) -> None:
        for index, route in enumerate(("defer", "rollback")):
            with self.subTest(route=route):
                self.paired()
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"recovery-late-sync-{route}",
                )
                manifest = self.build_manifest(
                    self.repair.Phase.PREPARED,
                    queue_enabled=True,
                )
                backend = FakeBackend(self.repair)
                backend.resume_orientation = (
                    self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
                )
                body_primary = self.repair.UnstablePathError(
                    f"recovery {route} body primary before late namespace sync"
                )
                evidence: Dict[str, Any] = {}

                def fail_pre_forward(
                    transaction: Any,
                    _source: Any,
                    _original: Any,
                    _clone: Any,
                ) -> None:
                    transaction.backend.events.append("revalidate_pre_forward")
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def fail_postverify(
                    transaction: Any,
                    _source: Any,
                    _clone: Any,
                ) -> Any:
                    transaction.backend.events.append("postverify")
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                helper = (
                    self.repair.RepairTool._defer_before_forward
                    if route == "defer"
                    else self.repair.RepairTool._rollback_after_postverify
                )
                receipt_name = (
                    "recovery_defer_receipt"
                    if route == "defer"
                    else "recovery_postverify_receipt"
                )
                syncs_before = 0 if route == "defer" else 1

                def late_sync_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    transaction = frame.f_locals.get("transaction")
                    current = frame.f_locals.get("current")
                    authorization = frame.f_locals.get("authorization")
                    initial_manifest = frame.f_locals.get("manifest")
                    if (
                        linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "transaction.sync_namespaces()"
                        or frame.f_locals.get("original_error") is not body_primary
                        or transaction is not backend.last_transaction
                        or current is None
                        or current.phase is not self.repair.Phase.ROLLED_BACK
                        or authorization is None
                        or authorization.state is not current
                        or initial_manifest is None
                        or initial_manifest.phase is not self.repair.Phase.PREPARED
                        or transaction.current_orientation
                        is not self.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING
                        or backend.events.count("unlink_clone") != 1
                        or backend.events.count("sync_namespaces") != syncs_before
                    ):
                        return False
                    recovery_frame = frame.f_back
                    while (
                        recovery_frame is not None
                        and recovery_frame.f_code
                        is not self.repair.RepairTool._resume_manifest.__code__
                    ):
                        recovery_frame = recovery_frame.f_back
                    if recovery_frame is None:
                        return False
                    action_receipt = recovery_frame.f_locals.get(receipt_name)
                    if action_receipt is None or action_receipt.dispatches != 1:
                        return False
                    captured["body_primary"] = body_primary
                    captured["body_origin_traceback"] = evidence[
                        "body_origin_traceback"
                    ]
                    captured["transaction"] = transaction
                    captured["action_receipt"] = action_receipt
                    captured["sync_line"] = frame.f_lineno
                    return True

                fault_patch = mock.patch.object(
                    FakeTransaction,
                    ("revalidate_pre_forward" if route == "defer" else "postverify"),
                    new=(fail_pre_forward if route == "defer" else fail_postverify),
                )
                with self.record_manifests(backend.events), fault_patch:
                    self.repair._write_manifest(config, manifest)
                    self.assert_cleanup_trace_allows_completion(
                        helper.__code__,
                        late_sync_boundary,
                        lambda: self.repair.RepairTool(config, backend).recover(
                            apply=True
                        ),
                        label=f"recovery {route} late cleanup sync",
                        evidence=evidence,
                    )

                result = self.results_by_id(evidence["result"])[UUID_A]
                action_receipt = evidence["action_receipt"]
                relevant_phases = [
                    event for event in backend.events if event.startswith("manifest:")
                ]
                self.assertIsInstance(evidence["sync_line"], int)
                self.assertEqual("ok", evidence["result"]["status"])
                self.assertEqual("recovery-deferred", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertEqual(2, action_receipt.dispatches)
                if route == "defer":
                    self.assertTrue(action_receipt.completed)
                    self.assertIsNone(action_receipt.error)
                    self.assertNotIn("swap_forward", backend.events)
                    self.assertNotIn("swap_back", backend.events)
                    self.assertEqual(1, backend.events.count("sync_namespaces"))
                else:
                    self.assertFalse(action_receipt.completed)
                    self.assertIsInstance(
                        action_receipt.error, self.repair.SafeRolledBack
                    )
                    self.assertEqual(1, backend.events.count("swap_forward"))
                    self.assertEqual(1, backend.events.count("swap_back"))
                    self.assertEqual(2, backend.events.count("sync_namespaces"))
                self.assertEqual(
                    [
                        "manifest:PREPARED",
                        "manifest:ROLLBACK_READY",
                        "manifest:ROLLED_BACK",
                        "manifest:DEFERRED",
                    ],
                    relevant_phases,
                )
                self.assertEqual(1, backend.events.count("unlink_clone"))
                self.assertEqual(1, backend.events.count("cleanup_stage"))
                self.assertNotIn("unlink_original", backend.events)
                self.assertTrue(evidence["transaction"].closed)
                self.assertEqual([UUID_A], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_manifests(config))

    def test_recover_prepared_with_missing_source_abandons_preswap_clone_and_defers(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED, queue_enabled=True)
        self.repair._write_manifest(self.config(), manifest)
        source = self.source_root / manifest.source_rel
        source.unlink()
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-deferred", receipt["results"][0]["classification"])
        self.assertEqual([None], backend.resume_sources)
        self.assertIn("unlink_clone", backend.events)
        self.assertIn("cleanup_stage", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertNotIn("postverify", backend.events)
        self.assertNotIn("swap_back", backend.events)
        self.assertNotIn("unlink_original", backend.events)
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_recover_prepared_source_parent_replacement_falls_back_to_safe_defer(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED, queue_enabled=True)
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_source_error = self.repair.UnstablePathError(
            "source parent differs from durable identity"
        )
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP

        receipt = self.repair.RepairTool(self.config(), backend).recover(apply=True)

        result = receipt["results"][0]
        self.assertEqual("recovery-deferred", result["classification"])
        self.assertEqual(
            [self.source_root / manifest.source_rel, None],
            backend.resume_sources,
        )
        self.assertNotIn("swap_forward", backend.events)
        self.assertNotIn("swap_back", backend.events)
        self.assertNotIn("unlink_original", backend.events)
        self.assertIn("unlink_clone", backend.events)
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_intent_recovery_ignores_relocated_source_parent_after_clone_fence(
        self,
    ) -> None:
        self.paired()
        config = self.config()
        intent = self.build_intent(
            self.repair.IntentState.STAGE_BOUND, queue_enabled=True
        )
        temporary = self.materialize_intent_stage(intent)
        self.repair._write_intent(config, intent)
        source = self.source_root / intent.source_rel
        self.replace_parent_preserving_leaf(source, suffix="intent-moved")
        backend = FakeBackend(self.repair)
        backend.intent_stage_disposition = "removed-clone"
        backend.intent_stage_cleanup_hook = lambda: (
            temporary.unlink(),
            temporary.parent.rmdir(),
        )

        receipt = self.repair.RepairTool(config, backend).recover(apply=True)

        result = receipt["results"][0]
        self.assertEqual("recovery-intent-cleaned", result["classification"])
        self.assertEqual("removed-clone", result["stage"])
        self.assertNotIn("resume", backend.events)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))

    def test_durable_intent_mapping_authorizes_clone_and_stage_cleanup(self) -> None:
        cases = (
            ("intent_unlink_clone", "removed-clone"),
            ("intent_remove_stage", "removed-empty"),
        )
        for index, (action, disposition) in enumerate(cases):
            for scope in ("leaf", "child", "root"):
                with self.subTest(action=action, scope=scope):
                    config = dataclasses.replace(
                        self.config(),
                        state_root=self.state_root / f"intent-auth-{index}-{scope}",
                    )
                    self.paired()
                    intent = self.build_intent(
                        self.repair.IntentState.STAGE_BOUND,
                        queue_enabled=True,
                        txid=f"{700 + index:032x}",
                    )
                    self.repair._write_intent(config, intent)
                    backend = FakeBackend(self.repair)
                    backend.intent_stage_disposition = disposition
                    retained: List[pathlib.Path] = []

                    def remap() -> None:
                        retained.append(
                            self.remap_private_state_record(
                                config,
                                self.repair._intent_path(config, UUID_A),
                                scope=scope,
                                suffix=f"{action}-moved",
                            )
                        )

                    backend.during_authorize_hooks[action] = remap
                    with self.assertRaises(self.repair.FatalRepairError):
                        self.repair.RepairTool(config, backend).recover(apply=True)

                    self.assertEqual(1, len(retained))
                    self.assertTrue(retained[0].exists())
                    self.assertNotIn(action, backend.events)
                    self.assertFalse(config.queue_path.exists())

    def test_recover_prepared_after_source_move_rolls_back_then_retry_relocates_id(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED, queue_enabled=True)
        self.repair._write_manifest(self.config(), manifest)
        old_source = self.source_root / manifest.source_rel
        moved_source = self.rollout_path(self.source_root, "archived_sessions", UUID_A)
        moved_source.parent.mkdir(parents=True, exist_ok=True)
        old_source.replace(moved_source)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-deferred", receipt["results"][0]["classification"])
        self.assertEqual([None], backend.resume_sources)
        self.assertLess(
            backend.events.index("revalidate_forward"),
            backend.events.index("swap_back"),
        )
        self.assertLess(
            backend.events.index("swap_back"), backend.events.index("unlink_clone")
        )
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

        retry = self.repair.RepairTool(self.config(), FakeBackend(self.repair)).retry(
            apply=True
        )
        self.assertEqual("repaired", self.results_by_id(retry)[UUID_A]["outcome"])
        self.assertEqual([], self.repair._load_queue(self.config()))

    def test_recover_commit_ready_with_missing_old_finishes_without_unlink(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.COMMIT_READY)
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.CLONE_FINAL_TEMP_MISSING
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-done", receipt["results"][0]["classification"])
        self.assertIn("sync_namespaces", backend.events)
        self.assertNotIn("swap_forward", backend.events)
        self.assertNotIn("postverify", backend.events)
        self.assertNotIn("unlink_original", backend.events)

    def test_terminal_missing_temporary_revalidates_live_final_before_cleanup(
        self,
    ) -> None:
        cases = (
            (
                self.repair.Phase.COMMIT_READY,
                self.repair.Orientation.CLONE_FINAL_TEMP_MISSING,
                "clone",
                "content",
                "revalidate_committed",
            ),
            (
                self.repair.Phase.ROLLED_BACK,
                self.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING,
                "original",
                "policy",
                "revalidate_rolled_back",
            ),
        )
        for index, (phase, orientation, target, property_name, validation) in enumerate(
            cases
        ):
            with self.subTest(phase=phase.value):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"missing-temp-{index}"
                )
                self.paired()
                manifest = self.build_manifest(phase, retryable=True)
                self.repair._write_manifest(config, manifest)
                before = self.repair._manifest_path(config, UUID_A).read_bytes()
                backend = FakeBackend(self.repair)
                backend.resume_orientation = orientation
                backend.resume_snapshot_mutations.append((target, property_name))

                with self.assertRaises(self.repair.FatalRepairError):
                    self.repair.RepairTool(config, backend).recover(apply=True)

                self.assertIn(validation, backend.events)
                self.assertNotIn("cleanup_stage", backend.events)
                self.assertEqual([], self.repair._load_queue(config))
                self.assertEqual(
                    before, self.repair._manifest_path(config, UUID_A).read_bytes()
                )

    def test_recover_rolled_back_missing_clone_repeats_namespace_fence(
        self,
    ) -> None:
        self.paired()
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "rolled-back-missing-fence"
        )
        manifest = self.build_manifest(
            self.repair.Phase.ROLLED_BACK,
            retryable=True,
            queue_enabled=True,
        )
        self.repair._write_manifest(config, manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.ORIGINAL_FINAL_TEMP_MISSING

        with self.record_manifests(backend.events):
            receipt = self.repair.RepairTool(config, backend).recover(apply=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("ok", receipt["status"])
        self.assertEqual("recovery-deferred", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertNotIn("unlink_clone", backend.events)
        self.assertIn("sync_namespaces", backend.events)
        self.assertLess(
            backend.events.index("sync_namespaces"),
            backend.events.index("revalidate_rolled_back"),
        )
        self.assertLess(
            backend.events.index("revalidate_rolled_back"),
            backend.events.index("cleanup_stage"),
        )
        self.assertLess(
            backend.events.index("cleanup_stage"),
            backend.events.index("manifest:DEFERRED"),
        )
        self.assertTrue(backend.last_transaction.closed)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))

    def test_recover_done_only_cleans_queue_and_manifest_without_resuming_backend(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.DONE, queue_enabled=True)
        self.repair._write_manifest(self.config(), manifest)
        self.repair._write_queue(self.config(), [UUID_A])
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-done", receipt["results"][0]["classification"])
        self.assertNotIn("resume", backend.events)
        self.assertEqual([], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_queue_disabled_terminal_recovery_preserves_independent_queue(self) -> None:
        for index, phase in enumerate(
            (self.repair.Phase.DONE, self.repair.Phase.DEFERRED)
        ):
            with self.subTest(phase=phase.value):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"independent-{index}"
                )
                self.paired()
                manifest = self.build_manifest(
                    phase,
                    retryable=phase == self.repair.Phase.DEFERRED,
                    queue_enabled=False,
                )
                self.repair._write_manifest(config, manifest)
                self.repair._write_queue(config, [UUID_A, UUID_B])
                before = config.queue_path.read_bytes()

                receipt = self.repair.RepairTool(
                    config, FakeBackend(self.repair)
                ).recover(apply=True)

                self.assertIn(
                    receipt["results"][0]["classification"],
                    {"recovery-done", "recovery-deferred"},
                )
                self.assertEqual(before, config.queue_path.read_bytes())
                self.assertEqual([UUID_A, UUID_B], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_manifests(config))

    def test_recover_rollback_ready_restores_original_then_queues_retry(self) -> None:
        self.paired()
        manifest = self.build_manifest(
            self.repair.Phase.ROLLBACK_READY,
            retryable=True,
            queue_enabled=True,
        )
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP
        tool = self.repair.RepairTool(self.config(), backend)
        events = backend.events
        original_write_queue = self.repair._write_queue
        original_delete_manifest = self.repair._delete_manifest

        def write_queue(
            config: Any,
            rollout_ids: Iterable[str],
            queue_path: Optional[pathlib.Path] = None,
        ) -> None:
            events.append("queue:add")
            original_write_queue(config, rollout_ids, queue_path)

        def delete_manifest(config: Any, rollout_id: str) -> None:
            events.append("manifest:delete")
            original_delete_manifest(config, rollout_id)

        with (
            self.record_manifests(events),
            mock.patch.object(self.repair, "_write_queue", side_effect=write_queue),
            mock.patch.object(
                self.repair, "_delete_manifest", side_effect=delete_manifest
            ),
        ):
            receipt = tool.recover(apply=True)

        self.assertEqual("recovery-deferred", receipt["results"][0]["classification"])
        self.assertLess(
            backend.events.index("swap_back"), backend.events.index("unlink_clone")
        )
        self.assertLess(events.index("unlink_clone"), events.index("cleanup_stage"))
        self.assertLess(
            events.index("cleanup_stage"), events.index("manifest:DEFERRED")
        )
        self.assertLess(events.index("manifest:DEFERRED"), events.index("queue:add"))
        self.assertLess(events.index("queue:add"), events.index("manifest:delete"))
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_recover_deferred_adds_default_queue_then_deletes_manifest(self) -> None:
        self.paired()
        manifest = self.build_manifest(
            self.repair.Phase.DEFERRED,
            retryable=True,
            queue_enabled=True,
        )
        self.repair._write_manifest(self.config(), manifest)
        self.repair._write_queue(self.config(), [UUID_B])
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        receipt = tool.recover(apply=True)

        self.assertEqual("recovery-deferred", receipt["results"][0]["classification"])
        self.assertEqual("deferred", receipt["results"][0]["outcome"])
        self.assertNotIn("resume", backend.events)
        self.assertEqual([UUID_A, UUID_B], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))

    def test_recover_failed_is_fatal_and_preserves_manifest_without_queueing(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.FAILED, retryable=False)
        self.repair._write_manifest(self.config(), manifest)
        before = self.repair._manifest_path(self.config(), UUID_A).read_bytes()
        backend = FakeBackend(self.repair)
        tool = self.repair.RepairTool(self.config(), backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.recover(apply=True)

        self.assertNotIn("resume", backend.events)
        self.assertEqual([], self.repair._load_queue(self.config()))
        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.FAILED, manifests[0].phase)
        self.assertEqual(
            before, self.repair._manifest_path(self.config(), UUID_A).read_bytes()
        )

    def test_recover_nonretryable_rollback_ready_finishes_cleanup_then_fails_closed(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(
            self.repair.Phase.ROLLBACK_READY, retryable=False
        )
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP
        tool = self.repair.RepairTool(self.config(), backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.recover(apply=True)

        self.assertLess(
            backend.events.index("swap_back"), backend.events.index("unlink_clone")
        )
        self.assertLess(
            backend.events.index("unlink_clone"), backend.events.index("cleanup_stage")
        )
        self.assertEqual([], self.repair._load_queue(self.config()))
        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertEqual(self.repair.Phase.FAILED, manifests[0].phase)

    def test_recovery_impossible_orientation_is_fatal_and_preserves_manifest(
        self,
    ) -> None:
        self.paired()
        manifest = self.build_manifest(self.repair.Phase.PREPARED)
        self.repair._write_manifest(self.config(), manifest)
        backend = FakeBackend(self.repair)
        backend.resume_orientation = self.repair.Orientation.IMPOSSIBLE
        tool = self.repair.RepairTool(self.config(), backend)

        with self.assertRaises(self.repair.FatalRepairError):
            tool.recover(apply=True)

        self.assertEqual(1, len(self.repair._load_manifests(self.config())))
        self.assertNotIn("swap_forward", backend.events)
        self.assertNotIn("unlink_original", backend.events)
        self.assertNotIn("unlink_clone", backend.events)

    def test_recovery_revalidates_same_inode_snapshots_before_each_destructive_step(
        self,
    ) -> None:
        self.paired()
        cases = (
            (
                self.repair.Phase.COMMIT_READY,
                self.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP,
                ("clone", "policy"),
                "revalidate_forward",
                "unlink_original",
            ),
            (
                self.repair.Phase.ROLLBACK_READY,
                self.repair.Orientation.CLONE_FINAL_ORIGINAL_TEMP,
                ("original", "content"),
                "revalidate_forward",
                "swap_back",
            ),
            (
                self.repair.Phase.ROLLED_BACK,
                self.repair.Orientation.ORIGINAL_FINAL_CLONE_TEMP,
                ("clone", "content"),
                "revalidate_before_cleanup",
                "unlink_clone",
            ),
        )
        for phase, orientation, mutation, validation, forbidden in cases:
            with self.subTest(phase=phase.value):
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / phase.value.lower()
                )
                manifest = self.build_manifest(phase, retryable=True)
                self.repair._write_manifest(config, manifest)
                backend = FakeBackend(self.repair)
                backend.resume_orientation = orientation
                backend.resume_snapshot_mutations.append(mutation)
                tool = self.repair.RepairTool(config, backend)

                with self.assertRaises(self.repair.FatalRepairError):
                    tool.recover(apply=True)

                self.assertIn(validation, backend.events)
                self.assertNotIn(forbidden, backend.events)
                manifests = self.repair._load_manifests(config)
                self.assertEqual(1, len(manifests))
                self.assertEqual(phase, manifests[0].phase)


@unittest.skipUnless(sys.platform == "darwin", "requires Darwin clonefile primitives")
class DarwinBackendIntegrationTests(FilesystemFixture):
    def setUp(self) -> None:
        super().setUp()
        self.darwin = load_module("codex_reflink_darwin", DARWIN_BACKEND_PATH)

    def prepare_pair(self, *, create_stage: bool = True) -> Any:
        content = b'{"type":"event","payload":"apfs-integration"}\n'
        source = self.write_rollout(self.source_root, "sessions", UUID_A, content)
        mirror = self.write_rollout(
            self.mirror_root, "archived_sessions", UUID_A, content
        )
        timestamp_ns = 1_700_000_000_000_000_000
        for path in (source, mirror):
            path.chmod(0o600)
            os.utime(path, ns=(timestamp_ns, timestamp_ns))
            subprocess.run(
                [
                    "/usr/bin/xattr",
                    "-w",
                    "com.openai.codex.reflink-test",
                    "policy-value",
                    str(path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        stage = mirror.parent / ".codex-reflink-repair-integration"
        if create_stage:
            stage.mkdir(mode=0o700)
            stage.chmod(0o700)
        return source, mirror, stage, stage / "clone"

    @staticmethod
    def expectation_for_path(backend: Any, path: pathlib.Path) -> Any:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            return backend.snapshot_expectation(backend.snapshot_file(descriptor))
        finally:
            os.close(descriptor)

    @staticmethod
    def identity_for_directory(backend: Any, path: pathlib.Path) -> Any:
        descriptor = backend.open_absolute_dir(str(path))
        try:
            return backend.identity(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def allow_state_mutation(_action: str) -> None:
        return None

    def mutate_same_inode(self, path: pathlib.Path, mutation: str) -> None:
        before = path.stat()
        if mutation == "content":
            with path.open("r+b", buffering=0) as stream:
                first = stream.read(1)
                self.assertTrue(first)
                stream.seek(0)
                stream.write(bytes([first[0] ^ 1]))
                stream.flush()
                os.fsync(stream.fileno())
        elif mutation == "policy":
            path.chmod(0o660)
        else:
            self.fail(f"unknown mutation kind: {mutation}")
        after = path.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(1, after.st_nlink)

    @staticmethod
    def open_fd_set(limit: int = 512) -> set[int]:
        descriptors: set[int] = set()
        for descriptor in range(limit):
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
            else:
                descriptors.add(descriptor)
        return descriptors

    def prepare_intent_clone_artifact(self, txid: str) -> Any:
        content = b'{"type":"event","payload":"intent-crash"}\n'
        source = self.write_rollout(self.source_root, "sessions", UUID_A, content)
        mirror = self.write_rollout(
            self.mirror_root, "archived_sessions", UUID_A, content
        )
        stage = mirror.parent / f".codex-reflink-repair-{txid}"
        backend = self.darwin.DarwinBackend()
        original_expectation = self.expectation_for_path(backend, mirror)
        container_fd = backend.open_absolute_dir(str(stage.parent))
        try:
            container_identity = backend.validate_stage_container(container_fd)
        finally:
            os.close(container_fd)
        stage_identity = backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        source_fd = os.open(source, os.O_RDONLY)
        stage_fd = backend.open_absolute_dir(str(stage))
        clone_fd = -1
        try:
            try:
                clone_fd = backend.strict_clone(
                    source_fd,
                    stage_fd,
                    "clone",
                    authorize_state=self.allow_state_mutation,
                )
            except self.darwin.BackendError as error:
                if error.errno_value in (errno.ENOTSUP, errno.EXDEV):
                    self.skipTest(f"filesystem does not support strict clone: {error}")
                raise
            clone_identity = backend.identity(clone_fd)
            clone_snapshot = backend.snapshot_file(clone_fd)
            clone_sha256 = clone_snapshot.sha256
            clone_expectation = backend.snapshot_expectation(clone_snapshot)
        finally:
            if clone_fd >= 0:
                os.close(clone_fd)
            os.close(stage_fd)
            os.close(source_fd)
        return (
            backend,
            source,
            mirror,
            stage,
            container_identity,
            original_expectation,
            stage_identity,
            clone_identity,
            clone_sha256,
            clone_expectation,
        )

    def test_real_stage_and_clone_creation_require_state_authorization(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'f' * 32}"
        backend = self.darwin.DarwinBackend()
        actions: List[str] = []

        def reject(action: str) -> None:
            actions.append(action)
            raise RuntimeError(f"reject {action}")

        with self.assertRaisesRegex(RuntimeError, "reject create_stage"):
            backend.create_private_stage(str(stage), authorize_state=reject)
        self.assertEqual(["create_stage"], actions)
        self.assertFalse(stage.exists())

        backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        source_fd = os.open(source, os.O_RDONLY)
        stage_fd = backend.open_absolute_dir(str(stage))
        actions.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "reject create_clone"):
                backend.strict_clone(
                    source_fd,
                    stage_fd,
                    "clone",
                    authorize_state=reject,
                )
            self.assertEqual(["create_clone"], actions)
            self.assertFalse((stage / "clone").exists())
        finally:
            os.close(stage_fd)
            os.close(source_fd)

    def test_private_stage_identity_keyboard_interrupt_drains_fd_and_cleans_stage(
        self,
    ) -> None:
        _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'b' * 32}"
        darwin = self.darwin
        sentinel = KeyboardInterrupt("stage identity interrupted")
        actions: List[str] = []

        class InterruptedStageBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.container_fd = -1
                self.stage_fds: List[int] = []
                self.interrupted = False
                super().__init__()

            def identity(self, descriptor: int) -> Any:
                if descriptor != self.container_fd and not self.interrupted:
                    self.stage_fds.append(descriptor)
                    self.interrupted = True
                    raise sentinel
                return super().identity(descriptor)

        backend = InterruptedStageBackend()
        container_fd = backend.open_absolute_dir(str(stage.parent))
        backend.container_fd = container_fd
        baseline = self.open_fd_set()

        def authorize(action: str) -> None:
            actions.append(action)

        try:
            with self.assertRaises(KeyboardInterrupt) as caught:
                backend.create_private_stage_parent(
                    container_fd,
                    stage.name,
                    authorize_state=authorize,
                )

            self.assertIs(sentinel, caught.exception)
            self.assertEqual(["create_stage", "remove_stage"], actions)
            self.assertEqual(1, len(backend.stage_fds))
            with self.assertRaises(OSError) as closed:
                os.fstat(backend.stage_fds[0])
            self.assertEqual(errno.EBADF, closed.exception.errno)
            self.assertEqual(baseline, self.open_fd_set())
            self.assertFalse(stage.exists())
        finally:
            os.close(container_fd)

    def test_open_absolute_dir_drains_new_component_when_old_close_fails(self) -> None:
        target = self.root / "open-drain" / "child"
        target.mkdir(parents=True)
        backend = self.darwin.DarwinBackend()
        baseline = self.open_fd_set()
        close_calls: List[int] = []
        real_close = self.darwin.os.close

        def fail_first_close(descriptor: int) -> None:
            close_calls.append(descriptor)
            real_close(descriptor)
            if len(close_calls) == 1:
                raise OSError(errno.EIO, "injected component close failure")

        with (
            mock.patch.object(self.darwin.os, "close", side_effect=fail_first_close),
            self.assertRaises(self.darwin.BackendError) as caught,
        ):
            backend.open_absolute_dir(str(target))

        self.assertEqual("close_failed", caught.exception.reason)
        self.assertEqual(2, len(close_calls))
        self.assertEqual(2, len(set(close_calls)))
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_inspection_preserves_primary_and_drains_every_owned_fd(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        sentinel = RuntimeError("injected inspection primary")
        armed = False
        close_calls: List[int] = []
        real_close = self.repair.os.close

        def fail_snapshot(_descriptor: int) -> Any:
            nonlocal armed
            armed = True
            raise sentinel

        def fail_first_owned_close(descriptor: int) -> None:
            real_close(descriptor)
            if not armed:
                return
            close_calls.append(descriptor)
            if len(close_calls) == 1:
                raise OSError(errno.EIO, "injected inspection close failure")

        baseline = self.open_fd_set()
        with (
            mock.patch.object(adapter.raw, "snapshot_file", side_effect=fail_snapshot),
            mock.patch.object(
                self.repair.os, "close", side_effect=fail_first_owned_close
            ),
            self.assertRaises(self.repair.SafetyError) as raised,
        ):
            adapter.inspect_pair(source, mirror)

        self.assertIs(sentinel, raised.exception.__cause__)
        self.assertEqual(4, len(close_calls))
        self.assertEqual(4, len(set(close_calls)))
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_inspection_trace_handoff_closes_each_owner_once(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        baseline = self.open_fd_set()
        boundary_active = False
        boundary_fds: set[int] = set()
        close_calls: List[int] = []
        real_close = self.darwin.os.close

        def record_boundary_close(descriptor: int) -> None:
            if boundary_active and descriptor in boundary_fds:
                close_calls.append(descriptor)
            real_close(descriptor)

        def acquired_all_owners(frame: Any, evidence: Dict[str, Any]) -> bool:
            nonlocal boundary_active
            names = (
                "source_parent_owner",
                "mirror_parent_owner",
                "source_owner",
                "mirror_owner",
            )
            owners = [frame.f_locals.get(name) for name in names]
            if any(owner is None or owner.closed for owner in owners):
                return False
            registered_owners = frame.f_locals.get("owners")
            if (
                not isinstance(registered_owners, list)
                or owners[-1] in registered_owners
            ):
                return False
            evidence["owners"] = tuple(owners)
            evidence["fds"] = tuple(owner.fileno() for owner in owners)
            evidence["line_number"] = frame.f_lineno
            boundary_fds.update(evidence["fds"])
            boundary_active = True
            return True

        with mock.patch.object(
            self.darwin.os, "close", side_effect=record_boundary_close
        ):
            evidence = self.assert_trace_interruption(
                adapter.inspect_pair.__func__.__code__,
                acquired_all_owners,
                lambda: adapter.inspect_pair(source, mirror),
                label="inspect owner handoff",
            )

        owned_fds = evidence["fds"]
        self.assertIsInstance(evidence["line_number"], int)
        self.assertEqual(4, len(set(owned_fds)))
        for descriptor in owned_fds:
            self.assertEqual(1, close_calls.count(descriptor))
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertTrue(all(owner.closed for owner in evidence["owners"]))
        self.assertEqual(baseline, self.open_fd_set())

        inspection = adapter.inspect_pair(source, mirror)
        self.assertEqual(self.repair.ContentRelation.EXACT, inspection.relation)
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_inspection_cleanup_trace_retries_first_owner(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        body_primary = KeyboardInterrupt("inspection body primary")
        evidence: Dict[str, Any] = {}
        owners: List[Any] = []
        close_calls: List[int] = []
        baseline = self.open_fd_set()
        real_parent_factory = adapter.raw._open_absolute_parent_owned
        real_leaf_factory = adapter.raw._open_leaf_owned
        real_close = self.darwin.os.close

        def recording_parent_factory(*arguments: Any, **keywords: Any) -> Any:
            owner, name = real_parent_factory(*arguments, **keywords)
            owners.append(owner)
            return owner, name

        def recording_leaf_factory(*arguments: Any, **keywords: Any) -> Any:
            owner = real_leaf_factory(*arguments, **keywords)
            owners.append(owner)
            return owner

        def fail_relation(*_arguments: Any, **_keywords: Any) -> None:
            evidence["fds"] = tuple(owner.fileno() for owner in owners)
            try:
                raise body_primary
            except BaseException as error:
                evidence["body_origin_traceback"] = error.__traceback__
                raise

        def record_close(descriptor: int) -> None:
            if descriptor in evidence.get("fds", ()):
                close_calls.append(descriptor)
            real_close(descriptor)

        def cleanup_entry(frame: Any, _captured: Dict[str, Any]) -> bool:
            return (
                frame.f_locals.get("primary_error") is body_primary
                and frame.f_locals.get("self") in owners
                and not frame.f_locals["self"].closed
            )

        with (
            mock.patch.object(
                adapter.raw,
                "_open_absolute_parent_owned",
                side_effect=recording_parent_factory,
            ),
            mock.patch.object(
                adapter.raw,
                "_open_leaf_owned",
                side_effect=recording_leaf_factory,
            ),
            mock.patch.object(adapter, "_relation", side_effect=fail_relation),
            mock.patch.object(self.darwin.os, "close", side_effect=record_close),
        ):
            try:
                self.assert_cleanup_trace_preserves_primary(
                    self.darwin._OwnedFD.close.__code__,
                    cleanup_entry,
                    lambda: adapter.inspect_pair(source, mirror),
                    body_primary,
                    label="adapter inspection owner cleanup",
                    evidence=evidence,
                )
            finally:
                for owner in owners:
                    owner.close(primary_error=body_primary)

        self.assertEqual(4, len(owners))
        self.assertEqual(4, len(set(evidence["fds"])))
        self.assertTrue(all(owner.closed for owner in owners))
        for descriptor in evidence["fds"]:
            self.assertEqual(1, close_calls.count(descriptor))
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_inspection_natural_close_error_survives_handler_trace(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        natural_error = OSError(errno.EIO, "inspection natural owner close error")
        evidence: Dict[str, Any] = {}
        owners: List[Any] = []
        owned_fds: set[int] = set()
        close_calls: List[int] = []
        baseline = self.open_fd_set()
        real_parent_factory = adapter.raw._open_absolute_parent_owned
        real_leaf_factory = adapter.raw._open_leaf_owned
        real_close = self.darwin.os.close
        natural_raised = False

        def record_owner(owner: Any) -> Any:
            real_owner_close = owner.close

            def close_owner(
                *,
                primary_error: Optional[BaseException] = None,
                durable_namespace_complete: bool = False,
            ) -> None:
                nonlocal natural_raised
                was_open = not owner.closed
                if was_open:
                    owned_fds.add(owner.fileno())
                real_owner_close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
                if was_open and not natural_raised:
                    natural_raised = True
                    try:
                        raise natural_error
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

            owner.close = close_owner
            owners.append(owner)
            return owner

        def recording_parent_factory(*arguments: Any, **keywords: Any) -> Any:
            owner, name = real_parent_factory(*arguments, **keywords)
            return record_owner(owner), name

        def recording_leaf_factory(*arguments: Any, **keywords: Any) -> Any:
            owner = real_leaf_factory(*arguments, **keywords)
            return record_owner(owner)

        def record_close(descriptor: int) -> None:
            if descriptor in owned_fds:
                close_calls.append(descriptor)
            real_close(descriptor)

        def cleanup_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            owned_handles = frame.f_locals.get("owned_handles")
            source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            if (
                source != "_cleanup_guard = True"
                or frame.f_locals.get("cleanup_error") is not natural_error
                or frame.f_locals.get("primary_error") is not None
            ):
                return False
            captured["natural_cleanup_error"] = frame.f_locals["cleanup_error"]
            captured["owners"] = tuple(owners)
            captured["owned_handles"] = owned_handles
            captured["fds"] = tuple(owned_fds)
            captured["close_calls_at_handler"] = tuple(close_calls)
            captured["cleanup_handler_line"] = frame.f_lineno
            return True

        with (
            mock.patch.object(
                adapter.raw,
                "_open_absolute_parent_owned",
                side_effect=recording_parent_factory,
            ),
            mock.patch.object(
                adapter.raw,
                "_open_leaf_owned",
                side_effect=recording_leaf_factory,
            ),
            mock.patch.object(
                self.darwin.os,
                "close",
                side_effect=record_close,
            ),
        ):
            self.assert_cleanup_trace_preserves_primary(
                adapter.inspect_pair.__func__.__code__,
                cleanup_handler_boundary,
                lambda: adapter.inspect_pair(source, mirror),
                natural_error,
                label="adapter inspection natural close handler",
                events=("line",),
                evidence=evidence,
            )

        self.assertIsInstance(evidence["cleanup_handler_line"], int)
        self.assertEqual(4, len(evidence["owners"]))
        self.assertIsInstance(evidence["owned_handles"], tuple)
        self.assertEqual(set(evidence["owners"]), set(evidence["owned_handles"]))
        self.assertEqual(set(evidence["fds"]), set(evidence["close_calls_at_handler"]))
        self.assertTrue(all(owner.closed for owner in evidence["owners"]))
        self.assertEqual(set(evidence["fds"]), set(close_calls))
        for descriptor in evidence["fds"]:
            self.assertEqual(1, close_calls.count(descriptor))
            with self.assertRaises(OSError) as closed:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

        inspection = adapter.inspect_pair(source, mirror)
        self.assertEqual(self.repair.ContentRelation.EXACT, inspection.relation)
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_prepare_trace_handoff_closes_raw_transaction_once(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        inspection = adapter.inspect_pair(source, mirror)
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        mirror_fd = os.open(mirror, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw_source_snapshot = adapter.raw.snapshot_file(source_fd)
            raw_mirror_snapshot = adapter.raw.snapshot_file(mirror_fd)
        finally:
            os.close(mirror_fd)
            os.close(source_fd)
        raw_source_parent = self.identity_for_directory(adapter.raw, source.parent)
        raw_destination_parent = self.identity_for_directory(adapter.raw, mirror.parent)
        stage = mirror.parent / f".codex-reflink-repair-{'c' * 32}"
        temporary = stage / "clone"
        created: List[TrackingAdapterTransactionOwner] = []
        baseline = self.open_fd_set()

        def bind_transaction_owned(*_arguments: Any, **_keywords: Any) -> Any:
            def acquire() -> TrackingRawAdapterTransaction:
                raw_stage = self.identity_for_directory(adapter.raw, stage)
                return TrackingRawAdapterTransaction(
                    source_snapshot=raw_source_snapshot,
                    original_snapshot=raw_mirror_snapshot,
                    source_parent_identity=raw_source_parent,
                    destination_parent_identity=raw_destination_parent,
                    temporary_parent_identity=raw_stage,
                    stage_path=stage,
                )

            owner = TrackingAdapterTransactionOwner(acquire)
            created.append(owner)
            return owner

        def registration_boundary(expect_registered: bool) -> Any:
            def predicate(frame: Any, evidence: Dict[str, Any]) -> bool:
                owner = frame.f_locals.get("raw_transaction_owner")
                if (
                    not isinstance(owner, TrackingAdapterTransactionOwner)
                    or owner.closed
                    or owner._retain_if_registered is None
                ):
                    return False
                transaction = owner.transaction()
                wrapper = frame.f_locals.get("transaction")
                registered = (
                    isinstance(wrapper, self.repair._DarwinTransaction)
                    and wrapper._raw_owner is owner
                    and wrapper.raw is transaction
                )
                if registered != expect_registered:
                    return False
                evidence["owner"] = owner
                evidence["wrapper"] = wrapper
                evidence["raw_transaction"] = transaction
                evidence["fds"] = transaction.original_fds
                evidence["line_number"] = frame.f_lineno
                return True

            return predicate

        try:
            with mock.patch.object(
                adapter.raw,
                "bind_transaction_owned",
                side_effect=bind_transaction_owned,
            ):
                for registered in (False, True):
                    with self.subTest(registered=registered):
                        evidence = self.assert_trace_interruption(
                            adapter.prepare.__func__.__code__,
                            registration_boundary(registered),
                            lambda: adapter.prepare(
                                source,
                                mirror,
                                temporary,
                                inspection,
                                self.allow_state_mutation,
                            ),
                            label=f"prepare registration {registered}",
                        )

                        interrupted_owner = evidence["owner"]
                        interrupted = evidence["raw_transaction"]
                        self.assertIsInstance(evidence["line_number"], int)
                        if registered:
                            self.assertIs(
                                interrupted_owner, evidence["wrapper"]._raw_owner
                            )
                        else:
                            self.assertIsNone(evidence["wrapper"])
                        self.assertTrue(interrupted_owner.closed)
                        self.assertEqual(int(registered), interrupted.abort_calls)
                        self.assertEqual(
                            [evidence["primary"]], interrupted.close_primaries
                        )
                        self.assertTrue(interrupted.closed)
                        for descriptor in interrupted.original_fds:
                            self.assertEqual(1, interrupted.close_counts[descriptor])
                        self.assertFalse(stage.exists())
                        self.assertEqual(baseline, self.open_fd_set())

                transaction = adapter.prepare(
                    source,
                    mirror,
                    temporary,
                    inspection,
                    self.allow_state_mutation,
                )
                normal_owner = created[-1]
                normal = normal_owner.transaction()
                self.assertIs(normal, transaction.raw)
                self.assertIs(normal_owner, transaction._raw_owner)
                self.assertFalse(normal.closed)
                for descriptor in normal.original_fds:
                    os.fstat(descriptor)
                transaction.abort_before_prepared()
                transaction.close()

            self.assertEqual(1, normal.abort_calls)
            self.assertEqual([None], normal.close_primaries)
            self.assertTrue(normal_owner.closed)
            self.assertTrue(normal.closed)
            for descriptor in normal.original_fds:
                self.assertEqual(1, normal.close_counts[descriptor])
            self.assertFalse(stage.exists())
            self.assertEqual(baseline, self.open_fd_set())
        finally:
            for owner in created:
                owner.close()
            if stage.exists():
                stage.rmdir()

    def test_adapter_prepare_abort_handler_trace_retries_and_defers(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        config = dataclasses.replace(
            self.config(), state_root=self.state_root / "adapter-abort-trace"
        )
        adapter = self.repair._DarwinAdapter()
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        mirror_fd = os.open(mirror, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw_source_snapshot = adapter.raw.snapshot_file(source_fd)
            raw_mirror_snapshot = adapter.raw.snapshot_file(mirror_fd)
        finally:
            os.close(mirror_fd)
            os.close(source_fd)
        mutated_source_snapshot = dataclasses.replace(
            raw_source_snapshot,
            sha256=("0" * 64 if raw_source_snapshot.sha256 != "0" * 64 else "1" * 64),
        )
        raw_source_parent = self.identity_for_directory(adapter.raw, source.parent)
        raw_destination_parent = self.identity_for_directory(adapter.raw, mirror.parent)
        created: List[TrackingAdapterTransactionOwner] = []
        evidence: Dict[str, Any] = {}
        baseline = self.open_fd_set()

        def bind_transaction_owned(*arguments: Any, **_keywords: Any) -> Any:
            stage_path = pathlib.Path(arguments[2]).parent

            def acquire() -> TrackingRawAdapterTransaction:
                raw_stage = self.identity_for_directory(adapter.raw, stage_path)
                transaction = TrackingRawAdapterTransaction(
                    source_snapshot=mutated_source_snapshot,
                    original_snapshot=raw_mirror_snapshot,
                    source_parent_identity=raw_source_parent,
                    destination_parent_identity=raw_destination_parent,
                    temporary_parent_identity=raw_stage,
                    stage_path=stage_path,
                )
                evidence["raw_transaction"] = transaction
                evidence["stage_path"] = stage_path
                return transaction

            owner = TrackingAdapterTransactionOwner(acquire)
            created.append(owner)
            evidence["owner"] = owner
            return owner

        def abort_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            transaction = captured.get("raw_transaction")
            if (
                frame.f_locals.get("self") is not transaction
                or transaction is None
                or transaction.abort_calls != 0
                or "self.abort_calls += 1"
                not in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            ):
                return False
            prepare_frame = frame.f_back
            while (
                prepare_frame is not None
                and prepare_frame.f_code is not adapter.prepare.__func__.__code__
            ):
                prepare_frame = prepare_frame.f_back
            if prepare_frame is None:
                return False
            body_primary = prepare_frame.f_locals.get("error")
            owner = captured.get("owner")
            if (
                not isinstance(body_primary, self.repair.UnstablePathError)
                or isinstance(body_primary, self.repair.SafetyError)
                or prepare_frame.f_locals.get("raw_transaction_owner") is not owner
                or owner is None
                or owner.closed
                or owner.transaction() is not transaction
            ):
                return False
            captured["body_primary"] = body_primary
            captured["body_origin_traceback"] = body_primary.__traceback__
            captured["abort_line"] = frame.f_lineno
            return True

        stage_exists_before_fallback = True
        try:
            with mock.patch.object(
                adapter.raw,
                "bind_transaction_owned",
                side_effect=bind_transaction_owned,
            ):
                self.assert_cleanup_trace_allows_completion(
                    TrackingRawAdapterTransaction.abort_before_prepared.__code__,
                    abort_boundary,
                    lambda: self.repair.RepairTool(config, adapter).repair(
                        apply=True,
                        rollout_ids=[UUID_A],
                        queue_unstable=True,
                    ),
                    label="adapter prepare abort handler",
                    evidence=evidence,
                )
            stage_exists_before_fallback = evidence["stage_path"].exists()
        finally:
            for owner in created:
                owner.close(primary_error=evidence.get("body_primary"))
            stage_path = evidence.get("stage_path")
            if isinstance(stage_path, pathlib.Path) and stage_path.exists():
                stage_path.rmdir()

        result = self.results_by_id(evidence["result"])[UUID_A]
        transaction = evidence["raw_transaction"]
        owner = evidence["owner"]
        self.assertIsInstance(evidence["abort_line"], int)
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertFalse(stage_exists_before_fallback)
        self.assertEqual(1, transaction.abort_calls)
        self.assertTrue(owner.closed)
        self.assertTrue(transaction.closed)
        self.assertEqual([evidence["body_primary"]], transaction.close_primaries)
        for descriptor in transaction.original_fds:
            self.assertEqual(1, transaction.close_counts[descriptor])
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_intents(config))
        self.assertEqual([], self.repair._load_manifests(config))
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_prepare_closed_enter_owner_trace_removes_unbound_stage(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        inspection = adapter.inspect_pair(source, mirror)
        stage = mirror.parent / f".codex-reflink-repair-{'e' * 32}"
        temporary = stage / "clone"
        natural_error = self.darwin.BackendError(
            "source_object_replaced",
            "source changed while entering the bound transaction owner",
        )
        evidence: Dict[str, Any] = {}
        owners: List[TrackingAdapterTransactionOwner] = []
        remove_calls: List[pathlib.Path] = []
        baseline = self.open_fd_set()
        real_remove = adapter.raw.remove_empty_private_stage

        class ClosedEnterOwner(TrackingAdapterTransactionOwner):
            def __enter__(self) -> "ClosedEnterOwner":
                super().__enter__()
                try:
                    raise natural_error
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    self.close(primary_error=error)
                    raise

        def bind_transaction_owned(*arguments: Any, **_keywords: Any) -> Any:
            stage_path = pathlib.Path(arguments[2]).parent
            owner = ClosedEnterOwner(
                lambda: TrackingRawAdapterTransaction(stage_path=stage_path)
            )
            owners.append(owner)
            evidence["owner"] = owner
            evidence["stage_path"] = stage_path
            return owner

        def record_remove(stage_path: str, *arguments: Any, **keywords: Any) -> None:
            remove_calls.append(pathlib.Path(stage_path))
            real_remove(stage_path, *arguments, **keywords)

        def prepare_handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            owner = captured.get("owner")
            abort_receipt = frame.f_locals.get("abort_receipt")
            stage_remove_receipt = frame.f_locals.get("stage_remove_receipt")
            stage_path = captured.get("stage_path")
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("error") is not natural_error
                or frame.f_locals.get("raw_transaction_owner") is not owner
                or not isinstance(owner, ClosedEnterOwner)
                or not owner.closed
                or frame.f_locals.get("transaction") is not None
                or frame.f_locals.get("owned_transaction") is not None
                or abort_receipt is None
                or abort_receipt.dispatches != 0
                or stage_remove_receipt is None
                or stage_remove_receipt.dispatches != 0
                or not isinstance(stage_path, pathlib.Path)
                or not stage_path.is_dir()
                or list(stage_path.iterdir())
            ):
                return False
            raw_transaction = owner.acquired_transactions[-1]
            if not raw_transaction.closed or raw_transaction.abort_calls != 0:
                return False
            captured["body_primary"] = natural_error
            captured["body_origin_traceback"] = evidence["body_origin_traceback"]
            captured["raw_transaction"] = raw_transaction
            captured["abort_receipt"] = abort_receipt
            captured["stage_remove_receipt"] = stage_remove_receipt
            captured["handler_line"] = frame.f_lineno
            return True

        def unwrap_mapped(caught: Any) -> Any:
            self.assertIsInstance(caught, self.repair.UnstablePathError)
            self.assertNotIsInstance(caught, self.repair.SafetyError)
            self.assertIs(natural_error, caught.__cause__)
            return caught.__cause__

        stage_absent_before_fallback = False
        try:
            with (
                mock.patch.object(
                    adapter.raw,
                    "bind_transaction_owned",
                    side_effect=bind_transaction_owned,
                ),
                mock.patch.object(
                    adapter.raw,
                    "remove_empty_private_stage",
                    side_effect=record_remove,
                ),
            ):
                self.assert_cleanup_trace_preserves_primary(
                    adapter.prepare.__func__.__code__,
                    prepare_handler_boundary,
                    lambda: adapter.prepare(
                        source,
                        mirror,
                        temporary,
                        inspection,
                        self.allow_state_mutation,
                    ),
                    natural_error,
                    label="adapter closed owner unbound stage cleanup",
                    events=("line",),
                    evidence=evidence,
                    unwrap_caught=unwrap_mapped,
                )
                stage_absent_before_fallback = not stage.exists()
        finally:
            for owner in owners:
                owner.close(primary_error=natural_error)
            if stage.exists():
                stage.rmdir()

        owner = evidence["owner"]
        raw_transaction = evidence["raw_transaction"]
        abort_receipt = evidence["abort_receipt"]
        stage_remove_receipt = evidence["stage_remove_receipt"]
        self.assertIsInstance(evidence["handler_line"], int)
        self.assertTrue(stage_absent_before_fallback)
        self.assertEqual([stage], remove_calls)
        self.assertEqual(0, abort_receipt.dispatches)
        self.assertFalse(abort_receipt.completed)
        self.assertEqual(1, stage_remove_receipt.dispatches)
        self.assertTrue(stage_remove_receipt.completed)
        self.assertIsNone(stage_remove_receipt.error)
        self.assertTrue(owner.closed)
        self.assertTrue(raw_transaction.closed)
        self.assertEqual(0, raw_transaction.abort_calls)
        self.assertEqual([natural_error], raw_transaction.close_primaries)
        for descriptor in raw_transaction.original_fds:
            self.assertEqual(1, raw_transaction.close_counts[descriptor])
            with self.assertRaises(OSError) as closed:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_recovery_trace_handoff_closes_raw_transaction_once(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        inspection = adapter.inspect_pair(source, mirror)
        clone_snapshot = dataclasses.replace(
            inspection.mirror,
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
        )
        txid = "d" * 32
        stage = mirror.parent / f".codex-reflink-repair-{txid}"
        temporary = stage / "clone"
        manifest = self.repair.RepairManifest(
            rollout_id=UUID_A,
            txid=txid,
            phase=self.repair.Phase.PREPARED,
            source_rel=str(source.relative_to(self.source_root)),
            final_rel=str(mirror.relative_to(self.mirror_root)),
            temporary_rel=str(temporary.relative_to(self.mirror_root)),
            source_parent_identity=inspection.source_parent,
            final_parent_identity=inspection.mirror_parent,
            temporary_parent_identity=self.repair.FileIdentity(
                device=inspection.mirror_parent.device,
                inode=inspection.mirror_parent.inode + 10000,
            ),
            source_snapshot=inspection.source,
            original_snapshot=inspection.mirror,
            clone_snapshot=clone_snapshot,
            created_at_ns=1,
            updated_at_ns=1,
        )
        created: List[TrackingAdapterTransactionOwner] = []
        baseline = self.open_fd_set()

        def bind_recovery_owned(*_arguments: Any, **_keywords: Any) -> Any:
            owner = TrackingAdapterTransactionOwner(
                lambda: TrackingRawAdapterTransaction(fd_count=6)
            )
            created.append(owner)
            return owner

        def registration_boundary(expect_registered: bool) -> Any:
            def predicate(frame: Any, evidence: Dict[str, Any]) -> bool:
                owner = frame.f_locals.get("raw_transaction_owner")
                if (
                    not isinstance(owner, TrackingAdapterTransactionOwner)
                    or owner.closed
                    or owner._retain_if_registered is None
                ):
                    return False
                transaction = owner.transaction()
                wrapper = frame.f_locals.get("transaction")
                registered = (
                    isinstance(wrapper, self.repair._DarwinTransaction)
                    and wrapper._raw_owner is owner
                    and wrapper.raw is transaction
                )
                if registered != expect_registered:
                    return False
                evidence["owner"] = owner
                evidence["wrapper"] = wrapper
                evidence["raw_transaction"] = transaction
                evidence["fds"] = transaction.original_fds
                evidence["line_number"] = frame.f_lineno
                return True

            return predicate

        try:
            with mock.patch.object(
                adapter.raw, "bind_recovery_owned", side_effect=bind_recovery_owned
            ):
                for registered in (False, True):
                    with self.subTest(registered=registered):
                        evidence = self.assert_trace_interruption(
                            adapter.resume.__func__.__code__,
                            registration_boundary(registered),
                            lambda: adapter.resume(
                                source,
                                mirror,
                                temporary,
                                manifest,
                                self.allow_state_mutation,
                            ),
                            label=f"recovery registration {registered}",
                        )

                        interrupted_owner = evidence["owner"]
                        interrupted = evidence["raw_transaction"]
                        self.assertIsInstance(evidence["line_number"], int)
                        if registered:
                            self.assertIs(
                                interrupted_owner, evidence["wrapper"]._raw_owner
                            )
                        else:
                            self.assertIsNone(evidence["wrapper"])
                        self.assertTrue(interrupted_owner.closed)
                        self.assertEqual(
                            [evidence["primary"]], interrupted.close_primaries
                        )
                        self.assertTrue(interrupted.closed)
                        for descriptor in interrupted.original_fds:
                            self.assertEqual(1, interrupted.close_counts[descriptor])
                        self.assertEqual(baseline, self.open_fd_set())

                transaction = adapter.resume(
                    source,
                    mirror,
                    temporary,
                    manifest,
                    self.allow_state_mutation,
                )
                normal_owner = created[-1]
                normal = normal_owner.transaction()
                self.assertIs(normal, transaction.raw)
                self.assertIs(normal_owner, transaction._raw_owner)
                self.assertFalse(normal.closed)
                for descriptor in normal.original_fds:
                    os.fstat(descriptor)
                transaction.close()

            self.assertEqual([None], normal.close_primaries)
            self.assertTrue(normal_owner.closed)
            self.assertTrue(normal.closed)
            for descriptor in normal.original_fds:
                self.assertEqual(1, normal.close_counts[descriptor])
            self.assertEqual(baseline, self.open_fd_set())
        finally:
            for owner in created:
                owner.close()

    def test_adapter_transaction_cleanup_trace_retries_first_owner(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        inspection = adapter.inspect_pair(source, mirror)
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        mirror_fd = os.open(mirror, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw_source_snapshot = adapter.raw.snapshot_file(source_fd)
            raw_mirror_snapshot = adapter.raw.snapshot_file(mirror_fd)
        finally:
            os.close(mirror_fd)
            os.close(source_fd)
        raw_source_parent = self.identity_for_directory(adapter.raw, source.parent)
        raw_destination_parent = self.identity_for_directory(adapter.raw, mirror.parent)
        clone_snapshot = dataclasses.replace(
            inspection.mirror,
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
        )

        for index, route in enumerate(("prepare", "resume"), start=980):
            with self.subTest(route=route):
                body_primary = KeyboardInterrupt(f"{route} body primary")
                evidence: Dict[str, Any] = {}
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                temporary = stage / "clone"
                baseline = self.open_fd_set()
                owner: Optional[TrackingAdapterTransactionOwner] = None
                transaction: Optional[TrackingRawAdapterTransaction] = None

                def raise_body_primary() -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                class FailAfterRegistrationOwner(TrackingAdapterTransactionOwner):
                    def __exit__(
                        self,
                        exc_type: Any,
                        exc_value: Any,
                        traceback: Any,
                    ) -> None:
                        super().__exit__(exc_type, exc_value, traceback)
                        if exc_type is None:
                            raise_body_primary()

                def bind_owner(*_arguments: Any, **_keywords: Any) -> Any:
                    nonlocal owner, transaction
                    if route == "prepare":
                        raw_stage = self.identity_for_directory(adapter.raw, stage)
                        transaction = TrackingRawAdapterTransaction(
                            source_snapshot=raw_source_snapshot,
                            original_snapshot=raw_mirror_snapshot,
                            source_parent_identity=raw_source_parent,
                            destination_parent_identity=raw_destination_parent,
                            temporary_parent_identity=raw_stage,
                            stage_path=stage,
                        )
                    else:
                        transaction = TrackingRawAdapterTransaction(fd_count=6)
                    owner = FailAfterRegistrationOwner(lambda: transaction)
                    return owner

                if route == "prepare":

                    def operation() -> None:
                        adapter.prepare(
                            source,
                            mirror,
                            temporary,
                            inspection,
                            self.allow_state_mutation,
                        )

                    patch_target = "bind_transaction_owned"
                else:
                    manifest = self.repair.RepairManifest(
                        rollout_id=UUID_A,
                        txid=f"{index:032x}",
                        phase=self.repair.Phase.PREPARED,
                        source_rel=str(source.relative_to(self.source_root)),
                        final_rel=str(mirror.relative_to(self.mirror_root)),
                        temporary_rel=str(temporary.relative_to(self.mirror_root)),
                        source_parent_identity=inspection.source_parent,
                        final_parent_identity=inspection.mirror_parent,
                        temporary_parent_identity=self.repair.FileIdentity(
                            device=inspection.mirror_parent.device,
                            inode=inspection.mirror_parent.inode + 10000,
                        ),
                        source_snapshot=inspection.source,
                        original_snapshot=inspection.mirror,
                        clone_snapshot=clone_snapshot,
                        created_at_ns=1,
                        updated_at_ns=1,
                    )

                    def operation() -> None:
                        adapter.resume(
                            source,
                            mirror,
                            temporary,
                            manifest,
                            self.allow_state_mutation,
                        )

                    patch_target = "bind_recovery_owned"

                def cleanup_entry(frame: Any, _captured: Dict[str, Any]) -> bool:
                    return (
                        frame.f_locals.get("self") is owner
                        and frame.f_locals.get("primary_error") is body_primary
                        and owner is not None
                        and not owner.closed
                    )

                with mock.patch.object(
                    adapter.raw, patch_target, side_effect=bind_owner
                ):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            TrackingAdapterTransactionOwner.close.__code__,
                            cleanup_entry,
                            operation,
                            body_primary,
                            label=f"adapter {route} owner cleanup",
                            evidence=evidence,
                        )
                    finally:
                        if owner is not None:
                            owner.close(primary_error=body_primary)

                self.assertIsNotNone(owner)
                self.assertIsNotNone(transaction)
                assert owner is not None
                assert transaction is not None
                self.assertTrue(owner.closed)
                self.assertTrue(transaction.closed)
                self.assertEqual([body_primary], transaction.close_primaries)
                self.assertEqual(int(route == "prepare"), transaction.abort_calls)
                for descriptor in transaction.original_fds:
                    self.assertEqual(1, transaction.close_counts[descriptor])
                self.assertFalse(stage.exists())
                self.assertEqual(baseline, self.open_fd_set())

    def test_adapter_transaction_handler_preamble_trace_drains_registered_owner(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        inspection = adapter.inspect_pair(source, mirror)
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        mirror_fd = os.open(mirror, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw_source_snapshot = adapter.raw.snapshot_file(source_fd)
            raw_mirror_snapshot = adapter.raw.snapshot_file(mirror_fd)
        finally:
            os.close(mirror_fd)
            os.close(source_fd)
        raw_source_parent = self.identity_for_directory(adapter.raw, source.parent)
        raw_destination_parent = self.identity_for_directory(adapter.raw, mirror.parent)
        clone_snapshot = dataclasses.replace(
            inspection.mirror,
            identity=self.repair.FileIdentity(
                device=inspection.mirror.identity.device,
                inode=inspection.mirror.identity.inode + 10000,
            ),
        )

        cases = (
            ("prepare", "cleanup-preamble", "interrupt"),
            ("resume", "cleanup-preamble", "interrupt"),
            ("prepare", "failure-reporting", "interrupt"),
            ("resume", "failure-reporting", "interrupt"),
            ("prepare", "failure-reporting", "repair-error"),
        )
        for index, (route, boundary, primary_kind) in enumerate(cases, start=990):
            with self.subTest(route=route, boundary=boundary, primary=primary_kind):
                body_primary = (
                    self.repair.UnstablePathError(f"{route} {boundary} repair primary")
                    if primary_kind == "repair-error"
                    else KeyboardInterrupt(f"{route} {boundary} body primary")
                )
                evidence: Dict[str, Any] = {}
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                temporary = stage / "clone"
                baseline = self.open_fd_set()
                owner: Optional[TrackingAdapterTransactionOwner] = None
                raw_transaction: Optional[TrackingRawAdapterTransaction] = None

                def raise_body_primary() -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                class FailAfterRegistrationOwner(TrackingAdapterTransactionOwner):
                    def __exit__(
                        self,
                        exc_type: Any,
                        exc_value: Any,
                        traceback: Any,
                    ) -> None:
                        super().__exit__(exc_type, exc_value, traceback)
                        if exc_type is None:
                            raise_body_primary()

                def bind_owner(*_arguments: Any, **_keywords: Any) -> Any:
                    nonlocal owner, raw_transaction
                    if route == "prepare":
                        raw_stage = self.identity_for_directory(adapter.raw, stage)
                        raw_transaction = TrackingRawAdapterTransaction(
                            source_snapshot=raw_source_snapshot,
                            original_snapshot=raw_mirror_snapshot,
                            source_parent_identity=raw_source_parent,
                            destination_parent_identity=raw_destination_parent,
                            temporary_parent_identity=raw_stage,
                            stage_path=stage,
                        )
                    else:
                        raw_transaction = TrackingRawAdapterTransaction(fd_count=6)
                    owner = FailAfterRegistrationOwner(lambda: raw_transaction)
                    return owner

                if route == "prepare":

                    def operation() -> None:
                        adapter.prepare(
                            source,
                            mirror,
                            temporary,
                            inspection,
                            self.allow_state_mutation,
                        )

                    target_code = adapter.prepare.__func__.__code__
                    patch_target = "bind_transaction_owned"
                else:
                    manifest = self.repair.RepairManifest(
                        rollout_id=UUID_A,
                        txid=f"{index:032x}",
                        phase=self.repair.Phase.PREPARED,
                        source_rel=str(source.relative_to(self.source_root)),
                        final_rel=str(mirror.relative_to(self.mirror_root)),
                        temporary_rel=str(temporary.relative_to(self.mirror_root)),
                        source_parent_identity=inspection.source_parent,
                        final_parent_identity=inspection.mirror_parent,
                        temporary_parent_identity=self.repair.FileIdentity(
                            device=inspection.mirror_parent.device,
                            inode=inspection.mirror_parent.inode + 10000,
                        ),
                        source_snapshot=inspection.source,
                        original_snapshot=inspection.mirror,
                        clone_snapshot=clone_snapshot,
                        created_at_ns=1,
                        updated_at_ns=1,
                    )

                    def operation() -> None:
                        adapter.resume(
                            source,
                            mirror,
                            temporary,
                            manifest,
                            self.allow_state_mutation,
                        )

                    target_code = adapter.resume.__func__.__code__
                    patch_target = "bind_recovery_owned"

                def handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                    raw_owner = frame.f_locals.get("raw_transaction_owner")
                    wrapper = frame.f_locals.get("transaction")
                    owned = frame.f_locals.get("owned_transaction")
                    if owner is None:
                        return False
                    is_cleanup_preamble = not owner.closed and owned is None
                    is_failure_reporting = owner.closed and owned is owner
                    if (
                        raw_owner is not owner
                        or frame.f_locals.get("error") is not body_primary
                        or not isinstance(wrapper, self.repair._DarwinTransaction)
                        or wrapper._raw_owner is not owner
                        or (boundary == "cleanup-preamble" and not is_cleanup_preamble)
                        or (
                            boundary == "failure-reporting" and not is_failure_reporting
                        )
                        or linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        != "_cleanup_guard = True"
                    ):
                        return False
                    captured["owner"] = owner
                    captured["wrapper"] = wrapper
                    captured["line_number"] = frame.f_lineno
                    return True

                with mock.patch.object(
                    adapter.raw, patch_target, side_effect=bind_owner
                ):
                    try:
                        self.assert_cleanup_trace_preserves_primary(
                            target_code,
                            handler_boundary,
                            operation,
                            body_primary,
                            label=f"adapter {route} {boundary}",
                            events=("line",),
                            evidence=evidence,
                        )
                    finally:
                        if owner is not None:
                            owner.close(primary_error=body_primary)

                self.assertIsNotNone(owner)
                self.assertIsNotNone(raw_transaction)
                assert owner is not None
                assert raw_transaction is not None
                self.assertIsInstance(evidence["line_number"], int)
                self.assertTrue(owner.closed)
                self.assertTrue(raw_transaction.closed)
                self.assertEqual([body_primary], raw_transaction.close_primaries)
                self.assertEqual(
                    int(route == "prepare"),
                    raw_transaction.abort_calls,
                )
                for descriptor in raw_transaction.original_fds:
                    self.assertEqual(1, raw_transaction.close_counts[descriptor])
                self.assertFalse(stage.exists())
                self.assertEqual(baseline, self.open_fd_set())

    def test_darwin_transaction_close_trace_retries_before_clearing_owner(
        self,
    ) -> None:
        adapter = self.repair._DarwinAdapter()
        baseline = self.open_fd_set()
        raw_transaction = TrackingRawAdapterTransaction(fd_count=6)
        owner = TrackingAdapterTransactionOwner(lambda: raw_transaction)
        with owner:
            pass
        transaction = self.repair._DarwinTransaction(
            adapter,
            owner,
            self.root / "unused-wrapper-stage",
            self.allow_state_mutation,
            mock.sentinel.expected_original,
            prepared_durable=True,
        )

        def cleanup_entry(frame: Any, evidence: Dict[str, Any]) -> bool:
            if (
                frame.f_locals.get("self") is not owner
                or frame.f_locals.get("primary_error") is not None
                or owner.closed
            ):
                return False
            evidence["owner_at_boundary"] = transaction._raw_owner
            return True

        evidence = self.assert_trace_interruption(
            TrackingAdapterTransactionOwner.close.__code__,
            cleanup_entry,
            transaction.close,
            label="darwin transaction wrapper close",
            events=("call",),
        )

        self.assertIs(owner, evidence["owner_at_boundary"])
        self.assertTrue(owner.closed)
        self.assertTrue(raw_transaction.closed)
        self.assertIsNone(transaction._raw_owner)
        self.assertEqual([evidence["primary"]], raw_transaction.close_primaries)
        for descriptor in raw_transaction.original_fds:
            self.assertEqual(1, raw_transaction.close_counts[descriptor])
        self.assertEqual(baseline, self.open_fd_set())

    def test_darwin_transaction_natural_close_error_survives_handler_trace(
        self,
    ) -> None:
        adapter = self.repair._DarwinAdapter()
        baseline = self.open_fd_set()
        natural_error = OSError(errno.EIO, "darwin transaction natural close error")
        evidence: Dict[str, Any] = {}

        class NaturalCloseTransaction(TrackingRawAdapterTransaction):
            def close(
                nested_self,
                *,
                primary_error: Optional[BaseException] = None,
            ) -> None:
                if nested_self.closed:
                    return
                super().close(primary_error=primary_error)
                try:
                    raise natural_error
                except BaseException as error:
                    evidence["body_origin_traceback"] = error.__traceback__
                    raise

        raw_transaction = NaturalCloseTransaction(fd_count=6)
        owner = TrackingAdapterTransactionOwner(lambda: raw_transaction)
        with owner:
            pass
        transaction = self.repair._DarwinTransaction(
            adapter,
            owner,
            self.root / "unused-natural-close-stage",
            self.allow_state_mutation,
            mock.sentinel.expected_original,
            prepared_durable=True,
        )

        def handler_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "_cleanup_guard = True"
                or frame.f_locals.get("self") is not transaction
                or frame.f_locals.get("owner") is not owner
                or frame.f_locals.get("cleanup_error") is not natural_error
                or not owner.closed
                or transaction._raw_owner is not owner
            ):
                return False
            captured["owner"] = owner
            captured["raw_transaction"] = raw_transaction
            captured["handler_line"] = frame.f_lineno
            return True

        self.assert_cleanup_trace_preserves_primary(
            self.repair._DarwinTransaction.close.__code__,
            handler_boundary,
            transaction.close,
            natural_error,
            label="darwin transaction natural close handler",
            events=("line",),
            evidence=evidence,
        )

        self.assertIsInstance(evidence["handler_line"], int)
        self.assertIs(evidence["owner"], owner)
        self.assertIs(evidence["raw_transaction"], raw_transaction)
        self.assertTrue(owner.closed)
        self.assertTrue(raw_transaction.closed)
        self.assertIsNone(transaction._raw_owner)
        self.assertEqual([None], raw_transaction.close_primaries)
        for descriptor in raw_transaction.original_fds:
            self.assertEqual(1, raw_transaction.close_counts[descriptor])
            with self.assertRaises(OSError) as closed:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_post_durable_close_failure_keeps_cleanup_success(self) -> None:
        _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'d' * 32}"
        darwin = self.darwin
        armed = False

        class ArmAfterRemovalBackend(darwin.DarwinBackend):
            def _require_stage_container_mapping(
                self, path: str, parent_fd: int, expected: Any
            ) -> None:
                nonlocal armed
                super()._require_stage_container_mapping(path, parent_fd, expected)
                if not pathlib.Path(path).exists():
                    armed = True

        backend = ArmAfterRemovalBackend()
        parent_fd = backend.open_absolute_dir(str(stage.parent))
        try:
            container_identity = backend.validate_stage_container(parent_fd)
        finally:
            os.close(parent_fd)
        stage_identity = backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        close_calls: List[int] = []
        real_close = self.darwin.os.close

        def fail_first_local_close(descriptor: int) -> None:
            if not armed:
                real_close(descriptor)
                return
            close_calls.append(descriptor)
            real_close(descriptor)
            if len(close_calls) == 1:
                raise OSError(errno.EIO, "injected local close failure")

        baseline = self.open_fd_set()
        with mock.patch.object(
            self.darwin.os, "close", side_effect=fail_first_local_close
        ):
            backend.remove_empty_private_stage(
                str(stage),
                stage_identity,
                expected_container=container_identity,
                authorize_state=self.allow_state_mutation,
            )

        self.assertEqual(2, len(close_calls))
        self.assertFalse(stage.exists())
        self.assertEqual(baseline, self.open_fd_set())

    def test_ambient_exception_does_not_suppress_read_only_close_failure(self) -> None:
        parent = self.root / "ambient-close"
        parent.mkdir()
        leaf = parent / "leaf"
        leaf.write_bytes(b"payload")
        leaf.chmod(0o600)
        darwin = self.darwin
        armed = False

        class ArmOnLeafOpenBackend(darwin.DarwinBackend):
            def open_leaf(
                self, parent_fd: int, name: str, *, writable: bool = False
            ) -> int:
                nonlocal armed
                descriptor = super().open_leaf(parent_fd, name, writable=writable)
                armed = True
                return descriptor

        backend = ArmOnLeafOpenBackend()
        parent_fd = backend.open_absolute_dir(str(parent))
        baseline = self.open_fd_set()
        close_calls: List[int] = []
        real_close = self.darwin.os.close

        def fail_read_only_close(descriptor: int) -> None:
            if not armed:
                real_close(descriptor)
                return
            close_calls.append(descriptor)
            real_close(descriptor)
            raise OSError(errno.EIO, "injected read-only close failure")

        try:
            try:
                raise ValueError("ambient caller exception")
            except ValueError:
                with (
                    mock.patch.object(
                        self.darwin.os, "close", side_effect=fail_read_only_close
                    ),
                    self.assertRaises(darwin.BackendError) as caught,
                ):
                    backend.identity_at(parent_fd, leaf.name)

            self.assertEqual("close_failed", caught.exception.reason)
            self.assertEqual(1, len(close_calls))
            self.assertEqual(baseline, self.open_fd_set())
        finally:
            os.close(parent_fd)

    def test_real_intent_stage_cleanup_accepts_only_one_proven_clone(self) -> None:
        for index, clone_identity_bound in enumerate((False, True), start=1):
            with self.subTest(clone_identity_bound=clone_identity_bound):
                (
                    backend,
                    _source,
                    mirror,
                    stage,
                    container_identity,
                    original_expectation,
                    stage_identity,
                    clone_identity,
                    clone_sha256,
                    clone_expectation,
                ) = self.prepare_intent_clone_artifact(f"{index:032x}")

                disposition = backend.cleanup_intent_stage(
                    str(stage),
                    final_path=str(mirror),
                    expected_container=container_identity,
                    expected_original=original_expectation,
                    expected_stage=stage_identity,
                    allow_clone=True,
                    expected_clone=clone_identity if clone_identity_bound else None,
                    expected_snapshot=(
                        clone_expectation if clone_identity_bound else None
                    ),
                    expected_size=clone_identity.size,
                    expected_sha256=clone_sha256,
                    authorize_state=self.allow_state_mutation,
                )

                self.assertEqual("removed-clone", disposition)
                self.assertFalse(stage.exists())

    def test_intent_cleanup_drains_fds_without_masking_primary_error(self) -> None:
        darwin = self.darwin
        cases = ("authorize", "validator", "close-only", "post-durable-close")
        for index, failure_site in enumerate(cases, start=40):
            with self.subTest(failure_site=failure_site):
                (
                    _preparing_backend,
                    _source,
                    mirror,
                    stage,
                    container_identity,
                    original_expectation,
                    stage_identity,
                    clone_identity,
                    clone_sha256,
                    clone_expectation,
                ) = self.prepare_intent_clone_artifact(f"{index:032x}")
                sentinel = RuntimeError(f"primary-{failure_site}")
                armed = False
                close_calls: List[int] = []

                class PrimaryFailureBackend(darwin.DarwinBackend):
                    def require_snapshot(
                        self,
                        descriptor: int,
                        expected: Any,
                        subject: str,
                        **keywords: Any,
                    ) -> Any:
                        nonlocal armed
                        if (
                            failure_site == "validator"
                            and subject
                            == "INTENT original survivor after cleanup authorization"
                        ):
                            armed = True
                            raise sentinel
                        return super().require_snapshot(
                            descriptor, expected, subject, **keywords
                        )

                    def _require_stage_container_mapping(
                        self, path: str, parent_fd: int, expected: Any
                    ) -> None:
                        nonlocal armed
                        super()._require_stage_container_mapping(
                            path, parent_fd, expected
                        )
                        if (
                            failure_site == "post-durable-close"
                            and not pathlib.Path(path).exists()
                        ):
                            armed = True

                backend = PrimaryFailureBackend()

                def authorize(action: str) -> None:
                    nonlocal armed
                    if failure_site == "authorize" and action == "intent_unlink_clone":
                        armed = True
                        raise sentinel
                    if failure_site == "close-only" and action == "intent_remove_stage":
                        armed = True

                real_close = self.darwin.os.close

                def flaky_close(descriptor: int) -> None:
                    if not armed:
                        real_close(descriptor)
                        return
                    close_calls.append(descriptor)
                    real_close(descriptor)
                    if len(close_calls) == 1:
                        raise OSError(errno.EIO, "injected close failure")

                baseline = self.open_fd_set()
                with mock.patch.object(
                    self.darwin.os, "close", side_effect=flaky_close
                ):
                    if failure_site == "post-durable-close":
                        disposition = backend.cleanup_intent_stage(
                            str(stage),
                            final_path=str(mirror),
                            expected_container=container_identity,
                            expected_original=original_expectation,
                            expected_stage=stage_identity,
                            allow_clone=True,
                            expected_clone=clone_identity,
                            expected_snapshot=clone_expectation,
                            expected_size=clone_identity.size,
                            expected_sha256=clone_sha256,
                            authorize_state=authorize,
                        )
                        self.assertEqual("removed-clone", disposition)
                    elif failure_site == "close-only":
                        with self.assertRaises(darwin.BackendError) as caught:
                            backend.cleanup_intent_stage(
                                str(stage),
                                final_path=str(mirror),
                                expected_container=container_identity,
                                expected_original=original_expectation,
                                expected_stage=stage_identity,
                                allow_clone=True,
                                expected_clone=clone_identity,
                                expected_snapshot=clone_expectation,
                                expected_size=clone_identity.size,
                                expected_sha256=clone_sha256,
                                authorize_state=authorize,
                            )
                        self.assertEqual("close_failed", caught.exception.reason)
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            backend.cleanup_intent_stage(
                                str(stage),
                                final_path=str(mirror),
                                expected_container=container_identity,
                                expected_original=original_expectation,
                                expected_stage=stage_identity,
                                allow_clone=True,
                                expected_clone=clone_identity,
                                expected_snapshot=clone_expectation,
                                expected_size=clone_identity.size,
                                expected_sha256=clone_sha256,
                                authorize_state=authorize,
                            )
                        self.assertIs(sentinel, caught.exception)

                self.assertGreaterEqual(len(close_calls), 3)
                self.assertEqual(len(close_calls), len(set(close_calls)))
                self.assertEqual(baseline, self.open_fd_set())
                self.assertEqual(failure_site != "post-durable-close", stage.exists())

    def test_real_intent_clone_cleanup_requires_original_survivor(self) -> None:
        cases = (
            ("missing", "open_leaf_failed"),
            ("wrong-inode", "intent_original_snapshot_mismatch"),
            ("content-mutation", "intent_original_snapshot_mismatch"),
            ("policy-mutation", "intent_original_snapshot_mismatch"),
            ("unreadable", "intent_original_snapshot_unreadable"),
        )
        for state_index, clone_identity_bound in enumerate((False, True)):
            for case_index, (case, expected_reason) in enumerate(cases):
                with self.subTest(
                    state=("CLONE_BOUND" if clone_identity_bound else "STAGE_BOUND"),
                    case=case,
                ):
                    txid = f"{500 + state_index * len(cases) + case_index:032x}"
                    (
                        backend,
                        _source,
                        mirror,
                        stage,
                        container_identity,
                        original_expectation,
                        stage_identity,
                        clone_identity,
                        clone_sha256,
                        clone_expectation,
                    ) = self.prepare_intent_clone_artifact(txid)
                    clone = stage / "clone"
                    before_clone = clone.read_bytes()
                    unreadable_patch: Any = contextlib.nullcontext()

                    if case == "missing":
                        mirror.unlink()
                    elif case == "wrong-inode":
                        replacement = mirror.with_name(f"{mirror.name}.replacement")
                        original_fd = os.open(mirror, os.O_RDONLY)
                        replacement_fd = -1
                        try:
                            original_snapshot = backend.snapshot_file(original_fd)
                            replacement.write_bytes(mirror.read_bytes())
                            replacement_fd = os.open(replacement, os.O_RDWR)
                            backend.calibrate_clone_policy(
                                original_fd,
                                replacement_fd,
                                original_snapshot.policy,
                            )
                        finally:
                            if replacement_fd >= 0:
                                os.close(replacement_fd)
                            os.close(original_fd)
                        original_inode = mirror.stat().st_ino
                        os.replace(replacement, mirror)
                        self.assertNotEqual(original_inode, mirror.stat().st_ino)
                    elif case == "content-mutation":
                        changed = bytearray(mirror.read_bytes())
                        changed[0] ^= 1
                        mirror.write_bytes(bytes(changed))
                        metadata = mirror.stat()
                        os.utime(
                            mirror,
                            ns=(metadata.st_atime_ns, original_expectation.mtime_ns),
                        )
                    elif case == "policy-mutation":
                        subprocess.run(
                            [
                                "/usr/bin/xattr",
                                "-w",
                                "com.openai.codex.intent-original-policy-test",
                                "changed",
                                str(mirror),
                            ],
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    elif case == "unreadable":
                        real_snapshot_xattrs = backend._snapshot_xattrs

                        def unreadable_original(descriptor: int) -> Any:
                            identity = backend.identity(descriptor)
                            if identity.object_key == (
                                original_expectation.dev,
                                original_expectation.ino,
                            ):
                                raise self.darwin.BackendError(
                                    "xattr_unreadable",
                                    "injected original xattr denial",
                                    errno.EACCES,
                                )
                            return real_snapshot_xattrs(descriptor)

                        unreadable_patch = mock.patch.object(
                            backend,
                            "_snapshot_xattrs",
                            side_effect=unreadable_original,
                        )

                    with (
                        unreadable_patch,
                        self.assertRaises(self.darwin.BackendError) as caught,
                    ):
                        backend.cleanup_intent_stage(
                            str(stage),
                            final_path=str(mirror),
                            expected_container=container_identity,
                            expected_original=original_expectation,
                            expected_stage=stage_identity,
                            allow_clone=True,
                            expected_clone=(
                                clone_identity if clone_identity_bound else None
                            ),
                            expected_snapshot=(
                                clone_expectation if clone_identity_bound else None
                            ),
                            expected_size=clone_identity.size,
                            expected_sha256=clone_sha256,
                            authorize_state=self.allow_state_mutation,
                        )

                    self.assertEqual(expected_reason, caught.exception.reason)
                    self.assertTrue(stage.exists())
                    self.assertEqual(before_clone, clone.read_bytes())

    def test_real_intent_stage_cleanup_rejects_unsafe_or_ambiguous_evidence(
        self,
    ) -> None:
        cases = (
            "extra-child",
            "symlink",
            "wrong-owner",
            "wrong-type",
            "unsafe-link-count",
            "clone-policy-mismatch",
            "clone-identity-mismatch",
            "clone-size-mismatch",
            "clone-sha-mismatch",
            "stage-identity-mismatch",
        )
        for index, case in enumerate(cases, start=10):
            with self.subTest(case=case):
                (
                    backend,
                    source,
                    mirror,
                    stage,
                    container_identity,
                    original_expectation,
                    stage_identity,
                    clone_identity,
                    clone_sha256,
                    clone_expectation,
                ) = self.prepare_intent_clone_artifact(f"{index:032x}")
                clone = stage / "clone"
                expected_stage = stage_identity
                expected_clone = clone_identity
                expected_snapshot = clone_expectation
                expected_size = clone_identity.size
                expected_sha256 = clone_sha256
                owner_patch: Any = contextlib.nullcontext()
                real_identity = backend.identity
                if case == "extra-child":
                    (stage / "extra").write_bytes(b"evidence")
                elif case == "symlink":
                    clone.unlink()
                    os.symlink(source, clone)
                    expected_clone = None
                    expected_snapshot = None
                elif case == "wrong-owner":

                    def wrong_clone_owner(descriptor: int) -> Any:
                        identity = real_identity(descriptor)
                        if identity.is_same_object(clone_identity):
                            return dataclasses.replace(identity, uid=identity.uid + 1)
                        return identity

                    owner_patch = mock.patch.object(
                        backend, "identity", side_effect=wrong_clone_owner
                    )
                elif case == "wrong-type":
                    clone.unlink()
                    clone.mkdir()
                    expected_clone = None
                    expected_snapshot = None
                elif case == "unsafe-link-count":
                    os.link(clone, self.root / f"clone-alias-{index}")
                elif case == "clone-policy-mismatch":
                    subprocess.run(
                        [
                            "/usr/bin/xattr",
                            "-w",
                            "com.openai.codex.intent-policy-test",
                            "changed",
                            str(clone),
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                elif case == "clone-identity-mismatch":
                    expected_clone = dataclasses.replace(
                        clone_identity, ino=clone_identity.ino + 1
                    )
                    expected_snapshot = dataclasses.replace(
                        clone_expectation, ino=clone_expectation.ino + 1
                    )
                elif case == "clone-size-mismatch":
                    expected_size += 1
                    expected_snapshot = dataclasses.replace(
                        clone_expectation, size=expected_size
                    )
                elif case == "clone-sha-mismatch":
                    expected_sha256 = "0" * 64
                    expected_snapshot = dataclasses.replace(
                        clone_expectation, sha256=expected_sha256
                    )
                elif case == "stage-identity-mismatch":
                    expected_stage = dataclasses.replace(
                        stage_identity, ino=stage_identity.ino + 1
                    )

                with owner_patch, self.assertRaises(self.darwin.BackendError):
                    backend.cleanup_intent_stage(
                        str(stage),
                        final_path=str(mirror),
                        expected_container=container_identity,
                        expected_original=original_expectation,
                        expected_stage=expected_stage,
                        allow_clone=True,
                        expected_clone=expected_clone,
                        expected_snapshot=expected_snapshot,
                        expected_size=expected_size,
                        expected_sha256=expected_sha256,
                        authorize_state=self.allow_state_mutation,
                    )

                self.assertTrue(stage.exists())
                self.assertTrue(os.path.lexists(clone))
                if case == "extra-child":
                    self.assertTrue((stage / "extra").exists())

    def test_real_planned_intent_adopts_only_exact_safe_empty_stage(self) -> None:
        cases = (
            "empty",
            "clone",
            "extra-child",
            "unsafe-policy",
            "symlink-stage",
            "non-tool-name",
        )
        for index, case in enumerate(cases, start=400):
            with self.subTest(case=case):
                mirror = self.write_rollout(
                    self.mirror_root, "archived_sessions", UUID_A, b"same\n"
                )
                backend = self.darwin.DarwinBackend()
                original_expectation = self.expectation_for_path(backend, mirror)
                stage_name = (
                    ".codex-reflink-repair-not-a-txid"
                    if case == "non-tool-name"
                    else f".codex-reflink-repair-{index:032x}"
                )
                stage = mirror.parent / stage_name
                if case == "symlink-stage":
                    target = mirror.parent / f"stage-target-{index}"
                    target.mkdir(mode=0o700)
                    os.symlink(target.name, stage)
                else:
                    stage.mkdir(mode=0o700)
                    stage.chmod(0o700)
                    if case == "clone":
                        (stage / "clone").write_bytes(b"unowned clone")
                    elif case == "extra-child":
                        (stage / "extra").write_bytes(b"unowned evidence")
                    elif case == "unsafe-policy":
                        stage.chmod(0o777)
                container_fd = backend.open_absolute_dir(str(stage.parent))
                try:
                    container_identity = backend.validate_stage_container(container_fd)
                finally:
                    os.close(container_fd)

                def cleanup() -> str:
                    return backend.cleanup_intent_stage(
                        str(stage),
                        final_path=str(mirror),
                        expected_container=container_identity,
                        expected_original=original_expectation,
                        expected_stage=None,
                        allow_clone=False,
                        expected_clone=None,
                        expected_snapshot=None,
                        expected_size=None,
                        expected_sha256=None,
                        authorize_state=self.allow_state_mutation,
                    )

                if case == "empty":
                    self.assertEqual("removed-empty", cleanup())
                    self.assertFalse(stage.exists())
                else:
                    with self.assertRaises(self.darwin.BackendError):
                        cleanup()
                    self.assertTrue(os.path.lexists(stage))
                    if case == "clone":
                        self.assertTrue((stage / "clone").exists())
                    elif case == "extra-child":
                        self.assertTrue((stage / "extra").exists())

    def test_real_absent_intent_stage_syncs_the_exact_bound_container(self) -> None:
        darwin = self.darwin

        class SyncSpyBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.synced_identities: List[Any] = []
                super().__init__()

            def fsync(self, descriptor: int) -> None:
                self.synced_identities.append(self.identity(descriptor))
                super().fsync(descriptor)

        mirror = self.write_rollout(
            self.mirror_root, "archived_sessions", UUID_A, b"same\n"
        )
        stage = mirror.parent / f".codex-reflink-repair-{'e' * 32}"
        backend = SyncSpyBackend()
        original_expectation = self.expectation_for_path(backend, mirror)
        container_fd = backend.open_absolute_dir(str(stage.parent))
        try:
            container_identity = backend.validate_stage_container(container_fd)
        finally:
            os.close(container_fd)

        disposition = backend.cleanup_intent_stage(
            str(stage),
            final_path=str(mirror),
            expected_container=container_identity,
            expected_original=original_expectation,
            expected_stage=None,
            allow_clone=False,
            expected_clone=None,
            expected_snapshot=None,
            expected_size=None,
            expected_sha256=None,
            authorize_state=self.allow_state_mutation,
        )

        self.assertEqual("absent", disposition)
        self.assertTrue(
            any(
                identity.is_same_object(container_identity)
                for identity in backend.synced_identities
            )
        )

    def test_real_intent_cleanup_rejects_container_replacement_after_rmdir(
        self,
    ) -> None:
        darwin = self.darwin
        mirror = self.write_rollout(
            self.mirror_root, "archived_sessions", UUID_A, b"same\n"
        )
        stage = mirror.parent / f".codex-reflink-repair-{'d' * 32}"
        moved_container = stage.parent.with_name(f"{stage.parent.name}.moved")

        class RemapAfterRmdirBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.remapped = False
                self.expected_container: Optional[Any] = None
                super().__init__()

            def fsync(self, descriptor: int) -> None:
                super().fsync(descriptor)
                if self.remapped or self.expected_container is None or stage.exists():
                    return
                current = self.identity(descriptor)
                if not current.is_same_object(self.expected_container):
                    return
                os.replace(stage.parent, moved_container)
                stage.parent.mkdir(mode=0o700)
                self.remapped = True

        backend = RemapAfterRmdirBackend()
        original_expectation = self.expectation_for_path(backend, mirror)
        container_fd = backend.open_absolute_dir(str(stage.parent))
        try:
            container_identity = backend.validate_stage_container(container_fd)
        finally:
            os.close(container_fd)
        stage_identity = backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        backend.expected_container = container_identity

        with self.assertRaises(darwin.BackendError):
            backend.cleanup_intent_stage(
                str(stage),
                final_path=str(mirror),
                expected_container=container_identity,
                expected_original=original_expectation,
                expected_stage=stage_identity,
                allow_clone=False,
                expected_clone=None,
                expected_snapshot=None,
                expected_size=None,
                expected_sha256=None,
                authorize_state=self.allow_state_mutation,
            )

        self.assertTrue(backend.remapped)
        self.assertTrue(moved_container.exists())

    def test_real_stage_container_write_exposure_is_rejected_without_hardening(
        self,
    ) -> None:
        for index, mode in enumerate((0o770, 0o777)):
            with self.subTest(mode=oct(mode)):
                source, mirror, _stage, _clone = self.prepare_pair(create_stage=False)
                container = mirror.parent
                container.chmod(mode)
                before_mode = container.stat().st_mode & 0o777
                before_inode = mirror.stat().st_ino
                stage = container / f".codex-reflink-repair-{index + 40:032x}"
                backend = self.darwin.DarwinBackend()

                with self.assertRaises(self.darwin.BackendError):
                    with backend.bind_transaction(
                        str(source),
                        str(mirror),
                        str(stage / "clone"),
                        source_parent_expected=self.identity_for_directory(
                            backend, source.parent
                        ),
                    ):
                        self.fail("unsafe stage container reached transaction body")

                self.assertEqual(before_mode, container.stat().st_mode & 0o777)
                self.assertEqual(before_inode, mirror.stat().st_ino)
                self.assertFalse(stage.exists())

    def test_real_acl_write_exposed_stage_container_is_rejected_unchanged(self) -> None:
        source, mirror, _stage, _clone = self.prepare_pair(create_stage=False)
        container = mirror.parent
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow add_file", str(container)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        before_acl = subprocess.check_output(
            ["/bin/ls", "-lde", str(container)], text=True
        )
        stage = container / f".codex-reflink-repair-{'a' * 32}"
        backend = self.darwin.DarwinBackend()

        with self.assertRaises(self.darwin.BackendError):
            with backend.bind_transaction(
                str(source),
                str(mirror),
                str(stage / "clone"),
                source_parent_expected=self.identity_for_directory(
                    backend, source.parent
                ),
            ):
                self.fail("ACL-write-exposed container reached transaction body")

        self.assertEqual(
            before_acl,
            subprocess.check_output(["/bin/ls", "-lde", str(container)], text=True),
        )
        self.assertFalse(stage.exists())

    def test_adapter_source_pathname_move_is_deferred_after_stage_cleanup(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        moved_source = source.with_name(f"{source.name}.moved")
        mirror_inode = mirror.stat().st_ino
        mirror_bytes = mirror.read_bytes()

        class MoveBeforeBindBackend:
            def inspect_pair(
                self, source_path: pathlib.Path, mirror_path: pathlib.Path
            ) -> Any:
                return adapter.inspect_pair(source_path, mirror_path)

            def prepare(
                self,
                source_path: pathlib.Path,
                mirror_path: pathlib.Path,
                temporary_path: pathlib.Path,
                expected: Any,
                authorize_state: Any,
            ) -> Any:
                source_path.replace(moved_source)
                return adapter.prepare(
                    source_path,
                    mirror_path,
                    temporary_path,
                    expected,
                    authorize_state,
                )

        receipt = self.repair.RepairTool(self.config(), MoveBeforeBindBackend()).repair(
            apply=True, rollout_ids=[UUID_A], queue_unstable=True
        )

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual(mirror_inode, mirror.stat().st_ino)
        self.assertEqual(mirror_bytes, mirror.read_bytes())
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_preprepared_policy_drift_without_clone_is_deferred_and_queued(
        self,
    ) -> None:
        _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        old_inode = mirror.stat().st_ino

        class DriftBeforeBindBackend:
            def inspect_pair(
                self, source_path: pathlib.Path, mirror_path: pathlib.Path
            ) -> Any:
                return adapter.inspect_pair(source_path, mirror_path)

            def prepare(
                self,
                source_path: pathlib.Path,
                mirror_path: pathlib.Path,
                temporary_path: pathlib.Path,
                expected: Any,
                authorize_state: Any,
            ) -> Any:
                mirror_path.chmod(0o660)
                return adapter.prepare(
                    source_path,
                    mirror_path,
                    temporary_path,
                    expected,
                    authorize_state,
                )

        receipt = self.repair.RepairTool(
            self.config(), DriftBeforeBindBackend()
        ).repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(old_inode, mirror.stat().st_ino)
        self.assertEqual(0o660, stat.S_IMODE(mirror.stat().st_mode))
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual([], self.repair._load_intents(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_preprepared_policy_drift_with_clone_retains_intent_and_stage(self) -> None:
        _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        old_inode = mirror.stat().st_ino
        darwin = self.darwin

        class DriftAfterCloneBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.clone_created = False
                self.swap_calls = 0
                super().__init__()

            def strict_clone(self, *arguments: Any, **keywords: Any) -> int:
                descriptor = super().strict_clone(*arguments, **keywords)
                mirror.chmod(0o660)
                self.clone_created = True
                return descriptor

            def swap_names(self, *arguments: Any, **keywords: Any) -> Any:
                self.swap_calls += 1
                return super().swap_names(*arguments, **keywords)

        raw = DriftAfterCloneBackend()
        adapter = self.repair._DarwinAdapter()
        adapter.raw = raw
        try:
            self.repair.RepairTool(self.config(), adapter).repair(
                apply=True,
                rollout_ids=[UUID_A],
                queue_unstable=True,
            )
        except self.repair.FatalRepairError:
            pass
        else:
            if not raw.clone_created:
                self.skipTest("filesystem does not support strict clone")
            self.fail("clone-bound nonexclusive policy drift was not fatal")

        self.assertTrue(raw.clone_created)
        self.assertEqual(0, raw.swap_calls)
        self.assertEqual(old_inode, mirror.stat().st_ino)
        self.assertEqual(0o660, stat.S_IMODE(mirror.stat().st_mode))
        intents = self.repair._load_intents(self.config())
        self.assertEqual(1, len(intents))
        self.assertEqual(self.repair.IntentState.STAGE_BOUND, intents[0].state)
        stages = list(mirror.parent.glob(".codex-reflink-repair-*"))
        self.assertEqual(1, len(stages))
        self.assertTrue((stages[0] / "clone").exists())
        self.assertEqual([], self.repair._load_manifests(self.config()))
        self.assertFalse(self.config().queue_path.exists())

    def test_adapter_rejects_replaced_source_parent_even_with_same_leaf_inode(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        fixture = self
        moved_parents: List[pathlib.Path] = []
        mirror_inode = mirror.stat().st_ino
        mirror_bytes = mirror.read_bytes()

        class ReplaceParentBeforeBindBackend:
            def inspect_pair(
                self, source_path: pathlib.Path, mirror_path: pathlib.Path
            ) -> Any:
                return adapter.inspect_pair(source_path, mirror_path)

            def prepare(
                self,
                source_path: pathlib.Path,
                mirror_path: pathlib.Path,
                temporary_path: pathlib.Path,
                expected: Any,
                authorize_state: Any,
            ) -> Any:
                moved_parents.append(
                    fixture.replace_parent_preserving_leaf(
                        source_path, suffix="same-inode-parent-moved"
                    )
                )
                return adapter.prepare(
                    source_path,
                    mirror_path,
                    temporary_path,
                    expected,
                    authorize_state,
                )

        receipt = self.repair.RepairTool(
            self.config(), ReplaceParentBeforeBindBackend()
        ).repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual(1, len(moved_parents))
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual(mirror_inode, mirror.stat().st_ino)
        self.assertEqual(mirror_bytes, mirror.read_bytes())
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_adapter_source_parent_unreadable_is_fatal_not_deferred(self) -> None:
        _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        adapter.raw.bind_transaction = mock.Mock(
            side_effect=self.darwin.BackendError(
                "source_parent_unreadable",
                "injected source parent access denial",
                errno.EACCES,
            )
        )
        old_inode = mirror.stat().st_ino

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(self.config(), adapter).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        self.assertEqual(old_inode, mirror.stat().st_ino)
        self.assertFalse(self.config().queue_path.exists())
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_real_prepared_recovery_rejects_replaced_source_parent_and_defers(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        config = self.config()
        original_delete_intent = self.repair._delete_intent
        crashed = False

        def crash_after_intent_delete(current_config: Any, rollout_id: str) -> None:
            nonlocal crashed
            original_delete_intent(current_config, rollout_id)
            if not crashed:
                crashed = True
                raise KeyboardInterrupt("injected crash after PREPARED publication")

        with (
            mock.patch.object(
                self.repair, "_delete_intent", side_effect=crash_after_intent_delete
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.repair.RepairTool(config, self.repair._DarwinAdapter()).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        manifests = self.repair._load_manifests(config)
        if not manifests:
            self.skipTest("filesystem does not support strict clone")
        self.assertEqual(self.repair.Phase.PREPARED, manifests[0].phase)
        self.assertEqual([], self.repair._load_intents(config))
        self.assertEqual(1, len(list(mirror.parent.glob(".codex-reflink-repair-*"))))
        old_mirror_inode = mirror.stat().st_ino
        old_mirror_bytes = mirror.read_bytes()
        self.replace_parent_preserving_leaf(
            source, suffix="prepared-recovery-parent-moved"
        )
        darwin = self.darwin

        class SwapSpyBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.swap_calls = 0
                super().__init__()

            def swap_names(self, *arguments: Any, **keywords: Any) -> Any:
                self.swap_calls += 1
                return super().swap_names(*arguments, **keywords)

        adapter = self.repair._DarwinAdapter()
        raw = SwapSpyBackend()
        adapter.raw = raw
        receipt = self.repair.RepairTool(config, adapter).recover(apply=True)

        self.assertEqual("recovery-deferred", receipt["results"][0]["classification"])
        self.assertEqual(0, raw.swap_calls)
        self.assertEqual(old_mirror_inode, mirror.stat().st_ino)
        self.assertEqual(old_mirror_bytes, mirror.read_bytes())
        self.assertEqual([UUID_A], self.repair._load_queue(config))
        self.assertEqual([], self.repair._load_manifests(config))
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_source_fifo_replacement_before_bind_is_nonblocking_and_deferred(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        adapter = self.repair._DarwinAdapter()
        moved_source = source.with_name(f"{source.name}.regular")
        mirror_inode = mirror.stat().st_ino

        class ReplaceWithFifoBeforeBindBackend:
            def inspect_pair(
                self, source_path: pathlib.Path, mirror_path: pathlib.Path
            ) -> Any:
                return adapter.inspect_pair(source_path, mirror_path)

            def prepare(
                self,
                source_path: pathlib.Path,
                mirror_path: pathlib.Path,
                temporary_path: pathlib.Path,
                expected: Any,
                authorize_state: Any,
            ) -> Any:
                os.replace(source_path, moved_source)
                os.mkfifo(source_path, 0o600)
                return adapter.prepare(
                    source_path,
                    mirror_path,
                    temporary_path,
                    expected,
                    authorize_state,
                )

        previous_handler = signal.getsignal(signal.SIGALRM)

        def timeout(_signum: int, _frame: Any) -> None:
            raise TimeoutError("FIFO open blocked instead of using O_NONBLOCK")

        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        started = time.monotonic()
        try:
            receipt = self.repair.RepairTool(
                self.config(), ReplaceWithFifoBeforeBindBackend()
            ).repair(apply=True, rollout_ids=[UUID_A], queue_unstable=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual(mirror_inode, mirror.stat().st_ino)
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_source_archive_move_after_clone_aborts_before_prepared_and_queues(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        archived = self.rollout_path(self.source_root, "archived_sessions", UUID_A)
        old_mirror_inode = mirror.stat().st_ino
        old_mirror_bytes = mirror.read_bytes()
        darwin = self.darwin

        class MoveSourceAfterCloneBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.moved = False
                super().__init__()

            def strict_clone(self, *arguments: Any, **keywords: Any) -> int:
                descriptor = super().strict_clone(*arguments, **keywords)
                os.replace(source, archived)
                self.moved = True
                return descriptor

        adapter = self.repair._DarwinAdapter()
        raw = MoveSourceAfterCloneBackend()
        adapter.raw = raw
        receipt = self.repair.RepairTool(self.config(), adapter).repair(
            apply=True, rollout_ids=[UUID_A], queue_unstable=True
        )

        result = self.results_by_id(receipt)[UUID_A]
        if result["classification"] == "unsupported":
            self.skipTest("filesystem does not support strict clone")
        self.assertTrue(raw.moved)
        self.assertEqual("unstable", result["classification"])
        self.assertEqual("deferred", result["outcome"])
        self.assertEqual([UUID_A], self.repair._load_queue(self.config()))
        self.assertEqual(old_mirror_inode, mirror.stat().st_ino)
        self.assertEqual(old_mirror_bytes, mirror.read_bytes())
        self.assertEqual([], self.repair._load_intents(self.config()))
        self.assertEqual([], self.repair._load_manifests(self.config()))
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_real_postswap_source_path_or_parent_move_rolls_back_and_defers(
        self,
    ) -> None:
        for index, mutation in enumerate(("pathname", "parent")):
            with self.subTest(mutation=mutation):
                source, mirror, _stage, _clone = self.prepare_pair(create_stage=False)
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"real-postswap-{index}"
                )
                moved = (
                    source.with_name(f"{source.name}.moved")
                    if mutation == "pathname"
                    else source.parent.with_name(f"{source.parent.name}.moved")
                )
                darwin = self.darwin

                class MoveAfterSwapBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.moved = False
                        super().__init__()

                    def swap_names(
                        self, *arguments: Any, **keyword_arguments: Any
                    ) -> Any:
                        result = super().swap_names(*arguments, **keyword_arguments)
                        if not self.moved:
                            if mutation == "pathname":
                                source.replace(moved)
                            else:
                                source.parent.replace(moved)
                                source.parent.mkdir(mode=0o755)
                            self.moved = True
                        return result

                adapter = self.repair._DarwinAdapter()
                adapter.raw = MoveAfterSwapBackend()
                old_inode = mirror.stat().st_ino
                old_bytes = mirror.read_bytes()
                phases: List[str] = []
                original_write = self.repair._write_manifest

                def record_manifest(current_config: Any, manifest: Any) -> Any:
                    phases.append(manifest.phase.value)
                    return original_write(current_config, manifest)

                with mock.patch.object(
                    self.repair, "_write_manifest", side_effect=record_manifest
                ):
                    receipt = self.repair.RepairTool(config, adapter).repair(
                        apply=True,
                        rollout_ids=[UUID_A],
                        queue_unstable=True,
                    )

                result = self.results_by_id(receipt)[UUID_A]
                if result["classification"] == "unsupported":
                    self.skipTest("filesystem does not support strict clone/swap")
                self.assertEqual("unstable", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertEqual(old_inode, mirror.stat().st_ino)
                self.assertEqual(old_bytes, mirror.read_bytes())
                self.assertIn(self.repair.Phase.DEFERRED.value, phases)
                self.assertNotIn(self.repair.Phase.FAILED.value, phases)
                self.assertEqual([UUID_A], self.repair._load_queue(config))
                self.assertEqual([], self.repair._load_manifests(config))
                self.assertEqual(
                    [], list(mirror.parent.glob(".codex-reflink-repair-*"))
                )

    def test_real_postswap_clone_mutation_is_fatal_and_retains_evidence(self) -> None:
        _source, mirror, _stage, _clone = self.prepare_pair(create_stage=False)
        darwin = self.darwin

        class MutateCloneAfterSwapBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.mutated = False
                super().__init__()

            def swap_names(self, *arguments: Any, **keyword_arguments: Any) -> Any:
                result = super().swap_names(*arguments, **keyword_arguments)
                if not self.mutated:
                    with mirror.open("r+b", buffering=0) as stream:
                        stream.write(b"X")
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.mutated = True
                return result

        adapter = self.repair._DarwinAdapter()
        adapter.raw = MutateCloneAfterSwapBackend()

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(self.config(), adapter).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        manifests = self.repair._load_manifests(self.config())
        self.assertEqual(1, len(manifests))
        self.assertIn(
            manifests[0].phase,
            {self.repair.Phase.PREPARED, self.repair.Phase.ROLLBACK_READY},
        )
        self.assertEqual([], self.repair._load_queue(self.config()))
        self.assertEqual(1, len(list(mirror.parent.glob(".codex-reflink-repair-*"))))

    def test_real_nonexclusive_mirror_mode_or_acl_is_skipped_before_stage(self) -> None:
        cases = (
            ("group-writable", 0o620, False),
            ("other-writable", 0o602, False),
            ("extended-acl", 0o600, True),
        )
        for index, (label, mode, add_acl) in enumerate(cases):
            with self.subTest(policy=label):
                _source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                config = dataclasses.replace(
                    self.config(),
                    state_root=self.state_root / f"real-nonexclusive-{index}",
                )
                mirror.chmod(mode)
                if add_acl:
                    subprocess.run(
                        [
                            "/bin/chmod",
                            "+a",
                            "everyone allow readattr",
                            str(mirror),
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                adapter = self.repair._DarwinAdapter()
                try:
                    dry_run = self.repair.RepairTool(config, adapter).repair(
                        apply=False, rollout_ids=[UUID_A]
                    )
                    applied = self.repair.RepairTool(config, adapter).repair(
                        apply=True, rollout_ids=[UUID_A]
                    )
                finally:
                    if add_acl:
                        subprocess.run(
                            ["/bin/chmod", "-N", str(mirror)],
                            check=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                for receipt in (dry_run, applied):
                    result = self.results_by_id(receipt)[UUID_A]
                    self.assertEqual("unsupported", result["classification"])
                    self.assertEqual("skipped", result["outcome"])
                self.assertEqual([], self.repair._load_intents(config))
                self.assertEqual([], self.repair._load_manifests(config))
                self.assertEqual(
                    [], list(mirror.parent.glob(".codex-reflink-repair-*"))
                )

    def test_real_mirror_mode_0644_is_accepted_and_preserved(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        mirror.chmod(0o644)
        old_inode = mirror.stat().st_ino
        receipt = self.repair.RepairTool(
            self.config(), self.repair._DarwinAdapter()
        ).repair(apply=True, rollout_ids=[UUID_A])

        result = self.results_by_id(receipt)[UUID_A]
        if result["classification"] == "unsupported":
            self.skipTest("filesystem does not support strict clone/swap")
        self.assertEqual("repaired", result["classification"])
        self.assertNotEqual(old_inode, mirror.stat().st_ino)
        self.assertEqual(source.read_bytes(), mirror.read_bytes())
        self.assertEqual(0o644, stat.S_IMODE(mirror.stat().st_mode))
        self.assertEqual(
            "policy-value",
            subprocess.check_output(
                [
                    "/usr/bin/xattr",
                    "-p",
                    "com.openai.codex.reflink-test",
                    str(mirror),
                ],
                text=True,
            ).rstrip("\n"),
        )
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))

    def test_adapter_preserves_unsafe_link_count_classification(self) -> None:
        source, _mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        os.link(source, source.with_name("source-hardlink"))
        adapter = self.repair._DarwinAdapter()

        receipt = self.repair.RepairTool(self.config(), adapter).repair(
            apply=False, rollout_ids=[UUID_A]
        )

        result = self.results_by_id(receipt)[UUID_A]
        self.assertEqual("unsafe-link-count", result["classification"])
        self.assertEqual("skipped", result["outcome"])

    def test_repeated_xattr_or_acl_generation_churn_is_deferred_and_queued(
        self,
    ) -> None:
        for index, policy_kind in enumerate(("xattr", "acl")):
            with self.subTest(policy_kind=policy_kind):
                source, _mirror, _stage, _clone = self.prepare_pair(create_stage=False)
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"policy-churn-{index}"
                )
                adapter = self.repair._DarwinAdapter()
                calls = 0
                if policy_kind == "xattr":
                    original = adapter.raw._snapshot_xattrs

                    def churn(descriptor: int) -> Any:
                        nonlocal calls
                        calls += 1
                        subprocess.run(
                            [
                                "/usr/bin/xattr",
                                "-w",
                                "com.openai.codex.policy-churn",
                                str(calls),
                                str(source),
                            ],
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        return original(descriptor)

                    patched = mock.patch.object(
                        adapter.raw, "_snapshot_xattrs", side_effect=churn
                    )
                else:
                    original = adapter.raw._snapshot_acl

                    def churn(descriptor: int) -> Any:
                        nonlocal calls
                        calls += 1
                        command = (
                            [
                                "/bin/chmod",
                                "+a",
                                "everyone allow readattr",
                                str(source),
                            ]
                            if calls % 2
                            else ["/bin/chmod", "-N", str(source)]
                        )
                        subprocess.run(
                            command,
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        return original(descriptor)

                    patched = mock.patch.object(
                        adapter.raw, "_snapshot_acl", side_effect=churn
                    )
                try:
                    with patched:
                        receipt = self.repair.RepairTool(config, adapter).repair(
                            apply=True,
                            rollout_ids=[UUID_A],
                            queue_unstable=True,
                        )
                finally:
                    subprocess.run(
                        ["/usr/bin/xattr", "-c", str(source)],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    subprocess.run(
                        ["/bin/chmod", "-N", str(source)],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )

                result = self.results_by_id(receipt)[UUID_A]
                self.assertEqual("unstable", result["classification"])
                self.assertEqual("deferred", result["outcome"])
                self.assertGreaterEqual(calls, 2)
                self.assertEqual([UUID_A], self.repair._load_queue(config))

    def test_policy_eacces_or_io_is_unreadable_during_inspection(self) -> None:
        for index, errno_value in enumerate((errno.EACCES, errno.EIO)):
            with self.subTest(errno_value=errno_value):
                self.prepare_pair(create_stage=False)
                config = dataclasses.replace(
                    self.config(), state_root=self.state_root / f"unreadable-{index}"
                )
                adapter = self.repair._DarwinAdapter()
                error = self.darwin.BackendError(
                    "policy_unreadable", "injected policy read failure", errno_value
                )
                adapter.raw.snapshot_policy = mock.Mock(side_effect=error)

                receipt = self.repair.RepairTool(config, adapter).repair(
                    apply=True, rollout_ids=[UUID_A], queue_unstable=True
                )

                result = self.results_by_id(receipt)[UUID_A]
                self.assertEqual("unreadable", result["classification"])
                self.assertEqual("skipped", result["outcome"])
                self.assertEqual([], self.repair._load_queue(config))

    def test_apply_time_policy_io_failure_is_safety_fatal_without_stage_leak(
        self,
    ) -> None:
        _source, mirror, _stage, _clone = self.prepare_pair(create_stage=False)
        adapter = self.repair._DarwinAdapter()
        original = adapter.raw.snapshot_policy
        calls = 0

        def fail_after_inspection(descriptor: int) -> Any:
            nonlocal calls
            calls += 1
            if calls > 4:
                raise self.darwin.BackendError(
                    "policy_unreadable", "injected apply-time EIO", errno.EIO
                )
            return original(descriptor)

        adapter.raw.snapshot_policy = mock.Mock(side_effect=fail_after_inspection)
        old_inode = mirror.stat().st_ino

        with self.assertRaises(self.repair.FatalRepairError):
            self.repair.RepairTool(self.config(), adapter).repair(
                apply=True, rollout_ids=[UUID_A], queue_unstable=True
            )

        self.assertEqual(old_inode, mirror.stat().st_ino)
        self.assertEqual([], list(mirror.parent.glob(".codex-reflink-repair-*")))
        self.assertFalse(self.config().queue_path.exists())

    def test_adapter_clone_enotsup_and_exdev_are_terminal_retry_outcomes(self) -> None:
        for rollout_id in (UUID_A, UUID_B):
            self.write_rollout(self.source_root, "sessions", rollout_id, b"same\n")
            self.write_rollout(
                self.mirror_root, "archived_sessions", rollout_id, b"same\n"
            )
        config = self.config()
        self.repair._write_queue(config, [UUID_A, UUID_B])
        adapter = self.repair._DarwinAdapter()
        adapter.raw.strict_clone = mock.Mock(
            side_effect=[
                self.darwin.BackendError(
                    "clone_failed", "injected ENOTSUP", errno.ENOTSUP
                ),
                self.darwin.BackendError("clone_failed", "injected EXDEV", errno.EXDEV),
            ]
        )

        receipt = self.repair.RepairTool(config, adapter).retry(apply=True)

        results = self.results_by_id(receipt)
        for rollout_id in (UUID_A, UUID_B):
            self.assertEqual("unsupported", results[rollout_id]["classification"])
            self.assertEqual("terminal", results[rollout_id]["outcome"])
        self.assertEqual(2, adapter.raw.strict_clone.call_count)
        self.assertEqual([], self.repair._load_queue(config))
        self.assertEqual([], list(self.mirror_root.rglob(".codex-reflink-repair-*")))

    def test_strict_clone_closes_new_fd_when_identity_binding_fails(self) -> None:
        darwin = self.darwin
        for index, failure in enumerate(("identity", "name-revalidation")):
            with self.subTest(failure=failure):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-fd-{index:032x}"
                stage.mkdir(mode=0o700)

                class FailingCloneBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.clone_fds: List[int] = []
                        super().__init__()

                    def open_leaf(
                        self, parent_fd: int, name: str, *, writable: bool = False
                    ) -> int:
                        descriptor = super().open_leaf(
                            parent_fd, name, writable=writable
                        )
                        if name == "clone":
                            self.clone_fds.append(descriptor)
                        return descriptor

                    def identity(self, descriptor: int) -> Any:
                        if failure == "identity" and descriptor in self.clone_fds:
                            raise darwin.BackendError(
                                "fstat_failed",
                                "injected clone identity failure",
                                errno.EIO,
                            )
                        return super().identity(descriptor)

                    def require_identity_at(
                        self, parent_fd: int, name: str, expected: Any
                    ) -> Any:
                        if failure == "name-revalidation" and name == "clone":
                            raise darwin.BackendError(
                                "identity_mismatch",
                                "injected clone pathname mismatch",
                            )
                        return super().require_identity_at(parent_fd, name, expected)

                backend = FailingCloneBackend()
                source_fd = os.open(source, os.O_RDONLY)
                stage_fd = backend.open_absolute_dir(str(stage))
                baseline = self.open_fd_set()
                try:
                    with self.assertRaises(darwin.BackendError) as caught:
                        backend.strict_clone(
                            source_fd,
                            stage_fd,
                            "clone",
                            authorize_state=self.allow_state_mutation,
                        )
                    if not backend.clone_fds and caught.exception.errno_value in {
                        errno.ENOTSUP,
                        errno.EXDEV,
                    }:
                        self.skipTest("filesystem does not support strict clone")
                    self.assertEqual(baseline, self.open_fd_set())
                    self.assertEqual(1, len(backend.clone_fds))
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.clone_fds[0])
                    self.assertEqual(errno.EBADF, closed.exception.errno)
                finally:
                    os.close(stage_fd)
                    os.close(source_fd)

    def test_strict_clone_keyboard_interrupt_drains_new_fd(self) -> None:
        darwin = self.darwin
        for index, failure in enumerate(("identity", "name-revalidation"), start=780):
            with self.subTest(failure=failure):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                stage.mkdir(mode=0o700)
                sentinel = KeyboardInterrupt(f"clone {failure} interrupted")

                class InterruptedCloneBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.clone_fds: List[int] = []
                        super().__init__()

                    def open_leaf(
                        self, parent_fd: int, name: str, *, writable: bool = False
                    ) -> int:
                        descriptor = super().open_leaf(
                            parent_fd, name, writable=writable
                        )
                        if name == "clone":
                            self.clone_fds.append(descriptor)
                        return descriptor

                    def identity(self, descriptor: int) -> Any:
                        if failure == "identity" and descriptor in self.clone_fds:
                            raise sentinel
                        return super().identity(descriptor)

                    def require_identity_at(
                        self, parent_fd: int, name: str, expected: Any
                    ) -> Any:
                        if failure == "name-revalidation" and name == "clone":
                            raise sentinel
                        return super().require_identity_at(parent_fd, name, expected)

                backend = InterruptedCloneBackend()
                source_fd = os.open(source, os.O_RDONLY)
                stage_fd = backend.open_absolute_dir(str(stage))
                baseline = self.open_fd_set()
                try:
                    try:
                        backend.strict_clone(
                            source_fd,
                            stage_fd,
                            "clone",
                            authorize_state=self.allow_state_mutation,
                        )
                    except KeyboardInterrupt as caught:
                        self.assertIs(sentinel, caught)
                    except darwin.BackendError as error:
                        if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                            self.skipTest(
                                f"filesystem does not support strict clone: {error}"
                            )
                        raise
                    else:
                        self.fail("strict clone did not propagate KeyboardInterrupt")

                    self.assertEqual(baseline, self.open_fd_set())
                    self.assertEqual(1, len(backend.clone_fds))
                    with self.assertRaises(OSError) as closed:
                        os.fstat(backend.clone_fds[0])
                    self.assertEqual(errno.EBADF, closed.exception.errno)
                    self.assertTrue((stage / "clone").is_file())
                finally:
                    os.close(stage_fd)
                    os.close(source_fd)

    def test_recovery_bind_closes_local_leaf_fds_when_identity_fails(self) -> None:
        darwin = self.darwin
        for index, failure in enumerate(("destination", "temporary"), start=800):
            with self.subTest(failure=failure):
                (
                    preparing_backend,
                    _source,
                    mirror,
                    stage,
                    container_identity,
                    _original_expectation,
                    stage_identity,
                    clone_identity,
                    _clone_sha256,
                    _clone_expectation,
                ) = self.prepare_intent_clone_artifact(f"{index:032x}")
                mirror_fd = os.open(mirror, os.O_RDONLY)
                try:
                    original_identity = preparing_backend.identity(mirror_fd)
                finally:
                    os.close(mirror_fd)

                class FailingRecoveryBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.local_leaf_fds: List[int] = []
                        super().__init__()

                    def open_leaf(
                        self, parent_fd: int, name: str, *, writable: bool = False
                    ) -> int:
                        descriptor = super().open_leaf(
                            parent_fd, name, writable=writable
                        )
                        if name in {mirror.name, "clone"}:
                            self.local_leaf_fds.append(descriptor)
                        return descriptor

                    def identity(self, descriptor: int) -> Any:
                        if descriptor in self.local_leaf_fds:
                            position = self.local_leaf_fds.index(descriptor)
                            target = 0 if failure == "destination" else 1
                            if position == target:
                                raise darwin.BackendError(
                                    "fstat_failed",
                                    f"injected {failure} identity failure",
                                    errno.EIO,
                                )
                        return super().identity(descriptor)

                backend = FailingRecoveryBackend()
                baseline = self.open_fd_set()
                with self.assertRaises(darwin.BackendError):
                    backend.bind_recovery(
                        None,
                        str(mirror),
                        str(stage / "clone"),
                        original_identity,
                        clone_identity,
                        destination_parent_expected=container_identity,
                        temporary_parent_expected=stage_identity,
                    )

                self.assertEqual(baseline, self.open_fd_set())
                self.assertGreaterEqual(len(backend.local_leaf_fds), 2)
                for descriptor in backend.local_leaf_fds[:2]:
                    with self.assertRaises(OSError) as closed:
                        os.fstat(descriptor)
                    self.assertEqual(errno.EBADF, closed.exception.errno)

    def test_bind_failures_drain_owned_fds_without_masking_primary_error(self) -> None:
        darwin = self.darwin
        for index, recovery in enumerate((False, True), start=820):
            with self.subTest(recovery=recovery):
                if recovery:
                    (
                        preparing_backend,
                        _source,
                        mirror,
                        stage,
                        container_identity,
                        _original_expectation,
                        stage_identity,
                        clone_identity,
                        _clone_sha256,
                        _clone_expectation,
                    ) = self.prepare_intent_clone_artifact(f"{index:032x}")
                    mirror_fd = os.open(mirror, os.O_RDONLY)
                    try:
                        original_identity = preparing_backend.identity(mirror_fd)
                    finally:
                        os.close(mirror_fd)
                    source = None
                else:
                    source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                        create_stage=False
                    )
                    stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                    stage.mkdir(mode=0o700)
                    container_identity = None
                    stage_identity = None
                    clone_identity = None
                    original_identity = None

                sentinel = RuntimeError(
                    "recovery bind primary" if recovery else "live bind primary"
                )
                armed = False
                close_calls: List[int] = []

                class PrimaryBindFailureBackend(darwin.DarwinBackend):
                    def require_exclusive_writer(
                        self, descriptor: int, subject: str
                    ) -> Any:
                        nonlocal armed
                        armed = True
                        raise sentinel

                backend = PrimaryBindFailureBackend()
                real_close = self.darwin.os.close

                def flaky_close(descriptor: int) -> None:
                    if not armed:
                        real_close(descriptor)
                        return
                    close_calls.append(descriptor)
                    real_close(descriptor)
                    if len(close_calls) == 1:
                        raise OSError(errno.EIO, "injected close failure")

                baseline = self.open_fd_set()
                with (
                    mock.patch.object(self.darwin.os, "close", side_effect=flaky_close),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    if recovery:
                        backend.bind_recovery(
                            None,
                            str(mirror),
                            str(stage / "clone"),
                            original_identity,
                            clone_identity,
                            destination_parent_expected=container_identity,
                            temporary_parent_expected=stage_identity,
                        )
                    else:
                        assert source is not None
                        backend.bind_transaction(
                            str(source),
                            str(mirror),
                            str(stage / "clone"),
                            source_parent_expected=self.identity_for_directory(
                                backend, source.parent
                            ),
                        )

                self.assertIs(sentinel, caught.exception)
                self.assertGreaterEqual(len(close_calls), 4)
                self.assertEqual(len(close_calls), len(set(close_calls)))
                self.assertEqual(baseline, self.open_fd_set())

    def test_live_bind_trace_registration_keeps_one_fd_owner(self) -> None:
        darwin = self.darwin
        for index, registered in enumerate((False, True), start=900):
            with self.subTest(registered=registered):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                stage.mkdir(mode=0o700)
                backend = darwin.DarwinBackend()
                expected_parent = self.identity_for_directory(backend, source.parent)
                baseline = self.open_fd_set()
                boundary_fds: set[int] = set()
                close_calls: List[int] = []
                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor in boundary_fds:
                        close_calls.append(descriptor)
                    real_close(descriptor)

                def registration_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
                    owner = frame.f_locals.get("owner")
                    transaction = frame.f_locals.get("self")
                    if (
                        not isinstance(owner, darwin._OwnedFD)
                        or owner.closed
                        or owner._retain_if_registered is None
                        or not isinstance(transaction, darwin.BoundTransaction)
                    ):
                        return False
                    slot_owner = transaction._source_parent_owner
                    is_registered = slot_owner is owner
                    if is_registered != registered:
                        return False
                    owner_attribute = frame.f_locals.get("owner_attribute")
                    if owner_attribute != "_source_parent_owner":
                        return False
                    descriptor = owner.fileno()
                    evidence["owner"] = owner
                    evidence["slot_owner"] = slot_owner
                    evidence["transaction"] = transaction
                    evidence["fd"] = descriptor
                    evidence["line_number"] = frame.f_lineno
                    boundary_fds.add(descriptor)
                    return True

                target_code = darwin.BoundTransaction._install_fd_owner.__code__
                with mock.patch.object(darwin.os, "close", side_effect=record_close):
                    evidence = self.assert_trace_interruption(
                        target_code,
                        registration_boundary,
                        lambda: backend.bind_transaction(
                            str(source),
                            str(mirror),
                            str(stage / "clone"),
                            source_parent_expected=expected_parent,
                        ),
                        label=f"live bind registration {registered}",
                        events=(("return",) if registered else ("line",)),
                    )
                    evidence["owner"].close()

                self.assertIsInstance(evidence["line_number"], int)
                self.assertEqual("return" if registered else "line", evidence["event"])
                self.assertEqual(
                    registered, evidence["slot_owner"] is evidence["owner"]
                )
                transaction = evidence["transaction"]
                self.assertTrue(evidence["owner"].closed)
                self.assertEqual(1, close_calls.count(evidence["fd"]))
                with self.assertRaises(OSError) as closed:
                    os.fstat(evidence["fd"])
                self.assertEqual(errno.EBADF, closed.exception.errno)
                self.assertTrue(transaction._closed)
                self.assertTrue(
                    all(
                        getattr(transaction, owner_attribute).closed
                        for owner_attribute in transaction._FD_OWNER_CLOSE_ORDER
                    )
                )
                self.assertTrue(stage.is_dir())
                self.assertEqual(baseline, self.open_fd_set())
                stage.rmdir()

    def test_recovery_trace_registration_closes_distinct_orientation_owners(
        self,
    ) -> None:
        darwin = self.darwin
        case_index = 920
        for forward in (False, True):
            for registered in (False, True):
                with self.subTest(forward=forward, registered=registered):
                    (
                        backend,
                        _source,
                        mirror,
                        stage,
                        container_identity,
                        original_expectation,
                        stage_identity,
                        clone_identity,
                        _clone_sha256,
                        _clone_expectation,
                    ) = self.prepare_intent_clone_artifact(f"{case_index:032x}")
                    case_index += 1
                    mirror_fd = os.open(mirror, os.O_RDONLY)
                    try:
                        original_identity = backend.identity(mirror_fd)
                    finally:
                        os.close(mirror_fd)
                    if forward:
                        destination_parent_fd = backend.open_absolute_dir(
                            str(mirror.parent)
                        )
                        temporary_parent_fd = backend.open_absolute_dir(str(stage))
                        try:
                            backend.swap_names(
                                destination_parent_fd,
                                mirror.name,
                                original_identity,
                                temporary_parent_fd,
                                "clone",
                                clone_identity,
                                authorize_state=self.allow_state_mutation,
                                action="swap_forward",
                            )
                        finally:
                            os.close(temporary_parent_fd)
                            os.close(destination_parent_fd)

                    destination_attribute = (
                        "_clone_owner" if forward else "_original_owner"
                    )
                    temporary_attribute = (
                        "_original_owner" if forward else "_clone_owner"
                    )
                    baseline = self.open_fd_set()
                    boundary_fds: set[int] = set()
                    close_calls: List[int] = []
                    real_close = darwin.os.close

                    def record_close(descriptor: int) -> None:
                        if descriptor in boundary_fds:
                            close_calls.append(descriptor)
                        real_close(descriptor)

                    def registration_boundary(
                        frame: Any, evidence: Dict[str, Any]
                    ) -> bool:
                        if registered:
                            transaction = frame.f_locals.get("self")
                            destination_owner = frame.f_locals.get("destination_owner")
                            temporary_owner = frame.f_locals.get("temporary_owner")
                        else:
                            transaction = frame.f_locals.get("self")
                            destination_owner = frame.f_locals.get("owner")
                            caller = frame.f_back
                            if (
                                frame.f_locals.get("owner_attribute")
                                != destination_attribute
                                or caller is None
                                or caller.f_code
                                is not darwin.BoundTransaction._install_recovery_leaf_owners.__code__
                            ):
                                return False
                            temporary_owner = caller.f_locals.get("temporary_owner")
                        if (
                            not isinstance(transaction, darwin.BoundTransaction)
                            or not isinstance(destination_owner, darwin._OwnedFD)
                            or not isinstance(temporary_owner, darwin._OwnedFD)
                            or destination_owner.closed
                            or temporary_owner.closed
                            or destination_owner._retain_if_registered is None
                        ):
                            return False
                        slot_owner = getattr(transaction, destination_attribute)
                        is_registered = slot_owner is destination_owner
                        if is_registered != registered:
                            return False
                        descriptors = (
                            destination_owner.fileno(),
                            temporary_owner.fileno(),
                        )
                        evidence["transaction"] = transaction
                        evidence["destination_owner"] = destination_owner
                        evidence["temporary_owner"] = temporary_owner
                        evidence["slot_owner"] = slot_owner
                        evidence["fds"] = descriptors
                        evidence["line_number"] = frame.f_lineno
                        boundary_fds.update(descriptors)
                        return True

                    target_code = (
                        darwin.BoundTransaction._install_recovery_leaf_owners.__code__
                        if registered
                        else darwin.BoundTransaction._install_fd_owner.__code__
                    )
                    with mock.patch.object(
                        darwin.os, "close", side_effect=record_close
                    ):
                        evidence = self.assert_trace_interruption(
                            target_code,
                            registration_boundary,
                            lambda: backend.bind_recovery(
                                None,
                                str(mirror),
                                str(stage / "clone"),
                                original_identity,
                                clone_identity,
                                destination_parent_expected=container_identity,
                                temporary_parent_expected=stage_identity,
                            ),
                            label=(
                                f"recovery registration {registered} forward {forward}"
                            ),
                        )
                        evidence["destination_owner"].close()
                        evidence["temporary_owner"].close()

                    self.assertIsInstance(evidence["line_number"], int)
                    self.assertEqual(
                        registered,
                        evidence["slot_owner"] is evidence["destination_owner"],
                    )
                    self.assertIsNot(
                        evidence["destination_owner"]._state,
                        evidence["temporary_owner"]._state,
                    )
                    transaction = evidence["transaction"]
                    self.assertIsNot(
                        evidence["temporary_owner"],
                        getattr(transaction, temporary_attribute),
                    )
                    self.assertTrue(evidence["destination_owner"].closed)
                    self.assertTrue(evidence["temporary_owner"].closed)
                    self.assertEqual(2, len(set(evidence["fds"])))
                    for descriptor in evidence["fds"]:
                        self.assertEqual(1, close_calls.count(descriptor))
                        with self.assertRaises(OSError) as closed:
                            os.fstat(descriptor)
                        self.assertEqual(errno.EBADF, closed.exception.errno)
                    self.assertTrue(transaction._closed)
                    self.assertTrue(
                        all(
                            getattr(transaction, owner_attribute).closed
                            for owner_attribute in transaction._FD_OWNER_CLOSE_ORDER
                        )
                    )
                    self.assertTrue(stage.is_dir())
                    self.assertTrue((stage / "clone").is_file())
                    self.assertEqual(baseline, self.open_fd_set())

    def test_bound_recovery_return_trace_drains_registered_fd_owners(self) -> None:
        darwin = self.darwin
        (
            backend,
            source,
            mirror,
            stage,
            container_identity,
            _original_expectation,
            stage_identity,
            clone_identity,
            _clone_sha256,
            _clone_expectation,
        ) = self.prepare_intent_clone_artifact(f"{940:032x}")
        source_parent_identity = self.identity_for_directory(backend, source.parent)
        source_fd = os.open(source, os.O_RDONLY)
        mirror_fd = os.open(mirror, os.O_RDONLY)
        try:
            source_identity = backend.identity(source_fd)
            original_identity = backend.identity(mirror_fd)
        finally:
            os.close(mirror_fd)
            os.close(source_fd)

        baseline = self.open_fd_set()
        evidence: Dict[str, Any] = {}
        captured_identities: Dict[int, Tuple[int, int]] = {}
        close_calls: List[int] = []
        close_identity_mismatches: List[Any] = []
        real_close = darwin.os.close

        def record_close(descriptor: int) -> None:
            expected = captured_identities.get(descriptor)
            if expected is not None:
                try:
                    metadata = os.fstat(descriptor)
                except OSError as error:
                    close_identity_mismatches.append((descriptor, error.errno))
                else:
                    observed = (metadata.st_dev, metadata.st_ino)
                    if observed != expected:
                        close_identity_mismatches.append(
                            (descriptor, expected, observed)
                        )
                close_calls.append(descriptor)
            real_close(descriptor)

        def recovery_return_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            if (
                linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                != "return transaction"
            ):
                return False
            transaction = frame.f_locals.get("transaction")
            if (
                not isinstance(transaction, darwin.BoundTransaction)
                or transaction._closed
            ):
                return False
            owner_slots = {
                owner_attribute: getattr(transaction, owner_attribute)
                for owner_attribute in transaction._FD_OWNER_CLOSE_ORDER
            }
            if len(owner_slots) != 6 or any(
                not isinstance(owner, darwin._OwnedFD) or owner.closed
                for owner in owner_slots.values()
            ):
                return False
            descriptors = {
                owner_attribute: owner.fileno()
                for owner_attribute, owner in owner_slots.items()
            }
            if len(set(descriptors.values())) != len(descriptors):
                return False
            identities = {}
            for owner_attribute, descriptor in descriptors.items():
                metadata = os.fstat(descriptor)
                identities[descriptor] = (metadata.st_dev, metadata.st_ino)
                self.assertIs(
                    owner_slots[owner_attribute],
                    getattr(transaction, owner_attribute),
                )
            captured_identities.update(identities)
            captured["transaction"] = transaction
            captured["owner_slots"] = owner_slots
            captured["descriptors"] = descriptors
            captured["line_number"] = frame.f_lineno
            return True

        def bind_recovery() -> Any:
            return backend.bind_recovery(
                str(source),
                str(mirror),
                str(stage / "clone"),
                original_identity,
                clone_identity,
                source_expected=source_identity,
                source_parent_expected=source_parent_identity,
                destination_parent_expected=container_identity,
                temporary_parent_expected=stage_identity,
            )

        closed_before_fallback: Dict[str, bool] = {}
        transaction_closed_before_fallback = False
        close_calls_before_fallback: Tuple[int, ...] = ()
        try:
            with mock.patch.object(darwin.os, "close", side_effect=record_close):
                self.assert_trace_interruption(
                    darwin.BoundTransaction.recover.__code__,
                    recovery_return_boundary,
                    bind_recovery,
                    label="bound recovery return handoff",
                    evidence=evidence,
                )
                transaction = evidence["transaction"]
                closed_before_fallback = {
                    owner_attribute: owner.closed
                    for owner_attribute, owner in evidence["owner_slots"].items()
                }
                transaction_closed_before_fallback = transaction._closed
                close_calls_before_fallback = tuple(close_calls)
        finally:
            transaction = evidence.get("transaction")
            if isinstance(transaction, darwin.BoundTransaction):
                transaction.close(primary_error=evidence.get("primary"))

        self.assertIsInstance(evidence["line_number"], int)
        self.assertTrue(transaction_closed_before_fallback)
        self.assertEqual(
            {owner_attribute: True for owner_attribute in evidence["owner_slots"]},
            closed_before_fallback,
        )
        self.assertEqual([], close_identity_mismatches)
        self.assertEqual(
            sorted(evidence["descriptors"].values()),
            sorted(close_calls_before_fallback),
        )
        for owner_attribute, owner in evidence["owner_slots"].items():
            self.assertIs(owner, getattr(evidence["transaction"], owner_attribute))
            self.assertTrue(owner.closed)
        for descriptor in evidence["descriptors"].values():
            self.assertEqual(1, close_calls_before_fallback.count(descriptor))
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

        normal_transaction = bind_recovery()
        normal_owner_slots = {
            owner_attribute: getattr(normal_transaction, owner_attribute)
            for owner_attribute in normal_transaction._FD_OWNER_CLOSE_ORDER
        }
        self.assertFalse(normal_transaction._closed)
        self.assertEqual(6, len(normal_owner_slots))
        self.assertTrue(all(not owner.closed for owner in normal_owner_slots.values()))
        normal_descriptors = {
            owner_attribute: owner.fileno()
            for owner_attribute, owner in normal_owner_slots.items()
        }
        self.assertEqual(6, len(set(normal_descriptors.values())))
        normal_identities = {
            descriptor: (
                os.fstat(descriptor).st_dev,
                os.fstat(descriptor).st_ino,
            )
            for descriptor in normal_descriptors.values()
        }
        normal_close_calls: List[int] = []
        normal_identity_mismatches: List[Any] = []

        def record_normal_close(descriptor: int) -> None:
            expected = normal_identities.get(descriptor)
            if expected is not None:
                metadata = os.fstat(descriptor)
                observed = (metadata.st_dev, metadata.st_ino)
                if observed != expected:
                    normal_identity_mismatches.append((descriptor, expected, observed))
                normal_close_calls.append(descriptor)
            real_close(descriptor)

        with mock.patch.object(darwin.os, "close", side_effect=record_normal_close):
            normal_transaction.close()
        self.assertTrue(normal_transaction._closed)
        self.assertTrue(all(owner.closed for owner in normal_owner_slots.values()))
        self.assertEqual([], normal_identity_mismatches)
        self.assertEqual(
            sorted(normal_descriptors.values()), sorted(normal_close_calls)
        )
        for descriptor in normal_descriptors.values():
            self.assertEqual(1, normal_close_calls.count(descriptor))
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_transaction_close_trace_retries_without_dropping_owner_slots(
        self,
    ) -> None:
        darwin = self.darwin
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'f' * 32}"
        stage.mkdir(mode=0o700)
        backend = darwin.DarwinBackend()
        baseline = self.open_fd_set()
        transaction = backend.bind_transaction(
            str(source),
            str(mirror),
            str(stage / "clone"),
            source_parent_expected=self.identity_for_directory(backend, source.parent),
        )
        owner_slots = {
            owner_attribute: getattr(transaction, owner_attribute)
            for owner_attribute in transaction._FD_OWNER_CLOSE_ORDER
        }
        active_fds = {
            owner_attribute: owner.fileno()
            for owner_attribute, owner in owner_slots.items()
            if not owner.closed
        }
        close_calls: List[int] = []
        real_close = darwin.os.close

        def record_close(descriptor: int) -> None:
            if descriptor in active_fds.values():
                close_calls.append(descriptor)
            real_close(descriptor)

        body_primary = RuntimeError("transaction-body-primary")
        cleanup_interrupt = KeyboardInterrupt("transaction-close-interrupt")
        previous_trace = sys.gettrace()
        evidence: Dict[str, Any] = {"fired": False}

        def raise_cleanup_interrupt() -> None:
            try:
                raise cleanup_interrupt
            except BaseException as error:
                evidence["cleanup_origin_traceback"] = error.__traceback__
                raise

        def tracer(frame: Any, event: str, _argument: Any) -> Any:
            owner_attribute = frame.f_locals.get("owner_attribute")
            owner = frame.f_locals.get("owner")
            caller = frame.f_back
            if (
                event == "line"
                and frame.f_code is darwin.BoundTransaction._close_owner_pass.__code__
                and not evidence["fired"]
                and isinstance(owner_attribute, str)
                and isinstance(owner, darwin._OwnedFD)
                and owner.closed
                and getattr(transaction, owner_attribute) is owner
                and owner_attribute in active_fds
                and close_calls == [active_fds[owner_attribute]]
                and caller is not None
                and caller.f_code is darwin.BoundTransaction.close.__code__
                and caller.f_locals.get("_cleanup_dispatch") == 1
                and "for owner_attribute in self._FD_OWNER_CLOSE_ORDER:"
                in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
                and any(
                    not owner_slots[remaining].closed
                    for remaining in transaction._FD_OWNER_CLOSE_ORDER
                    if remaining != owner_attribute
                )
            ):
                evidence["fired"] = True
                evidence["owner_attribute"] = owner_attribute
                evidence["owner"] = owner
                sys.settrace(None)
                raise_cleanup_interrupt()
            return tracer

        def close_with_body_primary() -> None:
            try:
                raise body_primary
            except RuntimeError as active_primary:
                evidence["body_origin_traceback"] = active_primary.__traceback__
                transaction.close(primary_error=active_primary)
                raise

        caught: Optional[BaseException] = None
        closed_before_fallback = False
        owner_states_before_fallback: tuple[bool, ...] = ()
        close_calls_before_fallback: tuple[int, ...] = ()
        with mock.patch.object(darwin.os, "close", side_effect=record_close):
            sys.settrace(tracer)
            try:
                close_with_body_primary()
            except BaseException as error:
                caught = error
            finally:
                sys.settrace(previous_trace)
            closed_before_fallback = transaction._closed
            owner_states_before_fallback = tuple(
                owner.closed for owner in owner_slots.values()
            )
            close_calls_before_fallback = tuple(close_calls)
            transaction.close()

        self.assertTrue(evidence["fired"])
        self.assertIs(body_primary, caught)
        traceback = caught.__traceback__ if caught is not None else None
        traceback_nodes: List[Any] = []
        while traceback is not None:
            traceback_nodes.append(traceback)
            traceback = traceback.tb_next
        self.assertIn(evidence["body_origin_traceback"], traceback_nodes)
        cleanup_traceback = cleanup_interrupt.__traceback__
        cleanup_nodes: List[Any] = []
        while cleanup_traceback is not None:
            cleanup_nodes.append(cleanup_traceback)
            cleanup_traceback = cleanup_traceback.tb_next
        self.assertIn(evidence["cleanup_origin_traceback"], cleanup_nodes)
        self.assertIn(
            "transaction-close-interrupt",
            getattr(body_primary, "cleanup_diagnostic", ""),
        )
        self.assertTrue(closed_before_fallback)
        self.assertTrue(all(owner_states_before_fallback))
        self.assertEqual(len(active_fds), len(close_calls_before_fallback))
        self.assertTrue(transaction._closed)
        for owner_attribute, owner in owner_slots.items():
            self.assertIs(owner, getattr(transaction, owner_attribute))
            self.assertTrue(owner.closed)
        self.assertIs(
            evidence["owner"],
            getattr(transaction, evidence["owner_attribute"]),
        )
        self.assertEqual(len(active_fds), len(set(active_fds.values())))
        for descriptor in active_fds.values():
            self.assertEqual(1, close_calls.count(descriptor))
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertTrue(stage.is_dir())
        self.assertEqual(baseline, self.open_fd_set())
        stage.rmdir()

    def test_owned_transaction_close_trace_retries_state_with_body_primary(
        self,
    ) -> None:
        darwin = self.darwin
        for index, layer in enumerate(("owner", "state"), start=940):
            with self.subTest(layer=layer):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                stage.mkdir(mode=0o700)
                backend = darwin.DarwinBackend()
                baseline = self.open_fd_set()
                transaction = backend.bind_transaction(
                    str(source),
                    str(mirror),
                    str(stage / "clone"),
                    source_parent_expected=self.identity_for_directory(
                        backend, source.parent
                    ),
                )
                owner = darwin._OwnedTransaction(
                    lambda target: target._adopt(transaction)
                )
                with owner:
                    self.assertIs(transaction, owner.transaction())
                owner_slots = {
                    owner_attribute: getattr(transaction, owner_attribute)
                    for owner_attribute in transaction._FD_OWNER_CLOSE_ORDER
                }
                active_fds = {
                    owner_attribute: fd_owner.fileno()
                    for owner_attribute, fd_owner in owner_slots.items()
                    if not fd_owner.closed
                }
                close_calls: List[int] = []
                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor in active_fds.values():
                        close_calls.append(descriptor)
                    real_close(descriptor)

                body_primary = RuntimeError(f"{layer}-body-primary")
                cleanup_interrupt = KeyboardInterrupt(f"{layer}-close-interrupt")
                previous_trace = sys.gettrace()
                evidence: Dict[str, Any] = {"fired": False}
                if layer == "owner":
                    target_code = darwin._TransactionState.close.__code__
                    expected_self = owner._state
                    caller_code = darwin._OwnedTransaction.close.__code__
                else:
                    target_code = darwin.BoundTransaction.close.__code__
                    expected_self = transaction
                    caller_code = darwin._TransactionState.close.__code__

                def raise_cleanup_interrupt() -> None:
                    raise cleanup_interrupt

                def tracer(frame: Any, event: str, _argument: Any) -> Any:
                    if (
                        event == "call"
                        and frame.f_code is target_code
                        and not evidence["fired"]
                        and frame.f_locals.get("self") is expected_self
                        and frame.f_locals.get("primary_error") is body_primary
                        and frame.f_back is not None
                        and frame.f_back.f_code is caller_code
                        and frame.f_back.f_locals.get("_attempt") == 0
                    ):
                        evidence["fired"] = True
                        sys.settrace(None)
                        raise_cleanup_interrupt()
                    return tracer

                def close_with_body_primary() -> None:
                    try:
                        raise body_primary
                    except RuntimeError as active_primary:
                        evidence["body_origin_traceback"] = active_primary.__traceback__
                        owner.close(primary_error=active_primary)
                        raise

                caught: Optional[BaseException] = None
                with mock.patch.object(darwin.os, "close", side_effect=record_close):
                    sys.settrace(tracer)
                    try:
                        close_with_body_primary()
                    except BaseException as error:
                        caught = error
                    finally:
                        sys.settrace(previous_trace)
                    owner.close()

                self.assertTrue(evidence["fired"])
                self.assertIs(body_primary, caught)
                traceback = caught.__traceback__ if caught is not None else None
                traceback_nodes: List[Any] = []
                while traceback is not None:
                    traceback_nodes.append(traceback)
                    traceback = traceback.tb_next
                self.assertIn(evidence["body_origin_traceback"], traceback_nodes)
                self.assertIn(
                    f"{layer}-close-interrupt",
                    getattr(body_primary, "cleanup_diagnostic", ""),
                )
                self.assertTrue(owner.closed)
                self.assertIsNone(owner._state.transaction)
                self.assertTrue(transaction._closed)
                for owner_attribute, fd_owner in owner_slots.items():
                    self.assertIs(fd_owner, getattr(transaction, owner_attribute))
                    self.assertTrue(fd_owner.closed)
                self.assertEqual(len(active_fds), len(set(active_fds.values())))
                for descriptor in active_fds.values():
                    self.assertEqual(1, close_calls.count(descriptor))
                self.assertEqual(baseline, self.open_fd_set())
                stage.rmdir()

    def test_legacy_fd_setter_trace_registration_closes_once(self) -> None:
        darwin = self.darwin
        for registered in (False, True):
            with self.subTest(registered=registered):
                backend = darwin.DarwinBackend()
                transaction = darwin.BoundTransaction.__new__(darwin.BoundTransaction)
                transaction._initialize(
                    backend,
                    str(self.root / "unused-source"),
                    str(self.root / "unused-destination"),
                    str(self.root / "unused-stage" / "clone"),
                )
                baseline = self.open_fd_set()
                raw_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                close_calls: List[int] = []
                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor == raw_fd:
                        close_calls.append(descriptor)
                    real_close(descriptor)

                def registration_boundary(frame: Any, evidence: Dict[str, Any]) -> bool:
                    replacement = frame.f_locals.get("replacement")
                    if (
                        frame.f_locals.get("instance") is not transaction
                        or frame.f_locals.get("value") != raw_fd
                        or not isinstance(replacement, darwin._OwnedFD)
                        or replacement.closed
                    ):
                        return False
                    is_registered = transaction._source_owner is replacement
                    if is_registered != registered:
                        return False
                    if not registered and replacement._retain_if_registered is None:
                        return False
                    evidence["replacement"] = replacement
                    evidence["slot_owner"] = transaction._source_owner
                    evidence["line_number"] = frame.f_lineno
                    return True

                def assign_with_cleanup() -> None:
                    try:
                        transaction.source_fd = raw_fd
                    finally:
                        transaction.close()

                with mock.patch.object(darwin.os, "close", side_effect=record_close):
                    evidence = self.assert_trace_interruption(
                        darwin._OwnedFDSlot.__set__.__code__,
                        registration_boundary,
                        assign_with_cleanup,
                        label=f"legacy fd slot registration {registered}",
                        events=(("return",) if registered else ("line",)),
                    )
                    evidence["replacement"].close()

                self.assertIsInstance(evidence["line_number"], int)
                self.assertEqual(registered, evidence["event"] == "return")
                self.assertEqual(
                    registered,
                    evidence["slot_owner"] is evidence["replacement"],
                )
                self.assertTrue(evidence["replacement"].closed)
                self.assertTrue(transaction._closed)
                self.assertEqual(1, close_calls.count(raw_fd))
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
            self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

        backend = darwin.DarwinBackend()
        transaction = darwin.BoundTransaction.__new__(darwin.BoundTransaction)
        transaction._initialize(
            backend,
            str(self.root / "unused-source-normal"),
            str(self.root / "unused-destination-normal"),
            str(self.root / "unused-stage-normal" / "clone"),
        )
        baseline = self.open_fd_set()
        raw_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        transaction.source_fd = raw_fd
        self.assertEqual(raw_fd, transaction.source_fd)
        self.assertFalse(transaction._source_owner.closed)
        transaction.close()
        self.assertTrue(transaction._source_owner.closed)
        self.assertEqual(-1, transaction.source_fd)
        self.assertEqual(baseline, self.open_fd_set())

    def test_legacy_fd_slots_trace_close_raw_before_owner_state_handoff(
        self,
    ) -> None:
        darwin = self.darwin
        slot_cases = (
            ("source_parent_fd", "_source_parent_owner"),
            ("destination_parent_fd", "_destination_parent_owner"),
            ("temporary_parent_fd", "_temporary_parent_owner"),
            ("source_fd", "_source_owner"),
            ("original_fd", "_original_owner"),
            ("clone_fd", "_clone_owner"),
        )
        boundaries = ("setter", "backend-adopt", "owner-store")

        def initialized_transaction(backend: Any, suffix: str) -> Any:
            transaction = darwin.BoundTransaction.__new__(darwin.BoundTransaction)
            transaction._initialize(
                backend,
                str(self.root / f"unused-source-{suffix}"),
                str(self.root / f"unused-destination-{suffix}"),
                str(self.root / f"unused-stage-{suffix}" / "clone"),
            )
            return transaction

        for slot_name, owner_attribute in slot_cases:
            for boundary in boundaries:
                with self.subTest(slot=slot_name, boundary=boundary):
                    backend = darwin.DarwinBackend()
                    transaction = initialized_transaction(
                        backend, f"{slot_name}-{boundary}"
                    )
                    original_owner = getattr(transaction, owner_attribute)
                    self.assertTrue(original_owner.closed)
                    baseline = self.open_fd_set()
                    raw_fd = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    raw_metadata = os.fstat(raw_fd)
                    raw_identity = (raw_metadata.st_dev, raw_metadata.st_ino)
                    close_calls: List[int] = []
                    close_identity_mismatches: List[Any] = []
                    real_close = darwin.os.close
                    evidence: Dict[str, Any] = {}

                    def record_close(descriptor: int) -> None:
                        if descriptor == raw_fd:
                            metadata = os.fstat(descriptor)
                            observed_identity = (metadata.st_dev, metadata.st_ino)
                            if observed_identity != raw_identity:
                                close_identity_mismatches.append(
                                    (raw_identity, observed_identity)
                                )
                            close_calls.append(descriptor)
                        real_close(descriptor)

                    def handoff_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
                        source = linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        if boundary == "setter":
                            if (
                                source != "_adoption_guard = 1"
                                or frame.f_locals.get("instance") is not transaction
                                or frame.f_locals.get("value") != raw_fd
                                or frame.f_locals.get("replacement") is not None
                                or frame.f_locals.get("current") is not original_owner
                                or frame.f_locals.get("pending_fd") != [raw_fd]
                            ):
                                return False
                            owner = None
                            pending_fd = frame.f_locals["pending_fd"]
                        elif boundary == "backend-adopt":
                            owner = frame.f_locals.get("owner")
                            caller = frame.f_back
                            receipt = frame.f_locals.get("receipt")
                            if (
                                source != "_adoption_guard = 1"
                                or frame.f_locals.get("self") is not backend
                                or frame.f_locals.get("fd") != raw_fd
                                or owner is not None
                                or receipt != [raw_fd]
                                or caller is None
                                or caller.f_code
                                is not darwin._OwnedFDSlot.__set__.__code__
                                or caller.f_locals.get("instance") is not transaction
                                or caller.f_locals.get("replacement") is not None
                                or caller.f_locals.get("pending_fd") is not receipt
                            ):
                                return False
                            pending_fd = receipt
                        else:
                            owner = frame.f_locals.get("self")
                            caller = frame.f_back
                            receipt = (
                                None
                                if caller is None
                                else caller.f_locals.get("receipt")
                            )
                            if (
                                source != "self._state.fd = fd"
                                or frame.f_locals.get("fd") != raw_fd
                                or not isinstance(owner, darwin._OwnedFD)
                                or not owner.closed
                                or owner._state.fd != -1
                                or caller is None
                                or caller.f_code
                                is not backend._adopt_fd.__func__.__code__
                                or caller.f_locals.get("owner") is not owner
                                or receipt != [raw_fd]
                            ):
                                return False
                            pending_fd = receipt
                        captured["transaction"] = transaction
                        captured["original_owner"] = original_owner
                        captured["provisional_owner"] = owner
                        captured["pending_fd"] = pending_fd
                        captured["raw_fd"] = raw_fd
                        captured["boundary_line"] = frame.f_lineno
                        return True

                    target_code = {
                        "setter": darwin._OwnedFDSlot.__set__.__code__,
                        "backend-adopt": backend._adopt_fd.__func__.__code__,
                        "owner-store": darwin._OwnedFD._adopt.__code__,
                    }[boundary]

                    def assign_with_cleanup() -> None:
                        try:
                            setattr(transaction, slot_name, raw_fd)
                        finally:
                            transaction.close()

                    raw_closed_before_fallback = False
                    close_calls_before_fallback: Tuple[int, ...] = ()
                    try:
                        with mock.patch.object(
                            darwin.os, "close", side_effect=record_close
                        ):
                            self.assert_trace_interruption(
                                target_code,
                                handoff_boundary,
                                assign_with_cleanup,
                                label=f"{slot_name} {boundary} raw handoff",
                                events=("line",),
                                evidence=evidence,
                            )
                            try:
                                fcntl.fcntl(raw_fd, fcntl.F_GETFD)
                            except OSError as error:
                                raw_closed_before_fallback = error.errno == errno.EBADF
                            close_calls_before_fallback = tuple(close_calls)
                    finally:
                        try:
                            fcntl.fcntl(raw_fd, fcntl.F_GETFD)
                        except OSError as error:
                            if error.errno != errno.EBADF:
                                raise
                        else:
                            real_close(raw_fd)

                    self.assertIsInstance(evidence["boundary_line"], int)
                    self.assertTrue(raw_closed_before_fallback)
                    self.assertEqual((raw_fd,), close_calls_before_fallback)
                    self.assertEqual([-1], evidence["pending_fd"])
                    self.assertEqual([], close_identity_mismatches)
                    self.assertIs(original_owner, getattr(transaction, owner_attribute))
                    self.assertTrue(original_owner.closed)
                    provisional_owner = evidence.get("provisional_owner")
                    if provisional_owner is not None:
                        self.assertTrue(provisional_owner.closed)
                    self.assertTrue(transaction._closed)
                    self.assertEqual(baseline, self.open_fd_set())

        for slot_name, owner_attribute in slot_cases:
            with self.subTest(slot=slot_name, boundary="normal"):
                backend = darwin.DarwinBackend()
                transaction = initialized_transaction(backend, f"{slot_name}-normal")
                original_owner = getattr(transaction, owner_attribute)
                baseline = self.open_fd_set()
                raw_fd = os.open(
                    os.devnull,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                close_calls: List[int] = []
                real_close = darwin.os.close

                def record_normal_close(descriptor: int) -> None:
                    if descriptor == raw_fd:
                        close_calls.append(descriptor)
                    real_close(descriptor)

                with mock.patch.object(
                    darwin.os, "close", side_effect=record_normal_close
                ):
                    setattr(transaction, slot_name, raw_fd)
                    installed_owner = getattr(transaction, owner_attribute)
                    self.assertIsNot(original_owner, installed_owner)
                    self.assertFalse(installed_owner.closed)
                    self.assertEqual(raw_fd, getattr(transaction, slot_name))
                    transaction.close()

                self.assertTrue(installed_owner.closed)
                self.assertTrue(transaction._closed)
                self.assertEqual([raw_fd], close_calls)
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(errno.EBADF, closed.exception.errno)
                self.assertEqual(baseline, self.open_fd_set())

    def test_bound_stage_container_normal_exit_trace_drains_retained_owner(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'f' * 32}"
        backend = self.darwin.DarwinBackend()
        baseline = self.open_fd_set()
        backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        stage_metadata = stage.stat()
        stage_identity = (stage_metadata.st_dev, stage_metadata.st_ino)
        transaction = backend.bind_transaction(
            str(source),
            str(mirror),
            str(stage / "clone"),
            source_parent_expected=self.identity_for_directory(backend, source.parent),
        )
        original_expectation = backend.snapshot_expectation(
            transaction.original_snapshot()
        )
        evidence: Dict[str, Any] = {}
        physical_closes: List[tuple[int, tuple[int, int]]] = []
        cleanup_calls: List[
            tuple[tuple[tuple[str, Any], ...], Optional[BaseException], bool]
        ] = []
        real_close = self.darwin.os.close
        real_close_owners = backend._close_fd_owners

        def record_close(descriptor: int) -> None:
            if descriptor == evidence.get("container_fd"):
                metadata = os.fstat(descriptor)
                physical_closes.append((descriptor, (metadata.st_dev, metadata.st_ino)))
            real_close(descriptor)

        def record_close_owners(
            descriptors: Any,
            *,
            primary_error: Optional[BaseException] = None,
            durable_namespace_complete: bool = False,
        ) -> None:
            owned = tuple(descriptors)
            if evidence.get("container_owner") is not None:
                cleanup_calls.append((owned, primary_error, durable_namespace_complete))
            real_close_owners(
                owned,
                primary_error=primary_error,
                durable_namespace_complete=durable_namespace_complete,
            )

        def retained_owner_boundary(frame: Any, captured: Dict[str, Any]) -> bool:
            source_line = linecache.getline(
                frame.f_code.co_filename, frame.f_lineno
            ).strip()
            owner = frame.f_locals.get("container_owner")
            container_fd = frame.f_locals.get("container_fd")
            if (
                source_line != "self.backend._require_stage_container_mapping("
                or frame.f_locals.get("self") is not transaction
                or not isinstance(owner, self.darwin._OwnedFD)
                or owner.closed
                or not owner._entered
                or container_fd != owner.fileno()
                or frame.f_locals.get("container_primary_error") is not None
                or frame.f_locals.get("stage_path") != str(stage)
            ):
                return False
            metadata = os.fstat(container_fd)
            captured["container_owner"] = owner
            captured["container_fd"] = container_fd
            captured["container_identity"] = (metadata.st_dev, metadata.st_ino)
            captured["boundary_line"] = frame.f_lineno
            return True

        owner_closed_before_fallback = False
        stage_identity_before_fallback: Optional[tuple[int, int]] = None
        stage_entries_before_fallback: Optional[tuple[str, ...]] = None
        try:
            with (
                mock.patch.object(self.darwin.os, "close", side_effect=record_close),
                mock.patch.object(
                    backend, "_close_fd_owners", side_effect=record_close_owners
                ),
            ):
                self.assert_trace_interruption(
                    self.darwin.BoundTransaction.remove_empty_stage_parent.__code__,
                    retained_owner_boundary,
                    lambda: transaction.abort_before_prepared(
                        original_expectation,
                        authorize_state=self.allow_state_mutation,
                    ),
                    label="bound retained stage-container owner",
                    events=("line",),
                    evidence=evidence,
                )
                owner_closed_before_fallback = evidence["container_owner"].closed
                current_stage = stage.stat()
                stage_identity_before_fallback = (
                    current_stage.st_dev,
                    current_stage.st_ino,
                )
                stage_entries_before_fallback = tuple(
                    sorted(entry.name for entry in stage.iterdir())
                )
        finally:
            transaction.close(primary_error=evidence.get("primary"))
            if stage.exists():
                stage.rmdir()

        owner = evidence["container_owner"]
        descriptor = evidence["container_fd"]
        self.assertIsInstance(evidence["boundary_line"], int)
        self.assertTrue(owner_closed_before_fallback)
        self.assertEqual(stage_identity, stage_identity_before_fallback)
        self.assertEqual((), stage_entries_before_fallback)
        self.assertFalse(transaction._stage_removed)
        self.assertFalse(transaction._namespace_lifecycle_complete)
        self.assertEqual(
            [(descriptor, evidence["container_identity"])], physical_closes
        )
        self.assertEqual(1, len(cleanup_calls))
        self.assertEqual((("stage container", owner),), cleanup_calls[0][0])
        self.assertIs(evidence["primary"], cleanup_calls[0][1])
        self.assertFalse(cleanup_calls[0][2])
        with self.assertRaises(OSError) as closed:
            fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_fd_alias_rejection_preserves_the_original_owner(self) -> None:
        darwin = self.darwin
        for route in ("legacy-setter", "owned-install"):
            with self.subTest(route=route):
                backend = darwin.DarwinBackend()
                transaction = darwin.BoundTransaction.__new__(darwin.BoundTransaction)
                transaction._initialize(
                    backend,
                    str(self.root / f"alias-source-{route}"),
                    str(self.root / f"alias-destination-{route}"),
                    str(self.root / f"alias-stage-{route}" / "clone"),
                )
                baseline = self.open_fd_set()
                raw_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                close_calls: List[int] = []
                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor == raw_fd:
                        close_calls.append(descriptor)
                    real_close(descriptor)

                with mock.patch.object(darwin.os, "close", side_effect=record_close):
                    if route == "legacy-setter":
                        transaction.source_fd = raw_fd
                        source_owner = transaction._source_owner
                        with self.assertRaises(darwin.BackendError) as rejected:
                            transaction.original_fd = raw_fd
                        duplicate_owner = transaction._original_owner
                    else:
                        source_owner = backend._adopt_fd(raw_fd, "alias source")
                        transaction._install_fd_owner("_source_owner", source_owner)
                        duplicate_owner = backend._adopt_fd(raw_fd, "alias duplicate")
                        with self.assertRaises(darwin.BackendError) as rejected:
                            transaction._install_fd_owner(
                                "_original_owner", duplicate_owner
                            )
                    self.assertEqual("fd_alias_rejected", rejected.exception.reason)
                    self.assertIs(source_owner, transaction._source_owner)
                    self.assertFalse(source_owner.closed)
                    self.assertEqual(raw_fd, source_owner.fileno())
                    self.assertTrue(duplicate_owner.closed)
                    os.fstat(raw_fd)
                    transaction.close()

                self.assertTrue(transaction._closed)
                self.assertTrue(source_owner.closed)
                self.assertEqual([raw_fd], close_calls)
                with self.assertRaises(OSError) as closed:
                    os.fstat(raw_fd)
                self.assertEqual(errno.EBADF, closed.exception.errno)
                self.assertEqual(baseline, self.open_fd_set())

    def test_fd_alias_trace_before_disarm_neutralizes_duplicate(self) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        transaction = darwin.BoundTransaction.__new__(darwin.BoundTransaction)
        transaction._initialize(
            backend,
            str(self.root / "alias-trace-source"),
            str(self.root / "alias-trace-destination"),
            str(self.root / "alias-trace-stage" / "clone"),
        )
        baseline = self.open_fd_set()
        raw_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        source_owner = backend._adopt_fd(raw_fd, "alias trace source")
        transaction._install_fd_owner("_source_owner", source_owner)
        duplicate = backend._adopt_fd(raw_fd, "alias trace duplicate")
        close_calls: List[int] = []
        real_close = darwin.os.close

        def record_close(descriptor: int) -> None:
            if descriptor == raw_fd:
                close_calls.append(descriptor)
            real_close(descriptor)

        def alias_before_disarm(frame: Any, evidence: Dict[str, Any]) -> bool:
            if (
                frame.f_locals.get("self") is not transaction
                or frame.f_locals.get("owner") is not duplicate
                or frame.f_locals.get("owner_attribute") != "_original_owner"
                or frame.f_locals.get("alias_attribute") != "_source_owner"
                or duplicate.closed
                or duplicate._neutralize_on_exception_if is None
                or transaction._source_owner is not source_owner
                or not transaction._original_owner.closed
            ):
                return False
            evidence["source_owner"] = source_owner
            evidence["duplicate"] = duplicate
            evidence["line_number"] = frame.f_lineno
            return True

        def install_duplicate() -> None:
            with duplicate:
                transaction._install_fd_owner("_original_owner", duplicate)

        close_calls_before_final_drain: tuple[int, ...] = ()
        with mock.patch.object(darwin.os, "close", side_effect=record_close):
            try:
                evidence = self.assert_trace_interruption(
                    darwin.BoundTransaction._install_fd_owner.__code__,
                    alias_before_disarm,
                    install_duplicate,
                    label="same-number alias neutralization",
                    events=("line",),
                )
                close_calls_before_final_drain = tuple(close_calls)
                self.assertTrue(duplicate.closed)
                self.assertEqual(-1, duplicate._state.fd)
                self.assertIs(source_owner, transaction._source_owner)
                self.assertFalse(source_owner.closed)
                self.assertTrue(transaction._original_owner.closed)
                os.fstat(raw_fd)
            finally:
                duplicate.close()
                transaction.close()

        self.assertIsInstance(evidence["line_number"], int)
        self.assertEqual((), close_calls_before_final_drain)
        self.assertEqual([raw_fd], close_calls)
        self.assertTrue(source_owner.closed)
        self.assertTrue(transaction._closed)
        with self.assertRaises(OSError) as closed:
            os.fstat(raw_fd)
        self.assertEqual(errno.EBADF, closed.exception.errno)
        self.assertEqual(baseline, self.open_fd_set())

    def test_empty_acl_raw_handoff_trace_frees_pointer_once(self) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        raw_address = 0xA11CE
        free_calls: List[int] = []
        probe = backend._empty_acl_owned()
        assert probe._acquire is not None

        def record_free(pointer: Any) -> int:
            free_calls.append(pointer.value)
            return 0

        def raw_pointer_handoff(frame: Any, evidence: Dict[str, Any]) -> bool:
            owner = frame.f_locals.get("target")
            if (
                not isinstance(owner, darwin._OwnedACL)
                or not owner.closed
                or frame.f_locals.get("pointer") != raw_address
            ):
                return False
            evidence["owner"] = owner
            evidence["line_number"] = frame.f_lineno
            return True

        closed_before_fallback = False
        with (
            mock.patch.object(backend, "_acl_init", return_value=raw_address),
            mock.patch.object(backend, "_acl_free", side_effect=record_free),
        ):
            evidence: Dict[str, Any] = {}
            try:
                evidence = self.assert_trace_interruption(
                    probe._acquire.__code__,
                    raw_pointer_handoff,
                    backend._empty_acl,
                    label="empty ACL raw handoff",
                    events=("line",),
                )
                closed_before_fallback = evidence["owner"].closed
            finally:
                owner = evidence.get("owner")
                if owner is not None:
                    owner.close(primary_error=evidence.get("primary"))

        self.assertIsInstance(evidence["line_number"], int)
        self.assertTrue(closed_before_fallback)
        self.assertEqual([raw_address], free_calls)

    def test_backend_owner_cleanup_handlers_retry_once(self) -> None:
        darwin = self.darwin
        for owner_kind in ("fd", "acl", "transaction", "stage"):
            for lifecycle in ("enter", "exit"):
                with self.subTest(owner=owner_kind, lifecycle=lifecycle):
                    backend = darwin.DarwinBackend()
                    baseline = self.open_fd_set()
                    body_primary = RuntimeError(
                        f"{owner_kind} {lifecycle} body primary"
                    )
                    evidence: Dict[str, Any] = {}
                    raw_fds: List[int] = []
                    close_calls: List[int] = []
                    acl_free_calls: List[int] = []
                    stage_cleanup_calls: List[str] = []
                    transaction: Optional[Any] = None
                    raw_pointer = 0xA1100 + len(owner_kind) * 10 + len(lifecycle)

                    def raise_body_primary() -> None:
                        try:
                            raise body_primary
                        except BaseException as error:
                            evidence["body_origin_traceback"] = error.__traceback__
                            raise

                    if owner_kind == "fd":
                        raw_fd = os.open(
                            os.devnull,
                            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                        )
                        raw_fds.append(raw_fd)

                        def acquire(target: Any) -> None:
                            target._adopt(raw_fd)
                            if lifecycle == "enter":
                                raise_body_primary()

                        owner = darwin._OwnedFD(backend, "FD lifecycle trace", acquire)
                    elif owner_kind == "acl":
                        pointer = darwin.ctypes.c_void_p(raw_pointer)

                        def acquire(target: Any) -> None:
                            target._adopt(pointer)
                            if lifecycle == "enter":
                                raise_body_primary()

                        owner = darwin._OwnedACL(
                            backend, "ACL lifecycle trace", acquire
                        )
                    elif owner_kind == "transaction":
                        transaction = darwin.BoundTransaction.__new__(
                            darwin.BoundTransaction
                        )
                        transaction._initialize(
                            backend,
                            str(self.root / "owner-trace-source"),
                            str(self.root / "owner-trace-destination"),
                            str(self.root / "owner-trace-stage" / "clone"),
                        )
                        raw_fd = os.open(
                            os.devnull,
                            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                        )
                        raw_fds.append(raw_fd)
                        transaction._install_fd_owner(
                            "_source_owner",
                            backend._adopt_fd(raw_fd, "transaction lifecycle"),
                        )

                        def acquire(target: Any) -> None:
                            assert transaction is not None
                            target._adopt(transaction)
                            if lifecycle == "enter":
                                raise_body_primary()

                        owner = darwin._OwnedTransaction(acquire)
                    else:
                        raw_fd = os.open(
                            os.devnull,
                            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                        )
                        raw_fds.append(raw_fd)

                        def acquire(target: Any) -> None:
                            target._adopt(raw_fd)
                            if lifecycle == "enter":
                                raise_body_primary()

                        owner = darwin._OwnedStageFD(
                            backend, "stage lifecycle trace", acquire
                        )

                        def cleanup_stage() -> None:
                            stage_cleanup_calls.append("cleanup")
                            owner._mark_namespace_cleanup_attempted()

                        owner._configure_namespace_cleanup(cleanup_stage)
                        owner._mark_namespace_created()

                    handler_name = (
                        "_handle_acquisition_failure"
                        if lifecycle == "enter"
                        else "_handle_exception"
                    )
                    target_code = getattr(type(owner), handler_name).__code__
                    real_close = darwin.os.close

                    def record_close(descriptor: int) -> None:
                        if descriptor in raw_fds:
                            close_calls.append(descriptor)
                        real_close(descriptor)

                    def record_acl_free(pointer: Any) -> int:
                        acl_free_calls.append(pointer.value)
                        return 0

                    def first_handler_dispatch(
                        frame: Any, _captured: Dict[str, Any]
                    ) -> bool:
                        caller = frame.f_back
                        return (
                            frame.f_locals.get("self") is owner
                            and frame.f_locals.get("primary_error") is body_primary
                            and caller is not None
                            and (
                                caller.f_locals.get("_handler_attempt") == 1
                                or caller.f_locals.get("handler_attempt") == 1
                            )
                            and not owner.closed
                        )

                    def operation() -> None:
                        if lifecycle == "enter":
                            owner.__enter__()
                        else:
                            with owner:
                                raise_body_primary()

                    closed_before_fallback = False
                    with (
                        mock.patch.object(darwin.os, "close", side_effect=record_close),
                        mock.patch.object(
                            backend, "_acl_free", side_effect=record_acl_free
                        ),
                    ):
                        try:
                            self.assert_cleanup_trace_preserves_primary(
                                target_code,
                                first_handler_dispatch,
                                operation,
                                body_primary,
                                label=f"backend {owner_kind} {lifecycle}",
                                evidence=evidence,
                            )
                            closed_before_fallback = owner.closed
                        finally:
                            owner.close(primary_error=body_primary)

                    self.assertTrue(closed_before_fallback)
                    self.assertIn(
                        str(evidence["cleanup_interrupt"]),
                        getattr(body_primary, "cleanup_diagnostic", ""),
                    )
                    for descriptor in raw_fds:
                        self.assertEqual(1, close_calls.count(descriptor))
                        with self.assertRaises(OSError) as closed:
                            os.fstat(descriptor)
                        self.assertEqual(errno.EBADF, closed.exception.errno)
                    if owner_kind == "acl":
                        self.assertEqual([raw_pointer], acl_free_calls)
                    if owner_kind == "transaction":
                        assert transaction is not None
                        self.assertIsNone(owner._state.transaction)
                        self.assertTrue(transaction._closed)
                    if owner_kind == "stage":
                        self.assertEqual(["cleanup"], stage_cleanup_calls)
                        self.assertTrue(owner._namespace_cleanup_attempted)
                        self.assertTrue(owner._namespace_cleanup_complete)
                    self.assertEqual(baseline, self.open_fd_set())

    def test_backend_destructors_retry_first_cleanup_interruption(self) -> None:
        darwin = self.darwin
        for resource_kind in ("fd-state", "acl-state", "transaction-state", "bound"):
            with self.subTest(resource=resource_kind):
                backend = darwin.DarwinBackend()
                baseline = self.open_fd_set()
                raw_fds: List[int] = []
                close_calls: List[int] = []
                acl_free_calls: List[int] = []
                raw_pointer = 0xD3100 + len(resource_kind)
                transaction: Optional[Any] = None

                if resource_kind == "fd-state":
                    raw_fd = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    raw_fds.append(raw_fd)
                    resource = darwin._FDState(backend, "destructor FD state", raw_fd)
                    close_code = darwin._FDState.close.__code__
                    destructor_code = darwin._FDState.__del__.__code__
                elif resource_kind == "acl-state":
                    resource = darwin._ACLState(
                        backend,
                        "destructor ACL state",
                        darwin.ctypes.c_void_p(raw_pointer),
                        True,
                    )
                    close_code = darwin._ACLState.close.__code__
                    destructor_code = darwin._ACLState.__del__.__code__
                else:
                    transaction = darwin.BoundTransaction.__new__(
                        darwin.BoundTransaction
                    )
                    transaction._initialize(
                        backend,
                        str(self.root / f"{resource_kind}-source"),
                        str(self.root / f"{resource_kind}-destination"),
                        str(self.root / f"{resource_kind}-stage" / "clone"),
                    )
                    raw_fd = os.open(
                        os.devnull,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    raw_fds.append(raw_fd)
                    transaction._install_fd_owner(
                        "_source_owner",
                        backend._adopt_fd(raw_fd, resource_kind),
                    )
                    if resource_kind == "transaction-state":
                        resource = darwin._TransactionState(transaction)
                        close_code = darwin._TransactionState.close.__code__
                        destructor_code = darwin._TransactionState.__del__.__code__
                    else:
                        resource = transaction
                        close_code = darwin.BoundTransaction.close.__code__
                        destructor_code = darwin.BoundTransaction.__del__.__code__

                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor in raw_fds:
                        close_calls.append(descriptor)
                    real_close(descriptor)

                def record_acl_free(pointer: Any) -> int:
                    acl_free_calls.append(pointer.value)
                    return 0

                cleanup_interrupt = KeyboardInterrupt(
                    f"{resource_kind} destructor cleanup"
                )
                previous_trace = sys.gettrace()
                evidence: Dict[str, Any] = {"fired": False}

                def tracer(frame: Any, event: str, _argument: Any) -> Any:
                    caller = frame.f_back
                    if (
                        event == "call"
                        and frame.f_code is close_code
                        and not evidence["fired"]
                        and frame.f_locals.get("self") is resource
                        and caller is not None
                        and caller.f_code is destructor_code
                        and (
                            caller.f_locals.get("_cleanup_attempt") == 1
                            or caller.f_locals.get("cleanup_attempt") == 1
                        )
                    ):
                        evidence["fired"] = True
                        sys.settrace(None)
                        raise cleanup_interrupt
                    return tracer

                closed_before_fallback = False
                with (
                    mock.patch.object(darwin.os, "close", side_effect=record_close),
                    mock.patch.object(
                        backend, "_acl_free", side_effect=record_acl_free
                    ),
                ):
                    sys.settrace(tracer)
                    try:
                        resource.__del__()
                    finally:
                        sys.settrace(previous_trace)
                    if resource_kind == "fd-state":
                        closed_before_fallback = resource.fd < 0
                    elif resource_kind == "acl-state":
                        closed_before_fallback = not resource.active
                    elif resource_kind == "transaction-state":
                        closed_before_fallback = (
                            resource.transaction is None
                            and transaction is not None
                            and transaction._closed
                        )
                    else:
                        closed_before_fallback = resource._closed
                    if not closed_before_fallback:
                        resource.close()

                self.assertTrue(evidence["fired"])
                self.assertTrue(closed_before_fallback)
                for descriptor in raw_fds:
                    self.assertEqual(1, close_calls.count(descriptor))
                    with self.assertRaises(OSError) as closed:
                        os.fstat(descriptor)
                    self.assertEqual(errno.EBADF, closed.exception.errno)
                if resource_kind == "acl-state":
                    self.assertEqual([raw_pointer], acl_free_calls)
                if transaction is not None:
                    self.assertTrue(transaction._closed)
                self.assertEqual(baseline, self.open_fd_set())

    def test_stage_namespace_cleanup_retries_after_recorded_attempt_trace(
        self,
    ) -> None:
        darwin = self.darwin
        for index, route in enumerate(("native", "override"), start=930):
            with self.subTest(route=route):
                actions: List[str] = []

                def authorize(action: str) -> None:
                    actions.append(action)

                if route == "native":
                    backend = darwin.DarwinBackend()
                else:

                    class OverrideBackend(darwin.DarwinBackend):
                        def create_private_stage_parent(
                            self,
                            parent_fd: int,
                            name: str,
                            *,
                            authorize_state: Any,
                        ) -> Any:
                            return super().create_private_stage_parent(
                                parent_fd,
                                name,
                                authorize_state=authorize_state,
                            )

                    backend = OverrideBackend()

                stage = self.root / f".codex-reflink-repair-{index:032x}"
                baseline = self.open_fd_set()
                parent_fd = backend.open_absolute_dir(str(stage.parent))
                container_identity = backend.validate_stage_container(parent_fd)
                owner = backend._create_private_stage_parent_owned(
                    parent_fd,
                    stage.name,
                    authorize_state=authorize,
                )
                body_primary = RuntimeError(f"{route} stage cleanup body primary")
                evidence: Dict[str, Any] = {}
                close_calls: List[int] = []
                owner.__enter__()
                stage_identity = owner.identity()
                stage_fd = owner.fileno()
                stage_fd_identity = os.fstat(stage_fd)
                expected_fd_identity = (
                    stage_fd_identity.st_dev,
                    stage_fd_identity.st_ino,
                )
                cleanup = owner._namespace_cleanup
                self.assertIsNotNone(cleanup)
                assert cleanup is not None
                real_close = darwin.os.close

                def record_close(descriptor: int) -> None:
                    if descriptor == stage_fd:
                        metadata = os.fstat(descriptor)
                        self.assertEqual(
                            expected_fd_identity,
                            (metadata.st_dev, metadata.st_ino),
                        )
                        close_calls.append(descriptor)
                    real_close(descriptor)

                def raise_body_primary() -> None:
                    try:
                        raise body_primary
                    except BaseException as error:
                        evidence["body_origin_traceback"] = error.__traceback__
                        raise

                def operation() -> None:
                    try:
                        raise_body_primary()
                    except BaseException as error:
                        owner.__exit__(type(error), error, error.__traceback__)
                        raise

                def identity_cleanup_boundary(
                    frame: Any, captured: Dict[str, Any]
                ) -> bool:
                    caller = frame.f_back
                    source_line = linecache.getline(
                        frame.f_code.co_filename, frame.f_lineno
                    ).strip()
                    if (
                        caller is None
                        or caller.f_code
                        is not darwin._OwnedStageFD._cleanup_created_namespace.__code__
                        or caller.f_locals.get("self") is not owner
                        or caller.f_locals.get("primary_error") is not body_primary
                        or caller.f_locals.get("_attempt") != 0
                        or frame.f_locals.get("target") is not owner
                        or frame.f_locals.get("name") != stage.name
                        or not owner._namespace_cleanup_attempted
                        or owner._namespace_cleanup_complete
                        or not stage.exists()
                        or "return self._cleanup_unopened_stage(" not in source_line
                    ):
                        return False
                    captured["cleanup_line"] = frame.f_lineno
                    captured["cleanup_source"] = source_line
                    return True

                closed_before_fallback = False
                removed_before_fallback = False
                actions_before_fallback: tuple[str, ...] = ()
                close_calls_before_fallback: tuple[int, ...] = ()
                try:
                    with mock.patch.object(
                        darwin.os, "close", side_effect=record_close
                    ):
                        try:
                            self.assert_cleanup_trace_preserves_primary(
                                cleanup.__code__,
                                identity_cleanup_boundary,
                                operation,
                                body_primary,
                                label=f"{route} stage identity cleanup",
                                events=("line",),
                                evidence=evidence,
                            )
                            closed_before_fallback = owner.closed
                            removed_before_fallback = not stage.exists()
                            actions_before_fallback = tuple(actions)
                            close_calls_before_fallback = tuple(close_calls)
                        finally:
                            owner.close(primary_error=body_primary)
                finally:
                    if stage.exists():
                        backend.remove_empty_private_stage(
                            str(stage),
                            stage_identity,
                            expected_container=container_identity,
                            authorize_state=authorize,
                        )
                    os.close(parent_fd)

                self.assertTrue(closed_before_fallback)
                self.assertTrue(removed_before_fallback)
                self.assertEqual(
                    ("create_stage", "remove_stage"), actions_before_fallback
                )
                self.assertEqual((stage_fd,), close_calls_before_fallback)
                self.assertTrue(owner._namespace_cleanup_attempted)
                self.assertTrue(owner._namespace_cleanup_complete)
                self.assertIn(
                    str(evidence["cleanup_interrupt"]),
                    getattr(body_primary, "cleanup_diagnostic", ""),
                )
                self.assertIn(
                    "return self._cleanup_unopened_stage(",
                    evidence["cleanup_source"],
                )
                self.assertFalse(stage.exists())
                self.assertEqual(baseline, self.open_fd_set())

    def test_direct_stage_cleanup_owner_pass_trace_drains_all_owners(self) -> None:
        darwin = self.darwin
        case_number = 950
        for route in ("remove", "intent"):
            for boundary in ("between-owners", "postprocess"):
                with self.subTest(route=route, boundary=boundary):
                    backend = darwin.DarwinBackend()
                    if route == "intent":
                        mirror = self.write_rollout(
                            self.mirror_root,
                            "archived_sessions",
                            UUID_A,
                            f"intent owner pass {case_number}\n".encode(),
                        )
                        stage_parent = mirror.parent
                        original_expectation = self.expectation_for_path(
                            backend, mirror
                        )
                    else:
                        mirror = None
                        stage_parent = self.root
                        original_expectation = None
                    stage = stage_parent / (f".codex-reflink-repair-{case_number:032x}")
                    case_number += 1
                    container_identity = self.identity_for_directory(
                        backend, stage.parent
                    )
                    stage_identity = backend.create_private_stage(
                        str(stage), authorize_state=self.allow_state_mutation
                    )
                    baseline = self.open_fd_set()
                    body_primary = KeyboardInterrupt(f"{route} {boundary} body primary")
                    evidence: Dict[str, Any] = {}
                    state_close_calls: List[tuple[Any, int, tuple[int, int]]] = []
                    physical_close_calls: List[tuple[Any, int, tuple[int, int]]] = []
                    active_state: Optional[Any] = None
                    real_state_close = darwin._FDState.close
                    real_close = darwin.os.close

                    def recording_state_close(
                        state: Any, *arguments: Any, **keywords: Any
                    ) -> Any:
                        nonlocal active_state
                        descriptor = state.fd
                        if descriptor < 0:
                            return real_state_close(state, *arguments, **keywords)
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        state_close_calls.append((state, descriptor, identity))
                        previous_state = active_state
                        active_state = state
                        try:
                            return real_state_close(state, *arguments, **keywords)
                        finally:
                            active_state = previous_state

                    def recording_close(descriptor: int) -> None:
                        if active_state is not None:
                            metadata = os.fstat(descriptor)
                            physical_close_calls.append(
                                (
                                    active_state,
                                    descriptor,
                                    (metadata.st_dev, metadata.st_ino),
                                )
                            )
                        real_close(descriptor)

                    def raise_body_primary() -> None:
                        try:
                            raise body_primary
                        except BaseException as error:
                            evidence["body_origin_traceback"] = error.__traceback__
                            raise

                    def fail_stage_validation(_descriptor: int) -> Any:
                        raise_body_primary()

                    def operation() -> None:
                        with mock.patch.object(
                            backend,
                            "validate_private_stage_parent",
                            side_effect=fail_stage_validation,
                        ):
                            if route == "remove":
                                backend.remove_empty_private_stage(
                                    str(stage),
                                    stage_identity,
                                    expected_container=container_identity,
                                    authorize_state=self.allow_state_mutation,
                                )
                            else:
                                assert mirror is not None
                                assert original_expectation is not None
                                backend.cleanup_intent_stage(
                                    str(stage),
                                    final_path=str(mirror),
                                    expected_container=container_identity,
                                    expected_original=original_expectation,
                                    expected_stage=stage_identity,
                                    allow_clone=False,
                                    expected_clone=None,
                                    expected_snapshot=None,
                                    expected_size=None,
                                    expected_sha256=None,
                                    authorize_state=self.allow_state_mutation,
                                )

                    def owner_pass_boundary(
                        frame: Any, captured: Dict[str, Any]
                    ) -> bool:
                        caller = frame.f_back
                        descriptors = frame.f_locals.get("descriptors")
                        source_line = linecache.getline(
                            frame.f_code.co_filename, frame.f_lineno
                        ).strip()
                        if frame.f_locals.get(
                            "primary_error"
                        ) is not body_primary or not isinstance(descriptors, tuple):
                            return False
                        owners = tuple(
                            owner
                            for _subject, owner in descriptors
                            if owner is not None
                        )
                        if len(owners) != 2:
                            return False
                        if boundary == "between-owners":
                            if (
                                caller is None
                                or caller.f_code
                                is not darwin.DarwinBackend._close_fd_owners.__code__
                                or caller.f_locals.get("_cleanup_dispatch") != 1
                                or source_line != "if owner is None:"
                                or frame.f_locals.get("owner") is not owners[1]
                                or not owners[0].closed
                                or owners[1].closed
                            ):
                                return False
                        elif (
                            frame.f_code
                            is not darwin.DarwinBackend._close_fd_owners.__code__
                            or frame.f_locals.get("_cleanup_dispatch") != 4
                            or "close_failure: Optional[BackendError] = None"
                            not in source_line
                            or frame.f_locals.get("first_interruption") is not None
                            or frame.f_locals.get("first_traceback") is not None
                            or frame.f_locals.get("close_errors") != []
                            or not all(owner.closed for owner in owners)
                        ):
                            return False
                        owner_states = tuple(owner._state for owner in owners)
                        closed_states = tuple(
                            state
                            for state, _descriptor, _identity in state_close_calls
                            if state in owner_states
                        )
                        if boundary == "between-owners" and len(closed_states) != 1:
                            return False
                        captured["owners"] = owners
                        captured["owner_states"] = owner_states
                        captured["descriptors"] = descriptors
                        captured["pass_line"] = frame.f_lineno
                        captured["pass_source"] = source_line
                        return True

                    closed_before_fallback: tuple[bool, ...] = ()
                    state_calls_before_fallback: tuple[
                        tuple[Any, int, tuple[int, int]], ...
                    ] = ()
                    physical_calls_before_fallback: tuple[
                        tuple[Any, int, tuple[int, int]], ...
                    ] = ()
                    try:
                        with (
                            mock.patch.object(
                                darwin._FDState,
                                "close",
                                autospec=True,
                                side_effect=recording_state_close,
                            ),
                            mock.patch.object(
                                darwin.os, "close", side_effect=recording_close
                            ),
                        ):
                            try:
                                self.assert_cleanup_trace_preserves_primary(
                                    (
                                        darwin.DarwinBackend._close_fd_owner_pass.__code__
                                        if boundary == "between-owners"
                                        else darwin.DarwinBackend._close_fd_owners.__code__
                                    ),
                                    owner_pass_boundary,
                                    operation,
                                    body_primary,
                                    label=f"{route} {boundary} owner pass",
                                    events=("line",),
                                    evidence=evidence,
                                )
                                closed_before_fallback = tuple(
                                    owner.closed for owner in evidence["owners"]
                                )
                                state_calls_before_fallback = tuple(state_close_calls)
                                physical_calls_before_fallback = tuple(
                                    physical_close_calls
                                )
                            finally:
                                for owner in evidence.get("owners", ()):
                                    owner.close(primary_error=body_primary)
                    finally:
                        if stage.exists():
                            if route == "remove":
                                backend.remove_empty_private_stage(
                                    str(stage),
                                    stage_identity,
                                    expected_container=container_identity,
                                    authorize_state=self.allow_state_mutation,
                                )
                            else:
                                assert mirror is not None
                                assert original_expectation is not None
                                backend.cleanup_intent_stage(
                                    str(stage),
                                    final_path=str(mirror),
                                    expected_container=container_identity,
                                    expected_original=original_expectation,
                                    expected_stage=stage_identity,
                                    allow_clone=False,
                                    expected_clone=None,
                                    expected_snapshot=None,
                                    expected_size=None,
                                    expected_sha256=None,
                                    authorize_state=self.allow_state_mutation,
                                )

                    self.assertEqual((True, True), closed_before_fallback)
                    for state in evidence["owner_states"]:
                        state_calls = [
                            (descriptor, identity)
                            for candidate, descriptor, identity in state_calls_before_fallback
                            if candidate is state
                        ]
                        physical_calls = [
                            (descriptor, identity)
                            for candidate, descriptor, identity in physical_calls_before_fallback
                            if candidate is state
                        ]
                        self.assertEqual(1, len(state_calls))
                        self.assertEqual(state_calls, physical_calls)
                    self.assertIn(
                        str(evidence["cleanup_interrupt"]),
                        getattr(body_primary, "cleanup_diagnostic", ""),
                    )
                    self.assertEqual(
                        "if owner is None:"
                        if boundary == "between-owners"
                        else "close_failure: Optional[BackendError] = None",
                        evidence["pass_source"],
                    )
                    self.assertFalse(stage.exists())
                    self.assertEqual(baseline, self.open_fd_set())

    def test_create_private_stage_return_cleanup_trace_removes_orphan(
        self,
    ) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        stage = self.root / f".codex-reflink-repair-{'e' * 32}"
        baseline = self.open_fd_set()
        actions: List[str] = []
        close_calls: List[int] = []
        boundary_fds: Dict[int, tuple[int, int]] = {}
        boundary_active = False
        real_close = darwin.os.close

        def authorize(action: str) -> None:
            actions.append(action)

        def record_close(descriptor: int) -> None:
            if boundary_active and descriptor in boundary_fds:
                metadata = os.fstat(descriptor)
                self.assertEqual(
                    boundary_fds[descriptor],
                    (metadata.st_dev, metadata.st_ino),
                )
                close_calls.append(descriptor)
            real_close(descriptor)

        def final_cleanup_entry(frame: Any, evidence: Dict[str, Any]) -> bool:
            nonlocal boundary_active
            caller = frame.f_back
            if (
                caller is None
                or caller.f_code is not backend.create_private_stage.__func__.__code__
                or caller.f_locals.get("_cleanup_dispatch") != 1
                or frame.f_locals.get("primary_error") is not None
                or not frame.f_locals.get("durable_namespace_complete", False)
            ):
                return False
            stage_owner = caller.f_locals.get("stage_owner")
            parent_owner = caller.f_locals.get("parent_owner")
            identity = caller.f_locals.get("identity")
            container_identity = caller.f_locals.get("container_identity")
            if (
                not isinstance(stage_owner, darwin._OwnedStageFD)
                or not isinstance(parent_owner, darwin._OwnedFD)
                or stage_owner.closed
                or parent_owner.closed
                or not isinstance(identity, darwin.FileIdentity)
                or not isinstance(container_identity, darwin.FileIdentity)
            ):
                return False
            owners = (stage_owner, parent_owner)
            evidence["owners"] = owners
            evidence["identity"] = identity
            evidence["container_identity"] = container_identity
            for owner in owners:
                descriptor = owner.fileno()
                metadata = os.fstat(descriptor)
                boundary_fds[descriptor] = (
                    metadata.st_dev,
                    metadata.st_ino,
                )
            evidence["fds"] = tuple(boundary_fds)
            boundary_active = True
            return True

        immediate_closed: tuple[bool, ...] = ()
        immediate_close_calls: tuple[int, ...] = ()
        removed_before_fallback = False
        actions_before_fallback: tuple[str, ...] = ()
        evidence: Dict[str, Any] = {}
        with mock.patch.object(darwin.os, "close", side_effect=record_close):
            try:
                evidence = self.assert_trace_interruption(
                    darwin.DarwinBackend._close_fd_owners.__code__,
                    final_cleanup_entry,
                    lambda: backend.create_private_stage(
                        str(stage), authorize_state=authorize
                    ),
                    label="create stage final cleanup",
                )
                immediate_closed = tuple(owner.closed for owner in evidence["owners"])
                immediate_close_calls = tuple(close_calls)
                removed_before_fallback = not stage.exists()
                actions_before_fallback = tuple(actions)
            finally:
                boundary_active = False
                for owner in evidence.get("owners", ()):
                    owner.close(
                        primary_error=evidence.get("primary"),
                        durable_namespace_complete=True,
                    )
                if stage.exists() and "identity" in evidence:
                    backend.remove_empty_private_stage(
                        str(stage),
                        evidence["identity"],
                        expected_container=evidence["container_identity"],
                        authorize_state=authorize,
                    )

        self.assertTrue(all(immediate_closed))
        for descriptor in evidence["fds"]:
            self.assertEqual(1, immediate_close_calls.count(descriptor))
        self.assertTrue(removed_before_fallback)
        self.assertEqual(("create_stage", "remove_stage"), actions_before_fallback)
        self.assertFalse(stage.exists())
        self.assertEqual(baseline, self.open_fd_set())

    def test_create_private_stage_parent_commit_trace_removes_unreturned_stage(
        self,
    ) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        stage = self.root / f".codex-reflink-repair-{'f' * 32}"
        baseline = self.open_fd_set()
        actions: List[str] = []
        close_calls: List[int] = []
        returned_identities: List[Any] = []
        boundary_active = False
        captured_fd = -1
        captured_identity: Optional[tuple[int, int]] = None
        real_close = darwin.os.close

        def authorize(action: str) -> None:
            actions.append(action)

        def record_close(descriptor: int) -> None:
            if boundary_active and descriptor == captured_fd:
                metadata = os.fstat(descriptor)
                self.assertEqual(
                    captured_identity,
                    (metadata.st_dev, metadata.st_ino),
                )
                close_calls.append(descriptor)
            real_close(descriptor)

        def operation() -> None:
            returned_identities.append(
                backend.create_private_stage(str(stage), authorize_state=authorize)
            )

        def parent_commit_close(frame: Any, evidence: Dict[str, Any]) -> bool:
            nonlocal boundary_active, captured_fd, captured_identity
            owned_close = frame.f_back
            create_frame = owned_close.f_back if owned_close is not None else None
            if (
                owned_close is None
                or owned_close.f_code is not darwin._OwnedFD.close.__code__
                or create_frame is None
                or create_frame.f_code
                is not backend.create_private_stage.__func__.__code__
                or frame.f_locals.get("primary_error") is not None
                or not frame.f_locals.get("durable_namespace_complete", False)
            ):
                return False
            stage_owner = create_frame.f_locals.get("stage_owner")
            parent_owner = create_frame.f_locals.get("parent_owner")
            identity = create_frame.f_locals.get("identity")
            container_identity = create_frame.f_locals.get("container_identity")
            source_line = linecache.getline(
                create_frame.f_code.co_filename, create_frame.f_lineno
            ).strip()
            if (
                frame.f_locals.get("self") is not getattr(parent_owner, "_state", None)
                or owned_close.f_locals.get("self") is not parent_owner
                or not isinstance(stage_owner, darwin._OwnedStageFD)
                or not stage_owner.closed
                or not isinstance(parent_owner, darwin._OwnedFD)
                or parent_owner.closed
                or not isinstance(identity, darwin.FileIdentity)
                or not isinstance(container_identity, darwin.FileIdentity)
                or "parent_owner.close" not in source_line
                or "return identity" not in source_line
            ):
                return False
            captured_fd = parent_owner.fileno()
            metadata = os.fstat(captured_fd)
            captured_identity = (metadata.st_dev, metadata.st_ino)
            evidence["stage_owner"] = stage_owner
            evidence["parent_owner"] = parent_owner
            evidence["identity"] = identity
            evidence["container_identity"] = container_identity
            evidence["commit_line"] = create_frame.f_lineno
            evidence["commit_source"] = source_line
            boundary_active = True
            return True

        evidence: Dict[str, Any] = {}
        closed_before_fallback = False
        removed_before_fallback = False
        close_calls_before_fallback: tuple[int, ...] = ()
        actions_before_fallback: tuple[str, ...] = ()
        with mock.patch.object(darwin.os, "close", side_effect=record_close):
            try:
                evidence = self.assert_trace_interruption(
                    darwin._FDState.close.__code__,
                    parent_commit_close,
                    operation,
                    label="create stage parent commit close",
                    events=("line",),
                )
                closed_before_fallback = evidence["parent_owner"].closed
                removed_before_fallback = not stage.exists()
                close_calls_before_fallback = tuple(close_calls)
                actions_before_fallback = tuple(actions)
            finally:
                boundary_active = False
                parent_owner = evidence.get("parent_owner")
                if parent_owner is not None:
                    parent_owner.close(
                        primary_error=evidence.get("primary"),
                        durable_namespace_complete=True,
                    )
                if stage.exists() and "identity" in evidence:
                    backend.remove_empty_private_stage(
                        str(stage),
                        evidence["identity"],
                        expected_container=evidence["container_identity"],
                        authorize_state=authorize,
                    )

        self.assertEqual([], returned_identities)
        self.assertTrue(evidence["stage_owner"].closed)
        self.assertTrue(closed_before_fallback)
        self.assertTrue(removed_before_fallback)
        self.assertEqual((captured_fd,), close_calls_before_fallback)
        self.assertEqual(("create_stage", "remove_stage"), actions_before_fallback)
        self.assertIn("parent_owner.close", evidence["commit_source"])
        self.assertIn("return identity", evidence["commit_source"])
        self.assertFalse(stage.exists())

        normal_stage = self.root / f".codex-reflink-repair-{'1' * 32}"
        create_code = backend.create_private_stage.__func__.__code__
        normal_events: List[tuple[str, int]] = []
        commit_seen = False
        previous_trace = sys.gettrace()

        def normal_tracer(frame: Any, event: str, _argument: Any) -> Any:
            nonlocal commit_seen
            if frame.f_code is create_code:
                source_line = linecache.getline(
                    frame.f_code.co_filename, frame.f_lineno
                )
                if event == "line" and "parent_owner.close" in source_line:
                    self.assertIn("return identity", source_line)
                    commit_seen = True
                    normal_events.append((event, frame.f_lineno))
                elif commit_seen:
                    normal_events.append((event, frame.f_lineno))
            return normal_tracer

        normal_identity: Optional[Any] = None
        sys.settrace(normal_tracer)
        try:
            normal_identity = backend.create_private_stage(
                str(normal_stage), authorize_state=lambda _action: None
            )
        finally:
            sys.settrace(previous_trace)
        self.assertIsInstance(normal_identity, darwin.FileIdentity)
        self.assertTrue(commit_seen)
        self.assertEqual("line", normal_events[0][0])
        self.assertEqual(
            [], [event for event, _line in normal_events[1:] if event == "line"]
        )
        self.assertEqual("return", normal_events[-1][0])
        assert normal_identity is not None
        backend.remove_empty_private_stage(
            str(normal_stage),
            normal_identity,
            expected_container=self.identity_for_directory(
                backend, normal_stage.parent
            ),
            authorize_state=self.allow_state_mutation,
        )
        self.assertFalse(normal_stage.exists())
        self.assertEqual(baseline, self.open_fd_set())

    def test_publish_final_cleanup_trace_closes_published_owner_once(self) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        stage = self.root / f".codex-reflink-repair-{'a' * 32}"
        staged = stage / "staged"
        destination = self.root / "published"
        backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        staged.write_bytes(b"published payload\n")
        staged.chmod(0o600)
        stage_parent_fd = backend.open_absolute_dir(str(stage))
        destination_parent_fd = backend.open_absolute_dir(str(self.root))
        staged_fd = os.open(staged, os.O_RDONLY)
        try:
            staged_identity = backend.identity(staged_fd)
        finally:
            os.close(staged_fd)
        baseline = self.open_fd_set()
        actions: List[str] = []
        close_calls: List[int] = []
        boundary_active = False
        captured_fd = -1
        captured_identity: Optional[tuple[int, int]] = None
        real_close = darwin.os.close

        def authorize(action: str) -> None:
            actions.append(action)

        def record_close(descriptor: int) -> None:
            if boundary_active and descriptor == captured_fd:
                metadata = os.fstat(descriptor)
                self.assertEqual(
                    captured_identity,
                    (metadata.st_dev, metadata.st_ino),
                )
                close_calls.append(descriptor)
            real_close(descriptor)

        def published_cleanup_entry(frame: Any, evidence: Dict[str, Any]) -> bool:
            nonlocal boundary_active, captured_fd, captured_identity
            caller = frame.f_back
            if (
                caller is None
                or caller.f_code is not backend.publish_staged_name.__func__.__code__
                or caller.f_locals.get("_cleanup_dispatch") != 1
                or frame.f_locals.get("primary_error") is not None
                or not frame.f_locals.get("durable_namespace_complete", False)
            ):
                return False
            owner = caller.f_locals.get("published_owner")
            if not isinstance(owner, darwin._OwnedFD) or owner.closed:
                return False
            captured_fd = owner.fileno()
            metadata = os.fstat(captured_fd)
            captured_identity = (metadata.st_dev, metadata.st_ino)
            evidence["owner"] = owner
            evidence["fd"] = captured_fd
            boundary_active = True
            return True

        immediate_closed = False
        immediate_close_calls: tuple[int, ...] = ()
        evidence: Dict[str, Any] = {}
        try:
            with mock.patch.object(darwin.os, "close", side_effect=record_close):
                try:
                    evidence = self.assert_trace_interruption(
                        darwin.DarwinBackend._close_fd_owners.__code__,
                        published_cleanup_entry,
                        lambda: backend.publish_staged_name(
                            stage_parent_fd,
                            staged.name,
                            staged_identity,
                            destination_parent_fd,
                            destination.name,
                            None,
                            authorize_namespace=authorize,
                            validate_after_authorization=lambda: None,
                        ),
                        label="published owner final cleanup",
                    )
                    immediate_closed = evidence["owner"].closed
                    immediate_close_calls = tuple(close_calls)
                finally:
                    boundary_active = False
                    owner = evidence.get("owner")
                    if owner is not None:
                        owner.close(durable_namespace_complete=True)

            self.assertTrue(immediate_closed)
            self.assertEqual((captured_fd,), immediate_close_calls)
            self.assertEqual(["publish"], actions)
            self.assertFalse(staged.exists())
            self.assertTrue(destination.is_file())
            destination_fd = os.open(destination, os.O_RDONLY)
            try:
                self.assertTrue(
                    backend.identity(destination_fd).is_same_object(staged_identity)
                )
            finally:
                os.close(destination_fd)
        finally:
            if destination.exists():
                destination.unlink()
            if staged.exists():
                staged.unlink()
            if stage.exists():
                stage.rmdir()
            os.close(destination_parent_fd)
            os.close(stage_parent_fd)

        self.assertEqual(
            baseline - {stage_parent_fd, destination_parent_fd},
            self.open_fd_set(),
        )

    def test_calibration_final_cleanup_trace_frees_acl_owner_once(self) -> None:
        darwin = self.darwin
        backend = darwin.DarwinBackend()
        original = self.root / "calibration-original"
        clone = self.root / "calibration-clone"
        original.write_bytes(b"calibration payload\n")
        clone.write_bytes(original.read_bytes())
        original.chmod(0o600)
        clone.chmod(0o600)
        original_fd = os.open(original, os.O_RDWR)
        clone_fd = os.open(clone, os.O_RDWR)
        baseline = self.open_fd_set()
        raw_pointer = 0xCA11B
        free_calls: List[int] = []
        live_owners: List[Any] = []
        apply_owners: List[Any] = []

        def no_acl_owner(fd: int) -> Any:
            owner = darwin._OwnedACL(
                backend,
                f"test no ACL for {fd}",
                lambda target: target._adopt(None),
            )
            live_owners.append(owner)
            return owner

        def empty_acl_owner() -> Any:
            owner = darwin._OwnedACL(
                backend,
                "test empty ACL",
                lambda target: target._adopt(darwin.ctypes.c_void_p(raw_pointer)),
            )
            apply_owners.append(owner)
            return owner

        def record_free(pointer: Any) -> int:
            free_calls.append(pointer.value)
            return 0

        def acl_cleanup_entry(frame: Any, evidence: Dict[str, Any]) -> bool:
            caller = frame.f_back
            if (
                caller is None
                or caller.f_code is not backend.calibrate_clone_policy.__func__.__code__
                or caller.f_locals.get("_cleanup_dispatch") != 1
                or frame.f_locals.get("primary_error") is not None
            ):
                return False
            acl_owners = caller.f_locals.get("acl_owners")
            if not isinstance(acl_owners, tuple):
                return False
            owners = tuple(owner for _subject, owner in acl_owners if owner is not None)
            if len(owners) != 2 or any(owner.closed for owner in owners):
                return False
            evidence["owners"] = owners
            return True

        try:
            expected = backend.snapshot_policy(original_fd)
            clone_stat = os.fstat(clone_fd)
            os.utime(
                clone_fd,
                ns=(clone_stat.st_atime_ns, expected.mtime_ns),
            )
            with (
                mock.patch.object(backend, "_get_acl_owned", side_effect=no_acl_owner),
                mock.patch.object(
                    backend, "_empty_acl_owned", side_effect=empty_acl_owner
                ),
                mock.patch.object(backend, "_acl_set_fd_np", return_value=0),
                mock.patch.object(backend, "_acl_free", side_effect=record_free),
            ):
                evidence = self.assert_trace_interruption(
                    darwin.DarwinBackend._close_acl_owners.__code__,
                    acl_cleanup_entry,
                    lambda: backend.calibrate_clone_policy(
                        original_fd, clone_fd, expected
                    ),
                    label="calibration ACL final cleanup",
                )

            self.assertTrue(all(owner.closed for owner in evidence["owners"]))
            self.assertEqual([raw_pointer], free_calls)
            self.assertEqual(expected, backend.snapshot_policy(clone_fd))
        finally:
            for owner in [*live_owners, *apply_owners]:
                owner.close()
            os.close(clone_fd)
            os.close(original_fd)

        self.assertEqual(baseline - {original_fd, clone_fd}, self.open_fd_set())

    def test_context_body_exception_survives_close_failure_and_drains_transaction(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'8' * 32}"
        stage.mkdir(mode=0o700)
        backend = self.darwin.DarwinBackend()
        sentinel = RuntimeError("with-body-primary")
        armed = False
        close_calls: List[int] = []
        transaction: Optional[Any] = None
        real_close = self.darwin.os.close

        def flaky_close(descriptor: int) -> None:
            if not armed:
                real_close(descriptor)
                return
            close_calls.append(descriptor)
            real_close(descriptor)
            if len(close_calls) == 1:
                raise OSError(errno.EIO, "injected close failure")

        baseline = self.open_fd_set()
        with (
            mock.patch.object(self.darwin.os, "close", side_effect=flaky_close),
            self.assertRaises(RuntimeError) as caught,
        ):
            with backend.bind_transaction(
                str(source),
                str(mirror),
                str(stage / "clone"),
                source_parent_expected=self.identity_for_directory(
                    backend, source.parent
                ),
            ) as transaction:
                armed = True
                raise sentinel

        self.assertIs(sentinel, caught.exception)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction._closed)
        for name in (
            "clone_fd",
            "original_fd",
            "source_fd",
            "temporary_parent_fd",
            "destination_parent_fd",
            "source_parent_fd",
        ):
            self.assertEqual(-1, getattr(transaction, name))
        self.assertGreaterEqual(len(close_calls), 5)
        self.assertEqual(len(close_calls), len(set(close_calls)))
        self.assertEqual(baseline, self.open_fd_set())

    def test_transaction_close_failure_respects_durable_completion(self) -> None:
        darwin = self.darwin
        for index, completed in enumerate((False, True), start=840):
            with self.subTest(completed=completed):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                backend = darwin.DarwinBackend()
                backend.create_private_stage(
                    str(stage), authorize_state=self.allow_state_mutation
                )
                baseline = self.open_fd_set()
                transaction = backend.bind_transaction(
                    str(source),
                    str(mirror),
                    str(stage / "clone"),
                    source_parent_expected=self.identity_for_directory(
                        backend, source.parent
                    ),
                )
                try:
                    if completed:
                        original_snapshot = transaction.original_snapshot()
                        try:
                            transaction.clone(authorize_state=self.allow_state_mutation)
                        except darwin.BackendError as error:
                            if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                                self.skipTest(
                                    f"filesystem does not support strict clone: {error}"
                                )
                            raise
                        transaction.calibrate_clone_policy(original_snapshot.policy)
                        backend.full_fsync(transaction.clone_fd)
                        transaction.unlink_clone(
                            backend.snapshot_expectation(original_snapshot),
                            authorize_state=self.allow_state_mutation,
                        )
                        transaction.remove_empty_stage_parent(
                            authorize_state=self.allow_state_mutation
                        )
                        self.assertTrue(transaction._stage_removed)
                        self.assertTrue(transaction._namespace_lifecycle_complete)
                        self.assertFalse(stage.exists())

                    if not completed:
                        self.assertFalse(transaction._namespace_lifecycle_complete)

                    close_calls: List[int] = []
                    real_close = darwin.os.close

                    def fail_first_close(descriptor: int) -> None:
                        close_calls.append(descriptor)
                        real_close(descriptor)
                        if len(close_calls) == 1:
                            raise OSError(
                                errno.EIO, "injected transaction close failure"
                            )

                    with mock.patch.object(
                        darwin.os, "close", side_effect=fail_first_close
                    ):
                        if completed:
                            transaction.close()
                        else:
                            with self.assertRaises(darwin.BackendError) as caught:
                                transaction.close()
                            self.assertEqual("close_failed", caught.exception.reason)

                    self.assertTrue(transaction._closed)
                    for name in (
                        "clone_fd",
                        "original_fd",
                        "source_fd",
                        "temporary_parent_fd",
                        "destination_parent_fd",
                        "source_parent_fd",
                    ):
                        self.assertEqual(-1, getattr(transaction, name))
                    self.assertGreaterEqual(len(close_calls), 4)
                    self.assertEqual(len(close_calls), len(set(close_calls)))
                    self.assertEqual(baseline, self.open_fd_set())
                    self.assertEqual(completed, not stage.exists())
                finally:
                    if not transaction._closed:
                        transaction.close()

    def test_authorization_precedes_final_survivor_snapshot_before_unlink(self) -> None:
        darwin = self.darwin
        for index, (rollback, mutation) in enumerate(
            (
                (False, "content"),
                (False, "policy"),
                (True, "content"),
                (True, "policy"),
            ),
            start=850,
        ):
            with self.subTest(rollback=rollback, mutation=mutation):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                backend = darwin.DarwinBackend()
                backend.create_private_stage(
                    str(stage), authorize_state=self.allow_state_mutation
                )
                expected_action = "unlink_clone" if rollback else "unlink_original"
                mutated = False
                try:
                    with backend.bind_transaction(
                        str(source),
                        str(mirror),
                        str(stage / "clone"),
                        source_parent_expected=self.identity_for_directory(
                            backend, source.parent
                        ),
                    ) as transaction:
                        source_snapshot = transaction.source_snapshot()
                        original_snapshot = transaction.original_snapshot()
                        transaction.clone(authorize_state=self.allow_state_mutation)
                        transaction.calibrate_clone_policy(original_snapshot.policy)
                        backend.full_fsync(transaction.clone_fd)
                        clone_snapshot = transaction.clone_snapshot()
                        transaction.swap_forward_verified(
                            backend.snapshot_expectation(source_snapshot),
                            backend.snapshot_expectation(original_snapshot),
                            backend.snapshot_expectation(clone_snapshot),
                            authorize_state=self.allow_state_mutation,
                        )
                        if rollback:
                            transaction.swap_back(
                                authorize_state=self.allow_state_mutation
                            )
                        survivor_inode = mirror.stat().st_ino
                        target_inode = (stage / "clone").stat().st_ino

                        def mutate_during_authorization(action: str) -> None:
                            nonlocal mutated
                            self.assertEqual(expected_action, action)
                            self.mutate_same_inode(mirror, mutation)
                            mutated = True

                        with self.assertRaises(darwin.BackendError) as caught:
                            if rollback:
                                transaction.unlink_clone(
                                    backend.snapshot_expectation(original_snapshot),
                                    authorize_state=mutate_during_authorization,
                                )
                            else:
                                transaction.unlink_original(
                                    backend.snapshot_expectation(clone_snapshot),
                                    authorize_state=mutate_during_authorization,
                                )
                        self.assertEqual(
                            "original_snapshot_mismatch"
                            if rollback
                            else "clone_snapshot_mismatch",
                            caught.exception.reason,
                        )
                        self.assertTrue(mutated)
                        self.assertEqual(survivor_inode, mirror.stat().st_ino)
                        self.assertEqual(target_inode, (stage / "clone").stat().st_ino)
                except darwin.BackendError as error:
                    if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                        self.skipTest(
                            f"filesystem does not support strict clone/swap: {error}"
                        )
                    raise

                self.assertTrue(stage.exists())
                self.assertTrue((stage / "clone").exists())

    def test_intent_cleanup_authorization_precedes_original_survivor_snapshot(
        self,
    ) -> None:
        for index, mutation in enumerate(("content", "policy"), start=870):
            with self.subTest(mutation=mutation):
                (
                    backend,
                    _source,
                    mirror,
                    stage,
                    container_identity,
                    original_expectation,
                    stage_identity,
                    clone_identity,
                    clone_sha256,
                    clone_expectation,
                ) = self.prepare_intent_clone_artifact(f"{index:032x}")
                original_inode = mirror.stat().st_ino
                clone_inode = (stage / "clone").stat().st_ino
                actions: List[str] = []

                def mutate_during_authorization(action: str) -> None:
                    actions.append(action)
                    if action == "intent_unlink_clone":
                        self.mutate_same_inode(mirror, mutation)

                with self.assertRaises(self.darwin.BackendError) as caught:
                    backend.cleanup_intent_stage(
                        str(stage),
                        final_path=str(mirror),
                        expected_container=container_identity,
                        expected_original=original_expectation,
                        expected_stage=stage_identity,
                        allow_clone=True,
                        expected_clone=clone_identity,
                        expected_snapshot=clone_expectation,
                        expected_size=clone_identity.size,
                        expected_sha256=clone_sha256,
                        authorize_state=mutate_during_authorization,
                    )

                self.assertEqual(
                    "intent_original_snapshot_mismatch", caught.exception.reason
                )
                self.assertEqual(["intent_unlink_clone"], actions)
                self.assertEqual(original_inode, mirror.stat().st_ino)
                self.assertEqual(clone_inode, (stage / "clone").stat().st_ino)
                self.assertTrue(stage.exists())

    def test_unlink_primitive_orders_authorization_snapshot_identity_and_syscall(
        self,
    ) -> None:
        parent = self.root / "unlink-order"
        parent.mkdir()
        target = parent / "target"
        survivor = parent / "survivor"
        target.write_bytes(b"target")
        survivor.write_bytes(b"survivor")
        target.chmod(0o600)
        survivor.chmod(0o600)
        darwin = self.darwin
        events: List[str] = []

        class RecordingBackend(darwin.DarwinBackend):
            def require_snapshot(
                self, descriptor: int, expected: Any, subject: str, **keywords: Any
            ) -> Any:
                events.append("survivor-snapshot")
                return super().require_snapshot(
                    descriptor, expected, subject, **keywords
                )

            def require_identity_at(
                self, parent_fd: int, name: str, expected: Any
            ) -> Any:
                events.append("target-identity")
                return super().require_identity_at(parent_fd, name, expected)

            def unlink_name(self, *arguments: Any, **keywords: Any) -> None:
                super().unlink_name(*arguments, **keywords)
                events.append("postcheck")

        backend = RecordingBackend()
        real_unlinkat = backend._unlinkat

        def recording_unlinkat(*arguments: Any) -> int:
            events.append("unlink-syscall")
            return int(real_unlinkat(*arguments))

        backend._unlinkat = recording_unlinkat
        parent_fd = backend.open_absolute_dir(str(parent))
        target_fd = backend.open_leaf(parent_fd, target.name)
        survivor_fd = backend.open_leaf(parent_fd, survivor.name)
        try:
            target_identity = backend.identity(target_fd)
            survivor_expectation = backend.snapshot_expectation(
                backend.snapshot_file(survivor_fd)
            )

            def authorize(action: str) -> None:
                self.assertEqual("test_unlink", action)
                events.append("authorize")

            def validate_survivor() -> None:
                snapshot = backend.require_snapshot(
                    survivor_fd, survivor_expectation, "ordered survivor"
                )
                backend.require_exclusive_writer_policy(
                    snapshot.policy, "ordered survivor"
                )

            backend.unlink_name(
                parent_fd,
                target.name,
                target_identity,
                authorize_state=authorize,
                action="test_unlink",
                validate_after_authorization=validate_survivor,
            )
        finally:
            os.close(survivor_fd)
            os.close(target_fd)
            os.close(parent_fd)

        self.assertEqual(
            [
                "authorize",
                "survivor-snapshot",
                "target-identity",
                "unlink-syscall",
                "postcheck",
            ],
            events,
        )
        self.assertFalse(target.exists())

    def test_repeated_abort_revalidates_rolled_back_survivor_before_stage_removal(
        self,
    ) -> None:
        darwin = self.darwin
        for index, mutation in enumerate(
            ("content", "policy", "unreadable"), start=880
        ):
            with self.subTest(mutation=mutation):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"
                fixture = self

                class PostUnlinkFailureBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.after_unlink = False
                        super().__init__()

                    def unlink_name(self, *arguments: Any, **keywords: Any) -> None:
                        super().unlink_name(*arguments, **keywords)
                        if keywords.get("action") == "unlink_clone":
                            self.after_unlink = True
                            if mutation != "unreadable":
                                fixture.mutate_same_inode(mirror, mutation)

                    def require_snapshot(
                        self,
                        descriptor: int,
                        expected: Any,
                        subject: str,
                        **keywords: Any,
                    ) -> Any:
                        if (
                            mutation == "unreadable"
                            and self.after_unlink
                            and subject == "rolled-back final original"
                        ):
                            raise darwin.BackendError(
                                "original_snapshot_unreadable",
                                "injected rolled-back survivor read failure",
                                errno.EACCES,
                            )
                        return super().require_snapshot(
                            descriptor, expected, subject, **keywords
                        )

                backend = PostUnlinkFailureBackend()
                backend.create_private_stage(
                    str(stage), authorize_state=self.allow_state_mutation
                )
                try:
                    with backend.bind_transaction(
                        str(source),
                        str(mirror),
                        str(stage / "clone"),
                        source_parent_expected=self.identity_for_directory(
                            backend, source.parent
                        ),
                    ) as transaction:
                        original_snapshot = transaction.original_snapshot()
                        transaction.clone(authorize_state=self.allow_state_mutation)
                        transaction.calibrate_clone_policy(original_snapshot.policy)
                        backend.full_fsync(transaction.clone_fd)
                        expectation = backend.snapshot_expectation(original_snapshot)

                        with self.assertRaises(darwin.BackendError):
                            transaction.abort_before_prepared(
                                expectation,
                                authorize_state=self.allow_state_mutation,
                            )
                        self.assertFalse((stage / "clone").exists())
                        self.assertTrue(stage.exists())

                        with self.assertRaises(darwin.BackendError):
                            transaction.abort_before_prepared(
                                expectation,
                                authorize_state=self.allow_state_mutation,
                            )
                        self.assertTrue(stage.exists())
                        self.assertEqual([], list(stage.iterdir()))
                except darwin.BackendError as error:
                    if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                        self.skipTest(
                            f"filesystem does not support strict clone: {error}"
                        )
                    raise

    def test_stage_removal_close_failure_keeps_success_and_proves_absence(
        self,
    ) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'9' * 32}"
        backend = self.darwin.DarwinBackend()
        backend.create_private_stage(
            str(stage), authorize_state=self.allow_state_mutation
        )
        transaction = backend.bind_transaction(
            str(source),
            str(mirror),
            str(stage / "clone"),
            source_parent_expected=self.identity_for_directory(backend, source.parent),
        )
        try:
            original_snapshot = transaction.original_snapshot()
            transaction.clone(authorize_state=self.allow_state_mutation)
            transaction.calibrate_clone_policy(original_snapshot.policy)
            backend.full_fsync(transaction.clone_fd)
            transaction.unlink_clone(
                backend.snapshot_expectation(original_snapshot),
                authorize_state=self.allow_state_mutation,
            )
            temporary_parent_fd = transaction.temporary_parent_fd
            original_absence_check = transaction._require_stage_parent_absent
            transaction._require_stage_parent_absent = mock.Mock(
                wraps=original_absence_check
            )
            failed_close = False
            real_close = self.darwin.os.close

            def fail_removed_parent_close(descriptor: int) -> None:
                nonlocal failed_close
                real_close(descriptor)
                if descriptor == temporary_parent_fd and not failed_close:
                    failed_close = True
                    raise OSError(errno.EIO, "injected removed-parent close failure")

            with mock.patch.object(
                self.darwin.os, "close", side_effect=fail_removed_parent_close
            ):
                transaction.remove_empty_stage_parent(
                    authorize_state=self.allow_state_mutation
                )

            self.assertTrue(failed_close)
            self.assertTrue(transaction._stage_removed)
            self.assertEqual(-1, transaction.temporary_parent_fd)
            transaction._require_stage_parent_absent.assert_called_once_with()
            self.assertFalse(stage.exists())
        except self.darwin.BackendError as error:
            if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                self.skipTest(f"filesystem does not support strict clone: {error}")
            raise
        finally:
            transaction.close()

    def test_stage_absence_primary_error_survives_final_close_failure(self) -> None:
        source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
            create_stage=False
        )
        stage = mirror.parent / f".codex-reflink-repair-{'a' * 32}"
        stage.mkdir(mode=0o700)
        backend = self.darwin.DarwinBackend()
        transaction = backend.bind_transaction(
            str(source),
            str(mirror),
            str(stage / "clone"),
            source_parent_expected=self.identity_for_directory(backend, source.parent),
        )
        armed = False
        close_failed = False
        real_stat = self.darwin.os.stat
        real_close = self.darwin.os.close

        def arm_on_present_stage(*arguments: Any, **keywords: Any) -> Any:
            nonlocal armed
            result = real_stat(*arguments, **keywords)
            if arguments and arguments[0] == stage.name:
                armed = True
            return result

        def fail_primary_cleanup_close(descriptor: int) -> None:
            nonlocal close_failed
            real_close(descriptor)
            if armed and not close_failed:
                close_failed = True
                raise OSError(errno.EIO, "injected final close failure")

        try:
            with (
                mock.patch.object(
                    self.darwin.os, "stat", side_effect=arm_on_present_stage
                ),
                mock.patch.object(
                    self.darwin.os, "close", side_effect=fail_primary_cleanup_close
                ),
                self.assertRaises(self.darwin.BackendError) as caught,
            ):
                transaction._require_stage_parent_absent()

            self.assertEqual("stage_absence_unverified", caught.exception.reason)
            self.assertTrue(close_failed)
            self.assertTrue(stage.exists())
        finally:
            transaction.close()

    def test_real_unlink_revalidates_survivor_before_stage_cleanup(self) -> None:
        darwin = self.darwin
        for index, rollback in enumerate((False, True), start=900):
            with self.subTest(rollback=rollback):
                source, mirror, _unused_stage, _unused_clone = self.prepare_pair(
                    create_stage=False
                )
                stage = mirror.parent / f".codex-reflink-repair-{index:032x}"

                class MutateSurvivorAfterUnlinkBackend(darwin.DarwinBackend):
                    def __init__(self) -> None:
                        self.mutated = False
                        super().__init__()

                    def unlink_name(self, *arguments: Any, **keywords: Any) -> None:
                        super().unlink_name(*arguments, **keywords)
                        expected_action = (
                            "unlink_clone" if rollback else "unlink_original"
                        )
                        if keywords.get("action") == expected_action:
                            with mirror.open("r+b", buffering=0) as stream:
                                current = stream.read(1)
                                stream.seek(0)
                                stream.write(bytes([current[0] ^ 1]))
                                stream.flush()
                                os.fsync(stream.fileno())
                            self.mutated = True

                backend = MutateSurvivorAfterUnlinkBackend()
                backend.create_private_stage(
                    str(stage), authorize_state=self.allow_state_mutation
                )
                try:
                    with backend.bind_transaction(
                        str(source),
                        str(mirror),
                        str(stage / "clone"),
                        source_parent_expected=self.identity_for_directory(
                            backend, source.parent
                        ),
                    ) as transaction:
                        source_snapshot = transaction.source_snapshot()
                        original_snapshot = transaction.original_snapshot()
                        transaction.clone(authorize_state=self.allow_state_mutation)
                        transaction.calibrate_clone_policy(original_snapshot.policy)
                        backend.full_fsync(transaction.clone_fd)
                        clone_snapshot = transaction.clone_snapshot()
                        transaction.swap_forward_verified(
                            backend.snapshot_expectation(source_snapshot),
                            backend.snapshot_expectation(original_snapshot),
                            backend.snapshot_expectation(clone_snapshot),
                            authorize_state=self.allow_state_mutation,
                        )
                        if rollback:
                            transaction.swap_back(
                                authorize_state=self.allow_state_mutation
                            )
                            with self.assertRaises(darwin.BackendError):
                                transaction.unlink_clone(
                                    backend.snapshot_expectation(original_snapshot),
                                    authorize_state=self.allow_state_mutation,
                                )
                        else:
                            with self.assertRaises(darwin.BackendError):
                                transaction.unlink_original(
                                    backend.snapshot_expectation(clone_snapshot),
                                    authorize_state=self.allow_state_mutation,
                                )
                except darwin.BackendError as error:
                    if error.errno_value in {errno.ENOTSUP, errno.EXDEV}:
                        self.skipTest(
                            f"filesystem does not support strict clone/swap: {error}"
                        )
                    raise

                self.assertTrue(backend.mutated)
                self.assertTrue(stage.exists())

    def test_real_clone_and_single_swap_preserve_bytes_and_mirror_policy(self) -> None:
        source, mirror, stage, clone = self.prepare_pair()
        darwin = self.darwin

        class SpyBackend(darwin.DarwinBackend):
            def __init__(self) -> None:
                self.clone_calls = 0
                self.swap_calls = 0
                super().__init__()

            def strict_clone(self, *arguments: Any, **keyword_arguments: Any) -> int:
                self.clone_calls += 1
                return super().strict_clone(*arguments, **keyword_arguments)

            def swap_names(self, *arguments: Any, **keyword_arguments: Any) -> Any:
                self.swap_calls += 1
                return super().swap_names(*arguments, **keyword_arguments)

        backend = SpyBackend()
        old_inode = mirror.stat().st_ino
        try:
            with backend.bind_transaction(
                str(source),
                str(mirror),
                str(clone),
                source_parent_expected=self.identity_for_directory(
                    backend, source.parent
                ),
            ) as tx:
                source_snapshot = tx.source_snapshot()
                original_snapshot = tx.original_snapshot()
                clone_identity = tx.clone(authorize_state=self.allow_state_mutation)
                tx.calibrate_clone_policy(original_snapshot.policy)
                backend.full_fsync(tx.clone_fd)
                clone_snapshot = tx.clone_snapshot()
                self.assertEqual(source_snapshot.sha256, clone_snapshot.sha256)
                self.assertEqual(original_snapshot.policy, clone_snapshot.policy)
                tx.swap_forward_verified(
                    backend.snapshot_expectation(source_snapshot),
                    backend.snapshot_expectation(original_snapshot),
                    backend.snapshot_expectation(clone_snapshot),
                    authorize_state=self.allow_state_mutation,
                )
                tx.verify_forward()
                self.assertEqual("forward", tx.orientation())
                self.assertEqual(clone_identity.ino, mirror.stat().st_ino)
                tx.unlink_original(
                    backend.snapshot_expectation(clone_snapshot),
                    authorize_state=self.allow_state_mutation,
                )
                tx.remove_empty_stage_parent(authorize_state=self.allow_state_mutation)
        except darwin.BackendError as error:
            if error.errno_value in (errno.ENOTSUP, errno.EXDEV):
                self.skipTest(f"filesystem does not support strict clone/swap: {error}")
            raise

        self.assertEqual(1, backend.clone_calls)
        self.assertEqual(1, backend.swap_calls)
        self.assertNotEqual(old_inode, mirror.stat().st_ino)
        self.assertEqual(source.read_bytes(), mirror.read_bytes())
        self.assertEqual(0o600, mirror.stat().st_mode & 0o777)
        self.assertEqual(
            "policy-value",
            subprocess.check_output(
                [
                    "/usr/bin/xattr",
                    "-p",
                    "com.openai.codex.reflink-test",
                    str(mirror),
                ],
                text=True,
            ).rstrip("\n"),
        )
        self.assertFalse(stage.exists())

    def test_preexisting_clone_child_is_never_overwritten(self) -> None:
        source, mirror, _stage, clone = self.prepare_pair()
        clone.write_bytes(b"sentinel")
        backend = self.darwin.DarwinBackend()
        old_inode = mirror.stat().st_ino

        with self.assertRaises(self.darwin.BackendError) as raised:
            backend.bind_transaction(
                str(source),
                str(mirror),
                str(clone),
                source_parent_expected=self.identity_for_directory(
                    backend, source.parent
                ),
            )

        self.assertEqual(errno.EEXIST, raised.exception.errno_value)
        self.assertEqual(b"sentinel", clone.read_bytes())
        self.assertEqual(old_inode, mirror.stat().st_ino)


if __name__ == "__main__":
    unittest.main()
