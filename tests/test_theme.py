from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSize, Qt, Signal  # noqa: E402
from PySide6.QtGui import QAction, QColor, QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLineEdit,
    QStyle,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QWidget,
)

from katzenqt import persistent  # noqa: E402
from katzenqt.theme import (  # noqa: E402
    DEFAULT_MODE,
    THEME_SETTING,
    ThemeDialog,
    ThemeManager,
    _build_dark_palette,
    _build_solarized_dark_palette,
    _build_solarized_light_palette,
    normalize_mode,
    themed_icon,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ACCEPT_SVG = "resources/accept.svg"

_CORE_ROLES = (
    QPalette.ColorRole.Window,
    QPalette.ColorRole.WindowText,
    QPalette.ColorRole.Base,
    QPalette.ColorRole.Text,
    QPalette.ColorRole.Button,
    QPalette.ColorRole.HighlightedText,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> Iterator[QApplication]:
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    yield app


class _FakeStyleHints(QObject):
    colorSchemeChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._scheme = Qt.ColorScheme.Unknown
        self.requested: list[Qt.ColorScheme] = []

    def colorScheme(self) -> Qt.ColorScheme:
        return self._scheme

    def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
        self.requested.append(scheme)

    def set_desktop_scheme(self, scheme: Qt.ColorScheme) -> None:
        self._scheme = scheme
        self.colorSchemeChanged.emit(scheme)


class _FakeApp:
    def __init__(self, style: QStyle, widgets: list[QWidget]) -> None:
        self._hints = _FakeStyleHints()
        self._style = style
        self._palette = QPalette()
        self._widgets = widgets

    def styleHints(self) -> _FakeStyleHints:
        return self._hints

    def style(self) -> QStyle:
        return self._style

    def palette(self) -> QPalette:
        return self._palette

    def setPalette(self, palette: QPalette) -> None:
        self._palette = palette

    def allWidgets(self) -> list[QWidget]:
        return list(self._widgets)


class _FakeQml:
    def __init__(self) -> None:
        self.clear_colors: list[QColor] = []
        self.updates = 0

    def setClearColor(self, color: QColor) -> None:
        self.clear_colors.append(color)

    def update(self) -> None:
        self.updates += 1


class _BareUi:
    def __init__(self, parent: QWidget) -> None:
        self.chat_lineEdit = QLineEdit(parent)


class _FakeUi:
    def __init__(self, parent: QWidget) -> None:
        self.defaultcontext = QWidget(parent)
        self.singleline_tab = QWidget(parent)
        self.chat_lineEdit = QLineEdit(parent)
        self.toolBar = QToolBar(parent)
        self.invite_contact_toolButton = QToolButton(parent)
        self.contacts_treeWidget = QTreeWidget(parent)
        self.qml_ChatLines = _FakeQml()
        self.action_accept_invitation = QAction(parent)
        self.action_invite_contact = QAction(parent)
        self.action_quit = QAction(parent)
        self.action_new_conversation = QAction(parent)
        self.action_display_font = QAction(parent)
        self.action_display_language = QAction(parent)
        self.action_theme = QAction(parent)
        self.actionDocumentation = QAction(parent)

    def widgets(self) -> list[QWidget]:
        return [
            self.defaultcontext,
            self.singleline_tab,
            self.chat_lineEdit,
            self.toolBar,
            self.invite_contact_toolButton,
            self.contacts_treeWidget,
        ]


class _Window(QWidget):
    def __init__(self, ui: _FakeUi | _BareUi | None) -> None:
        super().__init__()
        self.ui = ui


class _Harness:
    def __init__(self, style: QStyle, ui_kind: str) -> None:
        self.window = _Window(None)
        self.witness = QWidget(self.window)
        ui: _FakeUi | _BareUi | None = None
        if ui_kind == "full":
            ui = _FakeUi(self.window)
        elif ui_kind == "bare":
            ui = _BareUi(self.window)
        self.window.ui = ui
        widgets = ui.widgets() if isinstance(ui, _FakeUi) else [self.witness]
        self.app = _FakeApp(style, widgets)
        self.manager = ThemeManager(self.app, self.window)

    @property
    def ui(self) -> _FakeUi:
        assert isinstance(self.window.ui, _FakeUi)
        return self.window.ui

    def hints(self) -> _FakeStyleHints:
        return self.app.styleHints()


MakeTheme = Callable[[str], _Harness]


@pytest.fixture()
def make_theme(qt_app: QApplication) -> Iterator[MakeTheme]:
    made: list[_Harness] = []
    style = qt_app.style()

    def factory(ui_kind: str) -> _Harness:
        harness = _Harness(style, ui_kind)
        made.append(harness)
        return harness

    yield factory
    qt_app.processEvents()
    for harness in made:
        harness.window.deleteLater()
    qt_app.processEvents()


@pytest.fixture()
def real_app(qt_app: QApplication) -> Iterator[QApplication]:
    palette = QPalette(qt_app.palette())
    scheme = qt_app.styleHints().colorScheme()
    yield qt_app
    qt_app.processEvents()
    qt_app.styleHints().setColorScheme(scheme)
    qt_app.setPalette(palette)


def _fully_opaque_colors(icon: QIcon, size: int) -> set[tuple[int, int, int]]:
    image = icon.pixmap(QSize(size, size)).toImage()
    found: set[tuple[int, int, int]] = set()
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() == 255:
                found.add((color.red(), color.green(), color.blue()))
    return found


def _stored_mode() -> str | None:
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, THEME_SETTING)
        return None if row is None else str(row.value)


def _seed_mode(value: str) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.AppSetting(
            id=THEME_SETTING, type="str", value=value,
        ))
        sess.commit()


def _setting_row_count() -> int:
    with persistent.Session(persistent._engine_sync) as sess:
        return len(sess.exec(persistent.select(persistent.AppSetting)).all())


@pytest.mark.parametrize(
    "mode",
    ["system", "light", "dark", "solarized_light", "solarized_dark"],
)
def test_normalize_mode_keeps_every_known_mode(mode: str) -> None:
    assert normalize_mode(mode) == mode


@pytest.mark.parametrize(
    "junk", ["", "neon", "Dark", "SYSTEM", "solarized", None, 7, b"dark"],
)
def test_normalize_mode_rejects_junk(junk: object) -> None:
    assert normalize_mode(junk) == "system"
    assert DEFAULT_MODE == "system"


def test_themed_icon_recolors_every_fully_opaque_pixel(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    icon = themed_icon(_ACCEPT_SVG, QColor(0xFF, 0x00, 0x00), 27)
    assert icon.isNull() is False
    assert icon.pixmap(QSize(27, 27)).size() == QSize(27, 27)
    assert _fully_opaque_colors(icon, 27) == {(0xFF, 0x00, 0x00)}


def test_themed_icon_honours_the_requested_size(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    icon = themed_icon(_ACCEPT_SVG, QColor(0x00, 0xFF, 0x00), 25)
    assert icon.pixmap(QSize(25, 25)).size() == QSize(25, 25)
    assert _fully_opaque_colors(icon, 25) == {(0x00, 0xFF, 0x00)}


def test_themed_icon_is_empty_when_the_path_does_not_resolve(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    icon = themed_icon("resources/no-such-glyph.svg", QColor(0xFF, 0xFF, 0xFF))
    assert icon.isNull() is True
    assert icon.availableSizes() == []


def test_dark_palette_uses_the_documented_colours() -> None:
    p = _build_dark_palette()
    assert p.color(QPalette.ColorRole.Window) == QColor(0x35, 0x35, 0x35)
    assert p.color(QPalette.ColorRole.Base) == QColor(0x23, 0x23, 0x23)
    assert p.color(QPalette.ColorRole.Text) == QColor(0xFF, 0xFF, 0xFF)
    assert p.color(QPalette.ColorRole.ButtonText) == QColor(0xFF, 0xFF, 0xFF)
    assert p.color(QPalette.ColorRole.BrightText) == QColor(0xFF, 0x55, 0x55)
    assert p.color(QPalette.ColorRole.Highlight) == QColor(0x2A, 0x82, 0xDA)
    assert p.color(QPalette.ColorRole.Link) == QColor(0x2A, 0x82, 0xDA)
    assert p.color(QPalette.ColorRole.ToolTipBase) == QColor(0x35, 0x35, 0x35)
    assert p.color(QPalette.ColorRole.PlaceholderText) == QColor(
        0x7F, 0x7F, 0x7F,
    )


@pytest.mark.parametrize(
    "role",
    [
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ],
)
def test_fill_palette_greys_the_disabled_text_roles(
    role: QPalette.ColorRole,
) -> None:
    dark = _build_dark_palette()
    assert dark.color(QPalette.ColorGroup.Disabled, role) == QColor(
        0x7F, 0x7F, 0x7F,
    )
    assert dark.color(QPalette.ColorGroup.Active, role) != QColor(
        0x7F, 0x7F, 0x7F,
    )
    solarized = _build_solarized_light_palette()
    assert solarized.color(QPalette.ColorGroup.Disabled, role) == QColor(
        0x93, 0xA2, 0xA1,
    )


def test_solarized_light_uses_the_canonical_colours() -> None:
    p = _build_solarized_light_palette()
    assert p.color(QPalette.ColorRole.Window) == QColor(0xFD, 0xF6, 0xE3)
    assert p.color(QPalette.ColorRole.Base) == QColor(0xFD, 0xF6, 0xE3)
    assert p.color(QPalette.ColorRole.Text) == QColor(0x65, 0x7B, 0x83)
    assert p.color(QPalette.ColorRole.Button) == QColor(0xEE, 0xE8, 0xD5)
    assert p.color(QPalette.ColorRole.AlternateBase) == QColor(
        0xEE, 0xE8, 0xD5,
    )
    assert p.color(QPalette.ColorRole.Highlight) == QColor(0x26, 0x8B, 0xD2)
    assert p.color(QPalette.ColorRole.HighlightedText) == QColor(
        0xFD, 0xF6, 0xE3,
    )
    assert p.color(QPalette.ColorRole.BrightText) == QColor(0xDC, 0x32, 0x2F)


def test_solarized_dark_uses_the_canonical_colours() -> None:
    p = _build_solarized_dark_palette()
    assert p.color(QPalette.ColorRole.Window) == QColor(0x00, 0x2B, 0x36)
    assert p.color(QPalette.ColorRole.Base) == QColor(0x07, 0x36, 0x42)
    assert p.color(QPalette.ColorRole.Text) == QColor(0x93, 0xA2, 0xA1)
    assert p.color(QPalette.ColorRole.Button) == QColor(0x00, 0x2B, 0x36)
    assert p.color(QPalette.ColorRole.Highlight) == QColor(0x26, 0x8B, 0xD2)
    assert p.color(QPalette.ColorRole.HighlightedText) == QColor(
        0x07, 0x36, 0x42,
    )
    assert p.color(QPalette.ColorRole.BrightText) == QColor(0xCB, 0x4B, 0x16)
    assert p.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text) == (
        QColor(0x58, 0x6E, 0x75)
    )


def test_the_two_solarized_palettes_disagree_on_every_core_role() -> None:
    light = _build_solarized_light_palette()
    dark = _build_solarized_dark_palette()
    for role in _CORE_ROLES:
        assert light.color(role) != dark.color(role)


@pytest.mark.parametrize(
    ("mode", "scheme", "window"),
    [
        ("dark", Qt.ColorScheme.Dark, QColor(0x35, 0x35, 0x35)),
        ("solarized_dark", Qt.ColorScheme.Dark, QColor(0x00, 0x2B, 0x36)),
        ("solarized_light", Qt.ColorScheme.Light, QColor(0xFD, 0xF6, 0xE3)),
    ],
)
def test_apply_requests_the_scheme_and_installs_the_palette(
    make_theme: MakeTheme,
    mode: str,
    scheme: Qt.ColorScheme,
    window: QColor,
) -> None:
    harness = make_theme("none")
    harness.manager.apply(mode)
    assert harness.manager.mode == mode
    assert harness.hints().requested == [scheme]
    assert harness.app.palette().color(QPalette.ColorRole.Window) == window
    assert _stored_mode() == mode


@pytest.mark.parametrize("mode", ["light", "system"])
def test_light_and_system_modes_take_the_style_standard_palette(
    make_theme: MakeTheme, mode: str,
) -> None:
    harness = make_theme("none")
    harness.manager.apply(mode)
    expected = harness.app.style().standardPalette()
    assert harness.app.palette().color(QPalette.ColorRole.Window) == (
        expected.color(QPalette.ColorRole.Window)
    )
    assert harness.manager._resolve_scheme() == "light"


def test_apply_normalizes_junk_to_system(make_theme: MakeTheme) -> None:
    harness = make_theme("none")
    harness.manager.apply("chartreuse")
    assert harness.manager.mode == "system"
    assert harness.hints().requested == [Qt.ColorScheme.Unknown]
    assert _stored_mode() == "system"


def test_apply_reuses_a_single_settings_row(make_theme: MakeTheme) -> None:
    harness = make_theme("none")
    harness.manager.apply("dark")
    harness.manager.apply("solarized_light")
    assert _stored_mode() == "solarized_light"
    assert _setting_row_count() == 1
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, THEME_SETTING)
        assert row is not None
        assert row.type == "str"


def test_apply_without_persist_leaves_the_table_empty(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("dark", persist=False)
    assert harness.manager.mode == "dark"
    assert _stored_mode() is None


def test_restore_applies_the_persisted_mode(make_theme: MakeTheme) -> None:
    _seed_mode("solarized_dark")
    harness = make_theme("none")
    assert harness.manager.mode == "system"
    harness.manager.restore()
    assert harness.manager.mode == "solarized_dark"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0x00, 0x2B, 0x36,
    )
    assert _setting_row_count() == 1
    assert _stored_mode() == "solarized_dark"


@pytest.mark.parametrize("stored", ["", "neon", "Dark"])
def test_restore_falls_back_for_an_unusable_stored_value(
    make_theme: MakeTheme, stored: str,
) -> None:
    _seed_mode(stored)
    harness = make_theme("none")
    harness.manager.restore()
    assert harness.manager.mode == "system"
    assert _stored_mode() == stored


def test_restore_without_a_stored_row_is_system(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.restore()
    assert harness.manager.mode == "system"
    assert _stored_mode() is None


def test_load_mode_survives_a_broken_session(
    make_theme: MakeTheme,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("no database")

    harness = make_theme("none")
    monkeypatch.setattr(persistent, "Session", boom)
    caplog.set_level(logging.WARNING, logger="katzenqt.theme")
    harness.manager.restore()
    assert harness.manager.mode == "system"
    assert "could not load theme mode: no database" in caplog.text


def test_save_mode_survives_a_broken_session(
    make_theme: MakeTheme,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("disk on fire")

    harness = make_theme("none")
    monkeypatch.setattr(persistent, "Session", boom)
    caplog.set_level(logging.WARNING, logger="katzenqt.theme")
    harness.manager.apply("dark")
    assert harness.manager.mode == "dark"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0x35, 0x35, 0x35,
    )
    assert "could not persist theme mode: disk on fire" in caplog.text


def test_system_mode_follows_a_dark_desktop(make_theme: MakeTheme) -> None:
    harness = make_theme("none")
    harness.hints().set_desktop_scheme(Qt.ColorScheme.Dark)
    harness.manager.apply("system", persist=False)
    assert harness.manager._resolve_scheme() == "dark"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0x35, 0x35, 0x35,
    )


def test_system_mode_follows_a_light_desktop(make_theme: MakeTheme) -> None:
    harness = make_theme("none")
    harness.hints().set_desktop_scheme(Qt.ColorScheme.Light)
    harness.manager.apply("system", persist=False)
    assert harness.manager._resolve_scheme() == "light"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == (
        harness.app.style().standardPalette().color(QPalette.ColorRole.Window)
    )


def test_a_desktop_scheme_change_reapplies_the_palette(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("system", persist=False)
    light_window = harness.app.palette().color(QPalette.ColorRole.Window)

    harness.hints().set_desktop_scheme(Qt.ColorScheme.Dark)
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0x35, 0x35, 0x35,
    )

    harness.hints().set_desktop_scheme(Qt.ColorScheme.Light)
    assert harness.app.palette().color(QPalette.ColorRole.Window) == (
        light_window
    )


def test_a_forced_mode_ignores_the_desktop_scheme(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("solarized_light", persist=False)
    harness.hints().set_desktop_scheme(Qt.ColorScheme.Dark)
    assert harness.manager._resolve_scheme() == "light"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0xFD, 0xF6, 0xE3,
    )


def test_sync_pushes_the_dark_palette_into_the_pinned_widgets(
    make_theme: MakeTheme, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    ui = harness.ui
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()

    base = QColor(0x23, 0x23, 0x23)
    for widget in ui.widgets():
        assert widget.palette().color(QPalette.ColorRole.Base) == base
    assert ui.qml_ChatLines.clear_colors == [base]
    assert ui.qml_ChatLines.updates == 1


def test_sync_clears_the_pinned_invite_button_stylesheet(
    make_theme: MakeTheme, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    harness.ui.invite_contact_toolButton.setStyleSheet(
        "background-color: rgb(170, 220, 255);"
    )
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()
    assert harness.ui.invite_contact_toolButton.styleSheet() == ""


def test_sync_restyles_the_contacts_tree_from_the_palette(
    make_theme: MakeTheme, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()
    dark_sheet = harness.ui.contacts_treeWidget.styleSheet()
    assert "QTreeView { background-color: #232323; }" in dark_sheet
    assert "background-color: #2a82da;" in dark_sheet
    assert "color: #232323;" in dark_sheet
    assert "height: 1.6em;" in dark_sheet

    harness.manager.apply("solarized_light", persist=False)
    harness.manager._sync_theme()
    light_sheet = harness.ui.contacts_treeWidget.styleSheet()
    assert "QTreeView { background-color: #fdf6e3; }" in light_sheet
    assert "#232323" not in light_sheet


def test_dark_mode_tints_the_toolbar_icons_white(
    make_theme: MakeTheme, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()

    assert _fully_opaque_colors(harness.ui.action_quit.icon(), 27) == {
        (0xFF, 0xFF, 0xFF),
    }
    assert _fully_opaque_colors(
        harness.ui.action_accept_invitation.icon(), 27,
    ) == {(0xFF, 0xFF, 0xFF)}
    assert _fully_opaque_colors(
        harness.ui.invite_contact_toolButton.icon(), 25,
    ) == {(0xFF, 0xFF, 0xFF)}
    assert _fully_opaque_colors(
        harness.ui.actionDocumentation.icon(), 27,
    ) == {(0xFF, 0xFF, 0xFF)}


def test_light_mode_uses_the_untinted_toolbar_icons(
    make_theme: MakeTheme, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()
    tinted = _fully_opaque_colors(harness.ui.action_accept_invitation.icon(), 27)

    harness.manager.apply("light", persist=False)
    harness.manager._sync_theme()
    plain = _fully_opaque_colors(harness.ui.action_accept_invitation.icon(), 27)
    assert plain
    assert plain != tinted
    assert (0xFF, 0xFF, 0xFF) not in plain
    assert harness.ui.invite_contact_toolButton.icon().isNull() is False


def test_sync_ignores_a_window_without_a_ui(make_theme: MakeTheme) -> None:
    harness = make_theme("none")
    before = harness.witness.palette().color(QPalette.ColorRole.Window)
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()
    themed = harness.app.palette().color(QPalette.ColorRole.Window)
    assert harness.window.ui is None
    assert themed != before
    assert harness.witness.palette().color(
        QPalette.ColorRole.Window,
    ) == before


def test_sync_tolerates_a_ui_without_the_named_widgets(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("bare")
    ui = harness.window.ui
    assert isinstance(ui, _BareUi)
    before = ui.chat_lineEdit.palette().color(QPalette.ColorRole.Base)
    harness.manager.apply("dark", persist=False)
    harness.manager._sync_theme()
    themed = harness.app.palette().color(QPalette.ColorRole.Base)
    assert hasattr(ui, "toolBar") is False
    assert themed != before
    assert ui.chat_lineEdit.palette().color(QPalette.ColorRole.Base) == themed


def test_apply_defers_the_widget_sync_to_the_event_loop(
    make_theme: MakeTheme,
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    harness = make_theme("full")
    harness.manager.apply("dark", persist=False)
    assert harness.ui.contacts_treeWidget.styleSheet() == ""

    qt_app.processEvents()
    assert "#232323" in harness.ui.contacts_treeWidget.styleSheet()
    assert harness.ui.qml_ChatLines.clear_colors == [QColor(0x23, 0x23, 0x23)]


def test_the_real_application_receives_the_dark_palette(
    real_app: QApplication,
) -> None:
    window = _Window(None)
    manager = ThemeManager(real_app, window)
    manager.apply("dark", persist=False)
    assert real_app.palette().color(QPalette.ColorRole.Window) == QColor(
        0x35, 0x35, 0x35,
    )
    assert real_app.palette().color(QPalette.ColorRole.Highlight) == QColor(
        0x2A, 0x82, 0xDA,
    )
    window.deleteLater()


def test_the_dialog_preselects_the_current_mode(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("solarized_dark", persist=False)
    dialog = ThemeDialog(harness.manager)
    assert dialog.windowTitle() == "Display mode"
    assert set(dialog._buttons) == {
        "system", "light", "dark", "solarized_light", "solarized_dark",
    }
    checked = [m for m, b in dialog._buttons.items() if b.isChecked()]
    assert checked == ["solarized_dark"]
    assert dialog._buttons["light"].text() == "Light"
    assert dialog._buttons["system"].text() == (
        "Follow window manager (system)"
    )
    dialog.deleteLater()


def test_the_dialog_applies_the_checked_mode_on_accept(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("system", persist=False)
    dialog = ThemeDialog(harness.manager)
    dialog._buttons["solarized_light"].setChecked(True)
    dialog.accept()

    assert harness.manager.mode == "solarized_light"
    assert harness.app.palette().color(QPalette.ColorRole.Window) == QColor(
        0xFD, 0xF6, 0xE3,
    )
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert _stored_mode() == "solarized_light"
    dialog.deleteLater()


def test_the_dialog_leaves_the_mode_alone_on_reject(
    make_theme: MakeTheme,
) -> None:
    harness = make_theme("none")
    harness.manager.apply("dark", persist=False)
    dialog = ThemeDialog(harness.manager, harness.window)
    dialog._buttons["light"].setChecked(True)
    dialog.reject()

    assert harness.manager.mode == "dark"
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.parent() is harness.window
    assert _stored_mode() is None
