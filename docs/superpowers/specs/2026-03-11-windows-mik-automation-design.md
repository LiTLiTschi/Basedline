# Windows MIK Automation — Design Spec

## Context

Basedline is a fork of [Baseline](https://github.com/AlexEneas/Baseline). The fork added a Raspberry Pi / Wine automation pipeline for headless Mixed In Key analysis. That approach proved infeasible. This spec replaces it with a Windows-native automation pipeline.

## Goal

Automatically analyze new audio files with Mixed In Key 11 on Windows. Files arrive on a network share (SMB from Raspi, mounted as `H:\`). MIK runs natively on Windows — no Wine, no Xvfb.

MIK has "Write to file tags" enabled, so after analysis the Key/BPM/Energy tags are written back into the audio files automatically by MIK itself.

## Requirements

- Python 3.10+ (Windows)
- Dependencies: `watchdog`, `mutagen` (add to `requirements.txt`)
- No `psutil` required — uses `tasklist`/`taskkill` (available on all Windows)

## Data Flow

```
New file appears on H:\ (downloaded by separate project on Raspi)
      |
mik_watcher.py (watchdog, debounced, with file-stability check)
      |
mik_queue_insert.py (INSERT into MIKStore.db with IsAnalyzed=0)
      |
mik_launcher.py (restart MIK.exe, poll until analysis done)
      |
MIK 11 (analyzes tracks, writes Key/BPM/Energy tags to files)
```

## Repo Cleanup (removing Wine/Raspi code)

### Delete
- `MixedinKey/mik_process_manager.py` — Wine/Xvfb process lifecycle
- `MixedinKey/mik_watcher_daemon.py` — Linux inotify watcher
- `MixedinKey/MIK_AUTOMATION_PLAN.md` — 57KB Raspi/Wine plan
- `data/automation_config.example.json` — Wine-specific config

### Rewrite
- `MixedinKey/mik_queue_insert.py` — strip Wine path translation, use native Windows paths

### Keep as-is
- `MixedinKey/mik_identify_hash.py` — still needed to verify FilePathHash algorithm
- `MixedinKey/mik_prune_missing.py` — upstream, platform-agnostic
- `MixedinKey/mik_sync_tags_from_files.py` — upstream, platform-agnostic
- `MixedinKey/mik_sync_artwork.py` — upstream, platform-agnostic
- All non-MIK tools (Discogs/, Rekordbox/, Filename/)
- `app.py`, `music_suite.py` — untouched

### Update
- `README.md` — replace Raspi/Wine automation section with Windows automation docs

## Script 1: `mik_queue_insert.py` (rewrite)

Single responsibility: insert audio files into MIKStore.db as unanalyzed tracks.

### Interface
```
python mik_queue_insert.py [--dry-run] [--no-backup] [--config PATH] FILE [FILE ...]
python mik_queue_insert.py [--dry-run] [--no-backup] [--config PATH] --batch FILE_LIST
```

### Behavior
- Reads config from `data/automation_config.json`
- For each file:
  - Reads metadata via Mutagen (artist, title, album, genre, year, BPM, bitrate, sample rate, file size, file type)
  - Computes FilePathHash using configured algorithm (default SHA256) with UTF-8 encoding of the absolute file path
  - Checks if already in DB by FilePathHash (skip if exists)
  - Inserts `Song` row with:
    - `Id`: new UUID v4
    - `File`: absolute Windows path
    - `FilePathHash`: computed hash
    - All metadata fields from Mutagen
    - `IsAnalyzed=0`, `MainKey=NULL`, `SecondKey='-1A'`, `OverallEnergy=0`
    - `DiskIsRemovable`: from config (default `0`)
    - `DiskLabel`: from config (default empty string)
    - `DiskSerialNumber`: from config (default empty string)
    - `DateAdded`: current ISO datetime
    - `LastModifiedUtc`: current ISO UTC datetime
  - Inserts `SongCollectionMembership` row linking to MIKRoot collection
  - Does NOT insert `SongSegment` or `SerializedSongStructure` rows — MIK creates these during analysis. The existing code also does not insert these, and MIK handles them.
- `--dry-run`: logs what would be inserted, no DB writes
- `--no-backup`: skip the timestamped backup (useful when called programmatically by the watcher)
- Creates timestamped `.backup_*` copy of DB before first write (unless `--no-backup`)
- Returns count of files actually inserted (for callers to know if MIK restart is needed)

### Logging
Uses Python `logging` module. When run standalone, logs to stderr. When imported, the caller controls log configuration.

### Key difference from current version
- No `linux_to_wine_path()` — file paths used as-is (they're already Windows paths on `H:\`)
- No Wine path encoding logic
- Simpler config (no `path_map`, `wine_prefix`, `wine_display`)

### Importable API
```python
from MixedinKey.mik_queue_insert import queue_file, load_config
# queue_file(filepath, config, dry_run=False) -> bool  (True if inserted, False if skipped)
```

## Script 2: `mik_watcher.py` (new, replaces mik_watcher_daemon.py)

Single responsibility: watch a directory for new audio files and trigger the injection + launch pipeline.

### Interface
```
python mik_watcher.py [--config PATH]
```

### Behavior
- Uses `watchdog` library (uses ReadDirectoryChangesW on Windows — efficient, native)
- Watches `watch_dir` from config for new/moved audio files (recursive)
- Filters by `audio_extensions` from config
- **File stability check**: before processing a file, waits until file size stops changing for 2 seconds (handles large files still being copied over SMB)
- Debounces: collects files for `debounce_seconds`, then processes as batch
- For each batch:
  1. Calls `mik_queue_insert.queue_file()` for each file (with `no_backup=True` after first batch)
  2. If any files were actually inserted (not skipped as dupes), calls `mik_launcher.restart_mik()`
- **Network share resilience**: if watchdog raises an error (share disconnected), logs warning and retries connection every 30 seconds until the share is available again
- Runs indefinitely (designed to be launched at Windows startup)

### Logging
Uses Python `logging` module. Logs to both stderr and a rotating log file at `data/mik_watcher.log` (configurable via `log_file` in config, optional).

### No daemon/service framework
Just a long-running Python script. User can add it to Windows Task Scheduler "at logon" or create a shortcut in Startup folder.

## Script 3: `mik_launcher.py` (new, replaces mik_process_manager.py)

Single responsibility: manage the MIK.exe process lifecycle on Windows.

### Interface
```
python mik_launcher.py [--config PATH] {restart|start|stop|status|wait}
```

### Importable API
```python
from MixedinKey.mik_launcher import restart_mik, start_mik, stop_mik, is_mik_running, count_pending, wait_for_completion
# restart_mik(config) -> None
# wait_for_completion(config) -> bool  (True if queue drained, False if timeout)
```

### Behavior
- `is_mik_running()` — checks via `tasklist` for `Mixed In Key.exe` (parses stdout, no `psutil` needed)
- `stop_mik()` — kills MIK process via `taskkill /f /im "Mixed In Key.exe"`, waits up to 5 seconds for process to exit
- `start_mik()` — launches MIK exe via `subprocess.Popen()` (detached, no wait), then sleeps `mik_startup_wait_seconds` to let MIK initialize
- `restart_mik()` — stop (if running) → start
- `count_pending()` — queries `SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 0`
- `wait_for_completion(config)` — polls `count_pending()` every `poll_interval_seconds` until count reaches 0 or `analysis_timeout_minutes` is exceeded. Returns True if queue drained, False on timeout.
- `status` — prints running/not running + pending track count
- `wait` — calls `wait_for_completion()`, prints progress, exits with code 0 (done) or 1 (timeout)

When `close_mik_when_done` is True: after `wait_for_completion()` returns True, calls `stop_mik()` to close MIK.

### No Xvfb, no Wine, no POSIX signals
Pure Windows process management using `tasklist`/`taskkill` and `subprocess`.

### Logging
Uses Python `logging` module, same pattern as other scripts.

## Config: `data/automation_config.example.json`

```json
{
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

## README Update

Remove the "MIK Automation Plan (Raspberry Pi / Wine / Headless)" section (lines 340-403 approx). Replace with:

### Windows MIK Automation

- Brief description of the 3 scripts and their roles
- Config setup instructions (copy example, fill in `db_path`, `watch_dir`, `mik_exe_path`)
- Quick start: verify hash algo → dry-run insert → live insert → run watcher
- Note about adding to Windows startup (Task Scheduler or Startup folder)
- Note: `watch_dir` is intentionally renamed from the old `music_dir` config key

Keep the MIKStore.db schema analysis section — it's valuable reference regardless of platform.

## Subagent Development Strategy

These scripts are designed for parallel subagent development:

| Task | Dependencies | Can parallelize with |
|---|---|---|
| Delete Wine/Raspi files | None | Everything |
| Rewrite `mik_queue_insert.py` | None (standalone) | `mik_launcher.py`, cleanup |
| Write `mik_launcher.py` | None (standalone) | `mik_queue_insert.py`, cleanup |
| Write new config template | None | Everything |
| Write `mik_watcher.py` | Needs interfaces from queue_insert + launcher | Must come after both |
| Update README.md | After all scripts done | None |

Maximum parallelism: cleanup + queue_insert + launcher + config can all run simultaneously (4 parallel tasks). Watcher depends on the other two scripts' importable APIs. README comes last.
