"""Account management window: add, remove, enable/disable accounts."""

import logging
import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from onedrive_atom.auth.oauth import device_code_login, remove_account_tokens
from onedrive_atom.config import get_config
from onedrive_atom.gui.icons import icon_account
from onedrive_atom.sync.database import AccountRecord, get_db

log = logging.getLogger(__name__)


class AccountItem(QListWidgetItem):
    def __init__(self, account: AccountRecord):
        super().__init__()
        self.account = account
        self._refresh()

    def _refresh(self):
        self.setText(f"{self.account.name}\n{self.account.email}")
        self.setIcon(icon_account(32))
        state = "Ativo" if self.account.enabled else "Pausado"
        self.setToolTip(f"{self.account.email} — {state}")


class DeviceCodeDialog(QDialog):
    """
    Shows the device code with a countdown timer.
    When the code expires the UI switches to an expired state with a Retry button.
    """

    retry_requested = pyqtSignal()

    def __init__(self, user_code: str, verification_url: str, expires_in: int,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Autenticação Microsoft")
        self.setMinimumWidth(440)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._remaining = expires_in
        self._build_ui(user_code, verification_url)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _build_ui(self, user_code: str, verification_url: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Title
        title = QLabel("Entre com sua conta Microsoft")
        font = title.font()
        font.setPointSize(13)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Steps
        steps = QLabel(
            "1. Clique em <b>Abrir</b> para acessar o site da Microsoft\n"
            "2. Digite o código abaixo quando solicitado\n"
            "3. Faça login com sua conta — esta janela fecha sozinha"
        )
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setStyleSheet("color:#444;")
        steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(steps)

        # Open button
        btn_open = QPushButton("  Abrir microsoft.com/devicelogin  ↗")
        btn_open.setMinimumHeight(38)
        btn_open.setStyleSheet(
            "QPushButton{background:#0078d4;color:white;border-radius:6px;"
            "font-size:13px;font-weight:bold;padding:4px 20px;}"
            "QPushButton:hover{background:#005fa3;}"
        )
        btn_open.clicked.connect(lambda: webbrowser.open(verification_url))
        layout.addWidget(btn_open)

        # Code box
        code_frame = QFrame()
        code_frame.setStyleSheet(
            "QFrame{background:#fff8e1;border:2px solid #ffcc00;border-radius:8px;}"
        )
        code_layout = QVBoxLayout(code_frame)
        code_layout.setContentsMargins(16, 12, 16, 12)
        code_layout.setSpacing(4)

        hint = QLabel("Código de acesso")
        hint.setStyleSheet("color:#888;font-size:11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_layout.addWidget(hint)

        self._code_lbl = QLabel(user_code)
        font2 = self._code_lbl.font()
        font2.setPointSize(30)
        font2.setBold(True)
        font2.setFamily("monospace")
        self._code_lbl.setFont(font2)
        self._code_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code_layout.addWidget(self._code_lbl)

        copy_btn = QPushButton("Copiar código")
        copy_btn.setStyleSheet(
            "QPushButton{color:#0078d4;border:none;font-size:12px;}"
            "QPushButton:hover{text-decoration:underline;}"
        )
        copy_btn.clicked.connect(lambda: (
            QApplication.clipboard().setText(user_code),
            copy_btn.setText("✓ Copiado!"),
        ))
        code_layout.addWidget(copy_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(code_frame)

        # Countdown / status row
        status_row = QHBoxLayout()

        self._wait_lbl = QLabel("Aguardando login…")
        self._wait_lbl.setStyleSheet("color:#888;font-size:11px;")
        status_row.addWidget(self._wait_lbl)

        status_row.addStretch()

        self._countdown_lbl = QLabel()
        self._countdown_lbl.setStyleSheet("color:#888;font-size:11px;font-family:monospace;")
        status_row.addWidget(self._countdown_lbl)

        layout.addLayout(status_row)
        self._update_countdown()

        # Bottom row
        bottom = QHBoxLayout()

        self._retry_btn = QPushButton("Gerar novo código")
        self._retry_btn.setVisible(False)
        self._retry_btn.setStyleSheet(
            "QPushButton{background:#0078d4;color:white;border-radius:4px;padding:5px 12px;}"
            "QPushButton:hover{background:#005fa3;}"
        )
        self._retry_btn.clicked.connect(self._on_retry)
        bottom.addWidget(self._retry_btn)

        bottom.addStretch()

        btn_cancel = QPushButton("Cancelar")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)

        layout.addLayout(bottom)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self._show_expired()
        else:
            self._update_countdown()

    def _update_countdown(self):
        m = self._remaining // 60
        s = self._remaining % 60
        self._countdown_lbl.setText(f"Expira em {m}:{s:02d}")
        if self._remaining <= 60:
            self._countdown_lbl.setStyleSheet("color:#d13438;font-size:11px;font-weight:bold;font-family:monospace;")

    def _show_expired(self):
        self._wait_lbl.setText("⚠ Código expirado")
        self._wait_lbl.setStyleSheet("color:#d13438;font-size:11px;font-weight:bold;")
        self._countdown_lbl.clear()
        self._code_lbl.setStyleSheet("color:#aaa;text-decoration:line-through;")
        self._retry_btn.setVisible(True)

    def _on_retry(self):
        self._timer.stop()
        self.retry_requested.emit()
        self.accept()

    def mark_success(self):
        self._timer.stop()
        self.accept()

    def mark_error(self, msg: str):
        self._timer.stop()
        if msg == "__expired__":
            self._show_expired()
        else:
            self.reject()


class AccountWindow(QDialog):
    account_added   = pyqtSignal(str)
    account_removed = pyqtSignal(str)
    account_toggled = pyqtSignal(str, bool)

    # (user_code, verification_url, expires_in)
    _sig_login_message = pyqtSignal(str, str, int)
    _sig_login_success = pyqtSignal(dict)
    _sig_login_error   = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Gerenciar contas")
        self.setMinimumSize(480, 400)
        self._db = get_db()
        self._code_dialog: DeviceCodeDialog | None = None

        self._sig_login_message.connect(self._show_device_code_dialog)
        self._sig_login_success.connect(self._finish_add_account)
        self._sig_login_error.connect(self._finish_add_error)

        self._build_ui()
        self._load_accounts()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel("Contas do OneDrive")
        font = header.font()
        font.setPointSize(14)
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header)

        sub = QLabel("Gerencie suas contas pessoais, corporativas e SharePoint.")
        sub.setStyleSheet("color:#666;")
        layout.addWidget(sub)
        layout.addSpacing(8)

        box = QGroupBox("Contas conectadas")
        box_layout = QVBoxLayout(box)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setIconSize(QSize(32, 32))
        self._list.currentItemChanged.connect(self._on_selection_changed)
        box_layout.addWidget(self._list)

        btn_row = QHBoxLayout()

        self._btn_add = QPushButton("Adicionar conta")
        self._btn_add.setDefault(True)
        self._btn_add.clicked.connect(self._add_account)

        self._btn_remove = QPushButton("Remover")
        self._btn_remove.setEnabled(False)
        self._btn_remove.clicked.connect(self._remove_account)

        self._btn_toggle = QPushButton("Pausar")
        self._btn_toggle.setEnabled(False)
        self._btn_toggle.clicked.connect(self._toggle_account)

        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_toggle)
        btn_row.addWidget(self._btn_remove)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)

        layout.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

    def _load_accounts(self):
        self._list.clear()
        for acc in self._db.get_accounts(enabled_only=False):
            item = AccountItem(acc)
            self._list.addItem(item)

    def _on_selection_changed(self, current: QListWidgetItem | None, _):
        has_sel = current is not None
        self._btn_remove.setEnabled(has_sel)
        self._btn_toggle.setEnabled(has_sel)
        if has_sel and isinstance(current, AccountItem):
            self._btn_toggle.setText("Retomar" if not current.account.enabled else "Pausar")

    def _add_account(self):
        from onedrive_atom.gui.windows.setup_wizard import (
            SetupWizard, mark_setup_done, should_show_wizard,
        )
        if should_show_wizard():
            wizard = SetupWizard(parent=self)
            wizard.setup_complete.connect(lambda _: mark_setup_done())
            if wizard.exec() != SetupWizard.DialogCode.Accepted:
                return  # user cancelled setup

        self._btn_add.setEnabled(False)
        self._btn_add.setText("Gerando código…")

        device_code_login(
            on_message=self._sig_login_message.emit,
            on_success=self._sig_login_success.emit,
            on_error=self._sig_login_error.emit,
        )

    @pyqtSlot(str, str, int)
    def _show_device_code_dialog(self, user_code: str, verification_url: str, expires_in: int):
        self._btn_add.setText("Aguardando login…")

        dlg = DeviceCodeDialog(user_code, verification_url, expires_in, parent=self)
        dlg.retry_requested.connect(self._add_account)
        self._code_dialog = dlg
        dlg.show()

    @pyqtSlot(dict)
    def _finish_add_account(self, info: dict):
        if self._code_dialog:
            self._code_dialog.mark_success()
            self._code_dialog = None

        cfg = get_config()
        # Use full email (sanitized) to ensure uniqueness across different tenants.
        safe_email = info["email"].replace("@", "_").replace(".", "_")
        sync_dir = str(Path(cfg.sync_base_dir) / safe_email)

        acc = AccountRecord(
            id=info["account_id"],
            email=info["email"],
            name=info["name"],
            tenant_id=info.get("tenant_id", ""),
            sync_dir=sync_dir,
            enabled=True,
        )
        self._db.upsert_account(acc)
        self._load_accounts()
        self.account_added.emit(info["account_id"])

        self._btn_add.setEnabled(True)
        self._btn_add.setText("Adicionar conta")
        QMessageBox.information(
            self, "Conta adicionada",
            f"Conta {info['email']} adicionada com sucesso!\n\nA sincronização iniciará em breve."
        )

    @pyqtSlot(str)
    def _finish_add_error(self, msg: str):
        if self._code_dialog:
            self._code_dialog.mark_error(msg)
            if msg != "__expired__":
                self._code_dialog = None

        self._btn_add.setEnabled(True)
        self._btn_add.setText("Adicionar conta")

        if msg != "__expired__":
            QMessageBox.critical(
                self, "Erro de autenticação",
                f"Falha ao adicionar conta:\n\n{msg}"
            )

    def _remove_account(self):
        item = self._list.currentItem()
        if not isinstance(item, AccountItem):
            return
        acc = item.account
        reply = QMessageBox.question(
            self, "Remover conta",
            f"Deseja remover a conta {acc.email}?\n\nOs arquivos locais não serão deletados.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._db.delete_account(acc.id)
        remove_account_tokens(acc.id)
        self._load_accounts()
        self.account_removed.emit(acc.id)

    def _toggle_account(self):
        item = self._list.currentItem()
        if not isinstance(item, AccountItem):
            return
        acc = item.account
        new_state = not acc.enabled
        self._db.set_account_enabled(acc.id, new_state)
        acc.enabled = new_state
        item._refresh()
        self._btn_toggle.setText("Retomar" if not new_state else "Pausar")
        self.account_toggled.emit(acc.id, new_state)
