#!/usr/bin/env python3
"""Safely replace byte-identical rollout mirror files with APFS clones.

The utility is intentionally conservative.  It pairs rollouts by the canonical
UUID at the end of the filename, validates both content and access policy, and
uses a durable per-rollout manifest around every atomic path swap.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import enum
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import time
import uuid
import weakref
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TextIO,
    Tuple,
)


SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_FATAL = 2
MAX_STATE_JSON_BYTES = 16 * 1024 * 1024
STATE_READ_ATTEMPTS = 3
ROLLOUT_ID_PATTERN = re.compile(
    r"^rollout-.+-(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PRIVATE_STAGE_PATTERN = re.compile(r"^\.codex-reflink-repair-[0-9a-f]{32}$")
EMPTY_ACL_SHA256 = hashlib.sha256(b"").hexdigest()
# Darwin flags that the backend can faithfully preserve without granting a
# second writer or introducing immutable/internal namespace semantics.
SAFE_EXCLUSIVE_WRITER_FLAGS = 0x00000001 | 0x00000008 | 0x00008000 | 0x00010000


class RepairError(Exception):
    """Base class for expected repair failures."""


class FatalRepairError(RepairError):
    """A state or syscall failure that requires operator attention."""


class PartialProgressError(FatalRepairError):
    """A fatal operation that follows already reported per-item progress."""

    def __init__(
        self,
        cause: Exception,
        results: Sequence[Mapping[str, Any]],
        *,
        rollout_id: Optional[str] = None,
    ) -> None:
        self.cause = cause
        self.results = [dict(result) for result in results]
        self.rollout_id = rollout_id
        super().__init__(str(cause))


class CommandFatalError(FatalRepairError):
    """A command failure carrying the single authoritative JSON receipt."""

    def __init__(self, cause: Exception, receipt: Mapping[str, Any]) -> None:
        self.cause = cause
        self.receipt = dict(receipt)
        super().__init__(str(cause))


class AtomicPublication(str, enum.Enum):
    NOT_PUBLISHED = "not-published"
    PUBLISHED_UNSYNCED = "published-unsynced"
    DURABLE = "durable"
    AMBIGUOUS = "ambiguous"


class AtomicWriteError(FatalRepairError):
    """A durable JSON replacement failed with a proved publication state."""

    def __init__(self, message: str, *, publication: AtomicPublication) -> None:
        self.publication = publication
        # Keep the earlier injectable API useful while exposing the full state.
        self.replaced: Optional[bool] = {
            AtomicPublication.NOT_PUBLISHED: False,
            AtomicPublication.PUBLISHED_UNSYNCED: True,
            AtomicPublication.DURABLE: True,
            AtomicPublication.AMBIGUOUS: None,
        }[publication]
        super().__init__(message)


class AtomicWriteInterruption(BaseException):
    """A non-Exception interruption during durable JSON replacement."""

    def __init__(
        self,
        cause: BaseException,
        *,
        publication: AtomicPublication,
        detail: Optional[str] = None,
        cleanup_error: Optional[BaseException] = None,
        cleanup_receipt: Optional["_CleanupActionReceipt"] = None,
    ) -> None:
        self.cause = cause
        self.publication = publication
        self.detail = str(cause) if detail is None else detail
        self.cleanup_error = cleanup_error
        self.cleanup_receipt = cleanup_receipt
        self.replaced: Optional[bool] = {
            AtomicPublication.NOT_PUBLISHED: False,
            AtomicPublication.PUBLISHED_UNSYNCED: True,
            AtomicPublication.DURABLE: True,
            AtomicPublication.AMBIGUOUS: None,
        }[publication]
        super().__init__(self.detail)
        try:
            setattr(cause, "_atomic_write_interruption", self)
            add_note = getattr(cause, "add_note", None)
            if cleanup_error is not None and callable(add_note):
                notes = getattr(cause, "__notes__", ())
                if self.detail not in notes:
                    add_note(self.detail)
        except BaseException:
            # The wrapper remains the authoritative diagnostic bridge even for
            # an unusual BaseException that rejects attributes or notes.
            pass


class SafetyError(FatalRepairError):
    """The protected content, policy, or identity property is not proven."""


class UnsupportedError(FatalRepairError):
    """The platform cannot provide strict clone and atomic-swap primitives."""


class CandidateUnsupportedError(RepairError):
    """A safely cleaned candidate cannot be repaired on this filesystem."""


class MissingPathError(RepairError):
    """A path disappeared during a bounded operation."""


class UnreadablePathError(RepairError):
    """A path exists but cannot be inspected safely."""


class UnstablePathError(RepairError):
    """A protected property changed during repeated inspection."""


class UnsafeLinkCountError(RepairError):
    """A readable candidate has more than one hard link."""


class SafeRolledBack(UnstablePathError):
    """Postverify failed, but the original object was durably restored."""


class Phase(str, enum.Enum):
    INTENT = "INTENT"
    PREPARED = "PREPARED"
    COMMIT_READY = "COMMIT_READY"
    DONE = "DONE"
    ROLLBACK_READY = "ROLLBACK_READY"
    ROLLED_BACK = "ROLLED_BACK"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class IntentState(str, enum.Enum):
    PLANNED = "PLANNED"
    STAGE_BOUND = "STAGE_BOUND"
    CLONE_BOUND = "CLONE_BOUND"


class ContentRelation(str, enum.Enum):
    EXACT = "exact"
    MIRROR_COMPLETE_PREFIX = "mirror-complete-prefix"
    DIFFERENT = "different"


class Orientation(str, enum.Enum):
    ORIGINAL_FINAL_CLONE_TEMP = "original-final-clone-temp"
    CLONE_FINAL_ORIGINAL_TEMP = "clone-final-original-temp"
    CLONE_FINAL_TEMP_MISSING = "clone-final-temp-missing"
    ORIGINAL_FINAL_TEMP_MISSING = "original-final-temp-missing"
    IMPOSSIBLE = "impossible"


@dataclasses.dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int

    def to_json(self) -> Dict[str, int]:
        return {"device": self.device, "inode": self.inode}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FileIdentity":
        try:
            return cls(device=int(value["device"]), inode=int(value["inode"]))
        except (KeyError, TypeError, ValueError) as error:
            raise FatalRepairError(
                "invalid file identity in durable manifest"
            ) from error


@dataclasses.dataclass(frozen=True)
class PolicyFingerprint:
    uid: int
    gid: int
    mode: int
    flags: int
    acl_sha256: str
    xattrs_sha256: str

    def to_json(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "PolicyFingerprint":
        try:
            return cls(
                uid=int(value["uid"]),
                gid=int(value["gid"]),
                mode=int(value["mode"]),
                flags=int(value["flags"]),
                acl_sha256=str(value["acl_sha256"]),
                xattrs_sha256=str(value["xattrs_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FatalRepairError(
                "invalid policy fingerprint in durable manifest"
            ) from error


@dataclasses.dataclass(frozen=True)
class FileSnapshot:
    identity: FileIdentity
    size: int
    mtime_ns: int
    nlink: int
    content_sha256: str
    policy: PolicyFingerprint

    def to_json(self) -> Dict[str, Any]:
        return {
            "identity": self.identity.to_json(),
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "nlink": self.nlink,
            "content_sha256": self.content_sha256,
            "policy": self.policy.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FileSnapshot":
        try:
            return cls(
                identity=FileIdentity.from_json(_mapping(value["identity"])),
                size=int(value["size"]),
                mtime_ns=int(value["mtime_ns"]),
                nlink=int(value["nlink"]),
                content_sha256=str(value["content_sha256"]),
                policy=PolicyFingerprint.from_json(_mapping(value["policy"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FatalRepairError(
                "invalid file snapshot in durable manifest"
            ) from error


AUTHORIZED_STATE_ACTIONS = frozenset(
    {
        "create_stage",
        "create_clone",
        "swap_forward",
        "swap_back",
        "unlink_original",
        "unlink_clone",
        "remove_stage",
        "intent_unlink_clone",
        "intent_remove_stage",
    }
)


@dataclasses.dataclass(frozen=True)
class DurableFence:
    path: pathlib.Path
    parent_identity: FileIdentity
    leaf_identity: FileIdentity
    encoded_payload: bytes


@dataclasses.dataclass
class _AtomicWriteReceipt:
    publication: AtomicPublication = AtomicPublication.NOT_PUBLISHED
    fence: Optional[DurableFence] = None


@dataclasses.dataclass(frozen=True)
class Candidate:
    rollout_id: str
    source: pathlib.Path
    mirror: pathlib.Path
    source_rel: str
    mirror_rel: str


@dataclasses.dataclass(frozen=True)
class PairInspection:
    source: FileSnapshot
    mirror: FileSnapshot
    source_parent: FileIdentity
    mirror_parent: FileIdentity
    relation: ContentRelation


@dataclasses.dataclass(frozen=True)
class QueueEntry:
    rollout_id: str

    def to_json(self) -> str:
        return self.rollout_id

    @classmethod
    def from_json(cls, value: Any) -> "QueueEntry":
        try:
            return cls(rollout_id=canonical_rollout_id(str(value)))
        except (TypeError, ValueError) as error:
            raise FatalRepairError("invalid retry queue entry") from error


@dataclasses.dataclass(frozen=True)
class RepairIntent:
    rollout_id: str
    txid: str
    state: IntentState
    source_rel: str
    final_rel: str
    temporary_rel: str
    source_snapshot: FileSnapshot
    original_snapshot: FileSnapshot
    source_parent_identity: FileIdentity
    final_parent_identity: FileIdentity
    temporary_parent_identity: Optional[FileIdentity]
    clone_snapshot: Optional[FileSnapshot]
    queue_enabled: bool
    created_at_ns: int
    updated_at_ns: int
    fence: Optional[DurableFence] = dataclasses.field(
        default=None, compare=False, repr=False
    )

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "phase": Phase.INTENT.value,
            "rollout_id": self.rollout_id,
            "txid": self.txid,
            "state": self.state.value,
            "source_rel": self.source_rel,
            "final_rel": self.final_rel,
            "temporary_rel": self.temporary_rel,
            "source_snapshot": self.source_snapshot.to_json(),
            "original_snapshot": self.original_snapshot.to_json(),
            "source_parent_identity": self.source_parent_identity.to_json(),
            "final_parent_identity": self.final_parent_identity.to_json(),
            "temporary_parent_identity": (
                None
                if self.temporary_parent_identity is None
                else self.temporary_parent_identity.to_json()
            ),
            "clone_snapshot": (
                None if self.clone_snapshot is None else self.clone_snapshot.to_json()
            ),
            "queue_enabled": self.queue_enabled,
            "created_at_ns": self.created_at_ns,
            "updated_at_ns": self.updated_at_ns,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "RepairIntent":
        try:
            if value.get("version") != SCHEMA_VERSION:
                raise FatalRepairError("unsupported durable intent version")
            if value.get("phase") != Phase.INTENT.value:
                raise FatalRepairError("invalid durable intent phase")
            txid = str(value["txid"])
            if not re.fullmatch(r"[0-9a-f]{32}", txid):
                raise FatalRepairError("invalid transaction ID in durable intent")
            queue_enabled = value.get("queue_enabled", False)
            if not isinstance(queue_enabled, bool):
                raise FatalRepairError("invalid queue_enabled flag in durable intent")
            state = IntentState(str(value["state"]))
            temporary_parent_raw = value.get("temporary_parent_identity")
            clone_raw = value.get("clone_snapshot")
            intent = cls(
                rollout_id=canonical_rollout_id(str(value["rollout_id"])),
                txid=txid,
                state=state,
                source_rel=str(value["source_rel"]),
                final_rel=str(value["final_rel"]),
                temporary_rel=str(value["temporary_rel"]),
                source_snapshot=FileSnapshot.from_json(
                    _mapping(value["source_snapshot"])
                ),
                original_snapshot=FileSnapshot.from_json(
                    _mapping(value["original_snapshot"])
                ),
                source_parent_identity=FileIdentity.from_json(
                    _mapping(value["source_parent_identity"])
                ),
                final_parent_identity=FileIdentity.from_json(
                    _mapping(value["final_parent_identity"])
                ),
                temporary_parent_identity=(
                    None
                    if temporary_parent_raw is None
                    else FileIdentity.from_json(_mapping(temporary_parent_raw))
                ),
                clone_snapshot=(
                    None
                    if clone_raw is None
                    else FileSnapshot.from_json(_mapping(clone_raw))
                ),
                queue_enabled=queue_enabled,
                created_at_ns=int(value["created_at_ns"]),
                updated_at_ns=int(value["updated_at_ns"]),
            )
            _manifest_paths_unchecked(intent)
            if state == IntentState.PLANNED and (
                intent.temporary_parent_identity is not None
                or intent.clone_snapshot is not None
            ):
                raise FatalRepairError(
                    "PLANNED intent contains premature object identity"
                )
            if state == IntentState.STAGE_BOUND and (
                intent.temporary_parent_identity is None
                or intent.clone_snapshot is not None
            ):
                raise FatalRepairError("STAGE_BOUND intent has invalid object evidence")
            if state == IntentState.CLONE_BOUND and (
                intent.temporary_parent_identity is None
                or intent.clone_snapshot is None
            ):
                raise FatalRepairError("CLONE_BOUND intent lacks object evidence")
            return intent
        except (KeyError, TypeError, ValueError) as error:
            raise FatalRepairError("invalid durable intent") from error


@dataclasses.dataclass(frozen=True)
class RepairManifest:
    rollout_id: str
    txid: str
    phase: Phase
    source_rel: str
    final_rel: str
    temporary_rel: str
    source_parent_identity: FileIdentity
    final_parent_identity: FileIdentity
    temporary_parent_identity: FileIdentity
    source_snapshot: FileSnapshot
    original_snapshot: FileSnapshot
    clone_snapshot: FileSnapshot
    created_at_ns: int
    updated_at_ns: int
    retryable: bool = False
    queue_enabled: bool = False
    error: str = ""
    fence: Optional[DurableFence] = dataclasses.field(
        default=None, compare=False, repr=False
    )

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "rollout_id": self.rollout_id,
            "txid": self.txid,
            "phase": self.phase.value,
            "source_rel": self.source_rel,
            "final_rel": self.final_rel,
            "temporary_rel": self.temporary_rel,
            "source_parent_identity": self.source_parent_identity.to_json(),
            "final_parent_identity": self.final_parent_identity.to_json(),
            "temporary_parent_identity": self.temporary_parent_identity.to_json(),
            "source_snapshot": self.source_snapshot.to_json(),
            "original_snapshot": self.original_snapshot.to_json(),
            "clone_snapshot": self.clone_snapshot.to_json(),
            "created_at_ns": self.created_at_ns,
            "updated_at_ns": self.updated_at_ns,
            "retryable": self.retryable,
            "queue_enabled": self.queue_enabled,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "RepairManifest":
        try:
            if value.get("version") != SCHEMA_VERSION:
                raise FatalRepairError("unsupported durable manifest version")
            txid = str(value["txid"])
            if not re.fullmatch(r"[0-9a-f]{32}", txid):
                raise FatalRepairError("invalid transaction ID in durable manifest")
            retryable = value.get("retryable", False)
            if not isinstance(retryable, bool):
                raise FatalRepairError("invalid retryable flag in durable manifest")
            queue_enabled = value.get("queue_enabled", False)
            if not isinstance(queue_enabled, bool):
                raise FatalRepairError("invalid queue_enabled flag in durable manifest")
            manifest = cls(
                rollout_id=canonical_rollout_id(str(value["rollout_id"])),
                txid=txid,
                phase=Phase(str(value["phase"])),
                source_rel=str(value["source_rel"]),
                final_rel=str(value["final_rel"]),
                temporary_rel=str(value["temporary_rel"]),
                source_parent_identity=FileIdentity.from_json(
                    _mapping(value["source_parent_identity"])
                ),
                final_parent_identity=FileIdentity.from_json(
                    _mapping(value["final_parent_identity"])
                ),
                temporary_parent_identity=FileIdentity.from_json(
                    _mapping(value["temporary_parent_identity"])
                ),
                source_snapshot=FileSnapshot.from_json(
                    _mapping(value["source_snapshot"])
                ),
                original_snapshot=FileSnapshot.from_json(
                    _mapping(value["original_snapshot"])
                ),
                clone_snapshot=FileSnapshot.from_json(
                    _mapping(value["clone_snapshot"])
                ),
                created_at_ns=int(value["created_at_ns"]),
                updated_at_ns=int(value["updated_at_ns"]),
                retryable=retryable,
                queue_enabled=queue_enabled,
                error=str(value.get("error", "")),
            )
            _manifest_paths_unchecked(manifest)
            return manifest
        except (KeyError, TypeError, ValueError) as error:
            raise FatalRepairError("invalid durable manifest") from error


@dataclasses.dataclass(frozen=True)
class Config:
    codex_root: pathlib.Path
    mirror_root: pathlib.Path
    state_root: pathlib.Path

    @property
    def queue_path(self) -> pathlib.Path:
        return self.state_root / "retry-queue.json"

    @property
    def manifests_dir(self) -> pathlib.Path:
        return self.state_root / "manifests"

    @property
    def intents_dir(self) -> pathlib.Path:
        return self.state_root / "intents"

    @property
    def lock_path(self) -> pathlib.Path:
        return self.state_root / "repair.lock"

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None) -> "Config":
        environment = os.environ if environ is None else environ
        home = pathlib.Path(environment.get("HOME", str(pathlib.Path.home())))
        backup_base = pathlib.Path(
            environment.get("CODEX_BACKUP_BASE", str(home / ".dotfiles/codex-backup"))
        )
        backup_state = pathlib.Path(
            environment.get("CODEX_BACKUP_STATE_ROOT", str(backup_base / "state"))
        )
        return cls(
            codex_root=pathlib.Path(
                environment.get("CODEX_ROOT", str(home / ".codex"))
            ),
            mirror_root=pathlib.Path(
                environment.get("CODEX_MIRROR_ROOT", str(backup_base / "mirror"))
            ),
            state_root=backup_state / "reflink-repair",
        )


class Backend(Protocol):
    """Strict filesystem primitives supplied by the Darwin backend."""

    def inspect_pair(
        self, source: pathlib.Path, mirror: pathlib.Path
    ) -> PairInspection: ...

    def prepare(
        self,
        source: pathlib.Path,
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        expected: PairInspection,
        authorize_state: Callable[[str], None],
    ) -> "BoundTransaction": ...

    def resume(
        self,
        source: Optional[pathlib.Path],
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        manifest: RepairManifest,
        authorize_state: Callable[[str], None],
    ) -> "BoundTransaction": ...

    def recover_intent_stage(
        self,
        intent: RepairIntent,
        temporary: pathlib.Path,
        authorize_state: Callable[[str], None],
    ) -> str: ...


class BoundTransaction(Protocol):
    """An identity-bound transaction that owns all safety-critical FDs."""

    def clone(
        self, expected_source: FileSnapshot, expected_original: FileSnapshot
    ) -> FileSnapshot: ...

    def mark_prepared(self) -> None: ...

    def revalidate_before_prepared(
        self,
        expected_source: FileSnapshot,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None: ...

    def parent_identities(self) -> Tuple[FileIdentity, FileIdentity]: ...

    def orientation(
        self,
        expected_original: FileIdentity,
        expected_clone: FileIdentity,
    ) -> Orientation: ...

    def swap_forward(
        self,
        expected_original: FileIdentity,
        expected_clone: FileIdentity,
    ) -> None: ...

    def revalidate_pre_forward(
        self,
        expected_source: FileSnapshot,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None: ...

    def postverify(
        self,
        expected_source: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> FileSnapshot: ...

    def revalidate_forward(
        self,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None: ...

    def revalidate_before_cleanup(
        self,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None: ...

    def revalidate_committed(self, expected_clone: FileSnapshot) -> None: ...

    def revalidate_rolled_back(self, expected_original: FileSnapshot) -> None: ...

    def unlink_original(
        self,
        expected_original: FileIdentity,
        expected_clone: FileSnapshot,
    ) -> None: ...

    def swap_back(
        self,
        expected_clone: FileIdentity,
        expected_original: FileIdentity,
    ) -> None: ...

    def unlink_clone(
        self,
        expected_clone: FileIdentity,
        expected_original: FileSnapshot,
    ) -> None: ...

    def sync_namespaces(self) -> None: ...

    def cleanup_stage(self) -> None: ...

    def abort_before_prepared(self) -> None: ...

    def close(self) -> None: ...


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FatalRepairError("expected JSON object")
    return value


def canonical_rollout_id(value: str) -> str:
    lowered = value.lower()
    if not CANONICAL_UUID_PATTERN.fullmatch(lowered):
        raise ValueError(f"not a canonical rollout UUID: {value}")
    try:
        parsed = uuid.UUID(lowered)
    except ValueError as error:
        raise ValueError(f"not a canonical rollout UUID: {value}") from error
    if str(parsed) != lowered:
        raise ValueError(f"not a canonical rollout UUID: {value}")
    return lowered


def rollout_id_from_name(name: str) -> Optional[str]:
    matched = ROLLOUT_ID_PATTERN.fullmatch(name)
    if matched is None:
        return None
    try:
        return canonical_rollout_id(matched.group("id"))
    except ValueError:
        return None


def _identity_matches(snapshot: FileSnapshot, identity: FileIdentity) -> bool:
    return snapshot.identity == identity


def _protected_equal(first: FileSnapshot, second: FileSnapshot) -> bool:
    return (
        first.size == second.size
        and first.mtime_ns == second.mtime_ns
        and first.nlink == 1
        and second.nlink == 1
        and first.content_sha256 == second.content_sha256
        and first.policy == second.policy
    )


def _exclusive_writer_policy_error(snapshot: FileSnapshot) -> Optional[str]:
    policy = snapshot.policy
    if policy.uid != os.geteuid():
        return "mirror owner is not the effective repair user"
    if policy.mode & 0o022:
        return "mirror is writable by its group or by other users"
    if policy.acl_sha256 != EMPTY_ACL_SHA256:
        return "mirror has an extended ACL"
    if policy.flags & ~SAFE_EXCLUSIVE_WRITER_FLAGS:
        return "mirror has blocking, internal, or unknown BSD flags"
    return None


def _snapshot_stable(first: FileSnapshot, second: FileSnapshot) -> bool:
    return first == second


def _safe_relative_path(value: str) -> pathlib.PurePath:
    candidate = pathlib.PurePath(value)
    if (
        not value
        or candidate.is_absolute()
        or "\x00" in value
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise FatalRepairError(f"unsafe relative path in durable state: {value!r}")
    return candidate


def _safe_component(value: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\x00" in value:
        raise FatalRepairError(f"unsafe path component in durable state: {value!r}")
    return value


def _manifest_paths_unchecked(
    manifest: Any,
) -> Tuple[pathlib.PurePath, pathlib.PurePath, pathlib.PurePath]:
    source_rel = _safe_relative_path(manifest.source_rel)
    final_rel = _safe_relative_path(manifest.final_rel)
    temporary_rel = _safe_relative_path(manifest.temporary_rel)
    temporary_parts = temporary_rel.parts
    if len(temporary_parts) < 2:
        raise FatalRepairError("durable manifest temporary path has no private stage")
    stage_name = temporary_parts[-2]
    temporary_name = temporary_parts[-1]
    if (
        stage_name != f".codex-reflink-repair-{manifest.txid}"
        or temporary_name != "clone"
    ):
        raise FatalRepairError("durable manifest contains a non-tool temporary path")
    final_parent_parts = final_rel.parts[:-1]
    if temporary_parts[:-2] != final_parent_parts:
        raise FatalRepairError(
            "durable manifest stage is outside the destination parent"
        )
    return source_rel, final_rel, temporary_rel


def _manifest_paths(
    config: Config, manifest: RepairManifest
) -> Tuple[Optional[pathlib.Path], pathlib.Path, pathlib.Path]:
    source_rel, final_rel, temporary_rel = _manifest_paths_unchecked(manifest)
    source = config.codex_root.joinpath(*source_rel.parts)
    final = config.mirror_root.joinpath(*final_rel.parts)
    temporary = config.mirror_root.joinpath(*temporary_rel.parts)
    return source, final, temporary


_DARWIN_BACKEND_MODULE: Optional[Any] = None
_STATE_HARDENER: Optional[Any] = None


def _load_darwin_backend_module() -> Any:
    global _DARWIN_BACKEND_MODULE
    if _DARWIN_BACKEND_MODULE is not None:
        return _DARWIN_BACKEND_MODULE
    try:
        import codex_reflink_darwin as backend_module
    except ImportError:
        import importlib.util

        backend_path = pathlib.Path(__file__).with_name("codex_reflink_darwin.py")
        specification = importlib.util.spec_from_file_location(
            "codex_reflink_darwin", backend_path
        )
        if specification is None or specification.loader is None:
            raise FatalRepairError("cannot load the Darwin reflink backend")
        backend_module = importlib.util.module_from_spec(specification)
        sys.modules.setdefault("codex_reflink_darwin", backend_module)
        specification.loader.exec_module(backend_module)
    _DARWIN_BACKEND_MODULE = backend_module
    return backend_module


def _harden_private_state_fd(descriptor: int, *, is_directory: bool) -> None:
    """Converge access policy on a held state object, never on a pathname."""

    global _STATE_HARDENER
    if sys.platform == "darwin":
        try:
            if _STATE_HARDENER is None:
                module = _load_darwin_backend_module()
                _STATE_HARDENER = module.DarwinBackend()
            _STATE_HARDENER.harden_private_state_fd(
                descriptor, is_directory=is_directory
            )
            return
        except Exception as error:
            raise FatalRepairError(
                f"cannot harden private state object: {error}"
            ) from error

    try:
        metadata = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if is_directory else stat.S_ISREG
        if not expected_kind(metadata.st_mode):
            raise FatalRepairError("private state object has the wrong type")
        if metadata.st_uid != os.geteuid():
            raise FatalRepairError("private state object is not owned by current user")
        if not is_directory and metadata.st_nlink != 1:
            raise FatalRepairError("private state file is not singly linked")
        os.fchmod(descriptor, 0o700 if is_directory else 0o600)
        os.fsync(descriptor)
        verified = os.fstat(descriptor)
        if (
            not expected_kind(verified.st_mode)
            or verified.st_uid != os.geteuid()
            or (not is_directory and verified.st_nlink != 1)
            or stat.S_IMODE(verified.st_mode) != (0o700 if is_directory else 0o600)
        ):
            raise FatalRepairError("private state policy did not converge")
    except FatalRepairError:
        raise
    except OSError as error:
        raise FatalRepairError(
            f"cannot harden private state object: {error}"
        ) from error


def _validate_private_state_fd(descriptor: int, *, is_directory: bool) -> None:
    """Non-mutating held-FD access-policy verification."""

    if sys.platform == "darwin":
        try:
            if _STATE_HARDENER is None:
                raise FatalRepairError("private state hardener is not initialized")
            _STATE_HARDENER.validate_private_state_fd(
                descriptor, is_directory=is_directory
            )
            return
        except FatalRepairError:
            raise
        except Exception as error:
            raise FatalRepairError(
                f"private state access policy is unsafe: {error}"
            ) from error
    metadata = os.fstat(descriptor)
    expected_kind = stat.S_ISDIR if is_directory else stat.S_ISREG
    if (
        not expected_kind(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (not is_directory and metadata.st_nlink != 1)
        or stat.S_IMODE(metadata.st_mode) != (0o700 if is_directory else 0o600)
    ):
        raise FatalRepairError("private state access policy is unsafe")


def _drain_owned_fds(
    descriptors: Iterable[int],
    *,
    primary_error: Optional[BaseException] = None,
    durable_namespace_complete: bool = False,
) -> None:
    """Close every transferred FD without overriding primary/durable results."""

    close_error: Optional[BaseException] = None
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except BaseException as error:
            if close_error is None:
                close_error = error
    if (
        close_error is not None
        and primary_error is None
        and not durable_namespace_complete
    ):
        raise close_error


@dataclasses.dataclass
class _CleanupDiagnostic:
    """Inspectable secondary cleanup evidence attached to a primary error."""

    detail: str
    error: BaseException
    receipt: Optional[Any] = None


def _attach_cleanup_diagnostic(
    primary_error: BaseException,
    *,
    detail: str,
    cleanup_error: BaseException,
    receipt: Optional[Any] = None,
) -> None:
    diagnostic = _CleanupDiagnostic(detail, cleanup_error, receipt)
    existing = getattr(primary_error, "_cleanup_diagnostics", ())
    if not isinstance(existing, tuple):
        existing = ()
    if not any(
        candidate.error is cleanup_error and candidate.receipt is receipt
        for candidate in existing
        if isinstance(candidate, _CleanupDiagnostic)
    ):
        setattr(primary_error, "_cleanup_diagnostics", existing + (diagnostic,))
    add_note = getattr(primary_error, "add_note", None)
    if callable(add_note):
        notes = getattr(primary_error, "__notes__", ())
        if detail not in notes:
            add_note(detail)


class _DescriptorCloseDisposition(enum.Enum):
    """Describe whether a raw descriptor may still be closed safely."""

    OWNED_PENDING = "owned-pending"
    CALL_OUTCOME_UNPROVED = "call-outcome-unproved"
    CLOSED_PROVED = "closed-proved"


@dataclasses.dataclass(frozen=True)
class _DescriptorCloseAttempt:
    """Atomically pair a close disposition with its dispatch count."""

    disposition: _DescriptorCloseDisposition
    dispatches: int


@dataclasses.dataclass
class _DescriptorCloseReceipt:
    """Keep one local FD ownership generation inspectable through close."""

    descriptor: int
    attempt: _DescriptorCloseAttempt = dataclasses.field(
        default_factory=lambda: _DescriptorCloseAttempt(
            _DescriptorCloseDisposition.OWNED_PENDING,
            0,
        )
    )
    error: Optional[BaseException] = None

    @property
    def disposition(self) -> _DescriptorCloseDisposition:
        return self.attempt.disposition

    @property
    def dispatches(self) -> int:
        return self.attempt.dispatches

    @property
    def completed(self) -> bool:
        return self.disposition is _DescriptorCloseDisposition.CLOSED_PROVED


@dataclasses.dataclass
class _DescriptorState:
    """Shared, destructor-backed ownership for one descriptor."""

    subject: str
    descriptor: int = -1
    close_receipt: Optional[_DescriptorCloseReceipt] = None

    def _close_receipt_for_current_descriptor(
        self,
    ) -> Optional[_DescriptorCloseReceipt]:
        descriptor = self.descriptor
        receipt = self.close_receipt
        if descriptor < 0:
            return receipt
        if receipt is None:
            receipt = _DescriptorCloseReceipt(descriptor)
            self.close_receipt = receipt
            return receipt
        if receipt.descriptor != descriptor:
            raise FatalRepairError(
                f"{self.subject} has conflicting descriptor close generations"
            )
        return receipt

    def _close_once(self, receipt: _DescriptorCloseReceipt) -> None:
        if receipt.completed:
            return
        if receipt.disposition is not _DescriptorCloseDisposition.OWNED_PENDING:
            if self.descriptor == receipt.descriptor:
                self.descriptor = -1
            return
        try:
            _cleanup_guard = True
            receipt.attempt = _DescriptorCloseAttempt(
                _DescriptorCloseDisposition.CALL_OUTCOME_UNPROVED,
                receipt.dispatches + 1,
            )
            if self.descriptor == receipt.descriptor:
                self.descriptor = -1
            os.close(receipt.descriptor)
            receipt.attempt = _DescriptorCloseAttempt(
                _DescriptorCloseDisposition.CLOSED_PROVED,
                receipt.dispatches,
            )
        except BaseException as error:
            try:
                _cleanup_guard = True
                if receipt.error is None:
                    receipt.error = error
            except BaseException:
                if receipt.error is None:
                    receipt.error = error
            raise

    def close(
        self,
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        receipt = self.close_receipt
        if self.descriptor < 0 and (receipt is None or receipt.completed):
            return
        first_cleanup_error: Optional[BaseException] = None
        try:
            _cleanup_guard = True
            receipt = self._close_receipt_for_current_descriptor()
            if receipt is None:
                return
            self._close_once(receipt)
        except BaseException as error:
            try:
                _cleanup_guard = True
                receipt = self._close_receipt_for_current_descriptor()
                if receipt is None:
                    first_cleanup_error = error
                    raise
                if receipt.error is None:
                    receipt.error = error
                first_cleanup_error = (
                    receipt.error if receipt.error is not None else error
                )
                if receipt.disposition is _DescriptorCloseDisposition.OWNED_PENDING:
                    self._close_once(receipt)
                elif self.descriptor == receipt.descriptor:
                    self.descriptor = -1
            except BaseException:
                if first_cleanup_error is None:
                    first_cleanup_error = (
                        receipt.error
                        if receipt is not None and receipt.error is not None
                        else error
                    )
                try:
                    receipt = self._close_receipt_for_current_descriptor()
                    if receipt is not None:
                        if (
                            receipt.disposition
                            is _DescriptorCloseDisposition.OWNED_PENDING
                        ):
                            self._close_once(receipt)
                        elif self.descriptor == receipt.descriptor:
                            self.descriptor = -1
                except BaseException:
                    pass
        authoritative_error = (
            receipt.error
            if receipt is not None and receipt.error is not None
            else first_cleanup_error
        )
        if authoritative_error is not None and primary_error is not None:
            try:
                _cleanup_guard = True
                detail = (
                    f"{self.subject} close was not clean "
                    f"({receipt.disposition.value if receipt is not None else 'unproved'}): "
                    f"{authoritative_error}"
                )
                _attach_cleanup_diagnostic(
                    primary_error,
                    detail=detail,
                    cleanup_error=authoritative_error,
                    receipt=receipt,
                )
            except BaseException:
                detail = (
                    f"{self.subject} close was not clean "
                    f"({receipt.disposition.value if receipt is not None else 'unproved'}): "
                    f"{authoritative_error}"
                )
                _attach_cleanup_diagnostic(
                    primary_error,
                    detail=detail,
                    cleanup_error=authoritative_error,
                    receipt=receipt,
                )
        if (
            authoritative_error is not None
            and primary_error is None
            and not durable_namespace_complete
        ):
            raise authoritative_error

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            return


class _OwnedDescriptor:
    """Lazy descriptor owner spanning Python-visible acquisition handoffs."""

    def __init__(
        self,
        subject: str,
        acquire: Optional[Callable[["_OwnedDescriptor"], None]] = None,
    ) -> None:
        self._subject = subject
        self._acquire = acquire
        self._state = _DescriptorState(subject)
        self._entered = False
        self._retain_if_registered: Optional[Callable[[], bool]] = None

    @property
    def closed(self) -> bool:
        return self._state.descriptor < 0

    def fileno(self) -> int:
        descriptor = self._state.descriptor
        if descriptor < 0:
            raise FatalRepairError(f"{self._subject} descriptor is closed")
        return descriptor

    def _adopt(self, descriptor: int) -> None:
        if (
            not isinstance(descriptor, int)
            or isinstance(descriptor, bool)
            or descriptor < 0
        ):
            raise FatalRepairError(f"{self._subject} returned an invalid descriptor")
        if not self.closed:
            raise FatalRepairError(f"{self._subject} descriptor is already owned")
        if (
            self._state.close_receipt is not None
            and not self._state.close_receipt.completed
        ):
            raise FatalRepairError(
                f"{self._subject} prior descriptor close disposition is unproved"
            )
        self._state.close_receipt = None
        self._state.descriptor = descriptor

    def _share_from(self, other: "_OwnedDescriptor") -> None:
        if not isinstance(other, _OwnedDescriptor) or other.closed:
            raise FatalRepairError(f"{self._subject} received a closed owner")
        if not self.closed:
            raise FatalRepairError(f"{self._subject} descriptor is already owned")
        self._state = other._state

    def retain_if_registered(self, predicate: Callable[[], bool]) -> "_OwnedDescriptor":
        if not callable(predicate):
            raise FatalRepairError(
                f"{self._subject} registration predicate is not callable"
            )
        self._retain_if_registered = predicate
        return self

    def _should_retain_on_exception(self) -> bool:
        predicate = self._retain_if_registered
        if predicate is None:
            return False
        for attempt in range(2):
            try:
                registered = predicate()
                if type(registered) is not bool:
                    raise FatalRepairError(
                        f"{self._subject} registration predicate returned a non-boolean"
                    )
                return registered
            except BaseException:
                if attempt == 0:
                    continue
                # Registration probing is cleanup-only.  Conservatively retain:
                # a registered outer owner will drain this state, while an
                # unregistered last reference has only the destructor fallback.
                return True
        raise AssertionError("unreachable registration probe state")

    def close(
        self,
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        self._state.close(
            primary_error=primary_error,
            durable_namespace_complete=durable_namespace_complete,
        )

    def __enter__(self) -> "_OwnedDescriptor":
        if self._entered:
            raise FatalRepairError(f"{self._subject} owner was re-entered")
        self._entered = True
        try:
            if self._acquire is not None:
                self._acquire(self)
            if self.closed:
                raise FatalRepairError(
                    f"{self._subject} acquisition returned no descriptor"
                )
            return self
        except BaseException as primary_error:
            try:
                _cleanup_guard = True
                _drain_descriptor_owners((self,), primary_error=primary_error)
            except BaseException:
                _drain_descriptor_owners((self,), primary_error=primary_error)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            _cleanup_guard = True
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if exc_type is None:
                self._retain_if_registered = None
                return
            retain = self._should_retain_on_exception()
        except BaseException as cleanup_error:
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            try:
                retain = self._should_retain_on_exception()
            except BaseException:
                retain = True
            if exc_type is None:
                if not retain:
                    _drain_descriptor_owners((self,), primary_error=cleanup_error)
                raise
        try:
            _cleanup_guard = True
            if retain:
                return
            _drain_descriptor_owners((self,), primary_error=primary_error)
        except BaseException as cleanup_error:
            if not retain:
                _drain_descriptor_owners(
                    (self,),
                    primary_error=(
                        primary_error if primary_error is not None else cleanup_error
                    ),
                )
            if primary_error is None:
                raise


@contextlib.contextmanager
def _closing_descriptor(
    owner: _OwnedDescriptor, *, durable_namespace_complete: bool = False
) -> Iterator[int]:
    """Acquire one owner and close it before its active context can exit."""

    with owner:
        try:
            yield owner.fileno()
        except BaseException as error:
            try:
                _cleanup_guard = True
                _drain_descriptor_owners(
                    (owner,),
                    primary_error=error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            except BaseException:
                _drain_descriptor_owners(
                    (owner,),
                    primary_error=error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            raise
        else:
            try:
                _cleanup_guard = True
                _drain_descriptor_owners(
                    (owner,), durable_namespace_complete=durable_namespace_complete
                )
            except BaseException as cleanup_error:
                _drain_descriptor_owners(
                    (owner,),
                    primary_error=cleanup_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
                if not durable_namespace_complete:
                    raise


def _open_fd_owned(
    path: str,
    flags: int,
    mode: int = 0o777,
    *,
    dir_fd: Optional[int] = None,
    subject: str,
    on_adopt: Optional[Callable[[_OwnedDescriptor], None]] = None,
) -> _OwnedDescriptor:
    """Return a lazy owner whose acquire calls os.open directly."""

    def acquire(target: _OwnedDescriptor) -> None:
        handed_off = -1
        adopt_hook_complete = on_adopt is None
        try:
            if dir_fd is None:
                handed_off = os.open(path, flags, mode)
            else:
                handed_off = os.open(path, flags, mode, dir_fd=dir_fd)
            target._adopt(handed_off)
            if on_adopt is not None:
                on_adopt(target)
                adopt_hook_complete = True
        except BaseException as primary_error:
            if handed_off >= 0 and target.closed:
                try:
                    target._adopt(handed_off)
                except BaseException:
                    _drain_owned_fds((handed_off,), primary_error=primary_error)
            if on_adopt is not None and not adopt_hook_complete and not target.closed:
                try:
                    on_adopt(target)
                except BaseException:
                    pass
            raise

    return _OwnedDescriptor(subject, acquire)


def _open_directory_nofollow_owned(
    path: pathlib.Path, *, create: bool, private_final: bool
) -> _OwnedDescriptor:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise FatalRepairError(f"state directory must be absolute: {path}")
    if private_final and absolute == pathlib.Path("/"):
        raise FatalRepairError("filesystem root cannot be used as private state")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def acquire(target: _OwnedDescriptor) -> None:
        owners: List[_OwnedDescriptor] = []
        cleanup_owners: Tuple[_OwnedDescriptor, ...] = ()
        current_owner = _open_fd_owned("/", flags, subject="filesystem root")
        owners.append(current_owner)
        try:
            try:
                with current_owner:
                    current_fd = current_owner.fileno()
            except OSError as error:
                raise FatalRepairError(
                    f"cannot open filesystem root: {error}"
                ) from error
            parts = absolute.parts[1:]
            for index, component in enumerate(parts):
                if component in ("", ".", "..") or "/" in component:
                    raise FatalRepairError(
                        f"unsafe state directory component: {component!r}"
                    )
                is_final = index == len(parts) - 1
                next_owner = _open_fd_owned(
                    component,
                    flags,
                    dir_fd=current_fd,
                    subject=f"state directory component {component!r}",
                )
                owners.append(next_owner)
                try:
                    with next_owner:
                        next_fd = next_owner.fileno()
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                        os.fsync(current_fd)
                        next_owner = _open_fd_owned(
                            component,
                            flags,
                            dir_fd=current_fd,
                            subject=f"created state directory {component!r}",
                        )
                        owners.append(next_owner)
                        with next_owner:
                            next_fd = next_owner.fileno()
                    except OSError as error:
                        raise FatalRepairError(
                            f"cannot create private state directory {absolute}: {error}"
                        ) from error
                except OSError as error:
                    raise FatalRepairError(
                        "cannot resolve state directory without symlinks "
                        f"{absolute}: {error}"
                    ) from error
                previous_owner = current_owner
                current_owner = next_owner
                current_fd = next_fd
                try:
                    try:
                        _cleanup_guard = True
                        _drain_descriptor_owners((previous_owner,))
                    except BaseException as cleanup_error:
                        _drain_descriptor_owners(
                            (previous_owner,), primary_error=cleanup_error
                        )
                        raise
                except OSError as error:
                    raise FatalRepairError(
                        f"cannot close previous state directory component: {error}"
                    ) from error
                metadata = os.fstat(current_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise FatalRepairError(f"state path is not a directory: {absolute}")
                if is_final and private_final:
                    _harden_private_state_fd(current_fd, is_directory=True)
            target._share_from(current_owner)
        except BaseException as primary_error:
            try:
                _cleanup_guard = True
                cleanup_owners = tuple(reversed(owners))
                _drain_descriptor_owners(cleanup_owners, primary_error=primary_error)
            except BaseException:
                if not cleanup_owners:
                    cleanup_owners = tuple(reversed(owners))
                _drain_descriptor_owners(cleanup_owners, primary_error=primary_error)
            raise

    return _OwnedDescriptor(f"state directory {absolute}", acquire)


def _open_directory_nofollow(
    path: pathlib.Path, *, create: bool, private_final: bool
) -> _OwnedDescriptor:
    """Compatibility name returning the safe lazy owner, never a raw FD."""

    return _open_directory_nofollow_owned(
        path, create=create, private_final=private_final
    )


class StateDirectory:
    """Lazy context owner for one identity-bound state directory."""

    def __init__(self, path: pathlib.Path, *, create: bool) -> None:
        self.path = path.absolute()
        self.create = create
        self._owner: Optional[_OwnedDescriptor] = None
        self.identity: Optional[Tuple[int, int]] = None
        self._entered = False

    @property
    def descriptor(self) -> int:
        owner = self._owner
        return -1 if owner is None or owner.closed else owner.fileno()

    def release(self) -> Optional[_OwnedDescriptor]:
        """Return the registered owner receipt without creating a raw gap."""

        owner = self._owner
        if owner is None or owner.closed:
            return None
        # The StateDirectory slot remains the authoritative registration until
        # the returned owner is closed.  A return-event interruption therefore
        # cannot leave a live descriptor owned only by a raw integer.
        return owner

    def __enter__(self) -> "StateDirectory":
        if self._entered:
            raise FatalRepairError(f"state directory owner was re-entered: {self.path}")
        self._entered = True
        _revalidate_active_state_root()
        owner = _open_directory_nofollow_owned(
            self.path, create=self.create, private_final=True
        )
        try:
            with owner:
                owner.retain_if_registered(lambda: self._owner is owner)
                metadata = os.fstat(owner.fileno())
                self._owner = owner
                self.identity = (metadata.st_dev, metadata.st_ino)
                self.revalidate()
                _revalidate_active_state_root()
            return self
        except BaseException as error:
            try:
                _cleanup_guard = True
                cleanup_owner = self if self._owner is owner else owner
                _drain_descriptor_owners((cleanup_owner,), primary_error=error)
            except BaseException:
                cleanup_owner = self if self._owner is owner else owner
                _drain_descriptor_owners((cleanup_owner,), primary_error=error)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        retry_dispatched = False
        try:
            _cleanup_guard = True
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            _drain_descriptor_owners((self,), primary_error=primary_error)
        except BaseException as cleanup_error:
            try:
                _cleanup_guard = True
                primary_error = (
                    exc_value if isinstance(exc_value, BaseException) else None
                )
                # fmt: off
                retry_dispatched = True; _drain_descriptor_owners(  # noqa: E702
                    (self,),
                    primary_error=(
                        primary_error if primary_error is not None else cleanup_error
                    ),
                )
            # fmt: on
            except BaseException:
                primary_error = (
                    exc_value if isinstance(exc_value, BaseException) else None
                )
                if not retry_dispatched:
                    _drain_descriptor_owners(
                        (self,),
                        primary_error=(
                            primary_error
                            if primary_error is not None
                            else cleanup_error
                        ),
                    )
            if primary_error is None:
                raise cleanup_error

    def revalidate(self) -> None:
        _validate_private_state_fd(self.descriptor, is_directory=True)
        candidate_owner = _open_directory_nofollow_owned(
            self.path, create=False, private_final=False
        )
        with _closing_descriptor(candidate_owner) as candidate:
            metadata = os.fstat(candidate)
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                raise FatalRepairError(
                    f"private state directory mapping changed: {self.path}"
                )
            _validate_private_state_fd(candidate, is_directory=True)

    def close(
        self,
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        owner = self._owner
        retry_dispatched = False
        try:
            if owner is not None:
                owner.close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
        except BaseException as cleanup_error:
            try:
                _cleanup_guard = True
                if owner is None:
                    owner = self._owner
                if owner is not None:
                    # fmt: off
                    retry_dispatched = True; owner.close(  # noqa: E702
                        primary_error=(
                            primary_error if primary_error is not None else cleanup_error
                        ),
                        durable_namespace_complete=durable_namespace_complete,
                    )
                    # fmt: on
            except BaseException:
                if owner is None:
                    owner = self._owner
                if owner is not None and not retry_dispatched:
                    try:
                        owner.close(
                            primary_error=(
                                primary_error
                                if primary_error is not None
                                else cleanup_error
                            ),
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    except BaseException:
                        pass
            if primary_error is None and not durable_namespace_complete:
                raise cleanup_error
        finally:
            if owner is not None and owner.closed and self._owner is owner:
                self._owner = None

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            return


@dataclasses.dataclass
class _DrainPassReceipt:
    """Distinguish an interrupted pass from one that drained every owner."""

    completed: bool = False
    first_error: Optional[BaseException] = None


def _drain_descriptor_owners_once(
    owners: Sequence[Any],
    *,
    primary_error: Optional[BaseException] = None,
    durable_namespace_complete: bool = False,
    _receipt: Optional[_DrainPassReceipt] = None,
) -> None:
    receipt = _DrainPassReceipt() if _receipt is None else _receipt
    close_error: Optional[BaseException] = None
    for owner in owners:
        if owner is None:
            continue
        active_primary = primary_error if primary_error is not None else close_error
        retry_dispatched = False
        try:
            _cleanup_guard = True
            owner.close(
                primary_error=active_primary,
                durable_namespace_complete=durable_namespace_complete,
            )
        except BaseException as error:
            try:
                _cleanup_guard = True
                if close_error is None:
                    close_error = error
                if receipt.first_error is None:
                    receipt.first_error = error
                retry_primary = (
                    primary_error if primary_error is not None else close_error
                )
                # fmt: off
                retry_dispatched = True; owner.close(  # noqa: E702
                    primary_error=retry_primary,
                    durable_namespace_complete=durable_namespace_complete,
                )
            # fmt: on
            except BaseException:
                if close_error is None:
                    close_error = error
                if receipt.first_error is None:
                    receipt.first_error = error
                if not retry_dispatched:
                    retry_primary = (
                        primary_error if primary_error is not None else close_error
                    )
                    try:
                        owner.close(
                            primary_error=retry_primary,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    except BaseException:
                        pass
    receipt.completed = True
    if (
        close_error is not None
        and primary_error is None
        and not durable_namespace_complete
    ):
        raise close_error


def _drain_descriptor_owners(
    owners: Iterable[Any],
    *,
    primary_error: Optional[BaseException] = None,
    durable_namespace_complete: bool = False,
) -> None:
    """Drain every owner despite one handler interruption."""

    owned: Tuple[Any, ...]
    first_pass_receipt = _DrainPassReceipt()
    second_pass_dispatched = False
    try:
        owned = tuple(owners)
        _drain_descriptor_owners_once(
            owned,
            primary_error=primary_error,
            durable_namespace_complete=durable_namespace_complete,
            _receipt=first_pass_receipt,
        )
        return
    except BaseException as first_cleanup_error:
        try:
            _cleanup_guard = True
            authoritative_error = (
                first_pass_receipt.first_error
                if first_pass_receipt.first_error is not None
                else first_cleanup_error
            )
        except BaseException:
            authoritative_error = (
                first_pass_receipt.first_error
                if first_pass_receipt.first_error is not None
                else first_cleanup_error
            )
        if first_pass_receipt.completed:
            if primary_error is None and not durable_namespace_complete:
                raise authoritative_error
            return
        try:
            _cleanup_guard = True
            try:
                owned
            except UnboundLocalError:
                owned = tuple(owners)
            active_primary = (
                primary_error if primary_error is not None else authoritative_error
            )
            # fmt: off
            second_pass_dispatched = True; _drain_descriptor_owners_once(  # noqa: E702
                owned,
                primary_error=active_primary,
                durable_namespace_complete=durable_namespace_complete,
            )
        # fmt: on
        except BaseException:
            try:
                owned
            except UnboundLocalError:
                owned = tuple(owners)
            if not second_pass_dispatched:
                active_primary = (
                    primary_error if primary_error is not None else authoritative_error
                )
                _drain_descriptor_owners_once(
                    owned,
                    primary_error=active_primary,
                    durable_namespace_complete=durable_namespace_complete,
                )
        if primary_error is None and not durable_namespace_complete:
            raise authoritative_error


def _drain_primary_closeables_once(
    owners: Sequence[Any],
    *,
    primary_error: Optional[BaseException] = None,
    _receipt: Optional[_DrainPassReceipt] = None,
) -> None:
    """Close backend owners with one bounded retry and first-primary semantics."""

    receipt = _DrainPassReceipt() if _receipt is None else _receipt
    close_error: Optional[BaseException] = None
    for owner in owners:
        if owner is None:
            continue
        active_primary = primary_error if primary_error is not None else close_error
        retry_dispatched = False
        try:
            _cleanup_guard = True
            owner.close(primary_error=active_primary)
        except BaseException as error:
            try:
                _cleanup_guard = True
                if close_error is None:
                    close_error = error
                if receipt.first_error is None:
                    receipt.first_error = error
                retry_primary = (
                    primary_error if primary_error is not None else close_error
                )
                # fmt: off
                retry_dispatched = True; owner.close(primary_error=retry_primary)  # noqa: E702
            # fmt: on
            except BaseException:
                if close_error is None:
                    close_error = error
                if receipt.first_error is None:
                    receipt.first_error = error
                if not retry_dispatched:
                    retry_primary = (
                        primary_error if primary_error is not None else close_error
                    )
                    try:
                        owner.close(primary_error=retry_primary)
                    except BaseException:
                        pass
    receipt.completed = True
    if close_error is not None and primary_error is None:
        raise close_error


def _drain_primary_closeables(
    owners: Iterable[Any], *, primary_error: Optional[BaseException] = None
) -> None:
    """Drain backend owners despite one interruption entering the first pass."""

    owned: Tuple[Any, ...]
    first_pass_receipt = _DrainPassReceipt()
    second_pass_dispatched = False
    try:
        owned = tuple(owners)
        _drain_primary_closeables_once(
            owned, primary_error=primary_error, _receipt=first_pass_receipt
        )
        return
    except BaseException as first_cleanup_error:
        try:
            _cleanup_guard = True
            authoritative_error = (
                first_pass_receipt.first_error
                if first_pass_receipt.first_error is not None
                else first_cleanup_error
            )
        except BaseException:
            authoritative_error = (
                first_pass_receipt.first_error
                if first_pass_receipt.first_error is not None
                else first_cleanup_error
            )
        if first_pass_receipt.completed:
            if primary_error is None:
                raise authoritative_error
            return
        try:
            _cleanup_guard = True
            try:
                owned
            except UnboundLocalError:
                owned = tuple(owners)
            active_primary = (
                primary_error if primary_error is not None else authoritative_error
            )
            # fmt: off
            second_pass_dispatched = True; _drain_primary_closeables_once(  # noqa: E702
                owned, primary_error=active_primary
            )
        # fmt: on
        except BaseException:
            try:
                owned
            except UnboundLocalError:
                owned = tuple(owners)
            if not second_pass_dispatched:
                active_primary = (
                    primary_error if primary_error is not None else authoritative_error
                )
                _drain_primary_closeables_once(owned, primary_error=active_primary)
        if primary_error is None:
            raise authoritative_error


@dataclasses.dataclass
class _CleanupActionReceipt:
    """Record whether an identity-bound cleanup call was actually entered."""

    dispatches: int = 0
    completed: bool = False
    error: Optional[BaseException] = None

    @property
    def dispatch_entered(self) -> bool:
        return self.dispatches > 0


def _dispatch_cleanup_action(
    receipt: _CleanupActionReceipt, action: Callable[[], None]
) -> None:
    if receipt.completed:
        return
    if receipt.dispatches >= 2 or (
        receipt.dispatches > 0 and isinstance(receipt.error, Exception)
    ):
        if receipt.error is not None:
            raise receipt.error
        raise FatalRepairError(
            "cleanup action was entered without a completion disposition"
        )
    try:
        _cleanup_guard = True
        # Keep the dispatch count and call on one trace line.  A single
        # non-Exception interruption at the callee's first Python body point
        # permits one identity-bound retry; ordinary cleanup failures remain
        # authoritative and are not retried speculatively.
        # fmt: off
        receipt.dispatches += 1; action(); receipt.completed = True; receipt.error = None  # noqa: E702
    # fmt: on
    except BaseException as error:
        try:
            _cleanup_guard = True
            receipt.error = error
        except BaseException:
            # ``error`` is the action's authoritative first failure.  A single
            # handler interruption must not leave the receipt empty and permit
            # the destructive action to be dispatched again.
            receipt.error = error
        raise


def _complete_cleanup_action(
    receipt: _CleanupActionReceipt, action: Callable[[], None]
) -> Optional[BaseException]:
    """Enter one cleanup action despite one pre-dispatch interruption."""

    first_cleanup_error: Optional[BaseException] = None
    try:
        _cleanup_guard = True
        _dispatch_cleanup_action(receipt, action)
        return None
    except BaseException as cleanup_error:
        try:
            _cleanup_guard = True
            first_cleanup_error = cleanup_error
        except BaseException:
            # The receipt already records any entered action's authoritative
            # failure.  Preserve the same object when the one interruption
            # arrives before this local handoff, so callers can report it
            # before constructing their terminal failure.
            first_cleanup_error = cleanup_error
    if receipt.completed:
        return None
    if receipt.dispatches == 0 or (
        receipt.dispatches < 2
        and first_cleanup_error is not None
        and not isinstance(first_cleanup_error, Exception)
    ):
        try:
            _cleanup_guard = True
            _dispatch_cleanup_action(receipt, action)
            return None
        except BaseException as retry_error:
            return receipt.error if receipt.error is not None else retry_error
    if receipt.error is not None:
        return receipt.error
    return first_cleanup_error


_ACTIVE_STATE_ROOT: Optional[weakref.ReferenceType[StateDirectory]] = None
_ACTIVE_LOCK_DESCRIPTOR: Optional[int] = None


def _active_state_root() -> Optional[StateDirectory]:
    global _ACTIVE_LOCK_DESCRIPTOR, _ACTIVE_STATE_ROOT
    reference = _ACTIVE_STATE_ROOT
    if reference is None:
        return None
    root = reference()
    if root is None or root.descriptor < 0:
        # A handler interruption may have prevented explicit global cleanup,
        # but the weak registration must never retain a closed/stale owner.
        _ACTIVE_LOCK_DESCRIPTOR = None
        _ACTIVE_STATE_ROOT = None
        return None
    return root


def _revalidate_active_state_root() -> None:
    root = _active_state_root()
    if root is not None:
        root.revalidate()
        if _ACTIVE_LOCK_DESCRIPTOR is None:
            raise FatalRepairError("active state root has no bound repair lock")
        _validate_private_state_fd(_ACTIVE_LOCK_DESCRIPTOR, is_directory=False)
        _require_leaf_mapping(
            root.descriptor,
            "repair.lock",
            _ACTIVE_LOCK_DESCRIPTOR,
            label="repair lock",
        )


def _open_state_directory(path: pathlib.Path, *, create: bool) -> StateDirectory:
    _revalidate_active_state_root()
    return StateDirectory(path, create=create)


def _revalidate_state_handles(*handles: StateDirectory) -> None:
    _revalidate_active_state_root()
    for handle in handles:
        handle.revalidate()
    _revalidate_active_state_root()


def _require_leaf_mapping(
    parent_fd: int, name: str, descriptor: int, *, label: str
) -> None:
    held = os.fstat(descriptor)
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise FatalRepairError(f"cannot revalidate {label} mapping: {error}") from error
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        raise FatalRepairError(f"{label} mapping changed")


def _ensure_private_directory(path: pathlib.Path) -> None:
    state_context = _open_state_directory(path, create=True)
    primary_error: Optional[BaseException] = None
    try:
        with state_context:
            state_context.revalidate()
    except BaseException as error:
        try:
            _cleanup_guard = True
            primary_error = error
            state_context.close(primary_error=error)
        except BaseException:
            primary_error = error
            state_context.close(primary_error=error)
        raise
    finally:
        try:
            _cleanup_guard = True
            state_context.close(primary_error=primary_error)
        except BaseException as cleanup_error:
            state_context.close(
                primary_error=(
                    primary_error if primary_error is not None else cleanup_error
                )
            )
            if primary_error is None:
                raise


def _read_fd_bounded(descriptor: int, size: int) -> bytes:
    if size < 0 or size > MAX_STATE_JSON_BYTES:
        raise FatalRepairError(f"canonical state exceeds {MAX_STATE_JSON_BYTES} bytes")
    chunks: List[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(65536, size - offset), offset)
        if not chunk:
            raise FatalRepairError("canonical state was truncated while reading")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise FatalRepairError("canonical state grew while reading")
    return b"".join(chunks)


def _state_identity_policy(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        int(getattr(metadata, "st_flags", 0)),
    )


def _state_generation(metadata: os.stat_result) -> Tuple[int, ...]:
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _probe_state_leaf(
    parent_fd: int, name: str, expected_payload: bytes
) -> Optional[Tuple[Tuple[int, int], bool]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    owner = _open_fd_owned(
        name,
        flags,
        dir_fd=parent_fd,
        subject=f"state publication probe {name!r}",
    )
    try:
        with _closing_descriptor(owner, durable_namespace_complete=True) as descriptor:
            return _probe_open_state_leaf(parent_fd, name, expected_payload, descriptor)
    except FileNotFoundError:
        return None
    except (OSError, FatalRepairError):
        return ((-1, -1), False)


def _probe_open_state_leaf(
    parent_fd: int, name: str, expected_payload: bytes, descriptor: int
) -> Tuple[Tuple[int, int], bool]:
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size > MAX_STATE_JSON_BYTES
        ):
            return ((before.st_dev, before.st_ino), False)
        raw = _read_fd_bounded(descriptor, before.st_size)
        after = os.fstat(descriptor)
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return ((after.st_dev, after.st_ino), False)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
        )
        mapped = (named.st_dev, named.st_ino) == (after.st_dev, after.st_ino)
        return (
            (after.st_dev, after.st_ino),
            stable and mapped and raw == expected_payload,
        )
    except (OSError, FatalRepairError):
        return ((-1, -1), False)


def _probe_atomic_publication(
    parent_fd: int,
    canonical_name: str,
    temporary_name: str,
    temporary_identity: Optional[Tuple[int, int]],
    encoded: bytes,
    *,
    directory_synced: bool,
) -> AtomicPublication:
    if temporary_identity is None:
        return AtomicPublication.NOT_PUBLISHED
    canonical = _probe_state_leaf(parent_fd, canonical_name, encoded)
    if canonical is not None and canonical == (temporary_identity, True):
        return (
            AtomicPublication.DURABLE
            if directory_synced
            else AtomicPublication.PUBLISHED_UNSYNCED
        )
    try:
        temporary = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        temporary = None
    if (
        temporary is not None
        and (temporary.st_dev, temporary.st_ino) == temporary_identity
        and stat.S_ISREG(temporary.st_mode)
        and temporary.st_nlink == 1
    ):
        # The captured inode still has its private temporary name.  It cannot
        # simultaneously be the singly-linked canonical publication, even if
        # its payload is only partially written.
        return AtomicPublication.NOT_PUBLISHED
    return AtomicPublication.AMBIGUOUS


def _unlink_atomic_temporary_once(
    parent_fd: int, name: str, identity: Tuple[int, int]
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        # A previous pass may have removed the exact inode before an
        # interruption reached the namespace durability fence.
        os.fsync(parent_fd)
        return
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise FatalRepairError("atomic temporary identity changed before cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _unlink_atomic_temporary(
    parent_fd: int, name: str, identity: Tuple[int, int]
) -> None:
    """Identity-bound unlink with one bounded cleanup retry."""

    first_cleanup_error: Optional[BaseException] = None
    try:
        try:
            _cleanup_guard = True
            _unlink_atomic_temporary_once(parent_fd, name, identity)
            return
        except BaseException as cleanup_error:
            first_cleanup_error = cleanup_error
    except BaseException as handler_interrupt:
        # The single interruption may arrive on the first handler line before
        # ``cleanup_error`` is stored.  The second identity check remains the
        # authority for whether the same temporary may be removed.
        if first_cleanup_error is None:
            first_cleanup_error = handler_interrupt
    try:
        _cleanup_guard = True
        _unlink_atomic_temporary_once(parent_fd, name, identity)
    except BaseException as retry_error:
        if first_cleanup_error is not None and isinstance(retry_error, Exception):
            raise first_cleanup_error
        # A non-Exception here is the one asynchronous interruption, not a
        # proved identity-cleanup disposition.  Let the outer action receipt
        # perform its bounded second dispatch instead of converting it into the
        # earlier ordinary failure and permanently suppressing compensation.
        raise


def _encode_json_payload(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            + "\n"
        ).encode("utf-8")
    except Exception as error:
        raise FatalRepairError(f"cannot encode durable JSON: {error}") from error
    if len(encoded) > MAX_STATE_JSON_BYTES:
        raise FatalRepairError(f"durable JSON exceeds {MAX_STATE_JSON_BYTES} bytes")
    return encoded


def _atomic_write_failure(
    path: pathlib.Path,
    error: BaseException,
    publication: AtomicPublication,
    *,
    mapping_error: Optional[Exception],
    cleanup_error: Optional[BaseException],
    classification_error: Optional[BaseException],
    reporting_error: Optional[BaseException] = None,
    cleanup_receipt: Optional[_CleanupActionReceipt] = None,
) -> BaseException:
    detail = f"cannot persist {path}: {error}"
    if mapping_error is not None:
        detail += f"; state directory mapping changed: {mapping_error}"
    if cleanup_error is not None:
        detail += f"; temporary cleanup was not proved: {cleanup_error}"
    if classification_error is not None:
        detail += (
            f"; publication classification was interrupted: {classification_error}"
        )
    if reporting_error is not None:
        detail += f"; failure reporting was interrupted: {reporting_error}"
    if isinstance(error, Exception):
        return AtomicWriteError(detail, publication=publication)
    return AtomicWriteInterruption(
        error,
        publication=publication,
        detail=detail,
        cleanup_error=cleanup_error,
        cleanup_receipt=cleanup_receipt,
    )


def atomic_write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> DurableFence:
    """Durably replace one canonical JSON file and classify every failure."""

    try:
        encoded = _encode_json_payload(payload)
    except FatalRepairError as error:
        raise AtomicWriteError(
            f"cannot encode {path}: {error}",
            publication=AtomicPublication.NOT_PUBLISHED,
        ) from error
    _safe_component(path.name)
    receipt = _AtomicWriteReceipt()
    parent_context = _open_state_directory(path.parent, create=True)
    try:
        with parent_context as parent:
            return _atomic_write_json_with_parent(
                path, encoded, parent, receipt=receipt
            )
    except (AtomicWriteError, AtomicWriteInterruption):
        raise
    except BaseException as error:
        if isinstance(error, Exception):
            raise AtomicWriteError(
                f"cannot persist {path}: {error}",
                publication=receipt.publication,
            ) from error
        raise AtomicWriteInterruption(error, publication=receipt.publication) from error


def _atomic_write_json_with_parent(
    path: pathlib.Path,
    encoded: bytes,
    parent: StateDirectory,
    *,
    receipt: _AtomicWriteReceipt,
) -> DurableFence:
    parent_fd = parent.descriptor
    temporary_name = f".{path.name}.tmp.{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    temporary_identity: Optional[Tuple[int, int]] = None

    def capture_temporary_identity(owner: _OwnedDescriptor) -> None:
        nonlocal temporary_identity
        metadata = os.fstat(owner.fileno())
        temporary_identity = (metadata.st_dev, metadata.st_ino)

    descriptor_owner = _open_fd_owned(
        temporary_name,
        flags,
        0o600,
        dir_fd=parent_fd,
        subject=f"atomic temporary {temporary_name!r}",
        on_adopt=capture_temporary_identity,
    )
    replace_attempted = False
    replace_returned = False
    directory_synced = False
    publication_verified = False
    primary_error: Optional[BaseException] = None
    publication = receipt.publication
    mapping_error: Optional[Exception] = None
    cleanup_error: Optional[BaseException] = None
    classification_error: Optional[BaseException] = None
    failure_reporting_error: Optional[BaseException] = None
    temporary_cleanup_receipt = _CleanupActionReceipt()

    def cleanup_temporary() -> None:
        if temporary_identity is None:
            raise FatalRepairError("atomic temporary identity was not captured")
        _unlink_atomic_temporary(parent_fd, temporary_name, temporary_identity)

    try:
        _revalidate_state_handles(parent)
        with _closing_descriptor(descriptor_owner) as descriptor:
            _harden_private_state_fd(descriptor, is_directory=False)
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise OSError("short write while persisting JSON")
                written += count
            os.fsync(descriptor)
        _revalidate_state_handles(parent)
        # Once namespace replacement may be entered, an interruption in the
        # later classification handler must not retain the default
        # NOT_PUBLISHED receipt.  Exact probes refine this conservative state.
        receipt.publication = AtomicPublication.AMBIGUOUS
        replace_attempted = True
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        replace_returned = True
        _revalidate_state_handles(parent)
        os.fsync(parent_fd)
        directory_synced = True
        _revalidate_state_handles(parent)
        publication_probe = _probe_state_leaf(parent_fd, path.name, encoded)
        _revalidate_state_handles(parent)
        if temporary_identity is None or publication_probe != (
            temporary_identity,
            True,
        ):
            raise FatalRepairError(
                "canonical state post-publication identity or payload mismatch"
            )
        publication_verified = True
        assert temporary_identity is not None
        assert parent.identity is not None
        fence = DurableFence(
            path=path.absolute(),
            parent_identity=FileIdentity(*parent.identity),
            leaf_identity=FileIdentity(*temporary_identity),
            encoded_payload=encoded,
        )
        receipt.publication = AtomicPublication.DURABLE
        receipt.fence = fence
        return fence
    except BaseException as error:
        try:
            _cleanup_guard = True
            primary_error = error
            mapping_error = None
            cleanup_error = None
            try:
                _revalidate_state_handles(parent)
            except Exception as candidate:
                mapping_error = candidate
            if mapping_error is not None:
                publication = AtomicPublication.AMBIGUOUS
            elif not replace_attempted:
                publication = _probe_atomic_publication(
                    parent_fd,
                    path.name,
                    temporary_name,
                    temporary_identity,
                    encoded,
                    directory_synced=False,
                )
                if publication != AtomicPublication.NOT_PUBLISHED:
                    # No namespace replacement was attempted, so only a changed or
                    # missing captured temporary can make cleanup ambiguous.
                    publication = AtomicPublication.AMBIGUOUS
            elif replace_returned:
                publication = _probe_atomic_publication(
                    parent_fd,
                    path.name,
                    temporary_name,
                    temporary_identity,
                    encoded,
                    directory_synced=directory_synced,
                )
            else:
                # The replacement syscall was entered but did not return.  Only
                # the canonical/temp identity probe can resolve its publication.
                publication = _probe_atomic_publication(
                    parent_fd,
                    path.name,
                    temporary_name,
                    temporary_identity,
                    encoded,
                    directory_synced=False,
                )
            receipt.publication = publication
            if (
                publication == AtomicPublication.NOT_PUBLISHED
                and temporary_identity is not None
            ):
                candidate = _complete_cleanup_action(
                    temporary_cleanup_receipt, cleanup_temporary
                )
                if candidate is not None:
                    cleanup_error = candidate
        except BaseException as candidate:
            primary_error = error
            classification_error = candidate
            publication = receipt.publication
        try:
            _cleanup_guard = True
            failure = _atomic_write_failure(
                path,
                error,
                publication,
                mapping_error=mapping_error,
                cleanup_error=cleanup_error,
                classification_error=classification_error,
                cleanup_receipt=temporary_cleanup_receipt,
            )
        except BaseException as reporting_error:
            try:
                _cleanup_guard = True
                failure_reporting_error = reporting_error
            except BaseException:
                failure_reporting_error = reporting_error
            failure = _atomic_write_failure(
                path,
                error,
                publication,
                mapping_error=mapping_error,
                cleanup_error=cleanup_error,
                classification_error=classification_error,
                reporting_error=reporting_error,
                cleanup_receipt=temporary_cleanup_receipt,
            )
        raise failure from error
    finally:
        try:
            _cleanup_guard = True
            active_primary = primary_error
            if active_primary is None:
                current_error = sys.exc_info()[1]
                if isinstance(current_error, BaseException):
                    active_primary = current_error
            if (
                receipt.publication == AtomicPublication.NOT_PUBLISHED
                and temporary_identity is not None
                and not temporary_cleanup_receipt.completed
            ):
                candidate = _complete_cleanup_action(
                    temporary_cleanup_receipt, cleanup_temporary
                )
                if cleanup_error is None and candidate is not None:
                    cleanup_error = candidate
            _drain_descriptor_owners(
                (descriptor_owner, parent),
                primary_error=active_primary,
                durable_namespace_complete=publication_verified,
            )
        except BaseException as finalization_error:
            active_primary = primary_error
            if active_primary is None:
                active_primary = finalization_error
            _drain_descriptor_owners(
                (descriptor_owner, parent),
                primary_error=active_primary,
                durable_namespace_complete=publication_verified,
            )
            if primary_error is None and not publication_verified:
                raise
        if primary_error is not None and cleanup_error is not None:
            try:
                _cleanup_guard = True
                final_failure = _atomic_write_failure(
                    path,
                    primary_error,
                    receipt.publication,
                    mapping_error=mapping_error,
                    cleanup_error=cleanup_error,
                    classification_error=classification_error,
                    reporting_error=failure_reporting_error,
                    cleanup_receipt=temporary_cleanup_receipt,
                )
            except BaseException as reporting_error:
                final_failure = _atomic_write_failure(
                    path,
                    primary_error,
                    receipt.publication,
                    mapping_error=mapping_error,
                    cleanup_error=cleanup_error,
                    classification_error=classification_error,
                    reporting_error=reporting_error,
                    cleanup_receipt=temporary_cleanup_receipt,
                )
            raise final_failure from primary_error


def _read_json_record(
    path: pathlib.Path,
    *,
    parent: Optional[StateDirectory] = None,
    harden: bool = True,
) -> Optional[Tuple[Mapping[str, Any], DurableFence]]:
    if parent is not None:
        if parent.path != path.parent.absolute():
            raise FatalRepairError("held state directory does not match canonical path")
        return _read_json_record_with_parent(path, parent=parent, harden=harden)
    parent_context = _open_state_directory(path.parent, create=False)
    try:
        with parent_context as opened_parent:
            return _read_json_record_with_parent(
                path, parent=opened_parent, harden=harden
            )
    except FileNotFoundError:
        return None


def _read_json_record_with_parent(
    path: pathlib.Path,
    *,
    parent: StateDirectory,
    harden: bool,
) -> Optional[Tuple[Mapping[str, Any], DurableFence]]:
    if parent.path != path.parent.absolute():
        raise FatalRepairError("held state directory does not match canonical path")
    parent_fd = parent.descriptor
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor_owner = _open_fd_owned(
        path.name,
        flags,
        dir_fd=parent_fd,
        subject=f"canonical state {path.name!r}",
    )
    try:
        _revalidate_state_handles(parent)
        with _closing_descriptor(descriptor_owner) as descriptor:
            return _read_json_from_descriptor(
                path,
                parent=parent,
                descriptor=descriptor,
                harden=harden,
            )
    except FileNotFoundError:
        _revalidate_state_handles(parent)
        return None
    except OSError as error:
        raise FatalRepairError(
            f"cannot read canonical state {path}: {error}"
        ) from error
    except BaseException:
        raise


def _read_json_from_descriptor(
    path: pathlib.Path,
    *,
    parent: StateDirectory,
    descriptor: int,
    harden: bool,
) -> Tuple[Mapping[str, Any], DurableFence]:
    parent_fd = parent.descriptor
    stable_metadata: Optional[os.stat_result] = None
    try:
        _revalidate_state_handles(parent)
        _require_leaf_mapping(parent_fd, path.name, descriptor, label="canonical state")
        if harden:
            _harden_private_state_fd(descriptor, is_directory=False)
        else:
            _validate_private_state_fd(descriptor, is_directory=False)
        observed: Optional[bytes] = None
        raw: Optional[bytes] = None
        for attempt in range(STATE_READ_ATTEMPTS):
            _revalidate_state_handles(parent)
            _validate_private_state_fd(descriptor, is_directory=False)
            before = os.fstat(descriptor)
            first = _read_fd_bounded(descriptor, before.st_size)
            middle = os.fstat(descriptor)
            second = _read_fd_bounded(descriptor, middle.st_size)
            after = os.fstat(descriptor)
            _validate_private_state_fd(descriptor, is_directory=False)
            _require_leaf_mapping(
                parent_fd, path.name, descriptor, label="canonical state"
            )
            _revalidate_state_handles(parent)
            if not (
                _state_identity_policy(before)
                == _state_identity_policy(middle)
                == _state_identity_policy(after)
            ):
                raise FatalRepairError(
                    f"canonical state identity or access policy changed: {path}"
                )
            if first != second or (observed is not None and observed != second):
                raise FatalRepairError(
                    f"canonical state content changed while reading: {path}"
                )
            observed = second
            if (
                _state_generation(before)
                == _state_generation(middle)
                == _state_generation(after)
            ):
                raw = second
                stable_metadata = after
                break
            if attempt + 1 == STATE_READ_ATTEMPTS:
                raise FatalRepairError(
                    f"canonical state generation remained unstable: {path}"
                )
        if raw is None:
            raise FatalRepairError(f"canonical state could not be read stably: {path}")
    except OSError as error:
        raise FatalRepairError(
            f"canonical state became unreadable: {path}: {error}"
        ) from error
    except BaseException:
        raise
    assert stable_metadata is not None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FatalRepairError(f"canonical state is corrupt: {path}") from error
    assert parent.identity is not None
    return (
        _mapping(value),
        DurableFence(
            path=path.absolute(),
            parent_identity=FileIdentity(*parent.identity),
            leaf_identity=FileIdentity(stable_metadata.st_dev, stable_metadata.st_ino),
            encoded_payload=raw,
        ),
    )


def _read_json(path: pathlib.Path) -> Optional[Mapping[str, Any]]:
    record = _read_json_record(path)
    return None if record is None else record[0]


@contextlib.contextmanager
def repair_lock(config: Config) -> Iterator[None]:
    if _active_state_root() is not None:
        raise FatalRepairError("nested reflink repair lock is not supported")
    state_context = _open_state_directory(config.state_root, create=True)
    with state_context as state:
        with _bound_repair_lock(config, state):
            yield


@contextlib.contextmanager
def _bound_repair_lock(config: Config, state: StateDirectory) -> Iterator[None]:
    global _ACTIVE_LOCK_DESCRIPTOR, _ACTIVE_STATE_ROOT
    state_fd = state.descriptor
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor_owner = _open_fd_owned(
        config.lock_path.name,
        flags,
        0o600,
        dir_fd=state_fd,
        subject="repair lock",
    )
    opened = False
    try:
        with descriptor_owner:
            descriptor = descriptor_owner.fileno()
            opened = True
            acquired = False
            primary_error: Optional[BaseException] = None
            cleanup_error: Optional[BaseException] = None
            unlock_error: Optional[BaseException] = None
            unlock_receipt = _CleanupActionReceipt()

            def unlock_repair_lock() -> None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

            try:
                state.revalidate()
                _require_leaf_mapping(
                    state_fd, config.lock_path.name, descriptor, label="repair lock"
                )
                _harden_private_state_fd(descriptor, is_directory=False)
                _validate_private_state_fd(descriptor, is_directory=False)
                state.revalidate()
                _require_leaf_mapping(
                    state_fd, config.lock_path.name, descriptor, label="repair lock"
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise FatalRepairError(
                        "another reflink repair process holds the lock"
                    ) from error
                acquired = True
                state.revalidate()
                _require_leaf_mapping(
                    state_fd, config.lock_path.name, descriptor, label="repair lock"
                )
                _ACTIVE_STATE_ROOT = weakref.ref(state)
                _ACTIVE_LOCK_DESCRIPTOR = descriptor
                try:
                    yield
                except BaseException as error:
                    try:
                        state.revalidate()
                        _require_leaf_mapping(
                            state_fd,
                            config.lock_path.name,
                            descriptor,
                            label="repair lock",
                        )
                    except Exception as verification_error:
                        try:
                            _cleanup_guard = True
                            detail = (
                                "repair lock namespace changed while handling failure: "
                                f"{verification_error}"
                            )
                            _attach_cleanup_diagnostic(
                                error,
                                detail=detail,
                                cleanup_error=verification_error,
                            )
                        except BaseException:
                            detail = (
                                "repair lock namespace changed while handling failure: "
                                f"{verification_error}"
                            )
                            _attach_cleanup_diagnostic(
                                error,
                                detail=detail,
                                cleanup_error=verification_error,
                            )
                    raise
                else:
                    state.revalidate()
                    _require_leaf_mapping(
                        state_fd,
                        config.lock_path.name,
                        descriptor,
                        label="repair lock",
                    )
            except OSError as error:
                try:
                    _cleanup_guard = True
                    primary_error = error
                except BaseException:
                    primary_error = error
                raise FatalRepairError(f"cannot hold repair lock: {error}") from error
            except BaseException as error:
                try:
                    _cleanup_guard = True
                    primary_error = error
                except BaseException:
                    primary_error = error
                raise
            finally:
                try:
                    _cleanup_guard = True
                    cleanup_error = None
                    try:
                        _ACTIVE_LOCK_DESCRIPTOR = None
                        _ACTIVE_STATE_ROOT = None
                    except BaseException as error:
                        cleanup_error = error
                        _ACTIVE_LOCK_DESCRIPTOR = None
                        _ACTIVE_STATE_ROOT = None
                    unlock_error = None
                    if acquired:
                        try:
                            _cleanup_guard = True
                            unlock_error = _complete_cleanup_action(
                                unlock_receipt,
                                unlock_repair_lock,
                            )
                        except BaseException:
                            unlock_error = _complete_cleanup_action(
                                unlock_receipt,
                                unlock_repair_lock,
                            )
                    if primary_error is not None and unlock_error is not None:
                        try:
                            _cleanup_guard = True
                            detail = (
                                "repair lock release was not proved clean: "
                                f"{unlock_error}"
                            )
                            _attach_cleanup_diagnostic(
                                primary_error,
                                detail=detail,
                                cleanup_error=unlock_error,
                                receipt=unlock_receipt,
                            )
                        except BaseException:
                            detail = (
                                "repair lock release was not proved clean: "
                                f"{unlock_error}"
                            )
                            _attach_cleanup_diagnostic(
                                primary_error,
                                detail=detail,
                                cleanup_error=unlock_error,
                                receipt=unlock_receipt,
                            )
                    active_primary = (
                        primary_error
                        if primary_error is not None
                        else unlock_error
                        if unlock_error is not None
                        else cleanup_error
                    )
                    try:
                        _drain_descriptor_owners(
                            (descriptor_owner,), primary_error=active_primary
                        )
                    except BaseException as error:
                        try:
                            _cleanup_guard = True
                            if cleanup_error is None:
                                cleanup_error = error
                        except BaseException:
                            if cleanup_error is None:
                                cleanup_error = error
                        _drain_descriptor_owners(
                            (descriptor_owner,),
                            primary_error=(
                                primary_error
                                if primary_error is not None
                                else unlock_error
                                if unlock_error is not None
                                else cleanup_error
                            ),
                        )
                    if primary_error is None:
                        if unlock_error is not None:
                            raise unlock_error
                        if cleanup_error is not None:
                            raise cleanup_error
                except BaseException as cleanup_interrupt:
                    _ACTIVE_LOCK_DESCRIPTOR = None
                    _ACTIVE_STATE_ROOT = None
                    if acquired:
                        fallback_unlock_error = _complete_cleanup_action(
                            unlock_receipt,
                            unlock_repair_lock,
                        )
                        if unlock_error is None:
                            unlock_error = fallback_unlock_error
                    if primary_error is not None and unlock_error is not None:
                        detail = (
                            f"repair lock release was not proved clean: {unlock_error}"
                        )
                        _attach_cleanup_diagnostic(
                            primary_error,
                            detail=detail,
                            cleanup_error=unlock_error,
                            receipt=unlock_receipt,
                        )
                    authoritative_cleanup = (
                        primary_error
                        if primary_error is not None
                        else unlock_error
                        if unlock_error is not None
                        else cleanup_error
                        if cleanup_error is not None
                        else cleanup_interrupt
                    )
                    _drain_descriptor_owners(
                        (descriptor_owner,),
                        primary_error=authoritative_cleanup,
                    )
                    if primary_error is None:
                        if unlock_error is not None:
                            raise unlock_error
                        if cleanup_error is not None:
                            raise cleanup_error
                        raise
    except OSError as error:
        if not opened:
            raise FatalRepairError(f"cannot open repair lock: {error}") from error
        raise


@dataclasses.dataclass
class Discovery:
    candidates: Dict[str, Candidate]
    source_duplicates: Dict[str, List[str]]
    mirror_duplicates: Dict[str, List[str]]
    source_only: Dict[str, str]
    mirror_only: Dict[str, str]
    noncanonical: List[Dict[str, str]]
    scan_errors: List[Dict[str, str]]


def _scan_rollout_side(
    root: pathlib.Path, side: str
) -> Tuple[Dict[str, List[pathlib.Path]], List[Dict[str, str]], List[Dict[str, str]]]:
    indexed: Dict[str, List[pathlib.Path]] = {}
    noncanonical: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    try:
        root_before = os.lstat(root)
    except FileNotFoundError:
        return indexed, noncanonical, errors
    except OSError as error:
        errors.append({"side": side, "path": str(root), "error": str(error)})
        return indexed, noncanonical, errors
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        errors.append(
            {
                "side": side,
                "path": str(root),
                "error": "discovery root is not a no-follow directory",
            }
        )
        return indexed, noncanonical, errors
    root_identity = (root_before.st_dev, root_before.st_ino)
    for state_name in ("sessions", "archived_sessions"):
        state_root = root / state_name
        try:
            state_before = os.lstat(state_root)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append({"side": side, "path": str(state_root), "error": str(error)})
            continue
        if stat.S_ISLNK(state_before.st_mode) or not stat.S_ISDIR(state_before.st_mode):
            errors.append(
                {
                    "side": side,
                    "path": str(state_root),
                    "error": "rollout discovery root is not a no-follow directory",
                }
            )
            continue
        state_identity = (state_before.st_dev, state_before.st_ino)
        state_indexed: Dict[str, List[pathlib.Path]] = {}

        def record_walk_error(error: OSError) -> None:
            errors.append(
                {
                    "side": side,
                    "path": error.filename or str(state_root),
                    "error": str(error),
                }
            )

        for directory, directory_names, file_names in os.walk(
            str(state_root), topdown=True, followlinks=False, onerror=record_walk_error
        ):
            directory_names.sort()
            file_names.sort()
            directory_path = pathlib.Path(directory)
            safe_directories: List[str] = []
            for directory_name in directory_names:
                child = directory_path / directory_name
                try:
                    child_metadata = os.lstat(child)
                except OSError as error:
                    errors.append(
                        {"side": side, "path": str(child), "error": str(error)}
                    )
                    continue
                if stat.S_ISLNK(child_metadata.st_mode):
                    errors.append(
                        {
                            "side": side,
                            "path": str(child),
                            "error": "symlinked directory encountered during discovery",
                        }
                    )
                    continue
                if not stat.S_ISDIR(child_metadata.st_mode):
                    errors.append(
                        {
                            "side": side,
                            "path": str(child),
                            "error": "non-directory entry appeared in directory traversal",
                        }
                    )
                    continue
                safe_directories.append(directory_name)
            directory_names[:] = safe_directories
            for file_name in file_names:
                if not file_name.startswith("rollout-"):
                    continue
                path = directory_path / file_name
                try:
                    file_metadata = os.lstat(path)
                except OSError as error:
                    errors.append(
                        {"side": side, "path": str(path), "error": str(error)}
                    )
                    continue
                if stat.S_ISLNK(file_metadata.st_mode) or not stat.S_ISREG(
                    file_metadata.st_mode
                ):
                    errors.append(
                        {
                            "side": side,
                            "path": str(path),
                            "error": "rollout candidate is not a no-follow regular file",
                        }
                    )
                    continue
                rollout_id = rollout_id_from_name(file_name)
                if rollout_id is None:
                    noncanonical.append(
                        {
                            "side": side,
                            "path": str(path.relative_to(root)),
                            "classification": "noncanonical-name",
                        }
                    )
                    continue
                state_indexed.setdefault(rollout_id, []).append(path)
        try:
            state_after = os.lstat(state_root)
        except OSError as error:
            errors.append({"side": side, "path": str(state_root), "error": str(error)})
            continue
        if (
            stat.S_ISLNK(state_after.st_mode)
            or not stat.S_ISDIR(state_after.st_mode)
            or (state_after.st_dev, state_after.st_ino) != state_identity
        ):
            errors.append(
                {
                    "side": side,
                    "path": str(state_root),
                    "error": "rollout discovery root identity changed during scan",
                }
            )
            continue
        for rollout_id, paths in state_indexed.items():
            indexed.setdefault(rollout_id, []).extend(paths)
    try:
        root_after = os.lstat(root)
    except OSError as error:
        errors.append({"side": side, "path": str(root), "error": str(error)})
    else:
        if (
            stat.S_ISLNK(root_after.st_mode)
            or not stat.S_ISDIR(root_after.st_mode)
            or (root_after.st_dev, root_after.st_ino) != root_identity
        ):
            errors.append(
                {
                    "side": side,
                    "path": str(root),
                    "error": "discovery root identity changed during scan",
                }
            )
    for paths in indexed.values():
        paths.sort(key=lambda path: str(path.relative_to(root)))
    return indexed, noncanonical, errors


def discover_candidates(config: Config) -> Discovery:
    sources, source_noncanonical, source_errors = _scan_rollout_side(
        config.codex_root, "source"
    )
    mirrors, mirror_noncanonical, mirror_errors = _scan_rollout_side(
        config.mirror_root, "mirror"
    )
    candidates: Dict[str, Candidate] = {}
    source_duplicates: Dict[str, List[str]] = {}
    mirror_duplicates: Dict[str, List[str]] = {}
    source_only: Dict[str, str] = {}
    mirror_only: Dict[str, str] = {}
    all_ids = sorted(set(sources) | set(mirrors))
    for rollout_id in all_ids:
        source_paths = sources.get(rollout_id, [])
        mirror_paths = mirrors.get(rollout_id, [])
        if len(source_paths) > 1:
            source_duplicates[rollout_id] = [
                str(path.relative_to(config.codex_root)) for path in source_paths
            ]
        if len(mirror_paths) > 1:
            mirror_duplicates[rollout_id] = [
                str(path.relative_to(config.mirror_root)) for path in mirror_paths
            ]
        if len(source_paths) == 1 and len(mirror_paths) == 1:
            source = source_paths[0]
            mirror = mirror_paths[0]
            candidates[rollout_id] = Candidate(
                rollout_id=rollout_id,
                source=source,
                mirror=mirror,
                source_rel=str(source.relative_to(config.codex_root)),
                mirror_rel=str(mirror.relative_to(config.mirror_root)),
            )
        elif len(source_paths) == 1 and not mirror_paths:
            source_only[rollout_id] = str(
                source_paths[0].relative_to(config.codex_root)
            )
        elif len(mirror_paths) == 1 and not source_paths:
            mirror_only[rollout_id] = str(
                mirror_paths[0].relative_to(config.mirror_root)
            )
    return Discovery(
        candidates=candidates,
        source_duplicates=source_duplicates,
        mirror_duplicates=mirror_duplicates,
        source_only=source_only,
        mirror_only=mirror_only,
        noncanonical=sorted(
            source_noncanonical + mirror_noncanonical,
            key=lambda item: (item["side"], item["path"]),
        ),
        scan_errors=sorted(
            source_errors + mirror_errors,
            key=lambda item: (item["side"], item["path"]),
        ),
    )


def _discover_private_stage_rels(root: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Find every tool-named stage without following directory symlinks."""

    stages: List[str] = []
    errors: List[str] = []
    try:
        root_before = os.lstat(root)
    except FileNotFoundError:
        return stages, errors
    except OSError as error:
        return stages, [f"cannot inspect mirror root {root}: {error}"]
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        return stages, [f"mirror root is not a no-follow directory: {root}"]
    root_identity = (root_before.st_dev, root_before.st_ino)

    def record_error(error: OSError) -> None:
        errors.append(f"stage discovery failed at {error.filename or root}: {error}")

    for directory, directory_names, file_names in os.walk(
        str(root), topdown=True, followlinks=False, onerror=record_error
    ):
        directory_names.sort()
        file_names.sort()
        directory_path = pathlib.Path(directory)
        safe: List[str] = []
        for name in directory_names:
            child = directory_path / name
            try:
                metadata = os.lstat(child)
            except OSError as error:
                errors.append(f"cannot inspect possible stage {child}: {error}")
                continue
            if PRIVATE_STAGE_PATTERN.fullmatch(name):
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    errors.append(
                        f"tool-named stage is not a no-follow directory: {child}"
                    )
                else:
                    stages.append(str(child.relative_to(root)))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                safe.append(name)
        directory_names[:] = safe
        for name in file_names:
            if PRIVATE_STAGE_PATTERN.fullmatch(name):
                errors.append(
                    f"tool-named stage is not a directory: {directory_path / name}"
                )
    try:
        root_after = os.lstat(root)
    except OSError as error:
        errors.append(f"cannot revalidate mirror root {root}: {error}")
    else:
        if (
            stat.S_ISLNK(root_after.st_mode)
            or not stat.S_ISDIR(root_after.st_mode)
            or (root_after.st_dev, root_after.st_ino) != root_identity
        ):
            errors.append("mirror root identity changed during stage discovery")
    return sorted(set(stages)), errors


def _manifest_path(config: Config, rollout_id: str) -> pathlib.Path:
    return config.manifests_dir / f"{canonical_rollout_id(rollout_id)}.json"


def _intent_path(config: Config, rollout_id: str) -> pathlib.Path:
    return config.intents_dir / f"{canonical_rollout_id(rollout_id)}.json"


def _write_manifest(config: Config, manifest: RepairManifest) -> RepairManifest:
    fence = atomic_write_json(
        _manifest_path(config, manifest.rollout_id), manifest.to_json()
    )
    return dataclasses.replace(manifest, fence=fence)


def _write_intent(config: Config, intent: RepairIntent) -> RepairIntent:
    fence = atomic_write_json(_intent_path(config, intent.rollout_id), intent.to_json())
    return dataclasses.replace(intent, fence=fence)


@dataclasses.dataclass
class DurableAuthorization:
    config: Config
    state: Any

    def authorize(self, action: str) -> None:
        if action not in AUTHORIZED_STATE_ACTIONS:
            raise FatalRepairError(f"unknown durable authorization action: {action}")
        if not isinstance(self.state, (RepairIntent, RepairManifest)):
            raise FatalRepairError("durable authorization has no typed state")
        fence = self.state.fence
        if fence is None:
            raise FatalRepairError("durable authorization has no bound state fence")
        expected_payload = self.state.to_json()
        expected_encoded = _encode_json_payload(expected_payload)
        if fence.encoded_payload != expected_encoded:
            raise FatalRepairError("durable state fence payload is not canonical")
        record = _read_json_record(fence.path, harden=False)
        if record is None:
            raise FatalRepairError("durable authorization state disappeared")
        actual_payload, actual_fence = record
        if (
            actual_fence.parent_identity != fence.parent_identity
            or actual_fence.leaf_identity != fence.leaf_identity
            or actual_fence.encoded_payload != fence.encoded_payload
        ):
            raise FatalRepairError(
                "durable authorization directory, leaf, or payload changed"
            )
        if isinstance(self.state, RepairIntent):
            typed = RepairIntent.from_json(actual_payload)
        else:
            typed = RepairManifest.from_json(actual_payload)
        if typed != self.state:
            raise FatalRepairError("durable authorization typed payload changed")
        _revalidate_active_state_root()

    def publish_intent(self, intent: RepairIntent) -> RepairIntent:
        # Keep durable publication and the in-memory phase receipt on one
        # caller line: after this call returns, cleanup retry must never observe
        # a phase older than the bytes just published.
        self.state = _write_intent(self.config, intent)
        return self.state

    def publish_manifest(self, manifest: RepairManifest) -> RepairManifest:
        self.state = _write_manifest(self.config, manifest)
        return self.state


def _delete_state_file(path: pathlib.Path, *, label: str) -> None:
    parent_context = _open_state_directory(path.parent, create=False)
    try:
        with parent_context as parent:
            _delete_state_file_with_parent(path, label=label, parent=parent)
    except FileNotFoundError:
        return


def _delete_state_file_with_parent(
    path: pathlib.Path, *, label: str, parent: StateDirectory
) -> None:
    parent_fd = parent.descriptor
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor_owner = _open_fd_owned(
        path.name,
        flags,
        dir_fd=parent_fd,
        subject=f"durable {label}",
    )
    try:
        _revalidate_state_handles(parent)
        with _closing_descriptor(descriptor_owner) as descriptor:
            _revalidate_state_handles(parent)
            _require_leaf_mapping(
                parent_fd, path.name, descriptor, label=f"durable {label}"
            )
            _harden_private_state_fd(descriptor, is_directory=False)
            _validate_private_state_fd(descriptor, is_directory=False)
            _revalidate_state_handles(parent)
            _require_leaf_mapping(
                parent_fd, path.name, descriptor, label=f"durable {label}"
            )
            _revalidate_state_handles(parent)
            _require_leaf_mapping(
                parent_fd, path.name, descriptor, label=f"durable {label}"
            )
            os.unlink(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            _revalidate_state_handles(parent)
    except FileNotFoundError:
        _revalidate_state_handles(parent)
        return
    except OSError as error:
        raise FatalRepairError(
            f"cannot safely delete durable {label}: {error}"
        ) from error
    except BaseException:
        raise


def _delete_manifest(config: Config, rollout_id: str) -> None:
    _delete_state_file(_manifest_path(config, rollout_id), label="manifest")


def _delete_intent(config: Config, rollout_id: str) -> None:
    _delete_state_file(_intent_path(config, rollout_id), label="intent")


def _load_manifests(config: Config) -> List[RepairManifest]:
    directory_context = _open_state_directory(config.manifests_dir, create=False)
    try:
        with directory_context as directory:
            return _load_manifests_from_directory(config, directory)
    except FileNotFoundError:
        return []


def _load_manifests_from_directory(
    config: Config, directory: StateDirectory
) -> List[RepairManifest]:
    manifests: List[RepairManifest] = []
    try:
        _revalidate_state_handles(directory)
        names = sorted(os.listdir(directory.descriptor))
        _revalidate_state_handles(directory)
        for name in names:
            if name.startswith(".") and ".tmp." in name:
                continue
            if not name.endswith(".json"):
                raise FatalRepairError(f"unexpected file in manifest directory: {name}")
            rollout_id = name[:-5]
            try:
                canonical_rollout_id(rollout_id)
            except ValueError as error:
                raise FatalRepairError(
                    f"invalid canonical manifest name: {name}"
                ) from error
            record = _read_json_record(config.manifests_dir / name, parent=directory)
            if record is None:
                raise FatalRepairError(
                    f"manifest disappeared during enumeration: {name}"
                )
            raw, fence = record
            manifest = RepairManifest.from_json(raw)
            if manifest.rollout_id != rollout_id:
                raise FatalRepairError(f"manifest filename/content mismatch: {name}")
            if fence.encoded_payload != _encode_json_payload(manifest.to_json()):
                raise FatalRepairError(f"manifest is not canonical JSON: {name}")
            manifests.append(dataclasses.replace(manifest, fence=fence))
        _revalidate_state_handles(directory)
    except OSError as error:
        raise FatalRepairError(f"cannot list durable manifests: {error}") from error
    except BaseException:
        raise
    return manifests


def _load_intents(config: Config) -> List[RepairIntent]:
    directory_context = _open_state_directory(config.intents_dir, create=False)
    try:
        with directory_context as directory:
            return _load_intents_from_directory(config, directory)
    except FileNotFoundError:
        return []


def _load_intents_from_directory(
    config: Config, directory: StateDirectory
) -> List[RepairIntent]:
    intents: List[RepairIntent] = []
    try:
        _revalidate_state_handles(directory)
        names = sorted(os.listdir(directory.descriptor))
        _revalidate_state_handles(directory)
        for name in names:
            if name.startswith(".") and ".tmp." in name:
                continue
            if not name.endswith(".json"):
                raise FatalRepairError(f"unexpected file in intent directory: {name}")
            rollout_id = name[:-5]
            try:
                canonical_rollout_id(rollout_id)
            except ValueError as error:
                raise FatalRepairError(
                    f"invalid canonical intent name: {name}"
                ) from error
            record = _read_json_record(config.intents_dir / name, parent=directory)
            if record is None:
                raise FatalRepairError(f"intent disappeared during enumeration: {name}")
            raw, fence = record
            intent = RepairIntent.from_json(raw)
            if intent.rollout_id != rollout_id:
                raise FatalRepairError(f"intent filename/content mismatch: {name}")
            if fence.encoded_payload != _encode_json_payload(intent.to_json()):
                raise FatalRepairError(f"intent is not canonical JSON: {name}")
            intents.append(dataclasses.replace(intent, fence=fence))
        _revalidate_state_handles(directory)
    except OSError as error:
        raise FatalRepairError(f"cannot list durable intents: {error}") from error
    except BaseException:
        raise
    return intents


def _load_queue(config: Config, queue_path: Optional[pathlib.Path] = None) -> List[str]:
    path = config.queue_path if queue_path is None else queue_path
    raw = _read_json(path)
    if raw is None:
        return []
    if raw.get("version") != SCHEMA_VERSION:
        raise FatalRepairError("unsupported retry queue version")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise FatalRepairError("retry queue entries must be a JSON array")
    rollout_ids = [QueueEntry.from_json(value).rollout_id for value in entries]
    if rollout_ids != sorted(set(rollout_ids)):
        raise FatalRepairError("retry queue is not canonical sorted unique UUIDs")
    return rollout_ids


def _write_queue(
    config: Config,
    rollout_ids: Iterable[str],
    queue_path: Optional[pathlib.Path] = None,
) -> None:
    path = config.queue_path if queue_path is None else queue_path
    canonical = sorted({canonical_rollout_id(value) for value in rollout_ids})
    atomic_write_json(path, {"version": SCHEMA_VERSION, "entries": canonical})


def _add_queue_id(
    config: Config, rollout_id: str, queue_path: Optional[pathlib.Path] = None
) -> None:
    queued = set(_load_queue(config, queue_path))
    queued.add(canonical_rollout_id(rollout_id))
    _write_queue(config, queued, queue_path)


def _remove_queue_id(
    config: Config, rollout_id: str, queue_path: Optional[pathlib.Path] = None
) -> None:
    path = config.queue_path if queue_path is None else queue_path
    queued = _load_queue(config, path)
    if rollout_id not in queued:
        return
    _write_queue(config, (item for item in queued if item != rollout_id), path)


def _phase_manifest(
    manifest: RepairManifest,
    phase: Phase,
    *,
    error: str = "",
    retryable: Optional[bool] = None,
) -> RepairManifest:
    changes: Dict[str, Any] = {
        "phase": phase,
        "updated_at_ns": time.time_ns(),
        "error": error,
    }
    if retryable is not None:
        changes["retryable"] = retryable
    return dataclasses.replace(manifest, **changes)


def _phase_intent(
    intent: RepairIntent,
    state: IntentState,
    *,
    temporary_parent_identity: Optional[FileIdentity] = None,
    clone_snapshot: Optional[FileSnapshot] = None,
) -> RepairIntent:
    return dataclasses.replace(
        intent,
        state=state,
        temporary_parent_identity=temporary_parent_identity,
        clone_snapshot=clone_snapshot,
        updated_at_ns=time.time_ns(),
    )


def _result(
    rollout_id: Optional[str],
    classification: str,
    outcome: str,
    **fields: Any,
) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "rollout_id": rollout_id,
        "classification": classification,
        "outcome": outcome,
    }
    value.update(fields)
    return value


def _receipt(
    command: str, results: Sequence[Mapping[str, Any]], **summary: Any
) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for result in results:
        classification = str(result.get("classification", "unknown"))
        counts[classification] = counts.get(classification, 0) + 1
    return {
        "command": command,
        "status": "ok",
        "summary": {"total": len(results), "classifications": counts, **summary},
        "results": list(results),
    }


def _command_fatal(
    command: str,
    cause: Exception,
    prior_results: Sequence[Mapping[str, Any]],
    *,
    rollout_id: Optional[str] = None,
    **summary: Any,
) -> CommandFatalError:
    results = [dict(result) for result in prior_results]
    results.append(_result(rollout_id, "fatal", "failed", detail=str(cause)))
    receipt = _receipt(
        command,
        results,
        completed_before_fatal=len(prior_results),
        **summary,
    )
    receipt["status"] = "fatal"
    return CommandFatalError(cause, receipt)


class RepairTool:
    """Discovery, queue, and durable transaction state machine."""

    def __init__(self, config: Config, backend: Optional[Backend] = None) -> None:
        self.config = config
        self._backend = backend

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = _default_backend()
        return self._backend

    def _evaluate(
        self, candidate: Candidate
    ) -> Tuple[Dict[str, Any], Optional[PairInspection]]:
        try:
            inspection = self.backend.inspect_pair(candidate.source, candidate.mirror)
        except FileNotFoundError:
            return _result(candidate.rollout_id, "missing", "deferred"), None
        except MissingPathError as error:
            return _result(
                candidate.rollout_id, "missing", "deferred", detail=str(error)
            ), None
        except PermissionError as error:
            return _result(
                candidate.rollout_id, "unreadable", "skipped", detail=str(error)
            ), None
        except UnreadablePathError as error:
            return _result(
                candidate.rollout_id, "unreadable", "skipped", detail=str(error)
            ), None
        except UnstablePathError as error:
            return _result(
                candidate.rollout_id, "unstable", "deferred", detail=str(error)
            ), None
        except UnsafeLinkCountError as error:
            return _result(
                candidate.rollout_id,
                "unsafe-link-count",
                "skipped",
                detail=str(error),
            ), None
        except UnsupportedError as error:
            return _result(
                candidate.rollout_id, "unsupported", "skipped", detail=str(error)
            ), None
        except OSError as error:
            return _result(
                candidate.rollout_id, "unreadable", "skipped", detail=str(error)
            ), None

        source = inspection.source
        mirror = inspection.mirror
        if source.nlink != 1 or mirror.nlink != 1:
            return _result(candidate.rollout_id, "unsafe-link-count", "skipped"), None
        exclusive_writer_error = _exclusive_writer_policy_error(mirror)
        if exclusive_writer_error is not None:
            return _result(
                candidate.rollout_id,
                "unsupported",
                "skipped",
                detail=(
                    "mirror does not have an exclusive-writer access policy: "
                    f"{exclusive_writer_error}"
                ),
            ), None
        if source.identity.device != mirror.identity.device:
            return _result(
                candidate.rollout_id,
                "unsupported",
                "skipped",
                detail="source and mirror are on different filesystems",
            ), None
        if inspection.relation == ContentRelation.MIRROR_COMPLETE_PREFIX:
            if pathlib.PurePath(candidate.source_rel).parts[0] == "sessions":
                return _result(
                    candidate.rollout_id,
                    "active-complete-prefix",
                    "deferred",
                    bytes=mirror.size,
                ), None
            return _result(
                candidate.rollout_id, "content-mismatch", "skipped", bytes=mirror.size
            ), None
        if (
            inspection.relation != ContentRelation.EXACT
            or source.size != mirror.size
            or source.content_sha256 != mirror.content_sha256
        ):
            return _result(
                candidate.rollout_id, "content-mismatch", "skipped", bytes=mirror.size
            ), None
        return (
            _result(candidate.rollout_id, "eligible", "eligible", bytes=mirror.size),
            inspection,
        )

    def _discovery_results(
        self,
        discovery: Discovery,
        requested_ids: Sequence[str],
        *,
        evaluate: bool,
        result_observer: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, PairInspection]]:
        requested = set(requested_ids)
        results: List[Dict[str, Any]] = []
        inspections: Dict[str, PairInspection] = {}

        def append_result(result: Dict[str, Any]) -> None:
            try:
                if result_observer is not None:
                    result_observer(result)
            except Exception as error:
                raw_rollout_id = result.get("rollout_id")
                raise PartialProgressError(
                    error,
                    results,
                    rollout_id=(
                        None if raw_rollout_id is None else str(raw_rollout_id)
                    ),
                ) from error
            results.append(result)

        ids = sorted(
            requested
            if requested
            else (
                set(discovery.candidates)
                | set(discovery.source_duplicates)
                | set(discovery.mirror_duplicates)
                | set(discovery.source_only)
                | set(discovery.mirror_only)
            )
        )
        for rollout_id in ids:
            source_duplicates = discovery.source_duplicates.get(rollout_id)
            mirror_duplicates = discovery.mirror_duplicates.get(rollout_id)
            if source_duplicates or mirror_duplicates:
                append_result(
                    _result(
                        rollout_id,
                        "ambiguous-pair",
                        "skipped",
                        source_paths=source_duplicates or [],
                        mirror_paths=mirror_duplicates or [],
                    )
                )
            elif rollout_id in discovery.source_only:
                append_result(_result(rollout_id, "mirror-missing", "deferred"))
            elif rollout_id in discovery.mirror_only:
                append_result(_result(rollout_id, "source-missing", "deferred"))
            elif rollout_id not in discovery.candidates:
                append_result(_result(rollout_id, "missing", "deferred"))
            elif evaluate:
                try:
                    result, inspection = self._evaluate(
                        discovery.candidates[rollout_id]
                    )
                except Exception as error:
                    raise PartialProgressError(
                        error, results, rollout_id=rollout_id
                    ) from error
                append_result(result)
                if inspection is not None:
                    inspections[rollout_id] = inspection
            else:
                candidate = discovery.candidates[rollout_id]
                append_result(
                    _result(
                        rollout_id,
                        "paired",
                        "discovered",
                        source_rel=candidate.source_rel,
                        mirror_rel=candidate.mirror_rel,
                    )
                )
        if not requested_ids:
            for item in discovery.noncanonical:
                append_result(
                    _result(
                        None,
                        item["classification"],
                        "skipped",
                        side=item["side"],
                        path=item["path"],
                    )
                )
        for item in discovery.scan_errors:
            append_result(
                _result(
                    None,
                    "scan-error",
                    "incomplete",
                    side=item["side"],
                    path=item["path"],
                    detail=item["error"],
                )
            )
        results.sort(
            key=lambda item: (
                str(item.get("rollout_id") or "~"),
                str(item.get("path", "")),
            )
        )
        return results, inspections

    def inventory(self, rollout_ids: Sequence[str] = ()) -> Dict[str, Any]:
        normalized = _normalize_rollout_ids(rollout_ids)
        with repair_lock(self.config):
            discovery = discover_candidates(self.config)
            results, _ = self._discovery_results(discovery, normalized, evaluate=True)
            receipt = _receipt(
                "inventory",
                results,
                complete=not discovery.scan_errors,
                scan_errors=len(discovery.scan_errors),
                requested=len(normalized),
            )
            if discovery.scan_errors:
                receipt["status"] = "incomplete"
            return receipt

    def repair(
        self,
        *,
        apply: bool = False,
        max_files: Optional[int] = None,
        max_bytes: Optional[int] = None,
        rollout_ids: Sequence[str] = (),
        queue_unstable: bool = False,
    ) -> Dict[str, Any]:
        normalized = _normalize_rollout_ids(rollout_ids)
        _validate_limit("max_files", max_files)
        _validate_limit("max_bytes", max_bytes)
        with repair_lock(self.config):
            recovery: List[Dict[str, Any]] = []
            selected_files = 0
            selected_bytes = 0
            would_queue = 0
            try:
                recovery = self._recover_all(apply=apply)
            except PartialProgressError as error:
                raise _command_fatal(
                    "repair",
                    error.cause,
                    error.results,
                    rollout_id=error.rollout_id,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_enabled=bool(apply and queue_unstable),
                    complete=False,
                    scan_errors=None,
                ) from error.cause
            except Exception as error:
                raise _command_fatal(
                    "repair",
                    error,
                    recovery,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_enabled=bool(apply and queue_unstable),
                    complete=False,
                    scan_errors=None,
                ) from error
            try:
                discovery = discover_candidates(self.config)
            except Exception as error:
                raise _command_fatal(
                    "repair",
                    error,
                    recovery,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_enabled=bool(apply and queue_unstable),
                    complete=False,
                    scan_errors=None,
                ) from error
            if apply and discovery.scan_errors:
                error = FatalRepairError(
                    "candidate discovery is incomplete; refusing apply"
                )
                raise _command_fatal(
                    "repair",
                    error,
                    recovery,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_enabled=bool(apply and queue_unstable),
                    complete=False,
                    scan_errors=len(discovery.scan_errors),
                ) from error
            try:
                queue = set(_load_queue(self.config)) if queue_unstable else set()
            except Exception as error:
                raise _command_fatal(
                    "repair",
                    error,
                    recovery,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_enabled=bool(apply and queue_unstable),
                    complete=not discovery.scan_errors,
                    scan_errors=len(discovery.scan_errors),
                ) from error
            deferred_classifications = {
                "unstable",
                "active-complete-prefix",
                "missing",
                "source-missing",
                "mirror-missing",
            }

            def authoritative_queue_size() -> Optional[int]:
                if not queue_unstable:
                    return None
                try:
                    return len(_load_queue(self.config))
                except Exception:
                    return None

            def persist_discovery_obligation(result: Mapping[str, Any]) -> None:
                if not (apply and queue_unstable):
                    return
                raw_rollout_id = result.get("rollout_id")
                if raw_rollout_id is None:
                    return
                rollout_id = str(raw_rollout_id)
                classification = str(result.get("classification", ""))
                if classification in deferred_classifications:
                    if rollout_id not in queue:
                        _add_queue_id(self.config, rollout_id)
                    queue.add(rollout_id)
                elif classification != "eligible":
                    if rollout_id in queue:
                        _remove_queue_id(self.config, rollout_id)
                    queue.discard(rollout_id)

            try:
                results, inspections = self._discovery_results(
                    discovery,
                    normalized,
                    evaluate=True,
                    result_observer=persist_discovery_obligation,
                )
            except PartialProgressError as error:
                raise _command_fatal(
                    "repair",
                    error.cause,
                    recovery + error.results,
                    rollout_id=error.rollout_id,
                    apply=apply,
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    queue_size=authoritative_queue_size(),
                    queue_enabled=bool(apply and queue_unstable),
                    complete=not discovery.scan_errors,
                    scan_errors=len(discovery.scan_errors),
                ) from error.cause
            rewritten: List[Dict[str, Any]] = []
            for result in results:
                rollout_id = result.get("rollout_id")
                classification = result["classification"]
                if (
                    queue_unstable
                    and rollout_id
                    and classification in deferred_classifications
                ):
                    if apply:
                        if str(rollout_id) not in queue:
                            _add_queue_id(self.config, str(rollout_id))
                        queue.add(str(rollout_id))
                    else:
                        would_queue += 1
                if classification != "eligible" or rollout_id is None:
                    if (
                        apply
                        and queue_unstable
                        and rollout_id is not None
                        and classification not in deferred_classifications
                    ):
                        if str(rollout_id) in queue:
                            _remove_queue_id(self.config, str(rollout_id))
                        queue.discard(str(rollout_id))
                    rewritten.append(result)
                    continue
                size = int(result.get("bytes", 0))
                if max_files is not None and selected_files >= max_files:
                    rewritten.append(
                        {
                            **result,
                            "classification": "limit-files",
                            "outcome": "skipped",
                        }
                    )
                    continue
                if max_bytes is not None and selected_bytes + size > max_bytes:
                    rewritten.append(
                        {
                            **result,
                            "classification": "limit-bytes",
                            "outcome": "skipped",
                        }
                    )
                    continue
                selected_files += 1
                selected_bytes += size
                if not apply:
                    rewritten.append({**result, "outcome": "dry-run"})
                    continue
                candidate = discovery.candidates[str(rollout_id)]
                try:
                    repaired = self._repair_one(
                        candidate,
                        inspections[str(rollout_id)],
                        queue_enabled=queue_unstable,
                    )
                    rewritten.append(repaired)
                    queue.discard(str(rollout_id))
                except CandidateUnsupportedError as error:
                    rewritten.append(
                        _result(
                            str(rollout_id),
                            "unsupported",
                            "terminal",
                            detail=str(error),
                        )
                    )
                    if queue_unstable and str(rollout_id) in queue:
                        try:
                            _remove_queue_id(self.config, str(rollout_id))
                        except Exception as queue_error:
                            raise _command_fatal(
                                "repair",
                                queue_error,
                                recovery + rewritten,
                                rollout_id=str(rollout_id),
                                apply=apply,
                                selected_files=selected_files,
                                selected_bytes=selected_bytes,
                                queue_size=authoritative_queue_size(),
                                queue_enabled=True,
                                complete=not discovery.scan_errors,
                                scan_errors=len(discovery.scan_errors),
                            ) from queue_error
                    queue.discard(str(rollout_id))
                except SafeRolledBack as error:
                    rewritten.append(
                        _result(
                            str(rollout_id),
                            "unstable",
                            "deferred",
                            detail=str(error),
                        )
                    )
                    if queue_unstable:
                        queue.add(str(rollout_id))
                except Exception as error:
                    raise _command_fatal(
                        "repair",
                        error,
                        recovery + rewritten,
                        rollout_id=str(rollout_id),
                        apply=apply,
                        selected_files=selected_files,
                        selected_bytes=selected_bytes,
                        queue_size=authoritative_queue_size(),
                        queue_enabled=bool(apply and queue_unstable),
                        complete=not discovery.scan_errors,
                        scan_errors=len(discovery.scan_errors),
                    ) from error
            if apply and queue_unstable:
                try:
                    _write_queue(self.config, queue)
                except Exception as error:
                    raise _command_fatal(
                        "repair",
                        error,
                        recovery + rewritten,
                        apply=apply,
                        selected_files=selected_files,
                        selected_bytes=selected_bytes,
                        queue_size=authoritative_queue_size(),
                        queue_enabled=True,
                        complete=not discovery.scan_errors,
                        scan_errors=len(discovery.scan_errors),
                    ) from error
            receipt = _receipt(
                "repair",
                recovery + rewritten,
                apply=apply,
                selected_files=selected_files,
                selected_bytes=selected_bytes,
                queue_size=len(queue) if queue_unstable else None,
                would_queue=would_queue if not apply and queue_unstable else None,
                queue_enabled=bool(apply and queue_unstable),
                complete=not discovery.scan_errors,
                scan_errors=len(discovery.scan_errors),
            )
            if discovery.scan_errors:
                receipt["status"] = "incomplete"
            return receipt

    def _repair_one(
        self,
        candidate: Candidate,
        inspection: PairInspection,
        *,
        queue_enabled: bool,
    ) -> Dict[str, Any]:
        txid = uuid.uuid4().hex
        stage_name = f".codex-reflink-repair-{txid}"
        temporary = candidate.mirror.parent / stage_name / "clone"
        temporary_rel = str(temporary.relative_to(self.config.mirror_root))
        intent_time = time.time_ns()
        intent = RepairIntent(
            rollout_id=candidate.rollout_id,
            txid=txid,
            state=IntentState.PLANNED,
            source_rel=candidate.source_rel,
            final_rel=candidate.mirror_rel,
            temporary_rel=temporary_rel,
            source_snapshot=inspection.source,
            original_snapshot=inspection.mirror,
            source_parent_identity=inspection.source_parent,
            final_parent_identity=inspection.mirror_parent,
            temporary_parent_identity=None,
            clone_snapshot=None,
            queue_enabled=queue_enabled,
            created_at_ns=intent_time,
            updated_at_ns=intent_time,
        )
        # The intent makes every subsequently created stage discoverable after SIGKILL.
        try:
            intent = _write_intent(self.config, intent)
        except AtomicWriteInterruption as error:
            raise error.cause
        authorization = DurableAuthorization(self.config, intent)
        transaction: Optional[BoundTransaction] = None
        clone_snapshot: Optional[FileSnapshot] = None
        manifest: Optional[RepairManifest] = None
        preprepared_cleanup_done = False
        primary_error: Optional[BaseException] = None

        def finish_safe_prepared_failure(error: BaseException) -> None:
            if (
                isinstance(error, (MissingPathError, UnstablePathError))
                and queue_enabled
            ):
                # Queue publication is the durable retry fence; never delete the
                # last intent first.
                _add_queue_id(self.config, candidate.rollout_id)
            _delete_intent(self.config, candidate.rollout_id)

        def publish_intent(next_intent: RepairIntent) -> None:
            nonlocal intent, preprepared_cleanup_done
            abort_receipt = _CleanupActionReceipt()
            finish_receipt = _CleanupActionReceipt()

            def cleanup_unpublished_intent(
                primary: BaseException,
            ) -> Optional[BaseException]:
                nonlocal preprepared_cleanup_done
                preprepared_cleanup_done = True
                assert transaction is not None
                cleanup_error = _complete_cleanup_action(
                    abort_receipt, transaction.abort_before_prepared
                )
                if cleanup_error is not None:
                    return cleanup_error
                return _complete_cleanup_action(
                    finish_receipt,
                    lambda: finish_safe_prepared_failure(primary),
                )

            try:
                next_intent = authorization.publish_intent(next_intent)
            except AtomicWriteInterruption as error:
                try:
                    _cleanup_guard = True
                    preprepared_cleanup_done = True
                    cleanup_error = (
                        cleanup_unpublished_intent(error.cause)
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                except BaseException:
                    preprepared_cleanup_done = True
                    cleanup_error = (
                        cleanup_unpublished_intent(error.cause)
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                if cleanup_error is not None:
                    raise FatalRepairError(
                        "intent transition was not published but stage cleanup "
                        f"was not proved: {cleanup_error}"
                    ) from error.cause
                raise error.cause
            except AtomicWriteError as error:
                try:
                    _cleanup_guard = True
                    preprepared_cleanup_done = True
                    cleanup_error = (
                        cleanup_unpublished_intent(error)
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                except BaseException:
                    preprepared_cleanup_done = True
                    cleanup_error = (
                        cleanup_unpublished_intent(error)
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                if cleanup_error is not None:
                    raise FatalRepairError(
                        "intent transition was not published but stage cleanup "
                        f"was not proved: {cleanup_error}"
                    ) from error
                raise
            except BaseException as error:
                try:
                    _cleanup_guard = True
                    preprepared_cleanup_done = True
                except BaseException:
                    preprepared_cleanup_done = True
                if isinstance(error, Exception):
                    raise FatalRepairError(
                        "intent transition publication is ambiguous; stage and "
                        f"intent were retained: {error}"
                    ) from error
                raise
            intent = next_intent

        preprepared_abort_receipt = _CleanupActionReceipt()
        preprepared_finish_receipt = _CleanupActionReceipt()

        def cleanup_prepared_attempt(
            error: BaseException,
        ) -> Optional[BaseException]:
            if preprepared_cleanup_done:
                return None
            if transaction is not None:
                cleanup_error = _complete_cleanup_action(
                    preprepared_abort_receipt, transaction.abort_before_prepared
                )
                if cleanup_error is not None:
                    return cleanup_error
            elif not isinstance(error, (UnstablePathError, UnsupportedError)):
                return None
            return _complete_cleanup_action(
                preprepared_finish_receipt,
                lambda: finish_safe_prepared_failure(error),
            )

        try:
            try:
                _revalidate_active_state_root()
                transaction = self.backend.prepare(
                    candidate.source,
                    candidate.mirror,
                    temporary,
                    inspection,
                    authorization.authorize,
                )
                final_parent, temporary_parent = transaction.parent_identities()
                if final_parent != inspection.mirror_parent:
                    raise SafetyError(
                        "destination parent changed before durable prepare"
                    )
                stage_bound = _phase_intent(
                    intent,
                    IntentState.STAGE_BOUND,
                    temporary_parent_identity=temporary_parent,
                )
                publish_intent(stage_bound)
                _revalidate_active_state_root()
                clone_snapshot = transaction.clone(inspection.source, inspection.mirror)
                if clone_snapshot.identity == inspection.mirror.identity:
                    raise SafetyError(
                        "clone and original unexpectedly have the same identity"
                    )
                if clone_snapshot.identity.device != inspection.mirror.identity.device:
                    raise SafetyError(
                        "clone and original are not on the same filesystem"
                    )
                if not _protected_equal(clone_snapshot, inspection.mirror):
                    raise SafetyError(
                        "clone does not preserve original protected properties"
                    )
                clone_bound = _phase_intent(
                    intent,
                    IntentState.CLONE_BOUND,
                    temporary_parent_identity=temporary_parent,
                    clone_snapshot=clone_snapshot,
                )
                publish_intent(clone_bound)
                # Close the post-clone/pre-PREPARED archive window while the
                # last durable fence is still the CLONE_BOUND intent.  A moved
                # source can then be safely aborted and queued without ever
                # publishing a PREPARED manifest.
                transaction.revalidate_before_prepared(
                    inspection.source, inspection.mirror, clone_snapshot
                )
            except BaseException as error:
                try:
                    _cleanup_guard = True
                    cleanup_error = cleanup_prepared_attempt(error)
                except BaseException:
                    cleanup_error = cleanup_prepared_attempt(error)
                if cleanup_error is not None:
                    raise FatalRepairError(
                        "repair failed before PREPARED and stage cleanup was not "
                        f"proved: {error}; cleanup: {cleanup_error}"
                    ) from error
                if isinstance(error, (MissingPathError, UnstablePathError)):
                    raise SafeRolledBack(
                        f"protected properties changed before PREPARED: {error}"
                    ) from error
                if isinstance(error, UnsupportedError):
                    raise CandidateUnsupportedError(str(error)) from error
                if isinstance(error, RepairError):
                    raise
                if isinstance(error, Exception):
                    raise FatalRepairError(f"reflink repair failed: {error}") from error
                raise

            assert clone_snapshot is not None
            now = time.time_ns()
            manifest = RepairManifest(
                rollout_id=candidate.rollout_id,
                txid=txid,
                phase=Phase.PREPARED,
                source_rel=candidate.source_rel,
                final_rel=candidate.mirror_rel,
                temporary_rel=temporary_rel,
                source_parent_identity=inspection.source_parent,
                final_parent_identity=final_parent,
                temporary_parent_identity=temporary_parent,
                source_snapshot=inspection.source,
                original_snapshot=inspection.mirror,
                clone_snapshot=clone_snapshot,
                created_at_ns=now,
                updated_at_ns=now,
                queue_enabled=queue_enabled,
            )
            prepared_abort_receipt = _CleanupActionReceipt()
            prepared_intent_delete_receipt = _CleanupActionReceipt()

            def cleanup_unpublished_manifest() -> Optional[BaseException]:
                assert transaction is not None
                cleanup_error = _complete_cleanup_action(
                    prepared_abort_receipt, transaction.abort_before_prepared
                )
                if cleanup_error is not None:
                    return cleanup_error
                return _complete_cleanup_action(
                    prepared_intent_delete_receipt,
                    lambda: _delete_intent(self.config, candidate.rollout_id),
                )

            try:
                manifest = authorization.publish_manifest(manifest)
            except AtomicWriteInterruption as error:
                try:
                    _cleanup_guard = True
                    cleanup_error = (
                        cleanup_unpublished_manifest()
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                except BaseException:
                    cleanup_error = (
                        cleanup_unpublished_manifest()
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                if cleanup_error is not None:
                    raise FatalRepairError(
                        "PREPARED was not published but stage cleanup was not "
                        f"proved: {cleanup_error}"
                    ) from error.cause
                # Published/ambiguous state is never followed by a swap.
                raise error.cause
            except AtomicWriteError as error:
                try:
                    _cleanup_guard = True
                    cleanup_error = (
                        cleanup_unpublished_manifest()
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                except BaseException:
                    cleanup_error = (
                        cleanup_unpublished_manifest()
                        if error.publication == AtomicPublication.NOT_PUBLISHED
                        else None
                    )
                if cleanup_error is not None:
                    raise FatalRepairError(
                        "PREPARED was not published but stage cleanup was not "
                        f"proved: {cleanup_error}"
                    ) from error
                raise
            except BaseException as error:
                # An unclassified write failure may have published canonical state.
                try:
                    _cleanup_guard = True
                    mapped_error = isinstance(error, Exception)
                except BaseException:
                    mapped_error = isinstance(error, Exception)
                if mapped_error:
                    raise FatalRepairError(
                        "initial PREPARED publication is ambiguous; stage and intent "
                        f"were retained: {error}"
                    ) from error
                raise
            transaction.mark_prepared()
            _delete_intent(self.config, candidate.rollout_id)
            pre_forward_defer_receipt = _CleanupActionReceipt()
            pre_forward_rollback_receipt = _CleanupActionReceipt()
            postverify_rollback_receipt = _CleanupActionReceipt()

            def pre_forward_failure(
                kind: str, error: UnstablePathError
            ) -> BaseException:
                if kind == "defer":
                    cleanup_error = _complete_cleanup_action(
                        pre_forward_defer_receipt,
                        lambda: self._defer_before_forward(
                            transaction, manifest, error, authorization
                        ),
                    )
                    if cleanup_error is None:
                        return SafeRolledBack(
                            "protected snapshots were unstable before the forward "
                            f"swap; original mirror was retained: {error}"
                        )
                elif kind == "rollback":
                    cleanup_error = _complete_cleanup_action(
                        pre_forward_rollback_receipt,
                        lambda: self._rollback_after_postverify(
                            transaction, manifest, error, authorization
                        ),
                    )
                    if cleanup_error is None:
                        return AssertionError("rollback helper must raise")
                else:
                    return FatalRepairError(
                        "pre-forward instability left an ambiguous namespace; "
                        "durable evidence was retained"
                    )
                if isinstance(cleanup_error, RepairError):
                    return cleanup_error
                return FatalRepairError(
                    "pre-forward compensation was interrupted; durable evidence "
                    f"was retained: {cleanup_error}"
                )

            def postverify_failure(error: Exception) -> BaseException:
                cleanup_error = _complete_cleanup_action(
                    postverify_rollback_receipt,
                    lambda: self._rollback_after_postverify(
                        transaction,
                        manifest,
                        error,
                        authorization,
                    ),
                )
                if cleanup_error is None:
                    return AssertionError("rollback helper must raise")
                if isinstance(cleanup_error, RepairError):
                    return cleanup_error
                return FatalRepairError(
                    "postverify rollback was interrupted; durable evidence was "
                    f"retained: {cleanup_error}"
                )

            try:
                _revalidate_active_state_root()
                transaction.revalidate_pre_forward(
                    inspection.source, inspection.mirror, clone_snapshot
                )
                transaction.swap_forward(
                    inspection.mirror.identity, clone_snapshot.identity
                )
            except UnstablePathError as error:
                try:
                    _cleanup_guard = True
                    orientation = transaction.orientation(
                        inspection.mirror.identity, clone_snapshot.identity
                    )
                    cleanup_kind = (
                        "defer"
                        if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP
                        else (
                            "rollback"
                            if orientation == Orientation.CLONE_FINAL_ORIGINAL_TEMP
                            else "ambiguous"
                        )
                    )
                except BaseException:
                    orientation = transaction.orientation(
                        inspection.mirror.identity, clone_snapshot.identity
                    )
                    cleanup_kind = (
                        "defer"
                        if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP
                        else (
                            "rollback"
                            if orientation == Orientation.CLONE_FINAL_ORIGINAL_TEMP
                            else "ambiguous"
                        )
                    )
                try:
                    _cleanup_guard = True
                    failure = pre_forward_failure(cleanup_kind, error)
                except BaseException:
                    failure = pre_forward_failure(cleanup_kind, error)
                raise failure from error
            try:
                transaction.revalidate_forward(inspection.mirror, clone_snapshot)
                verified = transaction.postverify(inspection.source, clone_snapshot)
                if not _identity_matches(verified, clone_snapshot.identity):
                    raise SafetyError(
                        "postverify did not bind the final name to the clone"
                    )
                if not _protected_equal(verified, clone_snapshot):
                    raise SafetyError("postverify found changed protected properties")
            except Exception as error:
                try:
                    _cleanup_guard = True
                    failure = postverify_failure(error)
                except BaseException:
                    failure = postverify_failure(error)
                raise failure from error
            manifest = _phase_manifest(manifest, Phase.COMMIT_READY)
            manifest = authorization.publish_manifest(manifest)
            try:
                _revalidate_active_state_root()
                transaction.revalidate_forward(
                    manifest.original_snapshot, manifest.clone_snapshot
                )
                transaction.unlink_original(
                    inspection.mirror.identity, manifest.clone_snapshot
                )
                transaction.sync_namespaces()
                transaction.revalidate_committed(manifest.clone_snapshot)
            except Exception as error:
                manifest = _phase_manifest(
                    manifest, Phase.COMMIT_READY, error=str(error)
                )
                manifest = authorization.publish_manifest(manifest)
                raise FatalRepairError(
                    f"old mirror unlink was not completed: {error}"
                ) from error
            _revalidate_active_state_root()
            transaction.cleanup_stage()
            manifest = _phase_manifest(manifest, Phase.DONE)
            manifest = authorization.publish_manifest(manifest)
            if queue_enabled:
                _remove_queue_id(self.config, candidate.rollout_id)
            _delete_manifest(self.config, candidate.rollout_id)
            return _result(
                candidate.rollout_id,
                "repaired",
                "repaired",
                bytes=clone_snapshot.size,
                txid=txid,
            )
        except Exception as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            if isinstance(error, RepairError):
                raise
            raise FatalRepairError(f"reflink repair failed: {error}") from error
        except BaseException as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise
        finally:
            try:
                _cleanup_guard = True
                if transaction is not None:
                    transaction.close()
            except BaseException as cleanup_error:
                try:
                    _cleanup_guard = True
                    retry_dispatched = False
                    if transaction is not None:
                        # Keep the receipt and dispatch on one trace line: an
                        # interruption before it must execute the fallback,
                        # while a natural second-close failure must not cause a
                        # third dispatch.
                        # fmt: off
                        retry_dispatched = True; transaction.close()  # noqa: E702
                        # fmt: on
                except BaseException:
                    try:
                        retry_dispatched
                    except UnboundLocalError:
                        retry_dispatched = False
                    if transaction is not None and not retry_dispatched:
                        try:
                            transaction.close()
                        except BaseException:
                            pass
                if primary_error is None:
                    raise cleanup_error

    def _defer_before_forward(
        self,
        transaction: BoundTransaction,
        manifest: RepairManifest,
        original_error: Exception,
        authorization: DurableAuthorization,
    ) -> None:
        current = authorization.state
        if (
            not isinstance(current, RepairManifest)
            or current.rollout_id != manifest.rollout_id
            or current.txid != manifest.txid
        ):
            raise FatalRepairError(
                "pre-forward deferral lost its authoritative durable manifest"
            ) from original_error
        try:
            orientation = transaction.orientation(
                current.original_snapshot.identity, current.clone_snapshot.identity
            )
            if current.phase == Phase.PREPARED:
                if orientation != Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    raise FatalRepairError(
                        "pre-forward deferral no longer has the proved BEFORE "
                        "orientation; durable evidence was retained"
                    )
                current = _phase_manifest(
                    current,
                    Phase.ROLLBACK_READY,
                    error=str(original_error),
                    retryable=True,
                )
                current = authorization.publish_manifest(current)
            if current.phase == Phase.ROLLBACK_READY:
                if orientation != Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    raise FatalRepairError(
                        "ROLLBACK_READY deferral has an unexpected namespace "
                        f"orientation: {orientation.value}"
                    )
                current = _phase_manifest(
                    current, Phase.ROLLED_BACK, error=str(original_error)
                )
                current = authorization.publish_manifest(current)
            if current.phase == Phase.ROLLED_BACK:
                orientation = transaction.orientation(
                    current.original_snapshot.identity,
                    current.clone_snapshot.identity,
                )
                if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    _revalidate_active_state_root()
                    transaction.revalidate_before_cleanup(
                        current.original_snapshot, current.clone_snapshot
                    )
                    transaction.unlink_clone(
                        current.clone_snapshot.identity, current.original_snapshot
                    )
                elif orientation != Orientation.ORIGINAL_FINAL_TEMP_MISSING:
                    raise SafetyError(
                        "ROLLED_BACK deferral has an unexpected namespace "
                        f"orientation: {orientation.value}"
                    )
                # This fence is intentionally repeated when the clone is already
                # absent: the preceding dispatch may have been interrupted after
                # unlink(2) but before namespace durability was proved.
                transaction.sync_namespaces()
                transaction.revalidate_rolled_back(current.original_snapshot)
                _revalidate_active_state_root()
                transaction.cleanup_stage()
                current = _phase_manifest(
                    current, Phase.DEFERRED, error=str(original_error)
                )
                current = authorization.publish_manifest(current)
            if current.phase != Phase.DEFERRED:
                raise FatalRepairError(
                    "pre-forward deferral has an unsupported durable phase: "
                    f"{current.phase.value}"
                )
        except Exception as cleanup_error:
            raise FatalRepairError(
                "pre-forward deferral cleanup failed; durable evidence was retained: "
                f"{cleanup_error}"
            ) from original_error
        if current.queue_enabled:
            _add_queue_id(self.config, current.rollout_id)
        _delete_manifest(self.config, current.rollout_id)

    def _rollback_after_postverify(
        self,
        transaction: BoundTransaction,
        manifest: RepairManifest,
        original_error: Exception,
        authorization: DurableAuthorization,
    ) -> None:
        current = authorization.state
        if (
            not isinstance(current, RepairManifest)
            or current.rollout_id != manifest.rollout_id
            or current.txid != manifest.txid
        ):
            raise FatalRepairError(
                "postverify rollback lost its authoritative durable manifest"
            ) from original_error
        try:
            orientation = transaction.orientation(
                current.original_snapshot.identity, current.clone_snapshot.identity
            )
            if current.phase == Phase.PREPARED:
                if orientation != Orientation.CLONE_FINAL_ORIGINAL_TEMP:
                    retained = _phase_manifest(
                        current,
                        Phase.PREPARED,
                        error=(
                            "postverify failed and rollback orientation is "
                            f"{orientation.value}: {original_error}"
                        ),
                    )
                    authorization.publish_manifest(retained)
                    raise FatalRepairError(
                        "postverify failed; identity mapping is not safe for "
                        "rollback; both objects were retained"
                    )
                current = _phase_manifest(
                    current,
                    Phase.ROLLBACK_READY,
                    error=str(original_error),
                    retryable=isinstance(original_error, UnstablePathError),
                )
                current = authorization.publish_manifest(current)
            if current.phase == Phase.ROLLBACK_READY:
                orientation = transaction.orientation(
                    current.original_snapshot.identity,
                    current.clone_snapshot.identity,
                )
                if orientation == Orientation.CLONE_FINAL_ORIGINAL_TEMP:
                    _revalidate_active_state_root()
                    transaction.revalidate_forward(
                        current.original_snapshot, current.clone_snapshot
                    )
                    transaction.swap_back(
                        current.clone_snapshot.identity,
                        current.original_snapshot.identity,
                    )
                elif orientation != Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    raise SafetyError(
                        "ROLLBACK_READY has an unexpected namespace orientation: "
                        f"{orientation.value}"
                    )
                # Repeating this fence is safe and closes the window where the
                # swap returned but its directory durability was interrupted.
                transaction.sync_namespaces()
                orientation = transaction.orientation(
                    current.original_snapshot.identity,
                    current.clone_snapshot.identity,
                )
                if orientation != Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    raise SafetyError(
                        f"rollback produced unexpected orientation: {orientation.value}"
                    )
                current = _phase_manifest(
                    current, Phase.ROLLED_BACK, error=str(original_error)
                )
                current = authorization.publish_manifest(current)
            if current.phase == Phase.ROLLED_BACK:
                orientation = transaction.orientation(
                    current.original_snapshot.identity,
                    current.clone_snapshot.identity,
                )
                if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    _revalidate_active_state_root()
                    transaction.revalidate_before_cleanup(
                        current.original_snapshot, current.clone_snapshot
                    )
                    transaction.unlink_clone(
                        current.clone_snapshot.identity, current.original_snapshot
                    )
                elif orientation != Orientation.ORIGINAL_FINAL_TEMP_MISSING:
                    raise SafetyError(
                        "ROLLED_BACK has an unexpected namespace orientation: "
                        f"{orientation.value}"
                    )
                transaction.sync_namespaces()
                transaction.revalidate_rolled_back(current.original_snapshot)
                _revalidate_active_state_root()
                transaction.cleanup_stage()
                terminal_phase = Phase.DEFERRED if current.retryable else Phase.FAILED
                current = _phase_manifest(
                    current, terminal_phase, error=str(original_error)
                )
                current = authorization.publish_manifest(current)
            if current.phase not in (Phase.DEFERRED, Phase.FAILED):
                raise FatalRepairError(
                    "postverify rollback has an unsupported durable phase: "
                    f"{current.phase.value}"
                )
        except Exception as rollback_error:
            if isinstance(rollback_error, RepairError):
                raise
            raise FatalRepairError(
                f"rollback failed; both objects and durable evidence were retained: "
                f"{rollback_error}"
            ) from original_error
        if current.phase == Phase.DEFERRED:
            if current.queue_enabled:
                _add_queue_id(self.config, current.rollout_id)
            _delete_manifest(self.config, current.rollout_id)
            raise SafeRolledBack(
                f"source changed while active; original mirror was restored: {original_error}"
            ) from original_error
        raise FatalRepairError(
            f"postverify safety failure; original mirror was restored and evidence retained: "
            f"{original_error}"
        ) from original_error

    def _recover_intent(
        self,
        intent: RepairIntent,
        manifest: Optional[RepairManifest],
        *,
        apply: bool,
    ) -> Dict[str, Any]:
        if manifest is not None:
            if (
                manifest.txid != intent.txid
                or manifest.source_rel != intent.source_rel
                or manifest.final_rel != intent.final_rel
                or manifest.temporary_rel != intent.temporary_rel
                or manifest.queue_enabled != intent.queue_enabled
                or intent.state != IntentState.CLONE_BOUND
                or manifest.source_snapshot != intent.source_snapshot
                or manifest.original_snapshot != intent.original_snapshot
                or manifest.source_parent_identity != intent.source_parent_identity
                or manifest.final_parent_identity != intent.final_parent_identity
                or manifest.temporary_parent_identity
                != intent.temporary_parent_identity
                or manifest.clone_snapshot != intent.clone_snapshot
            ):
                raise FatalRepairError(
                    f"intent/manifest mismatch for {intent.rollout_id}; evidence retained"
                )
            if apply:
                _delete_intent(self.config, intent.rollout_id)
            return _result(
                intent.rollout_id,
                "recovery-intent-covered",
                "completed" if apply else "dry-run",
                txid=intent.txid,
            )
        _, _, temporary = _manifest_paths(self.config, intent)
        if not apply:
            return _result(
                intent.rollout_id,
                "recovery-intent-pending",
                "dry-run",
                txid=intent.txid,
            )
        authorization = DurableAuthorization(self.config, intent)
        try:
            _revalidate_active_state_root()
            disposition = self.backend.recover_intent_stage(
                intent, temporary, authorization.authorize
            )
        except Exception as error:
            raise FatalRepairError(
                f"cannot prove orphan intent stage safe for {intent.rollout_id}; "
                f"evidence retained: {error}"
            ) from error
        if disposition not in {"absent", "removed-empty", "removed-clone"}:
            raise FatalRepairError(
                f"unexpected orphan intent recovery disposition {disposition!r}"
            )
        if intent.queue_enabled:
            # The queue entry is the durable retry obligation.  It must exist
            # before the last transaction fence is removed.
            _add_queue_id(self.config, intent.rollout_id)
        _delete_intent(self.config, intent.rollout_id)
        return _result(
            intent.rollout_id,
            "recovery-intent-cleaned",
            "completed",
            txid=intent.txid,
            stage=disposition,
        )

    def _resume_manifest(
        self,
        manifest: RepairManifest,
        *,
        apply: bool,
        queue_path: Optional[pathlib.Path] = None,
    ) -> Dict[str, Any]:
        if manifest.phase == Phase.DONE:
            if apply:
                if manifest.queue_enabled:
                    _remove_queue_id(self.config, manifest.rollout_id, queue_path)
                _delete_manifest(self.config, manifest.rollout_id)
            return _result(
                manifest.rollout_id,
                "recovery-done",
                "completed",
                phase=manifest.phase.value,
                txid=manifest.txid,
                queue_removed=bool(apply and manifest.queue_enabled),
            )
        if manifest.phase == Phase.DEFERRED:
            if apply:
                if manifest.queue_enabled:
                    _add_queue_id(
                        self.config,
                        manifest.rollout_id,
                        queue_path or self.config.queue_path,
                    )
                _delete_manifest(self.config, manifest.rollout_id)
            return _result(
                manifest.rollout_id,
                "recovery-deferred",
                "deferred",
                phase=manifest.phase.value,
                txid=manifest.txid,
                detail=manifest.error,
            )
        if manifest.phase == Phase.FAILED:
            raise FatalRepairError(
                f"non-retryable safety failure retained for {manifest.rollout_id}: "
                f"{manifest.error}"
            )
        if not apply:
            return _result(
                manifest.rollout_id,
                "recovery-pending",
                "dry-run",
                phase=manifest.phase.value,
                txid=manifest.txid,
            )

        authorization = DurableAuthorization(self.config, manifest)
        source, final, temporary = _manifest_paths(self.config, manifest)
        source_for_resume = source
        source_unavailable = False
        if manifest.phase == Phase.PREPARED and source is not None:
            try:
                source_metadata = os.lstat(source)
            except FileNotFoundError:
                source_for_resume = None
                source_unavailable = True
            except OSError:
                # Let the held-FD backend return a precise fail-closed error.
                pass
            else:
                if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(
                    source_metadata.st_mode
                ):
                    source_for_resume = None
                    source_unavailable = True
        transaction: Optional[BoundTransaction] = None
        primary_error: Optional[BaseException] = None
        try:
            try:
                transaction = self.backend.resume(
                    source_for_resume,
                    final,
                    temporary,
                    manifest,
                    authorization.authorize,
                )
            except (MissingPathError, UnstablePathError):
                if manifest.phase != Phase.PREPARED or source_for_resume is None:
                    raise
                transaction = self.backend.resume(
                    None,
                    final,
                    temporary,
                    manifest,
                    authorization.authorize,
                )
                source_unavailable = True
            orientation = transaction.orientation(
                manifest.original_snapshot.identity, manifest.clone_snapshot.identity
            )
            current = manifest
            if current.phase == Phase.PREPARED:
                if source_unavailable:
                    if orientation not in {
                        Orientation.ORIGINAL_FINAL_CLONE_TEMP,
                        Orientation.CLONE_FINAL_ORIGINAL_TEMP,
                    }:
                        raise SafetyError(
                            "PREPARED source is unavailable and namespace orientation "
                            f"is {orientation.value}"
                        )
                    current = _phase_manifest(
                        current,
                        Phase.ROLLBACK_READY,
                        error="recorded source pathname is missing or replaced",
                        retryable=True,
                    )
                    current = authorization.publish_manifest(current)
                else:
                    if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                        recovery_defer_receipt = _CleanupActionReceipt()
                        recovery_pre_forward_rollback_receipt = _CleanupActionReceipt()

                        def recovery_pre_forward_failure(
                            kind: str, error: UnstablePathError
                        ) -> Optional[BaseException]:
                            if kind == "defer":
                                return _complete_cleanup_action(
                                    recovery_defer_receipt,
                                    lambda: self._defer_before_forward(
                                        transaction, current, error, authorization
                                    ),
                                )
                            if kind == "rollback":
                                cleanup_error = _complete_cleanup_action(
                                    recovery_pre_forward_rollback_receipt,
                                    lambda: self._rollback_after_postverify(
                                        transaction, current, error, authorization
                                    ),
                                )
                                if isinstance(cleanup_error, SafeRolledBack):
                                    return None
                                if cleanup_error is None:
                                    return AssertionError("rollback helper must raise")
                                return cleanup_error
                            return SafetyError(
                                "recovery pre-forward instability left an ambiguous "
                                f"orientation: {orientation.value}"
                            )

                        try:
                            _revalidate_active_state_root()
                            transaction.revalidate_pre_forward(
                                current.source_snapshot,
                                current.original_snapshot,
                                current.clone_snapshot,
                            )
                            transaction.swap_forward(
                                current.original_snapshot.identity,
                                current.clone_snapshot.identity,
                            )
                        except UnstablePathError as error:
                            try:
                                _cleanup_guard = True
                                orientation = transaction.orientation(
                                    current.original_snapshot.identity,
                                    current.clone_snapshot.identity,
                                )
                                cleanup_kind = (
                                    "defer"
                                    if orientation
                                    == Orientation.ORIGINAL_FINAL_CLONE_TEMP
                                    else (
                                        "rollback"
                                        if orientation
                                        == Orientation.CLONE_FINAL_ORIGINAL_TEMP
                                        else "ambiguous"
                                    )
                                )
                            except BaseException:
                                orientation = transaction.orientation(
                                    current.original_snapshot.identity,
                                    current.clone_snapshot.identity,
                                )
                                cleanup_kind = (
                                    "defer"
                                    if orientation
                                    == Orientation.ORIGINAL_FINAL_CLONE_TEMP
                                    else (
                                        "rollback"
                                        if orientation
                                        == Orientation.CLONE_FINAL_ORIGINAL_TEMP
                                        else "ambiguous"
                                    )
                                )
                            try:
                                _cleanup_guard = True
                                cleanup_error = recovery_pre_forward_failure(
                                    cleanup_kind, error
                                )
                            except BaseException:
                                cleanup_error = recovery_pre_forward_failure(
                                    cleanup_kind, error
                                )
                            if cleanup_error is not None:
                                if isinstance(cleanup_error, RepairError):
                                    raise cleanup_error from error
                                raise FatalRepairError(
                                    "recovery pre-forward compensation was "
                                    "interrupted; durable evidence was retained: "
                                    f"{cleanup_error}"
                                ) from error
                            return _result(
                                current.rollout_id,
                                "recovery-deferred",
                                "deferred",
                                phase=Phase.DEFERRED.value,
                                txid=current.txid,
                                detail=str(error),
                            )
                    elif orientation != Orientation.CLONE_FINAL_ORIGINAL_TEMP:
                        raise SafetyError(
                            f"PREPARED has impossible orientation: {orientation.value}"
                        )
                    recovery_postverify_receipt = _CleanupActionReceipt()

                    def recovery_postverify_failure(
                        error: Exception,
                    ) -> Optional[BaseException]:
                        cleanup_error = _complete_cleanup_action(
                            recovery_postverify_receipt,
                            lambda: self._rollback_after_postverify(
                                transaction,
                                current,
                                error,
                                authorization,
                            ),
                        )
                        if isinstance(cleanup_error, SafeRolledBack):
                            return None
                        if cleanup_error is None:
                            return AssertionError("rollback helper must raise")
                        return cleanup_error

                    try:
                        transaction.revalidate_forward(
                            current.original_snapshot, current.clone_snapshot
                        )
                        verified = transaction.postverify(
                            current.source_snapshot, current.clone_snapshot
                        )
                        if not _identity_matches(
                            verified, current.clone_snapshot.identity
                        ):
                            raise SafetyError(
                                "recovery postverify final identity mismatch"
                            )
                        if not _protected_equal(verified, current.clone_snapshot):
                            raise SafetyError(
                                "recovery postverify protected property mismatch"
                            )
                    except Exception as error:
                        try:
                            _cleanup_guard = True
                            cleanup_error = recovery_postverify_failure(error)
                        except BaseException:
                            cleanup_error = recovery_postverify_failure(error)
                        if cleanup_error is None:
                            return _result(
                                current.rollout_id,
                                "recovery-deferred",
                                "deferred",
                                phase=Phase.DEFERRED.value,
                                txid=current.txid,
                                detail=str(error),
                            )
                        if isinstance(cleanup_error, RepairError):
                            raise cleanup_error from error
                        raise FatalRepairError(
                            "recovery postverify rollback was interrupted; durable "
                            f"evidence was retained: {cleanup_error}"
                        ) from error
                    current = _phase_manifest(current, Phase.COMMIT_READY)
                    current = authorization.publish_manifest(current)
                    orientation = Orientation.CLONE_FINAL_ORIGINAL_TEMP

            if current.phase == Phase.COMMIT_READY:
                if orientation == Orientation.CLONE_FINAL_ORIGINAL_TEMP:
                    _revalidate_active_state_root()
                    transaction.revalidate_forward(
                        current.original_snapshot, current.clone_snapshot
                    )
                    transaction.unlink_original(
                        current.original_snapshot.identity, current.clone_snapshot
                    )
                    transaction.sync_namespaces()
                    transaction.revalidate_committed(current.clone_snapshot)
                elif orientation == Orientation.CLONE_FINAL_TEMP_MISSING:
                    _revalidate_active_state_root()
                    transaction.revalidate_committed(current.clone_snapshot)
                    transaction.sync_namespaces()
                else:
                    raise SafetyError(
                        f"COMMIT_READY has impossible orientation: {orientation.value}"
                    )
                _revalidate_active_state_root()
                transaction.cleanup_stage()
                current = _phase_manifest(current, Phase.DONE)
                current = authorization.publish_manifest(current)
                if current.queue_enabled:
                    _remove_queue_id(self.config, current.rollout_id, queue_path)
                _delete_manifest(self.config, current.rollout_id)
                return _result(
                    current.rollout_id,
                    "recovery-done",
                    "completed",
                    phase=current.phase.value,
                    txid=current.txid,
                    queue_removed=bool(apply and current.queue_enabled),
                )

            if current.phase in (Phase.ROLLBACK_READY, Phase.ROLLED_BACK):
                if current.phase == Phase.ROLLBACK_READY:
                    if orientation == Orientation.CLONE_FINAL_ORIGINAL_TEMP:
                        _revalidate_active_state_root()
                        transaction.revalidate_forward(
                            current.original_snapshot, current.clone_snapshot
                        )
                        transaction.swap_back(
                            current.clone_snapshot.identity,
                            current.original_snapshot.identity,
                        )
                        transaction.sync_namespaces()
                    elif orientation != Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                        raise SafetyError(
                            f"ROLLBACK_READY has impossible orientation: {orientation.value}"
                        )
                    current = _phase_manifest(current, Phase.ROLLED_BACK)
                    current = authorization.publish_manifest(current)
                    orientation = Orientation.ORIGINAL_FINAL_CLONE_TEMP
                if orientation == Orientation.ORIGINAL_FINAL_CLONE_TEMP:
                    _revalidate_active_state_root()
                    transaction.revalidate_before_cleanup(
                        current.original_snapshot, current.clone_snapshot
                    )
                    transaction.unlink_clone(
                        current.clone_snapshot.identity, current.original_snapshot
                    )
                    transaction.sync_namespaces()
                    transaction.revalidate_rolled_back(current.original_snapshot)
                elif orientation == Orientation.ORIGINAL_FINAL_TEMP_MISSING:
                    _revalidate_active_state_root()
                    # The prior process may have stopped after unlink(2) but
                    # before the namespace durability fence.  Repeating the
                    # fence is required before terminal evidence is published.
                    transaction.sync_namespaces()
                    transaction.revalidate_rolled_back(current.original_snapshot)
                else:
                    raise SafetyError(
                        f"ROLLED_BACK has impossible orientation: {orientation.value}"
                    )
                _revalidate_active_state_root()
                transaction.cleanup_stage()
                terminal_phase = Phase.DEFERRED if current.retryable else Phase.FAILED
                current = _phase_manifest(current, terminal_phase, error=current.error)
                current = authorization.publish_manifest(current)
                if current.retryable:
                    if current.queue_enabled:
                        _add_queue_id(
                            self.config,
                            current.rollout_id,
                            queue_path or self.config.queue_path,
                        )
                    _delete_manifest(self.config, current.rollout_id)
                    return _result(
                        current.rollout_id,
                        "recovery-deferred",
                        "deferred",
                        phase=current.phase.value,
                        txid=current.txid,
                        detail=current.error,
                    )
                raise FatalRepairError(
                    f"non-retryable rollback failure retained for "
                    f"{current.rollout_id}: {current.error}"
                )
            raise FatalRepairError(f"unsupported recovery phase: {current.phase.value}")
        except FatalRepairError as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise
        except Exception as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise FatalRepairError(
                f"recovery failed for {manifest.rollout_id}; evidence retained: {error}"
            ) from error
        except BaseException as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise
        finally:
            try:
                _cleanup_guard = True
                if transaction is not None:
                    transaction.close()
            except BaseException as cleanup_error:
                try:
                    _cleanup_guard = True
                    retry_dispatched = False
                    if transaction is not None:
                        # See _repair_one: the receipt and second dispatch share
                        # one line so a line-event interruption cannot create a
                        # third close attempt.
                        # fmt: off
                        retry_dispatched = True; transaction.close()  # noqa: E702
                        # fmt: on
                except BaseException:
                    try:
                        retry_dispatched
                    except UnboundLocalError:
                        retry_dispatched = False
                    if transaction is not None and not retry_dispatched:
                        try:
                            transaction.close()
                        except BaseException:
                            pass
                if primary_error is None:
                    raise cleanup_error

    def _recover_all(
        self, *, apply: bool, queue_path: Optional[pathlib.Path] = None
    ) -> List[Dict[str, Any]]:
        manifests = _load_manifests(self.config)
        intents = _load_intents(self.config)
        manifests_by_id = {manifest.rollout_id: manifest for manifest in manifests}
        referenced_stages: Dict[str, str] = {}
        for state in [*manifests, *intents]:
            _, _, temporary_rel = _manifest_paths_unchecked(state)
            stage_rel = str(pathlib.PurePath(temporary_rel).parent)
            owner = f"{state.rollout_id}:{state.txid}"
            previous = referenced_stages.get(stage_rel)
            if previous is not None and previous != owner:
                raise FatalRepairError(
                    f"private stage {stage_rel} is referenced by multiple transactions"
                )
            referenced_stages[stage_rel] = owner
        discovered_stages, stage_errors = _discover_private_stage_rels(
            self.config.mirror_root
        )
        if stage_errors:
            raise FatalRepairError(
                "private stage discovery is incomplete: " + "; ".join(stage_errors)
            )
        unreferenced = sorted(set(discovered_stages) - set(referenced_stages))
        if unreferenced:
            raise FatalRepairError(
                "unreferenced private repair stage retained: " + ", ".join(unreferenced)
            )
        results: List[Dict[str, Any]] = []
        for intent in intents:
            try:
                results.append(
                    self._recover_intent(
                        intent,
                        manifests_by_id.get(intent.rollout_id),
                        apply=apply,
                    )
                )
            except Exception as error:
                raise PartialProgressError(
                    error, results, rollout_id=intent.rollout_id
                ) from error
        for manifest in manifests:
            try:
                results.append(
                    self._resume_manifest(manifest, apply=apply, queue_path=queue_path)
                )
            except Exception as error:
                raise PartialProgressError(
                    error, results, rollout_id=manifest.rollout_id
                ) from error
        return results

    def recover(self, *, apply: bool = False) -> Dict[str, Any]:
        with repair_lock(self.config):

            def queue_size() -> Optional[int]:
                try:
                    return len(_load_queue(self.config))
                except Exception:
                    return None

            try:
                results = self._recover_all(apply=apply)
            except PartialProgressError as error:
                raise _command_fatal(
                    "recover",
                    error.cause,
                    error.results,
                    rollout_id=error.rollout_id,
                    apply=apply,
                    queue_size=queue_size(),
                ) from error.cause
            except Exception as error:
                raise _command_fatal(
                    "recover",
                    error,
                    (),
                    apply=apply,
                    queue_size=queue_size(),
                ) from error
            return _receipt("recover", results, apply=apply, queue_size=queue_size())

    def retry(
        self,
        *,
        apply: bool = False,
    ) -> Dict[str, Any]:
        queue_path: Optional[pathlib.Path] = None
        with repair_lock(self.config):
            recovery: List[Dict[str, Any]] = []
            try:
                recovery = self._recover_all(apply=apply, queue_path=queue_path)
            except PartialProgressError as error:
                raise _command_fatal(
                    "retry",
                    error.cause,
                    error.results,
                    rollout_id=error.rollout_id,
                    apply=apply,
                    queue_size=None,
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=False,
                    scan_errors=None,
                ) from error.cause
            except Exception as error:
                raise _command_fatal(
                    "retry",
                    error,
                    recovery,
                    apply=apply,
                    queue_size=None,
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=False,
                    scan_errors=None,
                ) from error
            try:
                queued = _load_queue(self.config, queue_path)
            except Exception as error:
                raise _command_fatal(
                    "retry",
                    error,
                    recovery,
                    apply=apply,
                    queue_size=None,
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=False,
                    scan_errors=None,
                ) from error
            if not queued:
                return _receipt(
                    "retry",
                    recovery,
                    apply=apply,
                    queue_size=0,
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=True,
                    scan_errors=0,
                )
            try:
                discovery = discover_candidates(self.config)
            except Exception as error:
                raise _command_fatal(
                    "retry",
                    error,
                    recovery,
                    apply=apply,
                    queue_size=len(queued),
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=False,
                    scan_errors=None,
                ) from error
            if apply and discovery.scan_errors:
                error = FatalRepairError(
                    "candidate discovery is incomplete; refusing retry"
                )
                raise _command_fatal(
                    "retry",
                    error,
                    recovery,
                    apply=apply,
                    queue_size=len(queued),
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=False,
                    scan_errors=len(discovery.scan_errors),
                ) from error
            try:
                results, inspections = self._discovery_results(
                    discovery, queued, evaluate=True
                )
            except PartialProgressError as error:
                raise _command_fatal(
                    "retry",
                    error.cause,
                    recovery + error.results,
                    rollout_id=error.rollout_id,
                    apply=apply,
                    queue_size=len(queued),
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=not discovery.scan_errors,
                    scan_errors=len(discovery.scan_errors),
                ) from error.cause
            remaining = set(queued)
            completed_in_recovery = {
                str(item["rollout_id"])
                for item in recovery
                if item.get("queue_removed") is True
            }
            remaining.difference_update(completed_in_recovery)
            rewritten: List[Dict[str, Any]] = []
            deferred = {
                "missing",
                "source-missing",
                "mirror-missing",
                "unstable",
                "active-complete-prefix",
            }

            def retry_failure(
                error: Exception, rollout_id: Optional[str] = None
            ) -> CommandFatalError:
                try:
                    current_queue_size: Optional[int] = len(
                        _load_queue(self.config, queue_path)
                    )
                except Exception:
                    current_queue_size = None
                return _command_fatal(
                    "retry",
                    error,
                    recovery + rewritten,
                    rollout_id=rollout_id,
                    apply=apply,
                    queue_size=current_queue_size,
                    queue_path=str(queue_path or self.config.queue_path),
                    queue_enabled=apply,
                    complete=not discovery.scan_errors,
                    scan_errors=len(discovery.scan_errors),
                )

            for result in results:
                raw_rollout_id = result.get("rollout_id")
                if raw_rollout_id is None:
                    rewritten.append(dict(result))
                    continue
                rollout_id = str(raw_rollout_id)
                classification = str(result["classification"])
                if classification == "eligible":
                    if apply:
                        try:
                            rewritten.append(
                                self._repair_one(
                                    discovery.candidates[rollout_id],
                                    inspections[rollout_id],
                                    queue_enabled=True,
                                )
                            )
                            remaining.discard(rollout_id)
                        except CandidateUnsupportedError as error:
                            rewritten.append(
                                _result(
                                    rollout_id,
                                    "unsupported",
                                    "terminal",
                                    detail=str(error),
                                )
                            )
                            try:
                                _remove_queue_id(self.config, rollout_id, queue_path)
                            except Exception as queue_error:
                                raise retry_failure(
                                    queue_error, rollout_id
                                ) from queue_error
                            remaining.discard(rollout_id)
                        except SafeRolledBack as error:
                            rewritten.append(
                                _result(
                                    rollout_id,
                                    "unstable",
                                    "deferred",
                                    detail=str(error),
                                )
                            )
                        except Exception as error:
                            raise retry_failure(error, rollout_id) from error
                    else:
                        rewritten.append({**result, "outcome": "dry-run"})
                elif classification in deferred:
                    rewritten.append({**result, "outcome": "deferred"})
                else:
                    rewritten.append({**result, "outcome": "terminal"})
                    if apply:
                        try:
                            _remove_queue_id(self.config, rollout_id, queue_path)
                        except Exception as error:
                            raise retry_failure(error, rollout_id) from error
                        remaining.discard(rollout_id)
            if apply:
                try:
                    _write_queue(self.config, remaining, queue_path)
                except Exception as error:
                    raise retry_failure(error) from error
            receipt = _receipt(
                "retry",
                recovery + rewritten,
                apply=apply,
                queue_size=len(remaining) if apply else len(queued),
                queue_path=str(queue_path or self.config.queue_path),
                queue_enabled=apply,
                complete=not discovery.scan_errors,
                scan_errors=len(discovery.scan_errors),
            )
            if discovery.scan_errors:
                receipt["status"] = "incomplete"
            return receipt


def _normalize_rollout_ids(values: Sequence[str]) -> List[str]:
    try:
        return sorted({canonical_rollout_id(value) for value in values})
    except ValueError as error:
        raise FatalRepairError(str(error)) from error


def _validate_limit(name: str, value: Optional[int]) -> None:
    if value is not None and value < 0:
        raise FatalRepairError(f"{name} must be non-negative")


class _ReceiptArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FatalRepairError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ReceiptArgumentParser(
        prog="codex_repair_rollout_reflinks.py",
        description="Fail-closed APFS reflink repair for Codex rollout mirrors.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--rollout-id", action="append", default=[])
    inventory.add_argument("--json", action="store_true")

    repair = commands.add_parser("repair")
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--max-files", type=int)
    repair.add_argument("--max-bytes", type=int)
    repair.add_argument("--rollout-id", action="append", default=[])
    repair.add_argument("--queue-unstable", action="store_true")
    repair.add_argument("--json", action="store_true")

    retry = commands.add_parser("retry")
    retry.add_argument("--apply", action="store_true")
    retry.add_argument("--json", action="store_true")

    recover = commands.add_parser("recover")
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    backend: Optional[Backend] = None,
    environ: Optional[Mapping[str, str]] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    diagnostics = sys.stderr if stderr is None else stderr
    command = "unknown"
    try:
        arguments = _build_parser().parse_args(list(argv) if argv is not None else None)
        command = str(arguments.command)
        tool = RepairTool(Config.from_environment(environ), backend=backend)
        if command == "inventory":
            receipt = tool.inventory(arguments.rollout_id)
        elif command == "repair":
            receipt = tool.repair(
                apply=arguments.apply,
                max_files=arguments.max_files,
                max_bytes=arguments.max_bytes,
                rollout_ids=arguments.rollout_id,
                queue_unstable=arguments.queue_unstable,
            )
        elif command == "retry":
            receipt = tool.retry(apply=arguments.apply)
        elif command == "recover":
            receipt = tool.recover(apply=arguments.apply)
        else:
            raise FatalRepairError(f"unknown command: {command}")
        json.dump(receipt, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        return EXIT_OK
    except CommandFatalError as error:
        json.dump(error.receipt, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        diagnostics.write(f"{command}: {error}\n")
        return EXIT_FATAL
    except (FatalRepairError, UnsupportedError, SafetyError) as error:
        receipt = {
            "command": command,
            "status": "fatal",
            "summary": {"total": 1, "classifications": {"fatal": 1}},
            "results": [_result(None, "fatal", "failed", detail=str(error))],
        }
        json.dump(receipt, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        diagnostics.write(f"{command}: {error}\n")
        return EXIT_FATAL
    except Exception as error:
        receipt = {
            "command": command,
            "status": "fatal",
            "summary": {"total": 1, "classifications": {"fatal": 1}},
            "results": [
                _result(None, "fatal", "failed", detail=f"unexpected: {error}")
            ],
        }
        json.dump(receipt, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        diagnostics.write(f"{command}: unexpected failure: {error}\n")
        return EXIT_FATAL


class _DarwinAdapter:
    """Translate the held-FD Darwin backend into the core transaction API."""

    def __init__(self) -> None:
        backend_module = _load_darwin_backend_module()
        self.module = backend_module
        self.raw = backend_module.DarwinBackend()

    @staticmethod
    def _identity(raw_identity: Any) -> FileIdentity:
        return FileIdentity(device=int(raw_identity.dev), inode=int(raw_identity.ino))

    @staticmethod
    def _bytes_digest(items: Sequence[Tuple[bytes, bytes]]) -> str:
        digest = hashlib.sha256()
        for name, value in sorted(items):
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def _snapshot(self, raw_snapshot: Any) -> FileSnapshot:
        policy = raw_snapshot.policy
        return FileSnapshot(
            identity=self._identity(raw_snapshot.identity),
            size=int(raw_snapshot.identity.size),
            mtime_ns=int(policy.mtime_ns),
            nlink=int(raw_snapshot.identity.nlink),
            content_sha256=str(raw_snapshot.sha256),
            policy=PolicyFingerprint(
                uid=int(policy.uid),
                gid=int(policy.gid),
                mode=int(policy.mode),
                flags=int(policy.flags),
                acl_sha256=hashlib.sha256(policy.acl_native).hexdigest(),
                xattrs_sha256=self._bytes_digest(policy.xattrs),
            ),
        )

    def _raw_file_identity(self, snapshot: FileSnapshot) -> Any:
        return self.module.FileIdentity(
            dev=snapshot.identity.device,
            ino=snapshot.identity.inode,
            mode=stat.S_IFREG | snapshot.policy.mode,
            nlink=snapshot.nlink,
            size=snapshot.size,
            uid=snapshot.policy.uid,
            gid=snapshot.policy.gid,
            mtime_ns=snapshot.mtime_ns,
            ctime_ns=0,
        )

    def _raw_parent_identity(self, identity: FileIdentity) -> Any:
        return self.module.FileIdentity(
            dev=identity.device,
            ino=identity.inode,
            mode=stat.S_IFDIR | 0o700,
            nlink=1,
            size=0,
            uid=os.geteuid(),
            gid=os.getegid(),
            mtime_ns=0,
            ctime_ns=0,
        )

    def _expectation(self, snapshot: FileSnapshot) -> Any:
        return self.module.SnapshotExpectation(
            dev=snapshot.identity.device,
            ino=snapshot.identity.inode,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
            nlink=snapshot.nlink,
            sha256=snapshot.content_sha256,
            uid=snapshot.policy.uid,
            gid=snapshot.policy.gid,
            mode=snapshot.policy.mode,
            flags=snapshot.policy.flags,
            acl_sha256=snapshot.policy.acl_sha256,
            xattrs_sha256=snapshot.policy.xattrs_sha256,
        )

    def _mapped_error(self, error: Exception, *, inspection: bool) -> RepairError:
        if not isinstance(error, self.module.BackendError):
            if isinstance(error, OSError):
                return (
                    UnreadablePathError(str(error))
                    if inspection
                    else SafetyError(str(error))
                )
            return SafetyError(str(error))
        reason = str(error.reason)
        detail = str(error)
        errno_value = error.errno_value
        if reason == "exclusive_writer_required":
            return UnsupportedError(detail) if inspection else SafetyError(detail)
        if reason in {
            "source_parent_replaced",
            "source_object_replaced",
            "source_snapshot_mismatch",
            "source_snapshot_changed",
        }:
            # A source-specific replacement is retryable even when the raw
            # pathname error carries ENOENT.  Check the reason before errno so
            # archive/move races are not misclassified as an ordinary missing
            # candidate after a transaction has been bound.
            return UnstablePathError(detail)
        if errno_value == getattr(os, "ENOENT", 2) or reason in {
            "path_missing",
            "source_unavailable",
        }:
            return MissingPathError(detail)
        if reason in {
            "content_changed",
            "policy_unstable",
            "xattr_unstable",
            "acl_unstable",
            "snapshot_unstable",
            "generation_unstable",
            "original_policy_changed",
            "object_replaced",
            "identity_mismatch",
            "parent_replaced",
        }:
            return UnstablePathError(detail)
        if reason == "unsafe_link_count":
            return (
                UnsafeLinkCountError(detail)
                if inspection
                else UnstablePathError(detail)
            )
        if reason == "not_regular":
            return UnsupportedError(detail) if inspection else UnstablePathError(detail)
        if errno_value in (getattr(os, "ENOTSUP", 45), getattr(os, "EXDEV", 18)):
            return UnsupportedError(detail)
        if inspection:
            return UnreadablePathError(detail)
        return SafetyError(detail)

    def _mapped_prepared_error(self, error: Exception) -> RepairError:
        if isinstance(error, self.module.BackendError) and str(error.reason) in {
            "exclusive_writer_required",
            "original_policy_changed",
            "clone_policy_mismatch",
            "unsafe_bsd_flags",
        }:
            return UnstablePathError(str(error))
        return self._mapped_error(error, inspection=False)

    def _relation(
        self,
        source_fd: int,
        mirror_fd: int,
        source_size: int,
        mirror_size: int,
    ) -> ContentRelation:
        if source_size == mirror_size:
            return (
                ContentRelation.EXACT
                if self.raw.files_equal(source_fd, mirror_fd)
                else ContentRelation.DIFFERENT
            )
        if mirror_size <= 0 or mirror_size >= source_size:
            return ContentRelation.DIFFERENT
        offset = 0
        while offset < mirror_size:
            wanted = min(1024 * 1024, mirror_size - offset)
            source_block = os.pread(source_fd, wanted, offset)
            mirror_block = os.pread(mirror_fd, wanted, offset)
            if len(source_block) != wanted or len(mirror_block) != wanted:
                raise UnstablePathError("rollout changed during prefix comparison")
            if source_block != mirror_block:
                return ContentRelation.DIFFERENT
            offset += wanted
        if os.pread(mirror_fd, 1, mirror_size - 1) != b"\n":
            return ContentRelation.DIFFERENT
        return ContentRelation.MIRROR_COMPLETE_PREFIX

    def inspect_pair(
        self, source: pathlib.Path, mirror: pathlib.Path
    ) -> PairInspection:
        owners: List[Any] = []
        owned_handles: Tuple[Any, ...] = ()
        primary_error: Optional[BaseException] = None
        try:
            source_parent_owner, source_name = self.raw._open_absolute_parent_owned(
                str(source)
            )
            with source_parent_owner:
                owners.append(source_parent_owner)
                source_parent_fd = source_parent_owner.fileno()
            mirror_parent_owner, mirror_name = self.raw._open_absolute_parent_owned(
                str(mirror)
            )
            with mirror_parent_owner:
                owners.append(mirror_parent_owner)
                mirror_parent_fd = mirror_parent_owner.fileno()
            source_parent_before = self.raw.identity(source_parent_fd)
            mirror_parent_before = self.raw.identity(mirror_parent_fd)
            source_owner = self.raw._open_leaf_owned(source_parent_fd, source_name)
            with source_owner:
                owners.append(source_owner)
                source_fd = source_owner.fileno()
            mirror_owner = self.raw._open_leaf_owned(mirror_parent_fd, mirror_name)
            with mirror_owner:
                owners.append(mirror_owner)
                mirror_fd = mirror_owner.fileno()
            source_before = self.raw.snapshot_file(source_fd)
            mirror_before = self.raw.snapshot_file(mirror_fd)
            if source_before.identity.is_same_object(mirror_before.identity):
                raise SafetyError(
                    "source and mirror unexpectedly identify the same object"
                )
            relation = self._relation(
                source_fd,
                mirror_fd,
                int(source_before.identity.size),
                int(mirror_before.identity.size),
            )
            source_after = self.raw.snapshot_file(source_fd)
            mirror_after = self.raw.snapshot_file(mirror_fd)
            source_parent_after = self.raw.identity(source_parent_fd)
            mirror_parent_after = self.raw.identity(mirror_parent_fd)
            if source_before != source_after or mirror_before != mirror_after:
                raise UnstablePathError(
                    "protected file properties changed during inspection"
                )
            if not source_parent_before.is_same_object(source_parent_after):
                raise UnstablePathError("source parent was replaced during inspection")
            if not mirror_parent_before.is_same_object(mirror_parent_after):
                raise UnstablePathError("mirror parent was replaced during inspection")
            self.raw.require_exclusive_writer_policy(
                mirror_after.policy, "mirror candidate"
            )
            return PairInspection(
                source=self._snapshot(source_after),
                mirror=self._snapshot(mirror_after),
                source_parent=self._identity(source_parent_after),
                mirror_parent=self._identity(mirror_parent_after),
                relation=relation,
            )
        except RepairError as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise
        except Exception as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise self._mapped_error(error, inspection=True) from error
        except BaseException as error:
            try:
                _cleanup_guard = True
                primary_error = error
            except BaseException:
                primary_error = error
            raise
        finally:
            try:
                _cleanup_guard = True
                owned_handles = tuple(reversed(owners))
                active_primary = primary_error
                if active_primary is None:
                    current_error = sys.exc_info()[1]
                    if isinstance(current_error, BaseException):
                        active_primary = current_error
                _drain_primary_closeables(owned_handles, primary_error=active_primary)
            except BaseException as cleanup_error:
                try:
                    _cleanup_guard = True
                    if not owned_handles:
                        owned_handles = tuple(reversed(owners))
                    _drain_primary_closeables(
                        owned_handles,
                        primary_error=(
                            primary_error
                            if primary_error is not None
                            else cleanup_error
                        ),
                    )
                except BaseException:
                    if not owned_handles:
                        owned_handles = tuple(reversed(owners))
                    _drain_primary_closeables(
                        owned_handles,
                        primary_error=(
                            primary_error
                            if primary_error is not None
                            else cleanup_error
                        ),
                    )
                if primary_error is None:
                    raise cleanup_error

    def prepare(
        self,
        source: pathlib.Path,
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        expected: PairInspection,
        authorize_state: Callable[[str], None],
    ) -> BoundTransaction:
        stage_path = temporary.parent
        try:
            stage_identity = self.raw.create_private_stage(
                str(stage_path), authorize_state=authorize_state
            )
        except Exception as error:
            mapped = self._mapped_error(error, inspection=False)
            if isinstance(mapped, MissingPathError):
                mapped = UnstablePathError(
                    f"mirror parent changed before private stage creation: {mapped}"
                )
            raise mapped from error
        raw_transaction_owner: Optional[Any] = None
        owned_transaction: Optional[Any] = None
        transaction: Optional[Any] = None
        cleanup_error: Optional[BaseException] = None
        abort_receipt = _CleanupActionReceipt()
        stage_remove_receipt = _CleanupActionReceipt()

        def abort_bound_transaction() -> None:
            if raw_transaction_owner is None:
                raise SafetyError("bound transaction owner is unavailable for cleanup")
            raw_transaction_owner.transaction().abort_before_prepared(
                self._expectation(expected.mirror),
                authorize_state=authorize_state,
            )

        def remove_unbound_stage() -> None:
            self.raw.remove_empty_private_stage(
                str(stage_path),
                stage_identity,
                expected_container=self._raw_parent_identity(expected.mirror_parent),
                authorize_state=authorize_state,
            )

        try:
            raw_transaction_owner = self.raw.bind_transaction_owned(
                str(source),
                str(mirror),
                str(temporary),
                source_parent_expected=self._raw_parent_identity(
                    expected.source_parent
                ),
            )
            with raw_transaction_owner:
                raw_transaction_owner.retain_if_registered(
                    lambda: transaction is not None
                    and transaction._raw_owner is raw_transaction_owner
                )
                transaction = _DarwinTransaction(
                    self,
                    raw_transaction_owner,
                    stage_path,
                    authorize_state,
                    expected.mirror,
                    prepared_durable=False,
                )
                if transaction._raw_owner is not raw_transaction_owner:
                    raise SafetyError("live transaction owner registration changed")
            assert transaction is not None
            raw_transaction = transaction.raw
            raw_source_now = raw_transaction.source_snapshot()
            raw_mirror_now = raw_transaction.original_snapshot()
            self.raw.require_exclusive_writer_policy(
                raw_mirror_now.policy, "bound mirror before clone"
            )
            source_now = self._snapshot(raw_source_now)
            mirror_now = self._snapshot(raw_mirror_now)
            if source_now != expected.source or mirror_now != expected.mirror:
                raise UnstablePathError(
                    "source or mirror changed between inspection and transaction bind"
                )
            if self._identity(raw_transaction.source_parent_identity) != (
                expected.source_parent
            ):
                raise UnstablePathError("source parent changed before transaction bind")
            final_parent, temporary_parent = transaction.parent_identities()
            if final_parent != expected.mirror_parent:
                raise UnstablePathError("mirror parent changed before transaction bind")
            if temporary_parent != self._identity(stage_identity):
                raise SafetyError(
                    "private stage identity changed during transaction bind"
                )
            return transaction
        except BaseException as error:
            try:
                _cleanup_guard = True
                cleanup_error = None
                if (
                    raw_transaction_owner is not None
                    and not raw_transaction_owner.closed
                ):
                    owned_transaction = raw_transaction_owner
                    try:
                        cleanup_error = _complete_cleanup_action(
                            abort_receipt, abort_bound_transaction
                        )
                    finally:
                        try:
                            _cleanup_guard = True
                            _drain_primary_closeables(
                                (owned_transaction,), primary_error=error
                            )
                        except BaseException:
                            try:
                                _drain_primary_closeables(
                                    (owned_transaction,), primary_error=error
                                )
                            except BaseException as candidate:
                                if cleanup_error is None:
                                    cleanup_error = candidate
                else:
                    cleanup_error = _complete_cleanup_action(
                        stage_remove_receipt, remove_unbound_stage
                    )
            except BaseException as cleanup_interrupt:
                cleanup_owner = (
                    owned_transaction
                    if owned_transaction is not None
                    else raw_transaction_owner
                )
                if cleanup_owner is not None and not cleanup_owner.closed:
                    owned_transaction = cleanup_owner
                    candidate = _complete_cleanup_action(
                        abort_receipt, abort_bound_transaction
                    )
                    cleanup_error = candidate if candidate is not None else None
                else:
                    candidate = _complete_cleanup_action(
                        stage_remove_receipt, remove_unbound_stage
                    )
                    cleanup_error = candidate if candidate is not None else None
                if cleanup_owner is not None and not cleanup_owner.closed:
                    try:
                        _drain_primary_closeables((cleanup_owner,), primary_error=error)
                    except BaseException as candidate:
                        if cleanup_error is None:
                            cleanup_error = candidate
                if cleanup_error is None and not (
                    abort_receipt.completed or stage_remove_receipt.completed
                ):
                    cleanup_error = cleanup_interrupt
            try:
                _cleanup_guard = True
                if not isinstance(error, Exception):
                    # A trace/cancellation interruption is the first primary.  The
                    # durable PLANNED intent retains any cleanup uncertainty.
                    failure: BaseException = error
                elif cleanup_error is not None:
                    failure = SafetyError(
                        "transaction bind failed and private stage cleanup was not "
                        f"proved: {error}; cleanup: {cleanup_error}"
                    )
                elif isinstance(error, RepairError):
                    failure = error
                else:
                    failure = self._mapped_prepared_error(error)
                    if isinstance(failure, MissingPathError):
                        failure = UnstablePathError(
                            "source or mirror pathname changed before transaction "
                            f"bind: {failure}"
                        )
            except BaseException as reporting_error:
                if cleanup_error is None and isinstance(error, RepairError):
                    failure = error
                elif isinstance(error, Exception):
                    failure = SafetyError(
                        "transaction bind failure reporting was interrupted: "
                        f"{reporting_error}; original: {error}"
                    )
                else:
                    failure = error
            if failure is error:
                raise
            raise failure from error

    def resume(
        self,
        source: Optional[pathlib.Path],
        mirror: pathlib.Path,
        temporary: pathlib.Path,
        manifest: RepairManifest,
        authorize_state: Callable[[str], None],
    ) -> BoundTransaction:
        source_path: Optional[str] = None
        source_expected: Optional[Any] = None
        if source is not None and manifest.phase == Phase.PREPARED:
            source_path = str(source)
            source_expected = self._raw_file_identity(manifest.source_snapshot)
        raw_transaction_owner: Optional[Any] = None
        owned_transaction: Optional[Any] = None
        transaction: Optional[Any] = None
        try:
            raw_transaction_owner = self.raw.bind_recovery_owned(
                source_path,
                str(mirror),
                str(temporary),
                self._raw_file_identity(manifest.original_snapshot),
                self._raw_file_identity(manifest.clone_snapshot),
                source_expected=source_expected,
                source_parent_expected=(
                    None
                    if source_path is None
                    else self._raw_parent_identity(manifest.source_parent_identity)
                ),
                destination_parent_expected=self._raw_parent_identity(
                    manifest.final_parent_identity
                ),
                temporary_parent_expected=self._raw_parent_identity(
                    manifest.temporary_parent_identity
                ),
            )
            with raw_transaction_owner:
                raw_transaction_owner.retain_if_registered(
                    lambda: transaction is not None
                    and transaction._raw_owner is raw_transaction_owner
                )
                transaction = _DarwinTransaction(
                    self,
                    raw_transaction_owner,
                    temporary.parent,
                    authorize_state,
                    manifest.original_snapshot,
                    prepared_durable=True,
                )
                if transaction._raw_owner is not raw_transaction_owner:
                    raise SafetyError("recovery transaction owner registration changed")
            assert transaction is not None
            return transaction
        except BaseException as error:
            try:
                _cleanup_guard = True
                if raw_transaction_owner is not None:
                    owned_transaction = raw_transaction_owner
                    _drain_primary_closeables((owned_transaction,), primary_error=error)
            except BaseException:
                cleanup_owner = (
                    owned_transaction
                    if owned_transaction is not None
                    else raw_transaction_owner
                )
                if cleanup_owner is not None and not cleanup_owner.closed:
                    _drain_primary_closeables((cleanup_owner,), primary_error=error)
            try:
                _cleanup_guard = True
                failure = (
                    self._mapped_error(error, inspection=False)
                    if isinstance(error, Exception)
                    else error
                )
            except BaseException as reporting_error:
                if isinstance(error, Exception):
                    failure = SafetyError(
                        "recovery bind failure reporting was interrupted: "
                        f"{reporting_error}; original: {error}"
                    )
                else:
                    failure = error
            if failure is error:
                raise
            raise failure from error

    def recover_intent_stage(
        self,
        intent: RepairIntent,
        temporary: pathlib.Path,
        authorize_state: Callable[[str], None],
    ) -> str:
        stage_path = temporary.parent
        _, final_rel, _ = _manifest_paths_unchecked(intent)
        final_path = temporary.parent.parent / final_rel.name
        expected_stage = (
            None
            if intent.temporary_parent_identity is None
            else self._raw_parent_identity(intent.temporary_parent_identity)
        )
        expected_clone = (
            None
            if intent.clone_snapshot is None
            else self._raw_file_identity(intent.clone_snapshot)
        )
        expected_snapshot = (
            None
            if intent.clone_snapshot is None
            else self._expectation(intent.clone_snapshot)
        )
        allow_clone = intent.state in {
            IntentState.STAGE_BOUND,
            IntentState.CLONE_BOUND,
        }
        protected_source = (
            intent.clone_snapshot
            if intent.clone_snapshot is not None
            else intent.source_snapshot
        )
        try:
            return str(
                self.raw.cleanup_intent_stage(
                    str(stage_path),
                    final_path=str(final_path),
                    expected_container=self._raw_parent_identity(
                        intent.final_parent_identity
                    ),
                    expected_original=self._expectation(intent.original_snapshot),
                    expected_stage=expected_stage,
                    allow_clone=allow_clone,
                    expected_clone=expected_clone,
                    expected_snapshot=expected_snapshot,
                    expected_size=(protected_source.size if allow_clone else None),
                    expected_sha256=(
                        protected_source.content_sha256 if allow_clone else None
                    ),
                    authorize_state=authorize_state,
                )
            )
        except Exception as error:
            raise self._mapped_error(error, inspection=False) from error


class _DarwinTransaction:
    def __init__(
        self,
        adapter: _DarwinAdapter,
        raw_owner: Any,
        stage_path: pathlib.Path,
        authorize_state: Callable[[str], None],
        expected_original: FileSnapshot,
        *,
        prepared_durable: bool,
    ) -> None:
        self.adapter = adapter
        self._raw_owner: Optional[Any] = raw_owner
        self.raw = raw_owner.transaction()
        self.stage_path = stage_path
        self.authorize_state = authorize_state
        self.expected_original = expected_original
        self.prepared_durable = prepared_durable

    def _expect(self, actual: Any, expected: FileIdentity, label: str) -> None:
        if self.adapter._identity(actual) != expected:
            raise SafetyError(
                f"{label} identity does not match the durable expectation"
            )

    def mark_prepared(self) -> None:
        self.prepared_durable = True

    def clone(
        self, expected_source: FileSnapshot, expected_original: FileSnapshot
    ) -> FileSnapshot:
        try:
            self._expect(self.raw.source_identity, expected_source.identity, "source")
            self._expect(
                self.raw.original_identity, expected_original.identity, "original"
            )
            self.raw.clone(authorize_state=self.authorize_state)
            original_live = self.raw.original_snapshot()
            self.adapter.raw.require_exclusive_writer_policy(
                original_live.policy, "original mirror before clone policy calibration"
            )
            if self.adapter._snapshot(original_live) != expected_original:
                raise UnstablePathError(
                    "original mirror changed before policy calibration"
                )
            self.raw.calibrate_clone_policy(original_live.policy)
            clone = self.raw.clone_snapshot()
            self.adapter.raw.require_exclusive_writer_policy(
                clone.policy, "calibrated clone"
            )
            self.raw.backend.full_fsync(self.raw.clone_fd)
            raw_source_after = self.raw.source_snapshot()
            raw_original_after = self.raw.original_snapshot()
            self.adapter.raw.require_exclusive_writer_policy(
                raw_original_after.policy, "original mirror after clone preparation"
            )
            source_after = self.adapter._snapshot(raw_source_after)
            original_after = self.adapter._snapshot(raw_original_after)
            if source_after != expected_source or original_after != expected_original:
                raise UnstablePathError(
                    "source or mirror changed while preparing clone"
                )
            return self.adapter._snapshot(clone)
        except RepairError:
            raise
        except Exception as error:
            raise self.adapter._mapped_prepared_error(error) from error

    def parent_identities(self) -> Tuple[FileIdentity, FileIdentity]:
        return (
            self.adapter._identity(self.raw.destination_parent_identity),
            self.adapter._identity(self.raw.temporary_parent_identity),
        )

    def orientation(
        self, expected_original: FileIdentity, expected_clone: FileIdentity
    ) -> Orientation:
        self._expect(self.raw.original_identity, expected_original, "original")
        self._expect(self.raw.clone_identity, expected_clone, "clone")
        try:
            value = self.raw.orientation()
        except self.adapter.module.BackendError as error:
            if error.reason == "orientation_unknown":
                return Orientation.IMPOSSIBLE
            raise self.adapter._mapped_error(error, inspection=False) from error
        return {
            "before": Orientation.ORIGINAL_FINAL_CLONE_TEMP,
            "forward": Orientation.CLONE_FINAL_ORIGINAL_TEMP,
            "committed": Orientation.CLONE_FINAL_TEMP_MISSING,
            "rolled_back": Orientation.ORIGINAL_FINAL_TEMP_MISSING,
        }.get(value, Orientation.IMPOSSIBLE)

    def swap_forward(
        self, expected_original: FileIdentity, expected_clone: FileIdentity
    ) -> None:
        self._expect(self.raw.original_identity, expected_original, "original")
        self._expect(self.raw.clone_identity, expected_clone, "clone")
        try:
            self.raw.swap_forward(authorize_state=self.authorize_state)
        except Exception as error:
            raise self._mapped_pre_forward_error(error) from error

    def revalidate_before_prepared(
        self,
        expected_source: FileSnapshot,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None:
        if self.prepared_durable:
            raise SafetyError(
                "pre-PREPARED revalidation cannot run after durable PREPARED"
            )
        try:
            self.raw.revalidate_pre_forward(
                self.adapter._expectation(expected_source),
                self.adapter._expectation(expected_original),
                self.adapter._expectation(expected_clone),
            )
        except Exception as error:
            raise self._mapped_pre_forward_error(error) from error

    def _mapped_pre_forward_error(self, error: Exception) -> RepairError:
        if isinstance(error, self.adapter.module.BackendError):
            reason = str(error.reason)
            if reason in {
                "source_snapshot_mismatch",
                "source_snapshot_changed",
                "source_object_replaced",
                "source_parent_replaced",
            }:
                return UnstablePathError(str(error))
            if not self.prepared_durable and reason in {
                "exclusive_writer_required",
                "original_snapshot_mismatch",
                "original_snapshot_changed",
                "clone_snapshot_mismatch",
                "clone_snapshot_changed",
            }:
                return UnstablePathError(str(error))
        mapped = self.adapter._mapped_error(error, inspection=False)
        if isinstance(mapped, MissingPathError):
            return UnstablePathError(str(mapped))
        return mapped

    def revalidate_pre_forward(
        self,
        expected_source: FileSnapshot,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None:
        try:
            self.raw.revalidate_pre_forward(
                self.adapter._expectation(expected_source),
                self.adapter._expectation(expected_original),
                self.adapter._expectation(expected_clone),
            )
        except Exception as error:
            raise self._mapped_pre_forward_error(error) from error

    def _verify_forward_mapping(self) -> None:
        try:
            self.raw.verify_forward()
        except Exception as error:
            if isinstance(error, self.adapter.module.BackendError) and str(
                error.reason
            ) in {
                "source_object_replaced",
                "source_parent_replaced",
                "source_snapshot_mismatch",
                "source_snapshot_changed",
            }:
                raise UnstablePathError(
                    f"source changed after forward swap: {error}"
                ) from error
            raise SafetyError(
                f"forward namespace verification failed: {error}"
            ) from error

    def postverify(
        self, expected_source: FileSnapshot, expected_clone: FileSnapshot
    ) -> FileSnapshot:
        self._verify_forward_mapping()
        try:
            clone = self.adapter._snapshot(self.raw.clone_snapshot())
        except Exception as error:
            raise SafetyError(f"final clone revalidation failed: {error}") from error
        if clone != expected_clone:
            raise SafetyError("final clone protected properties changed")
        if self.raw.source_fd >= 0:
            try:
                source = self.adapter._snapshot(self.raw.source_snapshot())
            except Exception as error:
                if isinstance(error, self.adapter.module.BackendError) and str(
                    error.reason
                ) in {
                    "source_object_replaced",
                    "source_snapshot_mismatch",
                    "source_snapshot_changed",
                    "content_changed",
                    "policy_unstable",
                    "xattr_unstable",
                    "acl_unstable",
                }:
                    raise UnstablePathError(
                        f"source pathname or content changed after forward swap: {error}"
                    ) from error
                mapped = self.adapter._mapped_error(error, inspection=False)
                if isinstance(mapped, (MissingPathError, UnstablePathError)):
                    raise UnstablePathError(
                        f"source pathname or content changed after forward swap: {mapped}"
                    ) from error
                raise SafetyError(f"source revalidation failed: {mapped}") from error
            if source != expected_source:
                raise UnstablePathError("source changed after forward swap")
        # The source snapshot may be long.  Rebind every pathname after it so
        # an archive/move cannot authorize committing a clone of the old name.
        self._verify_forward_mapping()
        return clone

    def revalidate_forward(
        self,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None:
        try:
            if self.raw.orientation() != "forward":
                raise SafetyError(
                    "forward revalidation found an unexpected orientation"
                )
            original = self.adapter._snapshot(self.raw.original_snapshot())
            clone = self.adapter._snapshot(self.raw.clone_snapshot())
        except RepairError:
            raise
        except Exception as error:
            raise SafetyError(
                f"forward protected snapshots could not be revalidated: {error}"
            ) from error
        if original != expected_original or clone != expected_clone:
            raise SafetyError(
                "forward protected snapshots changed before namespace mutation"
            )

    def revalidate_committed(self, expected_clone: FileSnapshot) -> None:
        try:
            self.raw.revalidate_committed(self.adapter._expectation(expected_clone))
        except Exception as error:
            raise SafetyError(
                f"committed clone snapshot could not be revalidated: {error}"
            ) from error

    def revalidate_rolled_back(self, expected_original: FileSnapshot) -> None:
        try:
            self.raw.revalidate_rolled_back(
                self.adapter._expectation(expected_original)
            )
        except Exception as error:
            raise SafetyError(
                f"rolled-back original snapshot could not be revalidated: {error}"
            ) from error

    def revalidate_before_cleanup(
        self,
        expected_original: FileSnapshot,
        expected_clone: FileSnapshot,
    ) -> None:
        try:
            if self.raw.orientation() != "before":
                raise SafetyError("rollback cleanup found an unexpected orientation")
            original = self.adapter._snapshot(self.raw.original_snapshot())
            clone = self.adapter._snapshot(self.raw.clone_snapshot())
        except RepairError:
            raise
        except Exception as error:
            raise SafetyError(
                f"rollback protected snapshots could not be revalidated: {error}"
            ) from error
        if original != expected_original or clone != expected_clone:
            raise SafetyError(
                "rollback protected snapshots changed before clone cleanup"
            )

    def unlink_original(
        self,
        expected_original: FileIdentity,
        expected_clone: FileSnapshot,
    ) -> None:
        self._expect(self.raw.original_identity, expected_original, "original")
        try:
            self.raw.unlink_original(
                self.adapter._expectation(expected_clone),
                authorize_state=self.authorize_state,
            )
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def swap_back(
        self, expected_clone: FileIdentity, expected_original: FileIdentity
    ) -> None:
        self._expect(self.raw.clone_identity, expected_clone, "clone")
        self._expect(self.raw.original_identity, expected_original, "original")
        try:
            self.raw.swap_back(authorize_state=self.authorize_state)
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def unlink_clone(
        self,
        expected_clone: FileIdentity,
        expected_original: FileSnapshot,
    ) -> None:
        self._expect(self.raw.clone_identity, expected_clone, "clone")
        try:
            self.raw.unlink_clone(
                self.adapter._expectation(expected_original),
                authorize_state=self.authorize_state,
            )
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def sync_namespaces(self) -> None:
        try:
            self.raw.backend.fsync(self.raw.destination_parent_fd)
            if self.raw.temporary_parent_fd >= 0:
                self.raw.backend.fsync(self.raw.temporary_parent_fd)
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def cleanup_stage(self) -> None:
        try:
            self.raw.remove_empty_stage_parent(authorize_state=self.authorize_state)
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def abort_before_prepared(self) -> None:
        try:
            self.raw.abort_before_prepared(
                self.adapter._expectation(self.expected_original),
                authorize_state=self.authorize_state,
            )
        except Exception as error:
            raise self.adapter._mapped_error(error, inspection=False) from error

    def close(self) -> None:
        owner = self._raw_owner
        try:
            if owner is not None:
                try:
                    _cleanup_guard = True
                    _drain_primary_closeables((owner,))
                except BaseException as cleanup_error:
                    try:
                        _cleanup_guard = True
                        _drain_primary_closeables((owner,), primary_error=cleanup_error)
                    except BaseException:
                        _drain_primary_closeables((owner,), primary_error=cleanup_error)
                    raise
        finally:
            if owner is not None and owner.closed and self._raw_owner is owner:
                self._raw_owner = None


def _default_backend() -> Backend:
    if sys.platform != "darwin":
        raise UnsupportedError("strict reflink repair is supported only on Darwin")
    try:
        return _DarwinAdapter()
    except (ImportError, OSError) as error:
        raise UnsupportedError(
            f"cannot load Darwin reflink backend: {error}"
        ) from error


if __name__ == "__main__":
    raise SystemExit(main())
