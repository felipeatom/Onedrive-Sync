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
    kind: str  # 'upload' | 'download' | 'delete_remote' | 'delete_local' | 'conflict' | 'error' | 'status' | 'syncing' | 'synced'
    path: str = ""
    message: str = ""
    progress: float = 0.0  # 0.0–1.0


def _is_path_in_selection(remote_path: str, selected: list[str]) -> bool:
    """Return True if remote_path falls within any of the selected paths."""
    if not selected:
        return True
    rp = "/" + remote_path.strip("/")
    for sel in selected:
        sel_norm = "/" + sel.strip("/")
        if rp == sel_norm or rp.startswith(sel_norm + "/"):
            return True
    return False


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
        self._local_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sync_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sync-{self.account.email}",
            daemon=True,
        )
        self._thread.start()
        log.info("Sync engine started for %s", self.account.email)

    def stop(self):
        self._stop.set()
        self._wake.set()  # unblock any ongoing sleep
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Sync engine stopped for %s", self.account.email)

    def enqueue_local_change(self, local_path: str, event_type: str, dest_path: str | None = None):
        """Called by the file watcher when a local change is detected."""
        self._local_queue.put((local_path, event_type, dest_path))
        self._wake.set()  # Process this change promptly instead of waiting for next poll interval

    def sync_now(self):
        """Wake the sync thread to run a cycle immediately."""
        self._wake.set()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _run(self):
        # Detect files created while the app was offline (only for drives that
        # have been synced before — fresh installs are handled by _sync_drive).
        try:
            for drive in self._db.get_drives(self.account.id):
                if drive.delta_link and not self._stop.is_set():
                    self._scan_for_untracked_files(drive)
        except Exception as e:
            log.error("Startup local scan failed: %s", e)

        error_count = 0
        while not self._stop.is_set():
            try:
                with self._sync_lock:
                    if not self._stop.is_set():
                        self._process_local_changes()
                    if not self._stop.is_set():
                        self._run_delta_sync()
                error_count = 0
            except Exception as e:
                error_count += 1
                log.exception("Sync cycle error for %s: %s", self.account.email, e)
                self._emit(SyncEvent("error", message=str(e)))

            if self._stop.is_set():
                break

            # Exponential backoff on consecutive errors (max 1 hour wait).
            if error_count:
                wait = min(self._cfg.sync_interval * (2 ** min(error_count - 1, 4)), 3600)
            else:
                wait = self._cfg.sync_interval

            # Only clear the wake flag if the local queue is empty.
            # _scan_for_untracked_files or enqueue_local_change may have set it
            # while the sync cycle was running — don't discard those signals.
            if self._local_queue.empty():
                self._wake.clear()
            self._wake.wait(timeout=wait)

    def _process_local_changes(self):
        events: list[tuple[str, str, str | None]] = []
        while True:
            try:
                events.append(self._local_queue.get_nowait())
            except queue.Empty:
                break

        for local_path, event_type, dest_path in events:
            if self._stop.is_set():
                break
            try:
                self._handle_local_event(local_path, event_type, dest_path)
            except Exception as e:
                log.error("Error handling local event %s %s: %s", event_type, local_path, e)

    def _handle_local_event(self, local_path: str, event_type: str, dest_path: str | None = None):
        path = Path(local_path)

        if self._stop.is_set():
            return

        if event_type == "moved" and dest_path:
            self._handle_move(local_path, dest_path)
            return

        if self._is_ignored(path):
            return

        item = self._db.get_item_by_local_path(local_path)
        drive = self._find_drive_for_path(local_path)
        if not drive:
            return

        if event_type == "deleted":
            if item:
                # Item tracked in DB — delete from OneDrive then clean up DB.
                log.info("DELETE remote: %s", item.remote_path)
                self._emit(SyncEvent("delete_remote", path=local_path))
                try:
                    self._client.delete_item(drive.id, item.item_id)
                except GraphError as e:
                    if e.status == 404:
                        log.debug("Already removed from OneDrive: %s", item.remote_path)
                    else:
                        log.error("Failed to delete remote %s: %s", item.remote_path, e)
                # Cascade-delete folder contents from DB so subsequent per-file delete
                # events don't make redundant API calls for already-deleted items.
                if item.is_folder:
                    self._db.delete_items_by_local_prefix(local_path)
                else:
                    self._db.delete_item(item.item_id, drive.id)
                self._db.log_action(self.account.id, drive.id, item.item_id, "delete_remote", "ok", item.remote_path)
            else:
                # No DB record — may be an untracked folder (e.g., created via
                # ensure_folder_by_path during upload) whose children ARE tracked.
                # Also handles file events that arrive after the parent folder was
                # already cascade-deleted from the DB.
                remote_path = self._local_to_remote(local_path, drive)
                if remote_path:
                    try:
                        folder_item = self._client.get_item_by_path(drive.id, remote_path)
                        self._client.delete_item(drive.id, folder_item.get("id", ""))
                        self._emit(SyncEvent("delete_remote", path=local_path))
                        log.info("DELETE remote (untracked folder): %s", remote_path)
                    except GraphError as e:
                        if e.status != 404:
                            log.error("Failed to delete untracked remote %s: %s", remote_path, e)
                    # Always clean up any orphaned DB children
                    self._db.delete_items_by_local_prefix(local_path)
            return

        if not path.exists():
            return

        if event_type in ("created", "modified"):
            remote_path = self._local_to_remote(local_path, drive)
            if remote_path is None:
                return

            # Only respect the selective sync filter for files that have already
            # been synced before (item exists in DB). Locally-created content that
            # has never been uploaded should always go to OneDrive, regardless of
            # which paths were selected for download.
            if item is not None and not self._db.is_path_selected(drive.id, remote_path):
                return

            if path.is_dir():
                self._ensure_remote_dir(drive, path, remote_path)
            else:
                self._upload_file(drive, path, remote_path, item)

    def _handle_move(self, src_path: str, dest_path: str):
        """Propagate a local rename/move to the remote using the Graph move API."""
        dest_p = Path(dest_path)

        if self._is_ignored(dest_p):
            # Destination is ignored — treat the source as deleted.
            self._handle_local_event(src_path, "deleted")
            return

        src_item = self._db.get_item_by_local_path(src_path)
        drive = self._find_drive_for_path(dest_path) or self._find_drive_for_path(src_path)

        if not src_item or not drive:
            # Item not tracked or drive unknown — fall back to delete + upload.
            self._handle_local_event(src_path, "deleted")
            self._handle_local_event(dest_path, "created")
            return

        new_remote_path = self._local_to_remote(dest_path, drive)
        if not new_remote_path:
            self._handle_local_event(src_path, "deleted")
            return

        if not self._db.is_path_selected(drive.id, new_remote_path):
            # Moved outside selective sync — delete remote, no upload.
            try:
                self._client.delete_item(drive.id, src_item.item_id)
                self._db.delete_item(src_item.item_id, drive.id)
                self._db.log_action(self.account.id, drive.id, src_item.item_id,
                                    "delete_remote", "ok", f"moved outside selective sync: {src_path}")
            except GraphError as e:
                log.error("Failed to delete remote after out-of-scope move: %s", e)
            return

        new_name = dest_p.name
        new_parent_remote = new_remote_path.strip("/").rsplit("/", 1)[0] if "/" in new_remote_path.strip("/") else ""

        try:
            if new_parent_remote:
                parent_item = self._client.get_item_by_path(drive.id, new_parent_remote)
                new_parent_id = parent_item.get("id", "")
            else:
                root = self._client.get_root(drive.id)
                new_parent_id = root.get("id", "")

            if not new_parent_id:
                raise GraphError(404, "Parent folder ID not found")

            self._client.move_item(drive.id, src_item.item_id, new_parent_id, new_name)

            self._db.upsert_item(SyncedItem(
                item_id=src_item.item_id,
                drive_id=drive.id,
                local_path=dest_path,
                remote_path=new_remote_path,
                size=src_item.size,
                local_mtime=dest_p.stat().st_mtime if dest_p.exists() else src_item.local_mtime,
                remote_mtime=src_item.remote_mtime,
                local_hash=src_item.local_hash,
                remote_etag=src_item.remote_etag,
                sync_status="synced",
            ))
            self._db.log_action(self.account.id, drive.id, src_item.item_id, "move", "ok",
                                f"{src_path} → {dest_path}")
            self._emit(SyncEvent("upload", path=dest_path,
                                 message=f"Movido: {Path(src_path).name} → {dest_p.name}"))

        except GraphError as e:
            log.error("Remote move failed for %s, falling back to delete+upload: %s", src_path, e)
            self._handle_local_event(src_path, "deleted")
            self._handle_local_event(dest_path, "created")

    def _ensure_remote_dir(self, drive: DriveRecord, local_path: Path, remote_path: str):
        """Create a directory on OneDrive and record it in the DB if not already tracked."""
        if self._db.get_item_by_local_path(str(local_path)):
            return  # Already tracked — nothing to do
        try:
            result = self._client.ensure_folder_by_path(drive.id, remote_path)
            if result:
                self._db.upsert_item(SyncedItem(
                    item_id=result.get("id", ""),
                    drive_id=drive.id,
                    local_path=str(local_path),
                    remote_path=remote_path,
                    is_folder=True,
                    remote_mtime=result.get("lastModifiedDateTime", ""),
                    remote_etag=result.get("eTag", ""),
                    sync_status="synced",
                ))
                self._db.log_action(self.account.id, drive.id, result.get("id", ""), "mkdir", "ok", remote_path)
                self._emit(SyncEvent("upload", path=str(local_path), message=f"Pasta criada: {local_path.name}"))
                log.info("MKDIR remote: %s", remote_path)
        except GraphError as e:
            log.error("Failed to create remote dir %s: %s", remote_path, e)

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
        self._emit(SyncEvent("syncing", message=f"Sincronizando {drive.name}…"))

        # Cache selected paths once per drive sync cycle to avoid repeated DB queries.
        selected_paths = self._db.get_selective_sync(drive.id)

        if selected_paths and not delta_link:
            self._sync_selected_paths(drive, selected_paths)
            if self._stop.is_set():
                return
            # Upload any local-only files that were created while this initial scan ran.
            self._scan_for_untracked_files(drive)
            if not self._stop.is_set():
                latest_delta = self._client.get_latest_delta_link(drive.id)
                if latest_delta:
                    self._db.update_delta_link(drive.id, latest_delta)
                self._emit(SyncEvent("synced", message=f"Sincronizado — {drive.name}"))
            return

        try:
            items, new_delta = self._client.get_delta(drive.id, delta_link)
        except GraphError as e:
            if e.status in (410, 400, 404) and delta_link:
                # Delta token expired or invalid — start a fresh full sync.
                log.warning("Delta link expired for drive %s (HTTP %d), resetting.", drive.name, e.status)
                self._db.reset_delta_link(drive.id)
                items, new_delta = self._client.get_delta(drive.id, None)
            else:
                raise

        if self._stop.is_set():
            return
        log.debug("Delta for %s: %d items", drive.name, len(items))

        for remote_item in items:
            if self._stop.is_set():
                break
            self._process_remote_item(drive, remote_item, selected_paths)

        if new_delta and not self._stop.is_set():
            self._db.update_delta_link(drive.id, new_delta)

        if not self._stop.is_set():
            self._emit(SyncEvent("synced", message=f"Sincronizado — {drive.name}"))

    def _sync_selected_paths(self, drive: DriveRecord, selected_paths: list[str]):
        for remote_path in selected_paths:
            if self._stop.is_set():
                break
            # Upload any files the user added while we were scanning previous paths.
            self._process_local_changes()
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
                if self._stop.is_set():
                    break
                # Drain local uploads between children so the user doesn't wait for the
                # entire tree walk before their newly added files get uploaded.
                if not self._local_queue.empty():
                    self._process_local_changes()
                child_path = f"{remote_path.rstrip('/')}/{child.get('name', '')}"
                self._sync_remote_subtree(drive, child, child_path)
            return

        # File — check content before downloading to avoid overwriting an identical local copy.
        local_p = Path(self._remote_to_local(remote_path, drive))
        if local_p.exists():
            remote_sha1 = (remote_item.get("file") or {}).get("hashes", {}).get("sha1Hash", "").lower()
            if remote_sha1:
                local_hash = file_hash(local_p)
                if local_hash == remote_sha1:
                    existing = self._db.get_item_by_id(remote_item.get("id", ""), drive.id)
                    self._db.upsert_item(SyncedItem(
                        item_id=remote_item.get("id", ""),
                        drive_id=drive.id,
                        local_path=str(local_p),
                        remote_path=remote_path,
                        size=remote_item.get("size", 0),
                        local_mtime=local_p.stat().st_mtime,
                        remote_mtime=remote_item.get("lastModifiedDateTime", ""),
                        local_hash=local_hash,
                        remote_etag=remote_item.get("eTag", ""),
                        sync_status="synced",
                    ))
                    log.debug("SKIP initial download (content match): %s", local_p)
                    return
        self._download_file(drive, remote_item, local_p, remote_path)

    def _process_remote_item(self, drive: DriveRecord, remote_item: dict, selected_paths: list[str] | None = None):
        item_id = remote_item.get("id", "")
        remote_path = self._remote_item_path(remote_item)

        if self._stop.is_set():
            return

        # Use the pre-fetched selection list when available to avoid per-item DB queries.
        cached = selected_paths if selected_paths is not None else self._db.get_selective_sync(drive.id)
        if not _is_path_in_selection(remote_path, cached):
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

        # File: decide if download is needed
        remote_etag = remote_item.get("eTag", "")

        if existing and existing.remote_etag == remote_etag:
            return  # eTag unchanged → already up to date

        local_p = Path(local_path)
        local_exists = local_p.exists()
        # Compute local hash once — used for both the content-match check and conflict detection.
        local_hash = file_hash(local_p) if local_exists else ""

        # Content-hash shortcut: compare SHA1 from the Graph API with the local file.
        # When only metadata (permissions, label, etc.) changed on OneDrive the eTag
        # updates but file bytes are identical — no download needed.
        if local_hash:
            remote_sha1 = (remote_item.get("file") or {}).get("hashes", {}).get("sha1Hash", "").lower()
            if remote_sha1 and local_hash == remote_sha1:
                self._db.upsert_item(SyncedItem(
                    item_id=item_id,
                    drive_id=drive.id,
                    local_path=local_path,
                    remote_path=remote_path,
                    size=remote_item.get("size", existing.size if existing else 0),
                    local_mtime=local_p.stat().st_mtime,
                    remote_mtime=remote_item.get("lastModifiedDateTime", ""),
                    local_hash=local_hash,
                    remote_etag=remote_etag,
                    sync_status="synced",
                ))
                log.debug("SKIP download (content match): %s", local_path)
                return

        # Conflict: eTag changed AND local file also changed since last sync.
        conflict = existing and local_exists and local_hash != existing.local_hash

        if conflict:
            self._handle_conflict(drive, remote_item, local_p, remote_path, existing)
            return

        self._download_file(drive, remote_item, local_p, remote_path)

    def _scan_for_untracked_files(self, drive: DriveRecord):
        """Walk the local sync tree and queue upload for files not tracked in the DB.

        Covers two scenarios:
        - Files created while the app was not running (missed by the watcher).
        - Files created during a long initial sync before the watcher enqueued them.
        """
        tracked = {item.local_path for item in self._db.get_items_by_drive(drive.id)}
        drive_local = Path(self.account.sync_dir) / _safe_name(drive.name)

        # Always scan the full drive directory so that locally-created folders outside
        # the selective-sync selection are also detected and uploaded.  The upload check
        # in _handle_local_event already allows new (untracked) files to bypass the
        # selective-sync filter, so scanning wider here is safe.
        roots = [drive_local]

        count = 0
        for root in roots:
            if not root.exists():
                continue
            for local_path in root.rglob("*"):
                if self._stop.is_set():
                    return
                if self._is_ignored(local_path):
                    continue
                path_str = str(local_path)
                if path_str in tracked:
                    continue
                if local_path.is_dir():
                    # Queue empty directories — non-empty ones will be created
                    # automatically via ensure_folder_by_path when their files upload.
                    try:
                        if not any(True for _ in local_path.iterdir()):
                            self._local_queue.put((path_str, "created", None))
                            count += 1
                    except OSError:
                        pass
                else:
                    self._local_queue.put((path_str, "created", None))
                    count += 1

        if count:
            log.info("Local scan queued %d untracked file(s) for upload (%s)", count, drive.name)
            self._wake.set()

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

    def enqueue_local_change(self, local_path: str, event_type: str, dest_path: str | None = None):
        for engine in self._engines.values():
            engine.enqueue_local_change(local_path, event_type, dest_path)

    def trigger_sync_now(self, account_id: str | None = None):
        targets = ([self._engines[account_id]] if account_id and account_id in self._engines
                   else list(self._engines.values()))
        for engine in targets:
            engine.sync_now()
