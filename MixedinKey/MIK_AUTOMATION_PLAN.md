# MIK Automation Plan

> **Status:** Draft — written 2026-03-08 by automation agent while Liu slept.  
> **Scope:** Fully hands-free Mixed In Key 11 analysis on Raspberry Pi (Wine), triggered by new files appearing on disk. No GUI interaction required at any point.

---

## Table of Contents

1. [Core Mechanism](#1-core-mechanism)
2. [Architecture Overview](#2-architecture-overview)
3. [Path Translation: Linux ↔ Wine](#3-path-translation-linux--wine)
4. [FilePathHash: Algorithm Discovery](#4-filepathhash-algorithm-discovery)
5. [Component: mik_queue_insert.py](#5-component-mik_queue_insertpy)
6. [Component: mik_watcher_daemon.py](#6-component-mik_watcher_daemonpy)
7. [Component: mik_process_manager.py](#7-component-mik_process_managerpy)
8. [Configuration: config.json](#8-configuration-configjson)
9. [Systemd Service Files](#9-systemd-service-files)
10. [Testing Procedure](#10-testing-procedure)
11. [Batch Backfill: Queuing an Entire Directory](#11-batch-backfill-queuing-an-entire-directory)
12. [Known Limitations and Risks](#12-known-limitations-and-risks)
13. [Open Questions for Liu](#13-open-questions-for-liu)

---

## 1. Core Mechanism

Mixed In Key 11 reads `MIKStore.db` on startup and queues **all `Song` rows where `IsAnalyzed = 0`** for cloud analysis. This is the entire automation trigger:

```
IsAnalyzed = 0  →  MIK queues track for analysis on next startup
IsAnalyzed = 1  →  MIK skips the track (already done)
```

This means we never need to touch MIK's GUI. The full loop is:

```
[new file on disk]
      ↓
[mik_queue_insert.py: INSERT Song row with IsAnalyzed=0]
      ↓
[MIK process restarted]
      ↓
[MIK reads DB, sees IsAnalyzed=0, calls cloud analysis API]
      ↓
[MIK writes Key/BPM/Energy back to Song row + audio file tags]
      ↓
[IsAnalyzed = 1, Comment = "02A - 155 - 7"]
```

**Evidence base:**
- `Song.IsAnalyzed` column confirmed in DB schema (README, LiTLiTschi/Basedline)
- All analyzed tracks in a 123k library have `IsAnalyzed = 1`
- `LastAnalyzedUtc` is NULL on unanalyzed rows
- MIK confirmed to be cloud-only analysis (no local processing)

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Raspberry Pi                             │
│                                                                 │
│  ┌──────────────────────────────────┐                          │
│  │  mik_watcher_daemon.py           │                          │
│  │  (systemd service: mik-watcher)  │                          │
│  │                                  │                          │
│  │  watchdog / inotify watching     │                          │
│  │  MUSIC_DIR for new .mp3/.flac/   │                          │
│  │  .m4a/.wav/.aiff files           │                          │
│  └──────────────┬───────────────────┘                          │
│                 │ new file event                               │
│                 ▼                                               │
│  ┌──────────────────────────────────┐                          │
│  │  mik_queue_insert.py             │                          │
│  │                                  │                          │
│  │  1. Read audio metadata (mutagen)│                          │
│  │  2. Translate Linux→Wine path    │                          │
│  │  3. Compute FilePathHash         │                          │
│  │  4. Check: already in DB?        │                          │
│  │  5. INSERT into Song             │                          │
│  │  6. INSERT into                  │                          │
│  │     SongCollectionMembership     │                          │
│  │  7. Signal mik_process_manager   │                          │
│  └──────────────┬───────────────────┘                          │
│                 │                                               │
│                 ▼                                               │
│  ┌──────────────────────────────────┐                          │
│  │  mik_process_manager.py          │                          │
│  │  (systemd service: mik-process)  │                          │
│  │                                  │                          │
│  │  Debounced: waits N seconds for  │                          │
│  │  more files before restart       │                          │
│  │  1. SIGTERM to MIK Wine process  │                          │
│  │  2. Wait for wineserver to quit  │                          │
│  │  3. Start Xvfb if not running    │                          │
│  │  4. wine MixedInKey.exe (headless│                          │
│  │  5. Monitor until queue empty    │                          │
│  └──────────────┬───────────────────┘                          │
│                 │                                               │
│  ┌──────────────▼───────────────────┐                          │
│  │  Wine + MIK 11.exe               │                          │
│  │  (DISPLAY=:99 via Xvfb)          │                          │
│  │                                  │                          │
│  │  MIKStore.db (local Wine prefix) │                          │
│  │  ~/.wine/drive_c/users/Liu/      │                          │
│  │  AppData/Local/Mixed In Key/     │                          │
│  │  Mixed In Key/11.0/MIKStore.db   │                          │
│  └──────────────────────────────────┘                          │
│                                                                 │
│  Music SSD: /mnt/music/  (also Wine drive H:\)                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Path Translation: Linux ↔ Wine

MIK stores **Windows-style paths** in the `Song.File` column (e.g., `H:\music\track.mp3`).  
The Raspi sees the same files at a Linux path (e.g., `/mnt/music/track.mp3`).

Wine maps drives to directories in `~/.wine/dosdevices/`:
- `~/.wine/dosdevices/h:` → symlink to `/mnt/music` (or wherever the SMB share is mounted)

If the drive mapping doesn't exist yet, create it:
```bash
mkdir -p ~/.wine/dosdevices
ln -s /mnt/music ~/.wine/dosdevices/h:
```

Path translation is a two-step normalization:

```python
def linux_to_wine_path(linux_path: str, config: dict) -> str:
    """
    Convert a Linux path to the Wine Windows path that MIK will store.
    config example:
        {"path_map": {"/mnt/music": "H:\\music"}}
    """
    for linux_prefix, wine_prefix in config["path_map"].items():
        if linux_path.startswith(linux_prefix):
            relative = linux_path[len(linux_prefix):]
            wine_path = wine_prefix + relative.replace("/", "\\")
            return wine_path
    raise ValueError(f"No path mapping found for: {linux_path}")
```

The config `path_map` must be set correctly by the user (see [Section 8](#8-configuration-configjson)).

---

## 4. FilePathHash: Algorithm Discovery

The `Song.FilePathHash` column is indexed and used for deduplication. The algorithm is **not documented** by MIK. The README confirms it is a hex string but does not give its length.

### Verification Script

Run this **once** against your existing DB to identify the algorithm before first use:

```python
#!/usr/bin/env python3
"""
mik_identify_hash.py
Run once to identify the FilePathHash algorithm used by MIK.
Usage: python mik_identify_hash.py /path/to/MIKStore.db
"""
import sqlite3, hashlib, sys
from urllib.parse import unquote

CANDIDATES = [
    ("md5",    lambda s: hashlib.md5(s).hexdigest()),
    ("sha1",   lambda s: hashlib.sha1(s).hexdigest()),
    ("sha256", lambda s: hashlib.sha256(s).hexdigest()),
    ("sha512", lambda s: hashlib.sha512(s).hexdigest()),
]

ENCODINGS = ["utf-8", "utf-16-le", "utf-16-be"]
CASES = ["asis", "lower", "upper"]

def normalize(path: str) -> str:
    # Strip file:// prefix, decode percent-encoding
    if path.lower().startswith("file:///"):
        path = path[8:]
    return unquote(path)

def try_all(path: str, known_hash: str):
    norm = normalize(path)
    variants = [
        norm,
        norm.lower(),
        norm.upper(),
        norm.replace("/", "\\"),
        norm.replace("/", "\\").lower(),
        norm.replace("/", "\\").upper(),
    ]
    for variant in variants:
        for enc in ENCODINGS:
            raw = variant.encode(enc, errors="replace")
            for name, fn in CANDIDATES:
                result = fn(raw)
                if result.lower() == known_hash.lower():
                    return name, enc, variant[:60]
    return None

def main():
    db_path = sys.argv[1]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT File, FilePathHash FROM Song WHERE FilePathHash IS NOT NULL LIMIT 20"
    ).fetchall()
    conn.close()

    for row in rows:
        result = try_all(row["File"], row["FilePathHash"])
        if result:
            algo, enc, variant = result
            print(f"MATCH FOUND: {algo.upper()} / {enc}")
            print(f"  Path variant: {variant}")
            print(f"  Known hash:   {row['FilePathHash']}")
            return
    print("No match found. FilePathHash may use a custom algorithm.")
    print("Sample path:", rows[0]["File"] if rows else "(no rows)")
    print("Sample hash:", rows[0]["FilePathHash"] if rows else "(no rows)")
    print(f"Sample hash length: {len(rows[0]['FilePathHash']) if rows else 0} chars")

if __name__ == "__main__":
    main()
```

### Best Guess (before running verification)

MIK 11 is a .NET 6+ application. The most common approach in modern .NET for a deterministic, non-security hex hash used for deduplication is **SHA256 of UTF-8 encoded path**. The path is likely stored in its original Windows backslash form.

Default implementation used in `mik_queue_insert.py` until verified:

```python
import hashlib

def compute_file_path_hash(wine_path: str) -> str:
    """Best-guess: SHA256 of UTF-8 encoded Windows path."""
    return hashlib.sha256(wine_path.encode("utf-8")).hexdigest()
```

**If `mik_identify_hash.py` reveals a different algorithm, update the function above accordingly.**  
The config file supports a `hash_algo` key to override this at runtime without code changes.

---

## 5. Component: mik_queue_insert.py

This is the core injection script. It takes one or more audio file paths, builds the `Song` row, and inserts it into MIK's DB.

```python
#!/usr/bin/env python3
"""
mik_queue_insert.py

Injects one or more audio files into MIK's SQLite DB as unanalyzed tracks.
MIK will pick them up and analyze on next startup.

Usage:
    python mik_queue_insert.py /mnt/music/track.mp3
    python mik_queue_insert.py /mnt/music/*.flac
    python mik_queue_insert.py --batch /path/to/filelist.txt
    python mik_queue_insert.py --dry-run /mnt/music/track.mp3

Requires:
    pip install mutagen
    config.json in same directory (see MIK_AUTOMATION_PLAN.md Section 8)
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    print("ERROR: mutagen is required. Run: pip install mutagen")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "data" / "automation_config.json"

def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            f"Create it from template: data/automation_config.example.json"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------

def linux_to_wine_path(linux_path: str, config: dict) -> str:
    """
    Translate a Linux absolute path to the Windows path MIK uses.
    Uses config["path_map"] which is a dict of {linux_prefix: wine_prefix}.
    """
    for linux_prefix, wine_prefix in config["path_map"].items():
        if linux_path.startswith(linux_prefix):
            relative = linux_path[len(linux_prefix):]
            wine_path = wine_prefix.rstrip("\\") + "\\" + relative.lstrip("/").replace("/", "\\")
            return wine_path
    raise ValueError(
        f"No path mapping for: {linux_path}\n"
        f"Add it to 'path_map' in config.json"
    )


# ---------------------------------------------------------------------------
# FilePathHash
# ---------------------------------------------------------------------------

def compute_file_path_hash(wine_path: str, algo: str = "sha256") -> str:
    """
    Compute FilePathHash as MIK would.
    Default: SHA256 of UTF-8 encoded Windows path.
    Run mik_identify_hash.py to verify the correct algorithm.
    Override via config["hash_algo"].
    """
    data = wine_path.encode("utf-8")
    if algo == "sha256":
        return hashlib.sha256(data).hexdigest()
    elif algo == "md5":
        return hashlib.md5(data).hexdigest()
    elif algo == "sha1":
        return hashlib.sha1(data).hexdigest()
    elif algo == "sha256_lower":
        return hashlib.sha256(wine_path.lower().encode("utf-8")).hexdigest()
    elif algo == "md5_lower":
        return hashlib.md5(wine_path.lower().encode("utf-8")).hexdigest()
    else:
        raise ValueError(f"Unknown hash_algo: {algo}")


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def read_audio_metadata(linux_path: str) -> dict:
    """Extract metadata from audio file using mutagen."""
    meta = {
        "artist": "",
        "title": "",
        "album": "",
        "genre": "",
        "year": 0,
        "label": "",
        "remixer": "",
        "composer": "",
        "grouping": "",
        "bpm": None,
        "bitrate": 0,
        "sample_rate": 44100,
        "filesize": os.path.getsize(linux_path),
    }
    try:
        audio = MutagenFile(linux_path, easy=True)
        if audio is None:
            return meta
        if hasattr(audio, "info"):
            info = audio.info
            meta["bitrate"] = int(getattr(info, "bitrate", 0) / 1000)
            meta["sample_rate"] = getattr(info, "sample_rate", 44100)
        def get(key):
            val = audio.get(key)
            if val:
                return str(val[0]).strip()
            return ""
        meta["artist"] = get("artist")
        meta["title"] = get("title")
        meta["album"] = get("album")
        meta["genre"] = get("genre")
        meta["label"] = get("organization") or get("label")
        meta["remixer"] = get("remixer") or get("tp1")
        meta["composer"] = get("composer")
        meta["grouping"] = get("grouping")
        bpm_str = get("bpm")
        if bpm_str:
            try:
                meta["bpm"] = float(bpm_str)
            except ValueError:
                pass
        year_str = get("date")
        if year_str:
            try:
                meta["year"] = int(year_str[:4])
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f"  [WARN] Could not read metadata from {linux_path}: {e}")
    return meta


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safer for concurrent access
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def song_exists(conn: sqlite3.Connection, file_path_hash: str) -> Optional[str]:
    """Return existing Song.Id if a song with this FilePathHash already exists."""
    row = conn.execute(
        "SELECT Id FROM Song WHERE FilePathHash = ?", (file_path_hash,)
    ).fetchone()
    return row["Id"] if row else None


def get_mik_root_collection_id(conn: sqlite3.Connection) -> str:
    """Get the UUID of the MIKRoot collection (main MIK library)."""
    row = conn.execute(
        "SELECT Id FROM Collection WHERE Name = 'MIKRoot' AND IsLibrary = 1"
    ).fetchone()
    if not row:
        raise RuntimeError(
            "MIKRoot collection not found in DB. "
            "Has MIK 11 been launched at least once on this Wine prefix?"
        )
    return row["Id"]


def get_disk_info(linux_path: str, config: dict) -> tuple:
    """
    Return (DiskIsRemovable, DiskLabel, DiskSerialNumber).
    Uses config values if set, otherwise tries to detect from mount.
    """
    disk_label = config.get("disk_label", "")
    disk_serial = config.get("disk_serial", "")
    disk_removable = config.get("disk_removable", 0)
    return disk_removable, disk_label, disk_serial


def insert_song(conn: sqlite3.Connection, wine_path: str, linux_path: str,
                file_path_hash: str, meta: dict, config: dict) -> str:
    """INSERT a new Song row with IsAnalyzed=0. Returns the new Song UUID."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    song_id = str(uuid.uuid4())
    ext = Path(linux_path).suffix.lower()
    disk_removable, disk_label, disk_serial = get_disk_info(linux_path, config)

    conn.execute("""
        INSERT INTO Song (
            Id, File, FilePathHash,
            ArtistName, SongName, Album, Genre, Year,
            Label, Remixer, Composer, Grouping,
            Tempo, OverallVolume, OverallEnergy, EnergySegmentsCount,
            StandardPitch,
            KeyResultSummary, MainKey, MainKeyConfidence,
            SecondKey, SecondKeyConfidence,
            IsAnalyzed,
            Comment,
            DateAdded, LastModifiedUtc, LastAnalyzedUtc,
            ClippedPeaksCount,
            HasPNTag, PNTagIsProcessed, PNTagAppliedClipRepair,
            PNTagVolumeAnalysisVersion, PNTagVolumeUnits, PNTagOutputVolume,
            OverallVolumeRMS1, OverallVolumeRMS2, OverallVolumeLUFS,
            DiskIsRemovable, DiskLabel, DiskSerialNumber,
            FileType, FileSize, Bitrate, SampleRate,
            Rating
        ) VALUES (
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, 0.0, 0, 0,
            0.0,
            NULL, NULL, 0.0,
            '-1A', 0.0,
            0,
            '',
            ?, ?, NULL,
            0,
            0, 0, 0,
            0, '', 0.0,
            0.0, 0.0, 0.0,
            ?, ?, ?,
            ?, ?, ?, ?,
            0
        )
    """, (
        song_id, wine_path, file_path_hash,
        meta["artist"], meta["title"], meta["album"], meta["genre"], meta["year"],
        meta["label"], meta["remixer"], meta["composer"], meta["grouping"],
        meta["bpm"],
        now_utc, now_utc,
        disk_removable, disk_label, disk_serial,
        ext, meta["filesize"], meta["bitrate"], meta["sample_rate"],
    ))
    return song_id


def insert_collection_membership(conn: sqlite3.Connection, song_id: str,
                                  collection_id: str) -> None:
    """Add track to MIKRoot collection."""
    # Get current max Sequence in this collection
    row = conn.execute(
        "SELECT COALESCE(MAX(Sequence), 0) as max_seq FROM SongCollectionMembership "
        "WHERE CollectionId = ?", (collection_id,)
    ).fetchone()
    next_seq = row["max_seq"] + 1

    membership_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO SongCollectionMembership (Id, SongId, CollectionId, Sequence)
        VALUES (?, ?, ?, ?)
    """, (membership_id, song_id, collection_id, next_seq))


# ---------------------------------------------------------------------------
# Main queue function
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif", ".mp4", ".ogg"}


def queue_file(linux_path: str, config: dict, dry_run: bool = False,
               backup: bool = True) -> bool:
    """
    Queue a single audio file for MIK analysis.
    Returns True if inserted, False if skipped (already exists).
    """
    linux_path = str(Path(linux_path).resolve())

    ext = Path(linux_path).suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        print(f"  [SKIP] Not an audio file: {linux_path}")
        return False

    if not os.path.exists(linux_path):
        print(f"  [SKIP] File not found: {linux_path}")
        return False

    # Translate path
    wine_path = linux_to_wine_path(linux_path, config)
    hash_algo = config.get("hash_algo", "sha256")
    file_path_hash = compute_file_path_hash(wine_path, hash_algo)

    db_path = Path(config["db_path"]).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"MIK DB not found: {db_path}")

    if dry_run:
        print(f"  [DRY RUN] Would queue: {linux_path}")
        print(f"            Wine path:  {wine_path}")
        print(f"            Hash:       {file_path_hash}")
        return True

    # Backup DB before first modification in this session
    if backup and not getattr(queue_file, "_backed_up", False):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_suffix(f".backup_{ts}")
        shutil.copy2(db_path, backup_path)
        print(f"  [BACKUP] {backup_path}")
        queue_file._backed_up = True

    conn = db_connect(db_path)
    try:
        # Dedup check
        existing_id = song_exists(conn, file_path_hash)
        if existing_id:
            print(f"  [EXISTS] Already in DB (id={existing_id}): {linux_path}")
            return False

        collection_id = get_mik_root_collection_id(conn)
        meta = read_audio_metadata(linux_path)

        conn.execute("BEGIN")
        try:
            song_id = insert_song(conn, wine_path, linux_path,
                                   file_path_hash, meta, config)
            insert_collection_membership(conn, song_id, collection_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        print(f"  [QUEUED] {linux_path}")
        print(f"           → {wine_path}")
        print(f"           id={song_id}")
        return True

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Queue audio files for MIK analysis via SQLite injection."
    )
    ap.add_argument("files", nargs="*", help="Audio file paths to queue.")
    ap.add_argument("--batch", help="Text file with one path per line.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview what would be inserted, no DB changes.")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip automatic DB backup before first write.")
    ap.add_argument("--config", default=None,
                    help="Path to config JSON (default: data/automation_config.json).")
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)

    files = list(args.files)
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            files += [line.strip() for line in f if line.strip()]

    if not files:
        ap.print_help()
        sys.exit(1)

    inserted = 0
    skipped = 0
    for fp in files:
        try:
            result = queue_file(fp, config,
                                dry_run=args.dry_run,
                                backup=not args.no_backup)
            if result:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [ERROR] {fp}: {e}")
            skipped += 1

    print(f"\nDone: {inserted} queued, {skipped} skipped/errors.")


if __name__ == "__main__":
    main()
```

---

## 6. Component: mik_watcher_daemon.py

This daemon watches the music directory and calls `mik_queue_insert.py` automatically.

```python
#!/usr/bin/env python3
"""
mik_watcher_daemon.py

Watches MUSIC_DIR for new audio files and queues them into MIK's DB.
Sends a restart signal to mik_process_manager after each batch.

Run as systemd service (see MIK_AUTOMATION_PLAN.md Section 9).

Usage:
    python mik_watcher_daemon.py
    python mik_watcher_daemon.py --config /path/to/config.json
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent
except ImportError:
    print("ERROR: watchdog is required. Run: pip install watchdog")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from mik_queue_insert import queue_file, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mik-watcher")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif"}


class MIKEventHandler(FileSystemEventHandler):
    """
    Handles file creation events. Uses a debounce timer to batch
    multiple rapid file additions (e.g., rsync of 100 files) into
    a single MIK restart rather than 100 restarts.
    """

    def __init__(self, config: dict, debounce_seconds: float = 30.0):
        super().__init__()
        self.config = config
        self.debounce_seconds = debounce_seconds
        self._pending: list = []
        self._timer: threading.Timer = None
        self._lock = threading.Lock()

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_path(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._handle_path(event.dest_path)

    def _handle_path(self, path: str):
        if Path(path).suffix.lower() not in AUDIO_EXTENSIONS:
            return
        log.info(f"New file detected: {path}")
        with self._lock:
            self._pending.append(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self.debounce_seconds, self._flush
            )
            self._timer.start()

    def _flush(self):
        with self._lock:
            files = list(self._pending)
            self._pending.clear()
            self._timer = None

        if not files:
            return

        log.info(f"Processing batch of {len(files)} file(s)...")
        inserted = 0
        for fp in files:
            try:
                # Brief wait: ensure file is fully written before reading metadata
                time.sleep(1)
                if queue_file(fp, self.config, dry_run=False, backup=True):
                    inserted += 1
            except Exception as e:
                log.error(f"Failed to queue {fp}: {e}")

        if inserted > 0:
            log.info(f"Queued {inserted} track(s). Signaling MIK restart...")
            _signal_mik_restart(self.config)
        else:
            log.info("No new tracks to queue (all duplicates or errors).")


def _signal_mik_restart(config: dict):
    """
    Signal the MIK process manager to restart MIK.
    Writes a flag file that mik_process_manager.py watches.
    """
    flag_path = Path(config.get("restart_flag_path",
                                "/tmp/mik_restart_requested"))
    flag_path.touch()
    log.info(f"Restart flag written: {flag_path}")


def main():
    ap = argparse.ArgumentParser(description="Watch music dir and queue new files into MIK.")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else (
        Path(__file__).parent.parent / "data" / "automation_config.json"
    )
    config = load_config(config_path)

    music_dir = config["music_dir"]
    debounce = config.get("debounce_seconds", 30.0)

    log.info(f"Starting MIK watcher. Watching: {music_dir}")
    log.info(f"Debounce: {debounce}s")

    handler = MIKEventHandler(config, debounce_seconds=debounce)
    observer = Observer()
    observer.schedule(handler, music_dir, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(5)
            if not observer.is_alive():
                log.error("Observer died, restarting...")
                observer.start()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
```

---

## 7. Component: mik_process_manager.py

Manages the Wine + MIK process lifecycle: graceful shutdown, Xvfb setup, restart, and completion detection.

```python
#!/usr/bin/env python3
"""
mik_process_manager.py

Manages the MIK Wine process:
- Watches for restart_flag_path flag file
- Gracefully stops MIK
- Restarts MIK under Wine + Xvfb
- Monitors until all IsAnalyzed=0 rows are gone (analysis complete)
- Optionally keeps MIK running continuously

Usage:
    python mik_process_manager.py            # Run as daemon
    python mik_process_manager.py --restart  # One-shot restart now
    python mik_process_manager.py --status   # Show current MIK Wine status
"""

import argparse
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mik_queue_insert import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mik-process-manager")


# ---------------------------------------------------------------------------
# Xvfb management
# ---------------------------------------------------------------------------

def ensure_xvfb(display: str = ":99") -> bool:
    """
    Ensure a virtual framebuffer is running on the given DISPLAY.
    Returns True if Xvfb is running (started or was already running).
    """
    result = subprocess.run(
        ["pgrep", "-f", f"Xvfb {display}"],
        capture_output=True
    )
    if result.returncode == 0:
        log.info(f"Xvfb already running on {display}")
        return True

    log.info(f"Starting Xvfb on {display}...")
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1024x768x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)  # Give Xvfb time to start

    result = subprocess.run(
        ["pgrep", "-f", f"Xvfb {display}"],
        capture_output=True
    )
    if result.returncode != 0:
        log.error("Failed to start Xvfb. Is it installed? apt install xvfb")
        return False
    log.info(f"Xvfb started on {display}")
    return True


# ---------------------------------------------------------------------------
# MIK Wine process management
# ---------------------------------------------------------------------------

def find_mik_pids() -> list:
    """Return PIDs of running MIK Wine processes."""
    result = subprocess.run(
        ["pgrep", "-f", "Mixed In Key"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [int(pid.strip()) for pid in result.stdout.strip().split("\n") if pid.strip()]


def stop_mik(timeout: int = 30) -> bool:
    """
    Gracefully stop MIK. Sends SIGTERM, waits, then SIGKILL if needed.
    Also runs wineserver -k to clean up Wine state.
    Returns True if stopped cleanly.
    """
    pids = find_mik_pids()
    if not pids:
        log.info("MIK not running.")
        return True

    log.info(f"Stopping MIK (PIDs: {pids})...")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for i in range(timeout):
        time.sleep(1)
        if not find_mik_pids():
            log.info(f"MIK stopped cleanly after {i+1}s.")
            break
    else:
        log.warning("MIK did not stop on SIGTERM, sending SIGKILL...")
        for pid in find_mik_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Clean up Wine server state
    log.info("Cleaning up Wine server...")
    subprocess.run(["wineserver", "-k"], capture_output=True)
    time.sleep(3)
    log.info("Wine server stopped.")
    return True


def start_mik(config: dict) -> subprocess.Popen:
    """
    Start MIK under Wine with a virtual display.
    Returns the Popen object for the Wine process.
    """
    display = config.get("wine_display", ":99")
    wine_exe = config.get("wine_executable", "wine")
    mik_exe = config.get("mik_windows_exe_path",
                          "C:\\Program Files\\Mixed In Key\\Mixed In Key 11\\Mixed In Key.exe")
    wine_prefix = config.get("wine_prefix", os.path.expanduser("~/.wine"))

    env = os.environ.copy()
    env["DISPLAY"] = display
    env["WINEPREFIX"] = wine_prefix
    env["WINEDEBUG"] = "-all"  # Suppress Wine debug spam

    log.info(f"Starting MIK: {wine_exe} '{mik_exe}' on DISPLAY={display}")
    proc = subprocess.Popen(
        [wine_exe, mik_exe],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    log.info(f"MIK process started (PID: {proc.pid})")
    return proc


# ---------------------------------------------------------------------------
# Analysis completion detection
# ---------------------------------------------------------------------------

def count_pending(db_path: Path) -> int:
    """Count rows in Song with IsAnalyzed=0."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        row = conn.execute(
            "SELECT COUNT(*) as c FROM Song WHERE IsAnalyzed = 0"
        ).fetchone()
        conn.close()
        return row[0]
    except Exception:
        return -1


def wait_for_analysis_complete(db_path: Path, poll_interval: int = 60,
                                timeout_minutes: int = 240) -> bool:
    """
    Poll the DB until all IsAnalyzed=0 rows are gone.
    Returns True if complete, False if timed out.
    """
    log.info("Waiting for MIK to complete analysis...")
    start = time.time()
    timeout_seconds = timeout_minutes * 60

    while True:
        pending = count_pending(db_path)
        elapsed = int(time.time() - start)
        log.info(f"  Pending tracks: {pending} (elapsed: {elapsed}s)")

        if pending == 0:
            log.info("All tracks analyzed!")
            return True

        if elapsed > timeout_seconds:
            log.warning(f"Timed out after {timeout_minutes} min. {pending} tracks still pending.")
            return False

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Restart routine
# ---------------------------------------------------------------------------

def do_restart(config: dict):
    """Full restart cycle: stop → start → wait for completion."""
    display = config.get("wine_display", ":99")
    db_path = Path(config["db_path"]).expanduser()

    pending_before = count_pending(db_path)
    if pending_before == 0:
        log.info("No pending tracks. Restart skipped.")
        return

    log.info(f"Starting restart cycle. Pending tracks: {pending_before}")

    stop_mik()
    ensure_xvfb(display)
    start_mik(config)

    # Give MIK time to load and start the analysis queue
    startup_wait = config.get("mik_startup_wait_seconds", 15)
    log.info(f"Waiting {startup_wait}s for MIK to initialize...")
    time.sleep(startup_wait)

    wait_for_analysis_complete(
        db_path,
        poll_interval=config.get("poll_interval_seconds", 60),
        timeout_minutes=config.get("analysis_timeout_minutes", 240)
    )


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def daemon_loop(config: dict):
    """Watch for restart flag and trigger restarts."""
    flag_path = Path(config.get("restart_flag_path", "/tmp/mik_restart_requested"))
    poll_seconds = config.get("manager_poll_seconds", 10)

    log.info(f"MIK process manager running. Watching: {flag_path}")

    # Start MIK immediately if there are pending tracks
    db_path = Path(config["db_path"]).expanduser()
    if count_pending(db_path) > 0:
        log.info("Found pending tracks on startup. Triggering immediate restart.")
        do_restart(config)

    while True:
        try:
            if flag_path.exists():
                flag_path.unlink()
                log.info("Restart flag detected. Initiating restart cycle.")
                do_restart(config)
        except Exception as e:
            log.error(f"Error in daemon loop: {e}")
        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Manage MIK Wine process.")
    ap.add_argument("--restart", action="store_true",
                    help="Perform one-shot restart now.")
    ap.add_argument("--stop", action="store_true",
                    help="Stop MIK and exit.")
    ap.add_argument("--status", action="store_true",
                    help="Show MIK process status and pending count.")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else (
        Path(__file__).parent.parent / "data" / "automation_config.json"
    )
    config = load_config(config_path)

    if args.status:
        pids = find_mik_pids()
        db_path = Path(config["db_path"]).expanduser()
        pending = count_pending(db_path)
        print(f"MIK PIDs: {pids if pids else 'not running'}")
        print(f"Pending tracks (IsAnalyzed=0): {pending}")
        return

    if args.stop:
        stop_mik()
        return

    if args.restart:
        do_restart(config)
        return

    # Default: daemon mode
    daemon_loop(config)


if __name__ == "__main__":
    main()
```

---

## 8. Configuration: config.json

Save as `data/automation_config.json`. **This file is gitignored** (contains local paths).

A template is provided at `data/automation_config.example.json`:

```json
{
  "_comment": "MIK Automation Config — edit paths for your setup",

  "db_path": "~/.wine/drive_c/users/liu/AppData/Local/Mixed In Key/Mixed In Key/11.0/MIKStore.db",

  "music_dir": "/mnt/music",

  "path_map": {
    "/mnt/music": "H:\\music"
  },

  "hash_algo": "sha256",

  "disk_label": "",
  "disk_serial": "",
  "disk_removable": 0,

  "wine_display": ":99",
  "wine_executable": "wine",
  "wine_prefix": "~/.wine",
  "mik_windows_exe_path": "C:\\Program Files\\Mixed In Key\\Mixed In Key 11\\Mixed In Key.exe",

  "restart_flag_path": "/tmp/mik_restart_requested",
  "debounce_seconds": 30,
  "mik_startup_wait_seconds": 15,
  "poll_interval_seconds": 60,
  "analysis_timeout_minutes": 240,
  "manager_poll_seconds": 10
}
```

### Key Config Fields

| Field | Description |
|---|---|
| `db_path` | Path to `MIKStore.db` in Wine prefix. Use `~` for home dir. |
| `music_dir` | Linux path to watch for new audio files. |
| `path_map` | Dict mapping Linux path prefix → Windows path prefix. Must match what MIK sees. |
| `hash_algo` | Hash algorithm for FilePathHash. Run `mik_identify_hash.py` to verify. Options: `sha256`, `sha256_lower`, `md5`, `md5_lower`, `sha1` |
| `disk_label` | Volume label of your music disk (check with `lsblk -o LABEL`). Can be empty. |
| `disk_serial` | Serial number of music disk (check with `lsblk -o SERIAL`). Can be empty. |
| `wine_prefix` | Path to Wine prefix directory. Default: `~/.wine`. |
| `mik_windows_exe_path` | Windows path to `Mixed In Key.exe` inside Wine. |
| `debounce_seconds` | Wait this long after last file event before triggering restart. Batches rapid file additions. |
| `analysis_timeout_minutes` | Give up waiting for analysis after this many minutes. |

---

## 9. Systemd Service Files

Create two services: the file watcher and the process manager.

### `/etc/systemd/system/mik-watcher.service`

```ini
[Unit]
Description=MIK File Watcher — queues new audio files for analysis
After=network.target
Wants=mik-process-manager.service

[Service]
Type=simple
User=liu
WorkingDirectory=/home/liu/Basedline
ExecStart=/usr/bin/python3 /home/liu/Basedline/MixedinKey/mik_watcher_daemon.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mik-watcher

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/mik-process-manager.service`

```ini
[Unit]
Description=MIK Process Manager — controls Wine/MIK lifecycle
After=network.target

[Service]
Type=simple
User=liu
WorkingDirectory=/home/liu/Basedline
ExecStart=/usr/bin/python3 /home/liu/Basedline/MixedinKey/mik_process_manager.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mik-process-manager
Environment=WINEPREFIX=/home/liu/.wine
Environment=DISPLAY=:99

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable mik-watcher mik-process-manager
sudo systemctl start mik-watcher mik-process-manager

# Check status
sudo systemctl status mik-watcher
sudo systemctl status mik-process-manager

# Follow logs
journalctl -u mik-watcher -f
journalctl -u mik-process-manager -f
```

---

## 10. Testing Procedure

Follow this step-by-step to validate the system before going live.

### Step 1: Verify FilePathHash algorithm

```bash
# From the repo root
python3 MixedinKey/mik_identify_hash.py ~/.wine/drive_c/users/liu/AppData/Local/"Mixed In Key"/"Mixed In Key"/11.0/MIKStore.db
```

Expected output:
```
MATCH FOUND: SHA256 / utf-8
  Path variant: H:\music\something.mp3
  Known hash:   a3f2...
```

If algo is not `sha256`, update `hash_algo` in `automation_config.json`.

### Step 2: Dry-run a single file

```bash
python3 MixedinKey/mik_queue_insert.py --dry-run /mnt/music/test_track.mp3
```

Should print the wine path and hash without touching the DB.

### Step 3: Live insert test (DB backup auto-created)

```bash
# Backup is auto-created before first write
python3 MixedinKey/mik_queue_insert.py /mnt/music/test_track.mp3
```

Verify in DB:
```bash
sqlite3 ~/.wine/drive_c/users/liu/AppData/Local/"Mixed In Key"/"Mixed In Key"/11.0/MIKStore.db \
  "SELECT Id, File, IsAnalyzed, MainKey FROM Song ORDER BY DateAdded DESC LIMIT 3;"
```

Expected: new row with `IsAnalyzed=0`, `MainKey=NULL`.

### Step 4: Trigger MIK restart and watch

```bash
python3 MixedinKey/mik_process_manager.py --restart
```

Watch logs:
```bash
journalctl -u mik-process-manager -f
```

After MIK finishes, verify:
```bash
sqlite3 ... "SELECT File, IsAnalyzed, MainKey, Tempo FROM Song WHERE File LIKE '%test_track%';"
```

Expected: `IsAnalyzed=1`, `MainKey` has a Camelot value (e.g., `8A`), `Tempo` has BPM.

### Step 5: End-to-end watcher test

```bash
# Start services
sudo systemctl start mik-watcher mik-process-manager

# Drop a new file into the watched directory
cp /tmp/new_track.mp3 /mnt/music/new_track.mp3

# Watch watcher logs
journalctl -u mik-watcher -f
# Should see: "New file detected", "Queued 1 track(s)", "Signaling MIK restart"

# After debounce_seconds, watch process manager
journalctl -u mik-process-manager -f
# Should see: "Restart flag detected", "Starting MIK", eventually "All tracks analyzed!"
```

---

## 11. Batch Backfill: Queuing an Entire Directory

To queue an entire existing music directory that MIK hasn't seen:

```bash
# Generate file list
find /mnt/music -type f \( -name '*.mp3' -o -name '*.flac' -o -name '*.m4a' \) \
  > /tmp/music_filelist.txt

wc -l /tmp/music_filelist.txt  # Check count

# Dry run first
python3 MixedinKey/mik_queue_insert.py --batch /tmp/music_filelist.txt --dry-run

# Live insert (will create one backup, then batch insert)
python3 MixedinKey/mik_queue_insert.py --batch /tmp/music_filelist.txt
```

For very large libraries (100k+ tracks), this will generate a lot of DB inserts. Run it overnight and trigger MIK restart when done:

```bash
python3 MixedinKey/mik_process_manager.py --restart
```

MIK's cloud analysis rate may limit throughput. At ~1-5 sec/track, 100k tracks = 28-140 hours of analysis time.

---

## 12. Known Limitations and Risks

### ⚠️ FilePathHash mismatch (highest risk)
If the `hash_algo` in config does not match what MIK actually uses, MIK will **not recognize** injected tracks as duplicates. MIK will create duplicate entries when it later scans the same file via its own UI. **Mitigation:** Always run `mik_identify_hash.py` before first use.

### ⚠️ IsAnalyzed trigger assumption (unverified)
The assumption that `IsAnalyzed=0` rows are auto-picked-up on MIK startup is based on logical inference from the schema. It has not been empirically verified. **If this assumption is wrong**, the fallback is the diff-based approach (see [Section 13](#13-open-questions-for-liu)).

### ⚠️ Wine + ARM (Box64) stability
Running MIK (Windows x86_64) on Raspberry Pi ARM requires Box64 for binary translation. Performance will be significantly slower than native Windows, and stability is not guaranteed. MIK's network calls (cloud analysis) should not be affected by this, but startup time may be very long. **Mitigation:** Increase `mik_startup_wait_seconds` in config if MIK doesn't have time to initialize.

### ⚠️ SQLite concurrent access
If MIK is running while `mik_queue_insert.py` writes to the DB, there is a risk of write contention. The script uses `PRAGMA journal_mode=WAL` which greatly reduces this risk, but ideally MIK should not be running during batch inserts. The watcher daemon uses a debounce to batch inserts before triggering MIK restart, which naturally separates write phases.

### ⚠️ DB schema changes between MIK versions
The documented schema is for MIK 11 (SchemaVersion 11009). Future MIK updates may change the schema. **Mitigation:** Always backup before writes.

### ⚠️ MIK may re-analyze existing tracks
If MIK's internal logic re-queues tracks for re-analysis (e.g., when it detects `FilePathHash` collision with different file content), BPM/Key data will be overwritten. This is generally desired behavior but worth noting.

### Cloud analysis requires internet
MIK only works with an active internet connection. The Raspi must have outbound internet access when MIK is running.

---

## 13. Open Questions for Liu

These could not be resolved without empirical testing. Best guesses have been implemented, but answers may require config or code changes.

1. **FilePathHash algorithm** — `mik_identify_hash.py` will answer this definitively. Currently defaulting to SHA256 of UTF-8 Windows path. If wrong, update `hash_algo` in config.

2. **Exact Windows path of MIK EXE in Wine** — config defaults to `C:\Program Files\Mixed In Key\Mixed In Key 11\Mixed In Key.exe`. Check actual path with:
   ```bash
   find ~/.wine -name 'Mixed In Key.exe' 2>/dev/null
   ```

3. **Username inside Wine prefix** — affects DB path. Config defaults use `liu`. Check with:
   ```bash
   ls ~/.wine/drive_c/users/
   ```

4. **Drive letter for music SSD** — config defaults to `H:\`. Check Wine dosdevices:
   ```bash
   ls -la ~/.wine/dosdevices/
   ```
   If no drive is mapped, create the symlink as shown in [Section 3](#3-path-translation-linux--wine).

5. **IsAnalyzed=0 trigger confirmation** — if MIK does NOT auto-analyze rows on startup, the alternative is GUI automation via `xdotool` to simulate drag-and-drop. This is the fallback plan and can be implemented if needed.

6. **DiskLabel / DiskSerialNumber** — these are stored by MIK when it adds a track. The correct values for the SMB-mounted music SSD are unknown. The script defaults to empty strings, which should be fine — MIK may overwrite these fields when it processes the queued track.
