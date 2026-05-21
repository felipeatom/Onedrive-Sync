"""Settings window."""

import logging
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from onedrive_atom.config import get_config
from onedrive_atom.sync.database import get_db

log = logging.getLogger(__name__)

AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "onedrive-sync.desktop"
AUTOSTART_CONTENT = """\
[Desktop Entry]
Type=Application
Name=Onedrive-Sync
Exec=onedrive-sync --minimized
Icon=onedrive-sync
Comment=Onedrive-Sync client
X-GNOME-Autostart-enabled=true
"""


class SettingsWindow(QDialog):
    _sig_folder_tree_loaded = pyqtSignal(str, object, list)
    _sig_folder_tree_error = pyqtSignal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        start_selective: bool = False,
        selective_account_id: str | None = None,
        force_selective: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configurações — Onedrive-Sync")
        self.setMinimumSize(720, 560)
        self._cfg = get_config()
        self._db = get_db()
        self._start_selective = start_selective
        self._selective_account_id = selective_account_id
        self._force_selective = force_selective
        self._selective_current_drive_id: str | None = None
        self._selective_values: dict[str, list[str]] = {}
        self._selective_loading = False
        self._selective_loading_items: set[str] = set()
        self._build_ui()
        self._sig_folder_tree_loaded.connect(self._on_folder_tree_loaded)
        self._sig_folder_tree_error.connect(self._on_folder_tree_error)
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._general_tab(), "Geral")
        self._tabs.addTab(self._sync_tab(), "Sincronização")
        self._tabs.addTab(self._selective_tab(), "Seletiva")
        self._tabs.addTab(self._advanced_tab(), "Avançado")
        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── Tabs ──────────────────────────────────────────────────────────────────

    def _general_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Sync directory
        dir_box = QGroupBox("Pasta de sincronização base")
        dir_layout = QHBoxLayout(dir_box)

        self._sync_dir_edit = QLineEdit()
        btn_browse = QPushButton("Procurar…")
        btn_browse.clicked.connect(self._browse_sync_dir)
        dir_layout.addWidget(self._sync_dir_edit)
        dir_layout.addWidget(btn_browse)
        layout.addWidget(dir_box)

        # Startup
        startup_box = QGroupBox("Inicialização")
        startup_layout = QVBoxLayout(startup_box)

        self._autostart_cb = QCheckBox("Iniciar com o sistema (autostart)")
        self._minimized_cb = QCheckBox("Iniciar minimizado na bandeja")
        self._notifications_cb = QCheckBox("Mostrar notificações")
        startup_layout.addWidget(self._autostart_cb)
        startup_layout.addWidget(self._minimized_cb)
        startup_layout.addWidget(self._notifications_cb)
        layout.addWidget(startup_box)

        layout.addStretch()
        return w

    def _sync_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(10, 3600)
        self._interval_spin.setSuffix(" segundos")
        form.addRow("Intervalo de sync:", self._interval_spin)

        self._conflict_combo = QComboBox()
        self._conflict_combo.addItems([
            "Versão mais nova vence",
            "Manter ambas (cópia de conflito)",
            "Sempre preferir remoto",
        ])
        form.addRow("Resolução de conflitos:", self._conflict_combo)

        self._hidden_cb = QCheckBox("Sincronizar arquivos ocultos (começam com .)")
        form.addRow(self._hidden_cb)

        self._max_size_spin = QSpinBox()
        self._max_size_spin.setRange(1, 10240)
        self._max_size_spin.setSuffix(" MB")
        form.addRow("Tamanho máximo de arquivo:", self._max_size_spin)

        return w

    def _selective_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Escolha visualmente quais pastas remotas sincronizar em cada drive. "
            "A árvore mostra as pastas do OneDrive e salva apenas os caminhos marcados."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(info)

        body = QHBoxLayout()

        self._selective_drive_list = QListWidget()
        self._selective_drive_list.setMinimumWidth(180)
        self._selective_drive_list.currentItemChanged.connect(self._on_selective_drive_changed)
        body.addWidget(self._selective_drive_list, 1)

        right = QVBoxLayout()
        self._selective_title = QLabel("Nenhum drive selecionado")
        font = self._selective_title.font()
        font.setBold(True)
        self._selective_title.setFont(font)
        right.addWidget(self._selective_title)

        self._selective_all_cb = QCheckBox("Sincronizar tudo neste drive")
        self._selective_all_cb.toggled.connect(self._on_selective_all_toggled)
        right.addWidget(self._selective_all_cb)

        btn_row = QHBoxLayout()
        self._selective_load_btn = QPushButton("Carregar árvore")
        self._selective_load_btn.clicked.connect(self._load_selected_drive_tree)
        self._selective_check_all_btn = QPushButton("Marcar tudo")
        self._selective_check_all_btn.clicked.connect(lambda: self._set_tree_checked(True))
        self._selective_clear_btn = QPushButton("Limpar seleção")
        self._selective_clear_btn.clicked.connect(lambda: self._set_tree_checked(False))
        btn_row.addWidget(self._selective_load_btn)
        btn_row.addWidget(self._selective_check_all_btn)
        btn_row.addWidget(self._selective_clear_btn)
        btn_row.addStretch()
        right.addLayout(btn_row)

        self._selective_tree = QTreeWidget()
        self._selective_tree.setHeaderLabels(["Pasta remota"])
        self._selective_tree.itemChanged.connect(self._on_tree_item_changed)
        self._selective_tree.itemExpanded.connect(self._on_tree_item_expanded)
        right.addWidget(self._selective_tree, 1)

        hint = QLabel("Se 'Sincronizar tudo' estiver desmarcado, apenas as pastas marcadas serão sincronizadas.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        right.addWidget(hint)

        body.addLayout(right, 2)
        layout.addLayout(body, 1)
        return w

    def _advanced_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._client_id_edit = QLineEdit()
        self._client_id_edit.setPlaceholderText("ID do aplicativo Azure (opcional)")
        form.addRow("Azure App Client ID:", self._client_id_edit)

        lbl = QLabel(
            "Para registrar seu próprio aplicativo, acesse:\n"
            "portal.azure.com → Azure Active Directory → Registros de aplicativo\n"
            "Adicione permissões: Files.ReadWrite.All, Sites.ReadWrite.All"
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #666; font-size: 11px;")
        form.addRow(lbl)

        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        form.addRow("Nível de log:", self._log_level_combo)

        btn_open_logs = QPushButton("Abrir pasta de logs")
        btn_open_logs.clicked.connect(self._open_logs)
        form.addRow(btn_open_logs)

        return w

    # ── Load / Save ───────────────────────────────────────────────────────────

    def _load_values(self):
        self._sync_dir_edit.setText(self._cfg.get("sync_base_dir", ""))
        self._autostart_cb.setChecked(AUTOSTART_FILE.exists())
        self._minimized_cb.setChecked(self._cfg.get("start_minimized", True))
        self._notifications_cb.setChecked(self._cfg.get("notifications_enabled", True))
        self._interval_spin.setValue(self._cfg.get("sync_interval_seconds", 30))
        self._hidden_cb.setChecked(self._cfg.get("sync_hidden_files", False))
        self._max_size_spin.setValue(self._cfg.get("max_file_size_mb", 250))
        self._client_id_edit.setText(self._cfg.get("client_id", ""))

        conflict_map = {
            "newer_wins": 0,
            "keep_both": 1,
            "prefer_remote": 2,
        }
        self._conflict_combo.setCurrentIndex(
            conflict_map.get(self._cfg.get("conflict_resolution", "newer_wins"), 0)
        )

        level_map = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
        self._log_level_combo.setCurrentIndex(
            level_map.get(self._cfg.get("log_level", "INFO"), 1)
        )
        self._load_selective_drives()
        if self._start_selective:
            self._tabs.setCurrentIndex(2)

    def _save_and_close(self):
        self._store_current_selective_paths()

        if self._force_selective and not any(self._selective_values.values()):
            QMessageBox.warning(
                self,
                "Sincronização seletiva",
                "Selecione pelo menos uma pasta para iniciar a sincronização seletiva.\n\n"
                "Se quiser sincronizar tudo, volte e escolha 'Sincronizar tudo'.",
            )
            return

        self._cfg.set("sync_base_dir", self._sync_dir_edit.text())
        self._cfg.set("start_minimized", self._minimized_cb.isChecked())
        self._cfg.set("notifications_enabled", self._notifications_cb.isChecked())
        self._cfg.set("sync_interval_seconds", self._interval_spin.value())
        self._cfg.set("sync_hidden_files", self._hidden_cb.isChecked())
        self._cfg.set("max_file_size_mb", self._max_size_spin.value())
        self._cfg.set("log_level", self._log_level_combo.currentText())

        if self._client_id_edit.text().strip():
            self._cfg.set("client_id", self._client_id_edit.text().strip())

        conflict_values = ["newer_wins", "keep_both", "prefer_remote"]
        self._cfg.set("conflict_resolution", conflict_values[self._conflict_combo.currentIndex()])

        for drive_id, paths in self._selective_values.items():
            self._db.set_selective_sync(drive_id, paths)

        # Autostart
        if self._autostart_cb.isChecked():
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(AUTOSTART_CONTENT)
        elif AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink()

        self.accept()

    def _browse_sync_dir(self):
        current = self._sync_dir_edit.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Escolha a pasta de sincronização", current)
        if path:
            self._sync_dir_edit.setText(path)

    def _open_logs(self):
        from onedrive_atom.config import LOG_DIR
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(LOG_DIR)])

    def _load_selective_drives(self):
        self._selective_drive_list.clear()
        self._selective_values.clear()

        drives = self._db.get_drives(self._selective_account_id, enabled_only=False)
        for drive in drives:
            self._selective_values[drive.id] = self._db.get_selective_sync(drive.id)
            item = QListWidgetItem(f"{drive.name}\n{drive.drive_type}")
            item.setData(Qt.ItemDataRole.UserRole, drive.id)
            item.setToolTip("Sincroniza tudo" if not self._selective_values[drive.id] else "Sincronização seletiva ativa")
            self._selective_drive_list.addItem(item)

        if drives:
            self._selective_drive_list.setCurrentRow(0)
        else:
            self._selective_tree.setEnabled(False)
            self._selective_tree.clear()
            self._selective_all_cb.setEnabled(False)
            self._selective_load_btn.setEnabled(False)
            self._selective_title.setText("Nenhum drive disponível")

    def _on_selective_drive_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None):
        self._store_current_selective_paths()
        if current is None:
            self._selective_current_drive_id = None
            return

        drive_id = current.data(Qt.ItemDataRole.UserRole)
        self._selective_current_drive_id = drive_id
        self._selective_title.setText(current.text().split("\n", 1)[0])
        self._selective_tree.clear()
        self._selective_tree.setEnabled(True)
        self._selective_all_cb.setEnabled(True)
        self._selective_load_btn.setEnabled(True)
        self._selective_all_cb.blockSignals(True)
        sync_all = not self._selective_values.get(drive_id, []) and not self._force_selective
        self._selective_all_cb.setChecked(sync_all)
        self._selective_all_cb.blockSignals(False)
        self._update_selective_tree_enabled()
        if not sync_all:
            QTimer.singleShot(0, self._load_selected_drive_tree)

    def _store_current_selective_paths(self):
        drive_id = self._selective_current_drive_id
        if not drive_id:
            return
        if self._selective_all_cb.isChecked():
            self._selective_values[drive_id] = []
            return
        self._selective_values[drive_id] = self._checked_tree_paths()

    def _load_selected_drive_tree(self):
        drive_id = self._selective_current_drive_id
        if not drive_id or self._selective_loading:
            return

        if self._selective_all_cb.isChecked():
            self._selective_all_cb.setChecked(False)

        account_id = None
        for drive in self._db.get_drives(enabled_only=False):
            if drive.id == drive_id:
                account_id = drive.account_id
                break
        if not account_id:
            QMessageBox.warning(self, "Sincronização seletiva", "Conta do drive não encontrada.")
            return

        self._selective_loading = True
        self._selective_load_btn.setEnabled(False)
        self._selective_load_btn.setText("Carregando...")
        self._selective_tree.clear()
        self._selective_tree.addTopLevelItem(QTreeWidgetItem(["Carregando pastas..."]))

        def _load():
            try:
                from onedrive_atom.api.graph import GraphClient
                folders = GraphClient(account_id).list_folders(drive_id)
                self._sig_folder_tree_loaded.emit(drive_id, None, folders)
            except Exception as e:
                log.exception("Could not load selective sync tree: %s", e)
                self._sig_folder_tree_error.emit(str(e))

        threading.Thread(target=_load, daemon=True, name="load-selective-tree").start()

    @pyqtSlot(str, object, list)
    def _on_folder_tree_loaded(self, drive_id: str, parent_item: QTreeWidgetItem | None, folders: list):
        if parent_item is None:
            self._selective_loading = False
            self._selective_load_btn.setEnabled(True)
            self._selective_load_btn.setText("Recarregar árvore")
        if drive_id != self._selective_current_drive_id:
            return

        selected = set(self._selective_values.get(drive_id, []))
        self._selective_tree.blockSignals(True)
        if parent_item is None:
            self._selective_tree.clear()
            for folder in folders:
                self._add_folder_item(None, folder, selected)
            if not folders:
                self._selective_tree.addTopLevelItem(QTreeWidgetItem(["Nenhuma pasta encontrada na raiz deste drive."]))
            self._selective_tree.expandToDepth(0)
        else:
            self._clear_loading_child(parent_item)
            parent_checked = parent_item.checkState(0) == Qt.CheckState.Checked
            parent_item.setData(0, Qt.ItemDataRole.UserRole + 2, True)
            for folder in folders:
                self._add_folder_item(parent_item, folder, selected)
            if parent_checked:
                self._set_children_check_state(parent_item, Qt.CheckState.Checked)
            self._selective_loading_items.discard(parent_item.data(0, Qt.ItemDataRole.UserRole))
        self._selective_tree.blockSignals(False)
        self._update_parent_checks()

    @pyqtSlot(str)
    def _on_folder_tree_error(self, message: str):
        self._selective_loading = False
        self._selective_load_btn.setEnabled(True)
        self._selective_load_btn.setText("Carregar árvore")
        QMessageBox.critical(self, "Erro ao carregar árvore", message)

    def _add_folder_item(self, parent: QTreeWidgetItem | None, folder: dict, selected: set[str]):
        item = QTreeWidgetItem([folder.get("name", "")])
        item.setData(0, Qt.ItemDataRole.UserRole, folder.get("path", ""))
        item.setData(0, Qt.ItemDataRole.UserRole + 1, folder.get("id", ""))
        item.setData(0, Qt.ItemDataRole.UserRole + 2, False)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        path = folder.get("path", "")
        item.setCheckState(0, Qt.CheckState.Checked if path in selected else Qt.CheckState.Unchecked)

        if parent is None:
            self._selective_tree.addTopLevelItem(item)
        else:
            parent.addChild(item)

        if folder.get("child_count", 0) > 0:
            item.addChild(QTreeWidgetItem(["Carregar subpastas..."]))

        if path in selected:
            self._set_children_check_state(item, Qt.CheckState.Checked)
        else:
            self._sync_item_check_from_children(item)

    def _on_tree_item_expanded(self, item: QTreeWidgetItem):
        if item.data(0, Qt.ItemDataRole.UserRole + 2):
            return
        item_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
        path = item.data(0, Qt.ItemDataRole.UserRole)
        drive_id = self._selective_current_drive_id
        if not drive_id or not item_id or path in self._selective_loading_items:
            return

        account_id = None
        for drive in self._db.get_drives(enabled_only=False):
            if drive.id == drive_id:
                account_id = drive.account_id
                break
        if not account_id:
            return

        self._selective_loading_items.add(path)
        self._clear_loading_child(item)
        item.addChild(QTreeWidgetItem(["Carregando subpastas..."]))

        def _load():
            try:
                from onedrive_atom.api.graph import GraphClient
                folders = GraphClient(account_id).list_folders(drive_id, item_id, path)
                self._sig_folder_tree_loaded.emit(drive_id, item, folders)
            except Exception as e:
                log.exception("Could not load selective sync child folders: %s", e)
                self._selective_loading_items.discard(path)
                self._sig_folder_tree_error.emit(str(e))

        threading.Thread(target=_load, daemon=True, name="load-selective-children").start()

    def _clear_loading_child(self, item: QTreeWidgetItem):
        for i in reversed(range(item.childCount())):
            child = item.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole) is None:
                item.removeChild(child)

    def _on_selective_all_toggled(self, _checked: bool):
        self._update_selective_tree_enabled()

    def _update_selective_tree_enabled(self):
        enabled = self._selective_current_drive_id is not None and not self._selective_all_cb.isChecked()
        self._selective_tree.setEnabled(enabled)
        self._selective_check_all_btn.setEnabled(enabled)
        self._selective_clear_btn.setEnabled(enabled)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0:
            return
        self._selective_tree.blockSignals(True)
        self._set_children_check_state(item, item.checkState(0))
        self._update_parent_check(item.parent())
        self._selective_tree.blockSignals(False)

    def _set_children_check_state(self, item: QTreeWidgetItem, state: Qt.CheckState):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_children_check_state(child, state)

    def _update_parent_checks(self):
        for i in range(self._selective_tree.topLevelItemCount()):
            self._sync_item_check_from_children(self._selective_tree.topLevelItem(i))

    def _update_parent_check(self, item: QTreeWidgetItem | None):
        if item is None:
            return
        checked = 0
        partial = False
        for i in range(item.childCount()):
            state = item.child(i).checkState(0)
            if state == Qt.CheckState.PartiallyChecked:
                partial = True
            elif state == Qt.CheckState.Checked:
                checked += 1

        if partial or (0 < checked < item.childCount()):
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)
        elif item.childCount() and checked == item.childCount():
            item.setCheckState(0, Qt.CheckState.Checked)
        elif item.childCount():
            item.setCheckState(0, Qt.CheckState.Unchecked)

        self._update_parent_check(item.parent())

    def _sync_item_check_from_children(self, item: QTreeWidgetItem):
        for i in range(item.childCount()):
            self._sync_item_check_from_children(item.child(i))
        if item.childCount() == 0 or item.checkState(0) == Qt.CheckState.Checked:
            return

        states = [item.child(i).checkState(0) for i in range(item.childCount()) if item.child(i).data(0, Qt.ItemDataRole.UserRole)]
        if not states:
            return
        if all(state == Qt.CheckState.Unchecked for state in states):
            item.setCheckState(0, Qt.CheckState.Unchecked)
        elif all(state == Qt.CheckState.Checked for state in states):
            item.setCheckState(0, Qt.CheckState.Checked)
        else:
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _set_tree_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._selective_tree.blockSignals(True)
        for i in range(self._selective_tree.topLevelItemCount()):
            item = self._selective_tree.topLevelItem(i)
            item.setCheckState(0, state)
            self._set_children_check_state(item, state)
        self._selective_tree.blockSignals(False)

    def _checked_tree_paths(self) -> list[str]:
        paths = []

        def collect(item: QTreeWidgetItem, parent_checked: bool = False) -> bool:
            state = item.checkState(0)
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if not path:
                return False

            if state == Qt.CheckState.Checked and not parent_checked:
                paths.append(path)
                return True

            child_selected = False
            next_parent_checked = parent_checked or state == Qt.CheckState.Checked
            for i in range(item.childCount()):
                if collect(item.child(i), next_parent_checked):
                    child_selected = True

            if state == Qt.CheckState.PartiallyChecked and not child_selected and not parent_checked:
                paths.append(path)
                return True

            return child_selected

        for i in range(self._selective_tree.topLevelItemCount()):
            collect(self._selective_tree.topLevelItem(i))
        return paths
