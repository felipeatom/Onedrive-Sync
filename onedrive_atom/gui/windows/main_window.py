"""Main dashboard window showing sync status, recent activity, and drive usage."""

import logging
import subprocess
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from onedrive_atom.gui.icons import icon_folder, icon_synced, icon_syncing
from onedrive_atom.sync.database import get_db

log = logging.getLogger(__name__)


def _human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class DriveCard(QFrame):
    def __init__(self, drive, parent: QWidget | None = None):
        super().__init__(parent)
        self.drive = drive
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            QFrame {
                border: 1px solid #ddd;
                border-radius: 6px;
                background: #fafafa;
                padding: 4px;
            }
        """)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Name row
        name_row = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon_folder(20).pixmap(20, 20))
        name_lbl = QLabel(self.drive.name)
        font = name_lbl.font()
        font.setBold(True)
        name_lbl.setFont(font)
        name_row.addWidget(icon_lbl)
        name_row.addWidget(name_lbl)
        name_row.addStretch()

        type_lbl = QLabel(self.drive.drive_type)
        type_lbl.setStyleSheet("color: #888; font-size: 11px;")
        name_row.addWidget(type_lbl)
        layout.addLayout(name_row)

        # Quota bar
        if self.drive.quota_total and self.drive.quota_total > 0:
            pct = int(self.drive.quota_used / self.drive.quota_total * 100)
            bar = QProgressBar()
            bar.setValue(pct)
            bar.setMaximumHeight(8)
            bar.setTextVisible(False)
            if pct > 90:
                bar.setStyleSheet("QProgressBar::chunk { background: #d13438; }")
            elif pct > 70:
                bar.setStyleSheet("QProgressBar::chunk { background: #ffaa00; }")
            else:
                bar.setStyleSheet("QProgressBar::chunk { background: #0078d4; }")
            layout.addWidget(bar)

            used_lbl = QLabel(f"{_human_size(self.drive.quota_used)} de {_human_size(self.drive.quota_total)} usados")
            used_lbl.setStyleSheet("color: #666; font-size: 11px;")
            layout.addWidget(used_lbl)


class MainWindow(QWidget):
    sync_now_requested = pyqtSignal()
    open_accounts_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Onedrive-Sync")
        self.setMinimumSize(560, 500)
        self._db = get_db()
        self._activity_initialized = False
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(5000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header bar
        header = QFrame()
        header.setStyleSheet("QFrame { background: #0078d4; }")
        header.setFixedHeight(56)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)

        title = QLabel("Onedrive-Sync")
        title.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self._status_lbl = QLabel("Offline")
        self._status_lbl.setStyleSheet("color: rgba(255,255,255,0.85); font-size: 12px;")
        header_layout.addWidget(self._status_lbl)

        layout.addWidget(header)

        # Content
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(12)

        # Left: accounts + drives
        left = QVBoxLayout()

        accounts_lbl = QLabel("Drives sincronizados")
        font = accounts_lbl.font()
        font.setPointSize(11)
        font.setBold(True)
        accounts_lbl.setFont(font)
        left.addWidget(accounts_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._drives_container = QWidget()
        self._drives_layout = QVBoxLayout(self._drives_container)
        self._drives_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._drives_container)
        left.addWidget(scroll, 1)

        btn_add_account = QPushButton("+ Adicionar conta")
        btn_add_account.setStyleSheet("QPushButton { color: #0078d4; border: none; text-align: left; }")
        btn_add_account.clicked.connect(self.open_accounts_requested)
        left.addWidget(btn_add_account)

        # Right: activity log
        right = QVBoxLayout()

        activity_lbl = QLabel("Atividade recente")
        font2 = activity_lbl.font()
        font2.setPointSize(11)
        font2.setBold(True)
        activity_lbl.setFont(font2)
        right.addWidget(activity_lbl)

        self._activity_list = QListWidget()
        self._activity_list.setAlternatingRowColors(True)
        right.addWidget(self._activity_list, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_w = QWidget()
        left_w.setLayout(left)
        right_w = QWidget()
        right_w.setLayout(right)
        splitter.addWidget(left_w)
        splitter.addWidget(right_w)
        splitter.setSizes([240, 300])
        content_layout.addWidget(splitter)

        layout.addWidget(content, 1)

        # Bottom toolbar
        toolbar = QFrame()
        toolbar.setStyleSheet("QFrame { background: #f3f3f3; border-top: 1px solid #ddd; }")
        toolbar.setFixedHeight(44)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(12, 0, 12, 0)

        btn_sync = QPushButton("Sincronizar agora")
        btn_sync.setStyleSheet("QPushButton { background: #0078d4; color: white; border-radius: 4px; padding: 4px 12px; }")
        btn_sync.clicked.connect(self.sync_now_requested)
        tb_layout.addWidget(btn_sync)
        tb_layout.addStretch()

        btn_settings = QPushButton("Configurações")
        btn_settings.clicked.connect(self.open_settings_requested)
        tb_layout.addWidget(btn_settings)

        layout.addWidget(toolbar)

    def refresh(self):
        self._refresh_drives()
        if not self._activity_initialized:
            self._refresh_activity()
            self._activity_initialized = True

    def set_status(self, text: str):
        self._status_lbl.setText(text)

    def add_activity(self, message: str, is_error: bool = False):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{ts}] {message}")
        if is_error:
            item.setForeground(__import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#d13438"))
        self._activity_list.insertItem(0, item)
        if self._activity_list.count() > 200:
            self._activity_list.takeItem(self._activity_list.count() - 1)

    def _refresh_drives(self):
        # Clear existing cards
        while self._drives_layout.count():
            w = self._drives_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        drives = self._db.get_drives()
        if not drives:
            lbl = QLabel("Nenhuma conta conectada.\nClique em '+ Adicionar conta'.")
            lbl.setStyleSheet("color: #888; font-size: 12px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._drives_layout.addWidget(lbl)
            return

        for drive in drives:
            card = DriveCard(drive)
            self._drives_layout.addWidget(card)

    def _refresh_activity(self):
        logs = self._db.get_recent_logs(50)
        self._activity_list.clear()
        for entry in logs:
            ts = entry.get("ts", "")[:19]
            action = entry.get("action", "")
            status = entry.get("status", "")
            msg = entry.get("message", "")
            text = f"[{ts}] {action.upper()} {status} — {msg}" if msg else f"[{ts}] {action.upper()} {status}"
            item = QListWidgetItem(text)
            if status == "error":
                item.setForeground(__import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#d13438"))
            self._activity_list.addItem(item)
