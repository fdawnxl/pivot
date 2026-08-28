from __future__ import annotations

from pathlib import Path

import pytest

from pivot.lease import RuntimeLease, RuntimeLeaseError


def test_runtime_lease_allows_only_one_owner_per_instance(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path)
    second = RuntimeLease(tmp_path)

    first.acquire()
    assert not first.unexpected_exit
    try:
        with pytest.raises(RuntimeLeaseError, match="already owns"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    assert second.acquired
    assert not second.unexpected_exit
    second.acquire()
    second.release()
    second.release()


def test_runtime_lease_detects_unclean_previous_owner(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path)
    first.acquire()
    # Simulate process death: the descriptor is closed without RuntimeLease.release.
    first._handle.close()  # type: ignore[union-attr]
    first._handle = None

    second = RuntimeLease(tmp_path)
    second.acquire()
    try:
        assert second.unexpected_exit
    finally:
        second.release()


def test_runtime_lease_marks_clean_release(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path)
    first.acquire()
    first.release()

    second = RuntimeLease(tmp_path)
    second.acquire()
    try:
        assert not second.unexpected_exit
    finally:
        second.release()
