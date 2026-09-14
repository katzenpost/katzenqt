"""The database engines must not echo statements.

SQLAlchemy's statement logging prints bound parameters; for these tables those
are BACAP caps (signing keys), voucher secret keys, and message plaintext.
Echo must stay off so none of that reaches the log / systemd journal.
"""
from katzenqt import persistent


def test_sync_engine_does_not_echo() -> None:
    assert persistent._engine_sync.echo is False


def test_async_engine_does_not_echo() -> None:
    assert persistent._engine.sync_engine.echo is False
