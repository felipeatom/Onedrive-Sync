"""File system watcher using watchdog. Feeds local changes into sync engines."""

import logging
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[str, str, str | None], None]):
        self._callback = callback

    def on_created(self, event: FileSystemEvent):
        self._callback(event.src_path, "created", None)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._callback(event.src_path, "modified", None)

    def on_deleted(self, event: FileSystemEvent):
        self._callback(event.src_path, "deleted", None)

    def on_moved(self, event: FileSystemEvent):
        self._callback(event.src_path, "moved", event.dest_path)


class FileWatcher:
    """
    Watches one or more directories for changes and calls callback(path, event_type, dest_path).
    Event types: 'created', 'modified', 'deleted', 'moved'.
    dest_path is only set for 'moved' events.
    """

    def __init__(self, callback: Callable[[str, str, str | None], None]):
        self._callback = callback
        self._observer = Observer()
        self._watched: dict[str, object] = {}  # path -> watch handle
        self._lock = threading.Lock()
        self._running = False

    def watch(self, directory: str | Path):
        directory = str(directory)
        with self._lock:
            if directory in self._watched:
                return
            Path(directory).mkdir(parents=True, exist_ok=True)
            handler = _Handler(self._callback)
            watch = self._observer.schedule(handler, directory, recursive=True)
            self._watched[directory] = watch
            log.info("Watching: %s", directory)

    def unwatch(self, directory: str | Path):
        directory = str(directory)
        with self._lock:
            watch = self._watched.pop(directory, None)
            if watch:
                self._observer.unschedule(watch)
                log.info("Stopped watching: %s", directory)

    def start(self):
        if self._running:
            return
        self._observer.start()
        self._running = True

    def stop(self):
        if not self._running:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._observer = Observer()
        self._watched.clear()
        self._running = False

    def update_watched_dirs(self, directories: list[str | Path]):
        new = {str(d) for d in directories}
        current = set(self._watched.keys())

        for d in current - new:
            self.unwatch(d)
        for d in new - current:
            Path(d).mkdir(parents=True, exist_ok=True)
            self.watch(d)
