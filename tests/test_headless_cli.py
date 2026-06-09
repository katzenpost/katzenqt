"""``katzenqt-headless`` console-script entry point.

Pins three invariants for commit 5:

* ``katzenqt.headless.cli`` is callable and dispatches actions
  identically to the legacy ``integration_runner.main`` (which now
  delegates to it).
* ``pyproject.toml`` exposes ``katzenqt-headless`` as a console
  script pointed at the new ``cli`` function.
* The action dispatch table now lives in ``katzenqt.headless._actions``
  rather than the legacy runner module.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from katzenqt import headless, integration_runner, persistent


_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_headless_has_cli_function():
    assert callable(headless.cli)


def test_pyproject_declares_katzenqt_headless_script():
    data = tomllib.loads(_PYPROJECT.read_text())
    scripts = data["project"]["scripts"]
    assert scripts.get("katzenqt-headless") == "katzenqt.headless:cli", (
        f"expected katzenqt-headless console script entry; got: {scripts!r}"
    )


def test_actions_module_exists_with_parser_builder():
    """The plan moves the action dispatch table out of
    integration_runner and into katzenqt.headless._actions. Pin the
    new home so it does not drift back."""
    from katzenqt.headless import _actions
    assert callable(_actions._build_parser)
    assert callable(_actions._action_create_conv)
    assert callable(_actions._action_voucher_mint)
    assert callable(_actions._action_voucher_induct)
    assert callable(_actions._action_voucher_await)
    assert callable(_actions._action_send)
    assert callable(_actions._action_multi_send)
    assert callable(_actions._action_read)
    assert callable(_actions._action_chat_session)


def test_cli_dispatches_read_against_missing_conv(monkeypatch, capsys):
    monkeypatch.setattr(persistent, "init_and_migrate", lambda: None)
    rc = headless.cli(["read", "no-such-conv", "0.1", "--address", "127.0.0.1:64331"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "conversation 'no-such-conv' not found" in captured.out + captured.err


def test_main_is_thin_shim_for_cli(monkeypatch, capsys):
    """integration_runner.main and headless.cli must produce the same
    exit code and the same result for the same argv; the legacy runner
    is now a thin shim. (Output is compared by content, not byte-for-byte,
    since the logged stream carries per-record timestamps.)"""
    monkeypatch.setattr(persistent, "init_and_migrate", lambda: None)
    rc1 = headless.cli(["read", "ghost", "0.1", "--address", "127.0.0.1:64331"])
    c1 = capsys.readouterr()
    rc2 = integration_runner.main(["read", "ghost", "0.1", "--address", "127.0.0.1:64331"])
    c2 = capsys.readouterr()
    assert rc1 == rc2 == 2
    msg = "conversation 'ghost' not found"
    assert msg in c1.out + c1.err
    assert msg in c2.out + c2.err
