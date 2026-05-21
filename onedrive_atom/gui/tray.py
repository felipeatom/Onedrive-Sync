"""System tray icon and context menu."""

import logging
import subprocess
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from onedrive_atom.gui.icons import (
    icon_error, icon_offline, icon_paused, icon_synced, icon_syncing,
)

log = logging.getLogger(__name__)

_STATE_ICONS = {
    "synced": icon_synced,
    "syncing": icon_syncing,
    "paused": icon_paused,
    "error": icon_error,
    "offline": icon_offline,
}


class TrayIcon(QSystemTrayIcon):
    show_main_window = pyqtSignal()
    show_settings = pyqtSignal()
    show_accounts = pyqtSignal()
    pause_requested = pyqtSignal(bool)  # True = pause, False = resume
    sync_now_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._paused = False
        self._sync_dir: str = str(Path.home() / "OneDrive")
        self._state = "offline"

        self._build_menu()
        self.set_state("offline")

        self.activated.connect(self._on_activated)

    def _build_menu(self):
        menu = QMenu()

        self._status_action = menu.addAction("Onedrive-Sync")
        self._status_action.setEnabled(False)
        font = self._status_action.font()
        font.setBold(True)
        self._status_action.setFont(font)

        menu.addSeparator()

        self._open_folder_action = menu.addAction("Abrir pasta OneDrive")
        self._open_folder_action.triggered.connect(self._open_sync_dir)

        self._sync_now_action = menu.addAction("Sincronizar agora")
        self._sync_now_action.triggered.connect(self.sync_now_requested)

        self._pause_action = menu.addAction("Pausar sincronização")
        self._pause_action.triggered.connect(self._toggle_pause)

        menu.addSeparator()

        accounts_action = menu.addAction("Contas...")
        accounts_action.triggered.connect(self.show_accounts)

        settings_action = menu.addAction("Configurações...")
        settings_action.triggered.connect(self.show_settings)

        menu.addSeparator()

        quit_action = menu.addAction("Sair")
        quit_action.triggered.connect(self.quit_requested)

        self.setContextMenu(menu)

    def set_state(self, state: str, tooltip: str = ""):
        self._state = state
        icon_fn = _STATE_ICONS.get(state, icon_offline)
        self.setIcon(icon_fn())

        labels = {
            "synced": "Onedrive-Sync — Sincronizado",
            "syncing": "Onedrive-Sync — Sincronizando…",
            "paused": "Onedrive-Sync — Pausado",
            "error": "Onedrive-Sync — Erro",
            "offline": "Onedrive-Sync — Offline",
        }
        tip = tooltip or labels.get(state, "Onedrive-Sync")
        self.setToolTip(tip)
        self._status_action.setText(tip)

    def set_sync_dir(self, path: str):
        self._sync_dir = path

    def set_paused(self, paused: bool):
        self._paused = paused
        self._pause_action.setText("Retomar sincronização" if paused else "Pausar sincronização")
        self.set_state("paused" if paused else "synced")

    def show_message(self, title: str, message: str, icon=QSystemTrayIcon.MessageIcon.Information):
        if self.supportsMessages():
            self.showMessage(title, message, icon, 4000)

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_requested.emit(self._paused)
        self.set_paused(self._paused)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_main_window.emit()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_main_window.emit()

    def _open_sync_dir(self):
        path = Path(self._sync_dir)
        path.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            log.error("Could not open sync dir: %s", e)
