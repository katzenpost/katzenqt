"""Unit tests for the state-file permission clamp."""
import os
import stat

from katzenqt import persistent
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


def test_init_and_migrate_restricts_the_umask_during_upgrade(tmp_path, monkeypatch):
    """Migrations create the state file under the ambient umask; on a
    permissive one it would be briefly group/world-readable for the whole
    upgrade run. init_and_migrate must not rely solely on the post-hoc
    _restrict_state_file_perms fixup for this -- isolate that by making it
    a no-op and checking the file's mode right after the (faked) upgrade."""
    f = tmp_path / "state.sqlite3"
    monkeypatch.setattr(persistent, "state_file", f)
    monkeypatch.setattr(persistent, "_restrict_state_file_perms", lambda path: None)

    def fake_upgrade(cfg, revision):
        # Mimics what the real migrations do: create the file under
        # whatever umask is in effect right now.
        fd = os.open(f, os.O_CREAT | os.O_WRONLY, 0o666)
        os.close(fd)

    monkeypatch.setattr(persistent.alembic.command, "upgrade", fake_upgrade)

    old_umask = os.umask(0o022)  # a permissive, common default
    try:
        persistent.init_and_migrate()
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600, f"state file created at {oct(mode)} under a 022 umask"
