# Windows MIK Automation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Wine/Raspi MIK automation with a Windows-native pipeline that watches for new audio files, injects them into MIK's DB, and launches MIK to analyze them.

**Architecture:** Three independent scripts (`mik_queue_insert.py`, `mik_launcher.py`, `mik_watcher.py`) plus a config template. The watcher imports the other two. Cleanup deletes Wine/Raspi-specific files first.

**Tech Stack:** Python 3.10+, watchdog, mutagen, sqlite3 (stdlib), subprocess (stdlib)

**Spec:** `docs/superpowers/specs/2026-03-11-windows-mik-automation-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Delete | `MixedinKey/mik_process_manager.py` | Wine/Xvfb process manager (obsolete) |
| Delete | `MixedinKey/mik_watcher_daemon.py` | Linux inotify watcher (obsolete) |
| Delete | `MixedinKey/MIK_AUTOMATION_PLAN.md` | Raspi/Wine plan doc (obsolete) |
| Delete | `data/automation_config.example.json` | Wine-specific config (obsolete) |
| Rewrite | `MixedinKey/mik_queue_insert.py` | Insert audio files into MIKStore.db as unanalyzed |
| Create | `MixedinKey/mik_launcher.py` | Windows-native MIK.exe process management |
| Create | `MixedinKey/mik_watcher.py` | Watch directory for new files, orchestrate pipeline |
| Create | `data/automation_config.example.json` | Windows config template |
| Modify | `README.md:340-405` | Replace Raspi/Wine section with Windows automation docs |
| Modify | `requirements.txt` | Add `watchdog` dependency |

---

## Chunk 1: Cleanup and Config

### Task 1: Delete Wine/Raspi-specific files

**Files:**
- Delete: `MixedinKey/mik_process_manager.py`
- Delete: `MixedinKey/mik_watcher_daemon.py`
- Delete: `MixedinKey/MIK_AUTOMATION_PLAN.md`
- Delete: `data/automation_config.example.json`

- [ ] **Step 1: Delete the four obsolete files**

```bash
git rm MixedinKey/mik_process_manager.py
git rm MixedinKey/mik_watcher_daemon.py
git rm MixedinKey/MIK_AUTOMATION_PLAN.md
git rm data/automation_config.example.json
```

- [ ] **Step 2: Verify no other files import the deleted modules**

```bash
grep -r "mik_process_manager\|mik_watcher_daemon" --include="*.py" .
```

Expected: no matches (these modules are only imported by each other and run standalone).

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove Wine/Raspi automation files

Delete mik_process_manager.py, mik_watcher_daemon.py,
MIK_AUTOMATION_PLAN.md, and Wine-specific config template.
These are replaced by Windows-native automation."
```

---

### Task 2: Create Windows config template

**Dependency:** Must run after Task 1 (Task 1 deletes the old config at the same path).

**Files:**
- Create: `data/automation_config.example.json`

- [ ] **Step 1: Write the config template**

```json
{
  "_comment": "MIK Automation Config — copy to automation_config.json and edit for your setup",

  "db_path": "C:\\Users\\<USER>\\AppData\\Local\\Mixed In Key\\Mixed In Key\\11.0\\MIKStore.db",

  "watch_dir": "H:\\music",

  "audio_extensions": [".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif"],

  "hash_algo": "sha256",
  "hash_encoding": "utf-8",

  "debounce_seconds": 30,

  "mik_exe_path": "C:\\Program Files\\Mixed In Key\\11\\Mixed In Key.exe",
  "mik_startup_wait_seconds": 15,
  "poll_interval_seconds": 60,
  "analysis_timeout_minutes": 240,
  "close_mik_when_done": false,

  "disk_is_removable": 0,
  "disk_label": "",
  "disk_serial_number": "",

  "log_file": "data/mik_watcher.log"
}
```

- [ ] **Step 2: Verify JSON is valid**

```bash
python -c "import json; json.load(open('data/automation_config.example.json'))"
```

Expected: no output (valid JSON).

- [ ] **Step 3: Commit**

```bash
git add data/automation_config.example.json
git commit -m "feat: add Windows automation config template

Replaces Wine-specific config with native Windows paths.
No path_map, wine_prefix, or wine_display fields."
```

---

### Task 3: Add watchdog to requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Read current requirements.txt**

Read `requirements.txt` to see current contents.

- [ ] **Step 2: Add watchdog**

Add `watchdog` to `requirements.txt` (after `mutagen`).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add watchdog to requirements.txt"
```

---

## Chunk 2: mik_queue_insert.py (rewrite)

### Task 4: Rewrite mik_queue_insert.py for Windows

**Files:**
- Rewrite: `MixedinKey/mik_queue_insert.py`

This is a rewrite of the existing file. The core DB insertion logic stays the same. What changes: remove `linux_to_wine_path()`, use file paths as-is, switch from `print()` to `logging`, update config key names.

- [ ] **Step 1: Overwrite `MixedinKey/mik_queue_insert.py` with the following content**

This is a full rewrite — replace the entire file. Key changes from the existing version:
- Remove `linux_to_wine_path()` function entirely
- Remove the call to `linux_to_wine_path()` in `queue_file()` — use the file path directly
- Replace all `print()` calls with `logging` (`log.info()`, `log.warning()`, `log.error()`)
- Update config key names: `disk_removable` → `disk_is_removable`, `disk_serial` → `disk_serial_number`
- Add `hash_encoding` config support (default `utf-8`)
- `queue_file()` returns `bool` (True if inserted)
- `main()` returns inserted count via `sys.exit(0)` on success

```python
#!/usr/bin/env python3
"""
mik_queue_insert.py — Insert audio files into MIKStore.db as unanalyzed tracks.

MIK will pick them up and analyze on next startup.

Usage:
    python mik_queue_insert.py H:\\music\\track.mp3
    python mik_queue_insert.py H:\\music\\*.flac
    python mik_queue_insert.py --batch filelist.txt
    python mik_queue_insert.py --dry-run H:\\music\\track.mp3
"""

import argparse
import hashlib
import json
import logging
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
except ImportError:
    print("ERROR: mutagen is required. Run: pip install mutagen")
    sys.exit(1)

log = logging.getLogger("mik-queue-insert")

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "data" / "automation_config.json"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            f"Create from template: data/automation_config.example.json"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_file_path_hash(file_path: str, algo: str = "sha256",
                           encoding: str = "utf-8") -> str:
    # Support lowercase variants (e.g. "sha256_lower", "md5_lower")
    # which hash the lowercased path. These are candidates tested by
    # mik_identify_hash.py to discover MIK's actual algorithm.
    if algo.endswith("_lower"):
        file_path = file_path.lower()
        algo = algo[:-6]  # strip "_lower"
    data = file_path.encode(encoding)
    h = hashlib.new(algo, data)
    return h.hexdigest()


def read_audio_metadata(file_path: str) -> dict:
    meta = {
        "artist": "", "title": "", "album": "", "genre": "", "year": 0,
        "label": "", "remixer": "", "composer": "", "grouping": "",
        "bpm": None, "bitrate": 0, "sample_rate": 44100,
        "filesize": os.path.getsize(file_path),
    }
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return meta
        if hasattr(audio, "info"):
            meta["bitrate"] = int(getattr(audio.info, "bitrate", 0) / 1000)
            meta["sample_rate"] = getattr(audio.info, "sample_rate", 44100)

        def get(key):
            val = audio.get(key)
            return str(val[0]).strip() if val else ""

        meta["artist"] = get("artist")
        meta["title"] = get("title")
        meta["album"] = get("album")
        meta["genre"] = get("genre")
        meta["label"] = get("organization") or get("label")
        meta["remixer"] = get("remixer")
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
        log.warning(f"Metadata read failed for {file_path}: {e}")
    return meta


def db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def song_exists(conn: sqlite3.Connection, file_path_hash: str) -> Optional[str]:
    row = conn.execute(
        "SELECT Id FROM Song WHERE FilePathHash = ?", (file_path_hash,)
    ).fetchone()
    return row["Id"] if row else None


def get_mik_root_collection_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT Id FROM Collection WHERE Name = 'MIKRoot' AND IsLibrary = 1"
    ).fetchone()
    if not row:
        raise RuntimeError(
            "MIKRoot collection not found. Has MIK been launched at least once?"
        )
    return row["Id"]


def insert_song(conn: sqlite3.Connection, file_path: str,
                file_path_hash: str, meta: dict, config: dict) -> str:
    now_utc = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    song_id = str(uuid.uuid4())
    ext = Path(file_path).suffix.lower()

    conn.execute("""
        INSERT INTO Song (
            Id, File, FilePathHash,
            ArtistName, SongName, Album, Genre, Year,
            Label, Remixer, Composer, Grouping,
            Tempo, OverallVolume, OverallEnergy, EnergySegmentsCount,
            StandardPitch, KeyResultSummary, MainKey, MainKeyConfidence,
            SecondKey, SecondKeyConfidence,
            IsAnalyzed, Comment,
            DateAdded, LastModifiedUtc, LastAnalyzedUtc,
            ClippedPeaksCount,
            HasPNTag, PNTagIsProcessed, PNTagAppliedClipRepair,
            PNTagVolumeAnalysisVersion, PNTagVolumeUnits, PNTagOutputVolume,
            OverallVolumeRMS1, OverallVolumeRMS2, OverallVolumeLUFS,
            DiskIsRemovable, DiskLabel, DiskSerialNumber,
            FileType, FileSize, Bitrate, SampleRate, Rating
        ) VALUES (
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, 0.0, 0, 0,
            0.0, NULL, NULL, 0.0,
            '-1A', 0.0,
            0, '',
            ?, ?, NULL,
            0,
            0, 0, 0,
            0, '', 0.0,
            0.0, 0.0, 0.0,
            ?, ?, ?,
            ?, ?, ?, ?, 0
        )
    """, (
        song_id, file_path, file_path_hash,
        meta["artist"], meta["title"], meta["album"], meta["genre"],
        meta["year"],
        meta["label"], meta["remixer"], meta["composer"], meta["grouping"],
        meta["bpm"],
        now_utc, now_utc,
        config.get("disk_is_removable", 0),
        config.get("disk_label", ""),
        config.get("disk_serial_number", ""),
        ext, meta["filesize"], meta["bitrate"], meta["sample_rate"],
    ))
    return song_id


def insert_collection_membership(conn: sqlite3.Connection, song_id: str,
                                 collection_id: str) -> None:
    row = conn.execute(
        "SELECT COALESCE(MAX(Sequence), 0) as m "
        "FROM SongCollectionMembership WHERE CollectionId = ?",
        (collection_id,)
    ).fetchone()
    next_seq = row["m"] + 1
    conn.execute(
        "INSERT INTO SongCollectionMembership "
        "(Id, SongId, CollectionId, Sequence) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), song_id, collection_id, next_seq)
    )


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif",
                    ".mp4", ".ogg"}
_backed_up = False


def queue_file(file_path: str, config: dict, dry_run: bool = False,
               no_backup: bool = False) -> bool:
    global _backed_up
    file_path = str(Path(file_path).resolve())
    ext = Path(file_path).suffix.lower()

    audio_exts = config.get("audio_extensions", AUDIO_EXTENSIONS)
    if isinstance(audio_exts, list):
        audio_exts = set(audio_exts)
    if ext not in audio_exts:
        log.info(f"[SKIP] Not audio: {file_path}")
        return False
    if not os.path.exists(file_path):
        log.info(f"[SKIP] Not found: {file_path}")
        return False

    hash_algo = config.get("hash_algo", "sha256")
    hash_encoding = config.get("hash_encoding", "utf-8")
    file_path_hash = compute_file_path_hash(file_path, hash_algo,
                                            hash_encoding)

    db_path = Path(config["db_path"]).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    if dry_run:
        log.info(f"[DRY RUN] {file_path}")
        log.info(f"          hash: {file_path_hash}")
        return True

    if not no_backup and not _backed_up:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_suffix(f".backup_{ts}")
        shutil.copy2(db_path, backup_path)
        log.info(f"[BACKUP] {backup_path}")
        _backed_up = True

    conn = db_connect(db_path)
    try:
        existing = song_exists(conn, file_path_hash)
        if existing:
            log.info(f"[EXISTS] {file_path} (id={existing})")
            return False
        collection_id = get_mik_root_collection_id(conn)
        meta = read_audio_metadata(file_path)
        conn.execute("BEGIN")
        try:
            song_id = insert_song(conn, file_path, file_path_hash,
                                  meta, config)
            insert_collection_membership(conn, song_id, collection_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info(f"[QUEUED] {file_path} (id={song_id})")
        return True
    finally:
        conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(
        description="Queue audio files for MIK analysis.")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--batch", help="Text file with one path per line.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)

    files = list(args.files)
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            files += [ln.strip() for ln in f if ln.strip()]

    if not files:
        ap.print_help()
        sys.exit(1)

    inserted = skipped = 0
    for fp in files:
        try:
            if queue_file(fp, config, dry_run=args.dry_run,
                          no_backup=args.no_backup):
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"{fp}: {e}")
            skipped += 1

    log.info(f"Done: {inserted} queued, {skipped} skipped/errors.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('MixedinKey/mik_queue_insert.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add MixedinKey/mik_queue_insert.py
git commit -m "feat: rewrite mik_queue_insert.py for Windows

Remove linux_to_wine_path() and Wine path translation.
Use file paths as-is (native Windows paths).
Switch from print() to logging module.
Update config key names to match new template."
```

---

## Chunk 3: mik_launcher.py

### Task 5: Create mik_launcher.py

**Files:**
- Create: `MixedinKey/mik_launcher.py`

Windows-native MIK process management. No Wine, no Xvfb, no POSIX signals.

- [ ] **Step 1: Write mik_launcher.py**

```python
#!/usr/bin/env python3
"""
mik_launcher.py — Manage Mixed In Key process lifecycle on Windows.

Usage:
    python mik_launcher.py start
    python mik_launcher.py stop
    python mik_launcher.py restart
    python mik_launcher.py status
    python mik_launcher.py wait
"""

import argparse
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mik_queue_insert import load_config, DEFAULT_CONFIG_PATH

log = logging.getLogger("mik-launcher")

MIK_PROCESS_NAME = "Mixed In Key.exe"


def is_mik_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {MIK_PROCESS_NAME}",
             "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10
        )
        return MIK_PROCESS_NAME.lower() in result.stdout.lower()
    except Exception as e:
        log.warning(f"Could not check MIK status: {e}")
        return False


def stop_mik(timeout: int = 10) -> bool:
    if not is_mik_running():
        log.info("MIK is not running.")
        return True
    log.info("Stopping MIK...")
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", MIK_PROCESS_NAME],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        log.error(f"taskkill failed: {e}")
        return False
    for i in range(timeout):
        time.sleep(1)
        if not is_mik_running():
            log.info(f"MIK stopped after {i + 1}s.")
            return True
    log.warning(f"MIK still running after {timeout}s.")
    return False


def start_mik(config: dict) -> bool:
    mik_exe = config.get(
        "mik_exe_path",
        r"C:\Program Files\Mixed In Key\11\Mixed In Key.exe"
    )
    if not Path(mik_exe).exists():
        log.error(f"MIK exe not found: {mik_exe}")
        return False
    log.info(f"Starting MIK: {mik_exe}")
    try:
        subprocess.Popen(
            [mik_exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS
        )
    except Exception as e:
        log.error(f"Failed to start MIK: {e}")
        return False
    startup_wait = config.get("mik_startup_wait_seconds", 15)
    log.info(f"Waiting {startup_wait}s for MIK to initialize...")
    time.sleep(startup_wait)
    if is_mik_running():
        log.info("MIK is running.")
        return True
    log.warning("MIK does not appear to be running after startup.")
    return False


def restart_mik(config: dict) -> bool:
    if is_mik_running():
        if not stop_mik():
            return False
    return start_mik(config)


def count_pending(config: dict) -> int:
    db_path = Path(config["db_path"]).expanduser()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        c = conn.execute(
            "SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 0"
        ).fetchone()[0]
        conn.close()
        return c
    except Exception as e:
        log.warning(f"Could not query DB: {e}")
        return -1


def wait_for_completion(config: dict) -> bool:
    poll = config.get("poll_interval_seconds", 60)
    timeout_min = config.get("analysis_timeout_minutes", 240)
    start = time.time()
    while True:
        pending = count_pending(config)
        elapsed = int(time.time() - start)
        log.info(f"Pending: {pending} tracks (elapsed: {elapsed}s)")
        if pending == 0:
            log.info("Analysis complete — queue drained.")
            if config.get("close_mik_when_done", False):
                log.info("Closing MIK (close_mik_when_done=true).")
                stop_mik()
            return True
        if pending < 0:
            log.warning("Could not read DB. Retrying...")
        if elapsed > timeout_min * 60:
            log.warning(
                f"Timeout after {timeout_min}min. {pending} still pending."
            )
            return False
        time.sleep(poll)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Manage MIK process on Windows.")
    ap.add_argument("action", choices=["start", "stop", "restart", "status",
                                       "wait"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)

    if args.action == "status":
        running = is_mik_running()
        pending = count_pending(config)
        print(f"MIK: {'running' if running else 'not running'}")
        print(f"Pending (IsAnalyzed=0): {pending}")
    elif args.action == "start":
        start_mik(config)
    elif args.action == "stop":
        stop_mik()
    elif args.action == "restart":
        restart_mik(config)
    elif args.action == "wait":
        success = wait_for_completion(config)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('MixedinKey/mik_launcher.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add MixedinKey/mik_launcher.py
git commit -m "feat: add mik_launcher.py for Windows MIK process management

Native Windows process control using tasklist/taskkill.
Supports start, stop, restart, status, and wait-for-completion.
Replaces Wine/Xvfb-based mik_process_manager.py."
```

---

## Chunk 4: mik_watcher.py

### Task 6: Create mik_watcher.py

**Files:**
- Create: `MixedinKey/mik_watcher.py`

Depends on: `mik_queue_insert.queue_file()` and `mik_launcher.restart_mik()` (Tasks 4 and 5 must be done first).

- [ ] **Step 1: Write mik_watcher.py**

```python
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
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('MixedinKey/mik_watcher.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add MixedinKey/mik_watcher.py
git commit -m "feat: add mik_watcher.py for Windows file watching

Watches configurable directory via watchdog for new audio files.
Debounces, checks file stability (SMB), injects into MIK DB,
and restarts MIK. Handles network share reconnection."
```

---

## Chunk 5: README Update

### Task 7: Update README.md

**Files:**
- Modify: `README.md:340-405`

Replace the Raspi/Wine automation section with Windows automation docs.

- [ ] **Step 1: Read README.md lines 338-405 to confirm exact section boundaries**

Read `README.md` and identify the exact start/end of the "MIK Automation Plan (Raspberry Pi / Wine / Headless)" section.

- [ ] **Step 2: Replace the section**

Replace lines 340-405 (the `## 🤖 MIK Automation Plan` section through the trailing `---`) with:

```markdown
## Windows MIK Automation

This fork includes scripts for **automated Mixed In Key 11 analysis** on Windows. The pipeline watches a music directory (e.g. a network share) for new audio files, injects them into MIK's database as unanalyzed, and restarts MIK to trigger analysis.

MIK writes Key/BPM/Energy tags back into the audio files automatically (when "Write to file tags" is enabled in MIK settings).

### How It Works

```
New file on disk (e.g. H:\music\track.mp3)
      ↓
mik_watcher.py     (watchdog, debounced, file-stability check)
      ↓
mik_queue_insert.py (INSERT into Song with IsAnalyzed=0)
      ↓
mik_launcher.py     (restart MIK.exe)
      ↓
MIK 11              (analyzes tracks, writes tags to files)
```

### Scripts

| File | Description |
|---|---|
| [`MixedinKey/mik_queue_insert.py`](MixedinKey/mik_queue_insert.py) | Insert audio files into MIK's DB as unanalyzed tracks |
| [`MixedinKey/mik_watcher.py`](MixedinKey/mik_watcher.py) | Watch directory for new files, trigger queue insert + MIK restart |
| [`MixedinKey/mik_launcher.py`](MixedinKey/mik_launcher.py) | Manage MIK.exe process (start/stop/restart/wait) |
| [`MixedinKey/mik_identify_hash.py`](MixedinKey/mik_identify_hash.py) | One-time script to verify the `FilePathHash` algorithm |
| [`data/automation_config.example.json`](data/automation_config.example.json) | Config template — copy and fill in your paths |

### Quick Start

```bash
# 1. Copy and fill in config
copy data\automation_config.example.json data\automation_config.json
# Edit: db_path, watch_dir, mik_exe_path

# 2. Install dependencies
pip install mutagen watchdog

# 3. Confirm FilePathHash algorithm (run once against your live DB)
python MixedinKey\mik_identify_hash.py

# 4. Dry-run a single file
python MixedinKey\mik_queue_insert.py --dry-run H:\music\track.mp3

# 5. Live test a single file
python MixedinKey\mik_queue_insert.py H:\music\track.mp3

# 6. Run the watcher (add to Windows Startup for always-on)
python MixedinKey\mik_watcher.py
```

### Before You Run

1. **`FilePathHash` algorithm** — Run `mik_identify_hash.py` against your live DB first. Inserting with the wrong hash causes duplicate rows.
2. **MIK must have been launched once** — The scripts need the `MIKRoot` collection to exist in the DB.

```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: replace Raspi/Wine automation section with Windows docs

Remove references to Wine, Xvfb, systemd, and Linux paths.
Document the three new Windows-native automation scripts."
```

---

## Parallelization Summary for Subagents

```
           ┌─────────────────┐
           │  Task 1: Delete  │
           │  Wine/Raspi files│
           └────────┬────────┘
                    │
    ┌───────────────┼───────────────┬──────────────────┐
    │               │               │                  │
    v               v               v                  v
┌────────┐   ┌───────────┐   ┌───────────┐   ┌──────────────┐
│ Task 2 │   │  Task 3   │   │  Task 4   │   │   Task 5     │
│ Config │   │ req.txt   │   │ queue_    │   │  launcher.py │
│template│   │           │   │ insert.py │   │              │
│(after 1)│  └───────────┘   └─────┬─────┘   └──────┬───────┘
└────────┘                         │                 │
                                   └────────┬────────┘
                                            │
                                            v
                                     ┌────────────┐
                                     │  Task 6    │
                                     │ watcher.py │
                                     └──────┬─────┘
                                            │
                                            v
                                     ┌────────────┐
                                     │  Task 7    │
                                     │ README.md  │
                                     └────────────┘
```

**Wave 1 (parallel):** Tasks 1, 3, 4, 5 — all independent
**Wave 1.5:** Task 2 — depends on Task 1 (same file path: `data/automation_config.example.json`)
**Wave 2:** Task 6 — depends on Tasks 4 and 5 (imports their modules)
**Wave 3:** Task 7 — depends on all scripts being finalized
