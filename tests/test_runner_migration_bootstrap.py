"""Pin: ``integration_runner.main`` brings the schema to head before
each action runs.

The runner previously called ``SQLModel.metadata.create_all`` and
sidestepped alembic, which left us unable to detect schema-upgrade
regressions in headless callers. After this commit it must run
``persistent.init_and_migrate`` instead, exactly once per invocation,
before any subcommand fires.
"""
from __future__ import annotations

from pathlib import Path

from alembic.script import ScriptDirectory

from katzenqt import integration_runner, persistent


def test_main_calls_init_and_migrate(monkeypatch, capsys):
    calls: "list[str]" = []
    monkeypatch.setattr(
        persistent, "init_and_migrate",
        lambda: calls.append("init_and_migrate") or None,
    )

    # ``read`` against a missing conversation returns 2 without ever
    # touching kpclientd. That is enough surface to confirm the
    # bootstrap fired before the action body.
    rc = integration_runner.main(["read", "no-such-conv", "0.1"])

    assert calls == ["init_and_migrate"]
    assert rc == 2
    captured = capsys.readouterr()
    assert "conversation 'no-such-conv' not found" in captured.out + captured.err


def test_alembic_cfg_resolves_to_an_existing_ini():
    """Whether running from the source tree or from an installed
    package, ``persistent._alembic_cfg.config_file_name`` must point at
    a file alembic can read."""
    cfg_file = persistent._alembic_cfg.config_file_name
    assert cfg_file is not None
    assert Path(cfg_file).is_file(), (
        f"_alembic_cfg.config_file_name={cfg_file!r} does not exist; "
        "the resolver chain must yield a reachable alembic.ini for the "
        "current install layout"
    )


def test_alembic_script_location_resolves():
    """The migrations directory referenced by ``_alembic_cfg`` must
    contain an ``env.py``. Catches a misconfigured script_location or a
    missing package-data entry early."""
    sd = ScriptDirectory.from_config(persistent._alembic_cfg)
    assert (Path(sd.dir) / "env.py").is_file()
