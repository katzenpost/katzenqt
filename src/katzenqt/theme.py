# SPDX-FileCopyrightText: Copyright (C) 2026 David Stainton
# SPDX-License-Identifier: AGPL-3.0-only

"""Light, dark, and window-manager-aware theming for katzenqt.

The desktop's own preference is honoured through Qt's colour-scheme
machinery. ``setColorScheme(Unknown)`` follows the window manager (the
freedesktop appearance portal on Linux, the native setting on macOS and
Windows); ``Light`` and ``Dark`` force the choice. The Fusion style
renders a proper palette for whichever scheme is in force.

Two colour sources in the generated UI do not follow a themed palette on
their own: a handful of widgets carry palettes pinned by the designer,
and they are reset here so they inherit the themed one. (The QML chat
view tracks the palette via its own ``SystemPalette``.)

The chosen mode persists in the ``AppSetting`` table under
``theme.mode`` and is restored at startup.
"""

import logging

from PySide6.QtCore import Qt, QObject, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout,
)

from . import persistent

logger = logging.getLogger(__name__)

# AppSetting key under which the chosen mode is stored.
THEME_SETTING = "theme.mode"

# Mode string -> Qt colour scheme. "system" follows the window manager.
_SCHEME = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}
DEFAULT_MODE = "system"

# Widgets whose palettes the generated Ui_MainWindow pins by hand; named by
# their attribute on the Ui object. We reset these so they follow the theme.
_PINNED_PALETTE_WIDGETS = (
    "defaultcontext",
    "singleline_tab",
    "chat_lineEdit",
    "toolBar",
)


def normalize_mode(mode):
    """Coerce an arbitrary value to a known mode, defaulting to system."""
    return mode if mode in _SCHEME else DEFAULT_MODE


def _build_dark_palette():
    """A consistent dark palette for the Fusion style. The light palette is
    taken from the style's own standard palette; only dark needs building,
    since most desktops' default Qt palette is light."""
    p = QPalette()
    window = QColor(0x35, 0x35, 0x35)
    base = QColor(0x23, 0x23, 0x23)
    text = QColor(0xff, 0xff, 0xff)
    disabled = QColor(0x7f, 0x7f, 0x7f)
    highlight = QColor(0x2a, 0x82, 0xda)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, window)
    p.setColor(QPalette.ColorRole.ToolTipBase, window)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, window)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(0xff, 0x55, 0x55))
    p.setColor(QPalette.ColorRole.Link, highlight)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, base)
    p.setColor(QPalette.ColorRole.PlaceholderText, disabled)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


class ThemeManager(QObject):
    """Applies, persists, and restores the display mode for the GUI."""

    def __init__(self, app, window):
        super().__init__(window)
        self._app = app
        self._window = window
        self._mode = DEFAULT_MODE
        # Re-sync the pinned widgets whenever the effective scheme changes,
        # including a live window-manager change while in system mode.
        app.styleHints().colorSchemeChanged.connect(self._on_scheme_changed)

    @property
    def mode(self):
        return self._mode

    def restore(self):
        """Load the persisted mode and apply it. Call once at startup."""
        self.apply(self._load_mode(), persist=False)

    def apply(self, mode, persist=True):
        """Switch to ``mode`` ("system", "light", or "dark")."""
        mode = normalize_mode(mode)
        self._mode = mode
        # Requesting a scheme alone is unreliable (some platforms, and the
        # offscreen plugin, ignore it; forcing Light over a dark desktop can
        # leave an inconsistent palette). So we also install an explicit
        # palette below, which is authoritative everywhere. The scheme is
        # still set so native bits and QML SystemPalette agree.
        self._app.styleHints().setColorScheme(_SCHEME[mode])
        self._apply_palette()
        if persist:
            self._save_mode(mode)
        logger.info("theme mode applied: %s", mode)

    def _resolve_scheme(self):
        """Return "dark" or "light" for the current mode; system follows
        the desktop's reported scheme."""
        if self._mode == "dark":
            return "dark"
        if self._mode == "light":
            return "light"
        desktop = self._app.styleHints().colorScheme()
        return "dark" if desktop == Qt.ColorScheme.Dark else "light"

    def _apply_palette(self):
        scheme = self._resolve_scheme()
        if scheme == "dark":
            palette = _build_dark_palette()
        else:
            palette = self._app.style().standardPalette()
        self._app.setPalette(palette)
        # Defer the widget-level sync so it reads the palette we just set.
        QTimer.singleShot(0, self._sync_theme)

    def _on_scheme_changed(self, _scheme):
        # The desktop scheme changed under us (system mode): re-resolve and
        # re-apply the matching explicit palette.
        self._apply_palette()

    def _sync_theme(self):
        ui = getattr(self._window, "ui", None)
        if ui is None:
            return
        # This runs deferred / on colorSchemeChanged, so the scheme has
        # settled and the palette read here is current. Apply it explicitly
        # to the designer-pinned widgets: an empty QPalette() does not
        # guarantee contrast, whereas the live scheme palette does, in both
        # light and dark.
        themed = self._app.palette()
        for name in _PINNED_PALETTE_WIDGETS:
            widget = getattr(ui, name, None)
            if widget is not None:
                widget.setPalette(themed)
        # The QML chat host (a QQuickWidget) clears to white by default and
        # its rows are transparent over it, so in dark mode the themed light
        # text would land on white. Drive its clear colour from the theme.
        qml = getattr(ui, "qml_ChatLines", None)
        if qml is not None and hasattr(qml, "setClearColor"):
            qml.setClearColor(themed.base().color())
            qml.update()
        self._sync_themed_stylesheets(ui, themed)

    def _sync_themed_stylesheets(self, ui, pal):
        """Re-style the generated widgets that pin light colours in their
        stylesheets (which override the palette) so they stay legible in
        dark mode. Colours are re-derived from ``pal`` on every theme change.
        """
        # The invite-contact button hardcoded a light-blue background; clear
        # it so the button renders with the themed (Fusion) look instead.
        invite = getattr(ui, "invite_contact_toolButton", None)
        if invite is not None:
            invite.setStyleSheet("")
        # The contacts list pinned a light gradient behind every item, so the
        # themed (light) text was unreadable. Keep its layout rules but drive
        # the colours from the current palette.
        contacts = getattr(ui, "contacts_treeWidget", None)
        if contacts is not None:
            base = pal.base().color().name()
            highlight = pal.highlight().color().name()
            highlighted_text = pal.highlightedText().color().name()
            contacts.setStyleSheet(
                "QTreeView::item:selected {\n"
                f"    background-color: {highlight};\n"
                f"    color: {highlighted_text};\n"
                "}\n"
                "QTreeView::item {\n"
                "    height: 1.6em;\n"
                "    margin-top: 0.2em;\n"
                "    margin-bottom: 0.2em;\n"
                "    padding-left: 1em; padding-right: 1em;\n"
                f"    background-color: {base};\n"
                "    qproperty-alignment: AlignCenter;\n"
                "}"
            )

    def _load_mode(self):
        try:
            with persistent.Session(persistent._engine_sync) as sess:
                row = sess.get(persistent.AppSetting, THEME_SETTING)
                if row and row.value:
                    return normalize_mode(row.value)
        except Exception as e:  # a missing setting must never block startup
            logger.warning("could not load theme mode: %s", e)
        return DEFAULT_MODE

    def _save_mode(self, mode):
        try:
            with persistent.Session(persistent._engine_sync) as sess:
                row = sess.get(persistent.AppSetting, THEME_SETTING)
                if not row:
                    row = persistent.AppSetting(id=THEME_SETTING)
                row.type = "str"
                row.value = mode
                sess.add(row)
                sess.commit()
        except Exception as e:
            logger.warning("could not persist theme mode: %s", e)


class ThemeDialog(QDialog):
    """A small modal chooser: follow the window manager, light, or dark."""

    _CHOICES = (
        ("system", "Follow window manager (system)"),
        ("light", "Light"),
        ("dark", "Dark"),
    )

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("Display mode")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Display mode</b>"))

        self._buttons = {}
        for mode, text in self._CHOICES:
            button = QRadioButton(text, self)
            button.setChecked(mode == manager.mode)
            layout.addWidget(button)
            self._buttons[mode] = button

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def accept(self):
        for mode, button in self._buttons.items():
            if button.isChecked():
                self._manager.apply(mode)
                break
        super().accept()
