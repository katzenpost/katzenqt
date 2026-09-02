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
