from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from katzenqt import katzen, persistent  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    FakeMessageBox,
    boxes,
    qt_app,
)
from functools import partial
from tests.stubs import returning


def _fake_main(window: object) -> str:
    return f"main for {window!r}"

REPO_ROOT = Path(__file__).resolve().parent.parent

LOG_FORMAT = "%(asctime)s %(name)s: %(levelname)s: %(message)s"

LoggerState = tuple[list[logging.Handler], int, bool, bool]


@pytest.fixture()
def preserved_logging() -> Iterator[None]:
    loggers = [logging.getLogger(n) for n in sorted(katzen.get_all_loggers())]
    loggers.append(logging.root)
    saved: dict[logging.Logger, LoggerState] = {
        lg: (list(lg.handlers), lg.level, lg.propagate, lg.disabled)
        for lg in loggers
    }
    try:
        yield
    finally:
        for lg, (handlers, level, propagate, disabled) in saved.items():
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate
            lg.disabled = disabled


def test_every_logger_gets_one_formatted_stream_handler(
    preserved_logging: None,
) -> None:
    name = f"katzenqt.test.install.{uuid.uuid4().hex}"
    sentinel = logging.getLogger(name)
    sentinel.addHandler(logging.NullHandler())
    sentinel.addHandler(logging.NullHandler())

    katzen.install_log_handlers()

    assert len(sentinel.handlers) == 1
    handler = sentinel.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.formatter is not None
    assert handler.formatter._fmt == LOG_FORMAT
    sentinel.handlers.clear()


def test_the_log_level_command_takes_logger_and_level_pairs() -> None:
    command = katzen.log_level_command()
    assert command.name == "katzenqt"
    assert command.help == "Katzenpost group chat client."
    assert command.context_settings["help_option_names"] == ["-h", "--help"]
    option = next(p for p in command.params if p.name == "level")
    assert option.nargs == 2
    assert option.multiple is True


def test_each_requested_pair_is_parsed_in_order() -> None:
    assert katzen.parse_log_levels(
        ["--level", "katzen", "DEBUG", "--level", "katzen.net", "INFO"],
    ) == [("katzen", "DEBUG"), ("katzen.net", "INFO")]


def test_no_arguments_parse_to_no_overrides() -> None:
    assert katzen.parse_log_levels([]) == []


def test_an_unknown_option_exits_with_the_click_usage_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        katzen.parse_log_levels(["--bogus"])
    assert excinfo.value.code == click.UsageError.exit_code
    assert "No such option" in capsys.readouterr().err


def test_asking_for_help_prints_it_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        katzen.parse_log_levels(["--help"])
    assert excinfo.value.code == 0
    assert "Katzenpost group chat client." in capsys.readouterr().out


def test_an_interrupted_parse_exits_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @click.command(name="katzenqt")
    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(katzen, "log_level_command", returning(interrupted))
    with pytest.raises(SystemExit) as excinfo:
        katzen.parse_log_levels([])
    assert excinfo.value.code == 1


class FakeApp:
    def __init__(self, argv: list[str]) -> None:
        self.argv = list(argv)
        self.icons: list[object] = []
        self.styles: list[str] = []
        self.quits = 0

    def setWindowIcon(self, icon: object) -> None:
        self.icons.append(icon)

    def setStyle(self, style: str) -> None:
        self.styles.append(style)

    def quit(self) -> None:
        self.quits += 1


class FakeIoThread:
    def __init__(self) -> None:
        self.starts = 0

    def start(self) -> None:
        self.starts += 1


class FakeWindow:
    def __init__(self, app: object) -> None:
        self.app = app
        self.iothread: object = None


class CliProbe:
    def __init__(self) -> None:
        self.apps: list[FakeApp] = []
        self.threads: list[FakeIoThread] = []
        self.windows: list[FakeWindow] = []
        self.handlers_installed = 0
        self.migrations = 0
        self.handed_off: list[object] = []
        self.levels: list[tuple[str, str]] = []
        self.already_running = False
        self.migration_error: Exception | None = None


@pytest.fixture()
def cli_probe(
    boxes: type[FakeMessageBox], monkeypatch: pytest.MonkeyPatch,
) -> CliProbe:
    probe = CliProbe()
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.delenv("QT_QUICK_BACKEND", raising=False)
    monkeypatch.delenv("QT_QUICK_FLICKABLE_WHEEL_DECELERATION", raising=False)

    def make_app(argv: list[str]) -> FakeApp:
        app = FakeApp(argv)
        probe.apps.append(app)
        return app

    def make_thread() -> FakeIoThread:
        thread = FakeIoThread()
        probe.threads.append(thread)
        return thread

    def make_window(app: object) -> FakeWindow:
        win = FakeWindow(app)
        probe.windows.append(win)
        return win

    def install() -> None:
        probe.handlers_installed += 1

    def migrate() -> None:
        probe.migrations += 1
        if probe.migration_error is not None:
            raise probe.migration_error

    def hand_off(coro: object, handle_sigint: bool = False) -> str:
        probe.handed_off.append(coro)
        return "qtasyncio returned"

    monkeypatch.setattr(katzen, "QApplication", make_app)
    monkeypatch.setattr(katzen, "install_log_handlers", install)
    monkeypatch.setattr(katzen, "parse_log_levels", partial(getattr, probe, "levels"))
    monkeypatch.setattr(persistent, "init_and_migrate", migrate)
    monkeypatch.setattr(
        katzen, "is_there_already_an_instance_running",
        partial(getattr, probe, "already_running"),
    )
    monkeypatch.setattr(katzen, "AsyncioThread", make_thread)
    monkeypatch.setattr(katzen, "MainWindow", make_window)
    monkeypatch.setattr(katzen, "main", _fake_main)
    monkeypatch.setattr(katzen, "QtAsyncio", SimpleNamespace(run=hand_off))
    return probe


def test_cli_starts_the_io_thread_and_hands_off_to_qtasyncio(
    cli_probe: CliProbe,
) -> None:
    result = katzen.cli()

    assert result == "qtasyncio returned"
    assert Path.cwd() == REPO_ROOT
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QT_QUICK_FLICKABLE_WHEEL_DECELERATION"] == "14999"
    assert cli_probe.handlers_installed == 1
    assert cli_probe.migrations == 1
    app = cli_probe.apps[0]
    assert app.styles == ["Fusion"]
    assert len(app.icons) == 1
    assert cli_probe.threads[0].starts == 1
    window = cli_probe.windows[0]
    assert window.app is app
    assert window.iothread is cli_probe.threads[0]
    assert cli_probe.handed_off == [f"main for {window!r}"]


def test_a_requested_level_is_applied_and_an_unknown_one_disables(
    cli_probe: CliProbe,
) -> None:
    good = f"katzenqt.test.cli.good.{uuid.uuid4().hex}"
    bad = f"katzenqt.test.cli.bad.{uuid.uuid4().hex}"
    cli_probe.levels = [(good, "debug"), (bad, "nosuchlevel")]
    try:
        katzen.cli()
        assert logging.getLogger(good).level == logging.DEBUG
        assert logging.getLogger(good).disabled is False
        assert logging.getLogger(bad).disabled is True
    finally:
        for name in (good, bad):
            leftover = logging.getLogger(name)
            leftover.disabled = False
            leftover.setLevel(logging.NOTSET)


def test_a_failed_migration_reports_the_error_and_exits(
    cli_probe: CliProbe, boxes: type[FakeMessageBox],
) -> None:
    failure = RuntimeError("alembic refused the head")
    cli_probe.migration_error = failure

    with pytest.raises(SystemExit) as excinfo:
        katzen.cli()

    expected = f"Database schema migration failed:\n{failure!r}"
    assert excinfo.value.code == expected
    assert boxes.seen[0].text == expected
    assert cli_probe.apps[0].quits == 1
    assert cli_probe.threads == []


def test_a_second_instance_is_refused_before_any_thread_starts(
    cli_probe: CliProbe, boxes: type[FakeMessageBox],
) -> None:
    cli_probe.already_running = True

    with pytest.raises(SystemExit) as excinfo:
        katzen.cli()

    assert excinfo.value.code == f"{katzen.APP_NAME} is already running."
    assert boxes.seen[0].text == f"{katzen.APP_NAME} is already running."
    assert cli_probe.threads == []
    assert cli_probe.handed_off == []
