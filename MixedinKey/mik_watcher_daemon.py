#!/usr/bin/env python3
"""
mik_watcher_daemon.py

Watches music directory for new audio files and queues them into MIK's DB.
Uses debounce to batch rapid file additions before triggering a MIK restart.

See: MixedinKey/MIK_AUTOMATION_PLAN.md Section 6

Requires:
    pip install watchdog mutagen
    data/automation_config.json

Run as systemd service: see MIK_AUTOMATION_PLAN.md Section 9
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("ERROR: watchdog required. Run: pip install watchdog")
    sys.exit(1)

import argparse
sys.path.insert(0, str(Path(__file__).parent))
from mik_queue_insert import queue_file, load_config, DEFAULT_CONFIG_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mik-watcher")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif"}


class MIKEventHandler(FileSystemEventHandler):
    def __init__(self, config: dict, debounce_seconds: float = 30.0):
        super().__init__()
        self.config = config
        self.debounce_seconds = debounce_seconds
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
        if Path(path).suffix.lower() not in AUDIO_EXTENSIONS:
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
                time.sleep(1)  # Ensure file fully written
                if queue_file(fp, self.config, dry_run=False, backup=True):
                    inserted += 1
            except Exception as e:
                log.error(f"Failed to queue {fp}: {e}")
        if inserted > 0:
            log.info(f"Queued {inserted} track(s). Signaling MIK restart...")
            flag = Path(self.config.get("restart_flag_path", "/tmp/mik_restart_requested"))
            flag.touch()
        else:
            log.info("No new tracks (all duplicates or errors).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)

    music_dir = config["music_dir"]
    debounce = config.get("debounce_seconds", 30.0)
    log.info(f"Watching: {music_dir} (debounce: {debounce}s)")

    handler = MIKEventHandler(config, debounce_seconds=debounce)
    observer = Observer()
    observer.schedule(handler, music_dir, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
