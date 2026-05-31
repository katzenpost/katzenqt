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

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QPalette
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
        self._app.styleHints().setColorScheme(_SCHEME[mode])
        self._sync_pinned_palettes()
        if persist:
            self._save_mode(mode)
        logger.info("theme mode applied: %s", mode)

    def _on_scheme_changed(self, _scheme):
        # The palette has just changed under us; bring the pinned widgets
        # into line with the new one rather than leaving them stale.
        self._sync_pinned_palettes()

    def _sync_pinned_palettes(self):
        ui = getattr(self._window, "ui", None)
        if ui is None:
            return
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
