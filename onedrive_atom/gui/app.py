"""Qt application: wires tray, windows, sync engine, and file watcher together."""

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from onedrive_atom.config import get_config
from onedrive_atom.gui.tray import TrayIcon
from onedrive_atom.gui.windows.account_window import AccountWindow
from onedrive_atom.gui.windows.main_window import MainWindow
from onedrive_atom.gui.windows.settings_window import SettingsWindow
from onedrive_atom.sync.database import AccountRecord, DriveRecord, get_db
from onedrive_atom.sync.engine import SyncEvent, SyncManager
from onedrive_atom.sync.watcher import FileWatcher

log = logging.getLogger(__name__)


class Application(QObject):
    _sync_event_received = pyqtSignal(str, object)  # account_id, SyncEvent
    _drive_discovery_finished = pyqtSignal(str, bool, str)  # account_id, ask_sync_mode, error

    def __init__(self, qt_app: QApplication, start_minimized: bool = False):
        super().__init__()
        self._qt_app = qt_app
        self._start_minimized = start_minimized
        self._cfg = get_config()
        self._db = get_db()
        self._paused = False

        self._main_window: MainWindow | None = None
        self._account_window: AccountWindow | None = None
        self._settings_window: SettingsWindow | None = None

        self._sync_manager = SyncManager(status_cb=self._on_sync_event_threaded)
        self._watcher = FileWatcher(callback=self._on_local_change)

        self._tray = TrayIcon()
        self._tray.set_sync_dir(str(self._cfg.sync_base_dir))
        self._tray.show()

        self._wire_signals()
        self._sync_event_received.connect(self._on_sync_event)
        self._drive_discovery_finished.connect(self._on_drive_discovery_finished)

        self._watcher.start()
        self._update_watched_dirs()
        self._sync_manager.start_all()

        if not start_minimized:
            self._show_main_window()

        # Show tray icon balloon on first launch if no accounts
        if not self._db.get_accounts():
            self._tray.show_message(
                "Onedrive-Sync",
                "Clique com o botão direito no ícone para adicionar uma conta.",
            )

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self):
        t = self._tray
        t.show_main_window.connect(self._show_main_window)
        t.show_settings.connect(self._show_settings)
        t.show_accounts.connect(self._show_accounts)
        t.pause_requested.connect(self._set_paused)
        t.sync_now_requested.connect(self._sync_now)
        t.quit_requested.connect(self._quit)

    # ── Window management ─────────────────────────────────────────────────────

    @pyqtSlot()
    def _show_main_window(self):
        if self._main_window is None:
            self._main_window = MainWindow()
            self._main_window.sync_now_requested.connect(self._sync_now)
            self._main_window.open_accounts_requested.connect(self._show_accounts)
            self._main_window.open_settings_requested.connect(self._show_settings)

        self._main_window.refresh()
        self._main_window.show()
        self._main_window.raise_()
        self._main_window.activateWindow()

    @pyqtSlot()
    def _show_settings(self):
        if self._settings_window is None or not self._settings_window.isVisible():
            self._settings_window = SettingsWindow(self._main_window)
        self._settings_window.exec()

    @pyqtSlot()
    def _show_accounts(self):
        if self._account_window is None or not self._account_window.isVisible():
            self._account_window = AccountWindow(self._main_window)
            self._account_window.account_added.connect(self._on_account_added)
            self._account_window.account_removed.connect(self._on_account_removed)
            self._account_window.account_toggled.connect(self._on_account_toggled)
        self._account_window.exec()

    # ── Sync control ──────────────────────────────────────────────────────────

    @pyqtSlot(bool)
    def _set_paused(self, paused: bool):
        self._paused = paused
        if paused:
            self._sync_manager.stop_all()
            self._watcher.stop()
            self._tray.set_paused(True)
            self._tray.show_message("Sincronização pausada", "Nenhum arquivo será enviado ou baixado até retomar.")
        else:
            self._watcher.start()
            self._update_watched_dirs()
            self._sync_manager.start_all()
            self._tray.set_paused(False)
            self._tray.show_message("Sincronização retomada", "O monitoramento de arquivos foi reativado.")

    @pyqtSlot()
    def _sync_now(self):
        if self._paused:
            self._tray.show_message("Sincronização pausada", "Retome a sincronização antes de sincronizar agora.")
            return
        self._sync_manager.trigger_sync_now()
        self._tray.set_state("syncing")

    # ── Account events ────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_account_added(self, account_id: str):
        acc = self._db.get_account(account_id)
        if not acc:
            return

        self._tray.show_message("Conta adicionada", "Descobrindo drives antes da primeira sincronização.")
        self._discover_drives(acc, ask_sync_mode=True)

    @pyqtSlot(str)
    def _on_account_removed(self, account_id: str):
        self._sync_manager.stop_account(account_id)
        self._update_watched_dirs()

    @pyqtSlot(str, bool)
    def _on_account_toggled(self, account_id: str, enabled: bool):
        if enabled:
            acc = self._db.get_account(account_id)
            if acc and not self._paused:
                self._sync_manager.start_account(acc)
        else:
            self._sync_manager.stop_account(account_id)

    # ── Drive discovery ───────────────────────────────────────────────────────

    def _discover_drives(self, account: AccountRecord, ask_sync_mode: bool = False):
        """Fetches drives from the API and stores them in the database."""
        import threading
        threading.Thread(
            target=self._discover_drives_bg,
            args=(account, ask_sync_mode),
            daemon=True,
            name=f"discover-{account.email}",
        ).start()

    def _discover_drives_bg(self, account: AccountRecord, ask_sync_mode: bool = False):
        from onedrive_atom.api.graph import GraphClient, GraphError
        error = ""
        try:
            client = GraphClient(account.id)

            # Personal drive
            drive_info = client.get_default_drive()
            quota = drive_info.get("quota", {})
            self._db.upsert_drive(DriveRecord(
                id=drive_info["id"],
                account_id=account.id,
                name="OneDrive",
                drive_type=drive_info.get("driveType", "personal"),
                web_url=drive_info.get("webUrl", ""),
                quota_used=quota.get("used", 0),
                quota_total=quota.get("total", 0),
                enabled=True,
            ))

            # SharePoint / Teams drives
            for site in client.list_sharepoint_sites():
                for site_drive in client.get_site_drives(site["id"]):
                    quota2 = site_drive.get("quota", {})
                    self._db.upsert_drive(DriveRecord(
                        id=site_drive["id"],
                        account_id=account.id,
                        name=site_drive.get("name") or site.get("displayName", "SharePoint"),
                        drive_type="documentLibrary",
                        web_url=site_drive.get("webUrl", ""),
                        quota_used=quota2.get("used", 0),
                        quota_total=quota2.get("total", 0),
                        enabled=False,  # SharePoint drives opt-in by default
                    ))

            log.info("Drive discovery complete for %s", account.email)
        except GraphError as e:
            error = str(e)
            log.error("Drive discovery failed for %s: %s", account.email, e)
        except Exception as e:
            error = str(e)
            log.exception("Unexpected drive discovery failure for %s: %s", account.email, e)
        finally:
            self._drive_discovery_finished.emit(account.id, ask_sync_mode, error)

    @pyqtSlot(str, bool, str)
    def _on_drive_discovery_finished(self, account_id: str, ask_sync_mode: bool, error: str):
        acc = self._db.get_account(account_id)
        if not acc:
            return

        if error:
            QMessageBox.critical(
                self._main_window,
                "Erro ao descobrir drives",
                f"Não foi possível listar os drives da conta {acc.email}:\n\n{error}",
            )
            self._db.set_account_enabled(account_id, False)
            return

        if ask_sync_mode and not self._ask_initial_sync_mode(acc):
            self._db.set_account_enabled(account_id, False)
            if self._main_window:
                self._main_window.refresh()
            self._tray.show_message("Conta pausada", "A sincronização não foi iniciada.")
            return

        if self._paused:
            self._tray.show_message("Conta pronta", "A conta foi configurada, mas a sincronização está pausada.")
        else:
            self._sync_manager.start_account(acc)
            self._update_watched_dirs()

        if self._main_window:
            self._main_window.refresh()

        self._tray.show_message("Sincronização iniciada", f"Conta {acc.email} sincronizando.")

    def _ask_initial_sync_mode(self, account: AccountRecord) -> bool:
        msg = QMessageBox(self._main_window)
        msg.setWindowTitle("Primeira sincronização")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("Como deseja sincronizar esta conta?")
        msg.setInformativeText(
            "Você pode sincronizar todo o OneDrive agora ou escolher pastas específicas antes de iniciar."
        )
        sync_all_btn = msg.addButton("Sincronizar tudo", QMessageBox.ButtonRole.AcceptRole)
        selective_btn = msg.addButton("Escolher pastas", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = msg.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(selective_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == cancel_btn:
            return False

        if clicked == sync_all_btn:
            for drive in self._db.get_drives(account.id, enabled_only=False):
                self._db.set_selective_sync(drive.id, [])
            return True

        dlg = SettingsWindow(
            self._main_window,
            start_selective=True,
            selective_account_id=account.id,
            force_selective=True,
        )
        return dlg.exec() == SettingsWindow.DialogCode.Accepted

    # ── File watcher callback ─────────────────────────────────────────────────

    def _on_local_change(self, path: str, event_type: str):
        if self._paused:
            return
        self._sync_manager.enqueue_local_change(path, event_type)

    def _update_watched_dirs(self):
        accounts = self._db.get_accounts()
        dirs = [acc.sync_dir for acc in accounts if acc.sync_dir]
        self._tray.set_sync_dir(dirs[0] if len(dirs) == 1 else str(self._cfg.sync_base_dir))
        self._watcher.update_watched_dirs(dirs)

    # ── Sync event callbacks (cross-thread safe) ──────────────────────────────

    def _on_sync_event_threaded(self, account_id: str, event: SyncEvent):
        """Called from background thread — forward to GUI thread via signal."""
        self._sync_event_received.emit(account_id, event)

    @pyqtSlot(str, object)
    def _on_sync_event(self, account_id: str, event: SyncEvent):
        cfg = self._cfg
        kind = event.kind

        if self._paused:
            if self._main_window and self._main_window.isVisible() and kind == "error":
                self._main_window.add_activity(event.message, is_error=True)
            self._tray.set_paused(True)
            return

        if kind == "syncing" or kind == "upload" or kind == "download":
            self._tray.set_state("syncing")
        elif kind == "status" and "Synced" in event.message:
            self._tray.set_state("synced")
        elif kind == "error":
            self._tray.set_state("error")
            if cfg.get("notifications_enabled", True):
                self._tray.show_message(
                    "Erro de sincronização",
                    event.message[:100],
                    QSystemTrayIcon.MessageIcon.Critical,
                )
        elif kind == "conflict":
            if cfg.get("notifications_enabled", True):
                self._tray.show_message(
                    "Conflito detectado",
                    f"Cópia de conflito criada: {event.message}",
                    QSystemTrayIcon.MessageIcon.Warning,
                )

        if self._main_window and self._main_window.isVisible():
            is_err = kind == "error"
            msg = event.message or f"{kind}: {Path(event.path).name}" if event.path else kind
            self._main_window.add_activity(msg, is_error=is_err)

        # Update tray to synced after a brief delay (debounce)
        if kind in ("upload", "download"):
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(3000, lambda: None if self._paused else self._tray.set_state("synced"))

    # ── Quit ──────────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _quit(self):
        self._sync_manager.stop_all()
        self._watcher.stop()
        self._qt_app.quit()
