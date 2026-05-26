"""Tests for `network.resolve_thinclient_config`.

The resolver consults sources in precedence order:

  1. explicit ``config_path`` argument
  2. env var ``KATZENQT_THINCLIENT_CONFIG``
  3. ``$XDG_CONFIG_HOME/katzenqt/thinclient.toml`` (falling back to
     ``~/.config/katzenqt/thinclient.toml``)
  4. the bundled copy under ``katzenqt/data/thinclient.toml`` via
     ``importlib.resources``
  5. the development-tree fallback at ``<repo>/config/thinclient.toml``

Each step is exercised by a separate test. The whole suite must work
without ``KATZENQT_THINCLIENT_CONFIG`` set, hence the autouse fixture
that wipes the env.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from katzenqt import network


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KATZENQT_THINCLIENT_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _make_toml(path: Path, marker: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# marker={marker}\n")
    return path


def test_explicit_path_wins_over_everything(tmp_path, monkeypatch):
    explicit = _make_toml(tmp_path / "explicit.toml", "explicit")
    monkeypatch.setenv(
        "KATZENQT_THINCLIENT_CONFIG", str(tmp_path / "env.toml"),
    )
    _make_toml(tmp_path / "env.toml", "env")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _make_toml(tmp_path / "xdg" / "katzenqt" / "thinclient.toml", "xdg")

    resolved = network.resolve_thinclient_config(explicit)
    assert resolved == explicit


def test_env_wins_when_no_explicit(tmp_path, monkeypatch):
    env_path = _make_toml(tmp_path / "env.toml", "env")
    monkeypatch.setenv("KATZENQT_THINCLIENT_CONFIG", str(env_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _make_toml(tmp_path / "xdg" / "katzenqt" / "thinclient.toml", "xdg")

    resolved = network.resolve_thinclient_config()
    assert resolved == env_path


def test_xdg_used_when_no_explicit_or_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    xdg_path = _make_toml(
        tmp_path / "xdg" / "katzenqt" / "thinclient.toml", "xdg",
    )

    resolved = network.resolve_thinclient_config()
    assert resolved == xdg_path


def test_xdg_default_when_xdg_unset(tmp_path, monkeypatch):
    """Without ``XDG_CONFIG_HOME``, the resolver consults
    ``~/.config/katzenqt/thinclient.toml``. We redirect ``HOME`` to
    keep the test hermetic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg_path = _make_toml(
        tmp_path / ".config" / "katzenqt" / "thinclient.toml", "xdg-default",
    )

    resolved = network.resolve_thinclient_config()
    assert resolved == xdg_path


def test_falls_back_to_bundled_package_data(tmp_path, monkeypatch):
    """Without explicit / env / XDG, we fall back to the bundled copy
    shipped at ``src/katzenqt/data/thinclient.toml``. The test pins
    ``HOME`` to a sterile tmp so no user XDG file accidentally satisfies
    the earlier tiers."""
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = network.resolve_thinclient_config()
    assert resolved.is_file()
    # The bundled file lives under the installed katzenqt package's
    # ``data/`` subdirectory.
    assert resolved.parent.name == "data"
    assert resolved.name == "thinclient.toml"


def test_missing_path_raises_filenotfounderror(tmp_path, monkeypatch):
    """A non-existent explicit path should not silently fall through
    to a later tier; the caller asked for this file specifically."""
    explicit = tmp_path / "does-not-exist.toml"
    with pytest.raises(FileNotFoundError):
        network.resolve_thinclient_config(explicit)
