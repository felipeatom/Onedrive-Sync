"""SQLite database for tracking sync state across all accounts and drives."""

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path
from typing import Optional

from onedrive_atom.config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "sync_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    name        TEXT NOT NULL,
    tenant_id   TEXT,
    account_type TEXT DEFAULT 'personal',
    sync_dir    TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drives (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    drive_type  TEXT,
    web_url     TEXT,
    quota_used  INTEGER,
    quota_total INTEGER,
    delta_link  TEXT,
    last_sync   TIMESTAMP,
    enabled     INTEGER DEFAULT 1,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS synced_items (
    item_id         TEXT NOT NULL,
    drive_id        TEXT NOT NULL,
    local_path      TEXT NOT NULL,
    remote_path     TEXT NOT NULL,
    size            INTEGER DEFAULT 0,
    local_mtime     REAL,
    remote_mtime    TEXT,
    local_hash      TEXT,
    remote_etag     TEXT,
    is_folder       INTEGER DEFAULT 0,
    sync_status     TEXT DEFAULT 'synced',
    last_synced     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_id, drive_id),
    FOREIGN KEY (drive_id) REFERENCES drives(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_local_path ON synced_items(local_path);
CREATE INDEX IF NOT EXISTS idx_items_drive_id   ON synced_items(drive_id);
CREATE INDEX IF NOT EXISTS idx_items_status     ON synced_items(sync_status);

CREATE TABLE IF NOT EXISTS selective_sync (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id    TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    UNIQUE(drive_id, remote_path),
    FOREIGN KEY (drive_id) REFERENCES drives(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    account_id  TEXT,
    drive_id    TEXT,
    item_id     TEXT,
    action      TEXT,
    status      TEXT,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_ts ON sync_log(ts);
"""


@dataclass
class AccountRecord:
    id: str
    email: str
    name: str
    tenant_id: str = ""
    account_type: str = "personal"
    sync_dir: str = ""
    enabled: bool = True


@dataclass
class DriveRecord:
    id: str
    account_id: str
    name: str
    drive_type: str = "personal"
    web_url: str = ""
    quota_used: int = 0
    quota_total: int = 0
    delta_link: str = ""
    last_sync: str = ""
    enabled: bool = True


@dataclass
class SyncedItem:
    item_id: str
    drive_id: str
    local_path: str
    remote_path: str
    size: int = 0
    local_mtime: float = 0.0
    remote_mtime: str = ""
    local_hash: str = ""
    remote_etag: str = ""
    is_folder: bool = False
    sync_status: str = "synced"


def _to(cls, row):
    """Build a dataclass from a sqlite3.Row, ignoring extra DB-only columns."""
    known = {f.name for f in dc_fields(cls)}
    d = {k: v for k, v in dict(row).items() if k in known}
    return cls(**d)


class Database:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._init()

    def _init(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Accounts ─────────────────────────────────────────────────────────────

    def upsert_account(self, acc: AccountRecord):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO accounts (id, email, name, tenant_id, account_type, sync_dir, enabled)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     email=excluded.email, name=excluded.name,
                     tenant_id=excluded.tenant_id, account_type=excluded.account_type,
                     sync_dir=excluded.sync_dir, enabled=excluded.enabled""",
                (acc.id, acc.email, acc.name, acc.tenant_id, acc.account_type, acc.sync_dir, int(acc.enabled)),
            )

    def get_accounts(self, enabled_only: bool = True) -> list[AccountRecord]:
        with self._conn() as conn:
            q = "SELECT * FROM accounts"
            if enabled_only:
                q += " WHERE enabled=1"
            rows = conn.execute(q).fetchall()
        return [_to(AccountRecord, r) for r in rows]

    def get_account(self, account_id: str) -> AccountRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return _to(AccountRecord, row) if row else None

    def delete_account(self, account_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def set_account_enabled(self, account_id: str, enabled: bool):
        with self._conn() as conn:
            conn.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(enabled), account_id))

    # ── Drives ────────────────────────────────────────────────────────────────

    def upsert_drive(self, drive: DriveRecord):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO drives (id, account_id, name, drive_type, web_url,
                     quota_used, quota_total, delta_link, last_sync, enabled)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, drive_type=excluded.drive_type,
                     web_url=excluded.web_url, quota_used=excluded.quota_used,
                     quota_total=excluded.quota_total, enabled=excluded.enabled""",
                (
                    drive.id, drive.account_id, drive.name, drive.drive_type,
                    drive.web_url, drive.quota_used, drive.quota_total,
                    drive.delta_link, drive.last_sync, int(drive.enabled),
                ),
            )

    def get_drives(self, account_id: str | None = None, enabled_only: bool = True) -> list[DriveRecord]:
        with self._conn() as conn:
            q = "SELECT * FROM drives WHERE 1=1"
            params: list = []
            if account_id:
                q += " AND account_id=?"
                params.append(account_id)
            if enabled_only:
                q += " AND enabled=1"
            rows = conn.execute(q, params).fetchall()
        return [_to(DriveRecord, r) for r in rows]

    def get_drive(self, drive_id: str) -> DriveRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM drives WHERE id=?", (drive_id,)).fetchone()
        return _to(DriveRecord, row) if row else None

    def update_delta_link(self, drive_id: str, delta_link: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE drives SET delta_link=?, last_sync=CURRENT_TIMESTAMP WHERE id=?",
                (delta_link, drive_id),
            )

    def reset_delta_link(self, drive_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE drives SET delta_link='', last_sync=NULL WHERE id=?",
                (drive_id,),
            )

    def set_drive_enabled(self, drive_id: str, enabled: bool):
        with self._conn() as conn:
            conn.execute("UPDATE drives SET enabled=? WHERE id=?", (int(enabled), drive_id))

    # ── Synced items ──────────────────────────────────────────────────────────

    def upsert_item(self, item: SyncedItem):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO synced_items
                     (item_id, drive_id, local_path, remote_path, size,
                      local_mtime, remote_mtime, local_hash, remote_etag,
                      is_folder, sync_status, last_synced)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(item_id, drive_id) DO UPDATE SET
                     local_path=excluded.local_path, remote_path=excluded.remote_path,
                     size=excluded.size, local_mtime=excluded.local_mtime,
                     remote_mtime=excluded.remote_mtime, local_hash=excluded.local_hash,
                     remote_etag=excluded.remote_etag, is_folder=excluded.is_folder,
                     sync_status=excluded.sync_status, last_synced=CURRENT_TIMESTAMP""",
                (
                    item.item_id, item.drive_id, item.local_path, item.remote_path,
                    item.size, item.local_mtime, item.remote_mtime,
                    item.local_hash, item.remote_etag, int(item.is_folder), item.sync_status,
                ),
            )

    def _row_to_item(self, row) -> SyncedItem:
        item = _to(SyncedItem, row)
        item.is_folder = bool(item.is_folder)
        return item

    def get_item_by_id(self, item_id: str, drive_id: str) -> SyncedItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM synced_items WHERE item_id=? AND drive_id=?",
                (item_id, drive_id),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def get_item_by_local_path(self, local_path: str) -> SyncedItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM synced_items WHERE local_path=?", (local_path,)
            ).fetchone()
        return self._row_to_item(row) if row else None

    def get_items_by_drive(self, drive_id: str) -> list[SyncedItem]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM synced_items WHERE drive_id=?", (drive_id,)
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def delete_item(self, item_id: str, drive_id: str):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM synced_items WHERE item_id=? AND drive_id=?",
                (item_id, drive_id),
            )

    def update_item_status(self, item_id: str, drive_id: str, status: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE synced_items SET sync_status=? WHERE item_id=? AND drive_id=?",
                (status, item_id, drive_id),
            )

    # ── Selective sync ────────────────────────────────────────────────────────

    def set_selective_sync(self, drive_id: str, remote_paths: list[str]):
        with self._conn() as conn:
            old_rows = conn.execute(
                "SELECT remote_path FROM selective_sync WHERE drive_id=? AND enabled=1 ORDER BY remote_path",
                (drive_id,),
            ).fetchall()
            old_paths = [r[0] for r in old_rows]
            new_paths = sorted(remote_paths)
            conn.execute("DELETE FROM selective_sync WHERE drive_id=?", (drive_id,))
            for p in remote_paths:
                conn.execute(
                    "INSERT OR IGNORE INTO selective_sync (drive_id, remote_path, enabled) VALUES (?,?,1)",
                    (drive_id, p),
                )
            if old_paths != new_paths:
                conn.execute("UPDATE drives SET delta_link='', last_sync=NULL WHERE id=?", (drive_id,))

    def get_selective_sync(self, drive_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT remote_path FROM selective_sync WHERE drive_id=? AND enabled=1",
                (drive_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def is_path_selected(self, drive_id: str, remote_path: str) -> bool:
        selected = self.get_selective_sync(drive_id)
        if not selected:
            return True  # No restrictions: sync everything
        remote_path = "/" + remote_path.strip("/")
        for sel in selected:
            sel = "/" + sel.strip("/")
            if remote_path == sel or remote_path.startswith(sel + "/"):
                return True
        return False

    # ── Sync log ──────────────────────────────────────────────────────────────

    def log_action(self, account_id: str, drive_id: str, item_id: str, action: str, status: str, message: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sync_log (account_id, drive_id, item_id, action, status, message) VALUES (?,?,?,?,?,?)",
                (account_id, drive_id, item_id, action, status, message),
            )

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_old_logs(self, keep_days: int = 30):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM sync_log WHERE ts < datetime('now', ?)",
                (f"-{keep_days} days",),
            )


def file_hash(path: Path, block_size: int = 65536) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(block_size):
            h.update(chunk)
    return h.hexdigest()


_db_instance: Database | None = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
