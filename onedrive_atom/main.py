"""Entry point for Onedrive-Sync."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(prog="onedrive-sync", description="Onedrive-Sync client for Linux")
    parser.add_argument("--minimized", action="store_true", help="Start minimized in system tray")
    parser.add_argument("--no-gui", action="store_true", help="Run as headless daemon (no GUI)")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    args = parser.parse_args()

    if args.version:
        from onedrive_atom import __version__
        print(f"Onedrive-Sync {__version__}")
        return

    from onedrive_atom.config import setup_logging
    setup_logging()

    if args.no_gui:
        _run_headless()
    else:
        _run_gui(start_minimized=args.minimized)


def _run_gui(start_minimized: bool = False):
    import logging
    log = logging.getLogger(__name__)

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    app = QApplication(sys.argv)
    app.setApplicationName("Onedrive-Sync")
    app.setOrganizationName("Atom Automacao")
    app.setQuitOnLastWindowClosed(False)

    if not QApplication.instance().style():
        app.setStyle("Fusion")

    from onedrive_atom.gui.icons import app_icon
    app.setWindowIcon(app_icon())

    from onedrive_atom.gui.app import Application
    _app = Application(app, start_minimized=start_minimized)  # noqa: F841 (keep reference)

    sys.exit(app.exec())


def _run_headless():
    """Headless mode: runs sync engine without GUI, useful for servers/CI."""
    import logging
    import signal
    import time

    log = logging.getLogger(__name__)
    log.info("Starting Onedrive-Sync in headless mode")

    from onedrive_atom.sync.database import get_db
    from onedrive_atom.sync.engine import SyncManager
    from onedrive_atom.sync.watcher import FileWatcher

    db = get_db()

    def _status_cb(account_id: str, event):
        log.info("[%s] %s: %s", account_id[:8], event.kind, event.message or event.path)

    manager = SyncManager(status_cb=_status_cb)

    def _on_local_change(path: str, event_type: str):
        manager.enqueue_local_change(path, event_type)

    watcher = FileWatcher(callback=_on_local_change)
    watcher.start()

    accounts = db.get_accounts()
    for acc in accounts:
        watcher.watch(acc.sync_dir)

    manager.start_all()

    stop = False

    def _handle_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Running. Press Ctrl+C or send SIGTERM to stop.")
    while not stop:
        time.sleep(1)

    log.info("Shutting down…")
    manager.stop_all()
    watcher.stop()


if __name__ == "__main__":
    main()
