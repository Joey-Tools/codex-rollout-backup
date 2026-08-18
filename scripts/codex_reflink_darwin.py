#!/usr/bin/env python3
"""Fail-closed Darwin filesystem primitives for rollout reflink repair.

This module deliberately contains no CLI, pair discovery, queue handling, or
recovery state machine.  Callers retain the durable transaction policy; this
module retains file descriptors and enforces object-identity preconditions for
every namespace mutation.

Every tool-created namespace mutation requires caller-supplied authorization.
Reversible or destructive repair mutations require durable-state
authorization.  One-way mirror publication instead uses immediate live
bindings plus its receipt and post-error orientation contract.  Unlink
authorization precedes the final full survivor snapshot; the backend then
revalidates namespace identities and unlinks without another long or
externally controlled operation.  Callback failures propagate without
performing that mutation.

The mirror and every possible final survivor must satisfy the conservative
exclusive-writer policy: effective-uid ownership, no group/other write bits,
no extended ACL, and only safe user-settable BSD flags.  This reduces the last
content-write interval to the documented same-euid boundary.

The namespace contract assumes the caller holds the repair lock and enforces a
quiet window.  Owner-private stage directories and non-writable, ACL-free stage
containers exclude other principals.  Stable canonical ancestors above each
validated direct parent are a caller prerequisite.  Darwin has no
inode-conditional unlink or rmdir, so actively malicious same-euid replacement
in the final checked-name to syscall interval is explicitly outside this
backend's guarantee.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import types
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


CLONE_ACL = 0x0004
RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004
RENAME_NOFOLLOW_ANY = 0x00000010
ACL_TYPE_EXTENDED = 0x00000100
F_FULLFSYNC = 51
COPYFILE_ALL = 0x0000000F

_O_NOFOLLOW = 0x00000100
_O_DIRECTORY = 0x00100000
_O_CLOEXEC = 0x01000000

_ACL_FIRST_ENTRY = 0
_XATTR_OPTIONS = 0
_POLICY_SNAPSHOT_ATTEMPTS = 3
_CONTENT_CHUNK_SIZE = 1024 * 1024
_SNAPSHOT_MUTATION_REASONS = {
    "acl_unstable",
    "content_changed",
    "object_replaced",
    "policy_unstable",
    "unsafe_link_count",
    "xattr_unstable",
}
_PATH_REPLACEMENT_ERRNOS = {
    errno.ENOENT,
    errno.ENOTDIR,
    errno.ELOOP,
    getattr(errno, "ESTALE", 70),
}

_UF_NODUMP = 0x00000001
_UF_IMMUTABLE = 0x00000002
_UF_APPEND = 0x00000004
_UF_OPAQUE = 0x00000008
_UF_NOUNLINK = 0x00000010
_UF_COMPRESSED = 0x00000020
_UF_TRACKED = 0x00000040
_UF_DATAVAULT = 0x00000080
_UF_HIDDEN = 0x00008000
_SF_ARCHIVED = 0x00010000
_SF_IMMUTABLE = 0x00020000
_SF_APPEND = 0x00040000
_SF_RESTRICTED = 0x00080000
_SF_NOUNLINK = 0x00100000
_SF_FIRMLINK = 0x00800000
_SF_DATALESS = 0x40000000

_BLOCKING_FLAGS = (
    _UF_IMMUTABLE
    | _UF_APPEND
    | _UF_NOUNLINK
    | _SF_IMMUTABLE
    | _SF_APPEND
    | _SF_NOUNLINK
)
_SAFE_SETTABLE_FLAGS = _UF_NODUMP | _UF_OPAQUE | _UF_HIDDEN | _SF_ARCHIVED
_INTERNAL_FLAGS = (
    _UF_COMPRESSED
    | _UF_TRACKED
    | _UF_DATAVAULT
    | _SF_RESTRICTED
    | _SF_FIRMLINK
    | _SF_DATALESS
)
_KNOWN_FLAGS = _BLOCKING_FLAGS | _SAFE_SETTABLE_FLAGS | _INTERNAL_FLAGS

_PROTECTED_XATTRS = {
    b"com.apple.ResourceFork",
    b"com.apple.decmpfs",
}
_PROTECTED_XATTR_PREFIXES = (
    b"com.apple.fs.",
    b"com.apple.system.",
)


class _RetrySnapshot(Exception):
    pass


class BackendError(RuntimeError):
    """A classified backend failure suitable for a retry/state-machine decision."""

    def __init__(self, reason: str, detail: str, errno_value: Optional[int] = None):
        self.reason = reason
        self.detail = detail
        self.errno_value = errno_value
        super().__init__(f"{reason}: {detail}")


def _primary_before_handler_interruption(
    escaped: BaseException,
) -> Tuple[BaseException, Optional[types.TracebackType]]:
    """Recover a natural primary replaced while its handler was running."""
    context = escaped.__context__
    if context is None or escaped.__cause__ is context:
        return escaped, escaped.__traceback__
    traceback = context.__traceback__
    DarwinBackend._attach_cleanup_diagnostic(context, escaped)
    try:
        escaped.__traceback__ = None
        escaped.__context__ = None
    except BaseException:
        pass
    return context, traceback


@dataclass
class _FDState:
    backend: "DarwinBackend"
    subject: str
    fd: int = -1

    def close(
        self,
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        fd = self.fd
        if fd < 0:
            return
        try:
            # fmt: off
            self.fd = -1; os.close(fd)  # noqa: E702
        # fmt: on
        except BaseException as exc:
            close_failure = BackendError(
                "close_failed",
                f"{self.subject} fd {fd}: {DarwinBackend._exception_diagnostic(exc)}",
            )
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(primary_error, close_failure)
                return
            if not durable_namespace_complete:
                raise close_failure

    def __del__(self) -> None:
        for _attempt in range(2):
            if self.fd < 0:
                return
            try:
                _cleanup_attempt = _attempt + 1
                self.close()
            except BaseException:
                continue


class _OwnedFD:
    """Lazy, idempotent ownership for one file descriptor.

    Acquisition happens only in ``__enter__`` so an exceptional context exit
    can drain every Python-observable successful handoff.  A normal exit keeps
    the owner live, allowing callers to transfer the owner object itself rather
    than briefly creating two raw-FD owners.
    """

    def __init__(
        self,
        backend: "DarwinBackend",
        subject: str,
        acquire: Optional[Callable[["_OwnedFD"], None]] = None,
    ) -> None:
        self._backend = backend
        self._subject = subject
        self._acquire = acquire
        self._state = _FDState(backend, subject)
        self._entered = False
        self._retained_on_exception = False
        self._retain_if_registered: Optional[Callable[[], bool]] = None
        self._neutralize_on_exception_if: Optional[Callable[[], bool]] = None

    @property
    def closed(self) -> bool:
        return self._state.fd < 0

    def fileno(self) -> int:
        fd = self._state.fd
        if fd < 0:
            raise BackendError("fd_closed", f"{self._subject} is not open")
        return fd

    def _adopt(self, fd: int) -> None:
        if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
            raise BackendError("invalid_fd", f"{self._subject} has an invalid fd")
        if not self.closed:
            raise BackendError("fd_already_owned", f"{self._subject} is already open")
        self._state.fd = fd

    def _share_from(self, other: "_OwnedFD") -> None:
        if not isinstance(other, _OwnedFD) or other.closed:
            raise BackendError(
                "invalid_fd_owner", f"{self._subject} source is not open"
            )
        if not self.closed:
            raise BackendError("fd_already_owned", f"{self._subject} is already open")
        self._state = other._state

    def retain_on_exception(self) -> "_OwnedFD":
        self._retained_on_exception = True
        return self

    def retain_if_registered(self, predicate: Callable[[], bool]) -> "_OwnedFD":
        """Retain only after a caller-provided owner slot contains this owner."""
        if not callable(predicate):
            raise BackendError(
                "invalid_owner_registration",
                f"{self._subject} registration predicate is not callable",
            )
        self._retain_if_registered = predicate
        return self

    def neutralize_on_exception_if(self, predicate: Callable[[], bool]) -> "_OwnedFD":
        """Disarm a duplicate raw-FD owner when a caller can prove aliasing."""
        if not callable(predicate):
            raise BackendError(
                "invalid_owner_registration",
                f"{self._subject} neutralization predicate is not callable",
            )
        self._neutralize_on_exception_if = predicate
        return self

    def clear_exception_neutralizer(self) -> None:
        self._neutralize_on_exception_if = None

    def transfer(self) -> "_OwnedFD":
        """Mark a caller-installed owner as retained across exceptional exit."""
        return self.retain_on_exception()

    def _should_retain_on_exception(
        self, primary_error: Optional[BaseException]
    ) -> bool:
        predicate = self._retain_if_registered
        if self._retained_on_exception:
            return True
        if predicate is None:
            return False
        try:
            registered = predicate()
            if type(registered) is not bool:
                raise BackendError(
                    "fd_registration_probe_failed",
                    f"{self._subject} registration predicate returned a non-boolean",
                )
            return registered
        except BaseException as cleanup_failure:
            diagnostic = self._backend._exception_diagnostic(cleanup_failure)
            try:
                cleanup_failure.__traceback__ = None
            except BaseException:
                pass
            if primary_error is not None:
                self._backend._attach_cleanup_diagnostic(
                    primary_error,
                    BackendError(
                        "fd_registration_probe_failed",
                        diagnostic,
                    ),
                )
            return True

    def _neutralize_exception_alias(
        self, primary_error: Optional[BaseException]
    ) -> bool:
        predicate = self._neutralize_on_exception_if
        if predicate is None:
            return False
        try:
            aliased = predicate()
            if type(aliased) is not bool:
                raise BackendError(
                    "fd_alias_probe_failed",
                    f"{self._subject} alias predicate returned a non-boolean",
                )
            if not aliased:
                self._neutralize_on_exception_if = None
                return False
            # fmt: off
            self._neutralize_on_exception_if = None; self._retain_if_registered = None; self.disarm(); return True  # noqa: E702
        # fmt: on
        except BaseException as cleanup_failure:
            if primary_error is not None:
                self._backend._attach_cleanup_diagnostic(
                    primary_error,
                    BackendError(
                        "fd_alias_probe_failed",
                        self._backend._exception_diagnostic(cleanup_failure),
                    ),
                )
            raise

    def _drain(
        self,
        *,
        primary_error: Optional[BaseException],
        durable_namespace_complete: bool = False,
    ) -> None:
        first_failure: Optional[BaseException] = None
        for _attempt in range(2):
            if self.closed:
                break
            try:
                _cleanup_attempt = _attempt + 1
                self.close(
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            except BaseException as cleanup_failure:
                if first_failure is None:
                    first_failure = cleanup_failure
                if primary_error is not None:
                    self._backend._attach_cleanup_diagnostic(
                        primary_error, cleanup_failure
                    )
        if not self.closed:
            incomplete = BackendError(
                "close_failed", f"{self._subject} remained open after two attempts"
            )
            if primary_error is not None:
                self._backend._attach_cleanup_diagnostic(primary_error, incomplete)
                return
            if first_failure is None:
                raise incomplete
        if first_failure is not None and primary_error is None:
            raise first_failure

    def _handle_acquisition_failure(self, primary_error: BaseException) -> None:
        self._drain(primary_error=primary_error)

    def _acquisition_primary(
        self, escaped: BaseException
    ) -> Tuple[BaseException, Optional[types.TracebackType]]:
        return escaped, escaped.__traceback__

    def _handle_exception(self, primary_error: Optional[BaseException]) -> None:
        if self._neutralize_exception_alias(primary_error):
            return
        if self._should_retain_on_exception(primary_error):
            self._retained_on_exception = True
            self._retain_if_registered = None
            self._neutralize_on_exception_if = None
            return
        self._retain_if_registered = None
        self._neutralize_on_exception_if = None
        self._drain(primary_error=primary_error)

    def close(
        self,
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        self._retain_if_registered = None
        self._neutralize_on_exception_if = None
        self._state.close(
            primary_error=primary_error,
            durable_namespace_complete=durable_namespace_complete,
        )

    def disarm(self) -> int:
        """Return the raw fd for a legacy API and relinquish all shared owners."""
        fd = self.fileno()
        # fmt: off
        self._state.fd = -1; return fd  # noqa: E702
        # fmt: on

    def __enter__(self) -> "_OwnedFD":
        if self._entered:
            raise BackendError("fd_owner_reentered", f"{self._subject} was re-entered")
        self._entered = True
        try:
            if self._acquire is not None:
                self._acquire(self)
            if self.closed:
                raise BackendError(
                    "fd_acquisition_failed", f"{self._subject} acquired no fd"
                )
            return self
        except BaseException as escaped:
            try:
                _handler_attempt = 1
                primary, primary_traceback = self._acquisition_primary(escaped)
                self._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                primary, primary_traceback = self._acquisition_primary(escaped)
                self._backend._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _handler_attempt = 2
                    self._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    self._backend._attach_cleanup_diagnostic(primary, retry_failure)
            if primary is not escaped or primary.__traceback__ is not primary_traceback:
                raise primary.with_traceback(primary_traceback)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self._retain_if_registered = None
            self._neutralize_on_exception_if = None
            return
        try:
            _handler_attempt = 1
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            self._handle_exception(primary_error)
        except BaseException as cleanup_failure:
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if primary_error is None:
                try:
                    _handler_attempt = 2
                    self._handle_exception(primary_error)
                finally:
                    raise
            self._backend._attach_cleanup_diagnostic(primary_error, cleanup_failure)
            try:
                _handler_attempt = 2
                self._handle_exception(primary_error)
            except BaseException as retry_failure:
                self._backend._attach_cleanup_diagnostic(primary_error, retry_failure)


class _HandlerRecoveringOwnedFD(_OwnedFD):
    """FD owner that restores a natural primary interrupted in its handler."""

    def __init__(
        self,
        backend: "DarwinBackend",
        subject: str,
        acquire: Callable[["_OwnedFD"], None],
        handler_primary: Callable[
            [], Optional[Tuple[BaseException, Optional[types.TracebackType]]]
        ],
    ) -> None:
        super().__init__(backend, subject, acquire)
        self._handler_primary = handler_primary

    def _acquisition_primary(
        self, escaped: BaseException
    ) -> Tuple[BaseException, Optional[types.TracebackType]]:
        registered = self._handler_primary()
        if registered is not None:
            primary, traceback = registered
            if escaped is not primary:
                self._backend._attach_cleanup_diagnostic(primary, escaped)
                try:
                    escaped.__traceback__ = None
                    escaped.__context__ = None
                except BaseException:
                    pass
            return primary, traceback
        if escaped.__suppress_context__:
            return escaped, escaped.__traceback__
        context = escaped.__context__
        traceback = context.__traceback__ if context is not None else None
        while traceback is not None:
            frame = traceback.tb_frame
            if (
                frame.f_code is self._acquire.__code__
                and frame.f_locals.get("target") is self
            ):
                return _primary_before_handler_interruption(escaped)
            traceback = traceback.tb_next
        return escaped, escaped.__traceback__


@dataclass
class _ACLState:
    backend: "DarwinBackend"
    subject: str
    pointer: Optional[ctypes.c_void_p] = None
    active: bool = False

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        if not self.active:
            return
        pointer = self.pointer
        if pointer is None:
            # fmt: off
            self.pointer = None; self.active = False; return  # noqa: E702
            # fmt: on
        ctypes.set_errno(0)
        result = -1
        try:
            # fmt: off
            self.pointer = None; self.active = False; result = self.backend._acl_free(pointer)  # noqa: E702
            # fmt: on
            if result != 0:
                self.backend._raise_errno("acl_free_failed", "acl_free")
        except BaseException as exc:
            cleanup_failure = BackendError(
                "acl_free_failed",
                f"{self.subject}: {DarwinBackend._exception_diagnostic(exc)}",
            )
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(primary_error, cleanup_failure)
                return
            raise cleanup_failure

    def __del__(self) -> None:
        for _attempt in range(2):
            if not self.active:
                return
            try:
                _cleanup_attempt = _attempt + 1
                self.close()
            except BaseException:
                continue


class _OwnedACL:
    """Lazy, idempotent ownership for an optional Darwin ACL pointer."""

    def __init__(
        self,
        backend: "DarwinBackend",
        subject: str,
        acquire: Callable[["_OwnedACL"], None],
    ) -> None:
        self._backend = backend
        self._subject = subject
        self._acquire = acquire
        self._state = _ACLState(backend, subject)
        self._entered = False
        self._retained_on_exception = False

    @property
    def closed(self) -> bool:
        return not self._state.active

    def pointer(self) -> Optional[ctypes.c_void_p]:
        if self.closed:
            raise BackendError("acl_closed", f"{self._subject} is not active")
        return self._state.pointer

    def _adopt(self, pointer: Optional[ctypes.c_void_p]) -> None:
        if not self.closed:
            raise BackendError(
                "acl_already_owned", f"{self._subject} is already active"
            )
        self._state.pointer = pointer
        self._state.active = True

    def _share_from(self, other: "_OwnedACL") -> None:
        if not isinstance(other, _OwnedACL) or other.closed:
            raise BackendError(
                "invalid_acl_owner", f"{self._subject} source is not active"
            )
        if not self.closed:
            raise BackendError(
                "acl_already_owned", f"{self._subject} is already active"
            )
        self._state = other._state

    def retain_on_exception(self) -> "_OwnedACL":
        self._retained_on_exception = True
        return self

    def transfer(self) -> "_OwnedACL":
        return self.retain_on_exception()

    def _drain(self, *, primary_error: Optional[BaseException]) -> None:
        first_failure: Optional[BaseException] = None
        for _attempt in range(2):
            if self.closed:
                break
            try:
                _cleanup_attempt = _attempt + 1
                self.close(primary_error=primary_error)
            except BaseException as cleanup_failure:
                if first_failure is None:
                    first_failure = cleanup_failure
                if primary_error is not None:
                    self._backend._attach_cleanup_diagnostic(
                        primary_error, cleanup_failure
                    )
        if not self.closed:
            incomplete = BackendError(
                "acl_free_failed",
                f"{self._subject} remained active after two attempts",
            )
            if primary_error is not None:
                self._backend._attach_cleanup_diagnostic(primary_error, incomplete)
                return
            if first_failure is None:
                raise incomplete
        if first_failure is not None and primary_error is None:
            raise first_failure

    def _handle_acquisition_failure(self, primary_error: BaseException) -> None:
        self._drain(primary_error=primary_error)

    def _acquisition_primary(
        self, escaped: BaseException
    ) -> Tuple[BaseException, Optional[types.TracebackType]]:
        return escaped, escaped.__traceback__

    def _handle_exception(self, primary_error: Optional[BaseException]) -> None:
        if self._retained_on_exception:
            return
        self._drain(primary_error=primary_error)

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        self._state.close(primary_error=primary_error)

    def disarm(self) -> Optional[ctypes.c_void_p]:
        pointer = self.pointer()
        # fmt: off
        self._state.pointer = None; self._state.active = False; return pointer  # noqa: E702
        # fmt: on

    def __enter__(self) -> "_OwnedACL":
        if self._entered:
            raise BackendError("acl_owner_reentered", f"{self._subject} was re-entered")
        self._entered = True
        try:
            self._acquire(self)
            if self.closed:
                raise BackendError(
                    "acl_acquisition_failed", f"{self._subject} acquired no ACL state"
                )
            return self
        except BaseException as escaped:
            try:
                _handler_attempt = 1
                primary, primary_traceback = self._acquisition_primary(escaped)
                self._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                primary, primary_traceback = self._acquisition_primary(escaped)
                self._backend._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _handler_attempt = 2
                    self._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    self._backend._attach_cleanup_diagnostic(primary, retry_failure)
            if primary is not escaped or primary.__traceback__ is not primary_traceback:
                raise primary.with_traceback(primary_traceback)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            return
        try:
            _handler_attempt = 1
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            self._handle_exception(primary_error)
        except BaseException as cleanup_failure:
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if primary_error is None:
                try:
                    _handler_attempt = 2
                    self._handle_exception(primary_error)
                finally:
                    raise
            self._backend._attach_cleanup_diagnostic(primary_error, cleanup_failure)
            try:
                _handler_attempt = 2
                self._handle_exception(primary_error)
            except BaseException as retry_failure:
                self._backend._attach_cleanup_diagnostic(primary_error, retry_failure)


@dataclass
class _TransactionState:
    transaction: Optional["BoundTransaction"] = None

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        transaction = self.transaction
        if transaction is None:
            return
        first_failure: Optional[BaseException] = None
        complete = False
        for _attempt in range(2):
            try:
                transaction.close(primary_error=primary_error)
            except BaseException as cleanup_failure:
                if first_failure is None:
                    first_failure = cleanup_failure
                if primary_error is not None:
                    DarwinBackend._attach_cleanup_diagnostic(
                        primary_error, cleanup_failure
                    )
            complete = getattr(transaction, "_closed", True)
            if complete:
                break
        if complete:
            self.transaction = None
        elif primary_error is not None:
            DarwinBackend._attach_cleanup_diagnostic(
                primary_error,
                BackendError(
                    "close_failed",
                    "transaction remained active after two close attempts",
                ),
            )
        if first_failure is not None and primary_error is None:
            raise first_failure

    def __del__(self) -> None:
        for _attempt in range(2):
            if self.transaction is None:
                return
            try:
                _cleanup_attempt = _attempt + 1
                self.close()
            except BaseException:
                continue


class _OwnedTransaction:
    """Lazy owner for a BoundTransaction and all FDs retained by it."""

    def __init__(
        self,
        acquire: Callable[["_OwnedTransaction"], None],
    ) -> None:
        self._acquire = acquire
        self._state = _TransactionState()
        self._entered = False
        self._retained_on_exception = False
        self._retain_if_registered: Optional[Callable[[], bool]] = None

    @property
    def closed(self) -> bool:
        return self._state.transaction is None

    def transaction(self) -> "BoundTransaction":
        transaction = self._state.transaction
        if transaction is None:
            raise BackendError("transaction_closed", "owned transaction is unavailable")
        return transaction

    def _adopt(self, transaction: "BoundTransaction") -> None:
        if not isinstance(transaction, BoundTransaction):
            raise BackendError(
                "invalid_transaction", "transaction owner received an invalid value"
            )
        if not self.closed:
            raise BackendError(
                "transaction_already_owned", "transaction owner is already active"
            )
        self._state.transaction = transaction

    def retain_on_exception(self) -> "_OwnedTransaction":
        self._retained_on_exception = True
        return self

    def retain_if_registered(
        self, predicate: Callable[[], bool]
    ) -> "_OwnedTransaction":
        if not callable(predicate):
            raise BackendError(
                "invalid_owner_registration",
                "transaction registration predicate is not callable",
            )
        self._retain_if_registered = predicate
        return self

    def transfer(self) -> "_OwnedTransaction":
        return self.retain_on_exception()

    def _should_retain_on_exception(
        self, primary_error: Optional[BaseException]
    ) -> bool:
        predicate = self._retain_if_registered
        if self._retained_on_exception:
            return True
        if predicate is None:
            return False
        try:
            registered = predicate()
            if type(registered) is not bool:
                raise BackendError(
                    "transaction_registration_probe_failed",
                    "transaction registration predicate returned a non-boolean",
                )
            return registered
        except BaseException as cleanup_failure:
            diagnostic = DarwinBackend._exception_diagnostic(cleanup_failure)
            try:
                cleanup_failure.__traceback__ = None
            except BaseException:
                pass
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    primary_error,
                    BackendError(
                        "transaction_registration_probe_failed",
                        diagnostic,
                    ),
                )
            return True

    def _handle_acquisition_failure(self, primary_error: BaseException) -> None:
        self.close(primary_error=primary_error)

    def _acquisition_primary(
        self, escaped: BaseException
    ) -> Tuple[BaseException, Optional[types.TracebackType]]:
        return escaped, escaped.__traceback__

    def _handle_exception(self, primary_error: Optional[BaseException]) -> None:
        if self._should_retain_on_exception(primary_error):
            self._retained_on_exception = True
            self._retain_if_registered = None
            return
        self._retain_if_registered = None
        self.close(primary_error=primary_error)

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        self._retain_if_registered = None
        first_failure: Optional[BaseException] = None
        for _attempt in range(2):
            try:
                self._state.close(primary_error=primary_error)
            except BaseException as cleanup_failure:
                if first_failure is None:
                    first_failure = cleanup_failure
                if primary_error is not None:
                    DarwinBackend._attach_cleanup_diagnostic(
                        primary_error, cleanup_failure
                    )
            if self.closed:
                break
        if not self.closed:
            incomplete = BackendError(
                "close_failed",
                "owned transaction remained active after two close attempts",
            )
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(primary_error, incomplete)
                return
            if first_failure is None:
                raise incomplete
        if first_failure is not None and primary_error is None:
            raise first_failure

    def __enter__(self) -> "_OwnedTransaction":
        if self._entered:
            raise BackendError(
                "transaction_owner_reentered", "transaction owner was re-entered"
            )
        self._entered = True
        try:
            self._acquire(self)
            if self.closed:
                raise BackendError(
                    "transaction_bind_failed",
                    "transaction acquisition returned nothing",
                )
            return self
        except BaseException as escaped:
            try:
                _handler_attempt = 1
                primary, primary_traceback = self._acquisition_primary(escaped)
                self._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                primary, primary_traceback = self._acquisition_primary(escaped)
                DarwinBackend._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _handler_attempt = 2
                    self._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    DarwinBackend._attach_cleanup_diagnostic(primary, retry_failure)
            if primary is not escaped or primary.__traceback__ is not primary_traceback:
                raise primary.with_traceback(primary_traceback)
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self._retain_if_registered = None
            return
        try:
            _handler_attempt = 1
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            self._handle_exception(primary_error)
        except BaseException as cleanup_failure:
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if primary_error is None:
                try:
                    _handler_attempt = 2
                    self._handle_exception(primary_error)
                finally:
                    raise
            DarwinBackend._attach_cleanup_diagnostic(primary_error, cleanup_failure)
            try:
                _handler_attempt = 2
                self._handle_exception(primary_error)
            except BaseException as retry_failure:
                DarwinBackend._attach_cleanup_diagnostic(primary_error, retry_failure)


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    mode: int
    nlink: int
    size: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int

    @property
    def object_key(self) -> Tuple[int, int]:
        return (self.dev, self.ino)

    def is_same_object(self, other: "FileIdentity") -> bool:
        return self.object_key == other.object_key


class _OwnedStageFD(_OwnedFD):
    """Lazy stage-directory owner whose identity is set during acquisition."""

    def __init__(
        self,
        backend: "DarwinBackend",
        subject: str,
        acquire: Callable[[_OwnedFD], None],
    ) -> None:
        super().__init__(backend, subject, acquire)
        self._stage_identity: Optional[FileIdentity] = None
        self._namespace_created = False
        self._namespace_cleanup_attempted = False
        self._namespace_cleanup_complete = False
        self._namespace_cleanup_attempts = 0
        self._namespace_cleanup_authorization_attempted = False
        self._namespace_cleanup_authorized = False
        self._namespace_cleanup: Optional[Callable[[], Optional[str]]] = None

    def _configure_namespace_cleanup(
        self, cleanup: Callable[[], Optional[str]]
    ) -> None:
        if not callable(cleanup) or self._namespace_cleanup is not None:
            raise BackendError(
                "invalid_stage_cleanup",
                "stage owner received an invalid namespace cleanup callback",
            )
        self._namespace_cleanup = cleanup

    def _mark_namespace_created(self) -> None:
        self._namespace_created = True

    def _mark_namespace_cleanup_attempted(self) -> None:
        self._namespace_cleanup_attempted = True

    def _mark_namespace_cleanup_complete(self) -> None:
        self._namespace_cleanup_attempted = True
        self._namespace_cleanup_complete = True

    def _cleanup_created_namespace(self, primary_error: BaseException) -> None:
        if (
            not self._namespace_created
            or self._namespace_cleanup_complete
            or self._namespace_cleanup_attempts >= 2
        ):
            return
        cleanup = self._namespace_cleanup
        if cleanup is None:
            self._namespace_cleanup_attempted = True
            self._backend._attach_cleanup_diagnostic(
                primary_error,
                BackendError(
                    "stage_create_cleanup_failed",
                    "created stage has no namespace cleanup callback",
                ),
            )
            return
        cleanup_error: Optional[str] = None
        cleanup_failure: Optional[BaseException] = None
        for _attempt in range(self._namespace_cleanup_attempts, 2):
            self._namespace_cleanup_attempts = _attempt + 1
            try:
                cleanup_error = cleanup()
                cleanup_failure = None
            except BaseException as exc:
                cleanup_failure = exc
                self._backend._attach_cleanup_diagnostic(primary_error, exc)
                if (
                    self._namespace_cleanup_attempts < 2
                    and not self._namespace_cleanup_complete
                ):
                    continue
            break
        if not self._namespace_cleanup_attempted:
            self._namespace_cleanup_attempted = True
            if cleanup_failure is None:
                self._backend._attach_cleanup_diagnostic(
                    primary_error,
                    BackendError(
                        "stage_create_cleanup_failed",
                        "stage cleanup callback returned before recording an attempt",
                    ),
                )
            return
        if cleanup_failure is not None:
            return
        if cleanup_error is None:
            self._namespace_cleanup_complete = True
            return
        self._namespace_cleanup_attempts = 2
        detail = self._backend._exact_cleanup_text(cleanup_error) or "<unprintable>"
        self._backend._attach_cleanup_diagnostic(
            primary_error,
            BackendError(
                "stage_create_cleanup_failed",
                f"stage interruption cleanup not proved ({detail})",
            ),
        )

    def _set_identity(self, identity: FileIdentity) -> None:
        if not isinstance(identity, FileIdentity):
            raise BackendError(
                "invalid_stage_identity", "stage owner received an invalid identity"
            )
        self._stage_identity = identity

    def identity(self) -> FileIdentity:
        if self._stage_identity is None:
            raise BackendError(
                "stage_identity_unavailable", "stage owner has not been acquired"
            )
        return self._stage_identity

    def _handle_acquisition_failure(self, primary_error: BaseException) -> None:
        self._cleanup_created_namespace(primary_error)
        super()._handle_acquisition_failure(primary_error)

    def _acquisition_primary(
        self, escaped: BaseException
    ) -> Tuple[BaseException, Optional[types.TracebackType]]:
        if escaped.__suppress_context__:
            return escaped, escaped.__traceback__
        context = escaped.__context__
        traceback = context.__traceback__ if context is not None else None
        while traceback is not None:
            frame = traceback.tb_frame
            if (
                frame.f_code is self._acquire.__code__
                and frame.f_locals.get("target") is self
            ):
                return _primary_before_handler_interruption(escaped)
            traceback = traceback.tb_next
        return escaped, escaped.__traceback__

    def _handle_exception(self, primary_error: Optional[BaseException]) -> None:
        if self._neutralize_exception_alias(primary_error):
            return
        if self._should_retain_on_exception(primary_error):
            self._retained_on_exception = True
            self._retain_if_registered = None
            self._neutralize_on_exception_if = None
            return
        self._retain_if_registered = None
        self._neutralize_on_exception_if = None
        if primary_error is not None:
            self._cleanup_created_namespace(primary_error)
        self._drain(primary_error=primary_error)


class _StageCleanupAuthorizer:
    """Authorize stage removal once across a bounded cleanup retry."""

    def __init__(
        self,
        target: _OwnedStageFD,
        authorize: Callable[[str], None],
    ) -> None:
        self._target = target
        self._authorize = authorize

    def __call__(self, action: str) -> None:
        if self._target._namespace_cleanup_authorized:
            return
        if self._target._namespace_cleanup_authorization_attempted:
            raise BackendError(
                "stage_cleanup_authorization_failed",
                "stage cleanup authorization did not complete",
            )
        # fmt: off
        self._target._namespace_cleanup_authorization_attempted = True; self._authorize(action); self._target._namespace_cleanup_authorized = True  # noqa: E702
        # fmt: on


@dataclass(frozen=True)
class FilePolicy:
    """Access policy plus the exact caller-visible xattr set (options=0)."""

    uid: int
    gid: int
    mode: int
    flags: int
    mtime_ns: int
    xattrs: Tuple[Tuple[bytes, bytes], ...]
    acl_native: bytes


@dataclass(frozen=True)
class FileSnapshot:
    identity: FileIdentity
    sha256: str
    policy: FilePolicy


@dataclass(frozen=True)
class SnapshotExpectation:
    """Durable, serialization-friendly protected-file evidence."""

    dev: int
    ino: int
    size: int
    mtime_ns: int
    nlink: int
    sha256: str
    uid: int
    gid: int
    mode: int
    flags: int
    acl_sha256: str
    xattrs_sha256: str


class DarwinBackend:
    """Darwin syscall adapter for fail-closed reflink and mirror operations."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._bind_libc()

    @staticmethod
    def _authorize_state(authorize_state: Callable[[str], None], action: str) -> None:
        if not callable(authorize_state):
            raise BackendError(
                "invalid_state_authorizer", "authorize_state must be callable"
            )
        authorize_state(action)

    @staticmethod
    def _exception_diagnostic(exc: BaseException) -> str:
        try:
            exception_name = DarwinBackend._exact_cleanup_text(type(exc).__name__)
        except BaseException:
            return "<unprintable-exception>"
        if not exception_name:
            return "<unprintable-exception>"
        try:
            detail = DarwinBackend._exact_cleanup_text(str(exc)) or "<unprintable>"
        except BaseException:
            detail = "<unprintable>"
        return DarwinBackend._bounded_cleanup_diagnostic(f"{exception_name}: {detail}")

    @staticmethod
    def _bounded_cleanup_diagnostic(detail: str) -> str:
        return DarwinBackend._truncate_cleanup_diagnostic(detail, 4 * 1024)

    @staticmethod
    def _exact_cleanup_text(value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        try:
            encoded = str.encode(value, "utf-8", errors="replace")
            return bytes.decode(encoded, "utf-8")
        except BaseException:
            return None

    @staticmethod
    def _truncate_cleanup_diagnostic(detail: str, limit: int) -> str:
        marker = b"...[truncated]"
        normalized = DarwinBackend._exact_cleanup_text(detail) or "<unprintable>"
        encoded = str.encode(normalized, "utf-8", errors="replace")
        if len(encoded) <= limit:
            return normalized
        prefix = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
        return f"{prefix}{marker.decode('ascii')}"

    @staticmethod
    def _cleanup_diagnostic_parts(primary: BaseException) -> Tuple[str, ...]:
        try:
            parts = getattr(primary, "cleanup_diagnostics", ())
            if type(parts) is tuple and parts:
                limited_parts = parts[-8:]
                return tuple(
                    DarwinBackend._bounded_cleanup_diagnostic(normalized)
                    for part in limited_parts
                    if (normalized := DarwinBackend._exact_cleanup_text(part))
                )
            legacy = getattr(primary, "cleanup_diagnostic", "")
            normalized = DarwinBackend._exact_cleanup_text(legacy)
            return (
                (DarwinBackend._bounded_cleanup_diagnostic(normalized),)
                if normalized
                else ()
            )
        except BaseException:
            return ()

    @staticmethod
    def _combine_cleanup_diagnostics(parts: Tuple[str, ...]) -> str:
        if len(parts) > 8:
            parts = parts[-8:]
        parts = tuple(
            DarwinBackend._bounded_cleanup_diagnostic(normalized)
            for part in parts
            if (normalized := DarwinBackend._exact_cleanup_text(part))
        )
        if not parts:
            return ""
        limit = 4 * 1024
        combined = "; ".join(parts)
        if len(str.encode(combined, "utf-8", errors="replace")) <= limit:
            return combined
        separator_bytes = 2 * (len(parts) - 1)
        available = limit - separator_bytes
        lengths = [len(str.encode(part, "utf-8", errors="replace")) for part in parts]
        budgets = [0] * len(parts)
        remaining = set(range(len(parts)))
        while remaining:
            share, extra = divmod(available - sum(budgets), len(remaining))
            fixed = [index for index in remaining if lengths[index] <= share]
            if not fixed:
                for offset, index in enumerate(sorted(remaining)):
                    budgets[index] = share + (1 if offset < extra else 0)
                break
            for index in fixed:
                budgets[index] = lengths[index]
                remaining.remove(index)
        return "; ".join(
            DarwinBackend._truncate_cleanup_diagnostic(part, budget)
            for part, budget in zip(parts, budgets)
        )

    @staticmethod
    def _attach_cleanup_diagnostic(
        primary: BaseException, cleanup: BaseException
    ) -> None:
        """Annotate, but never replace, the first exception in a failure path."""
        note = DarwinBackend._bounded_cleanup_diagnostic(
            "identity-bound cleanup failed ("
            f"{DarwinBackend._exception_diagnostic(cleanup)})"
        )
        try:
            existing = DarwinBackend._cleanup_diagnostic_parts(primary)
            parts = (*existing[-7:], note)
            setattr(primary, "cleanup_diagnostics", parts)
            setattr(
                primary,
                "cleanup_diagnostic",
                DarwinBackend._combine_cleanup_diagnostics(parts),
            )
            add_note = getattr(BaseException, "add_note", None)
            if callable(add_note):
                add_note(primary, note)
            else:
                existing_notes = getattr(primary, "__notes__", ())
                notes = (
                    list(existing_notes[-7:])
                    if type(existing_notes) in (tuple, list)
                    else []
                )
                notes.append(note)
                setattr(primary, "__notes__", notes)
        except BaseException:
            return

    def _drain_pending_fd(
        self,
        pending_fd: list[int],
        subject: str,
        *,
        primary_error: Optional[BaseException],
    ) -> None:
        """Drain a raw-FD receipt that has not transferred into an owner."""
        if (
            type(pending_fd) is not list
            or len(pending_fd) != 1
            or not isinstance(pending_fd[0], int)
            or isinstance(pending_fd[0], bool)
        ):
            raise BackendError(
                "invalid_fd_receipt", f"{subject} has an invalid pending fd receipt"
            )
        first_failure: Optional[BackendError] = None
        for _attempt in range(2):
            fd = pending_fd[0]
            if fd < 0:
                break
            try:
                _cleanup_attempt = _attempt + 1
                # fmt: off
                pending_fd[0] = -1; os.close(fd)  # noqa: E702
            # fmt: on
            except BaseException as exc:
                close_failure = BackendError(
                    "close_failed",
                    f"{subject} fd {fd}: {self._exception_diagnostic(exc)}",
                )
                if first_failure is None:
                    first_failure = close_failure
                if primary_error is not None:
                    self._attach_cleanup_diagnostic(primary_error, close_failure)
        if pending_fd[0] >= 0:
            incomplete = BackendError(
                "close_failed", f"{subject} fd remained open after two attempts"
            )
            if primary_error is not None:
                self._attach_cleanup_diagnostic(primary_error, incomplete)
                return
            if first_failure is None:
                raise incomplete
        if first_failure is not None and primary_error is None:
            raise first_failure

    def _adopt_fd(
        self,
        fd: int,
        subject: str,
        *,
        pending_fd: Optional[list[int]] = None,
    ) -> _OwnedFD:
        """Adopt an already-handed-off fd for legacy setters and test doubles."""
        receipt = [fd] if pending_fd is None else pending_fd
        if type(receipt) is not list or len(receipt) != 1 or receipt[0] != fd:
            raise BackendError(
                "invalid_fd_receipt", f"{subject} has an invalid pending fd receipt"
            )
        owner: Optional[_OwnedFD] = None
        try:
            _adoption_guard = 1
            owner = _OwnedFD(self, subject)
            # fmt: off
            owner._adopt(receipt[0]); receipt[0] = -1  # noqa: E702
            # fmt: on
            return owner
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                if owner is not None and not owner.closed:
                    owner._drain(primary_error=primary)
                else:
                    self._drain_pending_fd(receipt, subject, primary_error=primary)
            except BaseException as cleanup_failure:
                self._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _cleanup_dispatch = 2
                    if owner is not None and not owner.closed:
                        owner._drain(primary_error=primary)
                    else:
                        self._drain_pending_fd(receipt, subject, primary_error=primary)
                except BaseException as retry_failure:
                    self._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _handoff_fd_owner(self, owner: _OwnedFD) -> int:
        """Acquire and disarm a legacy raw-FD result without an exit gap."""
        try:
            _handoff_guard = 1
            # fmt: off
            owner.__enter__(); return owner.disarm()  # noqa: E702
        # fmt: on
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                owner._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                self._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _cleanup_dispatch = 2
                    owner._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    self._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _handoff_acl_owner(self, owner: _OwnedACL) -> Optional[ctypes.c_void_p]:
        """Acquire and disarm a legacy raw-ACL result without an exit gap."""
        try:
            _handoff_guard = 1
            # fmt: off
            owner.__enter__(); return owner.disarm()  # noqa: E702
        # fmt: on
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                owner._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                self._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _cleanup_dispatch = 2
                    owner._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    self._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _handoff_stage_owner(self, owner: _OwnedStageFD) -> Tuple[int, FileIdentity]:
        """Acquire and disarm a raw stage tuple without losing cleanup evidence."""
        try:
            _handoff_guard = 1
            # fmt: off
            owner.__enter__(); identity = owner.identity(); return (owner.disarm(), identity)  # noqa: E702
        # fmt: on
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                owner._handle_acquisition_failure(primary)
            except BaseException as cleanup_failure:
                self._attach_cleanup_diagnostic(primary, cleanup_failure)
                try:
                    _cleanup_dispatch = 2
                    owner._handle_acquisition_failure(primary)
                except BaseException as retry_failure:
                    self._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _native_fd_owner(
        self,
        subject: str,
        opener: Callable[[], int],
        *,
        reason: str,
        operation: str,
    ) -> _OwnedFD:
        def acquire(target: _OwnedFD) -> None:
            handed_off_fd = -1
            try:
                handed_off_fd = opener()
                target._adopt(handed_off_fd)
            except BaseException as primary:
                if handed_off_fd >= 0 and target.closed:
                    try:
                        target._adopt(handed_off_fd)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                if isinstance(primary, OSError):
                    raise self._os_error(reason, operation, primary) from primary
                raise

        return _OwnedFD(self, subject, acquire)

    def _returned_fd_owner(
        self, subject: str, acquire_raw: Callable[[], int]
    ) -> _OwnedFD:
        """Guard a legacy overridable method that still returns a raw fd."""

        def acquire(target: _OwnedFD) -> None:
            handed_off_fd = -1
            try:
                handed_off_fd = acquire_raw()
                target._adopt(handed_off_fd)
            except BaseException as primary:
                if handed_off_fd >= 0 and target.closed:
                    try:
                        target._adopt(handed_off_fd)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedFD(self, subject, acquire)

    def _dispatch_open_leaf_owned(
        self, parent_fd: int, name: str, *, writable: bool = False
    ) -> _OwnedFD:
        bound = self.open_leaf
        if getattr(bound, "__func__", None) is DarwinBackend.open_leaf:
            return self._open_leaf_owned(parent_fd, name, writable=writable)
        return self._returned_fd_owner(
            f"overridden leaf {name!r}",
            lambda: bound(parent_fd, name, writable=writable),
        )

    def _open_absolute_dir_owned(self, path: str) -> _OwnedFD:
        components = self._absolute_components(path)
        flags = os.O_RDONLY | os.O_NONBLOCK | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW

        def acquire(target: _OwnedFD) -> None:
            owners = []
            try:
                current = self._native_fd_owner(
                    "absolute root",
                    lambda: os.open("/", flags),
                    reason="open_root_failed",
                    operation="open /",
                )
                owners.append(current)
                with current:
                    self._require_directory(current.fileno(), "root")
                for component in components:
                    next_owner = self._native_fd_owner(
                        f"directory component {component!r}",
                        lambda component=component, current=current: os.open(
                            component, flags, dir_fd=current.fileno()
                        ),
                        reason="open_directory_failed",
                        operation=f"open directory component {component!r}",
                    )
                    owners.append(next_owner)
                    with next_owner:
                        self._require_directory(
                            next_owner.fileno(),
                            f"directory component {component!r}",
                        )
                        current.close()
                        current = next_owner
                target._share_from(current)
            except BaseException as primary:
                try:
                    _cleanup_dispatch = 1
                    self._close_fd_owners(
                        tuple((owner._subject, owner) for owner in reversed(owners)),
                        primary_error=primary,
                    )
                except BaseException as cleanup_failure:
                    self._attach_cleanup_diagnostic(primary, cleanup_failure)
                    try:
                        _cleanup_dispatch = 2
                        self._close_fd_owners(
                            tuple(
                                (owner._subject, owner) for owner in reversed(owners)
                            ),
                            primary_error=primary,
                        )
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(primary, retry_failure)
                raise

        return _OwnedFD(self, f"absolute directory {path!r}", acquire)

    def open_absolute_dir(self, path: str) -> int:
        owner = self._open_absolute_dir_owned(path)
        return self._handoff_fd_owner(owner)

    def _open_absolute_parent_core_owned(self, path: str) -> Tuple[_OwnedFD, str]:
        if not isinstance(path, str) or not path.startswith("/"):
            raise BackendError("invalid_path", "path must be an absolute string")
        if "\x00" in path:
            raise BackendError("invalid_path", "path contains NUL")
        parent_path, separator, leaf = path.rpartition("/")
        if not separator or not leaf:
            raise BackendError("invalid_path", "path must end in a leaf component")
        self._leaf_bytes(leaf)
        if not parent_path:
            parent_path = "/"
        return (self._open_absolute_dir_owned(parent_path), leaf)

    def _open_absolute_parent_owned(self, path: str) -> Tuple[_OwnedFD, str]:
        core_owner, expected_leaf = self._open_absolute_parent_core_owned(path)
        bound = self.open_absolute_parent
        if getattr(bound, "__func__", None) is DarwinBackend.open_absolute_parent:
            return (core_owner, expected_leaf)

        handler_primary = [None]

        def acquire(target: _OwnedFD) -> None:
            unacquired = object()
            handed_off: object = unacquired

            def salvage_handed_off_fd() -> None:
                if target.closed and type(handed_off) is tuple and len(handed_off) > 0:
                    handed_off_fd = handed_off[0]
                    if (
                        isinstance(handed_off_fd, int)
                        and not isinstance(handed_off_fd, bool)
                        and handed_off_fd >= 0
                    ):
                        target._adopt(handed_off_fd)

            try:
                handed_off = bound(path)
                if type(handed_off) is tuple and len(handed_off) > 0:
                    candidate_fd = handed_off[0]
                    if (
                        isinstance(candidate_fd, int)
                        and not isinstance(candidate_fd, bool)
                        and candidate_fd >= 0
                    ):
                        target._adopt(candidate_fd)
                if type(handed_off) is not tuple or len(handed_off) != 2:
                    raise BackendError(
                        "invalid_path",
                        "overridden absolute-parent opener returned an invalid value",
                    )
                handed_off_fd, opened_leaf = handed_off
                if target.closed:
                    target._adopt(handed_off_fd)
                if opened_leaf != expected_leaf:
                    raise BackendError(
                        "invalid_path",
                        "overridden absolute-parent opener returned a different leaf",
                    )
            except BaseException as primary:
                try:
                    # fmt: off
                    handler_primary[0] = (primary, primary.__traceback__); _handler_guard = 1  # noqa: E702
                    # fmt: on
                    primary_traceback = primary.__traceback__
                    _handler_attempt = 1
                    salvage_handed_off_fd()
                except BaseException as cleanup:
                    primary_traceback = primary.__traceback__
                    self._attach_cleanup_diagnostic(primary, cleanup)
                    try:
                        _handler_attempt = 2
                        salvage_handed_off_fd()
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(primary, retry_failure)
                    raise primary.with_traceback(primary_traceback)
                raise

        del core_owner
        return (
            _HandlerRecoveringOwnedFD(
                self,
                f"overridden absolute parent for {path!r}",
                acquire,
                lambda: handler_primary[0],
            ),
            expected_leaf,
        )

    def open_absolute_parent(self, path: str) -> Tuple[int, str]:
        owner, leaf = self._open_absolute_parent_core_owned(path)
        return (self._handoff_fd_owner(owner), leaf)

    def _open_leaf_owned(
        self, parent_fd: int, name: str, *, writable: bool = False
    ) -> _OwnedFD:
        self._leaf_bytes(name)
        self._require_directory(parent_fd, "leaf parent")
        flags = (
            (os.O_RDWR if writable else os.O_RDONLY)
            | os.O_NONBLOCK
            | _O_CLOEXEC
            | _O_NOFOLLOW
        )

        def acquire(target: _OwnedFD) -> None:
            opened = self._native_fd_owner(
                f"leaf {name!r}",
                lambda: os.open(name, flags, dir_fd=parent_fd),
                reason="open_leaf_failed",
                operation=f"open leaf {name!r}",
            )
            with opened:
                self._require_regular(opened.fileno(), f"leaf {name!r}")
                try:
                    current_flags = fcntl.fcntl(opened.fileno(), fcntl.F_GETFL)
                    fcntl.fcntl(
                        opened.fileno(),
                        fcntl.F_SETFL,
                        current_flags & ~os.O_NONBLOCK,
                    )
                except OSError as exc:
                    raise self._os_error(
                        "open_leaf_failed",
                        f"clear O_NONBLOCK for leaf {name!r}",
                        exc,
                    )
                target._share_from(opened)

        return _OwnedFD(self, f"leaf {name!r}", acquire)

    def open_leaf(self, parent_fd: int, name: str, *, writable: bool = False) -> int:
        owner = self._open_leaf_owned(parent_fd, name, writable=writable)
        return self._handoff_fd_owner(owner)

    def identity(self, fd: int) -> FileIdentity:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise self._os_error("fstat_failed", f"fstat fd {fd}", exc)
        return self._identity_from_stat(info)

    def identity_at(self, parent_fd: int, name: str) -> FileIdentity:
        owner = self._dispatch_open_leaf_owned(parent_fd, name)
        with owner:
            result = self.identity(owner.fileno())
            owner.close()
            return result

    def require_identity_at(
        self, parent_fd: int, name: str, expected: FileIdentity
    ) -> FileIdentity:
        actual = self.identity_at(parent_fd, name)
        if not actual.is_same_object(expected):
            raise BackendError(
                "identity_mismatch",
                f"{name!r} maps to {actual.object_key}, expected {expected.object_key}",
            )
        return actual

    def require_directory_identity_at(
        self, parent_fd: int, name: str, expected: FileIdentity
    ) -> FileIdentity:
        self._leaf_bytes(name)
        self._require_directory(parent_fd, "directory parent")
        flags = os.O_RDONLY | os.O_NONBLOCK | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW
        owner = self._native_fd_owner(
            f"directory identity leaf {name!r}",
            lambda: os.open(name, flags, dir_fd=parent_fd),
            reason="open_directory_failed",
            operation=f"open directory leaf {name!r}",
        )
        with owner:
            actual = self.identity(owner.fileno())
            owner.close()
        if not actual.is_same_object(expected):
            raise BackendError(
                "identity_mismatch",
                f"directory {name!r} maps to {actual.object_key}, expected {expected.object_key}",
            )
        return actual

    def sha256_fd(self, fd: int) -> str:
        return self._sha256_fd_with_link_count(
            fd, expected_nlink=1, subject="hash input"
        )

    def _sha256_fd_with_link_count(
        self, fd: int, *, expected_nlink: int, subject: str
    ) -> str:
        before = self.identity(fd)
        self._require_regular_identity_with_link_count(
            before, subject, expected_nlink=expected_nlink
        )
        digest = hashlib.sha256()
        offset = 0
        while offset < before.size:
            wanted = min(_CONTENT_CHUNK_SIZE, before.size - offset)
            try:
                block = os.pread(fd, wanted, offset)
            except OSError as exc:
                raise self._os_error("content_unreadable", f"pread fd {fd}", exc)
            if not block:
                raise BackendError(
                    "content_changed", f"fd {fd} reached EOF before its recorded size"
                )
            digest.update(block)
            offset += len(block)
        try:
            trailing = os.pread(fd, 1, before.size)
        except OSError as exc:
            raise self._os_error("content_unreadable", f"final pread fd {fd}", exc)
        after = self.identity(fd)
        self._require_content_stable_with_link_count(
            before, after, f"fd {fd}", expected_nlink=expected_nlink
        )
        if trailing:
            raise BackendError("content_changed", f"fd {fd} grew while hashing")
        return digest.hexdigest()

    def files_equal(self, left_fd: int, right_fd: int) -> bool:
        left_before = self.identity(left_fd)
        right_before = self.identity(right_fd)
        self._require_regular_identity(left_before, "left comparison input")
        self._require_regular_identity(right_before, "right comparison input")
        if left_before.size != right_before.size:
            self._require_content_stable(
                left_before, self.identity(left_fd), "left input"
            )
            self._require_content_stable(
                right_before, self.identity(right_fd), "right input"
            )
            return False
        offset = 0
        mismatch = False
        while offset < left_before.size:
            wanted = min(_CONTENT_CHUNK_SIZE, left_before.size - offset)
            try:
                left = os.pread(left_fd, wanted, offset)
                right = os.pread(right_fd, wanted, offset)
            except OSError as exc:
                raise self._os_error(
                    "content_unreadable", "pread during comparison", exc
                )
            if len(left) != wanted or len(right) != wanted:
                raise BackendError(
                    "content_changed", "comparison input reached an unexpected EOF"
                )
            if left != right:
                mismatch = True
                break
            offset += wanted
        self._require_content_stable(left_before, self.identity(left_fd), "left input")
        self._require_content_stable(
            right_before, self.identity(right_fd), "right input"
        )
        return not mismatch

    def snapshot_policy(self, fd: int) -> FilePolicy:
        for _attempt in range(_POLICY_SNAPSHOT_ATTEMPTS):
            before_stat = self._fstat(fd, "policy snapshot")
            xattrs = self._snapshot_xattrs(fd)
            acl_native = self._snapshot_acl(fd)
            after_stat = self._fstat(fd, "policy snapshot revalidation")
            before = self._policy_from_stat(before_stat, xattrs, acl_native)
            after = self._policy_from_stat(after_stat, xattrs, acl_native)
            if (
                self._identity_from_stat(before_stat).is_same_object(
                    self._identity_from_stat(after_stat)
                )
                and before_stat.st_ctime_ns == after_stat.st_ctime_ns
                and before == after
            ):
                return before
        raise BackendError("policy_unstable", f"policy changed while reading fd {fd}")

    def require_exclusive_writer_policy(
        self, policy: FilePolicy, subject: str
    ) -> FilePolicy:
        """Require that only the current euid can write a protected file."""
        if not isinstance(policy, FilePolicy):
            raise BackendError(
                "invalid_policy_evidence", f"{subject} policy has the wrong type"
            )
        if policy.uid != os.geteuid():
            raise BackendError(
                "exclusive_writer_required",
                f"{subject} is not owned by the effective uid",
            )
        if policy.mode & 0o022:
            raise BackendError(
                "exclusive_writer_required",
                f"{subject} grants group or other write permission",
            )
        if policy.acl_native:
            raise BackendError(
                "exclusive_writer_required",
                f"{subject} has an extended ACL",
            )
        if policy.flags & ~_SAFE_SETTABLE_FLAGS:
            raise BackendError(
                "exclusive_writer_required",
                f"{subject} has blocking, internal, or unknown BSD flags",
            )
        return policy

    def require_exclusive_writer(self, fd: int, subject: str) -> FilePolicy:
        """Read and require the exclusive-writer policy on one held regular FD."""
        self._require_regular(fd, subject)
        return self.require_exclusive_writer_policy(self.snapshot_policy(fd), subject)

    def snapshot_file(self, fd: int) -> FileSnapshot:
        before = self.identity(fd)
        digest = self.sha256_fd(fd)
        policy = self.snapshot_policy(fd)
        after = self.identity(fd)
        self._require_content_stable(before, after, f"fd {fd}")
        if before.ctime_ns != after.ctime_ns:
            raise BackendError(
                "policy_unstable",
                f"fd {fd} metadata generation changed while snapshotting",
            )
        if (
            policy.uid != after.uid
            or policy.gid != after.gid
            or policy.mode != stat.S_IMODE(after.mode)
            or policy.mtime_ns != after.mtime_ns
        ):
            raise BackendError(
                "policy_unstable", f"policy changed while snapshotting fd {fd}"
            )
        return FileSnapshot(after, digest, policy)

    def snapshot_unlinked_file(self, fd: int) -> FileSnapshot:
        """Snapshot one held regular file after this backend unlinked its sole name."""
        before = self.identity(fd)
        digest = self._sha256_fd_with_link_count(
            fd, expected_nlink=0, subject="unlinked hash input"
        )
        policy = self.snapshot_policy(fd)
        after = self.identity(fd)
        self._require_content_stable_with_link_count(
            before, after, f"unlinked fd {fd}", expected_nlink=0
        )
        if before.ctime_ns != after.ctime_ns:
            raise BackendError(
                "policy_unstable",
                f"unlinked fd {fd} metadata generation changed while snapshotting",
            )
        if (
            policy.uid != after.uid
            or policy.gid != after.gid
            or policy.mode != stat.S_IMODE(after.mode)
            or policy.mtime_ns != after.mtime_ns
        ):
            raise BackendError(
                "policy_unstable", f"policy changed while snapshotting unlinked fd {fd}"
            )
        return FileSnapshot(after, digest, policy)

    @classmethod
    def snapshot_expectation(cls, snapshot: FileSnapshot) -> SnapshotExpectation:
        """Convert a live snapshot into evidence suitable for durable state."""
        return SnapshotExpectation(
            dev=snapshot.identity.dev,
            ino=snapshot.identity.ino,
            size=snapshot.identity.size,
            mtime_ns=snapshot.policy.mtime_ns,
            nlink=snapshot.identity.nlink,
            sha256=snapshot.sha256,
            uid=snapshot.policy.uid,
            gid=snapshot.policy.gid,
            mode=snapshot.policy.mode,
            flags=snapshot.policy.flags,
            acl_sha256=hashlib.sha256(snapshot.policy.acl_native).hexdigest(),
            xattrs_sha256=cls._xattrs_sha256(snapshot.policy.xattrs),
        )

    def require_snapshot(
        self,
        fd: int,
        expected: SnapshotExpectation,
        subject: str,
        *,
        mismatch_reason: str = "snapshot_mismatch",
        changed_reason: Optional[str] = None,
        unreadable_reason: Optional[str] = None,
    ) -> FileSnapshot:
        """Require one held regular FD to match complete durable evidence."""
        if not isinstance(expected, SnapshotExpectation):
            raise BackendError(
                "invalid_snapshot_expectation",
                f"{subject} expectation has the wrong type",
            )
        self._validate_sha256(expected.sha256, f"{subject} content SHA-256")
        self._validate_sha256(expected.acl_sha256, f"{subject} ACL SHA-256")
        self._validate_sha256(expected.xattrs_sha256, f"{subject} xattr SHA-256")
        try:
            actual = self.snapshot_file(fd)
        except BackendError as exc:
            reason = (
                changed_reason
                if exc.reason in _SNAPSHOT_MUTATION_REASONS
                else unreadable_reason
            )
            if reason is None:
                raise
            raise BackendError(
                reason,
                f"{subject} snapshot failed ({exc.reason}): {exc.detail}",
                exc.errno_value,
            ) from exc
        if self.snapshot_expectation(actual) != expected:
            raise BackendError(
                mismatch_reason,
                f"{subject} protected snapshot differs from durable evidence",
            )
        return actual

    def _strict_clone_core_owned(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
        writable: bool = False,
    ) -> _OwnedFD:
        """Return a lazy owner for a clone created at one absent child name."""
        if not isinstance(writable, bool):
            raise BackendError("invalid_clone_mode", "writable must be a boolean")
        name_bytes = self._leaf_bytes(name)

        def acquire(target: _OwnedFD) -> None:
            source_identity = self.identity(source_fd)
            self._require_regular_identity(source_identity, "clone source")
            self._require_directory(parent_fd, "clone destination parent")
            self._require_name_absent(parent_fd, name)
            ctypes.set_errno(0)
            self._authorize_state(authorize_state, "create_clone")
            source_identity = self.identity(source_fd)
            self._require_regular_identity(source_identity, "clone source")
            self._require_directory(parent_fd, "clone destination parent")
            self._require_name_absent(parent_fd, name)
            result = self._fclonefileat(source_fd, parent_fd, name_bytes, CLONE_ACL)
            if result != 0:
                self._raise_errno("clone_failed", f"fclonefileat to {name!r}")
            clone_owner = self._dispatch_open_leaf_owned(
                parent_fd, name, writable=writable
            )
            opened = False
            try:
                with clone_owner:
                    opened = True
                    clone_identity = self.identity(clone_owner.fileno())
                    if clone_identity.dev != source_identity.dev:
                        raise BackendError(
                            "clone_identity_invalid",
                            "clone and source are on different devices",
                        )
                    if clone_identity.is_same_object(source_identity):
                        raise BackendError(
                            "clone_identity_invalid",
                            "clone unexpectedly aliases the source inode",
                        )
                    if clone_identity.size != source_identity.size:
                        raise BackendError(
                            "clone_content_invalid",
                            "clone size does not match its source",
                        )
                    self.require_identity_at(parent_fd, name, clone_identity)
                    target._share_from(clone_owner)
            except BackendError as exc:
                if not opened:
                    raise BackendError(
                        "clone_created_unverified",
                        "clone syscall succeeded but the new name could not be opened: "
                        f"{exc.detail}",
                        exc.errno_value,
                    ) from exc
                raise

        return _OwnedFD(self, f"clone {name!r}", acquire)

    def _strict_clone_owned(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
        writable: bool = False,
    ) -> _OwnedFD:
        bound = self.strict_clone
        if getattr(bound, "__func__", None) is DarwinBackend.strict_clone:
            return self._strict_clone_core_owned(
                source_fd,
                parent_fd,
                name,
                authorize_state=authorize_state,
                writable=writable,
            )
        return self._returned_fd_owner(
            f"overridden clone {name!r}",
            lambda: bound(
                source_fd,
                parent_fd,
                name,
                authorize_state=authorize_state,
                writable=writable,
            ),
        )

    def strict_clone(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
        writable: bool = False,
    ) -> int:
        """Clone to an absent single-component name and return its held fd."""
        owner = self._strict_clone_core_owned(
            source_fd,
            parent_fd,
            name,
            authorize_state=authorize_state,
            writable=writable,
        )
        return self._handoff_fd_owner(owner)

    def _ordinary_copy_to_absent_core_owned(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> _OwnedFD:
        """Return a lazy owner for a newly-created ordinary copy.

        This is deliberately not an automatic fallback.  The caller decides
        whether a preceding clone failure is compatibility-only, proves that
        the failed clone left no child, and snapshots the source before and
        after this operation.  A failed ``fcopyfile`` can leave an
        identity-bound partial child for the caller to clean from its private
        stage directory.
        """
        self._leaf_bytes(name)

        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NONBLOCK
            | _O_CLOEXEC
            | _O_NOFOLLOW
        )

        def acquire(target: _OwnedFD) -> None:
            source_identity = self.identity(source_fd)
            self._require_regular_identity(source_identity, "copy source")
            self.require_exclusive_writer(source_fd, "copy source")
            self.validate_private_stage_parent(parent_fd)
            self._require_name_absent(parent_fd, name)
            self._authorize_state(authorize_state, "create_copy")
            source_identity = self.identity(source_fd)
            self._require_regular_identity(source_identity, "copy source")
            self.validate_private_stage_parent(parent_fd)
            self._require_name_absent(parent_fd, name)
            copy_owner = self._native_fd_owner(
                f"copy {name!r}",
                lambda: os.open(name, flags, 0o600, dir_fd=parent_fd),
                reason="copy_create_failed",
                operation=f"create copy {name!r}",
            )
            with copy_owner:
                copy_fd = copy_owner.fileno()
                created = self.identity(copy_fd)
                self._require_regular_identity(created, "copy destination")
                if created.uid != os.geteuid():
                    raise BackendError(
                        "copy_created_unverified",
                        "new copy is not owned by the effective uid",
                    )
                self.require_identity_at(parent_fd, name, created)
                try:
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    os.lseek(copy_fd, 0, os.SEEK_SET)
                except OSError as exc:
                    raise self._os_error(
                        "copy_failed", "rewind copy file descriptors", exc
                    )
                ctypes.set_errno(0)
                if self._fcopyfile(source_fd, copy_fd, None, COPYFILE_ALL) != 0:
                    self._raise_errno("copy_failed", f"fcopyfile to {name!r}")
                copied = self.identity(copy_fd)
                self._require_regular_identity(copied, "copy destination")
                if not copied.is_same_object(created):
                    raise BackendError(
                        "copy_created_unverified",
                        "copy destination changed identity during fcopyfile",
                    )
                self.require_identity_at(parent_fd, name, copied)
                target._share_from(copy_owner)

        return _OwnedFD(self, f"copy {name!r}", acquire)

    def _ordinary_copy_to_absent_owned(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> _OwnedFD:
        bound = self.ordinary_copy_to_absent
        if getattr(bound, "__func__", None) is DarwinBackend.ordinary_copy_to_absent:
            return self._ordinary_copy_to_absent_core_owned(
                source_fd,
                parent_fd,
                name,
                authorize_state=authorize_state,
            )
        return self._returned_fd_owner(
            f"overridden copy {name!r}",
            lambda: bound(
                source_fd,
                parent_fd,
                name,
                authorize_state=authorize_state,
            ),
        )

    def ordinary_copy_to_absent(
        self,
        source_fd: int,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> int:
        """Copy all source data and metadata into one newly-created held child."""
        owner = self._ordinary_copy_to_absent_core_owned(
            source_fd,
            parent_fd,
            name,
            authorize_state=authorize_state,
        )
        return self._handoff_fd_owner(owner)

    def truncate_preserving_policy(
        self,
        fd: int,
        size: int,
        expected: FilePolicy,
    ) -> FileSnapshot:
        """Truncate a private candidate and restore its captured source policy.

        The deterministic partial-file timestamp is the candidate's mtime
        captured immediately before truncation.  Atime is intentionally not
        preserved.  Truncation is not allowed to change uid, gid, xattrs, ACL,
        or any non-settable BSD flag; mode, mtime, and safe flags are restored
        when the syscall changes them.
        """
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackendError(
                "invalid_truncate_size", "truncate size must be nonnegative"
            )
        self._require_regular(fd, "truncate candidate")
        self.require_exclusive_writer_policy(expected, "truncate candidate policy")
        before = self.snapshot_policy(fd)
        if before != expected:
            raise BackendError(
                "candidate_policy_changed",
                "candidate policy changed before truncation",
            )
        before_identity = self.identity(fd)
        if size > before_identity.size:
            raise BackendError(
                "invalid_truncate_size",
                "truncate size exceeds the candidate size",
            )
        try:
            os.ftruncate(fd, size)
        except OSError as exc:
            raise self._os_error("truncate_failed", f"ftruncate fd {fd}", exc)

        current = self.snapshot_policy(fd)
        if (
            current.uid != expected.uid
            or current.gid != expected.gid
            or current.xattrs != expected.xattrs
            or current.acl_native != expected.acl_native
        ):
            raise BackendError(
                "candidate_policy_changed",
                "truncate changed a policy field that cannot be safely reconstructed",
            )
        self._preflight_policy_flags(expected.flags, current.flags)
        if current.mode != expected.mode:
            try:
                os.fchmod(fd, expected.mode)
            except OSError as exc:
                raise self._os_error(
                    "policy_apply_failed", "restore candidate mode", exc
                )
        if current.mtime_ns != expected.mtime_ns:
            current_stat = self._fstat(fd, "candidate timestamp restoration")
            try:
                os.utime(fd, ns=(current_stat.st_atime_ns, expected.mtime_ns))
            except OSError as exc:
                raise self._os_error(
                    "policy_apply_failed", "restore candidate mtime", exc
                )
        current_flags = int(
            getattr(self._fstat(fd, "candidate flags restoration"), "st_flags", 0)
        )
        if current_flags != expected.flags:
            self._set_flags(fd, current_flags, expected.flags)

        after = self.snapshot_file(fd)
        if after.identity.size != size:
            raise BackendError(
                "truncate_postcondition_unverified",
                f"candidate size is {after.identity.size}, expected {size}",
            )
        if after.policy != expected:
            raise BackendError(
                "clone_policy_mismatch",
                "truncated candidate policy does not match its pre-truncate policy",
            )
        self.require_exclusive_writer_policy(
            after.policy, "truncated candidate after policy restoration"
        )
        return after

    def publish_staged_name(
        self,
        stage_parent_fd: int,
        stage_name: str,
        stage_expected: FileIdentity,
        destination_parent_fd: int,
        destination_name: str,
        destination_expected: Optional[FileIdentity],
        *,
        authorize_namespace: Callable[[str], None],
        validate_after_authorization: Callable[[], None],
    ) -> FileIdentity:
        """Atomically publish one held staged file with identity postconditions."""
        stage_bytes = self._leaf_bytes(stage_name)
        destination_bytes = self._leaf_bytes(destination_name)
        if not isinstance(stage_expected, FileIdentity):
            raise BackendError(
                "invalid_publish_evidence", "stage identity has the wrong type"
            )
        if destination_expected is not None and not isinstance(
            destination_expected, FileIdentity
        ):
            raise BackendError(
                "invalid_publish_evidence", "destination identity has the wrong type"
            )
        if not callable(validate_after_authorization):
            raise BackendError(
                "invalid_post_authorization_validator",
                "validate_after_authorization must be callable",
            )
        self.validate_private_stage_parent(stage_parent_fd)
        self.validate_stage_container(destination_parent_fd)
        self.require_identity_at(stage_parent_fd, stage_name, stage_expected)
        if destination_expected is None:
            self._require_name_absent(destination_parent_fd, destination_name)
            flags = RENAME_EXCL | RENAME_NOFOLLOW_ANY
        else:
            self.require_identity_at(
                destination_parent_fd, destination_name, destination_expected
            )
            flags = RENAME_NOFOLLOW_ANY

        self._authorize_state(authorize_namespace, "publish")
        validate_after_authorization()
        self.validate_private_stage_parent(stage_parent_fd)
        self.validate_stage_container(destination_parent_fd)
        self.require_identity_at(stage_parent_fd, stage_name, stage_expected)
        if destination_expected is None:
            self._require_name_absent(destination_parent_fd, destination_name)
        else:
            self.require_identity_at(
                destination_parent_fd, destination_name, destination_expected
            )

        ctypes.set_errno(0)
        result = self._renameatx_np(
            stage_parent_fd,
            stage_bytes,
            destination_parent_fd,
            destination_bytes,
            flags,
        )
        if result != 0:
            value = ctypes.get_errno()
            raise BackendError(
                "publish_ambiguous",
                f"renameatx_np publish returned [Errno {value}] {os.strerror(value)}",
                value,
            )
        try:
            published = self.require_identity_at(
                destination_parent_fd, destination_name, stage_expected
            )
            self._require_name_absent(stage_parent_fd, stage_name)
            self.fsync(destination_parent_fd)
            if not self.identity(stage_parent_fd).is_same_object(
                self.identity(destination_parent_fd)
            ):
                self.fsync(stage_parent_fd)
            published_owner = self._dispatch_open_leaf_owned(
                destination_parent_fd, destination_name
            )
            published_primary: Optional[BaseException] = None
            published_durable = False
            try:
                with published_owner:
                    published_fd = published_owner.fileno()
                    if not self.identity(published_fd).is_same_object(stage_expected):
                        raise BackendError(
                            "publish_postcondition_unverified",
                            "reopened published file has the wrong identity",
                        )
                    self.full_fsync(published_fd)
                    published_durable = True
            except BaseException as exc:
                published_primary = exc
                raise
            finally:
                try:
                    _cleanup_dispatch = 1
                    self._close_fd_owners(
                        (("published destination", published_owner),),
                        primary_error=published_primary,
                        durable_namespace_complete=published_durable,
                    )
                except BaseException as cleanup_failure:
                    if published_primary is not None:
                        self._attach_cleanup_diagnostic(
                            published_primary, cleanup_failure
                        )
                        try:
                            _cleanup_dispatch = 2
                            self._close_fd_owners(
                                (("published destination", published_owner),),
                                primary_error=published_primary,
                                durable_namespace_complete=published_durable,
                            )
                        except BaseException as retry_failure:
                            self._attach_cleanup_diagnostic(
                                published_primary, retry_failure
                            )
                    else:
                        try:
                            _cleanup_dispatch = 2
                            self._close_fd_owners(
                                (("published destination", published_owner),),
                                primary_error=published_primary,
                                durable_namespace_complete=published_durable,
                            )
                        finally:
                            raise
            self.require_identity_at(
                destination_parent_fd, destination_name, stage_expected
            )
            self._require_name_absent(stage_parent_fd, stage_name)
            return published
        except BackendError as exc:
            raise BackendError(
                "publish_postcondition_unverified",
                f"publish syscall succeeded but its durable mapping is unverified: {exc.detail}",
                exc.errno_value,
            ) from exc

    def calibrate_clone_policy(
        self, original_fd: int, clone_fd: int, expected: FilePolicy
    ) -> FilePolicy:
        """Copy live original policy to clone and return the verified policy."""
        self.require_exclusive_writer_policy(expected, "expected original mirror")
        original_before = self.snapshot_policy(original_fd)
        if original_before != expected:
            raise BackendError(
                "original_policy_changed", "original policy no longer matches preflight"
            )
        clone_before = self.snapshot_policy(clone_fd)
        self._preflight_policy_flags(original_before.flags, clone_before.flags)

        live_acl_owner: Optional[_OwnedACL] = None
        apply_acl_owner: Optional[_OwnedACL] = None
        live_acl: Optional[ctypes.c_void_p] = None
        apply_acl: Optional[ctypes.c_void_p] = None
        acl_primary_error: Optional[BaseException] = None
        try:
            live_acl_owner = self._get_acl_owned(original_fd)
            with live_acl_owner:
                live_acl = live_acl_owner.pointer()
            apply_acl_owner = live_acl_owner
            if live_acl is None:
                apply_acl_owner = self._empty_acl_owned()
                with apply_acl_owner:
                    apply_acl = apply_acl_owner.pointer()
            else:
                apply_acl = live_acl
            live_acl_bytes = b"" if live_acl is None else self._acl_external(live_acl)
            if live_acl_bytes != expected.acl_native:
                raise BackendError(
                    "original_policy_changed", "original ACL changed before calibration"
                )
            clone_stat = self._fstat(clone_fd, "clone policy calibration")
            if clone_stat.st_uid != expected.uid or clone_stat.st_gid != expected.gid:
                try:
                    os.fchown(clone_fd, expected.uid, expected.gid)
                except OSError as exc:
                    raise self._os_error("policy_apply_failed", "fchown clone", exc)
            if (
                stat.S_IMODE(self._fstat(clone_fd, "clone mode").st_mode)
                != expected.mode
            ):
                try:
                    os.fchmod(clone_fd, expected.mode)
                except OSError as exc:
                    raise self._os_error("policy_apply_failed", "fchmod clone", exc)

            self._replace_xattrs(clone_fd, expected.xattrs)
            ctypes.set_errno(0)
            if self._acl_set_fd_np(clone_fd, apply_acl, ACL_TYPE_EXTENDED) != 0:
                self._raise_errno("policy_apply_failed", "acl_set_fd_np clone")

            clone_stat = self._fstat(clone_fd, "clone timestamp calibration")
            if clone_stat.st_mtime_ns != expected.mtime_ns:
                try:
                    os.utime(
                        clone_fd,
                        ns=(clone_stat.st_atime_ns, expected.mtime_ns),
                    )
                except OSError as exc:
                    raise self._os_error("policy_apply_failed", "set clone mtime", exc)

            current_flags = int(
                getattr(self._fstat(clone_fd, "clone flags"), "st_flags", 0)
            )
            if current_flags != expected.flags:
                self._set_flags(clone_fd, current_flags, expected.flags)
        except BaseException as exc:
            acl_primary_error = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                acl_owners = (
                    (
                        "clone policy apply ACL",
                        (
                            apply_acl_owner
                            if apply_acl_owner is not live_acl_owner
                            else None
                        ),
                    ),
                    ("clone policy live ACL", live_acl_owner),
                )
                self._close_acl_owners(acl_owners, primary_error=acl_primary_error)
            except BaseException as cleanup_failure:
                if acl_primary_error is not None:
                    self._attach_cleanup_diagnostic(acl_primary_error, cleanup_failure)
                    try:
                        _cleanup_dispatch = 2
                        acl_owners = (
                            (
                                "clone policy apply ACL",
                                (
                                    apply_acl_owner
                                    if apply_acl_owner is not live_acl_owner
                                    else None
                                ),
                            ),
                            ("clone policy live ACL", live_acl_owner),
                        )
                        self._close_acl_owners(
                            acl_owners, primary_error=acl_primary_error
                        )
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(
                            acl_primary_error, retry_failure
                        )
                else:
                    try:
                        _cleanup_dispatch = 2
                        acl_owners = (
                            (
                                "clone policy apply ACL",
                                (
                                    apply_acl_owner
                                    if apply_acl_owner is not live_acl_owner
                                    else None
                                ),
                            ),
                            ("clone policy live ACL", live_acl_owner),
                        )
                        self._close_acl_owners(
                            acl_owners, primary_error=acl_primary_error
                        )
                    finally:
                        raise

        original_after = self.snapshot_policy(original_fd)
        if original_after != expected:
            raise BackendError(
                "original_policy_changed", "original policy changed during calibration"
            )
        self.require_exclusive_writer_policy(
            original_after, "original mirror after clone calibration"
        )
        clone_after = self.snapshot_policy(clone_fd)
        if clone_after != expected:
            raise BackendError(
                "clone_policy_mismatch", "clone policy does not match the original"
            )
        self.require_exclusive_writer_policy(
            clone_after, "clone after policy calibration"
        )
        return clone_after

    def swap_names(
        self,
        left_parent_fd: int,
        left_name: str,
        left_expected: FileIdentity,
        right_parent_fd: int,
        right_name: str,
        right_expected: FileIdentity,
        *,
        authorize_state: Callable[[str], None],
        action: str,
    ) -> Tuple[FileIdentity, FileIdentity]:
        """Identity-check, atomically swap, and verify the resulting mapping."""
        left_bytes = self._leaf_bytes(left_name)
        right_bytes = self._leaf_bytes(right_name)
        self._require_directory(left_parent_fd, "left swap parent")
        self._require_directory(right_parent_fd, "right swap parent")
        if left_parent_fd == right_parent_fd and left_name == right_name:
            raise BackendError("invalid_swap", "swap names must be distinct")
        if left_expected.is_same_object(right_expected):
            raise BackendError("invalid_swap", "swap objects must be distinct")
        self.require_identity_at(left_parent_fd, left_name, left_expected)
        self.require_identity_at(right_parent_fd, right_name, right_expected)
        ctypes.set_errno(0)
        self._authorize_state(authorize_state, action)
        self._require_directory(left_parent_fd, "left swap parent")
        self._require_directory(right_parent_fd, "right swap parent")
        self.require_identity_at(left_parent_fd, left_name, left_expected)
        self.require_identity_at(right_parent_fd, right_name, right_expected)
        result = self._renameatx_np(
            left_parent_fd,
            left_bytes,
            right_parent_fd,
            right_bytes,
            RENAME_SWAP,
        )
        if result != 0:
            self._raise_errno(
                "swap_failed", f"renameatx_np({left_name!r}, {right_name!r})"
            )
        try:
            left_after = self.require_identity_at(
                left_parent_fd, left_name, right_expected
            )
            right_after = self.require_identity_at(
                right_parent_fd, right_name, left_expected
            )
        except BackendError as exc:
            raise BackendError(
                "swap_postcondition_unverified",
                f"namespace changed but post-swap identity is unverified: {exc.detail}",
                exc.errno_value,
            )
        return (left_after, right_after)

    def unlink_name(
        self,
        parent_fd: int,
        name: str,
        expected: FileIdentity,
        *,
        authorize_state: Callable[[str], None],
        action: str,
        validate_after_authorization: Callable[[], None],
    ) -> None:
        """Authorize, revalidate protected state, then unlink the expected object."""
        name_bytes = self._leaf_bytes(name)
        self._require_directory(parent_fd, "unlink parent")
        if not callable(validate_after_authorization):
            raise BackendError(
                "invalid_post_authorization_validator",
                "validate_after_authorization must be callable",
            )
        self._authorize_state(authorize_state, action)
        validate_after_authorization()
        self.require_identity_at(parent_fd, name, expected)
        ctypes.set_errno(0)
        if self._unlinkat(parent_fd, name_bytes, 0) != 0:
            self._raise_errno("unlink_failed", f"unlinkat {name!r}")
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._os_error(
                "unlink_postcondition_unverified", f"revalidate {name!r}", exc
            )
        raise BackendError(
            "unlink_postcondition_unverified", f"a name exists at {name!r} after unlink"
        )

    def fsync(self, fd: int) -> None:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise self._os_error("fsync_failed", f"fsync fd {fd}", exc)

    def full_fsync(self, fd: int) -> None:
        try:
            fcntl.fcntl(fd, F_FULLFSYNC)
        except OSError as exc:
            raise self._os_error("full_fsync_failed", f"F_FULLFSYNC fd {fd}", exc)

    def validate_private_stage_parent(self, fd: int) -> FileIdentity:
        info = self._fstat(fd, "stage parent")
        identity = self._identity_from_stat(info)
        if not stat.S_ISDIR(info.st_mode):
            raise BackendError("unsafe_stage_parent", "stage parent is not a directory")
        if info.st_uid != os.geteuid():
            raise BackendError(
                "unsafe_stage_parent", "stage parent is not owned by euid"
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BackendError("unsafe_stage_parent", "stage parent mode is not 0700")
        flags = int(getattr(info, "st_flags", 0))
        if flags & (_BLOCKING_FLAGS | _INTERNAL_FLAGS | ~_KNOWN_FLAGS):
            raise BackendError(
                "unsafe_stage_parent", "stage parent has unsafe BSD flags"
            )
        if self._acl_has_entries(fd):
            raise BackendError(
                "unsafe_stage_parent", "stage parent has an extended ACL"
            )
        return identity

    def validate_stage_container(self, fd: int) -> FileIdentity:
        """Require a held directory whose children other principals cannot replace."""
        info = self._fstat(fd, "stage container")
        identity = self._identity_from_stat(info)
        if not stat.S_ISDIR(info.st_mode):
            raise BackendError(
                "unsafe_stage_container", "stage container is not a directory"
            )
        if info.st_uid != os.geteuid():
            raise BackendError(
                "unsafe_stage_container", "stage container is not owned by euid"
            )
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise BackendError(
                "unsafe_stage_container",
                "stage container grants group or other write access",
            )
        flags = int(getattr(info, "st_flags", 0))
        if flags & (_BLOCKING_FLAGS | _INTERNAL_FLAGS | ~_KNOWN_FLAGS):
            raise BackendError(
                "unsafe_stage_container", "stage container has unsafe BSD flags"
            )
        if self._acl_has_entries(fd):
            raise BackendError(
                "unsafe_stage_container", "stage container has an extended ACL"
            )
        return identity

    def harden_private_state_fd(self, fd: int, *, is_directory: bool) -> FileIdentity:
        """Harden one held state FD and durably verify its access policy."""
        before = self.identity(fd)
        info = self._fstat(fd, "private state")
        if info.st_uid != os.geteuid():
            raise BackendError(
                "unsafe_private_state", "private state object is not owned by euid"
            )
        if is_directory:
            if not stat.S_ISDIR(info.st_mode):
                raise BackendError(
                    "unsafe_private_state", "private state object is not a directory"
                )
            required_mode = 0o700
        else:
            if not stat.S_ISREG(info.st_mode):
                raise BackendError(
                    "unsafe_private_state", "private state object is not a regular file"
                )
            if info.st_nlink != 1:
                raise BackendError(
                    "unsafe_private_state",
                    f"private state file has link count {info.st_nlink}, expected 1",
                )
            required_mode = 0o600

        current_flags = int(getattr(info, "st_flags", 0))
        self._preflight_policy_flags(0, current_flags)
        if current_flags:
            self._set_flags(fd, current_flags, 0)

        empty_acl_owner = self._empty_acl_owned()
        with empty_acl_owner:
            empty_acl = empty_acl_owner.pointer()
            ctypes.set_errno(0)
            if self._acl_set_fd_np(fd, empty_acl, ACL_TYPE_EXTENDED) != 0:
                self._raise_errno(
                    "private_state_hardening_failed", "clear private state ACL"
                )
            empty_acl_owner.close()
        try:
            os.fchmod(fd, required_mode)
        except OSError as exc:
            raise self._os_error(
                "private_state_hardening_failed",
                f"chmod private state {required_mode:04o}",
                exc,
            )

        verified = self._validate_private_state_fd(
            fd, is_directory=is_directory, required_mode=required_mode
        )
        if not verified.is_same_object(before):
            raise BackendError(
                "object_replaced", "private state FD changed identity while hardening"
            )
        self.fsync(fd)
        durable = self._validate_private_state_fd(
            fd, is_directory=is_directory, required_mode=required_mode
        )
        if not durable.is_same_object(before):
            raise BackendError(
                "object_replaced", "private state FD changed identity after fsync"
            )
        return durable

    def validate_private_state_fd(self, fd: int, *, is_directory: bool) -> FileIdentity:
        """Non-mutating strict access-policy validation for a held state FD."""
        if not isinstance(is_directory, bool):
            raise BackendError(
                "invalid_private_state_kind", "is_directory must be a boolean"
            )
        return self._validate_private_state_fd(
            fd,
            is_directory=is_directory,
            required_mode=0o700 if is_directory else 0o600,
        )

    def _validate_private_state_fd(
        self, fd: int, *, is_directory: bool, required_mode: int
    ) -> FileIdentity:
        info = self._fstat(fd, "private state revalidation")
        identity = self._identity_from_stat(info)
        if is_directory:
            valid_type = stat.S_ISDIR(info.st_mode)
            valid_links = True
        else:
            valid_type = stat.S_ISREG(info.st_mode)
            valid_links = info.st_nlink == 1
        if not valid_type or not valid_links:
            raise BackendError(
                "unsafe_private_state", "private state type or link count changed"
            )
        if info.st_uid != os.geteuid():
            raise BackendError("unsafe_private_state", "private state owner changed")
        if stat.S_IMODE(info.st_mode) != required_mode:
            raise BackendError(
                "unsafe_private_state",
                f"private state mode is not {required_mode:04o}",
            )
        if int(getattr(info, "st_flags", 0)) != 0:
            raise BackendError(
                "unsafe_private_state", "private state BSD flags are not clear"
            )
        if self._acl_has_entries(fd):
            raise BackendError(
                "unsafe_private_state", "private state still has an extended ACL"
            )
        return identity

    def _create_private_stage_parent_core_owned(
        self,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> _OwnedStageFD:
        """Return a lazy owner for one new, hardened private stage directory."""
        self._leaf_bytes(name)
        flags = os.O_RDONLY | os.O_NONBLOCK | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW

        def acquire(target: _OwnedFD) -> None:
            if not isinstance(target, _OwnedStageFD):
                raise BackendError(
                    "invalid_stage_owner", "stage acquisition requires a stage owner"
                )
            container_identity = self.validate_stage_container(parent_fd)
            self._authorize_state(authorize_state, "create_stage")
            if not self.validate_stage_container(parent_fd).is_same_object(
                container_identity
            ):
                raise BackendError(
                    "parent_replaced",
                    "stage container changed after creation authorization",
                )

            def cleanup_created_stage() -> Optional[str]:
                try:
                    # fmt: off
                    target._namespace_cleanup_attempted = True; current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)  # noqa: E702
                # fmt: on
                except FileNotFoundError:
                    self.fsync(parent_fd)
                    if not self.validate_stage_container(parent_fd).is_same_object(
                        container_identity
                    ):
                        return "stage container changed after absent-stage fsync"
                    return None
                expected = target._stage_identity
                if expected is None:
                    expected = self._identity_from_stat(current_stat)
                return self._cleanup_unopened_stage(
                    parent_fd,
                    name,
                    expected,
                    container_identity,
                    authorize_state=_StageCleanupAuthorizer(target, authorize_state),
                )

            target._configure_namespace_cleanup(cleanup_created_stage)
            try:
                # fmt: off
                os.mkdir(name, 0o700, dir_fd=parent_fd); target._namespace_created = True  # noqa: E702
            # fmt: on
            except OSError as exc:
                raise self._os_error(
                    "stage_create_failed", f"mkdir private stage {name!r}", exc
                )
            try:
                created_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise BackendError(
                    "stage_created_unverified",
                    f"stage was created but its initial identity is unavailable: {exc}",
                    exc.errno,
                ) from exc
            created_identity = self._identity_from_stat(created_stat)
            if (
                not stat.S_ISDIR(created_stat.st_mode)
                or created_stat.st_uid != os.geteuid()
            ):
                raise BackendError(
                    "stage_created_unverified",
                    "new stage name is not an euid-owned directory; preserving evidence",
                )
            target._set_identity(created_identity)
            stage_owner = self._native_fd_owner(
                f"private stage {name!r}",
                lambda: os.open(name, flags, dir_fd=parent_fd),
                reason="stage_open_failed",
                operation=f"open private stage {name!r}",
            )
            stage_owner.retain_on_exception()
            opened = False
            identity = created_identity
            try:
                with stage_owner:
                    opened = True
                    target._share_from(stage_owner)
                    stage_fd = stage_owner.fileno()
                    opened_identity = self.identity(stage_fd)
                    if not opened_identity.is_same_object(created_identity):
                        raise BackendError(
                            "stage_created_unverified",
                            "opened stage identity differs from the just-created directory",
                        )
                    self.require_directory_identity_at(
                        parent_fd, name, created_identity
                    )
                    info = self._fstat(stage_fd, "new stage parent")
                    if info.st_uid != os.geteuid():
                        raise BackendError(
                            "unsafe_stage_parent",
                            "new stage directory is not owned by euid",
                        )
                    current_flags = int(getattr(info, "st_flags", 0))
                    if current_flags:
                        self._preflight_policy_flags(0, current_flags)
                        self._set_flags(stage_fd, current_flags, 0)
                    empty_acl_owner = self._empty_acl_owned()
                    with empty_acl_owner:
                        empty_acl = empty_acl_owner.pointer()
                        ctypes.set_errno(0)
                        if (
                            self._acl_set_fd_np(stage_fd, empty_acl, ACL_TYPE_EXTENDED)
                            != 0
                        ):
                            self._raise_errno(
                                "stage_hardening_failed",
                                "clear inherited stage ACL",
                            )
                        empty_acl_owner.close()
                    try:
                        os.fchmod(stage_fd, 0o700)
                    except OSError as exc:
                        raise self._os_error(
                            "stage_hardening_failed",
                            "chmod private stage 0700",
                            exc,
                        )
                    identity = self.validate_private_stage_parent(stage_fd)
                    if not self.validate_stage_container(parent_fd).is_same_object(
                        container_identity
                    ):
                        raise BackendError(
                            "parent_replaced",
                            "stage container changed during creation",
                        )
                    self.require_directory_identity_at(parent_fd, name, identity)
                    self.fsync(stage_fd)
                    self.fsync(parent_fd)
                    target._set_identity(identity)
            except BaseException as exc:
                target._cleanup_created_namespace(exc)
                if not opened:
                    stage_owner.close(primary_error=exc)
                    if isinstance(exc, BackendError) and (
                        exc.reason == "stage_open_failed"
                    ):
                        if target._namespace_cleanup_complete:
                            raise BackendError(
                                "stage_open_failed",
                                "stage was created, identity-checked, and cleaned: "
                                f"{name!r}",
                                exc.errno_value,
                            ) from exc
                        cleanup_detail = self._exact_cleanup_text(
                            getattr(exc, "cleanup_diagnostic", None)
                        )
                        raise BackendError(
                            "stage_created_unverified",
                            "stage cannot be opened "
                            f"({self._exception_diagnostic(exc)}); cleanup not proved "
                            f"({cleanup_detail or '<unprintable>'})",
                            exc.errno_value,
                        ) from exc
                    raise
                stage_owner.close(primary_error=exc)
                raise

        return _OwnedStageFD(self, f"private stage {name!r}", acquire)

    def _create_private_stage_parent_owned(
        self,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> _OwnedStageFD:
        bound = self.create_private_stage_parent
        if (
            getattr(bound, "__func__", None)
            is DarwinBackend.create_private_stage_parent
        ):
            return self._create_private_stage_parent_core_owned(
                parent_fd, name, authorize_state=authorize_state
            )

        def acquire(target: _OwnedFD) -> None:
            if not isinstance(target, _OwnedStageFD):
                raise BackendError(
                    "invalid_stage_owner", "stage acquisition requires a stage owner"
                )
            container_identity = self.validate_stage_container(parent_fd)

            def cleanup_created_stage() -> Optional[str]:
                try:
                    # fmt: off
                    target._namespace_cleanup_attempted = True; os.stat(name, dir_fd=parent_fd, follow_symlinks=False)  # noqa: E702
                # fmt: on
                except FileNotFoundError:
                    self.fsync(parent_fd)
                    if not self.validate_stage_container(parent_fd).is_same_object(
                        container_identity
                    ):
                        return "stage container changed after absent-stage fsync"
                    return None
                expected = target._stage_identity
                if expected is None:
                    return "overridden stage identity was not handed off"
                return self._cleanup_unopened_stage(
                    parent_fd,
                    name,
                    expected,
                    container_identity,
                    authorize_state=_StageCleanupAuthorizer(target, authorize_state),
                )

            target._configure_namespace_cleanup(cleanup_created_stage)
            unacquired = object()
            handed_off: object = unacquired
            handed_off_fd = -1
            identity: Optional[FileIdentity] = None
            try:
                handed_off = bound(parent_fd, name, authorize_state=authorize_state)
                if type(handed_off) is tuple and len(handed_off) > 0:
                    candidate_fd = handed_off[0]
                    candidate_identity = handed_off[1] if len(handed_off) > 1 else None
                    target._namespace_created = True
                    if (
                        isinstance(candidate_fd, int)
                        and not isinstance(candidate_fd, bool)
                        and candidate_fd >= 0
                    ):
                        handed_off_fd = candidate_fd
                        target._adopt(handed_off_fd)
                    if isinstance(candidate_identity, FileIdentity):
                        identity = candidate_identity
                        target._set_identity(identity)
                if type(handed_off) is not tuple or len(handed_off) != 2:
                    raise BackendError(
                        "invalid_stage_result",
                        "overridden stage creator returned an invalid value",
                    )
                handed_off_fd, identity = handed_off
                target._namespace_created = True
                target._set_identity(identity)
                if target.closed:
                    target._adopt(handed_off_fd)
            except BaseException as primary:
                if type(handed_off) is tuple and len(handed_off) > 0:
                    candidate_fd = handed_off[0]
                    if (
                        isinstance(candidate_fd, int)
                        and not isinstance(candidate_fd, bool)
                        and candidate_fd >= 0
                    ):
                        handed_off_fd = candidate_fd
                    if len(handed_off) > 1:
                        identity = handed_off[1]
                if handed_off_fd >= 0 or identity is not None:
                    target._namespace_created = True
                if handed_off_fd >= 0 and target.closed:
                    try:
                        target._adopt(handed_off_fd)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                if identity is not None:
                    try:
                        target._set_identity(identity)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedStageFD(self, f"overridden private stage {name!r}", acquire)

    def create_private_stage_parent(
        self,
        parent_fd: int,
        name: str,
        *,
        authorize_state: Callable[[str], None],
    ) -> Tuple[int, FileIdentity]:
        """Create, harden, verify, and sync an owner-private empty directory."""
        owner = self._create_private_stage_parent_core_owned(
            parent_fd, name, authorize_state=authorize_state
        )
        return self._handoff_stage_owner(owner)

    def _cleanup_unopened_stage(
        self,
        parent_fd: int,
        name: str,
        expected: FileIdentity,
        expected_container: FileIdentity,
        *,
        authorize_state: Callable[[str], None],
    ) -> Optional[str]:
        """Best-effort cleanup whose rmdir remains identity- and empty-bound."""
        try:
            if not self.validate_stage_container(parent_fd).is_same_object(
                expected_container
            ):
                return "stage container identity or policy changed"
            current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            current = self._identity_from_stat(current_stat)
            if (
                not current.is_same_object(expected)
                or not stat.S_ISDIR(current_stat.st_mode)
                or current_stat.st_uid != os.geteuid()
            ):
                return "stage name no longer maps to the created directory"
            if not self.validate_stage_container(parent_fd).is_same_object(
                expected_container
            ):
                return "stage container changed before rmdir"
        except BaseException as exc:
            if isinstance(authorize_state, _StageCleanupAuthorizer):
                raise
            return self._exception_diagnostic(exc)
        try:
            self._authorize_state(authorize_state, "remove_stage")
            if not self.validate_stage_container(parent_fd).is_same_object(
                expected_container
            ):
                return "stage container changed after cleanup authorization"
            current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            current = self._identity_from_stat(current_stat)
            if (
                not current.is_same_object(expected)
                or not stat.S_ISDIR(current_stat.st_mode)
                or current_stat.st_uid != os.geteuid()
            ):
                return "stage name changed after cleanup authorization"
        except BaseException as exc:
            if isinstance(authorize_state, _StageCleanupAuthorizer):
                if (
                    authorize_state._target._namespace_cleanup_authorization_attempted
                    and not authorize_state._target._namespace_cleanup_authorized
                ):
                    return self._exception_diagnostic(exc)
                raise
            return self._exception_diagnostic(exc)
        try:
            os.rmdir(name, dir_fd=parent_fd)
            self.fsync(parent_fd)
            if not self.validate_stage_container(parent_fd).is_same_object(
                expected_container
            ):
                return "stage container changed after rmdir"
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            return "stage name still exists after rmdir"
        except BaseException as exc:
            if isinstance(authorize_state, _StageCleanupAuthorizer):
                raise
            return self._exception_diagnostic(exc)

    def create_private_stage(
        self, path: str, *, authorize_state: Callable[[str], None]
    ) -> FileIdentity:
        """Create a private stage by absolute path and close its verified fd."""
        parent_owner, name = self._open_absolute_parent_owned(path)
        parent_fd = -1
        stage_owner: Optional[_OwnedStageFD] = None
        primary_error: Optional[BaseException] = None
        durable_namespace_complete = False
        try:
            with parent_owner:
                parent_fd = parent_owner.fileno()
            container_identity = self.validate_stage_container(parent_fd)
            stage_owner = self._create_private_stage_parent_owned(
                parent_fd, name, authorize_state=authorize_state
            )
            with stage_owner:
                identity = stage_owner.identity()
            try:
                self._require_stage_container_mapping(
                    path, parent_fd, container_identity
                )
            except BaseException as exc:
                try:
                    _cleanup_dispatch = 1
                    stage_owner._cleanup_created_namespace(exc)
                except BaseException as cleanup_failure:
                    self._attach_cleanup_diagnostic(exc, cleanup_failure)
                    try:
                        _cleanup_dispatch = 2
                        stage_owner._cleanup_created_namespace(exc)
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(exc, retry_failure)
                if not stage_owner._namespace_cleanup_complete:
                    cleanup_detail = self._exact_cleanup_text(
                        getattr(exc, "cleanup_diagnostic", None)
                    )
                    self._attach_cleanup_diagnostic(
                        exc,
                        BackendError(
                            "stage_create_cleanup_failed",
                            "absolute stage binding cleanup failed "
                            f"({cleanup_detail or '<unprintable>'})",
                        ),
                    )
                raise
            durable_namespace_complete = True
            _cleanup_dispatch = 1
            self._close_fd_owners(
                (("created private stage", stage_owner),),
                durable_namespace_complete=durable_namespace_complete,
            )
            # Closing the last recovery parent and committing the return share
            # one trace line.  Before this line the exception path can still
            # remove an unreturned stage through the verified parent binding.
            # fmt: off
            parent_owner.close(durable_namespace_complete=True); return identity  # noqa: E702
        # fmt: on
        except BaseException as exc:
            primary_error = exc
            try:
                _cleanup_dispatch = 1
                if stage_owner is not None:
                    stage_owner._cleanup_created_namespace(exc)
                self._close_fd_owners(
                    (
                        ("created private stage", stage_owner),
                        ("created private stage parent", parent_owner),
                    ),
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            except BaseException as cleanup_failure:
                self._attach_cleanup_diagnostic(primary_error, cleanup_failure)
                try:
                    _cleanup_dispatch = 2
                    if stage_owner is not None:
                        stage_owner._cleanup_created_namespace(exc)
                    self._close_fd_owners(
                        (
                            ("created private stage", stage_owner),
                            ("created private stage parent", parent_owner),
                        ),
                        primary_error=primary_error,
                        durable_namespace_complete=False,
                    )
                except BaseException as retry_failure:
                    self._attach_cleanup_diagnostic(primary_error, retry_failure)
            raise

    def remove_empty_private_stage(
        self,
        path: str,
        expected: FileIdentity,
        *,
        expected_container: FileIdentity,
        authorize_state: Callable[[str], None],
    ) -> None:
        """Idempotently remove one recorded private stage if it is still empty."""
        if not isinstance(expected, FileIdentity) or not isinstance(
            expected_container, FileIdentity
        ):
            raise BackendError(
                "invalid_stage_evidence", "stage cleanup identities have the wrong type"
            )
        parent_owner, name = self._open_absolute_parent_owned(path)
        parent_fd = -1
        stage_owner: Optional[_OwnedFD] = None
        stage_fd = -1
        primary_error: Optional[BaseException] = None
        durable_namespace_complete = False
        try:
            with parent_owner:
                parent_fd = parent_owner.fileno()
            self._require_stage_container_mapping(path, parent_fd, expected_container)
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.fsync(parent_fd)
                self._require_stage_container_mapping(
                    path, parent_fd, expected_container
                )
                durable_namespace_complete = True
                return
            except OSError as exc:
                raise self._os_error(
                    "stage_absence_unverified", f"stat private stage {name!r}", exc
                )
            self.require_directory_identity_at(parent_fd, name, expected)
            flags = (
                os.O_RDONLY | os.O_NONBLOCK | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW
            )
            stage_owner = self._native_fd_owner(
                f"private stage {name!r}",
                lambda: os.open(name, flags, dir_fd=parent_fd),
                reason="stage_remove_failed",
                operation=f"open private stage {name!r}",
            )
            with stage_owner:
                stage_fd = stage_owner.fileno()
            if not self.identity(stage_fd).is_same_object(expected):
                raise BackendError(
                    "identity_mismatch", "opened private stage identity changed"
                )
            self.validate_private_stage_parent(stage_fd)
            if os.listdir(stage_fd):
                raise BackendError(
                    "stage_not_cleanable", "private stage directory is not empty"
                )
            self._require_stage_container_mapping(path, parent_fd, expected_container)
            self.require_directory_identity_at(parent_fd, name, expected)
            self._authorize_state(authorize_state, "remove_stage")
            self._require_stage_container_mapping(path, parent_fd, expected_container)
            self.validate_private_stage_parent(stage_fd)
            if os.listdir(stage_fd):
                raise BackendError(
                    "stage_not_cleanable",
                    "private stage became nonempty after removal authorization",
                )
            self.require_directory_identity_at(parent_fd, name, expected)
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as exc:
                raise self._os_error(
                    "stage_remove_failed", f"rmdir private stage {name!r}", exc
                )
            self.fsync(parent_fd)
            self._require_stage_container_mapping(path, parent_fd, expected_container)
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                durable_namespace_complete = True
                return
            except OSError as exc:
                raise self._os_error(
                    "stage_remove_postcondition_unverified",
                    f"revalidate removed private stage {name!r}",
                    exc,
                )
            raise BackendError(
                "stage_remove_postcondition_unverified",
                "an object exists at the private stage path after rmdir",
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                self._close_fd_owners(
                    (
                        ("private stage", stage_owner),
                        ("private stage parent", parent_owner),
                    ),
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            except BaseException as cleanup_failure:
                if primary_error is not None:
                    self._attach_cleanup_diagnostic(primary_error, cleanup_failure)
                    try:
                        _cleanup_dispatch = 2
                        self._close_fd_owners(
                            (
                                ("private stage", stage_owner),
                                ("private stage parent", parent_owner),
                            ),
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(primary_error, retry_failure)
                else:
                    try:
                        _cleanup_dispatch = 2
                        self._close_fd_owners(
                            (
                                ("private stage", stage_owner),
                                ("private stage parent", parent_owner),
                            ),
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    finally:
                        raise

    def cleanup_intent_stage(
        self,
        stage_path: str,
        *,
        final_path: str,
        expected_container: FileIdentity,
        expected_original: SnapshotExpectation,
        expected_stage: Optional[FileIdentity],
        allow_clone: bool,
        expected_clone: Optional[FileIdentity],
        expected_snapshot: Optional[SnapshotExpectation],
        expected_size: Optional[int],
        expected_sha256: Optional[str],
        authorize_state: Callable[[str], None],
    ) -> str:
        """Strictly remove an INTENT-owned empty stage or one proven clone.

        A lone clone is deletable only after a durable caller explicitly permits
        it, supplies its expected evidence, and the final name still maps to the
        complete durable original survivor.  Object identity is additionally
        required once the caller has durably observed the clone.  A PLANNED
        intent may adopt only an empty, safe stage at the exact tool txid-shaped
        path under its durable container.  No other entry, object type, link
        count, or owner is adopted.
        """
        if not isinstance(allow_clone, bool):
            raise BackendError(
                "invalid_intent_evidence", "allow_clone must be a boolean"
            )
        if not isinstance(expected_container, FileIdentity):
            raise BackendError(
                "invalid_intent_evidence",
                "expected stage container identity has the wrong type",
            )
        if not isinstance(final_path, str) or not final_path.startswith("/"):
            raise BackendError(
                "invalid_intent_evidence", "final path must be an absolute string"
            )
        if not isinstance(expected_original, SnapshotExpectation):
            raise BackendError(
                "invalid_intent_evidence",
                "expected original snapshot has the wrong type",
            )
        if expected_stage is not None and not isinstance(expected_stage, FileIdentity):
            raise BackendError(
                "invalid_intent_evidence", "expected stage identity has the wrong type"
            )
        if expected_clone is not None and not isinstance(expected_clone, FileIdentity):
            raise BackendError(
                "invalid_intent_evidence", "expected clone identity has the wrong type"
            )
        if expected_snapshot is not None and not isinstance(
            expected_snapshot, SnapshotExpectation
        ):
            raise BackendError(
                "invalid_intent_evidence",
                "expected clone snapshot has the wrong type",
            )
        if not allow_clone:
            if any(
                value is not None
                for value in (
                    expected_clone,
                    expected_snapshot,
                    expected_size,
                    expected_sha256,
                )
            ):
                raise BackendError(
                    "invalid_intent_evidence",
                    "clone evidence is not valid when clone cleanup is forbidden",
                )
        else:
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise BackendError(
                    "invalid_intent_evidence",
                    "clone cleanup requires a nonnegative expected size",
                )
            if not isinstance(expected_sha256, str):
                raise BackendError(
                    "invalid_intent_evidence",
                    "clone cleanup requires an expected SHA-256",
                )
            self._validate_sha256(expected_sha256, "intent clone SHA-256")
            if expected_clone is None and expected_snapshot is not None:
                raise BackendError(
                    "invalid_intent_evidence",
                    "STAGE_BOUND clone cleanup cannot claim a durable clone snapshot",
                )
            if expected_clone is not None:
                if expected_snapshot is None:
                    raise BackendError(
                        "invalid_intent_evidence",
                        "CLONE_BOUND cleanup requires the complete durable clone snapshot",
                    )
                if (
                    (expected_snapshot.dev, expected_snapshot.ino)
                    != expected_clone.object_key
                    or expected_snapshot.size != expected_size
                    or expected_snapshot.sha256 != expected_sha256
                ):
                    raise BackendError(
                        "invalid_intent_evidence",
                        "clone identity, size, digest, and snapshot evidence disagree",
                    )
                if (expected_original.dev, expected_original.ino) == (
                    expected_clone.dev,
                    expected_clone.ino,
                ):
                    raise BackendError(
                        "invalid_intent_evidence",
                        "durable original and clone identify the same object",
                    )

        parent_owner, stage_name = self._open_absolute_parent_owned(stage_path)
        parent_fd = -1
        stage_owner: Optional[_OwnedFD] = None
        stage_fd = -1
        clone_owner: Optional[_OwnedFD] = None
        clone_fd = -1
        final_parent_owner: Optional[_OwnedFD] = None
        final_parent_fd = -1
        original_owner: Optional[_OwnedFD] = None
        original_fd = -1
        final_name = ""
        primary_error: Optional[BaseException] = None
        durable_namespace_complete = False

        def require_original_survivor(subject: str) -> FileSnapshot:
            nonlocal final_parent_owner, final_parent_fd, final_name
            nonlocal original_owner, original_fd
            if final_parent_owner is None:
                opened_parent, opened_name = self._open_absolute_parent_owned(
                    final_path
                )
                with opened_parent:
                    opened_parent.retain_if_registered(
                        lambda: final_parent_owner is opened_parent
                    )
                    final_parent_owner = opened_parent
                    final_parent_fd = opened_parent.fileno()
                    final_name = opened_name
                self._require_stage_container_mapping(
                    final_path, final_parent_fd, expected_container
                )
            if original_owner is None:
                opened_original = self._dispatch_open_leaf_owned(
                    final_parent_fd, final_name
                )
                with opened_original:
                    opened_original.retain_if_registered(
                        lambda: original_owner is opened_original
                    )
                    original_owner = opened_original
                    original_fd = opened_original.fileno()
            survivor = self.require_snapshot(
                original_fd,
                expected_original,
                subject,
                mismatch_reason="intent_original_snapshot_mismatch",
                changed_reason="intent_original_snapshot_changed",
                unreadable_reason="intent_original_snapshot_unreadable",
            )
            self.require_exclusive_writer_policy(survivor.policy, subject)
            self._require_stage_container_mapping(
                final_path, final_parent_fd, expected_container
            )
            self.require_identity_at(final_parent_fd, final_name, survivor.identity)
            return survivor

        try:
            with parent_owner:
                parent_fd = parent_owner.fileno()
            self._require_stage_container_mapping(
                stage_path, parent_fd, expected_container
            )
            try:
                os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.fsync(parent_fd)
                self._require_stage_container_mapping(
                    stage_path, parent_fd, expected_container
                )
                if allow_clone:
                    require_original_survivor(
                        "INTENT original survivor with absent cleanup stage"
                    )
                durable_namespace_complete = True
                return "absent"
            except OSError as exc:
                raise self._os_error(
                    "stage_absence_unverified",
                    f"stat INTENT stage {stage_name!r}",
                    exc,
                )
            if expected_stage is None:
                if allow_clone:
                    raise BackendError(
                        "intent_stage_identity_required",
                        "clone-capable INTENT state lacks a durable stage identity",
                    )
                prefix = ".codex-reflink-repair-"
                txid = (
                    stage_name[len(prefix) :] if stage_name.startswith(prefix) else ""
                )
                if len(txid) != 32 or any(
                    character not in "0123456789abcdef" for character in txid
                ):
                    raise BackendError(
                        "invalid_intent_stage_path",
                        "unbound PLANNED stage does not have an exact tool txid name",
                    )

            flags = (
                os.O_RDONLY | os.O_NONBLOCK | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW
            )
            stage_owner = self._native_fd_owner(
                f"INTENT stage {stage_name!r}",
                lambda: os.open(stage_name, flags, dir_fd=parent_fd),
                reason="intent_stage_unreadable",
                operation=f"open INTENT stage {stage_name!r}",
            )
            with stage_owner:
                stage_fd = stage_owner.fileno()
            stage_identity = self.validate_private_stage_parent(stage_fd)
            if expected_stage is not None and not stage_identity.is_same_object(
                expected_stage
            ):
                raise BackendError(
                    "identity_mismatch",
                    "INTENT stage differs from its durable identity",
                )
            self.require_directory_identity_at(parent_fd, stage_name, stage_identity)

            entries = self._list_directory(stage_fd, "INTENT stage")
            removed_clone = False
            if entries:
                if entries != ("clone",):
                    raise BackendError(
                        "intent_stage_not_cleanable",
                        "INTENT stage does not contain exactly one 'clone' entry",
                    )
                if not allow_clone:
                    raise BackendError(
                        "intent_stage_not_cleanable",
                        "durable INTENT state does not permit clone cleanup",
                    )
                clone_owner = self._dispatch_open_leaf_owned(stage_fd, "clone")
                with clone_owner:
                    clone_fd = clone_owner.fileno()
                clone_identity = self.identity(clone_fd)
                if clone_identity.uid != os.geteuid():
                    raise BackendError(
                        "unsafe_intent_clone",
                        "INTENT clone is not owned by euid",
                    )
                if clone_identity.dev != stage_identity.dev:
                    raise BackendError(
                        "unsafe_intent_clone",
                        "INTENT clone is not on the stage filesystem",
                    )
                if expected_clone is not None and not clone_identity.is_same_object(
                    expected_clone
                ):
                    raise BackendError(
                        "identity_mismatch",
                        "INTENT clone differs from its durable identity",
                    )
                if clone_identity.size != expected_size:
                    raise BackendError(
                        "snapshot_mismatch",
                        "INTENT clone size differs from durable evidence",
                    )
                if expected_snapshot is not None:
                    clone_sha256 = self.require_snapshot(
                        clone_fd,
                        expected_snapshot,
                        "INTENT clone",
                        mismatch_reason="intent_clone_snapshot_mismatch",
                        changed_reason="intent_clone_snapshot_changed",
                        unreadable_reason="intent_clone_snapshot_unreadable",
                    ).sha256
                else:
                    clone_sha256 = self.sha256_fd(clone_fd)
                if clone_sha256 != expected_sha256:
                    raise BackendError(
                        "snapshot_mismatch",
                        "INTENT clone content differs from durable evidence",
                    )
                if self._list_directory(stage_fd, "INTENT stage") != ("clone",):
                    raise BackendError(
                        "intent_stage_not_cleanable",
                        "INTENT stage entries changed during clone validation",
                    )

                def validate_intent_unlink() -> None:
                    original_snapshot = require_original_survivor(
                        "INTENT original survivor after cleanup authorization"
                    )
                    if original_snapshot.identity.is_same_object(clone_identity):
                        raise BackendError(
                            "invalid_intent_evidence",
                            "INTENT original survivor and clone identify the same object",
                        )
                    self.validate_private_stage_parent(stage_fd)
                    if self._list_directory(stage_fd, "INTENT stage") != ("clone",):
                        raise BackendError(
                            "intent_stage_not_cleanable",
                            "INTENT stage entries changed after cleanup authorization",
                        )
                    self._require_stage_container_mapping(
                        stage_path, parent_fd, expected_container
                    )
                    self.require_directory_identity_at(
                        parent_fd, stage_name, stage_identity
                    )

                self.unlink_name(
                    stage_fd,
                    "clone",
                    clone_identity,
                    authorize_state=authorize_state,
                    action="intent_unlink_clone",
                    validate_after_authorization=validate_intent_unlink,
                )
                self.fsync(stage_fd)
                require_original_survivor(
                    "INTENT original survivor after clone cleanup"
                )
                removed_clone = True

            if self._list_directory(stage_fd, "INTENT stage"):
                raise BackendError(
                    "intent_stage_not_cleanable",
                    "INTENT stage is not empty after clone cleanup",
                )
            if not removed_clone:
                self.fsync(stage_fd)
            self._authorize_state(authorize_state, "intent_remove_stage")
            if allow_clone:
                # A prior attempt may have durably unlinked the clone before
                # interruption.  Re-prove the only survivor after the final
                # durable-state authorization before removing recovery evidence.
                require_original_survivor(
                    "INTENT original survivor after stage-removal authorization"
                )
            self.validate_private_stage_parent(stage_fd)
            self._require_stage_container_mapping(
                stage_path, parent_fd, expected_container
            )
            self.require_directory_identity_at(parent_fd, stage_name, stage_identity)
            if self._list_directory(stage_fd, "INTENT stage after authorization"):
                raise BackendError(
                    "intent_stage_not_cleanable",
                    "INTENT stage became nonempty after removal authorization",
                )
            try:
                os.rmdir(stage_name, dir_fd=parent_fd)
            except OSError as exc:
                raise self._os_error(
                    "stage_remove_failed", f"rmdir INTENT stage {stage_name!r}", exc
                )
            self.fsync(parent_fd)
            self._require_stage_container_mapping(
                stage_path, parent_fd, expected_container
            )
            try:
                os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                durable_namespace_complete = True
                return "removed-clone" if removed_clone else "removed-empty"
            except OSError as exc:
                raise self._os_error(
                    "stage_remove_postcondition_unverified",
                    f"revalidate removed INTENT stage {stage_name!r}",
                    exc,
                )
            raise BackendError(
                "stage_remove_postcondition_unverified",
                "an object exists at the INTENT stage path after rmdir",
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                self._close_fd_owners(
                    (
                        ("INTENT original", original_owner),
                        ("INTENT final parent", final_parent_owner),
                        ("INTENT clone", clone_owner),
                        ("INTENT stage", stage_owner),
                        ("INTENT stage parent", parent_owner),
                    ),
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            except BaseException as cleanup_failure:
                if primary_error is not None:
                    self._attach_cleanup_diagnostic(primary_error, cleanup_failure)
                    try:
                        _cleanup_dispatch = 2
                        self._close_fd_owners(
                            (
                                ("INTENT original", original_owner),
                                ("INTENT final parent", final_parent_owner),
                                ("INTENT clone", clone_owner),
                                ("INTENT stage", stage_owner),
                                ("INTENT stage parent", parent_owner),
                            ),
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    except BaseException as retry_failure:
                        self._attach_cleanup_diagnostic(primary_error, retry_failure)
                else:
                    try:
                        _cleanup_dispatch = 2
                        self._close_fd_owners(
                            (
                                ("INTENT original", original_owner),
                                ("INTENT final parent", final_parent_owner),
                                ("INTENT clone", clone_owner),
                                ("INTENT stage", stage_owner),
                                ("INTENT stage parent", parent_owner),
                            ),
                            primary_error=primary_error,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    finally:
                        raise

    def bind_transaction(
        self,
        source_path: str,
        destination_path: str,
        temporary_path: str,
        *,
        source_parent_expected: FileIdentity,
    ) -> "BoundTransaction":
        """Bind a live pair only under its previously inspected source parent."""
        if not isinstance(source_parent_expected, FileIdentity):
            raise BackendError(
                "invalid_source_parent_evidence",
                "expected source parent identity has the wrong type",
            )
        return BoundTransaction(
            self,
            source_path,
            destination_path,
            temporary_path,
            source_parent_expected=source_parent_expected,
        )

    def bind_transaction_owned(
        self,
        source_path: str,
        destination_path: str,
        temporary_path: str,
        *,
        source_parent_expected: FileIdentity,
    ) -> _OwnedTransaction:
        """Return a lazy owner for a newly bound live transaction."""

        def acquire(target: _OwnedTransaction) -> None:
            handed_off: Optional[BoundTransaction] = None
            try:
                handed_off = self.bind_transaction(
                    source_path,
                    destination_path,
                    temporary_path,
                    source_parent_expected=source_parent_expected,
                )
                target._adopt(handed_off)
            except BaseException as primary:
                if handed_off is not None and target.closed:
                    try:
                        target._adopt(handed_off)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedTransaction(acquire)

    def bind_recovery(
        self,
        source_path: Optional[str],
        destination_path: str,
        temporary_path: str,
        original_expected: FileIdentity,
        clone_expected: FileIdentity,
        *,
        source_expected: Optional[FileIdentity] = None,
        source_parent_expected: Optional[FileIdentity] = None,
        destination_parent_expected: Optional[FileIdentity] = None,
        temporary_parent_expected: Optional[FileIdentity] = None,
    ) -> "BoundTransaction":
        """Bind an existing before/forward namespace from durable identities."""
        return BoundTransaction.recover(
            self,
            source_path,
            destination_path,
            temporary_path,
            original_expected,
            clone_expected,
            source_expected=source_expected,
            source_parent_expected=source_parent_expected,
            destination_parent_expected=destination_parent_expected,
            temporary_parent_expected=temporary_parent_expected,
        )

    def bind_recovery_owned(
        self,
        source_path: Optional[str],
        destination_path: str,
        temporary_path: str,
        original_expected: FileIdentity,
        clone_expected: FileIdentity,
        *,
        source_expected: Optional[FileIdentity] = None,
        source_parent_expected: Optional[FileIdentity] = None,
        destination_parent_expected: Optional[FileIdentity] = None,
        temporary_parent_expected: Optional[FileIdentity] = None,
    ) -> _OwnedTransaction:
        """Return a lazy owner for a transaction rebound from durable state."""

        def acquire(target: _OwnedTransaction) -> None:
            handed_off: Optional[BoundTransaction] = None
            try:
                handed_off = self.bind_recovery(
                    source_path,
                    destination_path,
                    temporary_path,
                    original_expected,
                    clone_expected,
                    source_expected=source_expected,
                    source_parent_expected=source_parent_expected,
                    destination_parent_expected=destination_parent_expected,
                    temporary_parent_expected=temporary_parent_expected,
                )
                target._adopt(handed_off)
            except BaseException as primary:
                if handed_off is not None and target.closed:
                    try:
                        target._adopt(handed_off)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedTransaction(acquire)

    @staticmethod
    def _absolute_components(path: str) -> Tuple[str, ...]:
        if not isinstance(path, str) or not path.startswith("/"):
            raise BackendError("invalid_path", "directory path must be absolute")
        if "\x00" in path:
            raise BackendError("invalid_path", "directory path contains NUL")
        if path == "/":
            return ()
        if path.endswith("/"):
            raise BackendError("invalid_path", "directory path has a trailing slash")
        components = tuple(path[1:].split("/"))
        if any(component in ("", ".", "..") for component in components):
            raise BackendError(
                "invalid_path", "directory path has an unsafe or empty component"
            )
        for component in components:
            DarwinBackend._leaf_bytes(component)
        return components

    @staticmethod
    def _leaf_bytes(name: str) -> bytes:
        if not isinstance(name, str):
            raise BackendError("invalid_leaf", "leaf name must be a string")
        encoded = os.fsencode(name)
        if (
            not encoded
            or encoded in (b".", b"..")
            or b"/" in encoded
            or b"\x00" in encoded
        ):
            raise BackendError("invalid_leaf", "leaf name must be one safe component")
        return encoded

    @staticmethod
    def _identity_from_stat(info: os.stat_result) -> FileIdentity:
        return FileIdentity(
            dev=int(info.st_dev),
            ino=int(info.st_ino),
            mode=int(info.st_mode),
            nlink=int(info.st_nlink),
            size=int(info.st_size),
            uid=int(info.st_uid),
            gid=int(info.st_gid),
            mtime_ns=int(info.st_mtime_ns),
            ctime_ns=int(info.st_ctime_ns),
        )

    def _fstat(self, fd: int, subject: str) -> os.stat_result:
        try:
            return os.fstat(fd)
        except OSError as exc:
            raise self._os_error("fstat_failed", f"fstat {subject}", exc)

    def _require_directory(self, fd: int, subject: str) -> None:
        info = self._fstat(fd, subject)
        if not stat.S_ISDIR(info.st_mode):
            raise BackendError("not_directory", f"{subject} is not a directory")

    def _require_regular(self, fd: int, subject: str) -> None:
        self._require_regular_identity(self.identity(fd), subject)

    @staticmethod
    def _require_regular_identity(identity: FileIdentity, subject: str) -> None:
        DarwinBackend._require_regular_identity_with_link_count(
            identity, subject, expected_nlink=1
        )

    @staticmethod
    def _require_regular_identity_with_link_count(
        identity: FileIdentity, subject: str, *, expected_nlink: int
    ) -> None:
        if not stat.S_ISREG(identity.mode):
            raise BackendError("not_regular", f"{subject} is not a regular file")
        if identity.nlink != expected_nlink:
            raise BackendError(
                "unsafe_link_count",
                f"{subject} has link count {identity.nlink}, expected {expected_nlink}",
            )

    @staticmethod
    def _require_content_stable(
        before: FileIdentity, after: FileIdentity, subject: str
    ) -> None:
        DarwinBackend._require_content_stable_with_link_count(
            before, after, subject, expected_nlink=1
        )

    @staticmethod
    def _require_content_stable_with_link_count(
        before: FileIdentity,
        after: FileIdentity,
        subject: str,
        *,
        expected_nlink: int,
    ) -> None:
        if not before.is_same_object(after):
            raise BackendError(
                "object_replaced", f"{subject} inode changed during read"
            )
        if (
            not stat.S_ISREG(after.mode)
            or before.nlink != expected_nlink
            or after.nlink != expected_nlink
            or before.size != after.size
            or before.mtime_ns != after.mtime_ns
        ):
            raise BackendError(
                "content_changed",
                f"{subject} content-stability signals changed during read",
            )

    def _require_name_absent(self, parent_fd: int, name: str) -> None:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._os_error(
                "clone_target_unverified", f"check absent name {name!r}", exc
            )
        raise BackendError(
            "clone_target_exists", f"clone target {name!r} already exists", errno.EEXIST
        )

    @staticmethod
    def _validate_sha256(value: str, subject: str) -> None:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise BackendError(
                "invalid_snapshot_expectation",
                f"{subject} must be a lowercase hexadecimal SHA-256",
            )

    @staticmethod
    def _xattrs_sha256(xattrs: Tuple[Tuple[bytes, bytes], ...]) -> str:
        digest = hashlib.sha256()
        for name, value in sorted(xattrs):
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def _list_directory(self, fd: int, subject: str) -> Tuple[str, ...]:
        try:
            entries = os.listdir(fd)
        except OSError as exc:
            raise self._os_error("stage_not_cleanable", f"list {subject}", exc)
        if any(not isinstance(entry, str) for entry in entries):
            raise BackendError(
                "stage_not_cleanable", f"{subject} returned a non-string entry"
            )
        return tuple(sorted(entries))

    def _require_stage_container_mapping(
        self,
        stage_path: str,
        held_fd: int,
        expected: FileIdentity,
    ) -> None:
        held = self.validate_stage_container(held_fd)
        if not held.is_same_object(expected):
            raise BackendError(
                "parent_replaced",
                "held stage container differs from its durable identity",
            )
        reopened_owner, _stage_name = self._open_absolute_parent_owned(stage_path)
        with reopened_owner:
            reopened = self.validate_stage_container(reopened_owner.fileno())
            reopened_owner.close()
        if not reopened.is_same_object(expected):
            raise BackendError(
                "parent_replaced",
                "absolute stage container differs from its durable identity",
            )

    @staticmethod
    def _policy_from_stat(
        info: os.stat_result,
        xattrs: Tuple[Tuple[bytes, bytes], ...],
        acl_native: bytes,
    ) -> FilePolicy:
        return FilePolicy(
            uid=int(info.st_uid),
            gid=int(info.st_gid),
            mode=stat.S_IMODE(info.st_mode),
            flags=int(getattr(info, "st_flags", 0)),
            mtime_ns=int(info.st_mtime_ns),
            xattrs=xattrs,
            acl_native=acl_native,
        )

    def _list_xattr_names_once(self, fd: int) -> Tuple[bytes, ...]:
        ctypes.set_errno(0)
        size = self._flistxattr(fd, None, 0, _XATTR_OPTIONS)
        if size < 0:
            value = ctypes.get_errno()
            if value == errno.ERANGE:
                raise _RetrySnapshot()
            raise BackendError(
                "xattr_unreadable",
                f"flistxattr size: [Errno {value}] {os.strerror(value)}",
                value,
            )
        if size == 0:
            return ()
        buffer = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        actual = self._flistxattr(fd, buffer, size, _XATTR_OPTIONS)
        if actual < 0:
            value = ctypes.get_errno()
            if value in (errno.ERANGE, getattr(errno, "ENOATTR", 93)):
                raise _RetrySnapshot()
            raise BackendError(
                "xattr_unreadable",
                f"flistxattr data: [Errno {value}] {os.strerror(value)}",
                value,
            )
        if actual != size:
            raise _RetrySnapshot()
        raw = bytes(buffer.raw[:actual])
        if not raw.endswith(b"\x00"):
            raise BackendError("xattr_invalid", "xattr name list lacks final NUL")
        names = tuple(raw[:-1].split(b"\x00"))
        if any(not name or b"\x00" in name for name in names):
            raise BackendError("xattr_invalid", "xattr name list is malformed")
        if len(set(names)) != len(names):
            raise BackendError("xattr_invalid", "xattr name list contains duplicates")
        return tuple(sorted(names))

    def _get_xattr_once(self, fd: int, name: bytes) -> bytes:
        ctypes.set_errno(0)
        size = self._fgetxattr(fd, name, None, 0, 0, _XATTR_OPTIONS)
        if size < 0:
            value = ctypes.get_errno()
            if value in (errno.ERANGE, getattr(errno, "ENOATTR", 93)):
                raise _RetrySnapshot()
            raise BackendError(
                "xattr_unreadable",
                f"fgetxattr {name!r}: [Errno {value}] {os.strerror(value)}",
                value,
            )
        if size == 0:
            buffer = ctypes.c_void_p()
        else:
            storage = ctypes.create_string_buffer(size)
            buffer = ctypes.cast(storage, ctypes.c_void_p)
        ctypes.set_errno(0)
        actual = self._fgetxattr(fd, name, buffer, size, 0, _XATTR_OPTIONS)
        if actual < 0:
            value = ctypes.get_errno()
            if value in (errno.ERANGE, getattr(errno, "ENOATTR", 93)):
                raise _RetrySnapshot()
            raise BackendError(
                "xattr_unreadable",
                f"fgetxattr {name!r}: [Errno {value}] {os.strerror(value)}",
                value,
            )
        if actual != size:
            raise _RetrySnapshot()
        if size == 0:
            return b""
        return bytes(storage.raw[:actual])

    def _read_xattrs_once(self, fd: int) -> Tuple[Tuple[bytes, bytes], ...]:
        names = self._list_xattr_names_once(fd)
        values = tuple((name, self._get_xattr_once(fd, name)) for name in names)
        if self._list_xattr_names_once(fd) != names:
            raise _RetrySnapshot()
        return values

    def _snapshot_xattrs(self, fd: int) -> Tuple[Tuple[bytes, bytes], ...]:
        for _attempt in range(_POLICY_SNAPSHOT_ATTEMPTS):
            try:
                first = self._read_xattrs_once(fd)
                second = self._read_xattrs_once(fd)
            except _RetrySnapshot:
                continue
            if first == second:
                return first
        raise BackendError("xattr_unstable", f"xattrs changed while reading fd {fd}")

    def _get_acl_core_owned(self, fd: int) -> _OwnedACL:
        unacquired = object()

        def acquire(target: _OwnedACL) -> None:
            pointer: object = unacquired
            try:
                ctypes.set_errno(0)
                pointer = self._acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
                if not pointer:
                    value = ctypes.get_errno()
                    if value == errno.ENOENT:
                        target._adopt(None)
                        return
                    raise BackendError(
                        "acl_unreadable",
                        f"acl_get_fd_np: [Errno {value}] {os.strerror(value)}",
                        value,
                    )
                target._adopt(ctypes.c_void_p(pointer))
            except BaseException as primary:
                if pointer is not unacquired and target.closed:
                    try:
                        target._adopt(None if not pointer else ctypes.c_void_p(pointer))
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedACL(self, f"ACL for fd {fd}", acquire)

    def _get_acl_owned(self, fd: int) -> _OwnedACL:
        bound = self._get_acl
        if getattr(bound, "__func__", None) is DarwinBackend._get_acl:
            return self._get_acl_core_owned(fd)

        unacquired = object()

        def acquire(target: _OwnedACL) -> None:
            handed_off: object = unacquired
            try:
                handed_off = bound(fd)
                target._adopt(handed_off)
            except BaseException as primary:
                if handed_off is not unacquired and target.closed:
                    try:
                        target._adopt(handed_off)
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedACL(self, f"overridden ACL for fd {fd}", acquire)

    def _get_acl(self, fd: int) -> Optional[ctypes.c_void_p]:
        owner = self._get_acl_core_owned(fd)
        return self._handoff_acl_owner(owner)

    def _empty_acl_owned(self) -> _OwnedACL:
        unacquired = object()

        def acquire(target: _OwnedACL) -> None:
            pointer: object = unacquired
            try:
                ctypes.set_errno(0)
                pointer = self._acl_init(0)
                if not pointer:
                    self._raise_errno("acl_unreadable", "acl_init")
                target._adopt(ctypes.c_void_p(pointer))
            except BaseException as primary:
                if pointer is not unacquired and pointer and target.closed:
                    try:
                        target._adopt(ctypes.c_void_p(pointer))
                    except BaseException as cleanup:
                        self._attach_cleanup_diagnostic(primary, cleanup)
                raise

        return _OwnedACL(self, "empty ACL", acquire)

    def _empty_acl(self) -> ctypes.c_void_p:
        owner = self._empty_acl_owned()
        return self._handoff_acl_owner(owner)

    def _free_acl(self, acl: Optional[ctypes.c_void_p]) -> None:
        if acl is None:
            return
        ctypes.set_errno(0)
        if self._acl_free(acl) != 0:
            self._raise_errno("acl_free_failed", "acl_free")

    def _free_acls(
        self,
        descriptors: Tuple[Tuple[str, Optional[ctypes.c_void_p]], ...],
        *,
        primary_error: Optional[BaseException] = None,
    ) -> None:
        """Drain every owned ACL pointer without masking a primary exception."""
        errors = []
        for subject, acl in descriptors:
            if acl is None:
                continue
            try:
                self._free_acl(acl)
            except BaseException as exc:
                errors.append(f"{subject}: {self._exception_diagnostic(exc)}")
        if not errors:
            return
        cleanup_failure = BackendError("acl_free_failed", "; ".join(errors))
        if primary_error is not None:
            self._attach_cleanup_diagnostic(primary_error, cleanup_failure)
            return
        raise cleanup_failure

    def _acl_external(self, acl: ctypes.c_void_p) -> bytes:
        ctypes.set_errno(0)
        size = self._acl_size(acl)
        if size < 0:
            self._raise_errno("acl_unreadable", "acl_size")
        buffer = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        actual = self._acl_copy_ext_native(buffer, acl, size)
        if actual < 0:
            self._raise_errno("acl_unreadable", "acl_copy_ext_native")
        if actual != size:
            raise BackendError(
                "acl_unstable", f"ACL external size changed from {size} to {actual}"
            )
        return bytes(buffer.raw[:actual])

    def _acl_bytes_once(self, fd: int) -> bytes:
        owner = self._get_acl_owned(fd)
        with owner:
            acl = owner.pointer()
            if acl is None:
                owner.close()
                return b""
            result = self._acl_external(acl)
            owner.close()
            return result

    def _snapshot_acl(self, fd: int) -> bytes:
        for _attempt in range(_POLICY_SNAPSHOT_ATTEMPTS):
            first = self._acl_bytes_once(fd)
            second = self._acl_bytes_once(fd)
            if first == second:
                return first
        raise BackendError("acl_unstable", f"ACL changed while reading fd {fd}")

    def _acl_has_entries(self, fd: int) -> bool:
        owner = self._get_acl_owned(fd)
        with owner:
            acl = owner.pointer()
            if acl is None:
                owner.close()
                return False
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = self._acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry))
            if result == 0:
                if not entry.value:
                    raise BackendError(
                        "acl_unreadable",
                        "acl_get_entry succeeded without returning an entry",
                    )
                owner.close()
                return True
            if result == -1:
                # Darwin uses -1/EINVAL both for an exhausted ACL iterator and
                # malformed input.  acl_get_fd_np normally reports no extended
                # ACL as ENOENT before this point; fail closed if it instead
                # returns a non-null ACL whose first entry cannot be read.
                self._raise_errno("acl_unreadable", "acl_get_entry first entry")
            raise BackendError(
                "acl_unreadable",
                f"acl_get_entry returned unexpected status {result}",
            )

    @staticmethod
    def _is_protected_xattr(name: bytes) -> bool:
        return name in _PROTECTED_XATTRS or name.startswith(_PROTECTED_XATTR_PREFIXES)

    def _replace_xattrs(
        self, fd: int, expected: Tuple[Tuple[bytes, bytes], ...]
    ) -> None:
        current = self._snapshot_xattrs(fd)
        current_map = dict(current)
        expected_map = dict(expected)
        changed_names = {
            name
            for name in set(current_map) | set(expected_map)
            if current_map.get(name) != expected_map.get(name)
        }
        if any(self._is_protected_xattr(name) for name in changed_names):
            raise BackendError(
                "unsafe_internal_xattr",
                "clone differs in an internal or data-fork-adjacent xattr",
            )
        for name in sorted(set(current_map) - set(expected_map)):
            ctypes.set_errno(0)
            if self._fremovexattr(fd, name, _XATTR_OPTIONS) != 0:
                self._raise_errno("policy_apply_failed", f"fremovexattr {name!r}")
        for name in sorted(expected_map):
            value = expected_map[name]
            if current_map.get(name) == value:
                continue
            if value:
                storage = ctypes.create_string_buffer(value, len(value))
                pointer = ctypes.cast(storage, ctypes.c_void_p)
            else:
                pointer = ctypes.c_void_p()
            ctypes.set_errno(0)
            if self._fsetxattr(fd, name, pointer, len(value), 0, _XATTR_OPTIONS) != 0:
                self._raise_errno("policy_apply_failed", f"fsetxattr {name!r}")
        if self._snapshot_xattrs(fd) != expected:
            raise BackendError(
                "clone_policy_mismatch", "clone xattrs did not stabilize"
            )

    @staticmethod
    def _preflight_policy_flags(original_flags: int, clone_flags: int) -> None:
        combined = original_flags | clone_flags
        if combined & _BLOCKING_FLAGS:
            raise BackendError(
                "unsafe_bsd_flags", "immutable, append, or nounlink flag is present"
            )
        if combined & _INTERNAL_FLAGS:
            raise BackendError("unsafe_bsd_flags", "internal BSD flag is present")
        if combined & ~_KNOWN_FLAGS:
            raise BackendError("unsafe_bsd_flags", "unknown BSD flag is present")
        if (original_flags ^ clone_flags) & ~_SAFE_SETTABLE_FLAGS:
            raise BackendError("unsafe_bsd_flags", "non-settable BSD flags differ")

    def _set_flags(self, fd: int, current: int, expected: int) -> None:
        self._preflight_policy_flags(expected, current)
        ctypes.set_errno(0)
        if self._fchflags(fd, ctypes.c_uint32(expected)) != 0:
            self._raise_errno("policy_apply_failed", "fchflags clone")
        actual = int(
            getattr(self._fstat(fd, "clone flags revalidation"), "st_flags", 0)
        )
        if actual != expected:
            raise BackendError("clone_policy_mismatch", "clone BSD flags did not match")

    @staticmethod
    def _os_error(reason: str, operation: str, exc: OSError) -> BackendError:
        value = exc.errno
        detail = f"{operation}: {exc}"
        return BackendError(reason, detail, value)

    @staticmethod
    def _close_fds(
        descriptors: Tuple[Tuple[str, int], ...],
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        """Drain owned FDs without reversing a primary or durable mutation."""
        close_errors = []
        for subject, fd in descriptors:
            if fd < 0:
                continue
            try:
                os.close(fd)
            except BaseException as exc:
                close_errors.append(
                    f"{subject} fd {fd}: {DarwinBackend._exception_diagnostic(exc)}"
                )
        if not close_errors:
            return
        close_failure = BackendError("close_failed", "; ".join(close_errors))
        if primary_error is not None:
            DarwinBackend._attach_cleanup_diagnostic(primary_error, close_failure)
            return
        if not durable_namespace_complete:
            raise close_failure

    @staticmethod
    def _close_fd_owner_pass(
        descriptors: Tuple[Tuple[str, Optional[_OwnedFD]], ...],
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> Tuple[str, ...]:
        """Run one whole ordered FD-owner drain pass."""
        close_errors = []
        for subject, owner in descriptors:
            if owner is None:
                continue
            attempts = 0
            while not owner.closed and attempts < 2:
                try:
                    attempts += 1
                    owner.close(
                        primary_error=primary_error,
                        durable_namespace_complete=durable_namespace_complete,
                    )
                except BaseException as exc:
                    close_errors.append(
                        f"{subject}: {DarwinBackend._exception_diagnostic(exc)}"
                    )
        return tuple(close_errors)

    @staticmethod
    def _close_fd_owners(
        descriptors: Tuple[Tuple[str, Optional[_OwnedFD]], ...],
        *,
        primary_error: Optional[BaseException] = None,
        durable_namespace_complete: bool = False,
    ) -> None:
        """Drain every owner under two whole-pass interruption guards."""
        close_errors = []
        first_interruption: Optional[BaseException] = None
        first_traceback: Optional[types.TracebackType] = None
        try:
            _cleanup_dispatch = 1
            close_errors.extend(
                DarwinBackend._close_fd_owner_pass(
                    descriptors,
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            )
        except BaseException as cleanup_interruption:
            first_interruption = cleanup_interruption
            first_traceback = cleanup_interruption.__traceback__
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    primary_error, cleanup_interruption
                )
        try:
            _cleanup_dispatch = 2
            close_errors.extend(
                DarwinBackend._close_fd_owner_pass(
                    descriptors,
                    primary_error=primary_error,
                    durable_namespace_complete=durable_namespace_complete,
                )
            )
        except BaseException as retry_interruption:
            if first_interruption is None:
                first_interruption = retry_interruption
                first_traceback = retry_interruption.__traceback__
            else:
                DarwinBackend._attach_cleanup_diagnostic(
                    first_interruption, retry_interruption
                )
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    primary_error, retry_interruption
                )
        try:
            _cleanup_dispatch = 3
            for subject, owner in descriptors:
                if owner is not None and not owner.closed:
                    close_errors.append(f"{subject}: fd remained open")
        except BaseException as verification_interruption:
            if first_interruption is None:
                first_interruption = verification_interruption
                first_traceback = verification_interruption.__traceback__
            else:
                DarwinBackend._attach_cleanup_diagnostic(
                    first_interruption, verification_interruption
                )
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    primary_error, verification_interruption
                )

        try:
            _cleanup_dispatch = 4
            close_failure: Optional[BackendError] = None
            if close_errors:
                close_failure = BackendError("close_failed", "; ".join(close_errors))
                if primary_error is not None:
                    DarwinBackend._attach_cleanup_diagnostic(
                        primary_error, close_failure
                    )
                elif first_interruption is not None:
                    DarwinBackend._attach_cleanup_diagnostic(
                        first_interruption, close_failure
                    )
            if primary_error is not None:
                return
        except BaseException as finalization_interruption:
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    primary_error, finalization_interruption
                )
                return
            if first_interruption is not None:
                DarwinBackend._attach_cleanup_diagnostic(
                    first_interruption, finalization_interruption
                )
                raise first_interruption.with_traceback(first_traceback)
            raise
        if first_interruption is not None:
            raise first_interruption.with_traceback(first_traceback)
        if close_failure is not None and not durable_namespace_complete:
            raise close_failure

    @staticmethod
    def _close_acl_owners(
        descriptors: Tuple[Tuple[str, Optional[_OwnedACL]], ...],
        *,
        primary_error: Optional[BaseException] = None,
    ) -> None:
        """Drain every ACL owner twice while retaining the first primary."""
        close_errors = []
        first_failure: Optional[BaseException] = None
        for subject, owner in descriptors:
            if owner is None:
                continue
            attempts = 0
            while not owner.closed and attempts < 2:
                try:
                    attempts += 1
                    owner.close(primary_error=primary_error)
                except BaseException as exc:
                    if first_failure is None:
                        first_failure = exc
                    close_errors.append(
                        f"{subject}: {DarwinBackend._exception_diagnostic(exc)}"
                    )
            if not owner.closed:
                close_errors.append(f"{subject}: ACL remained active")
        if close_errors:
            close_failure = BackendError("acl_free_failed", "; ".join(close_errors))
            if primary_error is not None:
                DarwinBackend._attach_cleanup_diagnostic(primary_error, close_failure)
                return
        if first_failure is not None and primary_error is None:
            raise first_failure
        if close_errors:
            raise BackendError("acl_free_failed", "; ".join(close_errors))

    @staticmethod
    def _raise_errno(reason: str, operation: str) -> None:
        value = ctypes.get_errno()
        raise BackendError(
            reason, f"{operation}: [Errno {value}] {os.strerror(value)}", value
        )

    def _bind_libc(self) -> None:
        try:
            self._fclonefileat = self._libc.fclonefileat
            self._fcopyfile = self._libc.fcopyfile
            self._renameatx_np = self._libc.renameatx_np
            self._acl_get_fd_np = self._libc.acl_get_fd_np
            self._acl_set_fd_np = self._libc.acl_set_fd_np
            self._acl_init = self._libc.acl_init
            self._acl_size = self._libc.acl_size
            self._acl_copy_ext_native = self._libc.acl_copy_ext_native
            self._acl_free = self._libc.acl_free
            self._acl_get_entry = self._libc.acl_get_entry
            self._flistxattr = self._libc.flistxattr
            self._fgetxattr = self._libc.fgetxattr
            self._fsetxattr = self._libc.fsetxattr
            self._fremovexattr = self._libc.fremovexattr
            self._fchflags = self._libc.fchflags
            self._unlinkat = self._libc.unlinkat
        except AttributeError as exc:
            raise BackendError(
                "unsupported_platform", f"required Darwin libc symbol is missing: {exc}"
            )

        self._fclonefileat.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._fclonefileat.restype = ctypes.c_int
        self._fcopyfile.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._fcopyfile.restype = ctypes.c_int
        self._renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        self._renameatx_np.restype = ctypes.c_int
        self._acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        self._acl_get_fd_np.restype = ctypes.c_void_p
        self._acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self._acl_set_fd_np.restype = ctypes.c_int
        self._acl_init.argtypes = [ctypes.c_int]
        self._acl_init.restype = ctypes.c_void_p
        self._acl_size.argtypes = [ctypes.c_void_p]
        self._acl_size.restype = ctypes.c_ssize_t
        self._acl_copy_ext_native.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ssize_t,
        ]
        self._acl_copy_ext_native.restype = ctypes.c_ssize_t
        self._acl_free.argtypes = [ctypes.c_void_p]
        self._acl_free.restype = ctypes.c_int
        self._acl_get_entry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._acl_get_entry.restype = ctypes.c_int
        self._flistxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        self._flistxattr.restype = ctypes.c_ssize_t
        self._fgetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self._fgetxattr.restype = ctypes.c_ssize_t
        self._fsetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self._fsetxattr.restype = ctypes.c_int
        self._fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self._fremovexattr.restype = ctypes.c_int
        self._fchflags.argtypes = [ctypes.c_int, ctypes.c_uint32]
        self._fchflags.restype = ctypes.c_int
        self._unlinkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self._unlinkat.restype = ctypes.c_int


class _OwnedFDSlot:
    """Expose one transaction-owned descriptor through the legacy int API."""

    def __init__(self, owner_attribute: str, subject: str) -> None:
        self._owner_attribute = owner_attribute
        self._subject = subject

    def __get__(self, instance, owner):
        if instance is None:
            return self
        owned = getattr(instance, self._owner_attribute, None)
        if owned is None or owned.closed:
            return -1
        return owned.fileno()

    def __set__(self, instance, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < -1:
            raise BackendError("invalid_fd", f"{self._subject} has an invalid fd")
        current = getattr(instance, self._owner_attribute, None)
        if value == -1:
            if current is None:
                setattr(
                    instance,
                    self._owner_attribute,
                    _OwnedFD(instance.backend, self._subject),
                )
            else:
                durable_namespace_complete = getattr(
                    instance, "_namespace_lifecycle_complete", False
                )
                try:
                    _cleanup_dispatch = 1
                    current._drain(
                        primary_error=None,
                        durable_namespace_complete=durable_namespace_complete,
                    )
                except BaseException:
                    try:
                        _cleanup_dispatch = 2
                        current._drain(
                            primary_error=None,
                            durable_namespace_complete=durable_namespace_complete,
                        )
                    finally:
                        raise
            return
        if current is not None and not current.closed:
            if current.fileno() == value:
                return
            raise BackendError("fd_already_owned", f"{self._subject} is already open")
        for owner_attribute in instance._FD_OWNER_CLOSE_ORDER:
            existing = getattr(instance, owner_attribute, None)
            if (
                owner_attribute != self._owner_attribute
                and isinstance(existing, _OwnedFD)
                and not existing.closed
                and existing.fileno() == value
            ):
                raise BackendError(
                    "fd_alias_rejected",
                    f"{self._subject} aliases the owned fd in {owner_attribute}",
                )
        pending_fd = [value]
        replacement: Optional[_OwnedFD] = None
        try:
            _adoption_guard = 1
            replacement = instance.backend._adopt_fd(
                value, self._subject, pending_fd=pending_fd
            )
            with replacement:
                replacement.retain_if_registered(
                    lambda: getattr(instance, self._owner_attribute, None)
                    is replacement
                )
                setattr(instance, self._owner_attribute, replacement)
        except BaseException as primary:
            if (
                replacement is None
                or getattr(instance, self._owner_attribute, None) is not replacement
            ):
                try:
                    _cleanup_dispatch = 1
                    if replacement is not None and not replacement.closed:
                        replacement._drain(primary_error=primary)
                    else:
                        instance.backend._drain_pending_fd(
                            pending_fd, self._subject, primary_error=primary
                        )
                except BaseException as cleanup_failure:
                    instance.backend._attach_cleanup_diagnostic(
                        primary, cleanup_failure
                    )
                    try:
                        _cleanup_dispatch = 2
                        if replacement is not None and not replacement.closed:
                            replacement._drain(primary_error=primary)
                        else:
                            instance.backend._drain_pending_fd(
                                pending_fd, self._subject, primary_error=primary
                            )
                    except BaseException as retry_failure:
                        instance.backend._attach_cleanup_diagnostic(
                            primary, retry_failure
                        )
            raise


class BoundTransaction:
    """Held-FD namespace binding for one source/original/clone transaction."""

    source_parent_fd = _OwnedFDSlot("_source_parent_owner", "source_parent_fd")
    destination_parent_fd = _OwnedFDSlot(
        "_destination_parent_owner", "destination_parent_fd"
    )
    temporary_parent_fd = _OwnedFDSlot("_temporary_parent_owner", "temporary_parent_fd")
    source_fd = _OwnedFDSlot("_source_owner", "source_fd")
    original_fd = _OwnedFDSlot("_original_owner", "original_fd")
    clone_fd = _OwnedFDSlot("_clone_owner", "clone_fd")

    _FD_OWNER_CLOSE_ORDER = (
        "_clone_owner",
        "_original_owner",
        "_source_owner",
        "_temporary_parent_owner",
        "_destination_parent_owner",
        "_source_parent_owner",
    )

    def __init__(
        self,
        backend: DarwinBackend,
        source_path: str,
        destination_path: str,
        temporary_path: str,
        *,
        source_parent_expected: FileIdentity,
    ) -> None:
        self._initialize(backend, source_path, destination_path, temporary_path)
        self._bind(source_parent_expected)

    def _initialize(
        self,
        backend: DarwinBackend,
        source_path: Optional[str],
        destination_path: str,
        temporary_path: str,
    ) -> None:
        self.backend = backend
        self.source_path = source_path
        self.destination_path = destination_path
        self.temporary_path = temporary_path
        self._source_parent_owner = _OwnedFD(backend, "source_parent_fd")
        self._destination_parent_owner = _OwnedFD(backend, "destination_parent_fd")
        self._temporary_parent_owner = _OwnedFD(backend, "temporary_parent_fd")
        self._source_owner = _OwnedFD(backend, "source_fd")
        self._original_owner = _OwnedFD(backend, "original_fd")
        self._clone_owner = _OwnedFD(backend, "clone_fd")
        self.source_name = ""
        self.destination_name = ""
        self.temporary_name = ""
        self.source_parent_identity: Optional[FileIdentity] = None
        self.destination_parent_identity: Optional[FileIdentity] = None
        self.temporary_parent_identity: Optional[FileIdentity] = None
        self.source_identity: Optional[FileIdentity] = None
        self.original_identity: Optional[FileIdentity] = None
        self.clone_identity: Optional[FileIdentity] = None
        self._forward = False
        self._original_unlinked = False
        self._clone_unlinked = False
        self._stage_removed = False
        self._namespace_lifecycle_complete = False
        self._pre_forward_expectations: Optional[
            Tuple[SnapshotExpectation, SnapshotExpectation, SnapshotExpectation]
        ] = None
        self._closed = False

    @classmethod
    def recover(
        cls,
        backend: DarwinBackend,
        source_path: Optional[str],
        destination_path: str,
        temporary_path: str,
        original_expected: FileIdentity,
        clone_expected: FileIdentity,
        *,
        source_expected: Optional[FileIdentity] = None,
        source_parent_expected: Optional[FileIdentity] = None,
        destination_parent_expected: Optional[FileIdentity] = None,
        temporary_parent_expected: Optional[FileIdentity] = None,
    ) -> "BoundTransaction":
        transaction = cls.__new__(cls)
        transaction._initialize(backend, source_path, destination_path, temporary_path)
        try:
            transaction._bind_recovery(
                original_expected,
                clone_expected,
                source_expected=source_expected,
                source_parent_expected=source_parent_expected,
                destination_parent_expected=destination_parent_expected,
                temporary_parent_expected=temporary_parent_expected,
            )
            return transaction
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                transaction._drain_after_error(primary)
            except BaseException as cleanup:
                backend._attach_cleanup_diagnostic(primary, cleanup)
                try:
                    _cleanup_dispatch = 2
                    transaction._drain_after_error(primary)
                except BaseException as retry_failure:
                    backend._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _install_fd_owner(self, owner_attribute: str, owner: _OwnedFD) -> None:
        """Transfer one acquired owner into an empty transaction slot."""
        if not isinstance(owner, _OwnedFD) or owner.closed:
            raise BackendError(
                "invalid_fd_owner", f"{owner_attribute} source is not open"
            )
        current = getattr(self, owner_attribute)
        if not isinstance(current, _OwnedFD) or not current.closed:
            raise BackendError("fd_already_owned", f"{owner_attribute} is already open")
        owner.neutralize_on_exception_if(
            lambda: self._fd_alias_attribute(owner_attribute, owner) is not None
        )
        alias_attribute = self._fd_alias_attribute(owner_attribute, owner)
        if alias_attribute is not None:
            # fmt: off
            owner.clear_exception_neutralizer(); owner.disarm()  # noqa: E702
            # fmt: on
            raise BackendError(
                "fd_alias_rejected",
                f"{owner_attribute} aliases the owned fd in {alias_attribute}",
            )
        owner.clear_exception_neutralizer()
        owner.retain_if_registered(
            lambda: getattr(self, owner_attribute, None) is owner
        )
        setattr(self, owner_attribute, owner)

    def _fd_alias_attribute(
        self, owner_attribute: str, owner: _OwnedFD
    ) -> Optional[str]:
        owner_fd = owner.fileno()
        for existing_attribute in self._FD_OWNER_CLOSE_ORDER:
            existing = getattr(self, existing_attribute)
            if (
                existing_attribute != owner_attribute
                and not existing.closed
                and existing.fileno() == owner_fd
            ):
                return existing_attribute
        return None

    def _dispatch_absolute_parent_owned(
        self, path: str, subject: str
    ) -> Tuple[_OwnedFD, str]:
        del subject
        return self.backend._open_absolute_parent_owned(path)

    @staticmethod
    def _raise_source_parent_error(exc: BackendError) -> None:
        if exc.reason == "invalid_path":
            raise exc
        reason = (
            "source_parent_replaced"
            if exc.errno_value in _PATH_REPLACEMENT_ERRNOS
            else "source_parent_unreadable"
        )
        raise BackendError(
            reason,
            f"source parent could not be opened: {exc.detail}",
            exc.errno_value,
        ) from exc

    def _open_source_parent_owned(self) -> Tuple[_OwnedFD, str]:
        if self.source_path is None:
            raise BackendError("invalid_path", "source path is unavailable")
        try:
            return self._dispatch_absolute_parent_owned(
                self.source_path, "source_parent_fd"
            )
        except BackendError as exc:
            self._raise_source_parent_error(exc)

    def _dispatch_source_parent_owned(self) -> Tuple[_OwnedFD, str]:
        bound = self._open_source_parent
        if getattr(bound, "__func__", None) is BoundTransaction._open_source_parent:
            return self._open_source_parent_owned()
        if self.source_path is None:
            raise BackendError("invalid_path", "source path is unavailable")
        _unused, expected_name = self.backend._open_absolute_parent_owned(
            self.source_path
        )
        owner = self.backend._returned_fd_owner("source_parent_fd", lambda: bound()[0])
        return owner, expected_name

    def _install_source_parent_owner(self, owner: _OwnedFD) -> None:
        try:
            with owner:
                self._install_fd_owner("_source_parent_owner", owner)
        except BackendError as exc:
            self._raise_source_parent_error(exc)

    def _open_source_leaf_owned(self) -> _OwnedFD:
        return self.backend._dispatch_open_leaf_owned(
            self.source_parent_fd, self.source_name
        )

    def _dispatch_source_leaf_owned(self) -> _OwnedFD:
        bound = self._open_source_leaf
        if getattr(bound, "__func__", None) is BoundTransaction._open_source_leaf:
            return self._open_source_leaf_owned()
        return self.backend._returned_fd_owner("source_fd", bound)

    def _install_source_owner(self, owner: _OwnedFD) -> None:
        try:
            with owner:
                self._install_fd_owner("_source_owner", owner)
        except BackendError as exc:
            if (
                exc.reason == "not_regular"
                or exc.errno_value in _PATH_REPLACEMENT_ERRNOS
            ):
                raise BackendError(
                    "source_object_replaced",
                    f"source leaf changed after inspection: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise BackendError(
                "source_revalidation_failed",
                f"source leaf could not be opened after inspection: {exc.detail}",
                exc.errno_value,
            ) from exc

    def _open_bound_leaf_owned(
        self, parent_fd: int, name: str, subject: str
    ) -> _OwnedFD:
        return self.backend._dispatch_open_leaf_owned(parent_fd, name)

    def _dispatch_bound_leaf_owned(
        self, parent_fd: int, name: str, subject: str
    ) -> _OwnedFD:
        bound = self._open_bound_leaf
        if getattr(bound, "__func__", None) is BoundTransaction._open_bound_leaf:
            return self._open_bound_leaf_owned(parent_fd, name, subject)
        return self.backend._returned_fd_owner(
            f"{subject}_fd", lambda: bound(parent_fd, name, subject)
        )

    def _install_bound_owner(
        self, owner_attribute: str, owner: _OwnedFD, subject: str
    ) -> None:
        try:
            with owner:
                self._install_fd_owner(owner_attribute, owner)
        except BackendError as exc:
            if exc.reason == "not_regular":
                raise BackendError(
                    "object_replaced",
                    f"{subject} leaf changed to a non-regular object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise

    def _bind(self, source_parent_expected: FileIdentity) -> None:
        if not isinstance(source_parent_expected, FileIdentity):
            raise BackendError(
                "invalid_source_parent_evidence",
                "expected source parent identity has the wrong type",
            )
        try:
            source_parent_owner, self.source_name = self._dispatch_source_parent_owned()
            self._install_source_parent_owner(source_parent_owner)
            destination_parent_owner, self.destination_name = (
                self._dispatch_absolute_parent_owned(
                    self.destination_path, "destination_parent_fd"
                )
            )
            with destination_parent_owner:
                self._install_fd_owner(
                    "_destination_parent_owner", destination_parent_owner
                )
            temporary_parent_owner, self.temporary_name = (
                self._dispatch_absolute_parent_owned(
                    self.temporary_path, "temporary_parent_fd"
                )
            )
            with temporary_parent_owner:
                self._install_fd_owner(
                    "_temporary_parent_owner", temporary_parent_owner
                )
            self.source_parent_identity = self._source_parent_identity()
            if not self.source_parent_identity.is_same_object(source_parent_expected):
                raise BackendError(
                    "source_parent_replaced",
                    "source parent differs from the inspected identity",
                )
            self.destination_parent_identity = self.backend.validate_stage_container(
                self.destination_parent_fd
            )
            self.temporary_parent_identity = self.backend.validate_private_stage_parent(
                self.temporary_parent_fd
            )
            if (
                self.destination_parent_identity.dev
                != self.temporary_parent_identity.dev
            ):
                raise BackendError(
                    "cross_device_stage",
                    "destination and private stage directory are on different devices",
                    errno.EXDEV,
                )
            source_owner = self._dispatch_source_leaf_owned()
            self._install_source_owner(source_owner)
            original_owner = self._dispatch_bound_leaf_owned(
                self.destination_parent_fd,
                self.destination_name,
                "destination",
            )
            self._install_bound_owner("_original_owner", original_owner, "destination")
            self.source_identity = self.backend.identity(self.source_fd)
            self.original_identity = self.backend.identity(self.original_fd)
            self.backend.require_exclusive_writer(
                self.original_fd, "bound original mirror"
            )
            if self.source_identity.is_same_object(self.original_identity):
                raise BackendError(
                    "invalid_pair", "source and destination are the same object"
                )
            self._require_source_mapping()
            self._require_bound_mapping(
                self.destination_parent_fd,
                self.destination_name,
                self.original_identity,
                "destination",
            )
            self.backend._require_name_absent(
                self.temporary_parent_fd, self.temporary_name
            )
            self._require_parent_bindings(include_source=True)
        except BaseException as primary:
            try:
                _cleanup_dispatch = 1
                self._drain_after_error(primary)
            except BaseException as cleanup:
                self.backend._attach_cleanup_diagnostic(primary, cleanup)
                try:
                    _cleanup_dispatch = 2
                    self._drain_after_error(primary)
                except BaseException as retry_failure:
                    self.backend._attach_cleanup_diagnostic(primary, retry_failure)
            raise

    def _install_recovery_leaf_owners(
        self,
        destination_owner: _OwnedFD,
        temporary_owner: Optional[_OwnedFD],
        original_expected: FileIdentity,
        clone_expected: FileIdentity,
    ) -> None:
        destination_identity = self.backend.identity(destination_owner.fileno())
        temporary_identity = (
            self.backend.identity(temporary_owner.fileno())
            if temporary_owner is not None
            else None
        )
        if (
            destination_identity.is_same_object(original_expected)
            and temporary_identity is not None
            and temporary_identity.is_same_object(clone_expected)
        ):
            self._install_fd_owner("_original_owner", destination_owner)
            self._install_fd_owner("_clone_owner", temporary_owner)
            self._forward = False
        elif (
            destination_identity.is_same_object(clone_expected)
            and temporary_identity is not None
            and temporary_identity.is_same_object(original_expected)
        ):
            self._install_fd_owner("_clone_owner", destination_owner)
            self._install_fd_owner("_original_owner", temporary_owner)
            self._forward = True
        elif (
            destination_identity.is_same_object(clone_expected)
            and temporary_identity is None
        ):
            self._install_fd_owner("_clone_owner", destination_owner)
            self._forward = True
            self._original_unlinked = True
        elif (
            destination_identity.is_same_object(original_expected)
            and temporary_identity is None
        ):
            self._install_fd_owner("_original_owner", destination_owner)
            self._forward = False
            self._clone_unlinked = True
        else:
            raise BackendError(
                "orientation_unknown",
                "recovery names do not match the recorded original/clone identities",
            )

    def _bind_recovery(
        self,
        original_expected: FileIdentity,
        clone_expected: FileIdentity,
        *,
        source_expected: Optional[FileIdentity],
        source_parent_expected: Optional[FileIdentity],
        destination_parent_expected: Optional[FileIdentity],
        temporary_parent_expected: Optional[FileIdentity],
    ) -> None:
        if original_expected.is_same_object(clone_expected):
            raise BackendError(
                "invalid_recovery_manifest", "original and clone identities are equal"
            )
        if source_parent_expected is not None and not isinstance(
            source_parent_expected, FileIdentity
        ):
            raise BackendError(
                "invalid_recovery_manifest",
                "expected source parent identity has the wrong type",
            )
        if self.source_path is not None:
            if source_parent_expected is None:
                raise BackendError(
                    "invalid_recovery_manifest",
                    "a bound source requires its durable parent identity",
                )
            source_parent_owner, self.source_name = self._dispatch_source_parent_owned()
            self._install_source_parent_owner(source_parent_owner)
            self.source_parent_identity = self._source_parent_identity()
            if (
                source_parent_expected is not None
                and not self.source_parent_identity.is_same_object(
                    source_parent_expected
                )
            ):
                raise BackendError(
                    "source_parent_replaced",
                    "source parent differs from recovery manifest",
                )
            source_owner = self._dispatch_source_leaf_owned()
            self._install_source_owner(source_owner)
            self.source_identity = self.backend.identity(self.source_fd)
            if source_expected is not None and not self.source_identity.is_same_object(
                source_expected
            ):
                raise BackendError(
                    "source_object_replaced",
                    "source differs from recovery manifest",
                )
            self._require_source_mapping()
        elif source_expected is not None or source_parent_expected is not None:
            raise BackendError(
                "invalid_recovery_manifest",
                "source identities were supplied without a source path",
            )

        destination_parent_owner, self.destination_name = (
            self._dispatch_absolute_parent_owned(
                self.destination_path, "destination_parent_fd"
            )
        )
        with destination_parent_owner:
            self._install_fd_owner(
                "_destination_parent_owner", destination_parent_owner
            )
        self.destination_parent_identity = self.backend.validate_stage_container(
            self.destination_parent_fd
        )
        if (
            destination_parent_expected is not None
            and not self.destination_parent_identity.is_same_object(
                destination_parent_expected
            )
        ):
            raise BackendError(
                "parent_replaced", "destination parent differs from recovery manifest"
            )

        self.temporary_name = os.path.basename(self.temporary_path)
        self.backend._leaf_bytes(self.temporary_name)
        try:
            temporary_parent_owner, opened_name = self._dispatch_absolute_parent_owned(
                self.temporary_path, "temporary_parent_fd"
            )
            with temporary_parent_owner:
                if opened_name != self.temporary_name:
                    raise BackendError(
                        "invalid_path", "temporary path leaf changed during parsing"
                    )
                self._install_fd_owner(
                    "_temporary_parent_owner", temporary_parent_owner
                )
        except BackendError as exc:
            if exc.errno_value != errno.ENOENT:
                raise
            if temporary_parent_expected is None:
                raise BackendError(
                    "invalid_recovery_manifest",
                    "missing stage directory requires its recorded identity",
                )
            self._require_stage_parent_absent()
            self.temporary_parent_identity = temporary_parent_expected
            self._stage_removed = True
        else:
            self.temporary_parent_identity = self.backend.validate_private_stage_parent(
                self.temporary_parent_fd
            )
            if (
                temporary_parent_expected is not None
                and not self.temporary_parent_identity.is_same_object(
                    temporary_parent_expected
                )
            ):
                raise BackendError(
                    "parent_replaced", "temporary parent differs from recovery manifest"
                )
            if (
                self.destination_parent_identity.dev
                != self.temporary_parent_identity.dev
            ):
                raise BackendError(
                    "cross_device_stage",
                    "destination and private stage directory are on different devices",
                    errno.EXDEV,
                )

        destination_owner = self._dispatch_bound_leaf_owned(
            self.destination_parent_fd,
            self.destination_name,
            "destination",
        )
        destination_entered = False
        try:
            with destination_owner:
                destination_entered = True
                if self.temporary_parent_fd >= 0:
                    temporary_owner = self._dispatch_bound_leaf_owned(
                        self.temporary_parent_fd,
                        self.temporary_name,
                        "temporary",
                    )
                    temporary_entered = False
                    try:
                        with temporary_owner:
                            temporary_entered = True
                            self._install_recovery_leaf_owners(
                                destination_owner,
                                temporary_owner,
                                original_expected,
                                clone_expected,
                            )
                    except BackendError as exc:
                        if temporary_entered:
                            raise
                        if exc.reason == "not_regular":
                            raise BackendError(
                                "object_replaced",
                                "temporary leaf changed to a non-regular object: "
                                f"{exc.detail}",
                                exc.errno_value,
                            ) from exc
                        if exc.errno_value != errno.ENOENT:
                            raise
                        self._install_recovery_leaf_owners(
                            destination_owner,
                            None,
                            original_expected,
                            clone_expected,
                        )
                else:
                    self._install_recovery_leaf_owners(
                        destination_owner,
                        None,
                        original_expected,
                        clone_expected,
                    )
        except BackendError as exc:
            if not destination_entered and exc.reason == "not_regular":
                raise BackendError(
                    "object_replaced",
                    f"destination leaf changed to a non-regular object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise
        self.original_identity = original_expected
        self.clone_identity = clone_expected
        if self.original_fd >= 0:
            self.backend.require_exclusive_writer(self.original_fd, "recovery original")
        if self.clone_fd >= 0:
            self.backend.require_exclusive_writer(self.clone_fd, "recovery clone")
        self._require_parent_bindings(include_source=self.source_parent_fd >= 0)
        if self.orientation() not in ("before", "forward", "committed", "rolled_back"):
            raise BackendError(
                "orientation_unknown", "recovery mapping could not be revalidated"
            )

    def _open_source_leaf(self) -> int:
        try:
            return self.backend.open_leaf(self.source_parent_fd, self.source_name)
        except BackendError as exc:
            if (
                exc.reason == "not_regular"
                or exc.errno_value in _PATH_REPLACEMENT_ERRNOS
            ):
                raise BackendError(
                    "source_object_replaced",
                    f"source leaf changed after inspection: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise BackendError(
                "source_revalidation_failed",
                f"source leaf could not be opened after inspection: {exc.detail}",
                exc.errno_value,
            ) from exc

    def _open_source_parent(self) -> Tuple[int, str]:
        try:
            return self.backend.open_absolute_parent(self.source_path)
        except BackendError as exc:
            if exc.reason == "invalid_path":
                raise
            reason = (
                "source_parent_replaced"
                if exc.errno_value in _PATH_REPLACEMENT_ERRNOS
                else "source_parent_unreadable"
            )
            raise BackendError(
                reason,
                f"source parent could not be opened: {exc.detail}",
                exc.errno_value,
            ) from exc

    def _source_parent_identity(self) -> FileIdentity:
        try:
            return self.backend.identity(self.source_parent_fd)
        except BackendError as exc:
            raise BackendError(
                "source_parent_unreadable",
                f"source parent identity could not be read: {exc.detail}",
                exc.errno_value,
            ) from exc

    def _open_bound_leaf(self, parent_fd: int, name: str, subject: str) -> int:
        try:
            return self.backend.open_leaf(parent_fd, name)
        except BackendError as exc:
            if exc.reason == "not_regular":
                raise BackendError(
                    "object_replaced",
                    f"{subject} leaf changed to a non-regular object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise

    def _require_bound_mapping(
        self,
        parent_fd: int,
        name: str,
        expected: FileIdentity,
        subject: str,
    ) -> FileIdentity:
        try:
            return self.backend.require_identity_at(parent_fd, name, expected)
        except BackendError as exc:
            if exc.reason in {"identity_mismatch", "not_regular"} or (
                exc.errno_value in _PATH_REPLACEMENT_ERRNOS
            ):
                raise BackendError(
                    "object_replaced",
                    f"{subject} name no longer maps to its held object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise

    def clone(self, *, authorize_state: Callable[[str], None]) -> FileIdentity:
        self._require_open()
        if self.clone_fd >= 0 or self.clone_identity is not None:
            raise BackendError("clone_exists", "this transaction already has a clone")
        self._require_parent_bindings(include_source=True)
        self._require_source_mapping()
        self._require_bound_mapping(
            self.destination_parent_fd,
            self.destination_name,
            self._original_identity(),
            "destination",
        )
        self.backend._require_name_absent(self.temporary_parent_fd, self.temporary_name)
        clone_owner = self.backend._strict_clone_owned(
            self.source_fd,
            self.temporary_parent_fd,
            self.temporary_name,
            authorize_state=authorize_state,
        )
        with clone_owner:
            self._install_fd_owner("_clone_owner", clone_owner)
        self.clone_identity = self.backend.identity(self.clone_fd)
        self._require_bound_mapping(
            self.temporary_parent_fd,
            self.temporary_name,
            self.clone_identity,
            "temporary clone",
        )
        self.backend.fsync(self.temporary_parent_fd)
        return self.clone_identity

    def source_snapshot(self) -> FileSnapshot:
        self._require_open()
        if self.source_fd < 0:
            raise BackendError(
                "source_unavailable", "recovery binding has no source path"
            )
        self._require_parent_bindings(include_source=True)
        self._require_source_mapping()
        return self.backend.snapshot_file(self.source_fd)

    def original_snapshot(self) -> FileSnapshot:
        self._require_open()
        self._require_parent_bindings()
        if self._original_unlinked:
            raise BackendError(
                "original_unlinked", "the original name was already removed"
            )
        orientation = self.orientation()
        if orientation not in ("clone_absent", "before", "forward"):
            raise BackendError(
                "orientation_unknown", f"unexpected orientation {orientation}"
            )
        return self.backend.snapshot_file(self.original_fd)

    def clone_snapshot(self) -> FileSnapshot:
        self._require_open()
        self._require_parent_bindings()
        if self.clone_fd < 0:
            raise BackendError("clone_missing", "the clone has not been created")
        if self._clone_unlinked:
            raise BackendError("clone_unlinked", "the clone name was already removed")
        if self.orientation() not in ("before", "forward"):
            raise BackendError(
                "orientation_unknown", "clone name mapping is not verified"
            )
        return self.backend.snapshot_file(self.clone_fd)

    def calibrate_clone_policy(self, expected: FilePolicy) -> FilePolicy:
        self._require_open()
        if self.clone_fd < 0:
            raise BackendError("clone_missing", "the clone has not been created")
        if self.orientation() != "before":
            raise BackendError(
                "invalid_orientation", "clone policy can only be calibrated before swap"
            )
        return self.backend.calibrate_clone_policy(
            self.original_fd, self.clone_fd, expected
        )

    def revalidate_pre_forward(
        self,
        expected_source: SnapshotExpectation,
        expected_original: SnapshotExpectation,
        expected_clone: SnapshotExpectation,
    ) -> None:
        """Fully revalidate the durable BEFORE state and arm one forward swap."""
        self._pre_forward_expectations = None
        self._require_pre_forward_snapshots(
            expected_source, expected_original, expected_clone
        )
        self._pre_forward_expectations = (
            expected_source,
            expected_original,
            expected_clone,
        )

    def _require_pre_forward_snapshots(
        self,
        expected_source: SnapshotExpectation,
        expected_original: SnapshotExpectation,
        expected_clone: SnapshotExpectation,
    ) -> None:
        self._require_open()
        if self.source_fd < 0:
            raise BackendError(
                "source_unavailable",
                "forward swap requires a bound source for full revalidation",
            )
        self._require_parent_bindings(include_source=True)
        self._require_source_mapping()
        if self.orientation() != "before":
            raise BackendError(
                "invalid_orientation",
                "pre-forward revalidation requires destination=original,temp=clone",
            )
        self.backend.require_snapshot(
            self.source_fd,
            expected_source,
            "pre-forward source",
            mismatch_reason="source_snapshot_mismatch",
            changed_reason="source_snapshot_changed",
            unreadable_reason="source_snapshot_unreadable",
        )
        original_snapshot = self.backend.require_snapshot(
            self.original_fd,
            expected_original,
            "pre-forward original",
            mismatch_reason="original_snapshot_mismatch",
            changed_reason="original_snapshot_changed",
            unreadable_reason="original_snapshot_unreadable",
        )
        self.backend.require_exclusive_writer_policy(
            original_snapshot.policy, "pre-forward original"
        )
        clone_snapshot = self.backend.require_snapshot(
            self.clone_fd,
            expected_clone,
            "pre-forward clone",
            mismatch_reason="clone_snapshot_mismatch",
            changed_reason="clone_snapshot_changed",
            unreadable_reason="clone_snapshot_unreadable",
        )
        self.backend.require_exclusive_writer_policy(
            clone_snapshot.policy, "pre-forward clone"
        )
        # Snapshot reads may be long.  Rebind every protected name and parent
        # after them so the following namespace operation cannot use a stale
        # pathname mapping.
        self._require_parent_bindings(include_source=True)
        self._require_source_mapping()
        if self.orientation() != "before":
            raise BackendError(
                "invalid_orientation",
                "BEFORE namespace changed during pre-forward revalidation",
            )

    def revalidate_committed(self, expected_clone: SnapshotExpectation) -> None:
        """Require the one-object COMMITTED state and full final clone snapshot."""
        self._require_open()
        if self.orientation() != "committed":
            raise BackendError(
                "invalid_orientation",
                "committed revalidation requires final=clone,temp=missing",
            )
        clone_snapshot = self.backend.require_snapshot(
            self.clone_fd,
            expected_clone,
            "committed final clone",
            mismatch_reason="clone_snapshot_mismatch",
            changed_reason="clone_snapshot_changed",
            unreadable_reason="clone_snapshot_unreadable",
        )
        self.backend.require_exclusive_writer_policy(
            clone_snapshot.policy, "committed final clone"
        )
        if self.orientation() != "committed":
            raise BackendError(
                "invalid_orientation",
                "COMMITTED namespace changed during full revalidation",
            )

    def revalidate_rolled_back(self, expected_original: SnapshotExpectation) -> None:
        """Require the one-object ROLLED_BACK state and full final original."""
        self._require_open()
        if self.orientation() != "rolled_back":
            raise BackendError(
                "invalid_orientation",
                "rolled-back revalidation requires final=original,temp=missing",
            )
        original_snapshot = self.backend.require_snapshot(
            self.original_fd,
            expected_original,
            "rolled-back final original",
            mismatch_reason="original_snapshot_mismatch",
            changed_reason="original_snapshot_changed",
            unreadable_reason="original_snapshot_unreadable",
        )
        self.backend.require_exclusive_writer_policy(
            original_snapshot.policy, "rolled-back final original"
        )
        if self.orientation() != "rolled_back":
            raise BackendError(
                "invalid_orientation",
                "ROLLED_BACK namespace changed during full revalidation",
            )

    def swap_forward(self, *, authorize_state: Callable[[str], None]) -> None:
        """Consume a prior full pre-forward revalidation and swap atomically."""
        expectations = self._pre_forward_expectations
        if expectations is None:
            raise BackendError(
                "pre_forward_revalidation_required",
                "forward swap requires complete durable snapshot expectations",
            )
        self.swap_forward_verified(*expectations, authorize_state=authorize_state)

    def swap_forward_verified(
        self,
        expected_source: SnapshotExpectation,
        expected_original: SnapshotExpectation,
        expected_clone: SnapshotExpectation,
        *,
        authorize_state: Callable[[str], None],
    ) -> None:
        """Fully revalidate and immediately perform one forward swap."""
        self._require_open()
        self._pre_forward_expectations = None
        self._require_pre_forward_snapshots(
            expected_source, expected_original, expected_clone
        )
        self.backend.swap_names(
            self.destination_parent_fd,
            self.destination_name,
            self._original_identity(),
            self.temporary_parent_fd,
            self.temporary_name,
            self._clone_identity(),
            authorize_state=authorize_state,
            action="swap_forward",
        )
        self._sync_namespace_parents()
        self._forward = True
        self._verify_forward_namespace()

    def verify_forward(self) -> None:
        self._verify_forward_namespace()
        if self.source_fd >= 0:
            try:
                self._require_source_mapping()
            except BackendError as exc:
                if exc.errno_value in _PATH_REPLACEMENT_ERRNOS or exc.reason in {
                    "identity_mismatch",
                    "parent_replaced",
                    "source_object_replaced",
                    "source_parent_replaced",
                }:
                    raise BackendError(
                        "source_object_replaced",
                        f"source name no longer maps to the held source: {exc.detail}",
                    ) from exc
                raise BackendError(
                    "source_revalidation_failed",
                    f"source name could not be revalidated: {exc.detail}",
                    exc.errno_value,
                ) from exc

    def _verify_forward_namespace(self) -> None:
        self._require_open()
        self._require_parent_bindings()
        self._require_bound_mapping(
            self.destination_parent_fd,
            self.destination_name,
            self._clone_identity(),
            "destination clone",
        )
        self._require_bound_mapping(
            self.temporary_parent_fd,
            self.temporary_name,
            self._original_identity(),
            "temporary original",
        )
        if not self.backend.identity(self.clone_fd).is_same_object(
            self._clone_identity()
        ):
            raise BackendError("object_replaced", "held clone fd changed identity")
        if not self.backend.identity(self.original_fd).is_same_object(
            self._original_identity()
        ):
            raise BackendError("object_replaced", "held original fd changed identity")
        self._forward = True

    def swap_back(self, *, authorize_state: Callable[[str], None]) -> None:
        self._require_open()
        if self.orientation() != "forward":
            raise BackendError(
                "invalid_orientation",
                "rollback swap requires destination=clone,temp=original",
            )
        self.backend.swap_names(
            self.destination_parent_fd,
            self.destination_name,
            self._clone_identity(),
            self.temporary_parent_fd,
            self.temporary_name,
            self._original_identity(),
            authorize_state=authorize_state,
            action="swap_back",
        )
        self._sync_namespace_parents()
        if self.orientation() != "before":
            raise BackendError(
                "rollback_postcondition_unverified", "rollback mapping is not verified"
            )
        self._forward = False

    def unlink_original(
        self,
        expected_clone: SnapshotExpectation,
        *,
        authorize_state: Callable[[str], None],
    ) -> None:
        self._require_open()
        if self.orientation() != "forward":
            raise BackendError(
                "invalid_orientation",
                "original cleanup requires verified forward mapping",
            )

        def validate_survivor_after_authorization() -> None:
            clone_snapshot = self.backend.require_snapshot(
                self.clone_fd,
                expected_clone,
                "final clone survivor after original-cleanup authorization",
                mismatch_reason="clone_snapshot_mismatch",
                changed_reason="clone_snapshot_changed",
                unreadable_reason="clone_snapshot_unreadable",
            )
            self.backend.require_exclusive_writer_policy(
                clone_snapshot.policy,
                "final clone survivor after original-cleanup authorization",
            )
            self._verify_forward_namespace()

        self.backend.unlink_name(
            self.temporary_parent_fd,
            self.temporary_name,
            self._original_identity(),
            authorize_state=authorize_state,
            action="unlink_original",
            validate_after_authorization=validate_survivor_after_authorization,
        )
        self.backend.fsync(self.temporary_parent_fd)
        self._original_unlinked = True
        self.revalidate_committed(expected_clone)

    def unlink_clone(
        self,
        expected_original: SnapshotExpectation,
        *,
        authorize_state: Callable[[str], None],
    ) -> None:
        self._require_open()
        if self.orientation() != "before":
            raise BackendError(
                "invalid_orientation",
                "clone cleanup requires destination=original,temp=clone",
            )

        def validate_survivor_after_authorization() -> None:
            original_snapshot = self.backend.require_snapshot(
                self.original_fd,
                expected_original,
                "final original survivor after clone-cleanup authorization",
                mismatch_reason="original_snapshot_mismatch",
                changed_reason="original_snapshot_changed",
                unreadable_reason="original_snapshot_unreadable",
            )
            self.backend.require_exclusive_writer_policy(
                original_snapshot.policy,
                "final original survivor after clone-cleanup authorization",
            )
            if self.orientation() != "before":
                raise BackendError(
                    "invalid_orientation",
                    "rollback namespace changed during survivor validation",
                )

        self.backend.unlink_name(
            self.temporary_parent_fd,
            self.temporary_name,
            self._clone_identity(),
            authorize_state=authorize_state,
            action="unlink_clone",
            validate_after_authorization=validate_survivor_after_authorization,
        )
        self.backend.fsync(self.temporary_parent_fd)
        self._clone_unlinked = True
        self.revalidate_rolled_back(expected_original)

    def abort_before_prepared(
        self,
        expected_original: SnapshotExpectation,
        *,
        authorize_state: Callable[[str], None],
    ) -> None:
        """Identity-bound cleanup before a durable PREPARED record exists."""
        self._require_open()
        orientation = self.orientation()
        if orientation == "clone_absent":
            self._clone_unlinked = True
        elif orientation == "before":
            self.unlink_clone(expected_original, authorize_state=authorize_state)
        elif orientation == "rolled_back":
            # A prior attempt may have durably unlinked the clone and then
            # failed its post-unlink survivor check.  Re-prove the complete
            # exclusive original before discarding the remaining stage
            # evidence on a later abort attempt.
            self.revalidate_rolled_back(expected_original)
        else:
            raise BackendError(
                "abort_not_safe",
                f"pre-PREPARED abort is unsafe in {orientation!r} orientation",
            )
        self.remove_empty_stage_parent(authorize_state=authorize_state)

    def remove_empty_stage_parent(
        self, *, authorize_state: Callable[[str], None]
    ) -> None:
        """Remove the bound private stage directory only after safe object cleanup."""
        self._require_open()
        self._require_parent_bindings()
        if not (self._original_unlinked or self._clone_unlinked):
            raise BackendError(
                "stage_not_cleanable",
                "stage removal requires verified original or clone cleanup first",
            )
        if self._stage_removed:
            self._require_stage_parent_absent()
            self._namespace_lifecycle_complete = True
            return
        expected = self._temporary_parent_identity()
        try:
            entries = os.listdir(self.temporary_parent_fd)
        except OSError as exc:
            raise self.backend._os_error(
                "stage_not_cleanable", "list private stage directory", exc
            )
        if entries:
            raise BackendError(
                "stage_not_cleanable", "private stage directory is not empty"
            )
        stage_path = os.path.dirname(self.temporary_path)
        container_owner, stage_name = self.backend._open_absolute_parent_owned(
            stage_path
        )
        container_fd = -1
        container_primary_error: Optional[BaseException] = None
        try:
            with container_owner:
                container_fd = container_owner.fileno()
            self.backend._require_stage_container_mapping(
                stage_path,
                container_fd,
                self._destination_parent_identity(),
            )
            self.backend.require_directory_identity_at(
                container_fd, stage_name, expected
            )
            self.backend._authorize_state(authorize_state, "remove_stage")
            self._require_parent_bindings()
            if os.listdir(self.temporary_parent_fd):
                raise BackendError(
                    "stage_not_cleanable",
                    "private stage became nonempty after removal authorization",
                )
            self.backend._require_stage_container_mapping(
                stage_path,
                container_fd,
                self._destination_parent_identity(),
            )
            self.backend.require_directory_identity_at(
                container_fd, stage_name, expected
            )
            try:
                os.rmdir(stage_name, dir_fd=container_fd)
            except OSError as exc:
                raise self.backend._os_error(
                    "stage_remove_failed", f"rmdir stage {stage_name!r}", exc
                )
            self.backend.fsync(container_fd)
            self.backend._require_stage_container_mapping(
                stage_path,
                container_fd,
                self._destination_parent_identity(),
            )
        except BaseException as exc:
            container_primary_error = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                self.backend._close_fd_owners(
                    (("stage container", container_owner),),
                    primary_error=container_primary_error,
                    durable_namespace_complete=container_primary_error is None,
                )
            except BaseException as cleanup_failure:
                if container_primary_error is not None:
                    self.backend._attach_cleanup_diagnostic(
                        container_primary_error, cleanup_failure
                    )
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("stage container", container_owner),),
                            primary_error=container_primary_error,
                            durable_namespace_complete=container_primary_error is None,
                        )
                    except BaseException as retry_failure:
                        self.backend._attach_cleanup_diagnostic(
                            container_primary_error, retry_failure
                        )
                else:
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("stage container", container_owner),),
                            primary_error=container_primary_error,
                            durable_namespace_complete=container_primary_error is None,
                        )
                    finally:
                        raise
        self._stage_removed = True
        try:
            _cleanup_dispatch = 1
            self.backend._close_fd_owners(
                (("removed private stage", self._temporary_parent_owner),),
                durable_namespace_complete=True,
            )
        except BaseException:
            try:
                _cleanup_dispatch = 2
                self.backend._close_fd_owners(
                    (("removed private stage", self._temporary_parent_owner),),
                    durable_namespace_complete=True,
                )
            finally:
                raise
        self._require_stage_parent_absent()
        self._namespace_lifecycle_complete = True

    def orientation(self) -> str:
        """Return a verified pre/post-swap or cleaned namespace orientation."""
        self._require_open()
        self._require_parent_bindings()
        destination = self._identity_at_optional(
            self.destination_parent_fd, self.destination_name
        )
        temporary = (
            self._identity_at_optional(self.temporary_parent_fd, self.temporary_name)
            if self.temporary_parent_fd >= 0
            else None
        )
        original = self._original_identity()
        if self.clone_identity is None:
            if (
                destination is not None
                and destination.is_same_object(original)
                and temporary is None
            ):
                return "clone_absent"
            raise BackendError(
                "orientation_unknown", "namespace does not match the pre-clone mapping"
            )
        clone = self._clone_identity()
        if (
            destination is not None
            and destination.is_same_object(clone)
            and temporary is None
            and self._original_unlinked
        ):
            return "committed"
        if (
            destination is not None
            and destination.is_same_object(original)
            and temporary is None
            and self._clone_unlinked
        ):
            return "rolled_back"
        if (
            destination is not None
            and destination.is_same_object(original)
            and temporary is not None
            and temporary.is_same_object(clone)
        ):
            return "before"
        if (
            destination is not None
            and destination.is_same_object(clone)
            and temporary is not None
            and temporary.is_same_object(original)
        ):
            return "forward"
        raise BackendError(
            "orientation_unknown",
            "namespace is neither the verified before nor forward mapping",
        )

    def _identity_at_optional(
        self, parent_fd: int, name: str
    ) -> Optional[FileIdentity]:
        try:
            return self.backend.identity_at(parent_fd, name)
        except BackendError as exc:
            if exc.errno_value == errno.ENOENT:
                return None
            if exc.reason == "not_regular":
                raise BackendError(
                    "object_replaced",
                    f"bound leaf {name!r} changed to a non-regular object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise

    def _require_parent_bindings(self, *, include_source: bool = False) -> None:
        bindings = [
            (
                self.destination_parent_fd,
                self.destination_parent_identity,
                "destination parent",
            ),
        ]
        if self.temporary_parent_fd >= 0:
            bindings.append(
                (
                    self.temporary_parent_fd,
                    self.temporary_parent_identity,
                    "temporary parent",
                )
            )
        for fd, expected, label in bindings:
            if expected is None or not self.backend.identity(fd).is_same_object(
                expected
            ):
                raise BackendError("parent_replaced", f"{label} identity changed")
        if not self.backend.validate_stage_container(
            self.destination_parent_fd
        ).is_same_object(self._destination_parent_identity()):
            raise BackendError(
                "parent_replaced", "destination stage container identity changed"
            )
        if include_source and self.source_parent_fd >= 0:
            self._require_source_parent_binding()
        self.backend._require_stage_container_mapping(
            self.destination_path,
            self.destination_parent_fd,
            self._destination_parent_identity(),
        )
        self.backend._require_stage_container_mapping(
            os.path.dirname(self.temporary_path),
            self.destination_parent_fd,
            self._destination_parent_identity(),
        )
        if self.temporary_parent_fd >= 0:
            self._require_absolute_parent_mapping(
                self.temporary_path,
                self.temporary_parent_identity,
                "temporary parent",
            )
            self.backend.validate_private_stage_parent(self.temporary_parent_fd)
        elif self._stage_removed:
            self._require_stage_parent_absent()
        else:
            raise BackendError("parent_replaced", "temporary parent is unavailable")

    def _require_absolute_parent_mapping(
        self,
        child_path: Optional[str],
        expected: Optional[FileIdentity],
        label: str,
    ) -> None:
        if child_path is None or expected is None:
            raise BackendError("binding_incomplete", f"{label} binding is unavailable")
        reopened_owner, _leaf = self.backend._open_absolute_parent_owned(child_path)
        with reopened_owner:
            actual = self.backend.identity(reopened_owner.fileno())
            reopened_owner.close()
        if not actual.is_same_object(expected):
            raise BackendError(
                "parent_replaced",
                f"absolute path for {label} maps to {actual.object_key}, "
                f"expected {expected.object_key}",
            )

    def _require_source_mapping(self) -> None:
        if self.source_fd < 0:
            return
        self._require_source_parent_binding()
        try:
            self.backend.require_identity_at(
                self.source_parent_fd, self.source_name, self._source_identity()
            )
        except BackendError as exc:
            if exc.reason in {"identity_mismatch", "not_regular"} or (
                exc.errno_value in _PATH_REPLACEMENT_ERRNOS
            ):
                raise BackendError(
                    "source_object_replaced",
                    f"source name no longer maps to the held object: {exc.detail}",
                    exc.errno_value,
                ) from exc
            raise BackendError(
                "source_revalidation_failed",
                f"source name could not be revalidated: {exc.detail}",
                exc.errno_value,
            ) from exc

    def _require_source_parent_binding(self) -> None:
        expected = self.source_parent_identity
        if self.source_path is None or expected is None or self.source_parent_fd < 0:
            raise BackendError(
                "binding_incomplete", "source parent binding is unavailable"
            )
        try:
            held = self.backend.identity(self.source_parent_fd)
        except BackendError as exc:
            raise BackendError(
                "source_parent_unreadable",
                f"held source parent could not be revalidated: {exc.detail}",
                exc.errno_value,
            ) from exc
        if not held.is_same_object(expected):
            raise BackendError(
                "source_parent_replaced", "held source parent identity changed"
            )

        reopened_owner, reopened_name = self.backend._open_absolute_parent_owned(
            self.source_path
        )
        reopened_primary: Optional[BaseException] = None
        try:
            try:
                with reopened_owner:
                    pass
            except BackendError as exc:
                reason = (
                    "source_parent_replaced"
                    if exc.errno_value in _PATH_REPLACEMENT_ERRNOS
                    else "source_parent_unreadable"
                )
                raise BackendError(
                    reason,
                    f"absolute source parent could not be revalidated: {exc.detail}",
                    exc.errno_value,
                ) from exc
            try:
                actual = self.backend.identity(reopened_owner.fileno())
            except BackendError as exc:
                raise BackendError(
                    "source_parent_unreadable",
                    f"reopened source parent could not be inspected: {exc.detail}",
                    exc.errno_value,
                ) from exc
        except BaseException as exc:
            reopened_primary = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                self.backend._close_fd_owners(
                    (("reopened source parent", reopened_owner),),
                    primary_error=reopened_primary,
                )
            except BaseException as cleanup_failure:
                if reopened_primary is not None:
                    self.backend._attach_cleanup_diagnostic(
                        reopened_primary, cleanup_failure
                    )
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("reopened source parent", reopened_owner),),
                            primary_error=reopened_primary,
                        )
                    except BaseException as retry_failure:
                        self.backend._attach_cleanup_diagnostic(
                            reopened_primary, retry_failure
                        )
                else:
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("reopened source parent", reopened_owner),),
                            primary_error=reopened_primary,
                        )
                    finally:
                        raise
        if reopened_name != self.source_name or not actual.is_same_object(expected):
            raise BackendError(
                "source_parent_replaced",
                f"absolute source path maps to parent {actual.object_key}, "
                f"expected {expected.object_key}",
            )

    def _sync_namespace_parents(self) -> None:
        self.backend.fsync(self.destination_parent_fd)
        if (
            self.temporary_parent_fd >= 0
            and not self._destination_parent_identity().is_same_object(
                self._temporary_parent_identity()
            )
        ):
            self.backend.fsync(self.temporary_parent_fd)

    def _require_stage_parent_absent(self) -> None:
        stage_path = os.path.dirname(self.temporary_path)
        container_owner, stage_name = self.backend._open_absolute_parent_owned(
            stage_path
        )
        container_fd = -1
        primary_error: Optional[BaseException] = None
        absence_durable = False
        try:
            with container_owner:
                container_fd = container_owner.fileno()
            self.backend._require_stage_container_mapping(
                stage_path,
                container_fd,
                self._destination_parent_identity(),
            )
            try:
                os.stat(stage_name, dir_fd=container_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.backend.fsync(container_fd)
                self.backend._require_stage_container_mapping(
                    stage_path,
                    container_fd,
                    self._destination_parent_identity(),
                )
                absence_durable = True
                return
            except OSError as exc:
                raise self.backend._os_error(
                    "stage_absence_unverified", f"stat stage {stage_name!r}", exc
                )
            raise BackendError(
                "stage_absence_unverified",
                "a filesystem object exists at the stage path",
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                self.backend._close_fd_owners(
                    (("stage absence container", container_owner),),
                    primary_error=primary_error,
                    durable_namespace_complete=absence_durable,
                )
            except BaseException as cleanup_failure:
                if primary_error is not None:
                    self.backend._attach_cleanup_diagnostic(
                        primary_error, cleanup_failure
                    )
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("stage absence container", container_owner),),
                            primary_error=primary_error,
                            durable_namespace_complete=absence_durable,
                        )
                    except BaseException as retry_failure:
                        self.backend._attach_cleanup_diagnostic(
                            primary_error, retry_failure
                        )
                else:
                    try:
                        _cleanup_dispatch = 2
                        self.backend._close_fd_owners(
                            (("stage absence container", container_owner),),
                            primary_error=primary_error,
                            durable_namespace_complete=absence_durable,
                        )
                    finally:
                        raise

    def _require_open(self) -> None:
        if self._closed:
            raise BackendError("transaction_closed", "transaction is already closed")

    def _source_identity(self) -> FileIdentity:
        if self.source_identity is None:
            raise BackendError("binding_incomplete", "source identity is unavailable")
        return self.source_identity

    def _original_identity(self) -> FileIdentity:
        if self.original_identity is None:
            raise BackendError("binding_incomplete", "original identity is unavailable")
        return self.original_identity

    def _clone_identity(self) -> FileIdentity:
        if self.clone_identity is None:
            raise BackendError("clone_missing", "clone identity is unavailable")
        return self.clone_identity

    def _destination_parent_identity(self) -> FileIdentity:
        if self.destination_parent_identity is None:
            raise BackendError(
                "binding_incomplete", "destination parent identity is unavailable"
            )
        return self.destination_parent_identity

    def _temporary_parent_identity(self) -> FileIdentity:
        if self.temporary_parent_identity is None:
            raise BackendError(
                "binding_incomplete", "temporary parent identity is unavailable"
            )
        return self.temporary_parent_identity

    def _close_owner_pass(
        self, *, primary_error: Optional[BaseException] = None
    ) -> Tuple[str, ...]:
        """Run one whole ordered transaction-owner drain pass."""
        close_failures = []
        for owner_attribute in self._FD_OWNER_CLOSE_ORDER:
            owner = getattr(self, owner_attribute)
            attempts = 0
            while not owner.closed and attempts < 2:
                try:
                    attempts += 1
                    owner.close(
                        primary_error=primary_error,
                        durable_namespace_complete=self._namespace_lifecycle_complete,
                    )
                except BaseException as exc:
                    close_failures.append(
                        f"{owner_attribute}: {self.backend._exception_diagnostic(exc)}"
                    )
        return tuple(close_failures)

    def close(self, *, primary_error: Optional[BaseException] = None) -> None:
        if self._closed:
            return
        close_failures = []
        first_interruption: Optional[BaseException] = None
        first_traceback: Optional[types.TracebackType] = None
        try:
            _cleanup_dispatch = 1
            close_failures.extend(self._close_owner_pass(primary_error=primary_error))
        except BaseException as cleanup_interruption:
            first_interruption = cleanup_interruption
            first_traceback = cleanup_interruption.__traceback__
            if primary_error is not None:
                self.backend._attach_cleanup_diagnostic(
                    primary_error, cleanup_interruption
                )
        try:
            _cleanup_dispatch = 2
            close_failures.extend(self._close_owner_pass(primary_error=primary_error))
        except BaseException as retry_interruption:
            if first_interruption is None:
                first_interruption = retry_interruption
                first_traceback = retry_interruption.__traceback__
            else:
                self.backend._attach_cleanup_diagnostic(
                    first_interruption, retry_interruption
                )
            if primary_error is not None:
                self.backend._attach_cleanup_diagnostic(
                    primary_error, retry_interruption
                )
        try:
            _cleanup_dispatch = 3
            self._closed = all(
                getattr(self, owner_attribute).closed
                for owner_attribute in self._FD_OWNER_CLOSE_ORDER
            )
            if not self._closed:
                close_failures.extend(
                    f"{owner_attribute}: fd remained open"
                    for owner_attribute in self._FD_OWNER_CLOSE_ORDER
                    if not getattr(self, owner_attribute).closed
                )
        except BaseException as verification_interruption:
            if first_interruption is None:
                first_interruption = verification_interruption
                first_traceback = verification_interruption.__traceback__
            else:
                self.backend._attach_cleanup_diagnostic(
                    first_interruption, verification_interruption
                )
            if primary_error is not None:
                self.backend._attach_cleanup_diagnostic(
                    primary_error, verification_interruption
                )
        try:
            _cleanup_dispatch = 4
            close_failure: Optional[BackendError] = None
            if close_failures:
                close_failure = BackendError(
                    "close_failed",
                    self.backend._bounded_cleanup_diagnostic("; ".join(close_failures)),
                )
                if primary_error is not None:
                    self.backend._attach_cleanup_diagnostic(
                        primary_error, close_failure
                    )
                elif first_interruption is not None:
                    self.backend._attach_cleanup_diagnostic(
                        first_interruption, close_failure
                    )
            if primary_error is not None:
                return
        except BaseException as finalization_interruption:
            if primary_error is not None:
                self.backend._attach_cleanup_diagnostic(
                    primary_error, finalization_interruption
                )
                return
            if first_interruption is not None:
                self.backend._attach_cleanup_diagnostic(
                    first_interruption, finalization_interruption
                )
                raise first_interruption.with_traceback(first_traceback)
            raise
        if first_interruption is not None:
            raise first_interruption.with_traceback(first_traceback)
        if close_failure is not None and not self._namespace_lifecycle_complete:
            raise close_failure

    def _drain_after_error(self, primary_error: BaseException) -> None:
        for _attempt in range(2):
            if self._closed:
                break
            try:
                _cleanup_attempt = _attempt + 1
                self.close(primary_error=primary_error)
            except BaseException as cleanup_failure:
                self.backend._attach_cleanup_diagnostic(primary_error, cleanup_failure)
        if not self._closed:
            self.backend._attach_cleanup_diagnostic(
                primary_error,
                BackendError(
                    "close_failed",
                    "transaction remained active after two close attempts",
                ),
            )

    def __enter__(self) -> "BoundTransaction":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            try:
                _cleanup_dispatch = 1
                self.close()
            except BaseException:
                try:
                    _cleanup_dispatch = 2
                    self.close()
                finally:
                    raise
            return
        try:
            _cleanup_dispatch = 1
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if primary_error is None:
                self.close()
            else:
                self._drain_after_error(primary_error)
        except BaseException as cleanup:
            primary_error = exc_value if isinstance(exc_value, BaseException) else None
            if primary_error is None:
                try:
                    _cleanup_dispatch = 2
                    self.close()
                finally:
                    raise
            self.backend._attach_cleanup_diagnostic(primary_error, cleanup)
            try:
                _cleanup_dispatch = 2
                self._drain_after_error(primary_error)
            except BaseException as retry_failure:
                self.backend._attach_cleanup_diagnostic(primary_error, retry_failure)

    def __del__(self) -> None:
        for _attempt in range(2):
            if getattr(self, "_closed", True):
                return
            try:
                _cleanup_attempt = _attempt + 1
                self.close()
            except BaseException:
                continue


__all__ = [
    "ACL_TYPE_EXTENDED",
    "BackendError",
    "BoundTransaction",
    "CLONE_ACL",
    "COPYFILE_ALL",
    "DarwinBackend",
    "F_FULLFSYNC",
    "FileIdentity",
    "FilePolicy",
    "FileSnapshot",
    "RENAME_EXCL",
    "RENAME_NOFOLLOW_ANY",
    "RENAME_SWAP",
]
