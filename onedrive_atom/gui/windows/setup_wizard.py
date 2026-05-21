"""First-run setup wizard — guides Azure App Registration or uses built-in client."""

import re
import webbrowser

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from onedrive_atom.config import get_config

PORTAL_URL = "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade"
M365_DEV_URL = "https://developer.microsoft.com/microsoft-365/dev-program"

# Default public client used by the open-source onedrive-linux project.
# Works for most personal accounts; may be blocked by some org tenants.
DEFAULT_CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"


def _h(text: str, size: int = 14) -> QLabel:
    lbl = QLabel(text)
    f = QFont(); f.setPointSize(size); f.setBold(True)
    lbl.setFont(f)
    return lbl


def _p(html: str) -> QLabel:
    lbl = QLabel(html)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setOpenExternalLinks(False)
    lbl.setStyleSheet("color:#333; line-height:1.5;")
    return lbl


def _step(n: int, total: int) -> QLabel:
    lbl = QLabel(f"Passo {n} de {total}")
    lbl.setStyleSheet("color:#888; font-size:11px;")
    return lbl


def _btn_link(label: str, url: str) -> QPushButton:
    btn = QPushButton(f"  {label}  ↗")
    btn.setMinimumHeight(36)
    btn.setStyleSheet(
        "QPushButton{background:#0078d4;color:white;border-radius:5px;"
        "font-size:12px;font-weight:bold;padding:2px 14px;}"
        "QPushButton:hover{background:#005fa3;}"
    )
    btn.clicked.connect(lambda: webbrowser.open(url))
    return btn


def _is_guid(s: str) -> bool:
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s
    ))


class SetupWizard(QDialog):
    setup_complete = pyqtSignal(str)   # emits client_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Configuração — Onedrive-Sync")
        self.setMinimumSize(540, 480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._use_own_app = False
        self._pages: list[QWidget] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet("background:#0078d4;")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(20, 0, 20, 0)
        hl.addWidget(QLabel("Onedrive-Sync — Configuração inicial",
                            styleSheet="color:white;font-size:14px;font-weight:bold;"))
        root.addWidget(hdr)

        self._stack = QStackedWidget()
        self._stack.setContentsMargins(28, 20, 28, 16)

        self._pages = [
            self._page_choose(),       # 0
            self._page_use_default(),  # 1  — path A: use default client ID
            self._page_need_dir(),     # 2  — path B: no Azure directory
            self._page_create_app(),   # 3  — path B: create app
            self._page_permissions(),  # 4  — path B: add permissions
            self._page_client_id(),    # 5  — path B: paste client ID
            self._page_done(),         # 6
        ]
        for p in self._pages:
            self._stack.addWidget(p)
        root.addWidget(self._stack, 1)

        # Nav
        nav = QWidget()
        nav.setStyleSheet("background:#f3f3f3;border-top:1px solid #ddd;")
        nl = QHBoxLayout(nav); nl.setContentsMargins(20, 10, 20, 10)

        self._btn_back = QPushButton("← Voltar")
        self._btn_back.setEnabled(False)
        self._btn_back.clicked.connect(self._back)
        nl.addWidget(self._btn_back)
        nl.addStretch()

        self._btn_next = QPushButton("Próximo →")
        self._btn_next.setDefault(True)
        self._btn_next.setStyleSheet(
            "QPushButton{background:#0078d4;color:white;border-radius:5px;"
            "padding:6px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#005fa3;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
        )
        self._btn_next.clicked.connect(self._next)
        nl.addWidget(self._btn_next)
        root.addWidget(nav)

        self._history: list[int] = [0]

    # ── Pages ─────────────────────────────────────────────────────────────────

    def _page_choose(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("Como deseja autenticar?"))
        layout.addSpacing(12)
        layout.addWidget(_p(
            "O Onedrive-Sync usa a API da Microsoft para sincronizar seus arquivos. "
            "Escolha como deseja configurar o acesso:"
        ))
        layout.addSpacing(16)

        self._radio_default = QRadioButton(
            "Usar o app padrão (mais fácil, funciona para a maioria das contas pessoais)"
        )
        self._radio_default.setChecked(True)
        self._radio_own = QRadioButton(
            "Usar meu próprio app Azure (recomendado para contas corporativas)"
        )
        grp = QButtonGroup(w)
        grp.addButton(self._radio_default)
        grp.addButton(self._radio_own)

        layout.addWidget(self._radio_default)
        layout.addSpacing(4)
        layout.addWidget(self._radio_own)
        layout.addStretch()
        return w

    def _page_use_default(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("App padrão configurado"))
        layout.addSpacing(12)
        layout.addWidget(_p(
            "O app padrão <b>OneDrive Client for Linux</b> já está configurado.<br><br>"
            "Clique em <b>Próximo</b> para fazer login com sua conta Microsoft."
        ))
        layout.addSpacing(12)
        layout.addWidget(_p(
            "<b>Nota:</b> Se o login falhar com 'something went wrong', provavelmente "
            "sua conta corporativa precisa de um app próprio. Volte e escolha a segunda opção."
        ))
        layout.addStretch()
        return w

    def _page_need_dir(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("Você precisa de um diretório Azure"))
        layout.addSpacing(12)
        layout.addWidget(_p(
            "Para registrar um aplicativo, você precisa de um <b>diretório Azure AD</b>. "
            "Se você recebeu <i>\"The ability to create applications outside of a directory "
            "has been deprecated\"</i>, use uma das opções gratuitas abaixo:"
        ))
        layout.addSpacing(12)

        # Option 1: M365 Dev Program
        box1 = QWidget()
        box1.setStyleSheet("background:#f0f4ff;border:1px solid #b0c4ff;border-radius:6px;")
        bl1 = QVBoxLayout(box1); bl1.setContentsMargins(14, 10, 14, 10)
        bl1.addWidget(QLabel("<b>Opção 1 — M365 Developer Program (gratuito, sem cartão)</b>",
                             textFormat=Qt.TextFormat.RichText))
        bl1.addWidget(QLabel("Cria um tenant Azure AD completo com 25 licenças M365 por 90 dias renováveis.",
                             wordWrap=True))
        bl1.addWidget(_btn_link("Acessar M365 Dev Program", M365_DEV_URL))
        layout.addWidget(box1)
        layout.addSpacing(8)

        # Option 2: Azure free account
        box2 = QWidget()
        box2.setStyleSheet("background:#f5f5f5;border:1px solid #ddd;border-radius:6px;")
        bl2 = QVBoxLayout(box2); bl2.setContentsMargins(14, 10, 14, 10)
        bl2.addWidget(QLabel("<b>Opção 2 — Conta Azure gratuita</b>",
                             textFormat=Qt.TextFormat.RichText))
        bl2.addWidget(QLabel("$200 em créditos + serviços sempre gratuitos. Exige cartão de crédito.",
                             wordWrap=True))
        bl2.addWidget(_btn_link("Criar conta Azure gratuita", "https://azure.microsoft.com/free/"))
        layout.addWidget(box2)

        layout.addSpacing(8)
        layout.addWidget(_p("Após criar o diretório, clique em <b>Próximo</b> para continuar."))
        layout.addStretch()
        return w

    def _page_create_app(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("Registrar o aplicativo"))
        layout.addSpacing(10)
        layout.addWidget(_p(
            "1. Clique em <b>Abrir portal Azure</b><br>"
            "2. <i>Nome:</i> <b>Onedrive-Sync</b><br>"
            "3. <i>Tipos de conta:</i> <b>Contas em qualquer diretório + contas pessoais Microsoft</b><br>"
            "4. <i>URI de redirecionamento:</i> deixe em branco<br>"
            "5. Clique em <b>Registrar</b>"
        ))
        layout.addSpacing(10)
        layout.addWidget(_btn_link("Abrir portal Azure", PORTAL_URL))
        layout.addStretch()
        return w

    def _page_permissions(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("Adicionar permissões"))
        layout.addSpacing(10)
        layout.addWidget(_p(
            "Na página do app registrado:<br>"
            "1. Menu lateral → <b>Permissões de API</b><br>"
            "2. <b>Adicionar uma permissão</b> → Microsoft Graph → Permissões delegadas<br>"
            "3. Adicione: <b>Files.ReadWrite.All</b> e <b>User.Read</b><br>"
            "4. Clique em <b>Adicionar permissões</b><br>"
            "5. (Conta corporativa) <b>Conceder consentimento do administrador</b>"
        ))
        layout.addStretch()
        return w

    def _page_client_id(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(_h("Cole o Client ID"))
        layout.addSpacing(10)
        layout.addWidget(_p(
            "Na página do app, copie o valor de <b>ID do aplicativo (cliente)</b>:<br>"
            "<code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>"
        ))
        layout.addSpacing(10)

        self._client_id_edit = QLineEdit()
        self._client_id_edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self._client_id_edit.setMinimumHeight(36)
        self._client_id_edit.setStyleSheet("font-family:monospace;font-size:13px;padding:4px 8px;")
        self._client_id_edit.textChanged.connect(
            lambda t: self._id_err.setText("" if not t or _is_guid(t) else "Formato inválido")
        )
        layout.addWidget(self._client_id_edit)

        self._id_err = QLabel("")
        self._id_err.setStyleSheet("color:#d13438;font-size:11px;")
        layout.addWidget(self._id_err)
        layout.addStretch()
        return w

    def _page_done(self) -> QWidget:
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addStretch()
        done = QLabel("✓"); done.setStyleSheet("color:#00b050;font-size:56px;")
        done.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(done)
        layout.addWidget(_h("Pronto!", 14))
        layout.addSpacing(6)
        layout.addWidget(_p("Configuração concluída. Clique em <b>Concluir</b> para fazer login."))
        layout.addStretch()
        return w

    # ── Navigation ────────────────────────────────────────────────────────────

    def _next(self):
        idx = self._stack.currentIndex()

        if idx == 0:
            self._use_own_app = self._radio_own.isChecked()
            next_idx = 2 if self._use_own_app else 1  # skip "need dir" for default path
            # Actually: path A → page 1 → page 6; path B → page 2 → 3 → 4 → 5 → 6
            next_idx = 1 if not self._use_own_app else 2
            self._goto(next_idx)
            return

        if idx == 1:   # default app page → done
            get_config().set("client_id", DEFAULT_CLIENT_ID)
            self.setup_complete.emit(DEFAULT_CLIENT_ID)
            self._goto(6)
            return

        if idx == 5:   # client ID page
            cid = self._client_id_edit.text().strip()
            if not _is_guid(cid):
                self._id_err.setText("ID inválido. Formato: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
                return
            get_config().set("client_id", cid)
            self.setup_complete.emit(cid)
            self._goto(6)
            return

        if idx == 6:
            self.accept()
            return

        # Default: go to next page in sequence
        next_map = {2: 3, 3: 4, 4: 5}
        self._goto(next_map.get(idx, idx + 1))

    def _back(self):
        if len(self._history) > 1:
            self._history.pop()
            self._stack.setCurrentIndex(self._history[-1])
            self._btn_back.setEnabled(len(self._history) > 1)
            self._btn_next.setText("Próximo →")

    def _goto(self, idx: int):
        self._history.append(idx)
        self._stack.setCurrentIndex(idx)
        self._btn_back.setEnabled(len(self._history) > 1)
        is_last = idx == 6
        self._btn_next.setText("Concluir" if is_last else "Próximo →")


def should_show_wizard() -> bool:
    """Show wizard on first launch (config has no valid client_id yet)."""
    from onedrive_atom.config import DATA_DIR
    # Show wizard only once: mark with a sentinel file after first completion
    return not (DATA_DIR / ".setup_done").exists()


def mark_setup_done():
    from onedrive_atom.config import DATA_DIR
    (DATA_DIR / ".setup_done").touch()
