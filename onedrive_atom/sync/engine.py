"""Core sync engine: bidirectional sync using the Graph delta API."""

import fnmatch
import logging
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from onedrive_atom.api.graph import GraphClient, GraphError
from onedrive_atom.config import get_config
from onedrive_atom.sync.database import (
    AccountRecord, DriveRecord, SyncedItem,
    file_hash, get_db,
)

log = logging.getLogger(__name__)


@dataclass
class SyncEvent:
    kind: str  # 'upload' | 'download' | 'delete_remote' | 'delete_local' | 'conflict' | 'error' | 'status'
    path: str = ""
    message: str = ""
    progress: float = 0.0  # 0.0–1.0


class SyncEngine:
    """
    Runs one background thread per account.
    Processes local-change events from the watcher queue and polls the delta API.
    """

    def __init__(self, account: AccountRecord, status_cb: Callable[[SyncEvent], None] | None = None):
        self.account = account
        self.status_cb = status_cb or (lambda _: None)
        self._db = get_db()
        self._cfg = get_config()
        self._client = GraphClient(account.id)
        self._local_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._sync_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sync-{self.account.email}",
            daemon=True,
        )
        self._thread.start()
        log.info("Sync engine started for %s", self.account.email)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Sync engine stopped for %s", self.account.email)

    def enqueue_local_change(self, local_path: str, event_type: str):
        """Called by the file watcher when a local change is detected."""
        self._local_queue.put((local_path, event_type))

    def sync_now(self):
        threading.Thread(target=self._sync_now, daemon=True, name=f"sync-now-{self.account.email}").start()

    def _sync_now(self):
        if self._stop.is_set():
            return
        if not self._sync_lock.acquire(blocking=False):
            log.info("Sync already running for %s", self.account.email)
            return
        try:
            if not self._stop.is_set():
                self._process_local_changes()
            if not self._stop.is_set():
                self._run_delta_sync()
        finally:
            self._sync_lock.release()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._sync_lock:
                    if self._stop.is_set():
                        break
                    self._process_local_changes()
                    if self._stop.is_set():
                        break
                    self._run_delta_sync()
            except Exception as e:
                log.exception("Sync cycle error for %s: %s", self.account.email, e)
                self._emit(SyncEvent("error", message=str(e)))

            # Wait for next cycle, but wake up immediately for local events
            self._stop.wait(timeout=self._cfg.sync_interval)

    def _process_local_changes(self):
        events: list[tuple[str, str]] = []
        while True:
            try:
                events.append(self._local_queue.get_nowait())
            except queue.Empty:
                break

        for local_path, event_type in events:
            if self._stop.is_set():
                break
            try:
                self._handle_local_event(local_path, event_type)
            except Exception as e:
                log.error("Error handling local event %s %s: %s", event_type, local_path, e)

    def _handle_local_event(self, local_path: str, event_type: str):
        path = Path(local_path)

        if self._stop.is_set():
            return

        if self._is_ignored(path):
            return

        item = self._db.get_item_by_local_path(local_path)
        drive = self._find_drive_for_path(local_path)
        if not drive:
            return

        if event_type == "deleted":
            if item:
                log.info("DELETE remote: %s", item.remote_path)
                self._emit(SyncEvent("delete_remote", path=local_path))
                try:
                    self._client.delete_item(drive.id, item.item_id)
                    self._db.delete_item(item.item_id, drive.id)
                    self._db.log_action(self.account.id, drive.id, item.item_id, "delete_remote", "ok")
                except GraphError as e:
                    log.error("Failed to delete remote %s: %s", item.remote_path, e)
            return

        if not path.exists():
            return

        if event_type in ("created", "modified"):
            remote_path = self._local_to_remote(local_path, drive)
            if remote_path is None:
                return

            if not self._db.is_path_selected(drive.id, remote_path):
                return

            self._upload_file(drive, path, remote_path, item)

    def _upload_file(self, drive: DriveRecord, local_path: Path, remote_path: str, existing: SyncedItem | None):
        if local_path.is_dir():
            return

        size = local_path.stat().st_size
        if size > self._cfg.max_file_size:
            log.warning("Skipping %s: exceeds max file size", local_path)
            return

        local_hash = file_hash(local_path)
        if existing and existing.local_hash == local_hash:
            return  # File unchanged

        log.info("UPLOAD: %s -> %s", local_path, remote_path)
        self._emit(SyncEvent("upload", path=str(local_path)))

        def _progress(done: int, total: int):
            self._emit(SyncEvent("upload", path=str(local_path), progress=done / total))

        try:
            if self._stop.is_set():
                return
            parent_remote = remote_path.strip("/").rsplit("/", 1)[0]
            if parent_remote:
                self._client.ensure_folder_by_path(drive.id, parent_remote)
            result = self._client.upload_file(drive.id, remote_path, local_path, _progress)
            if self._stop.is_set():
                return
            item_id = result.get("id", existing.item_id if existing else "")
            mtime = local_path.stat().st_mtime

            self._db.upsert_item(SyncedItem(
                item_id=item_id,
                drive_id=drive.id,
                local_path=str(local_path),
                remote_path=remote_path,
                size=size,
                local_mtime=mtime,
                remote_mtime=result.get("lastModifiedDateTime", ""),
                local_hash=local_hash,
                remote_etag=result.get("eTag", ""),
                sync_status="synced",
            ))
            self._db.log_action(self.account.id, drive.id, item_id, "upload", "ok", str(local_path))
        except GraphError as e:
            log.error("Upload failed %s: %s", local_path, e)
            self._db.log_action(self.account.id, drive.id, "", "upload", "error", str(e))

    # ── Delta sync (remote → local) ───────────────────────────────────────────

    def _run_delta_sync(self):
        drives = self._db.get_drives(self.account.id)
        for drive in drives:
            if self._stop.is_set():
                break
            try:
                self._sync_drive(drive)
            except Exception as e:
                log.error("Delta sync error for drive %s: %s", drive.name, e)

    def _sync_drive(self, drive: DriveRecord):
        delta_link = drive.delta_link or None
        if self._stop.is_set():
            return
        self._emit(SyncEvent("status", message=f"Syncing {drive.name}…"))

        selected_paths = self._db.get_selective_sync(drive.id)
        if selected_paths and not delta_link:
            self._sync_selected_paths(drive, selected_paths)
            if not self._stop.is_set():
                latest_delta = self._client.get_latest_delta_link(drive.id)
                if latest_delta:
                    self._db.update_delta_link(drive.id, latest_delta)
                self._emit(SyncEvent("status", message=f"Synced {drive.name}"))
            return

        items, new_delta = self._client.get_delta(drive.id, delta_link)
        if self._stop.is_set():
            return
        log.debug("Delta for %s: %d items", drive.name, len(items))

        for remote_item in items:
            if self._stop.is_set():
                break
            self._process_remote_item(drive, remote_item)

        if new_delta and not self._stop.is_set():
            self._db.update_delta_link(drive.id, new_delta)

        if not self._stop.is_set():
            self._emit(SyncEvent("status", message=f"Synced {drive.name}"))

    def _sync_selected_paths(self, drive: DriveRecord, selected_paths: list[str]):
        for remote_path in selected_paths:
            if self._stop.is_set():
                break
            try:
                item = self._client.get_item_by_path(drive.id, remote_path)
                self._sync_remote_subtree(drive, item, "/" + remote_path.strip("/"))
            except GraphError as e:
                log.error("Selected path sync failed %s: %s", remote_path, e)
                self._db.log_action(self.account.id, drive.id, "", "selective_sync", "error", f"{remote_path}: {e}")

    def _sync_remote_subtree(self, drive: DriveRecord, remote_item: dict, remote_path: str):
        if self._stop.is_set():
            return

        remote_item = dict(remote_item)
        remote_item.setdefault("name", remote_path.strip("/").split("/")[-1])

        if "folder" in remote_item:
            local_path = self._remote_to_local(remote_path, drive)
            Path(local_path).mkdir(parents=True, exist_ok=True)
            self._db.upsert_item(SyncedItem(
                item_id=remote_item.get("id", ""),
                drive_id=drive.id,
                local_path=local_path,
                remote_path=remote_path,
                is_folder=True,
                remote_mtime=remote_item.get("lastModifiedDateTime", ""),
                remote_etag=remote_item.get("eTag", ""),
                sync_status="synced",
            ))

            for child in self._client.list_children(drive.id, remote_item.get("id", "")):
                child_path = f"{remote_path.rstrip('/')}/{child.get('name', '')}"
                self._sync_remote_subtree(drive, child, child_path)
            return

        self._download_file(drive, remote_item, Path(self._remote_to_local(remote_path, drive)), remote_path)

    def _process_remote_item(self, drive: DriveRecord, remote_item: dict):
        item_id = remote_item.get("id", "")
        remote_path = self._remote_item_path(remote_item)

        if self._stop.is_set():
            return

        if not self._db.is_path_selected(drive.id, remote_path):
            return

        # Deleted on remote
        if remote_item.get("deleted"):
            existing = self._db.get_item_by_id(item_id, drive.id)
            if existing and os.path.exists(existing.local_path):
                log.info("DELETE local: %s", existing.local_path)
                self._emit(SyncEvent("delete_local", path=existing.local_path))
                if os.path.isdir(existing.local_path):
                    shutil.rmtree(existing.local_path, ignore_errors=True)
                else:
                    os.unlink(existing.local_path)
            if existing:
                self._db.delete_item(item_id, drive.id)
            return

        local_path = self._remote_to_local(remote_path, drive)
        if self._is_ignored(Path(local_path)):
            return

        is_folder = "folder" in remote_item
        existing = self._db.get_item_by_id(item_id, drive.id)

        if is_folder:
            Path(local_path).mkdir(parents=True, exist_ok=True)
            self._db.upsert_item(SyncedItem(
                item_id=item_id,
                drive_id=drive.id,
                local_path=local_path,
                remote_path=remote_path,
                is_folder=True,
                remote_mtime=remote_item.get("lastModifiedDateTime", ""),
                remote_etag=remote_item.get("eTag", ""),
                sync_status="synced",
            ))
            return

        # File: check if download needed
        remote_etag = remote_item.get("eTag", "")
        remote_mtime = remote_item.get("lastModifiedDateTime", "")
        size = remote_item.get("size", 0)

        if existing and existing.remote_etag == remote_etag:
            return  # Already up to date

        local_p = Path(local_path)
        conflict = False

        if existing and local_p.exists():
            current_hash = file_hash(local_p)
            if current_hash != existing.local_hash:
                # Both local and remote changed
                conflict = True

        if conflict:
            self._handle_conflict(drive, remote_item, local_p, remote_path, existing)
            return

        self._download_file(drive, remote_item, local_p, remote_path)

    def _download_file(self, drive: DriveRecord, remote_item: dict, local_path: Path, remote_path: str):
        item_id = remote_item.get("id", "")
        log.info("DOWNLOAD: %s -> %s", remote_path, local_path)
        self._emit(SyncEvent("download", path=str(local_path)))

        def _progress(done: int, total: int):
            self._emit(SyncEvent("download", path=str(local_path), progress=done / total))

        try:
            if self._stop.is_set():
                return
            self._client.download_item(drive.id, item_id, local_path, _progress)
            if self._stop.is_set():
                return
            mtime = local_path.stat().st_mtime
            local_hash = file_hash(local_path)

            self._db.upsert_item(SyncedItem(
                item_id=item_id,
                drive_id=drive.id,
                local_path=str(local_path),
                remote_path=remote_path,
                size=remote_item.get("size", 0),
                local_mtime=mtime,
                remote_mtime=remote_item.get("lastModifiedDateTime", ""),
                local_hash=local_hash,
                remote_etag=remote_item.get("eTag", ""),
                sync_status="synced",
            ))
            self._db.log_action(self.account.id, drive.id, item_id, "download", "ok", str(local_path))
        except GraphError as e:
            log.error("Download failed %s: %s", remote_path, e)
            self._db.log_action(self.account.id, drive.id, item_id, "download", "error", str(e))

    def _handle_conflict(self, drive: DriveRecord, remote_item: dict, local_path: Path, remote_path: str, existing: SyncedItem | None):
        cfg = self._cfg
        strategy = cfg.conflict_resolution

        if strategy == "newer_wins":
            remote_mtime_str = remote_item.get("lastModifiedDateTime", "")
            try:
                remote_mtime = datetime.fromisoformat(remote_mtime_str.replace("Z", "+00:00"))
                local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc)
                if local_mtime > remote_mtime:
                    self._upload_file(drive, local_path, remote_path, existing)
                    return
            except Exception:
                pass
            self._download_file(drive, remote_item, local_path, remote_path)

        elif strategy == "keep_both":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = local_path.stem
            suffix = local_path.suffix
            conflict_path = local_path.with_name(f"{stem} (conflict {ts}){suffix}")
            shutil.copy2(local_path, conflict_path)
            log.warning("CONFLICT: saved local copy to %s", conflict_path)
            self._emit(SyncEvent("conflict", path=str(local_path), message=f"Conflict copy: {conflict_path.name}"))
            self._download_file(drive, remote_item, local_path, remote_path)
        else:
            # Default: keep remote
            self._download_file(drive, remote_item, local_path, remote_path)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _emit(self, event: SyncEvent):
        try:
            self.status_cb(event)
        except Exception:
            pass

    def _is_ignored(self, path: Path) -> bool:
        name = path.name
        if not self._cfg.sync_hidden and name.startswith("."):
            return True
        for pattern in self._cfg.ignored_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def _find_drive_for_path(self, local_path: str) -> DriveRecord | None:
        drives = self._db.get_drives(self.account.id)
        for drive in drives:
            drive_local = Path(self.account.sync_dir) / _safe_name(drive.name)
            if _is_relative_to(Path(local_path), drive_local):
                return drive
        return None

    def _local_to_remote(self, local_path: str, drive: DriveRecord) -> str | None:
        drive_local = Path(self.account.sync_dir) / _safe_name(drive.name)
        if not _is_relative_to(Path(local_path), drive_local):
            return None
        rel = Path(local_path).resolve().relative_to(drive_local.resolve())
        return rel.as_posix()

    def _remote_to_local(self, remote_path: str, drive: DriveRecord) -> str:
        drive_local = Path(self.account.sync_dir) / _safe_name(drive.name)
        rel = remote_path.lstrip("/").replace("/", os.sep)
        return str(drive_local / rel)

    def _remote_item_path(self, item: dict) -> str:
        parent_ref = item.get("parentReference", {})
        parent_path = parent_ref.get("path", "/drive/root:")
        # Strip the '/drive/root:' prefix
        if ":" in parent_path:
            parent_path = parent_path.split(":", 1)[1]
        name = item.get("name", "")
        if parent_path:
            return f"{parent_path}/{name}".replace("//", "/")
        return f"/{name}"


def _safe_name(name: str) -> str:
    """Replace characters invalid in Linux file names."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class SyncManager:
    """Manages sync engines for all active accounts."""

    def __init__(self, status_cb: Callable[[str, SyncEvent], None] | None = None):
        self._engines: dict[str, SyncEngine] = {}
        self._status_cb = status_cb
        self._db = get_db()

    def start_all(self):
        accounts = self._db.get_accounts()
        for acc in accounts:
            self.start_account(acc)

    def start_account(self, account: AccountRecord):
        if account.id in self._engines:
            return

        def _cb(event: SyncEvent):
            if self._status_cb:
                self._status_cb(account.id, event)

        engine = SyncEngine(account, status_cb=_cb)
        self._engines[account.id] = engine
        engine.start()

    def stop_all(self):
        for engine in list(self._engines.values()):
            engine.stop()
        self._engines.clear()

    def stop_account(self, account_id: str):
        engine = self._engines.pop(account_id, None)
        if engine:
            engine.stop()

    def enqueue_local_change(self, local_path: str, event_type: str):
        for engine in self._engines.values():
            engine.enqueue_local_change(local_path, event_type)

    def trigger_sync_now(self, account_id: str | None = None):
        targets = ([self._engines[account_id]] if account_id and account_id in self._engines
                   else list(self._engines.values()))
        for engine in targets:
            engine.sync_now()
