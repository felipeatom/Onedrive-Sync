"""Main commercial-style OneDrive client window."""

import logging
import subprocess
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from onedrive_atom.gui.icons import icon_folder
from onedrive_atom.sync.database import DriveRecord, get_db

log = logging.getLogger(__name__)


def _human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _safe_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def _open_path(path: str):
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        log.error("Could not open path %s: %s", target, e)


class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "0", detail: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(5)

        title = QLabel(label.upper())
        title.setObjectName("metricLabel")
        layout.addWidget(title)

        self._value = QLabel(value)
        self._value.setObjectName("metricValue")
        layout.addWidget(self._value)

        self._detail = QLabel(detail)
        self._detail.setObjectName("muted")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

    def set_values(self, value: str, detail: str = ""):
        self._value.setText(value)
        self._detail.setText(detail)


class DriveCard(QFrame):
    def __init__(self, drive: DriveRecord, parent: QWidget | None = None):
        super().__init__(parent)
        self.drive = drive
        self._db = get_db()
        self.setObjectName("driveCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._build()

    def _build(self):
        account = self._db.get_account(self.drive.account_id)
        local_path = ""
        if account and account.sync_dir:
            local_path = str(Path(account.sync_dir) / _safe_name(self.drive.name))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(11)

        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(icon_folder(24).pixmap(24, 24))
        top.addWidget(icon)

        names = QVBoxLayout()
        names.setSpacing(1)
        name = QLabel(self.drive.name)
        name.setObjectName("driveName")
        names.addWidget(name)
        email = QLabel(account.email if account else "Conta desconhecida")
        email.setObjectName("muted")
        names.addWidget(email)
        top.addLayout(names, 1)

        badge = QLabel("sincronizando" if self.drive.enabled else "desativado")
        badge.setObjectName("goodBadge" if self.drive.enabled else "warnBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(badge)
        layout.addLayout(top)

        location = QLabel(local_path or "Pasta local não configurada")
        location.setObjectName("muted")
        location.setWordWrap(True)
        layout.addWidget(location)

        if self.drive.quota_total:
            pct = min(int((self.drive.quota_used or 0) / self.drive.quota_total * 100), 100)
            bar = QProgressBar()
            bar.setObjectName("quotaBar")
            bar.setValue(pct)
            bar.setMaximumHeight(7)
            bar.setTextVisible(False)
            layout.addWidget(bar)
            quota = QLabel(f"{_human_size(self.drive.quota_used or 0)} / {_human_size(self.drive.quota_total)} ({pct}%)")
        else:
            quota = QLabel("Cota não informada")
        quota.setObjectName("muted")
        layout.addWidget(quota)

        items = self._db.get_items_by_drive(self.drive.id)
        folders = sum(1 for item in items if item.is_folder)
        files = len(items) - folders
        details = QLabel(f"{files} arquivos  |  {folders} pastas  |  {self.drive.drive_type or 'drive'}")
        details.setObjectName("muted")
        layout.addWidget(details)

        actions = QHBoxLayout()
        local_btn = QPushButton("Abrir local")
        local_btn.setEnabled(bool(local_path))
        local_btn.clicked.connect(lambda: _open_path(local_path))
        actions.addWidget(local_btn)

        web_btn = QPushButton("Abrir web")
        web_btn.setEnabled(bool(self.drive.web_url))
        web_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.drive.web_url)))
        actions.addWidget(web_btn)
        actions.addStretch()
        layout.addLayout(actions)


class MainWindow(QWidget):
    sync_now_requested = pyqtSignal()
    open_accounts_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Onedrive-Sync")
        self.setMinimumSize(1040, 700)
        self._db = get_db()
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(5000)

    def _build_ui(self):
        self.setObjectName("mainWindow")
        self.setStyleSheet("""
            QWidget#mainWindow { background: #eef4fb; color: #1f2937; }
            QFrame#shell { background: #ffffff; border: 1px solid #dce7f3; border-radius: 22px; }
            QFrame#topBar { background: #ffffff; border-bottom: 1px solid #edf2f7; }
            QLabel#brandMark {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #18c7a1, stop:1 #2488ff);
                color: white; border-radius: 16px; font-size: 18px; font-weight: 900;
            }
            QLabel#appTitle { color: #172033; font-size: 20px; font-weight: 900; }
            QLabel#appSub { color: #7b8798; font-size: 12px; }
            QLabel#sectionTitle { color: #2f3138; font-size: 16px; font-weight: 800; }
            QLabel#muted { color: #7d8492; font-size: 11px; }
            QLabel#bigTitle { color: #172033; font-size: 26px; font-weight: 900; }
            QLabel#heroText { color: #4b6078; font-size: 13px; }
            QLabel#statusBadge, QLabel#goodBadge, QLabel#warnBadge {
                border-radius: 12px; padding: 5px 12px; font-size: 12px; font-weight: 800;
            }
            QLabel#statusBadge, QLabel#goodBadge { background: #dff8ed; color: #16864f; }
            QLabel#warnBadge { background: #fff2cc; color: #9a6a00; }
            QLabel#driveName { color: #30323a; font-size: 15px; font-weight: 800; }
            QFrame#metricCard, QFrame#driveCard, QFrame#panel, QFrame#welcomeCard {
                background: #ffffff; border: 1px solid #e3e8f0; border-radius: 14px;
            }
            QFrame#welcomeCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #e9fff7, stop:0.45 #ffffff, stop:1 #eaf3ff);
                border: 1px solid #d7edf8; border-radius: 22px;
            }
            QLabel#metricLabel { color: #8b93a3; font-size: 10px; font-weight: 800; letter-spacing: 0.8px; }
            QLabel#metricValue { color: #172033; font-size: 27px; font-weight: 900; }
            QPushButton {
                background: #ffffff; color: #30323a; border: 1px solid #d8dee8;
                border-radius: 9px; padding: 8px 13px; font-weight: 700;
            }
            QPushButton:hover { background: #f1f5fb; border-color: #c8d2e2; }
            QPushButton:disabled { color: #a2a9b6; background: #f3f5f8; border-color: #e5e9f0; }
            QPushButton#primaryButton { background: #2f80ed; border-color: #2f80ed; color: white; }
            QPushButton#primaryButton:hover { background: #1f6fda; }
            QPushButton#successButton { background: #31c77f; border-color: #31c77f; color: white; }
            QPushButton#successButton:hover { background: #25b36f; }
            QTabWidget::pane { border: none; background: transparent; }
            QTabWidget::tab-bar { alignment: left; }
            QTabBar::tab {
                background: transparent; color: #667085; padding: 11px 16px; margin: 8px 4px 6px 4px;
                border: none; border-radius: 11px; font-weight: 800; min-width: 92px;
            }
            QTabBar::tab:selected { color: #1769d8; background: #e9f2ff; }
            QTabBar::tab:hover { color: #1769d8; background: #f3f8ff; }
            QProgressBar#quotaBar { background: #ecf1f7; border: none; border-radius: 4px; }
            QProgressBar#quotaBar::chunk { background: #42a5f5; border-radius: 4px; }
            QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { border: none; background: transparent; }
            QListWidget, QTreeWidget {
                background: #ffffff; border: 1px solid #e3e8f0; border-radius: 12px;
                color: #30323a; alternate-background-color: #f8fafc; outline: 0;
            }
            QListWidget::item, QTreeWidget::item { padding: 6px; border-radius: 6px; }
            QListWidget::item:selected, QTreeWidget::item:selected { background: #e8f1ff; color: #1d4f91; }
            QHeaderView::section { background: #f8fafc; color: #687181; border: none; padding: 7px; font-weight: 800; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
            QScrollBar::handle:vertical { background: #cfd7e3; border-radius: 5px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar:horizontal { background: transparent; height: 0px; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)

        shell = QFrame()
        shell.setObjectName("shell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        root.addWidget(shell, 1)

        top = QFrame()
        top.setObjectName("topBar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(22, 16, 22, 16)

        brand_mark = QLabel("OS")
        brand_mark.setObjectName("brandMark")
        brand_mark.setFixedSize(42, 42)
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(brand_mark)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Onedrive-Sync")
        title.setObjectName("appTitle")
        title_box.addWidget(title)
        subtitle = QLabel("Cliente OneDrive para Linux")
        subtitle.setObjectName("appSub")
        title_box.addWidget(subtitle)
        top_layout.addLayout(title_box, 1)

        self._status_lbl = QLabel("Offline")
        self._status_lbl.setObjectName("statusBadge")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self._status_lbl)
        shell_layout.addWidget(top)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.addTab(self._build_dashboard_tab(), "Visão geral")
        self._tabs.addTab(self._build_explorer_tab(), "Arquivos")
        self._tabs.addTab(self._build_remotes_tab(), "Drives")
        self._tabs.addTab(self._build_activity_tab(), "Atividade")
        self._tabs.addTab(self._build_settings_tab(), "Ajustes")
        shell_layout.addWidget(self._tabs, 1)

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(18)

        welcome = QFrame()
        welcome.setObjectName("welcomeCard")
        welcome_layout = QHBoxLayout(welcome)
        welcome_layout.setContentsMargins(26, 24, 26, 24)

        copy = QVBoxLayout()
        copy.setSpacing(8)
        title = QLabel("Seus arquivos do OneDrive, no Linux")
        title.setObjectName("bigTitle")
        copy.addWidget(title)
        subtitle = QLabel("Sincronize, acompanhe alterações e escolha exatamente quais pastas ficam disponíveis neste computador.")
        subtitle.setObjectName("heroText")
        subtitle.setWordWrap(True)
        copy.addWidget(subtitle)
        welcome_layout.addLayout(copy, 1)

        add_btn = QPushButton("Adicionar conta")
        add_btn.setObjectName("successButton")
        add_btn.clicked.connect(self.open_accounts_requested)
        welcome_layout.addWidget(add_btn)

        sync_btn = QPushButton("Sincronizar agora")
        sync_btn.setObjectName("primaryButton")
        sync_btn.clicked.connect(self.sync_now_requested)
        welcome_layout.addWidget(sync_btn)
        layout.addWidget(welcome)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        self._accounts_card = MetricCard("Contas")
        self._drives_card = MetricCard("Drives")
        self._items_card = MetricCard("Itens sincronizados")
        self._storage_card = MetricCard("Armazenamento")
        metrics.addWidget(self._accounts_card, 0, 0)
        metrics.addWidget(self._drives_card, 0, 1)
        metrics.addWidget(self._items_card, 0, 2)
        metrics.addWidget(self._storage_card, 0, 3)
        layout.addLayout(metrics)

        lower = QHBoxLayout()
        self._dashboard_remotes = self._new_panel("Resumo dos drives")
        self._dashboard_activity = self._new_panel("Atividade recente")
        lower.addWidget(self._dashboard_remotes, 1)
        lower.addWidget(self._dashboard_activity, 1)
        layout.addLayout(lower, 1)
        return tab

    def _build_explorer_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Arquivos sincronizados")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        header.addStretch()
        open_base = QPushButton("Abrir pasta local")
        open_base.clicked.connect(self._open_first_sync_dir)
        header.addWidget(open_base)
        layout.addLayout(header)

        self._explorer_tree = QTreeWidget()
        self._explorer_tree.setHeaderLabels(["Nome", "Tipo", "Caminho"])
        self._explorer_tree.setAlternatingRowColors(True)
        layout.addWidget(self._explorer_tree, 1)
        return tab

    def _build_remotes_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Drives conectados")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        header.addStretch()
        add_btn = QPushButton("Adicionar conta")
        add_btn.setObjectName("successButton")
        add_btn.clicked.connect(self.open_accounts_requested)
        header.addWidget(add_btn)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._remotes_container = QWidget()
        self._remotes_layout = QVBoxLayout(self._remotes_container)
        self._remotes_layout.setContentsMargins(0, 0, 0, 0)
        self._remotes_layout.setSpacing(10)
        self._remotes_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._remotes_container)
        layout.addWidget(scroll, 1)
        return tab

    def _build_activity_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Atividade")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        header.addStretch()
        refresh = QPushButton("Atualizar")
        refresh.clicked.connect(self._refresh_activity)
        header.addWidget(refresh)
        layout.addLayout(header)

        self._activity_list = QListWidget()
        self._activity_list.setAlternatingRowColors(True)
        self._activity_list.setWordWrap(True)
        self._activity_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self._activity_list, 1)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("panel")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel("Ajustes")
        title.setObjectName("bigTitle")
        card_layout.addWidget(title)
        detail = QLabel("Gerencie contas, sincronização seletiva e preferências do aplicativo.")
        detail.setObjectName("muted")
        card_layout.addWidget(detail)

        buttons = QHBoxLayout()
        accounts = QPushButton("Contas")
        accounts.clicked.connect(self.open_accounts_requested)
        buttons.addWidget(accounts)
        settings = QPushButton("Preferências")
        settings.setObjectName("primaryButton")
        settings.clicked.connect(self.open_settings_requested)
        buttons.addWidget(settings)
        buttons.addStretch()
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch()
        return tab

    def _new_panel(self, title: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        body = QListWidget()
        body.setAlternatingRowColors(True)
        body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(body, 1)
        panel._list = body  # type: ignore[attr-defined]
        return panel

    def refresh(self):
        self._refresh_summary()
        self._refresh_remotes()
        self._refresh_explorer()
        self._refresh_activity()

    def set_status(self, text: str):
        self._status_lbl.setText(text)
        self._status_lbl.setObjectName("warnBadge" if text.lower().startswith(("erro", "paus")) else "statusBadge")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)

    def add_activity(self, message: str, is_error: bool = False):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{ts}] {message}")
        if is_error:
            item.setForeground(QColor("#d93025"))
        self._activity_list.insertItem(0, item)
        if self._activity_list.count() > 200:
            self._activity_list.takeItem(self._activity_list.count() - 1)

    def _refresh_summary(self):
        accounts = self._db.get_accounts(enabled_only=False)
        drives = self._db.get_drives(enabled_only=False)
        active_accounts = [account for account in accounts if account.enabled]
        active_drives = [drive for drive in drives if drive.enabled]

        all_items = []
        for drive in drives:
            all_items.extend(self._db.get_items_by_drive(drive.id))
        folders = sum(1 for item in all_items if item.is_folder)
        files = len(all_items) - folders

        used = sum(drive.quota_used or 0 for drive in drives)
        total = sum(drive.quota_total or 0 for drive in drives)
        storage_detail = f"{_human_size(used)} usados"
        if total:
            storage_detail += f" de {_human_size(total)}"

        self._accounts_card.set_values(str(len(active_accounts)), f"{len(accounts)} cadastradas")
        self._drives_card.set_values(str(len(active_drives)), f"{len(drives)} encontrados")
        self._items_card.set_values(str(len(all_items)), f"{files} arquivos | {folders} pastas")
        self._storage_card.set_values(_human_size(used), storage_detail)

        remotes_list = self._dashboard_remotes._list  # type: ignore[attr-defined]
        remotes_list.clear()
        if not drives:
            remotes_list.addItem("Nenhum drive conectado ainda.")
        else:
            for drive in sorted(drives, key=lambda d: (d.quota_used or 0) / d.quota_total if d.quota_total else 0, reverse=True)[:6]:
                pct = int((drive.quota_used or 0) / drive.quota_total * 100) if drive.quota_total else 0
                remotes_list.addItem(f"{drive.name}  |  {pct}% usado  |  {drive.drive_type or 'drive'}")

    def _refresh_remotes(self):
        while self._remotes_layout.count():
            widget = self._remotes_layout.takeAt(0).widget()
            if widget:
                widget.deleteLater()

        drives = self._db.get_drives(enabled_only=False)
        if not drives:
            empty = QLabel("Nenhuma conta ainda. Use Adicionar conta para conectar o OneDrive.")
            empty.setObjectName("muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._remotes_layout.addWidget(empty)
            return

        for drive in drives:
            self._remotes_layout.addWidget(DriveCard(drive))

    def _refresh_explorer(self):
        self._explorer_tree.clear()
        accounts = self._db.get_accounts(enabled_only=False)
        if not accounts:
            QTreeWidgetItem(self._explorer_tree, ["Nenhuma conta configurada", "", ""])
            return

        for account in accounts:
            account_node = QTreeWidgetItem(self._explorer_tree, [account.email, "conta", account.sync_dir])
            for drive in self._db.get_drives(account.id, enabled_only=False):
                drive_path = str(Path(account.sync_dir) / _safe_name(drive.name)) if account.sync_dir else ""
                drive_node = QTreeWidgetItem(account_node, [drive.name, drive.drive_type or "drive", drive_path])
                folders = [item for item in self._db.get_items_by_drive(drive.id) if item.is_folder]
                for item in sorted(folders, key=lambda i: i.remote_path)[:250]:
                    name = Path(item.remote_path).name or item.remote_path.strip("/") or "/"
                    QTreeWidgetItem(drive_node, [name, "pasta", item.remote_path])
            account_node.setExpanded(True)
        self._explorer_tree.resizeColumnToContents(0)

    def _refresh_activity(self):
        logs = self._db.get_recent_logs(100)
        self._activity_list.clear()

        dash_list = self._dashboard_activity._list  # type: ignore[attr-defined]
        dash_list.clear()

        if not logs:
            empty = QListWidgetItem("Nenhuma atividade registrada ainda.")
            empty.setForeground(QColor("#7d8492"))
            self._activity_list.addItem(empty)
            dash_list.addItem("Nenhuma atividade registrada ainda.")
            return

        for index, entry in enumerate(logs):
            ts = entry.get("ts", "")[:19]
            action = entry.get("action", "")
            status = entry.get("status", "")
            msg = entry.get("message", "")
            text = f"[{ts}] {action.upper()} {status} - {msg}" if msg else f"[{ts}] {action.upper()} {status}"
            item = QListWidgetItem(text)
            if status == "error":
                item.setForeground(QColor("#d93025"))
            elif status == "ok":
                item.setForeground(QColor("#137333"))
            self._activity_list.addItem(item)
            if index < 8:
                dash_list.addItem(text)

    def _open_first_sync_dir(self):
        accounts = self._db.get_accounts(enabled_only=False)
        if accounts and accounts[0].sync_dir:
            _open_path(accounts[0].sync_dir)
