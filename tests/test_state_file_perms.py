"""Unit tests for the state-file permission clamp."""
import os
import stat

from katzenqt.persistent import _restrict_state_file_perms


def test_restricts_permissions_to_owner_only(tmp_path):
    f = tmp_path / "state.sqlite3"
    f.write_bytes(b"secret")
    os.chmod(f, 0o644)
    _restrict_state_file_perms(f)
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600


def test_missing_file_is_a_no_op(tmp_path):
    _restrict_state_file_perms(tmp_path / "does-not-exist.sqlite3")


def test_restricts_wal_and_shm_sidecars_too(tmp_path):
    f = tmp_path / "state.sqlite3"
    wal = tmp_path / "state.sqlite3-wal"
    shm = tmp_path / "state.sqlite3-shm"
    for sidecar in (f, wal, shm):
        sidecar.write_bytes(b"secret")
        os.chmod(sidecar, 0o644)
    _restrict_state_file_perms(f)
    for sidecar in (f, wal, shm):
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
