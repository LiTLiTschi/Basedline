#!/usr/bin/env python3
"""
mik_watcher.py — Watch directory for new audio files and trigger MIK analysis.

Watches a configurable directory for new/moved audio files, injects them into
MIK's database, and restarts MIK to analyze them.

Usage:
    python mik_watcher.py
    python mik_watcher.py --config path/to/config.json
"""

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("ERROR: watchdog required. Run: pip install watchdog")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from mik_queue_insert import queue_file, load_config, DEFAULT_CONFIG_PATH
from mik_launcher import restart_mik

import argparse

log = logging.getLogger("mik-watcher")


def file_is_stable(path: str, interval: float = 2.0) -> bool:
    """Wait until file size stops changing (handles SMB copies of large files)."""
    try:
        size1 = os.path.getsize(path)
        time.sleep(interval)
        size2 = os.path.getsize(path)
        return size1 == size2 and size2 > 0
    except OSError:
        return False


class MIKEventHandler(FileSystemEventHandler):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.debounce_seconds = config.get("debounce_seconds", 30.0)
        audio_exts = config.get("audio_extensions",
                                [".mp3", ".flac", ".m4a", ".wav",
                                 ".aiff", ".aif"])
        self.audio_extensions = set(audio_exts)
        self._pending: list = []
        self._timer = None
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    def _handle(self, path: str):
        if Path(path).suffix.lower() not in self.audio_extensions:
            return
        log.info(f"File detected: {path}")
        with self._lock:
            self._pending.append(path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._flush)
            self._timer.start()

    def _flush(self):
        with self._lock:
            files = list(self._pending)
            self._pending.clear()
            self._timer = None
        if not files:
            return

        log.info(f"Processing batch: {len(files)} file(s)")
        inserted = 0
        for fp in files:
            try:
                if not file_is_stable(fp):
                    log.warning(f"File not stable (still copying?): {fp}")
                    # Re-queue for next batch
                    with self._lock:
                        self._pending.append(fp)
                    continue
                if queue_file(fp, self.config, dry_run=False, no_backup=True):
                    inserted += 1
            except Exception as e:
                log.error(f"Failed to queue {fp}: {e}")

        if inserted > 0:
            log.info(f"Queued {inserted} track(s). Restarting MIK...")
            try:
                restart_mik(self.config)
            except Exception as e:
                log.error(f"Failed to restart MIK: {e}")
        else:
            log.info("No new tracks inserted (all duplicates or errors).")

        # If files were re-queued due to instability, schedule another flush
        with self._lock:
            if self._pending and not self._timer:
                self._timer = threading.Timer(self.debounce_seconds,
                                              self._flush)
                self._timer.start()


def setup_logging(config: dict):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Always log to stderr
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)
    # Optionally log to file
    log_file = config.get("log_file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def run_watcher(config: dict):
    watch_dir = config["watch_dir"]
    log.info(f"Watching: {watch_dir} "
             f"(debounce: {config.get('debounce_seconds', 30)}s)")

    handler = MIKEventHandler(config)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=True)
    observer.start()

    try:
        while True:
            # Check if observer is still alive (network share may drop)
            if not observer.is_alive():
                log.warning("Observer died (network share dropped?). "
                            "Reconnecting in 30s...")
                observer.stop()
                observer.join()
                time.sleep(30)
                observer = Observer()
                observer.schedule(handler, watch_dir, recursive=True)
                observer.start()
                log.info("Observer reconnected.")
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        observer.stop()
        observer.join()


def main():
    ap = argparse.ArgumentParser(
        description="Watch for new audio files and trigger MIK analysis.")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)
    setup_logging(config)
    run_watcher(config)


if __name__ == "__main__":
    main()
