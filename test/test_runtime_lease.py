from __future__ import annotations

from pathlib import Path

import pytest

from pivot.lease import RuntimeLease, RuntimeLeaseError


def test_runtime_lease_allows_only_one_owner_per_instance(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path)
    second = RuntimeLease(tmp_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeLeaseError, match="already owns"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    assert second.acquired
    second.release()


def test_runtime_lease_is_idempotent(tmp_path: Path) -> None:
    lease = RuntimeLease(tmp_path)
    lease.acquire()
    lease.acquire()
    lease.release()
    lease.release()
