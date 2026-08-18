#!/usr/bin/env python3
"""Safely copy one rollout JSONL file into its ordinary mirror location.

The direct source and destination parents must exclude other principals from
namespace writes.  Higher canonical ancestors are an environmental stability
prerequisite: traversal is component-wise and nofollow, but this helper does
not prove every ancestor's access policy (including sticky-directory rules).
As in the shared Darwin backend, malicious same-euid replacement after the
last check and before a syscall or point-in-time receipt is outside the threat
boundary.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from typing import Callable, Dict, Optional, Tuple

from codex_reflink_darwin import (
    BackendError,
    DarwinBackend,
    FileIdentity,
    FilePolicy,
    FileSnapshot,
    SnapshotExpectation,
    _OwnedFD,
)


RECEIPT_VERSION = 1
EXIT_OK = 0
EXIT_FATAL = 2
EXIT_DEFERRED = 75
_READ_CHUNK = 1024 * 1024
_SOURCE_PREFIX_MAX_ATTEMPTS = 3
_RECEIPT_LIMIT = 64 * 1024
_DIAGNOSTIC_LIMIT = 4 * 1024
_OWNER_DIAGNOSTIC_LIMIT = _DIAGNOSTIC_LIMIT
_OWNER_DIAGNOSTIC_PREFIX_LIMIT = 96
_OWNER_DIAGNOSTIC_BODY_FLOOR = 40
_GENERIC_DIAGNOSTIC_BODY_FLOOR = 128
_REASON_LIMIT = 128
_MAX_DIAGNOSTIC_SEGMENTS = 8
_TRUNCATED_MARKER = "...[truncated]"
_CLONE_COMPAT_ERRNOS = {
    errno.ENOTSUP,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    errno.EXDEV,
}
_PATH_TRANSIENT_ERRNOS = {
    errno.ENOENT,
    errno.ENOTDIR,
    errno.ELOOP,
    getattr(errno, "ESTALE", 70),
}
_SNAPSHOT_TRANSIENT_REASONS = {
    "acl_unstable",
    "content_changed",
    "object_replaced",
    "policy_unstable",
    "xattr_unstable",
}
_OUTCOME_EXITS = {
    "updated": EXIT_OK,
    "unchanged": EXIT_OK,
    "no-complete-line": EXIT_OK,
    "deferred": EXIT_DEFERRED,
    "fatal": EXIT_FATAL,
}


class DeferredSync(RuntimeError):
    """A pre-publish transient that leaves the destination untouched."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class SyncReceipt:
    version: int
    command: str
    outcome: str
    source: str
    destination: str
    old_size: Optional[int]
    new_size: Optional[int]
    publish_size: Optional[int]
    source_size: Optional[int]
    method: Optional[str]
    partial: Optional[bool]
    mtime_semantics: Optional[str]
    old_identity: Optional[Dict[str, int]]
    new_identity: Optional[Dict[str, int]]
    destination_mutated: Optional[bool]
    reason: Optional[str]
    detail: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _DiagnosticSegment:
    label: str
    message: str

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and (value in self.label or value in self.message)


class _RenderedOwnerDiagnostics(str):
    """Rendered owner diagnostics with private structural provenance."""


class _ReceiptFinalizationSignal(str):
    """Non-rendered control signal for a failed receipt finalization pass."""


def _identity_dict(identity: Optional[FileIdentity]) -> Optional[Dict[str, int]]:
    if identity is None:
        return None
    return {"dev": identity.dev, "ino": identity.ino}


def _exact_diagnostic_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        encoded = str.encode(value, "utf-8", errors="replace")
        return bytes.decode(encoded, "utf-8")
    except BaseException:
        return None


def _truncate_diagnostic(detail: str, limit: int) -> str:
    normalized = _exact_diagnostic_text(detail) or "<unprintable>"
    encoded = str.encode(normalized, "utf-8", errors="replace")
    marker = _TRUNCATED_MARKER.encode("ascii")
    if len(encoded) <= limit:
        return normalized
    if limit <= 0:
        return ""
    if limit <= len(marker):
        return marker[:limit].decode("ascii")
    prefix = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
    return f"{prefix}{_TRUNCATED_MARKER}"


def _bounded_diagnostic(detail: str) -> str:
    return _truncate_diagnostic(detail, _DIAGNOSTIC_LIMIT)


def _combine_diagnostics(*details: str) -> str:
    if len(details) > _MAX_DIAGNOSTIC_SEGMENTS:
        details = (details[0], *details[-(_MAX_DIAGNOSTIC_SEGMENTS - 1) :])
    parts = tuple(
        _bounded_diagnostic(normalized)
        for detail in details
        if (normalized := _exact_diagnostic_text(detail))
    )
    if not parts:
        return ""
    combined = "; ".join(parts)
    if len(str.encode(combined, "utf-8", errors="replace")) <= _DIAGNOSTIC_LIMIT:
        return combined
    separator_bytes = 2 * (len(parts) - 1)
    available = _DIAGNOSTIC_LIMIT - separator_bytes
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
        _truncate_diagnostic(part, budget) for part, budget in zip(parts, budgets)
    )


def _diagnostic_segment(label: str, message: str) -> _DiagnosticSegment:
    normalized_label = _exact_diagnostic_text(label) or "diagnostic"
    label_size = len(str.encode(normalized_label, "utf-8", errors="replace"))
    if label_size > _OWNER_DIAGNOSTIC_PREFIX_LIMIT or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- "
        for character in normalized_label
    ):
        normalized_label = "diagnostic"
    normalized_message = _exact_diagnostic_text(message) or "<unprintable>"
    return _DiagnosticSegment(normalized_label, normalized_message)


def _escape_diagnostic_message(message: str) -> str:
    return (
        message.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("; ", "\\x3b ")
    )


def _combine_owner_diagnostics(*segments: _DiagnosticSegment) -> str:
    """Render trusted labels and untrusted messages within the receipt bound."""
    normalized_segments = tuple(
        _diagnostic_segment(segment.label, segment.message)
        for segment in segments
        if isinstance(segment, _DiagnosticSegment)
    )
    if not normalized_segments:
        return ""
    labeled_parts = tuple(
        (segment.label, _escape_diagnostic_message(segment.message))
        for segment in normalized_segments
    )
    combined = "; ".join(f"{label}: {message}" for label, message in labeled_parts)
    if len(str.encode(combined, "utf-8", errors="replace")) <= _OWNER_DIAGNOSTIC_LIMIT:
        return combined

    fixed_bytes = 2 * (len(labeled_parts) - 1) + sum(
        len(str.encode(prefix, "utf-8", errors="replace")) + 2
        for prefix, _body in labeled_parts
    )
    available = max(0, _OWNER_DIAGNOSTIC_LIMIT - fixed_bytes)
    lengths = [
        len(str.encode(body, "utf-8", errors="replace"))
        for _prefix, body in labeled_parts
    ]
    budgets = [0] * len(labeled_parts)
    minimum_budgets = [
        min(
            length,
            (
                _GENERIC_DIAGNOSTIC_BODY_FLOOR
                if label == "operation" or label.startswith("cleanup-note-")
                else _OWNER_DIAGNOSTIC_BODY_FLOOR
            ),
        )
        for (label, _body), length in zip(labeled_parts, lengths)
    ]
    if sum(minimum_budgets) <= available:
        budgets = minimum_budgets
    remaining = set(range(len(labeled_parts)))
    while remaining:
        share, extra = divmod(available - sum(budgets), len(remaining))
        fixed = [
            index for index in remaining if lengths[index] - budgets[index] <= share
        ]
        if not fixed:
            for offset, index in enumerate(sorted(remaining)):
                budgets[index] += share + (1 if offset < extra else 0)
            break
        for index in fixed:
            budgets[index] = lengths[index]
            remaining.remove(index)
    combined = "; ".join(
        f"{prefix}: {_truncate_diagnostic(body, budget)}"
        for (prefix, body), budget in zip(labeled_parts, budgets)
    )
    return _truncate_diagnostic(combined, _OWNER_DIAGNOSTIC_LIMIT)


def _direct_cleanup_diagnostic_parts(primary: BaseException) -> Tuple[str, ...]:
    try:
        parts = getattr(primary, "cleanup_diagnostics", ())
        if type(parts) is tuple and parts:
            limited_parts = parts[-_MAX_DIAGNOSTIC_SEGMENTS:]
            return tuple(
                _bounded_diagnostic(normalized)
                for part in limited_parts
                if (normalized := _exact_diagnostic_text(part))
            )
        if type(getattr(primary, "owner_cleanup_diagnostics", None)) is tuple:
            return ()
        legacy = getattr(primary, "cleanup_diagnostic", "")
        normalized = _exact_diagnostic_text(legacy)
        return (_bounded_diagnostic(normalized),) if normalized else ()
    except BaseException:
        return ()


def _direct_owner_cleanup_diagnostics(
    primary: BaseException,
) -> Tuple[_DiagnosticSegment, ...]:
    try:
        segments = getattr(primary, "owner_cleanup_diagnostics", ())
        if type(segments) is not tuple:
            return ()
        return tuple(
            segment for segment in segments if isinstance(segment, _DiagnosticSegment)
        )
    except BaseException:
        return ()


def _render_cleanup_diagnostics(
    generic_parts: Tuple[str, ...],
    owner_segments: Tuple[_DiagnosticSegment, ...],
) -> str:
    return _combine_owner_diagnostics(
        *(
            _diagnostic_segment(f"cleanup-note-{index}", part)
            for index, part in enumerate(generic_parts, start=1)
        ),
        *owner_segments,
    )


def _cleanup_diagnostic_parts(primary: BaseException) -> Tuple[str, ...]:
    chain = []
    seen_exceptions = set()
    current: Optional[BaseException] = primary
    for _depth in range(_MAX_DIAGNOSTIC_SEGMENTS):
        if current is None or id(current) in seen_exceptions:
            break
        seen_exceptions.add(id(current))
        chain.append(current)
        try:
            cause = getattr(current, "__cause__", None)
        except BaseException:
            break
        if isinstance(cause, BaseException):
            current = cause
        else:
            try:
                suppress_context = bool(getattr(current, "__suppress_context__", False))
            except BaseException:
                break
            if suppress_context:
                current = None
            else:
                try:
                    current = getattr(current, "__context__", None)
                except BaseException:
                    break
        if not isinstance(current, BaseException):
            break
    collected = []
    seen_parts = set()
    for item in reversed(chain):
        for part in _direct_cleanup_diagnostic_parts(item):
            if part in seen_parts:
                collected.remove(part)
            else:
                seen_parts.add(part)
            collected.append(part)
        try:
            notes = getattr(item, "__notes__", ())
        except BaseException:
            notes = ()
        if type(notes) not in (tuple, list):
            continue
        for note in notes[-_MAX_DIAGNOSTIC_SEGMENTS:]:
            if isinstance(note, _RenderedOwnerDiagnostics):
                continue
            normalized_note = _exact_diagnostic_text(note)
            if not normalized_note:
                continue
            bounded_note = _bounded_diagnostic(normalized_note)
            if bounded_note in seen_parts:
                collected.remove(bounded_note)
            else:
                seen_parts.add(bounded_note)
            collected.append(bounded_note)
    return tuple(collected[-_MAX_DIAGNOSTIC_SEGMENTS:])


def _exception_diagnostic(exc: BaseException) -> str:
    try:
        exception_name = _exact_diagnostic_text(type(exc).__name__)
    except BaseException:
        return "<unprintable-exception>"
    if not exception_name:
        return "<unprintable-exception>"
    try:
        detail = _exact_diagnostic_text(str(exc)) or "<unprintable>"
    except BaseException:
        detail = "<unprintable>"
    return _bounded_diagnostic(f"{exception_name}: {detail}")


def _attach_cleanup_diagnostic(
    primary: BaseException,
    detail: str,
    *,
    context: Optional[str] = "owned-FD cleanup failed",
) -> None:
    safe_detail = _exact_diagnostic_text(detail) or "<unprintable>"
    if context is None:
        note = _bounded_diagnostic(safe_detail)
    else:
        safe_context = _exact_diagnostic_text(context) or "cleanup failed"
        note = _bounded_diagnostic(f"{safe_context} ({safe_detail})")
    try:
        existing = _direct_cleanup_diagnostic_parts(primary)
        parts = (*existing[-(_MAX_DIAGNOSTIC_SEGMENTS - 1) :], note)
        setattr(primary, "cleanup_diagnostics", parts)
        setattr(
            primary,
            "cleanup_diagnostic",
            _render_cleanup_diagnostics(
                parts,
                _direct_owner_cleanup_diagnostics(primary),
            ),
        )
        add_note = getattr(BaseException, "add_note", None)
        if callable(add_note):
            add_note(primary, note)
        else:
            existing_notes = getattr(primary, "__notes__", ())
            notes = (
                list(existing_notes[-(_MAX_DIAGNOSTIC_SEGMENTS - 1) :])
                if type(existing_notes) in (tuple, list)
                else []
            )
            notes.append(note)
            setattr(primary, "__notes__", notes)
    except BaseException:
        return


def _attach_owner_cleanup_diagnostics(
    primary: BaseException,
    segments: Tuple[_DiagnosticSegment, ...],
) -> None:
    try:
        normalized_segments = tuple(
            _diagnostic_segment(segment.label, segment.message)
            for segment in segments
            if isinstance(segment, _DiagnosticSegment)
        )
        if not normalized_segments:
            return
        owner_segments = (
            *_direct_owner_cleanup_diagnostics(primary),
            *normalized_segments,
        )
        setattr(primary, "owner_cleanup_diagnostics", owner_segments)
        generic_parts = _direct_cleanup_diagnostic_parts(primary)
        setattr(
            primary,
            "cleanup_diagnostic",
            _render_cleanup_diagnostics(generic_parts, owner_segments),
        )
        note = _RenderedOwnerDiagnostics(
            _combine_owner_diagnostics(*normalized_segments)
        )
        add_note = getattr(BaseException, "add_note", None)
        if callable(add_note):
            add_note(primary, note)
        else:
            existing_notes = getattr(primary, "__notes__", ())
            notes = (
                list(existing_notes[-(_MAX_DIAGNOSTIC_SEGMENTS - 1) :])
                if type(existing_notes) in (tuple, list)
                else []
            )
            notes.append(note)
            setattr(primary, "__notes__", notes)
    except BaseException:
        return


def _policy_access_key(policy: FilePolicy) -> Tuple[object, ...]:
    return (
        policy.uid,
        policy.gid,
        policy.mode,
        policy.flags,
        policy.xattrs,
        policy.acl_native,
    )


class _CompatFDSlot:
    """Expose an owner-backed transaction slot as the historical raw int view."""

    def __init__(self, owner_attribute: str, subject: str) -> None:
        self.owner_attribute = owner_attribute
        self.subject = subject

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        held = getattr(instance, self.owner_attribute, None)
        if held is None or held.closed:
            return -1
        return held.fileno()

    def __set__(self, instance, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < -1:
            raise BackendError("invalid_fd", f"{self.subject} has an invalid fd")
        current = getattr(instance, self.owner_attribute, None)
        if value >= 0 and current is not None and not current.closed:
            if current.fileno() == value:
                return
            raise BackendError("fd_already_owned", f"{self.subject} is already open")
        if value < 0:
            if current is not None:
                current.close()
            setattr(instance, self.owner_attribute, None)
            return
        pending_fd = [value]
        replacement: Optional[_OwnedFD] = None
        try:
            _adoption_guard = 1
            replacement = instance.backend._adopt_fd(
                value,
                self.subject,
                pending_fd=pending_fd,
            )
            with replacement:
                replacement.retain_if_registered(
                    lambda: getattr(instance, self.owner_attribute, None) is replacement
                )
                setattr(instance, self.owner_attribute, replacement)
        except BaseException as primary:
            if (
                replacement is None
                or getattr(instance, self.owner_attribute, None) is not replacement
            ):
                try:
                    _cleanup_dispatch = 1
                    if replacement is not None and not replacement.closed:
                        replacement._drain(primary_error=primary)
                    else:
                        instance.backend._drain_pending_fd(
                            pending_fd,
                            self.subject,
                            primary_error=primary,
                        )
                except BaseException as cleanup_failure:
                    _attach_cleanup_diagnostic(
                        primary,
                        _exception_diagnostic(cleanup_failure),
                        context="pending FD cleanup failed",
                    )
                    try:
                        _cleanup_dispatch = 2
                        if replacement is not None and not replacement.closed:
                            replacement._drain(primary_error=primary)
                        else:
                            instance.backend._drain_pending_fd(
                                pending_fd,
                                self.subject,
                                primary_error=primary,
                            )
                    except BaseException as retry_failure:
                        _attach_cleanup_diagnostic(
                            primary,
                            _exception_diagnostic(retry_failure),
                            context="pending FD cleanup retry failed",
                        )
            raise


class MirrorSync:
    """One held-FD bind, stage, publish, and cleanup transaction."""

    source_parent_fd = _CompatFDSlot("_source_parent_owner", "source parent")
    source_fd = _CompatFDSlot("_source_owner", "source")
    destination_parent_fd = _CompatFDSlot(
        "_destination_parent_owner", "destination parent"
    )
    destination_fd = _CompatFDSlot("_destination_owner", "destination")
    stage_fd = _CompatFDSlot("_stage_owner", "private stage")
    candidate_fd = _CompatFDSlot("_candidate_owner", "staged candidate")

    def __init__(
        self,
        backend: DarwinBackend,
        *,
        action_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.backend = backend
        self.action_hook = action_hook
        self.source_path = ""
        self.destination_path = ""
        self._source_parent_owner: Optional[_OwnedFD] = None
        self._source_owner: Optional[_OwnedFD] = None
        self._destination_parent_owner: Optional[_OwnedFD] = None
        self._destination_owner: Optional[_OwnedFD] = None
        self._stage_owner: Optional[_OwnedFD] = None
        self._candidate_owner: Optional[_OwnedFD] = None
        self.source_name = ""
        self.destination_name = ""
        self.stage_name = ""
        self.stage_path = ""
        self.candidate_name = "candidate.jsonl"
        self.source_parent_identity: Optional[FileIdentity] = None
        self.source_identity: Optional[FileIdentity] = None
        self.destination_parent_identity: Optional[FileIdentity] = None
        self.destination_identity: Optional[FileIdentity] = None
        self.destination_snapshot: Optional[FileSnapshot] = None
        self.stage_identity: Optional[FileIdentity] = None
        self.candidate_identity: Optional[FileIdentity] = None
        self.candidate_expectation: Optional[SnapshotExpectation] = None
        self.fallback_source_expectation: Optional[SnapshotExpectation] = None
        self.fallback_source_identity: Optional[FileIdentity] = None
        self.fallback_source_drifted = False
        self.method: Optional[str] = None
        self.publish_size: Optional[int] = None
        self.source_size: Optional[int] = None
        self.source_mtime_ns: Optional[int] = None
        self.source_ctime_ns: Optional[int] = None
        self.partial: Optional[bool] = None
        self.mtime_semantics: Optional[str] = None
        self.publish_attempted = False
        self.published_identity: Optional[FileIdentity] = None
        self.stage_removed = False
        self.cleanup_hook_ran = False
        self._owner_recovery_required = False
        self._owner_close_diagnostics: list[_DiagnosticSegment] = []

    def sync_one(self, source: str, destination: str) -> SyncReceipt:
        self.source_path = source
        self.destination_path = destination
        result: Optional[SyncReceipt] = None
        primary_error: Optional[BaseException] = None
        primary_detail_base: Optional[str] = None
        try:
            result = self._execute()
        except DeferredSync as exc:
            try:
                _handler_dispatch = 1
                primary_error = exc
                result, primary_detail_base = self._deferred_result(exc)
            except BaseException as handler_interruption:
                primary_error = exc
                _attach_cleanup_diagnostic(
                    exc,
                    _exception_diagnostic(handler_interruption),
                    context="deferred handler interrupted",
                )
                try:
                    _handler_dispatch = 2
                    result, primary_detail_base = self._deferred_result(exc)
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        exc,
                        _exception_diagnostic(retry_interruption),
                        context="deferred handler retry interrupted",
                    )
                    if self.published_identity is not None:
                        fallback_reason = exc.reason
                        primary_detail_base = exc.detail
                    elif self.publish_attempted:
                        fallback_reason = "publish_state_ambiguous"
                        primary_detail_base = (
                            "a deferred error occurred after publish was attempted"
                        )
                    else:
                        fallback_reason = "stage_cleanup_failed"
                        primary_detail_base = (
                            f"deferred operation ({_exception_diagnostic(exc)})"
                        )
                    result = self._fatal_receipt(
                        fallback_reason,
                        _combine_diagnostics(
                            primary_detail_base,
                            *_cleanup_diagnostic_parts(exc),
                        ),
                    )
        except BackendError as exc:
            try:
                _handler_dispatch = 1
                primary_error = exc
                primary_detail_base = exc.detail
                result = self._backend_error_result(exc)
            except BaseException as handler_interruption:
                primary_error = exc
                primary_detail_base = exc.detail
                _attach_cleanup_diagnostic(
                    exc,
                    _exception_diagnostic(handler_interruption),
                    context="receipt assembly interrupted",
                )
                try:
                    _handler_dispatch = 2
                    result = self._backend_error_result(exc)
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        exc,
                        _exception_diagnostic(retry_interruption),
                        context="receipt assembly retry interrupted",
                    )
                    result = self._fatal_receipt(
                        exc.reason,
                        _combine_diagnostics(
                            primary_detail_base,
                            *_cleanup_diagnostic_parts(exc),
                        ),
                    )
        except Exception as exc:  # pragma: no cover - defensive receipt boundary
            try:
                _handler_dispatch = 1
                primary_error = exc
                primary_detail_base = _exception_diagnostic(exc)
                result = self._unexpected_error_result(exc, primary_detail_base)
            except BaseException as handler_interruption:
                primary_error = exc
                primary_detail_base = _exception_diagnostic(exc)
                _attach_cleanup_diagnostic(
                    exc,
                    _exception_diagnostic(handler_interruption),
                    context="unexpected-error handler interrupted",
                )
                try:
                    _handler_dispatch = 2
                    result = self._unexpected_error_result(exc, primary_detail_base)
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        exc,
                        _exception_diagnostic(retry_interruption),
                        context="unexpected-error handler retry interrupted",
                    )
                    result = self._fatal_receipt(
                        "unexpected_error",
                        _combine_diagnostics(
                            primary_detail_base,
                            *_cleanup_diagnostic_parts(exc),
                        ),
                    )
        except BaseException as exc:
            try:
                _handler_dispatch = 1
                primary_error = exc
                if self.published_identity is not None or not self.publish_attempted:
                    self._cleanup_stage(primary_error=exc)
            except BaseException as handler_interruption:
                primary_error = exc
                _attach_cleanup_diagnostic(
                    exc,
                    _exception_diagnostic(handler_interruption),
                    context="raw handler interrupted",
                )
                try:
                    _handler_dispatch = 2
                    if (
                        self.published_identity is not None
                        or not self.publish_attempted
                    ):
                        self._cleanup_stage(primary_error=exc)
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        exc,
                        _exception_diagnostic(retry_interruption),
                        context="raw handler retry interrupted",
                    )
            raise
        finally:
            try:
                _cleanup_dispatch = 1
                close_error = self._close_all(primary_error=primary_error)
            except BaseException as cleanup_interruption:
                cleanup_detail = (
                    "owned-FD cleanup dispatch interrupted: "
                    f"{_exception_diagnostic(cleanup_interruption)}"
                )
                if primary_error is None:
                    primary_error = cleanup_interruption
                    primary_detail_base = _exception_diagnostic(cleanup_interruption)
                else:
                    _attach_cleanup_diagnostic(
                        primary_error, _exception_diagnostic(cleanup_interruption)
                    )
                try:
                    _cleanup_dispatch = 2
                    retry_error = self._close_all(primary_error=primary_error)
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        primary_error, _exception_diagnostic(retry_interruption)
                    )
                    retry_error = (
                        "owned-FD cleanup retry dispatch interrupted: "
                        f"{_exception_diagnostic(retry_interruption)}"
                    )
                if isinstance(retry_error, _RenderedOwnerDiagnostics):
                    close_error = retry_error
                else:
                    close_error = _combine_diagnostics(
                        cleanup_detail,
                        *(value for value in (retry_error,) if value is not None),
                    )
            try:
                _receipt_dispatch = 1
                result = self._finalize_close_result(
                    result,
                    primary_error=primary_error,
                    primary_detail_base=primary_detail_base,
                    close_error=close_error,
                )
            except BaseException as finalization_interruption:
                if primary_error is None:
                    primary_error = finalization_interruption
                    primary_detail_base = _exception_diagnostic(
                        finalization_interruption
                    )
                else:
                    _attach_cleanup_diagnostic(
                        primary_error,
                        _exception_diagnostic(finalization_interruption),
                        context="receipt finalization interrupted",
                    )
                if close_error is None:
                    close_error = _ReceiptFinalizationSignal()
                try:
                    _receipt_dispatch = 2
                    result = self._finalize_close_result(
                        result,
                        primary_error=primary_error,
                        primary_detail_base=primary_detail_base,
                        close_error=close_error,
                    )
                except BaseException as retry_interruption:
                    _attach_cleanup_diagnostic(
                        primary_error,
                        _exception_diagnostic(retry_interruption),
                        context="receipt finalization retry interrupted",
                    )
                    result = self._fatal_receipt(
                        "close_failed",
                        _combine_diagnostics(
                            primary_detail_base or "operation cleanup failed",
                            *_cleanup_diagnostic_parts(primary_error),
                            close_error,
                        ),
                    )
        if result is None:  # pragma: no cover - defensive invariant
            return self._fatal_receipt("internal_error", "sync produced no receipt")
        return result

    def _deferred_result(
        self, primary: DeferredSync
    ) -> Tuple[SyncReceipt, Optional[str]]:
        if self.published_identity is not None:
            self._cleanup_stage(primary_error=primary)
            return (
                self._fatal_receipt(
                    primary.reason,
                    _combine_diagnostics(
                        primary.detail,
                        *_cleanup_diagnostic_parts(primary),
                    ),
                ),
                primary.detail,
            )
        if self.publish_attempted:
            detail_base = "a deferred error occurred after publish was attempted"
            return (
                self._fatal_receipt(
                    "publish_state_ambiguous",
                    _combine_diagnostics(
                        detail_base,
                        *_cleanup_diagnostic_parts(primary),
                    ),
                ),
                detail_base,
            )
        cleanup_error = self._cleanup_stage(primary_error=primary)
        cleanup_parts = _cleanup_diagnostic_parts(primary)
        if cleanup_error is None and not cleanup_parts:
            return (
                self._receipt(
                    "deferred",
                    new_size=self._old_size(),
                    destination_mutated=False,
                    reason=primary.reason,
                    detail=primary.detail,
                ),
                None,
            )
        detail_base = f"deferred operation ({_exception_diagnostic(primary)})"
        return (
            self._fatal_receipt(
                "stage_cleanup_failed",
                _combine_diagnostics(detail_base, *cleanup_parts),
            ),
            detail_base,
        )

    def _unexpected_error_result(
        self, primary: Exception, detail_base: str
    ) -> SyncReceipt:
        if self.published_identity is not None or not self.publish_attempted:
            self._cleanup_stage(primary_error=primary)
        return self._fatal_receipt(
            "unexpected_error",
            _combine_diagnostics(
                detail_base,
                *_cleanup_diagnostic_parts(primary),
            ),
        )

    def _backend_error_result(self, primary: BackendError) -> SyncReceipt:
        if self.published_identity is not None or not self.publish_attempted:
            self._cleanup_stage(primary_error=primary)
        return self._fatal_receipt(
            primary.reason,
            _combine_diagnostics(
                primary.detail,
                *_cleanup_diagnostic_parts(primary),
            ),
        )

    def _finalize_close_result(
        self,
        result: Optional[SyncReceipt],
        *,
        primary_error: Optional[BaseException],
        primary_detail_base: Optional[str],
        close_error: Optional[str],
    ) -> Optional[SyncReceipt]:
        receipt_finalization_failed = type(close_error) is _ReceiptFinalizationSignal
        owners_remain_open = self._has_open_fd_owners()
        cleanup_failed = (
            owners_remain_open
            or self._owner_recovery_required
            or receipt_finalization_failed
        )
        if close_error is None and not cleanup_failed:
            return result

        close_parts = tuple(self._owner_close_diagnostics)
        normalized_close_error = _exact_diagnostic_text(close_error)
        if normalized_close_error and not isinstance(
            close_error, _RenderedOwnerDiagnostics
        ):
            close_parts = (
                *close_parts,
                _diagnostic_segment("cleanup-summary", normalized_close_error),
            )
        if not close_parts and not receipt_finalization_failed:
            close_parts = (
                _diagnostic_segment(
                    "cleanup",
                    "owned-FD cleanup failed (an owner remained open)",
                ),
            )
        close_detail = _combine_owner_diagnostics(*close_parts)
        if primary_error is not None and result is not None:
            cleanup_parts = _cleanup_diagnostic_parts(primary_error)
            operation_detail = (
                primary_detail_base or result.detail or "operation failed"
            )
            detail_parts = (
                _diagnostic_segment("operation", operation_detail),
                *(
                    _diagnostic_segment(f"cleanup-note-{index}", part)
                    for index, part in enumerate(cleanup_parts, start=1)
                ),
                *close_parts,
            )
            if result.outcome == "fatal":
                return replace(
                    result,
                    detail=_combine_owner_diagnostics(*detail_parts),
                )
            return self._fatal_receipt(
                "close_failed",
                _combine_owner_diagnostics(
                    _diagnostic_segment(
                        "operation", _exception_diagnostic(primary_error)
                    ),
                    *(
                        _diagnostic_segment(f"cleanup-note-{index}", part)
                        for index, part in enumerate(cleanup_parts, start=1)
                    ),
                    *close_parts,
                ),
            )
        if result is None or result.outcome not in {
            "updated",
            "unchanged",
            "no-complete-line",
        }:
            return self._fatal_receipt("close_failed", close_detail)
        if cleanup_failed:
            return self._fatal_receipt("close_failed", close_detail)
        return result

    def _execute(self) -> SyncReceipt:
        self._validate_paths()
        self._bind_source()
        self._hook("after_source_open")
        self._bind_destination()
        self._hook("after_destination_bind")
        self._create_stage()
        self._hook("after_stage_create")
        self._create_candidate()
        self._hook("after_clone")

        candidate = self.backend.snapshot_file(self._candidate_fd())
        self.backend.require_exclusive_writer_policy(
            candidate.policy, "staged rollout candidate"
        )
        initial_source_policy = self._snapshot_source_policy("rollout source")
        if _policy_access_key(candidate.policy) != _policy_access_key(
            initial_source_policy
        ):
            raise DeferredSync(
                "source_unstable",
                "source access policy differs from the staged candidate",
            )
        self.candidate_identity = candidate.identity
        self.candidate_expectation = self.backend.snapshot_expectation(candidate)

        complete_size = self._last_complete_line_size(
            self._candidate_fd(), candidate.identity.size
        )
        self.publish_size = complete_size
        if complete_size == 0:
            self.partial = None
            self.mtime_semantics = None
            self._require_source_mapping()
            self._require_destination_binding(include_stage=True)
            self._run_before_cleanup_hook()
            self._require_source_prefix(candidate)
            cleanup_error = self._cleanup_stage(keep_candidate_open=True)
            if cleanup_error is not None:
                raise BackendError("stage_cleanup_failed", cleanup_error)
            self._require_nonpublishing_result_bindings(candidate)
            return self._receipt(
                "no-complete-line",
                new_size=self._old_size(),
                destination_mutated=False,
            )

        self.partial = complete_size != candidate.identity.size
        if self.partial:
            self._hook("before_truncate")
            candidate = self.backend.truncate_preserving_policy(
                self._candidate_fd(), complete_size, candidate.policy
            )
            self.mtime_semantics = "captured-pre-truncate-source-policy"
            self._hook("after_truncate")
        else:
            self.mtime_semantics = "captured-source-policy"

        self.backend.full_fsync(self._candidate_fd())
        candidate = self.backend.snapshot_file(self._candidate_fd())
        if candidate.identity.size != complete_size:
            raise BackendError(
                "candidate_size_mismatch",
                f"candidate size is {candidate.identity.size}, expected {complete_size}",
            )
        self.backend.require_exclusive_writer_policy(
            candidate.policy, "durable staged rollout candidate"
        )
        self.candidate_identity = candidate.identity
        self.candidate_expectation = self.backend.snapshot_expectation(candidate)

        self._require_source_prefix(candidate)
        self._hook("before_compare")
        if self._destination_is_unchanged(candidate):
            self._require_source_mapping()
            self._require_destination_binding(include_stage=True)
            self._run_before_cleanup_hook()
            self._require_source_prefix(candidate)
            cleanup_error = self._cleanup_stage(keep_candidate_open=True)
            if cleanup_error is not None:
                raise BackendError("stage_cleanup_failed", cleanup_error)
            self._require_nonpublishing_result_bindings(candidate)
            return self._receipt(
                "unchanged",
                new_size=self._old_size(),
                destination_mutated=False,
                new_identity=self.destination_identity,
            )

        self._hook("before_publish")
        try:
            self.publish_attempted = True
            published = self.backend.publish_staged_name(
                self._stage_fd(),
                self.candidate_name,
                self._candidate_identity(),
                self._destination_parent_fd(),
                self.destination_name,
                self.destination_identity,
                authorize_namespace=self._authorize_publish,
                validate_after_authorization=self._validate_publish_bindings,
            )
            self.published_identity = published
        except BackendError as exc:
            self._orient_publish_failure(exc)
            raise
        except Exception as exc:
            self._orient_publish_failure(exc)
            raise
        except BaseException as exc:
            self._orient_publish_failure(exc)
            raise
        self._hook("after_publish")
        cleanup_error = self._cleanup_stage()
        if cleanup_error is not None:
            raise BackendError("stage_cleanup_failed", cleanup_error)
        self._require_updated_result_binding(published)
        return self._receipt(
            "updated",
            new_size=complete_size,
            destination_mutated=True,
            new_identity=published,
        )

    def _validate_paths(self) -> None:
        for label, path in (
            ("source", self.source_path),
            ("destination", self.destination_path),
        ):
            if not isinstance(path, str) or not path.startswith("/"):
                raise BackendError("invalid_path", f"{label} path must be absolute")
        if self.source_path == self.destination_path:
            raise BackendError(
                "invalid_path", "source and destination paths must be distinct"
            )

    def _bind_source(self) -> None:
        try:
            parent_owner, self.source_name = self.backend._open_absolute_parent_owned(
                self.source_path
            )
            with parent_owner:
                self._install_fd_owner("_source_parent_owner", parent_owner)
                self.source_parent_identity = self.backend.validate_stage_container(
                    self._source_parent_fd()
                )
                source_owner = self.backend._dispatch_open_leaf_owned(
                    self._source_parent_fd(), self.source_name
                )
                with source_owner:
                    self._install_fd_owner("_source_owner", source_owner)
        except BackendError as exc:
            self._raise_transient_path("source_unavailable", exc)
        self.source_identity = self.backend.identity(self._source_fd())
        self.source_size = self.source_identity.size
        self.source_mtime_ns = self.source_identity.mtime_ns
        self.source_ctime_ns = self.source_identity.ctime_ns
        self._snapshot_source_policy("rollout source")
        self._require_source_mapping()

    def _bind_destination(self) -> None:
        try:
            parent_owner, self.destination_name = (
                self.backend._open_absolute_parent_owned(self.destination_path)
            )
            with parent_owner:
                self._install_fd_owner("_destination_parent_owner", parent_owner)
        except BackendError as exc:
            self._raise_transient_path("destination_unavailable", exc)
        self.destination_parent_identity = self.backend.validate_stage_container(
            self._destination_parent_fd()
        )
        self._require_destination_parent_mapping()
        try:
            destination_owner = self.backend._dispatch_open_leaf_owned(
                self._destination_parent_fd(), self.destination_name
            )
            with destination_owner:
                self._install_fd_owner("_destination_owner", destination_owner)
        except BackendError as exc:
            if exc.errno_value == errno.ENOENT:
                self.destination_identity = None
                self.destination_snapshot = None
                return
            if exc.reason == "not_regular" or exc.errno_value in _PATH_TRANSIENT_ERRNOS:
                raise DeferredSync("destination_unstable", exc.detail) from exc
            raise
        self.destination_identity = self.backend.identity(self.destination_fd)
        if self.destination_identity.is_same_object(self._source_identity()):
            raise BackendError(
                "invalid_destination", "source and destination alias the same object"
            )
        try:
            self.destination_snapshot = self.backend.snapshot_file(self.destination_fd)
        except BackendError as exc:
            if exc.reason in _SNAPSHOT_TRANSIENT_REASONS:
                raise DeferredSync("destination_unstable", exc.detail) from exc
            raise
        self.backend.require_exclusive_writer_policy(
            self.destination_snapshot.policy, "existing rollout mirror"
        )

    def _create_stage(self) -> None:
        self.stage_name = (
            f".{self.destination_name}.codex-stage.{os.getpid()}."
            f"{secrets.token_hex(12)}"
        )
        self.stage_path = os.path.join(
            os.path.dirname(self.destination_path), self.stage_name
        )
        stage_owner = self.backend._create_private_stage_parent_owned(
            self._destination_parent_fd(),
            self.stage_name,
            authorize_state=self._authorize_create_stage,
        )
        with stage_owner:
            self._install_fd_owner("_stage_owner", stage_owner)
            self.stage_identity = stage_owner.identity()
        self._require_stage_mapping()

    def _create_candidate(self) -> None:
        self._hook("before_clone")
        self._snapshot_source_policy("rollout source")
        try:
            candidate_owner = self.backend._strict_clone_owned(
                self._source_fd(),
                self._stage_fd(),
                self.candidate_name,
                authorize_state=self._authorize_create_clone,
                writable=True,
            )
            with candidate_owner:
                self._install_fd_owner("_candidate_owner", candidate_owner)
                self.method = "reflink"
        except BackendError as exc:
            if exc.reason == "clone_content_invalid":
                self._bind_partial_candidate_if_present()
                raise DeferredSync("source_unstable", exc.detail) from exc
            if (
                exc.reason != "clone_failed"
                or exc.errno_value not in _CLONE_COMPAT_ERRNOS
            ):
                raise
            self.backend._require_name_absent(self._stage_fd(), self.candidate_name)
            source_before = self._snapshot_source_file("ordinary-copy source")
            try:
                candidate_owner = self.backend._ordinary_copy_to_absent_owned(
                    self._source_fd(),
                    self._stage_fd(),
                    self.candidate_name,
                    authorize_state=self._authorize_create_copy,
                )
                with candidate_owner:
                    self._install_fd_owner("_candidate_owner", candidate_owner)
            except BackendError as copy_exc:
                self._bind_partial_candidate_if_present()
                if copy_exc.reason in _SNAPSHOT_TRANSIENT_REASONS:
                    raise DeferredSync("source_unstable", copy_exc.detail) from copy_exc
                raise
            self.method = "copy"
            expected = self.backend.snapshot_expectation(source_before)
            self.fallback_source_expectation = expected
            self.fallback_source_identity = source_before.identity
            try:
                source_after = self.backend.require_snapshot(
                    self._source_fd(),
                    expected,
                    "ordinary-copy source",
                    mismatch_reason="source_unstable",
                    changed_reason="source_unstable",
                    unreadable_reason="source_unreadable",
                )
            except BackendError as after_exc:
                raise DeferredSync("source_unstable", after_exc.detail) from after_exc
            if source_after.identity != source_before.identity:
                raise DeferredSync(
                    "source_unstable",
                    "ordinary-copy source generation changed after copying",
                )
            copied = self.backend.snapshot_file(self._candidate_fd())
            if (
                copied.identity.size != source_before.identity.size
                or copied.sha256 != source_before.sha256
                or copied.policy != source_before.policy
            ):
                raise BackendError(
                    "copy_postcondition_unverified",
                    "ordinary copy does not match the stable source snapshot",
                )
        self.candidate_identity = self.backend.identity(self._candidate_fd())
        self.backend.require_identity_at(
            self._stage_fd(), self.candidate_name, self._candidate_identity()
        )
        staged_source = self.backend.identity(self._source_fd())
        if (
            not staged_source.is_same_object(self._source_identity())
            or not stat.S_ISREG(staged_source.mode)
            or staged_source.nlink != 1
            or staged_source.size < self._candidate_identity().size
            or (self.source_size is not None and staged_source.size < self.source_size)
        ):
            raise DeferredSync(
                "source_unstable",
                "source changed incompatibly while staging the candidate",
            )
        if (
            self.source_size is not None
            and staged_source.size == self.source_size
            and (
                staged_source.mtime_ns != self.source_mtime_ns
                or staged_source.ctime_ns != self.source_ctime_ns
            )
        ):
            raise DeferredSync(
                "source_unstable",
                "same-sized source generation changed while staging",
            )
        if self.method == "copy" and staged_source != self.fallback_source_identity:
            self.fallback_source_drifted = True
            raise DeferredSync(
                "source_unstable",
                "ordinary-copy source changed after staging",
            )
        self.source_size = staged_source.size
        self.source_mtime_ns = staged_source.mtime_ns
        self.source_ctime_ns = staged_source.ctime_ns

    def _last_complete_line_size(self, fd: int, size: int) -> int:
        offset = size
        while offset > 0:
            start = max(0, offset - _READ_CHUNK)
            try:
                block = os.pread(fd, offset - start, start)
            except OSError as exc:
                raise BackendError(
                    "candidate_unreadable", f"pread staged candidate: {exc}", exc.errno
                ) from exc
            if len(block) != offset - start:
                raise BackendError(
                    "candidate_changed", "staged candidate reached unexpected EOF"
                )
            position = block.rfind(b"\n")
            if position >= 0:
                return start + position + 1
            offset = start
        return 0

    def _require_source_prefix(
        self,
        candidate: FileSnapshot,
        *,
        candidate_unlinked: bool = False,
        _attempt: int = 1,
    ) -> None:
        self._require_source_mapping()
        if self.method == "copy":
            if (
                self.fallback_source_expectation is None
                or self.fallback_source_identity is None
            ):
                raise BackendError(
                    "binding_incomplete",
                    "ordinary-copy source snapshot is unavailable",
                )
            try:
                fallback_entry = self.backend.require_snapshot(
                    self._source_fd(),
                    self.fallback_source_expectation,
                    "ordinary-copy source before publish",
                    mismatch_reason="source_unstable",
                    changed_reason="source_unstable",
                    unreadable_reason="source_unreadable",
                )
            except BackendError as exc:
                raise DeferredSync("source_unstable", exc.detail) from exc
            if fallback_entry.identity != self.fallback_source_identity:
                self.fallback_source_drifted = True
                raise DeferredSync(
                    "source_unstable",
                    "ordinary-copy source generation changed before validation",
                )
        source_before = self.backend.identity(self._source_fd())
        prior_size = self.source_size
        if prior_size is not None:
            if source_before.size < prior_size:
                raise DeferredSync(
                    "source_unstable",
                    "source entered validation below its observed high-water size",
                )
            if source_before.size == prior_size and (
                source_before.mtime_ns != self.source_mtime_ns
                or source_before.ctime_ns != self.source_ctime_ns
            ):
                raise DeferredSync(
                    "source_unstable",
                    "same-sized source generation changed before validation",
                )
        if source_before.size < candidate.identity.size:
            raise DeferredSync(
                "source_unstable", "source became shorter than the staged prefix"
            )
        offset = 0
        while offset < candidate.identity.size:
            wanted = min(_READ_CHUNK, candidate.identity.size - offset)
            try:
                source_block = os.pread(self._source_fd(), wanted, offset)
                candidate_block = os.pread(self._candidate_fd(), wanted, offset)
            except OSError as exc:
                raise DeferredSync("source_unreadable", str(exc)) from exc
            if len(source_block) != wanted or len(candidate_block) != wanted:
                raise DeferredSync(
                    "source_unstable", "source or candidate reached unexpected EOF"
                )
            if source_block != candidate_block:
                raise DeferredSync(
                    "source_unstable", "staged bytes are not a source prefix"
                )
            offset += wanted
        source_after = self.backend.identity(self._source_fd())
        if self.method == "copy" and (
            source_before != self.fallback_source_identity
            or source_after != self.fallback_source_identity
        ):
            self.fallback_source_drifted = True
        if (
            not source_after.is_same_object(source_before)
            or not stat.S_ISREG(source_after.mode)
            or source_after.nlink != 1
            or source_after.size < source_before.size
            or source_after.size < candidate.identity.size
        ):
            raise DeferredSync(
                "source_unstable", "source identity or size changed incompatibly"
            )
        if self.source_size is not None:
            if source_after.size < self.source_size:
                raise DeferredSync(
                    "source_unstable",
                    "source became shorter than a previously observed size",
                )
            if source_after.size == self.source_size and (
                source_after.mtime_ns != self.source_mtime_ns
                or source_after.ctime_ns != self.source_ctime_ns
            ):
                raise DeferredSync(
                    "source_unstable",
                    "same-sized source generation changed between validations",
                )
        self.source_size = source_after.size
        self.source_mtime_ns = source_after.mtime_ns
        self.source_ctime_ns = source_after.ctime_ns
        source_policy = self._snapshot_source_policy("rollout source")
        if _policy_access_key(source_policy) != _policy_access_key(candidate.policy):
            raise DeferredSync(
                "source_unstable", "source access policy changed after staging"
            )
        if source_after.size == source_before.size and (
            source_after.mtime_ns != source_before.mtime_ns
            or source_after.ctime_ns != source_before.ctime_ns
        ):
            raise DeferredSync(
                "source_unstable", "same-sized source changed while comparing"
            )
        if source_after.size == candidate.identity.size:
            if source_policy != candidate.policy:
                raise DeferredSync(
                    "source_unstable",
                    "complete candidate does not preserve current source policy",
                )
        if candidate_unlinked:
            final_candidate = self.backend.snapshot_unlinked_file(self._candidate_fd())
            if (
                self.backend.snapshot_expectation(final_candidate)
                != self._candidate_expectation()
            ):
                raise BackendError(
                    "candidate_changed",
                    "unlinked held candidate changed during final validation",
                )
        else:
            self.backend.require_snapshot(
                self._candidate_fd(),
                self._candidate_expectation(),
                "staged rollout candidate",
                mismatch_reason="candidate_changed",
            )
        self._require_source_mapping()
        self._hook("before_source_prefix_return")
        if self.method == "copy":
            terminal_before_snapshot = self.backend.identity(self._source_fd())
            if (
                not terminal_before_snapshot.is_same_object(source_after)
                or not stat.S_ISREG(terminal_before_snapshot.mode)
                or terminal_before_snapshot.nlink != 1
                or self.source_size is None
                or terminal_before_snapshot.size < self.source_size
            ):
                raise DeferredSync(
                    "source_unstable",
                    "ordinary-copy source changed incompatibly at terminal boundary",
                )
            if terminal_before_snapshot != self.fallback_source_identity:
                self.fallback_source_drifted = True
            if terminal_before_snapshot.size > self.source_size:
                self.source_size = terminal_before_snapshot.size
                self.source_mtime_ns = terminal_before_snapshot.mtime_ns
                self.source_ctime_ns = terminal_before_snapshot.ctime_ns
            try:
                final_source = self.backend.require_snapshot(
                    self._source_fd(),
                    self.fallback_source_expectation,
                    "ordinary-copy source at terminal boundary",
                    mismatch_reason="source_unstable",
                    changed_reason="source_unstable",
                    unreadable_reason="source_unreadable",
                )
            except BackendError as exc:
                raise DeferredSync("source_unstable", exc.detail) from exc
            if (
                self.source_size is None
                or final_source.identity.size < self.source_size
                or final_source.identity != self.fallback_source_identity
            ):
                self.fallback_source_drifted = True
            if self.fallback_source_drifted:
                raise DeferredSync(
                    "source_unstable",
                    "ordinary-copy source drift was observed during validation",
                )
            self.source_size = final_source.identity.size
            self.source_mtime_ns = final_source.identity.mtime_ns
            self.source_ctime_ns = final_source.identity.ctime_ns
            return
        terminal = self.backend.identity(self._source_fd())
        if (
            not terminal.is_same_object(source_after)
            or not stat.S_ISREG(terminal.mode)
            or terminal.nlink != 1
            or self.source_size is None
            or terminal.size < self.source_size
        ):
            raise DeferredSync(
                "source_unstable",
                "source identity or size changed at the terminal validation boundary",
            )
        if terminal.size == self.source_size and (
            terminal.mtime_ns != self.source_mtime_ns
            or terminal.ctime_ns != self.source_ctime_ns
        ):
            raise DeferredSync(
                "source_unstable",
                "same-sized source generation changed at the terminal boundary",
            )
        self.source_size = terminal.size
        self.source_mtime_ns = terminal.mtime_ns
        self.source_ctime_ns = terminal.ctime_ns
        grew_during_attempt = (
            (prior_size is not None and source_before.size > prior_size)
            or source_after.size > source_before.size
            or terminal.size > source_after.size
        )
        if grew_during_attempt:
            if _attempt >= _SOURCE_PREFIX_MAX_ATTEMPTS:
                raise DeferredSync(
                    "source_unstable",
                    "source kept growing across bounded prefix validation",
                )
            return self._require_source_prefix(
                candidate,
                candidate_unlinked=candidate_unlinked,
                _attempt=_attempt + 1,
            )

    def _destination_is_unchanged(self, candidate: FileSnapshot) -> bool:
        if self.destination_fd < 0 or self.destination_snapshot is None:
            return False
        actual = self.backend.require_snapshot(
            self.destination_fd,
            self.backend.snapshot_expectation(self.destination_snapshot),
            "existing rollout mirror",
            mismatch_reason="destination_unstable",
            changed_reason="destination_unstable",
            unreadable_reason="destination_unreadable",
        )
        return (
            actual.identity.size == candidate.identity.size
            and actual.sha256 == candidate.sha256
            and actual.policy == candidate.policy
        )

    def _authorize_create_stage(self, _action: str) -> None:
        self._hook("authorize_create_stage")
        self._require_source_mapping()
        self._require_destination_binding(include_stage=False)

    def _authorize_create_clone(self, _action: str) -> None:
        self._hook("authorize_create_clone")
        self._require_source_mapping()
        self._require_destination_binding(include_stage=True)
        self.backend._require_name_absent(self._stage_fd(), self.candidate_name)

    def _authorize_create_copy(self, _action: str) -> None:
        self._hook("authorize_create_copy")
        self._require_source_mapping()
        self._require_destination_binding(include_stage=True)
        self.backend._require_name_absent(self._stage_fd(), self.candidate_name)

    def _authorize_publish(self, _action: str) -> None:
        self._hook("authorize_publish")
        self._validate_publish_bindings()

    def _validate_publish_bindings(self) -> None:
        self._require_destination_binding(include_stage=True)
        self.backend.require_snapshot(
            self._candidate_fd(),
            self._candidate_expectation(),
            "staged rollout candidate before publish",
            mismatch_reason="candidate_changed",
        )
        candidate = self.backend.snapshot_file(self._candidate_fd())
        self._require_source_prefix(candidate)

    def _require_nonpublishing_result_bindings(self, candidate: FileSnapshot) -> None:
        """Bind the final point-in-time receipt after stage cleanup."""
        unlinked = self.backend.snapshot_unlinked_file(self._candidate_fd())
        if (
            not unlinked.identity.is_same_object(candidate.identity)
            or unlinked.identity.size != candidate.identity.size
            or unlinked.sha256 != candidate.sha256
            or unlinked.policy != candidate.policy
        ):
            raise BackendError(
                "candidate_changed",
                "unlinked held candidate differs from its pre-cleanup snapshot",
            )
        self.candidate_identity = unlinked.identity
        self.candidate_expectation = self.backend.snapshot_expectation(unlinked)
        candidate = unlinked
        self._require_destination_binding(include_stage=False)
        self._require_source_prefix(candidate, candidate_unlinked=True)

    def _require_updated_result_binding(self, expected: FileIdentity) -> None:
        """Bind the canonical published leaf and its full protected snapshot."""
        self._require_destination_parent_mapping()
        published_owner = self.backend._dispatch_open_leaf_owned(
            self._destination_parent_fd(), self.destination_name
        )
        with published_owner:
            published_fd = published_owner.fileno()
            actual = self.backend.identity(published_owner.fileno())
            if not actual.is_same_object(expected):
                raise BackendError(
                    "publish_result_replaced",
                    "canonical destination no longer maps to the published inode",
                )
            self.backend.require_snapshot(
                published_fd,
                self._candidate_expectation(),
                "canonical published rollout",
                mismatch_reason="publish_result_changed",
            )
            self.backend.require_identity_at(
                self._destination_parent_fd(), self.destination_name, expected
            )
            self._require_destination_parent_mapping()
            close_receipt = BackendError(
                "close_failed", "canonical published FD close did not complete"
            )
            durable_close_recovery_required = False
            published_owner.close(
                primary_error=close_receipt,
                durable_namespace_complete=True,
            )
            if not published_owner.closed:
                durable_close_recovery_required = True
                published_owner.close(
                    primary_error=close_receipt,
                    durable_namespace_complete=True,
                )
            if durable_close_recovery_required:
                if published_owner.closed:
                    status = "canonical published FD required a recovery close"
                else:
                    status = (
                        "canonical published FD remained open after two close attempts"
                    )
                raise BackendError(
                    "close_failed",
                    _combine_diagnostics(
                        status,
                        *_cleanup_diagnostic_parts(close_receipt),
                    ),
                )

    def _prove_pre_publish_orientation(self) -> bool:
        """Prove a failed rename left the exact bound before-orientation."""
        try:
            self._require_destination_binding(include_stage=True)
            self.backend.require_identity_at(
                self._stage_fd(), self.candidate_name, self._candidate_identity()
            )
            self.backend.require_snapshot(
                self._candidate_fd(),
                self._candidate_expectation(),
                "staged rollout candidate after failed publish",
                mismatch_reason="candidate_changed",
            )
            return True
        except Exception:
            return False

    def _orient_publish_failure(self, primary: BaseException) -> None:
        """Classify a failed publish without replacing its first exception."""
        try:
            if self._prove_pre_publish_orientation():
                self.publish_attempted = False
                return
            if self._adopt_published_orientation_if_proved():
                return
            _attach_cleanup_diagnostic(
                primary,
                "cleanup skipped; orientation unverified",
                context=None,
            )
        except BaseException as probe_exc:
            _attach_cleanup_diagnostic(
                primary,
                _exception_diagnostic(probe_exc),
                context="publish orientation probe failed",
            )

    def _adopt_published_orientation_if_proved(self) -> bool:
        """Record a committed mapping after a postcondition error, if exact."""
        try:
            self._require_destination_parent_mapping()
            self._require_stage_mapping()
            published = self.backend.require_identity_at(
                self._destination_parent_fd(),
                self.destination_name,
                self._candidate_identity(),
            )
            self.backend._require_name_absent(self._stage_fd(), self.candidate_name)
            self.backend.require_snapshot(
                self._candidate_fd(),
                self._candidate_expectation(),
                "published rollout candidate after postcondition failure",
                mismatch_reason="candidate_changed",
            )
        except Exception:
            return False
        self.published_identity = published
        return True

    def _require_source_mapping(self) -> None:
        expected_parent = self._source_parent_identity()
        held_parent = self.backend.validate_stage_container(self._source_parent_fd())
        if not held_parent.is_same_object(expected_parent):
            raise DeferredSync("source_parent_replaced", "held source parent changed")
        reopened_owner, reopened_name = self.backend._open_absolute_parent_owned(
            self.source_path
        )
        entered = False
        try:
            with reopened_owner:
                entered = True
                reopened_identity = self.backend.validate_stage_container(
                    reopened_owner.fileno()
                )
                reopened_owner.close()
        except BackendError as exc:
            if not entered:
                raise DeferredSync("source_parent_replaced", exc.detail) from exc
            raise
        if reopened_name != self.source_name or not reopened_identity.is_same_object(
            expected_parent
        ):
            raise DeferredSync(
                "source_parent_replaced", "absolute source parent mapping changed"
            )
        try:
            self.backend.require_identity_at(
                self._source_parent_fd(), self.source_name, self._source_identity()
            )
        except BackendError as exc:
            raise DeferredSync("source_object_replaced", exc.detail) from exc

    def _require_destination_parent_mapping(self) -> None:
        expected = self._destination_parent_identity()
        held = self.backend.validate_stage_container(self._destination_parent_fd())
        if not held.is_same_object(expected):
            raise DeferredSync(
                "destination_parent_replaced", "held destination parent changed"
            )
        reopened_owner, reopened_name = self.backend._open_absolute_parent_owned(
            self.destination_path
        )
        entered = False
        try:
            with reopened_owner:
                entered = True
                reopened = self.backend.validate_stage_container(
                    reopened_owner.fileno()
                )
                reopened_owner.close()
        except BackendError as exc:
            if not entered:
                raise DeferredSync("destination_parent_replaced", exc.detail) from exc
            raise
        if reopened_name != self.destination_name or not reopened.is_same_object(
            expected
        ):
            raise DeferredSync(
                "destination_parent_replaced",
                "absolute destination parent mapping changed",
            )

    def _require_destination_binding(self, *, include_stage: bool) -> None:
        self._require_destination_parent_mapping()
        if self.destination_identity is None:
            try:
                self.backend._require_name_absent(
                    self._destination_parent_fd(), self.destination_name
                )
            except BackendError as exc:
                raise DeferredSync("destination_replaced", exc.detail) from exc
        else:
            try:
                self.backend.require_identity_at(
                    self._destination_parent_fd(),
                    self.destination_name,
                    self.destination_identity,
                )
                if self.destination_snapshot is not None:
                    self.backend.require_snapshot(
                        self._destination_fd(),
                        self.backend.snapshot_expectation(self.destination_snapshot),
                        "existing rollout mirror before publish",
                        mismatch_reason="destination_unstable",
                        changed_reason="destination_unstable",
                        unreadable_reason="destination_unreadable",
                    )
            except BackendError as exc:
                raise DeferredSync("destination_unstable", exc.detail) from exc
        if include_stage:
            self._require_stage_mapping()

    def _require_stage_mapping(self) -> None:
        if self.stage_removed:
            raise BackendError("stage_missing", "private stage was already removed")
        expected = self._stage_identity()
        self.backend.require_directory_identity_at(
            self._destination_parent_fd(), self.stage_name, expected
        )
        actual = self.backend.validate_private_stage_parent(self._stage_fd())
        if not actual.is_same_object(expected):
            raise BackendError("stage_replaced", "held private stage changed identity")

    def _cleanup_stage(
        self,
        *,
        keep_candidate_open: bool = False,
        primary_error: Optional[BaseException] = None,
    ) -> Optional[str]:
        if self.stage_fd < 0 or self.stage_removed:
            return None
        try:
            self._run_before_cleanup_hook()
            self._require_destination_parent_mapping()
            self._require_stage_mapping()
            entries = tuple(sorted(os.listdir(self._stage_fd())))
            if self.published_identity is not None:
                if entries:
                    raise BackendError(
                        "stage_not_cleanable",
                        f"published stage contains unexpected entries: {entries!r}",
                    )
            else:
                if entries == (self.candidate_name,):
                    self._bind_partial_candidate_if_present()
                    expected = self._candidate_identity()

                    def validate_child() -> None:
                        self._require_destination_parent_mapping()
                        self._require_stage_mapping()
                        self.backend.require_identity_at(
                            self._stage_fd(), self.candidate_name, expected
                        )

                    self.backend.unlink_name(
                        self._stage_fd(),
                        self.candidate_name,
                        expected,
                        authorize_state=self._authorize_cleanup_child,
                        action="cleanup_candidate",
                        validate_after_authorization=validate_child,
                    )
                elif entries:
                    raise BackendError(
                        "stage_not_cleanable",
                        f"private stage contains unexpected entries: {entries!r}",
                    )
            if not keep_candidate_open:
                self._close_candidate()
            self.backend.remove_empty_private_stage(
                self.stage_path,
                self._stage_identity(),
                expected_container=self._destination_parent_identity(),
                authorize_state=self._authorize_cleanup_stage,
            )
            self.stage_removed = True
            return None
        except BaseException as exc:
            diagnostic = _exception_diagnostic(exc)
            if primary_error is not None:
                _attach_cleanup_diagnostic(
                    primary_error,
                    diagnostic,
                    context="identity-bound cleanup failed",
                )
            elif not isinstance(exc, Exception):
                try:
                    _cleanup_dispatch = 2
                    self._cleanup_stage(
                        keep_candidate_open=keep_candidate_open,
                        primary_error=exc,
                    )
                except BaseException as retry_exc:
                    _attach_cleanup_diagnostic(
                        exc,
                        _exception_diagnostic(retry_exc),
                        context="identity-bound cleanup retry failed",
                    )
                raise
            return diagnostic

    def _run_before_cleanup_hook(self) -> None:
        if self.cleanup_hook_ran:
            return
        self.cleanup_hook_ran = True
        self._hook("before_cleanup")

    def _authorize_cleanup_child(self, _action: str) -> None:
        self._hook("authorize_cleanup_child")
        self._require_destination_parent_mapping()
        self._require_stage_mapping()

    def _authorize_cleanup_stage(self, _action: str) -> None:
        self._hook("authorize_cleanup_stage")
        self._require_destination_parent_mapping()

    def _bind_partial_candidate_if_present(self) -> None:
        if self.candidate_fd >= 0:
            if self.candidate_identity is None:
                self.candidate_identity = self.backend.identity(self._candidate_fd())
            return
        owner = self.backend._dispatch_open_leaf_owned(
            self._stage_fd(), self.candidate_name
        )
        entered = False
        try:
            with owner:
                entered = True
                self._install_fd_owner("_candidate_owner", owner)
                self.candidate_identity = self.backend.identity(self._candidate_fd())
                if self.candidate_identity.uid != os.geteuid():
                    raise BackendError(
                        "candidate_untrusted",
                        "partial candidate is not owned by euid",
                    )
                self.backend.require_identity_at(
                    self._stage_fd(), self.candidate_name, self.candidate_identity
                )
        except BackendError as exc:
            if not entered and exc.errno_value == errno.ENOENT:
                return
            raise

    def _raise_transient_path(self, reason: str, exc: BackendError) -> None:
        if exc.reason == "not_regular" or exc.errno_value in _PATH_TRANSIENT_ERRNOS:
            raise DeferredSync(reason, exc.detail) from exc
        raise exc

    def _snapshot_source_policy(self, subject: str) -> FilePolicy:
        try:
            policy = self.backend.snapshot_policy(self._source_fd())
            return self.backend.require_exclusive_writer_policy(policy, subject)
        except BackendError as exc:
            if exc.reason in _SNAPSHOT_TRANSIENT_REASONS:
                raise DeferredSync("source_unstable", exc.detail) from exc
            raise

    def _snapshot_source_file(self, subject: str) -> FileSnapshot:
        try:
            snapshot = self.backend.snapshot_file(self._source_fd())
            self.backend.require_exclusive_writer_policy(snapshot.policy, subject)
            return snapshot
        except BackendError as exc:
            if exc.reason in _SNAPSHOT_TRANSIENT_REASONS:
                raise DeferredSync("source_unstable", exc.detail) from exc
            raise

    def _hook(self, stage: str) -> None:
        if self.action_hook is not None:
            self.action_hook(stage)

    def _receipt(
        self,
        outcome: str,
        *,
        new_size: Optional[int],
        destination_mutated: Optional[bool],
        reason: Optional[str] = None,
        detail: Optional[str] = None,
        new_identity: Optional[FileIdentity] = None,
    ) -> SyncReceipt:
        if new_identity is None and destination_mutated is False:
            new_identity = self.destination_identity
        return SyncReceipt(
            version=RECEIPT_VERSION,
            command="sync-one",
            outcome=outcome,
            source=self.source_path,
            destination=self.destination_path,
            old_size=self._old_size(),
            new_size=new_size,
            publish_size=self.publish_size,
            source_size=self.source_size,
            method=self.method,
            partial=self.partial,
            mtime_semantics=self.mtime_semantics,
            old_identity=_identity_dict(self.destination_identity),
            new_identity=_identity_dict(new_identity),
            destination_mutated=destination_mutated,
            reason=reason,
            detail=_bounded_diagnostic(detail) if detail is not None else None,
        )

    def _fatal_receipt(self, reason: str, detail: str) -> SyncReceipt:
        if self.published_identity is not None:
            mutated: Optional[bool] = True
            new_size = self.publish_size
            new_identity = self.published_identity
        elif self.publish_attempted:
            mutated = None
            new_size = None
            new_identity = None
        else:
            mutated = False
            new_size = self._old_size()
            new_identity = self.destination_identity
        return self._receipt(
            "fatal",
            new_size=new_size,
            destination_mutated=mutated,
            reason=reason,
            detail=detail,
            new_identity=new_identity,
        )

    def _old_size(self) -> Optional[int]:
        if self.destination_snapshot is None:
            return None
        return self.destination_snapshot.identity.size

    def _install_fd_owner(self, attribute: str, owner: _OwnedFD) -> None:
        current = getattr(self, attribute)
        if current is not None and not current.closed:
            raise BackendError(
                "fd_already_owned", f"transaction slot {attribute!r} is already open"
            )
        owner.retain_if_registered(lambda: getattr(self, attribute, None) is owner)
        setattr(self, attribute, owner)

    def _close_candidate(self) -> None:
        owner = self._candidate_owner
        if owner is None:
            return
        owner.close()
        self._candidate_owner = None

    def _has_open_fd_owners(self) -> bool:
        return any(
            owner is not None and not owner.closed
            for owner in (
                self._candidate_owner,
                self._stage_owner,
                self._destination_owner,
                self._destination_parent_owner,
                self._source_owner,
                self._source_parent_owner,
            )
        )

    def _consume_latched_close_failure(
        self,
        active_failure: Dict[str, object],
        first_failure: Dict[str, object],
        errors: list,
        interruption: BaseException,
        *,
        primary_error: Optional[BaseException],
    ) -> None:
        """Preserve a close failure replaced while its handler was running."""
        if not active_failure:
            return
        label = active_failure.get("label")
        retry = active_failure.get("retry") is True
        owner = active_failure.get("owner")
        latched_failure = active_failure.get("failure")
        if (
            type(latched_failure) is tuple
            and len(latched_failure) == 2
            and isinstance(latched_failure[0], BaseException)
        ):
            failure, failure_traceback = latched_failure
        else:
            failure = None
            failure_traceback = None
        if not isinstance(failure, BaseException):
            try:
                context = BaseException.__getattribute__(interruption, "__context__")
            except BaseException:
                context = None
            if isinstance(context, BaseException) and context is not primary_error:
                try:
                    context_traceback = BaseException.__getattribute__(
                        context, "__traceback__"
                    )
                except BaseException:
                    context_traceback = None
                traceback_cursor = context_traceback
                for _depth in range(64):
                    if traceback_cursor is None:
                        break
                    try:
                        frame_locals = traceback_cursor.tb_frame.f_locals
                        if (
                            frame_locals.get("self") is self
                            and frame_locals.get("owner") is owner
                            and frame_locals.get("active_failure") is active_failure
                        ):
                            failure = context
                            failure_traceback = context_traceback
                            break
                        traceback_cursor = traceback_cursor.tb_next
                    except BaseException:
                        break
        if isinstance(label, str) and isinstance(failure, BaseException):
            suffix = " retry" if retry else ""
            segment = _diagnostic_segment(
                f"{label}{suffix}",
                _exception_diagnostic(failure),
            )
            errors.append(segment)
            if not first_failure:
                first_failure["record"] = (
                    label,
                    retry,
                    failure,
                    failure_traceback,
                )
            active_failure["failure"] = (failure, failure_traceback)
        active_failure.clear()

    def _close_all_pass(
        self,
        entries: Tuple[Tuple[str, str, _OwnedFD], ...],
        errors: list,
        active_failure: Dict[str, object],
        first_failure: Dict[str, object],
        *,
        retry: bool,
    ) -> None:
        """Run one complete owner drain and slot-reconciliation pass."""
        for _attribute, label, owner in entries:
            if owner.closed:
                continue
            active_failure.update(
                label=label,
                retry=retry,
                owner=owner,
                failure=None,
            )
            _owner_close_attempt = 1
            close_started = False
            try:
                # fmt: off
                close_started = True; owner.close()  # noqa: E702
            # fmt: on
            except BaseException as exc:
                if not close_started:
                    raise
                failure_traceback = sys.exc_info()[2]
                active_failure["failure"] = (exc, failure_traceback)
                if not first_failure:
                    first_failure["record"] = (
                        label,
                        retry,
                        exc,
                        failure_traceback,
                    )
                suffix = " retry" if retry else ""
                errors.append(
                    _diagnostic_segment(
                        f"{label}{suffix}",
                        _exception_diagnostic(exc),
                    )
                )
            active_failure.clear()

        for attribute, label, owner in entries:
            if owner.closed:
                if getattr(self, attribute) is owner:
                    setattr(self, attribute, None)
            elif retry:
                errors.append(
                    _diagnostic_segment(
                        label,
                        "owner remained open after close retry",
                    )
                )

    def _close_all(self, *, primary_error: Optional[BaseException]) -> Optional[str]:
        errors = self._owner_close_diagnostics
        error_start = len(errors)
        entries: Tuple[Tuple[str, str, _OwnedFD], ...] = ()
        active_failure: Dict[str, object] = {}
        first_failure: Dict[str, object] = {}
        cleanup_was_interrupted = False
        owner_recovery_required = False
        try:
            _cleanup_dispatch = 1
            entries = tuple(
                (attribute, label, owner)
                for attribute, label in (
                    ("_candidate_owner", "candidate_fd"),
                    ("_stage_owner", "stage_fd"),
                    ("_destination_owner", "destination_fd"),
                    ("_destination_parent_owner", "destination_parent_fd"),
                    ("_source_owner", "source_fd"),
                    ("_source_parent_owner", "source_parent_fd"),
                )
                if (owner := getattr(self, attribute)) is not None
            )
            self._close_all_pass(
                entries,
                errors,
                active_failure,
                first_failure,
                retry=False,
            )
        except BaseException as cleanup_interruption:
            cleanup_was_interrupted = True
            self._consume_latched_close_failure(
                active_failure,
                first_failure,
                errors,
                cleanup_interruption,
                primary_error=primary_error,
            )
            errors.append(
                _diagnostic_segment(
                    "owned-FD cleanup interruption",
                    _exception_diagnostic(cleanup_interruption),
                )
            )
            if primary_error is not None:
                _attach_cleanup_diagnostic(
                    primary_error, _exception_diagnostic(cleanup_interruption)
                )

        try:
            _cleanup_dispatch = 2
            entries = tuple(
                (attribute, label, owner)
                for attribute, label in (
                    ("_candidate_owner", "candidate_fd"),
                    ("_stage_owner", "stage_fd"),
                    ("_destination_owner", "destination_fd"),
                    ("_destination_parent_owner", "destination_parent_fd"),
                    ("_source_owner", "source_fd"),
                    ("_source_parent_owner", "source_parent_fd"),
                )
                if (owner := getattr(self, attribute)) is not None
            )
            self._close_all_pass(
                entries,
                errors,
                active_failure,
                first_failure,
                retry=True,
            )
        except BaseException as retry_interruption:
            cleanup_was_interrupted = True
            self._consume_latched_close_failure(
                active_failure,
                first_failure,
                errors,
                retry_interruption,
                primary_error=primary_error,
            )
            errors.append(
                _diagnostic_segment(
                    "owned-FD cleanup retry interruption",
                    _exception_diagnostic(retry_interruption),
                )
            )
            if primary_error is not None:
                _attach_cleanup_diagnostic(
                    primary_error, _exception_diagnostic(retry_interruption)
                )
        try:
            _cleanup_dispatch = 3
            owner_recovery_required = self._has_open_fd_owners()
            self._owner_recovery_required = (
                self._owner_recovery_required or owner_recovery_required
            )
            if cleanup_was_interrupted or owner_recovery_required:
                entries = tuple(
                    (attribute, label, owner)
                    for attribute, label in (
                        ("_candidate_owner", "candidate_fd"),
                        ("_stage_owner", "stage_fd"),
                        ("_destination_owner", "destination_fd"),
                        ("_destination_parent_owner", "destination_parent_fd"),
                        ("_source_owner", "source_fd"),
                        ("_source_parent_owner", "source_parent_fd"),
                    )
                    if (owner := getattr(self, attribute)) is not None
                )
                self._close_all_pass(
                    entries,
                    errors,
                    active_failure,
                    first_failure,
                    retry=True,
                )
        except BaseException as recovery_interruption:
            self._consume_latched_close_failure(
                active_failure,
                first_failure,
                errors,
                recovery_interruption,
                primary_error=primary_error,
            )
            errors.append(
                _diagnostic_segment(
                    "owned-FD cleanup recovery interruption",
                    _exception_diagnostic(recovery_interruption),
                )
            )
            if primary_error is not None:
                _attach_cleanup_diagnostic(
                    primary_error, _exception_diagnostic(recovery_interruption)
                )
        try:
            _cleanup_dispatch = 4
            owners_remain_open = self._has_open_fd_owners()
            current_errors = tuple(errors[error_start:])
            if current_errors:
                detail = _RenderedOwnerDiagnostics(
                    _combine_owner_diagnostics(*current_errors)
                )
                if primary_error is not None:
                    _attach_owner_cleanup_diagnostics(primary_error, current_errors)
                    return detail
                elif (
                    not self.stage_removed
                    or owners_remain_open
                    or self._owner_recovery_required
                ):
                    return detail
            return None
        except BaseException as finalization_interruption:
            finalization_segment = _diagnostic_segment(
                "owned-FD cleanup finalization interruption",
                _exception_diagnostic(finalization_interruption),
            )
            if primary_error is None:
                record = first_failure.get("record")
                if (
                    type(record) is tuple
                    and len(record) == 4
                    and isinstance(record[0], str)
                    and isinstance(record[2], BaseException)
                ):
                    failure_label, failure_retry, failure, failure_traceback = record
                    suffix = " retry" if failure_retry is True else ""
                    _attach_cleanup_diagnostic(
                        failure,
                        _exception_diagnostic(finalization_interruption),
                        context="owned-FD cleanup finalization interrupted",
                    )
                    current_errors = tuple(errors[error_start:])
                    if current_errors:
                        _attach_owner_cleanup_diagnostics(failure, current_errors)
                    else:
                        _attach_cleanup_diagnostic(
                            failure,
                            (
                                f"{failure_label}{suffix}: "
                                f"{_exception_diagnostic(failure)}"
                            ),
                        )
                    raise BaseException.with_traceback(
                        failure, failure_traceback
                    ) from None
                raise
            errors.append(finalization_segment)
            current_errors = tuple(errors[error_start:])
            detail = _RenderedOwnerDiagnostics(
                _combine_owner_diagnostics(*current_errors)
            )
            _attach_cleanup_diagnostic(
                primary_error, _exception_diagnostic(finalization_interruption)
            )
            _attach_owner_cleanup_diagnostics(primary_error, current_errors)
            return detail

    def _source_fd(self) -> int:
        if self.source_fd < 0:
            raise BackendError("binding_incomplete", "source FD is unavailable")
        return self.source_fd

    def _source_parent_fd(self) -> int:
        if self.source_parent_fd < 0:
            raise BackendError("binding_incomplete", "source parent FD is unavailable")
        return self.source_parent_fd

    def _destination_fd(self) -> int:
        if self.destination_fd < 0:
            raise BackendError("binding_incomplete", "destination FD is unavailable")
        return self.destination_fd

    def _destination_parent_fd(self) -> int:
        if self.destination_parent_fd < 0:
            raise BackendError(
                "binding_incomplete", "destination parent FD is unavailable"
            )
        return self.destination_parent_fd

    def _stage_fd(self) -> int:
        if self.stage_fd < 0:
            raise BackendError("binding_incomplete", "stage FD is unavailable")
        return self.stage_fd

    def _candidate_fd(self) -> int:
        if self.candidate_fd < 0:
            raise BackendError("binding_incomplete", "candidate FD is unavailable")
        return self.candidate_fd

    def _source_identity(self) -> FileIdentity:
        if self.source_identity is None:
            raise BackendError("binding_incomplete", "source identity is unavailable")
        return self.source_identity

    def _source_parent_identity(self) -> FileIdentity:
        if self.source_parent_identity is None:
            raise BackendError(
                "binding_incomplete", "source parent identity is unavailable"
            )
        return self.source_parent_identity

    def _destination_parent_identity(self) -> FileIdentity:
        if self.destination_parent_identity is None:
            raise BackendError(
                "binding_incomplete", "destination parent identity is unavailable"
            )
        return self.destination_parent_identity

    def _stage_identity(self) -> FileIdentity:
        if self.stage_identity is None and self._stage_owner is not None:
            identity = getattr(self._stage_owner, "identity", None)
            if callable(identity):
                return identity()
        if self.stage_identity is None:
            raise BackendError("binding_incomplete", "stage identity is unavailable")
        return self.stage_identity

    def _candidate_identity(self) -> FileIdentity:
        if self.candidate_identity is None:
            raise BackendError(
                "binding_incomplete", "candidate identity is unavailable"
            )
        return self.candidate_identity

    def _candidate_expectation(self) -> SnapshotExpectation:
        if self.candidate_expectation is None:
            raise BackendError(
                "binding_incomplete", "candidate snapshot is unavailable"
            )
        return self.candidate_expectation


def _startup_fatal_receipt(
    source: str,
    destination: str,
    reason: str,
    detail: str,
    *,
    invalid_reason: str = "unexpected_error",
) -> SyncReceipt:
    normalized_reason = _exact_diagnostic_text(reason)
    safe_detail = _exact_diagnostic_text(detail) or "<unprintable>"
    if (
        not normalized_reason
        or len(str.encode(normalized_reason, "ascii", errors="replace")) > _REASON_LIMIT
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in normalized_reason
        )
    ):
        reason_summary = normalized_reason or "<unprintable>"
        safe_detail = _combine_diagnostics(
            f"invalid startup reason ({reason_summary})",
            safe_detail,
        )
        safe_reason = invalid_reason
    else:
        safe_reason = normalized_reason
    return SyncReceipt(
        version=RECEIPT_VERSION,
        command="sync-one",
        outcome="fatal",
        source=source,
        destination=destination,
        old_size=None,
        new_size=None,
        publish_size=None,
        source_size=None,
        method=None,
        partial=None,
        mtime_semantics=None,
        old_identity=None,
        new_identity=None,
        destination_mutated=False,
        reason=safe_reason,
        detail=_bounded_diagnostic(safe_detail),
    )


def sync_one(
    source: str,
    destination: str,
    *,
    backend_factory: Callable[[], DarwinBackend] = DarwinBackend,
    action_hook: Optional[Callable[[str], None]] = None,
) -> SyncReceipt:
    try:
        backend = backend_factory()
    except BackendError as exc:
        return _startup_fatal_receipt(
            source,
            destination,
            exc.reason,
            exc.detail,
            invalid_reason="backend_initialization_failed",
        )
    except Exception as exc:
        return _startup_fatal_receipt(
            source,
            destination,
            "unexpected_error",
            _exception_diagnostic(exc),
        )
    return MirrorSync(backend, action_hook=action_hook).sync_one(source, destination)


def _cli_hook(source: str, destination: str) -> Optional[Callable[[str], None]]:
    hook = os.environ.get("CODEX_ROLLOUT_MIRROR_TEST_HOOK", "")
    if not hook:
        return None
    if not os.path.isabs(hook):
        raise BackendError("invalid_test_hook", "test hook path must be absolute")

    def run(stage: str) -> None:
        try:
            subprocess.run(
                [hook, stage, source, destination],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BackendError(
                "test_hook_failed", f"test hook failed at {stage}: {exc}"
            ) from exc

    return run


def _validate_receipt_payload(
    payload: object, source: str, destination: str, exit_status: int
) -> SyncReceipt:
    if not isinstance(payload, dict):
        raise ValueError("receipt must be a JSON object")
    expected_keys = set(SyncReceipt.__dataclass_fields__)
    if set(payload) != expected_keys:
        raise ValueError("receipt keys do not match schema version 1")
    version = payload.get("version")
    if (
        type(version) is not int
        or version != RECEIPT_VERSION
        or payload.get("command") != "sync-one"
    ):
        raise ValueError("receipt version or command is invalid")
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOME_EXITS:
        raise ValueError("receipt outcome is invalid")
    if _OUTCOME_EXITS[outcome] != exit_status:
        raise ValueError("receipt outcome does not match exit status")
    if payload.get("source") != source or payload.get("destination") != destination:
        raise ValueError("receipt paths do not match the invocation")
    for key in ("old_size", "new_size", "publish_size", "source_size"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"receipt {key} is invalid")
    method = payload.get("method")
    if method not in (None, "reflink", "copy"):
        raise ValueError("receipt method is invalid")
    partial = payload.get("partial")
    if partial is not None and not isinstance(partial, bool):
        raise ValueError("receipt partial is invalid")
    mutated = payload.get("destination_mutated")
    if mutated is not None and not isinstance(mutated, bool):
        raise ValueError("receipt destination_mutated is invalid")
    for key in ("reason", "detail", "mtime_semantics"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"receipt {key} is invalid")
    detail = payload.get("detail")
    if isinstance(detail, str):
        try:
            detail_size = len(str.encode(detail, "utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("receipt detail is not valid UTF-8") from exc
        if detail_size > _DIAGNOSTIC_LIMIT:
            raise ValueError("receipt detail exceeds the 4 KiB diagnostic limit")
    for key in ("old_identity", "new_identity"):
        identity = payload.get(key)
        if identity is not None and (
            not isinstance(identity, dict)
            or set(identity) != {"dev", "ino"}
            or any(
                not isinstance(identity[item], int)
                or isinstance(identity[item], bool)
                or identity[item] < 0
                for item in ("dev", "ino")
            )
        ):
            raise ValueError(f"receipt {key} is invalid")
    if (payload.get("old_size") is None) != (payload.get("old_identity") is None):
        raise ValueError("receipt old size and identity presence differ")
    if (payload.get("new_size") is None) != (payload.get("new_identity") is None):
        raise ValueError("receipt new size and identity presence differ")
    mtime_semantics = payload.get("mtime_semantics")
    if mtime_semantics not in {
        None,
        "captured-source-policy",
        "captured-pre-truncate-source-policy",
    }:
        raise ValueError("receipt mtime_semantics is invalid")
    if outcome == "deferred" and mutated is not False:
        raise ValueError("deferred receipt must prove destination_mutated=false")
    if outcome in {"deferred", "fatal"} and (
        not payload.get("reason") or not payload.get("detail")
    ):
        raise ValueError("failure receipt must include reason and detail")
    if outcome in {"updated", "unchanged", "no-complete-line"} and (
        payload.get("reason") is not None or payload.get("detail") is not None
    ):
        raise ValueError("successful receipt cannot include a failure reason")
    if outcome == "updated" and (
        mutated is not True or payload.get("new_size") != payload.get("publish_size")
    ):
        raise ValueError("updated receipt mutation or size is invalid")
    has_published_candidate = outcome in {"updated", "unchanged"} or (
        outcome == "fatal" and mutated is True
    )
    if has_published_candidate and (
        payload.get("publish_size") is None
        or payload["publish_size"] <= 0
        or payload.get("source_size") is None
        or payload["source_size"] < payload["publish_size"]
        or method not in {"reflink", "copy"}
        or not isinstance(partial, bool)
        or mtime_semantics is None
    ):
        raise ValueError("published-candidate receipt evidence is incomplete")
    if has_published_candidate:
        expected_mtime_semantics = (
            "captured-pre-truncate-source-policy"
            if partial
            else "captured-source-policy"
        )
        if mtime_semantics != expected_mtime_semantics:
            raise ValueError("receipt partial and mtime semantics disagree")
        if partial and payload["source_size"] <= payload["publish_size"]:
            raise ValueError("partial receipt source size must exceed publish size")
    if outcome == "updated" and payload.get("new_identity") is None:
        raise ValueError("updated receipt must bind the new destination identity")
    if outcome == "unchanged" and payload.get("old_identity") is None:
        raise ValueError("unchanged receipt must bind the existing destination")
    if outcome in {"unchanged", "no-complete-line"} and mutated is not False:
        raise ValueError("non-updating success must prove destination_mutated=false")
    if (
        outcome in {"unchanged", "no-complete-line", "deferred"}
        or (outcome == "fatal" and mutated is False)
    ) and (
        payload.get("new_size") != payload.get("old_size")
        or payload.get("new_identity") != payload.get("old_identity")
    ):
        raise ValueError("non-updating receipt must preserve destination evidence")
    if outcome == "unchanged" and not (
        payload.get("old_size")
        == payload.get("new_size")
        == payload.get("publish_size")
    ):
        raise ValueError("unchanged receipt sizes disagree")
    if outcome == "no-complete-line" and (
        payload.get("publish_size") != 0
        or payload.get("source_size") is None
        or partial is not None
        or mtime_semantics is not None
    ):
        raise ValueError("no-complete-line receipt evidence is invalid")
    if outcome == "fatal" and mutated is True:
        if payload.get("new_identity") is None:
            raise ValueError("mutated fatal receipt must bind the new destination")
        if payload.get("new_size") != payload.get("publish_size"):
            raise ValueError("mutated fatal receipt sizes disagree")
    if (
        mutated is True
        and payload.get("old_identity") is not None
        and payload.get("new_identity") == payload.get("old_identity")
    ):
        raise ValueError("mutated receipt must replace the destination identity")
    if (
        outcome == "fatal"
        and mutated is None
        and (
            payload.get("new_size") is not None
            or payload.get("new_identity") is not None
        )
    ):
        raise ValueError(
            "ambiguous fatal receipt cannot claim new destination evidence"
        )
    if outcome in {"updated", "unchanged", "no-complete-line"} and method not in {
        "reflink",
        "copy",
    }:
        raise ValueError("successful receipt must name its staging method")
    try:
        return SyncReceipt(**payload)
    except TypeError as exc:  # pragma: no cover - exact keys checked above
        raise ValueError(str(exc)) from exc


def _read_receipt() -> object:
    data = sys.stdin.buffer.read(_RECEIPT_LIMIT + 1)
    if len(data) > _RECEIPT_LIMIT:
        raise ValueError("receipt exceeds 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("receipt is not UTF-8") from exc
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"receipt is not valid JSON: {exc}") from exc
    if text[end:].strip():
        raise ValueError("receipt contains trailing data")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync-one")
    sync.add_argument("--source", required=True)
    sync.add_argument("--destination", required=True)
    sync.add_argument("--json", action="store_true", required=True)
    validate = subparsers.add_parser("validate-receipt")
    validate.add_argument("--source", required=True)
    validate.add_argument("--destination", required=True)
    validate.add_argument("--exit-status", type=int, required=True)
    return parser


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "sync-one":
        try:
            hook = _cli_hook(args.source, args.destination)
            receipt = sync_one(args.source, args.destination, action_hook=hook)
        except BackendError as exc:
            receipt = _startup_fatal_receipt(
                args.source,
                args.destination,
                exc.reason,
                exc.detail,
            )
        except Exception as exc:
            receipt = _startup_fatal_receipt(
                args.source,
                args.destination,
                "unexpected_error",
                _exception_diagnostic(exc),
            )
        sys.stdout.write(json.dumps(receipt.to_dict(), sort_keys=True) + "\n")
        return _OUTCOME_EXITS[receipt.outcome]
    try:
        payload = _read_receipt()
        receipt = _validate_receipt_payload(
            payload, args.source, args.destination, args.exit_status
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid receipt: {exc}\n")
        return EXIT_FATAL
    fields = (
        receipt.outcome,
        "null" if receipt.old_size is None else str(receipt.old_size),
        "null" if receipt.new_size is None else str(receipt.new_size),
        "null" if receipt.method is None else receipt.method,
    )
    sys.stdout.write("\t".join(fields) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
