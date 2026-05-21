import json
import logging
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "onedrive-sync"
DATA_DIR = Path.home() / ".local" / "share" / "onedrive-sync"
LOG_DIR = DATA_DIR / "logs"
DEFAULT_SYNC_BASE = Path.home() / "OneDrive"

# Public client registered for open-source OneDrive clients on Linux.
# Users can override this in config with their own Azure App Registration.
DEFAULT_CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"

# Sites.ReadWrite.All requires admin consent in many tenants and is optional
# for basic OneDrive sync. It can be added later when the user enables SharePoint.
GRAPH_SCOPES = [
    "Files.ReadWrite.All",
    "User.Read",
]

SHAREPOINT_SCOPES = [
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
    "User.Read",
]

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/auth/callback"

DEFAULT_CONFIG: dict[str, Any] = {
    "client_id": DEFAULT_CLIENT_ID,
    "authority": "https://login.microsoftonline.com/common",
    "scopes": GRAPH_SCOPES,  # offline_access is reserved and injected automatically by MSAL
    "sync_interval_seconds": 30,
    "sync_base_dir": str(DEFAULT_SYNC_BASE),
    "ignored_patterns": [
        "*.tmp", "~$*", ".~lock.*", "*.swp", "*.swo", "*.part",
        "desktop.ini", "thumbs.db", ".DS_Store", "*.crdownload",
    ],
    "sync_hidden_files": False,
    "max_file_size_mb": 250,
    "upload_chunk_size_mb": 10,
    "log_level": "INFO",
    "start_minimized": True,
    "notifications_enabled": True,
    "conflict_resolution": "newer_wins",  # newer_wins | keep_both | ask
    "theme": "auto",
}


class Config:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._path = CONFIG_DIR / "config.json"
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        merged = DEFAULT_CONFIG.copy()
        merged.update(self._data)
        # Strip MSAL-reserved scopes that must never be passed explicitly
        _reserved = {"offline_access", "openid", "profile"}
        merged["scopes"] = [s for s in merged.get("scopes", GRAPH_SCOPES) if s not in _reserved]
        self._data = merged

    def save(self):
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value
        self.save()

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any):
        self.set(key, value)

    @property
    def sync_base_dir(self) -> Path:
        return Path(self._data["sync_base_dir"])

    @property
    def client_id(self) -> str:
        return self._data["client_id"]

    @property
    def authority(self) -> str:
        return self._data["authority"]

    @property
    def scopes(self) -> list[str]:
        return self._data["scopes"]

    @property
    def sync_interval(self) -> int:
        return self._data["sync_interval_seconds"]

    @property
    def ignored_patterns(self) -> list[str]:
        return self._data["ignored_patterns"]

    @property
    def sync_hidden(self) -> bool:
        return self._data["sync_hidden_files"]

    @property
    def conflict_resolution(self) -> str:
        return self._data["conflict_resolution"]

    @property
    def upload_chunk_size(self) -> int:
        return self._data["upload_chunk_size_mb"] * 1024 * 1024

    @property
    def max_file_size(self) -> int:
        return self._data["max_file_size_mb"] * 1024 * 1024


_instance: Config | None = None
_instance_lock = threading.Lock()


def get_config() -> Config:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = Config()
    return _instance


def setup_logging():
    cfg = get_config()
    level = getattr(logging, cfg.get("log_level", "DEBUG"), logging.DEBUG)
    log_file = LOG_DIR / "onedrive-sync.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(ch)
