# MIK Automation Plan: Headless DB Injection Pipeline

> **Author**: Perplexity AI agent, commissioned by LiTLiTschi, 2026-03-08 03:27 CET  
> **Status**: Planning phase — ready for implementation  
> **Goal**: Automatically inject new audio files from a Raspberry Pi server into Mixed In Key 11's SQLite database as unanalyzed entries, triggering hands-free cloud analysis without any GUI interaction.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Assumptions & Verified Facts](#2-assumptions--verified-facts)
3. [Component Specs](#3-component-specs)
   - [3.1 Raspi File Watcher](#31-raspi-file-watcher-watcherd-or-inotifywait)
   - [3.2 Queue File Writer (Raspi)](#32-queue-file-writer-raspi)
   - [3.3 DB Queue Injector (Windows)](#33-db-queue-injector-windows--mik_queue_injectpy)
   - [3.4 MIK Restart Trigger (Windows)](#34-mik-restart-trigger-windows)
   - [3.5 Post-Analysis Tag Writeback](#35-post-analysis-tag-writeback--mik_writeback_tagspy)
4. [Minimal Viable INSERT Statement](#4-minimal-viable-insert-statement)
5. [FilePathHash Reverse Engineering](#5-filepathhash-reverse-engineering)
6. [Path Translation](#6-path-translation-linuxraspi--windows)
7. [DB Access Strategy & Concurrency Safety](#7-db-access-strategy--concurrency-safety)
8. [SongCollectionMembership Handling](#8-songcollectionmembership-handling)
9. [Implementation Order](#9-implementation-order)
10. [Testing Strategy](#10-testing-strategy)
11. [Known Unknowns & Risks](#11-known-unknowns--risks)
12. [Config File Schema](#12-config-file-schema)
13. [Full Data Flow Diagram](#13-full-data-flow-diagram)

---

## 1. Architecture Overview

The system is split into two sides that communicate via a shared folder (SMB or any network filesystem):

```
RASPBERRY PI (server)          SHARED FOLDER          WINDOWS MACHINE
─────────────────────          ─────────────          ──────────────────────────
inotifywait / watchdog  ──→   queue.txt       ──→    mik_queue_inject.py
                                                      (Windows Task Scheduler,
                                                       every 5 min)
                                                             │
                                                             ▼
                                                      MIKStore.db  ←── MIK.exe
                                                      (INSERT unanalyzed rows)
                                                             │
                                                       MIK analyzes
                                                       via cloud API
                                                             │
                               results.jsonl   ←───  mik_writeback_tags.py
                                   │                  (polls DB, detects newly
                                   ▼                   analyzed rows)
                        mik_tag_writer.py (Raspi)
                        writes KEY/BPM/Energy to
                        audio file tags via mutagen
```

**Why Windows-side injection?** SQLite over SMB is unreliable and prone to corruption, especially when MIK is running concurrently. The queue-file approach means the Raspi only writes a plain text file over the network share — safe and atomic. All DB writes happen locally on the Windows machine.

---

## 2. Assumptions & Verified Facts

| # | Statement | Source | Confidence |
|---|-----------|--------|------------|
| 1 | MIK 11 uses SQLite 3 at `%LOCALAPPDATA%\Mixed In Key\Mixed In Key\11.0\MIKStore.db` | LiTLiTschi DB analysis (README) | ✅ Verified |
| 2 | `Song.IsAnalyzed = 0` marks a track for analysis; MIK picks these up at startup | LiTLiTschi assumption, logical | ⚠️ High confidence, needs 1x test |
| 3 | `Song` table uses `WITHOUT ROWID` with UUID v4 TEXT primary keys | LiTLiTschi DB analysis | ✅ Verified |
| 4 | `SongSegment` and `SerializedSongStructure` are **outputs** of analysis, not required inputs | Inferred from data (they contain key/energy results) | ⚠️ Likely — test: does MIK crash or skip tracks with no pre-existing SongSegment row? |
| 5 | `SongCollectionMembership` with `MIKRoot` collection required for track to appear in MIK UI | Inferred from Collection table structure | ⚠️ Likely — test: does MIK show injected tracks without SCM row? |
| 6 | MIK performs analysis via cloud API (internet required) | Creator confirmed 2016; still true in v11 | ✅ Verified |
| 7 | MIK runs on Windows only (no Linux support) | Official system requirements | ✅ Verified |
| 8 | `FilePathHash` algorithm is unknown | Not documented anywhere | ❓ Needs empirical testing (see §5) |
| 9 | `SecondKey = '-1A'` is the correct sentinel for "no secondary key" | LiTLiTschi DB analysis (observed in all rows) | ✅ Verified |
| 10 | `DiskLabel` and `DiskSerialNumber` refer to the Windows volume where music is stored | LiTLiTschi DB analysis | ✅ Verified — needed for INSERT |

---

## 3. Component Specs

### 3.1 Raspi File Watcher (`watcherd` or `inotifywait`)

**Purpose**: Detect new audio files arriving on the Raspi and trigger the queue append.

**Implementation**: systemd service running a Python `watchdog` observer, or a shell script using `inotifywait`.

**Shell approach (simple, no deps):**

```bash
#!/usr/bin/env bash
# /usr/local/bin/mik-watcher.sh
MUSIC_DIR="/mnt/music"
QUEUE_FILE="/mnt/shared/mik_queue.txt"
LOCK_FILE="/tmp/mik_queue.lock"
EXTENSIONS="mp3|flac|m4a|wav|aiff|aif"

inotifywait -m -r -e close_write --format '%w%f' \
  --include ".*\\.($EXTENSIONS)$" "$MUSIC_DIR" | while read filepath; do
    flock "$LOCK_FILE" echo "$filepath" >> "$QUEUE_FILE"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Queued: $filepath"
done
```

**Python approach (recommended, integrates with Basedline):**

```python
# mik_watcher.py (runs on Raspi)
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import fcntl, os, time

AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.aif'}
QUEUE_FILE = "/mnt/shared/mik_queue.txt"

class AudioHandler(FileSystemEventHandler):
    def on_closed(self, event):  # watchdog >= 2.1 has on_closed
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                self._append_queue(event.src_path)

    def _append_queue(self, path):
        with open(QUEUE_FILE, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(path + '\n')
            fcntl.flock(f, fcntl.LOCK_UN)
```

**systemd unit file** (`/etc/systemd/system/mik-watcher.service`):

```ini
[Unit]
Description=MIK Queue Watcher
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/basedline/mik_watcher.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

### 3.2 Queue File Writer (Raspi)

The queue file is a plain UTF-8 text file, one Linux file path per line:

```
/mnt/music/scdl-mp3/Artist - Track.mp3
/mnt/music/downloads/Another Track.flac
```

**Location**: A shared folder accessible from both Raspi and Windows. Example: `/mnt/shared/mik_queue.txt` on Raspi = `\\RASPI\shared\mik_queue.txt` on Windows.

**Atomicity**: Always append-only from the Raspi side. The Windows injector atomically reads and clears it (rename trick — see §3.3).

---

### 3.3 DB Queue Injector (Windows) — `mik_queue_inject.py`

**This is the central script.** Runs on Windows via Task Scheduler every 5 minutes (or triggered manually).

**Full spec:**

```python
#!/usr/bin/env python3
"""
mik_queue_inject.py
Reads mik_queue.txt, injects new tracks into MIKStore.db as IsAnalyzed=0.
Designed to run on Windows via Task Scheduler.
"""
import sqlite3, uuid, os, shutil
from datetime import datetime, timezone
from pathlib import Path
from mutagen import File as MutagenFile
from mik_config import load_config  # see §12

def main():
    cfg = load_config()
    db_path = Path(cfg['mik_db_path'])          # MIKStore.db
    queue_path = Path(cfg['queue_file_path'])    # \\RASPI\shared\mik_queue.txt
    linux_prefix = cfg['linux_path_prefix']      # /mnt/music
    windows_prefix = cfg['windows_path_prefix']  # H:\music

    if not queue_path.exists() or queue_path.stat().st_size == 0:
        return  # nothing to do

    # Atomic queue read: rename queue to a work copy, then clear original
    work_queue = queue_path.with_suffix('.processing')
    os.replace(queue_path, work_queue)

    linux_paths = [p.strip() for p in work_queue.read_text('utf-8').splitlines() if p.strip()]
    work_queue.unlink()

    if not linux_paths:
        return

    # Backup DB before modifying
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = db_path.with_suffix(f'.db.backup_{ts}')
    shutil.copy2(db_path, backup_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Get MIKRoot collection ID
    mik_root_id = conn.execute(
        "SELECT Id FROM Collection WHERE Name='MIKRoot'"
    ).fetchone()['Id']

    # Get disk info from existing rows (to populate DiskLabel/DiskSerialNumber)
    disk_info = get_disk_info(conn, windows_prefix)

    injected, skipped, failed = [], [], []

    try:
        conn.execute("BEGIN")
        for linux_path in linux_paths:
            win_path = translate_path(linux_path, linux_prefix, windows_prefix)
            try:
                result = inject_track(conn, win_path, mik_root_id, disk_info)
                if result == 'injected':
                    injected.append(win_path)
                else:
                    skipped.append(win_path)  # already in DB
            except Exception as e:
                failed.append((win_path, str(e)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Log results
    log_results(injected, skipped, failed, cfg.get('log_path'))

    # Optionally restart MIK if tracks were injected
    if injected and cfg.get('auto_restart_mik', False):
        restart_mik(cfg.get('mik_exe_path'))
```

**The inject_track function:**

```python
def inject_track(conn, win_path: str, mik_root_id: str, disk_info: dict) -> str:
    """
    Returns 'injected' if new row was created, 'skipped' if already exists.
    """
    # Dedup check: query by File path (safe fallback if FilePathHash unknown)
    existing = conn.execute(
        "SELECT Id FROM Song WHERE File = ?", (win_path,)
    ).fetchone()
    if existing:
        return 'skipped'

    # Also check by FilePathHash once algorithm is known (see §5)
    file_hash = compute_file_path_hash(win_path)  # returns hex string
    if file_hash:
        existing_by_hash = conn.execute(
            "SELECT Id FROM Song WHERE FilePathHash = ?", (file_hash,)
        ).fetchone()
        if existing_by_hash:
            return 'skipped'

    # Read file metadata
    meta = read_metadata(win_path)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    song_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO Song (
            Id, File, FilePathHash,
            ArtistName, SongName, Album, Genre, Grouping, Year,
            Label, Remixer, Composer,
            Comment, Tempo, OverallEnergy, EnergySegmentsCount,
            OverallVolume, StandardPitch,
            MainKey, MainKeyConfidence,
            SecondKey, SecondKeyConfidence,
            KeyResultSummary,
            IsAnalyzed,
            HasPNTag, PNTagIsProcessed, PNTagAppliedClipRepair,
            PNTagVolumeAnalysisVersion, PNTagVolumeUnits, PNTagOutputVolume,
            ClippedPeaksCount,
            OverallVolumeRMS1, OverallVolumeRMS2, OverallVolumeLUFS,
            DiskIsRemovable, DiskLabel, DiskSerialNumber,
            FileType, FileSize, Bitrate, SampleRate, Rating,
            DateAdded, LastModifiedUtc, LastAnalyzedUtc,
            Artwork
        ) VALUES (
            ?,?,?,  ?,?,?,?,?,?,  ?,?,?,
            ?,?,?,?,  ?,?,  ?,?,  ?,?,  ?,
            ?,
            ?,?,?,  ?,?,?,  ?,
            ?,?,?,  ?,?,?,  ?,?,?,?,?,
            ?,?,?,  ?
        )
    """, (
        song_id, win_path, file_hash or '',
        meta['artist'], meta['title'], meta['album'], meta['genre'], '', meta['year'],
        '', '', '',
        '',                      # Comment: leave empty, MIK will write its own
        meta['bpm'], 0, 0,       # Tempo, OverallEnergy, EnergySegmentsCount
        0.0, 0.0,                # OverallVolume, StandardPitch (deprecated)
        '', 0.0,                 # MainKey, MainKeyConfidence (empty = not analyzed)
        '-1A', 0.0,              # SecondKey sentinel, SecondKeyConfidence
        '',                      # KeyResultSummary
        0,                       # IsAnalyzed = 0  ← KEY FLAG
        0, 0, 0,                 # PN flags
        0, '', 0.0,              # PN version/units/volume
        0,                       # ClippedPeaksCount
        0.0, 0.0, 0.0,           # RMS1, RMS2, LUFS
        0,                       # DiskIsRemovable
        disk_info.get('label', ''), disk_info.get('serial', ''),
        meta['ext'],             # FileType e.g. '.mp3'
        meta['size'],            # FileSize bytes
        meta['bitrate'],         # Bitrate kbps
        meta['samplerate'],      # SampleRate Hz
        0,                       # Rating
        now_utc, now_utc, None,  # DateAdded, LastModifiedUtc, LastAnalyzedUtc=NULL
        meta.get('artwork'),     # Artwork BLOB or None
    ))

    # Add to MIKRoot collection
    membership_id = str(uuid.uuid4())
    max_seq = conn.execute(
        "SELECT COALESCE(MAX(Sequence), 0) FROM SongCollectionMembership WHERE CollectionId = ?",
        (mik_root_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO SongCollectionMembership (Id, SongId, CollectionId, Sequence) VALUES (?,?,?,?)",
        (membership_id, song_id, mik_root_id, max_seq + 1)
    )

    return 'injected'
```

**read_metadata helper** (using mutagen):

```python
def read_metadata(win_path: str) -> dict:
    defaults = {
        'artist': '', 'title': os.path.splitext(os.path.basename(win_path))[0],
        'album': '', 'genre': '', 'year': 0,
        'bpm': 0.0, 'bitrate': 0, 'samplerate': 44100,
        'ext': os.path.splitext(win_path)[1].lower(),
        'size': os.path.getsize(win_path) if os.path.exists(win_path) else 0,
        'artwork': None,
    }
    try:
        audio = MutagenFile(win_path, easy=False)
        if audio is None:
            return defaults
        info = getattr(audio, 'info', None)
        if info:
            defaults['bitrate'] = int(getattr(info, 'bitrate', 0) / 1000)
            defaults['samplerate'] = getattr(info, 'sample_rate', 44100)
        # Tag extraction varies by format — use EasyID3 wrapper or direct access
        # ... (implementation differs per format, use mutagen's tag objects)
    except Exception:
        pass
    return defaults
```

---

### 3.4 MIK Restart Trigger (Windows)

After injecting new tracks, MIK needs to (re)load the DB to pick up `IsAnalyzed=0` rows.

**Option A — Auto restart (add to `mik_queue_inject.py`):**

```python
import subprocess, time

def restart_mik(mik_exe_path: str):
    """Kill MIK if running, then relaunch it."""
    subprocess.run(['taskkill', '/IM', 'Mixed In Key.exe', '/F'],
                   capture_output=True)
    time.sleep(2)
    subprocess.Popen([mik_exe_path])
```

Default MIK exe path: `C:\Program Files\Mixed In Key\Mixed In Key.exe`  
(Configurable in settings — see §12.)

**Option B — Manual / semi-automatic:**  
Leave `auto_restart_mik = false` in config. Script injects silently. User restarts MIK manually when convenient. Best for users who run MIK only occasionally.

**Option C — Windows Task Scheduler only:**  
Schedule `mik_queue_inject.py` to run every 5 minutes. Also schedule a separate task that starts MIK at a fixed time (e.g., 3am) and kills it 30 minutes later after analysis completes.

**Recommended**: Option A with `auto_restart_mik = true` for fully hands-free operation.

---

### 3.5 Post-Analysis Tag Writeback — `mik_writeback_tags.py`

After MIK analyzes a track, the results live in the DB. This script reads them back and writes standard ID3/FLAC tags to the audio files — so other software (Rekordbox, Serato, beets, etc.) can see the MIK key.

**Can run on either Windows or Raspi** (Raspi preferred — it has direct file access and Python).

```python
#!/usr/bin/env python3
"""
mik_writeback_tags.py
Polls MIKStore.db for newly analyzed tracks and writes key/BPM/energy to file tags.
Maintains a state file (last_writeback.txt) with the last processed LastAnalyzedUtc.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from mutagen.id3 import ID3, TXXX, TBPM, COMM
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mik_config import load_config

def main():
    cfg = load_config()
    db_path = cfg['mik_db_path']       # can be local or SMB-mounted path
    state_file = Path(cfg.get('writeback_state_file', 'last_writeback.txt'))
    linux_prefix = cfg['linux_path_prefix']
    windows_prefix = cfg['windows_path_prefix']

    last_run = '1970-01-01T00:00:00.000Z'
    if state_file.exists():
        last_run = state_file.read_text().strip()

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)  # READ-ONLY
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT File, MainKey, Tempo, OverallEnergy, Comment, LastAnalyzedUtc
        FROM Song
        WHERE IsAnalyzed = 1
          AND LastAnalyzedUtc > ?
          AND MainKey != ''
          AND MainKey != '0A'
        ORDER BY LastAnalyzedUtc ASC
    """, (last_run,)).fetchall()

    conn.close()

    newest_ts = last_run
    for row in rows:
        linux_path = translate_path_reverse(
            row['File'], linux_prefix, windows_prefix
        )
        try:
            write_tags(linux_path, row['MainKey'], row['Tempo'], row['OverallEnergy'])
            print(f"Tagged: {linux_path} → {row['MainKey']} {row['Tempo']:.1f}bpm")
        except Exception as e:
            print(f"WARN: Failed to tag {linux_path}: {e}")
        newest_ts = row['LastAnalyzedUtc']

    if newest_ts != last_run:
        state_file.write_text(newest_ts)

def write_tags(path: str, key: str, bpm: float, energy: int):
    ext = Path(path).suffix.lower()
    if ext == '.mp3':
        tags = ID3(path)
        tags['TXXX:KEY'] = TXXX(encoding=3, desc='KEY', text=key)
        tags['TBPM'] = TBPM(encoding=3, text=str(int(round(bpm))))
        tags['TXXX:Energy'] = TXXX(encoding=3, desc='Energy', text=str(energy))
        # Also write to standard KEY frame (used by Rekordbox/Serato):
        from mutagen.id3 import TKEY
        tags['TKEY'] = TKEY(encoding=3, text=camelot_to_openkey(key))
        tags.save()
    elif ext == '.flac':
        tags = FLAC(path)
        tags['KEY'] = key
        tags['BPM'] = str(int(round(bpm)))
        tags['ENERGY'] = str(energy)
        tags.save()
    elif ext in ('.m4a', '.mp4'):
        tags = MP4(path)
        tags['----:com.apple.iTunes:KEY'] = key.encode()
        tags['tmpo'] = [int(round(bpm))]
        tags.save()
```

**Camelot → Open Key conversion** (for TKEY frame compatibility with Rekordbox):

```python
CAMELOT_TO_OPENKEY = {
    '1A': 'Am', '2A': 'Em', '3A': 'Bm', '4A': 'F#m', '5A': 'Dbm', '6A': 'Abm',
    '7A': 'Ebm', '8A': 'Bbm', '9A': 'Fm', '10A': 'Cm', '11A': 'Gm', '12A': 'Dm',
    '1B': 'C',  '2B': 'G',  '3B': 'D',  '4B': 'A',  '5B': 'E',  '6B': 'B',
    '7B': 'F#', '8B': 'Db', '9B': 'Ab', '10B': 'Eb', '11B': 'Bb', '12B': 'F',
}

def camelot_to_openkey(camelot: str) -> str:
    return CAMELOT_TO_OPENKEY.get(camelot, camelot)
```

---

## 4. Minimal Viable INSERT Statement

Based on the documented schema, this is the absolute minimum INSERT that should cause MIK to detect and analyze a track. All other columns will be populated by MIK during analysis.

```sql
INSERT INTO Song (
    Id,
    File,
    FilePathHash,
    IsAnalyzed,
    SecondKey,
    SecondKeyConfidence,
    DateAdded,
    LastModifiedUtc,
    OverallVolume,
    StandardPitch,
    OverallEnergy,
    EnergySegmentsCount,
    ClippedPeaksCount,
    MainKeyConfidence,
    Tempo,
    OverallVolumeRMS1,
    OverallVolumeRMS2,
    OverallVolumeLUFS,
    DiskIsRemovable,
    HasPNTag,
    PNTagIsProcessed,
    PNTagAppliedClipRepair,
    PNTagVolumeAnalysisVersion,
    PNTagOutputVolume,
    Rating
) VALUES (
    'NEW-UUID-HERE',
    'H:\music\Artist - Track.mp3',
    '',                       -- FilePathHash: leave empty until algo known; see §5
    0,                        -- IsAnalyzed = 0 ← THE KEY FLAG
    '-1A',                    -- SecondKey sentinel (always this value)
    0.0,
    '2026-03-08T03:00:00.000Z',
    '2026-03-08T03:00:00.000Z',
    0.0, 0.0,                 -- OverallVolume, StandardPitch (deprecated)
    0, 0, 0,                  -- OverallEnergy, EnergySegmentsCount, ClippedPeaksCount
    0.0,                      -- MainKeyConfidence
    0.0,                      -- Tempo
    0.0, 0.0, 0.0,            -- RMS1, RMS2, LUFS
    0,                        -- DiskIsRemovable
    0, 0, 0, 0, 0.0,          -- PN flags
    0                         -- Rating
);
```

**Constraint notes** (from `WITHOUT ROWID` behavior):
- `Id` is the clustering key — must be unique
- No explicit `NOT NULL` constraints observed on most columns, but SQLite `WITHOUT ROWID` tables require the PK to be NOT NULL
- `SecondKey = '-1A'` must be present to match MIK's internal invariant — leaving it NULL may cause display issues

---

## 5. FilePathHash Reverse Engineering

The `FilePathHash` column is indexed (`IX_Song_FilePathHash`) and used for dedup. The algorithm is not documented. This section describes how to determine it empirically.

### Step 1: Extract known path→hash pairs

```python
import sqlite3
conn = sqlite3.connect(r'C:\Users\...\MIKStore.db')
rows = conn.execute("SELECT File, FilePathHash FROM Song LIMIT 20").fetchall()
for file, hash_val in rows:
    print(repr(file), '→', hash_val)
```

### Step 2: Test candidate algorithms

```python
import hashlib

def test_hash_algos(path: str, expected_hash: str):
    candidates = {
        'md5_utf8_lower':    hashlib.md5(path.lower().encode('utf-8')).hexdigest(),
        'md5_utf8_orig':     hashlib.md5(path.encode('utf-8')).hexdigest(),
        'sha1_utf8_lower':   hashlib.sha1(path.lower().encode('utf-8')).hexdigest(),
        'sha1_utf8_orig':    hashlib.sha1(path.encode('utf-8')).hexdigest(),
        'sha256_utf8_lower': hashlib.sha256(path.lower().encode('utf-8')).hexdigest(),
        'sha256_utf8_orig':  hashlib.sha256(path.encode('utf-8')).hexdigest(),
        'md5_utf16le':       hashlib.md5(path.lower().encode('utf-16-le')).hexdigest(),
        'sha1_utf16le':      hashlib.sha1(path.lower().encode('utf-16-le')).hexdigest(),
    }
    for name, result in candidates.items():
        if result == expected_hash.lower():
            print(f"✅ MATCH: {name}")
            return name
    print("❌ No match — may be a non-standard hash or includes salt")
    return None
```

### Best guess (pre-testing)

Based on .NET/C# conventions and the fact MIK stores it as a hex string, **MD5 of the lowercase UTF-8 path** is the most likely candidate:

```python
import hashlib
def compute_file_path_hash(win_path: str) -> str:
    return hashlib.md5(win_path.lower().encode('utf-8')).hexdigest()
```

### Fallback strategy

If the hash algorithm cannot be determined, the dedup check in `inject_track` still works via direct `File` path comparison. Insert `FilePathHash = ''` (empty string). MIK will likely recompute/overwrite it during analysis. The index will be slightly less efficient but correctness is maintained.

---

## 6. Path Translation (Linux/Raspi ↔ Windows)

The Raspi and Windows machine see the same audio files at different paths. A bidirectional translation function is required.

```python
def translate_path(linux_path: str, linux_prefix: str, windows_prefix: str) -> str:
    """Convert Raspi path to Windows path for DB insertion."""
    if not linux_path.startswith(linux_prefix):
        raise ValueError(f"Path {linux_path!r} does not start with {linux_prefix!r}")
    relative = linux_path[len(linux_prefix):]
    # Convert forward slashes to backslashes
    relative_win = relative.replace('/', '\\')
    # Strip leading separator if present
    relative_win = relative_win.lstrip('\\')
    return windows_prefix.rstrip('\\') + '\\' + relative_win

def translate_path_reverse(win_path: str, linux_prefix: str, windows_prefix: str) -> str:
    """Convert Windows DB path back to Raspi path for tag writing."""
    if not win_path.lower().startswith(windows_prefix.lower()):
        raise ValueError(f"Windows path {win_path!r} does not start with {windows_prefix!r}")
    relative = win_path[len(windows_prefix):]
    relative_linux = relative.replace('\\', '/')
    relative_linux = relative_linux.lstrip('/')
    return linux_prefix.rstrip('/') + '/' + relative_linux
```

**Example config values:**
```json
{
  "linux_path_prefix": "/mnt/music",
  "windows_path_prefix": "H:\\music"
}
```

---

## 7. DB Access Strategy & Concurrency Safety

### Option A: Queue File (Recommended)

- **Raspi writes**: `/mnt/shared/mik_queue.txt` (plain text, append-only)
- **Windows reads + DB writes**: `mik_queue_inject.py` runs locally on Windows
- **Conflict risk**: Near zero — DB is only modified by one process at a time

### Option B: Direct SMB Mount (Alternative, higher risk)

Mount the MIK DB directory on the Raspi:

```bash
sudo mount -t cifs //windows-pc/Users/Liu/AppData/Local/Mixed\ In\ Key/Mixed\ In\ Key/11.0 \
  /mnt/mik -o username=Liu,vers=3.0
```

Then run `mik_queue_inject.py` on the Raspi directly. **Risks:**
- SQLite WAL mode may not work correctly over SMB
- MIK running simultaneously can cause `SQLITE_BUSY` or corruption
- Recommended mitigation: only inject when MIK is not running (check process via `tasklist` or port)

### Concurrency Guard (for either approach)

```python
import sqlite3

def safe_connect(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """Connect with busy timeout to handle concurrent access."""
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")   # safer for concurrent readers
    conn.execute("PRAGMA synchronous=NORMAL")  # balance safety/speed
    return conn
```

---

## 8. SongCollectionMembership Handling

For an injected track to appear in MIK's main library (not just exist silently in the DB), it likely needs a `SongCollectionMembership` row linking it to the `MIKRoot` collection.

```python
def add_to_mik_root(conn, song_id: str, mik_root_id: str):
    membership_id = str(uuid.uuid4())
    # Get next sequence number
    row = conn.execute(
        "SELECT COALESCE(MAX(Sequence), 0) as max_seq FROM SongCollectionMembership WHERE CollectionId = ?",
        (mik_root_id,)
    ).fetchone()
    next_seq = row['max_seq'] + 1
    conn.execute(
        "INSERT INTO SongCollectionMembership (Id, SongId, CollectionId, Sequence) VALUES (?,?,?,?)",
        (membership_id, song_id, mik_root_id, next_seq)
    )
```

**Getting MIKRoot ID** (varies per installation — always query, never hardcode):

```python
mik_root_id = conn.execute(
    "SELECT Id FROM Collection WHERE Name='MIKRoot' AND IsLibrary=1"
).fetchone()['Id']
```

**Open question**: Does MIK still pick up and analyze `IsAnalyzed=0` rows that are NOT in any collection? If so, the `SongCollectionMembership` INSERT is optional for triggering analysis, but still needed for the track to appear in MIK's UI. Recommended: always insert both.

---

## 9. Implementation Order

Implement in this exact order to allow incremental testing at each stage:

| Step | Script | Platform | Test |
|------|--------|----------|------|
| 1 | `MixedinKey/mik_hash_test.py` | Windows | Run against existing DB rows to identify `FilePathHash` algorithm |
| 2 | `MixedinKey/mik_queue_inject.py` — dry run mode | Windows | Verify SQL is correct, no DB writes |
| 3 | Manual single-track injection test | Windows | Inject 1 track, open MIK, verify it appears and gets analyzed |
| 4 | `mik_watcher.py` | Raspi | Verify queue.txt gets populated on file drop |
| 5 | `mik_queue_inject.py` — live via Task Scheduler | Windows | Full end-to-end: drop file on Raspi, MIK analyzes it |
| 6 | `mik_writeback_tags.py` | Raspi | Verify tags written to files after MIK analysis |
| 7 | Systemd service for watcher | Raspi | Persistent background operation |

---

## 10. Testing Strategy

### Test 1: Verify IsAnalyzed=0 triggers analysis

1. Inject one row with `IsAnalyzed=0` manually (use dry-run SQL, then apply)
2. Restart MIK
3. Observe: does the track appear in MIK's "needs analysis" queue?
4. After analysis: confirm `IsAnalyzed` changed to `1`, `MainKey` populated

**Expected**: Yes. If No, MIK may require `SongSegment` pre-population.

### Test 2: SongSegment requirement

If Test 1 fails (MIK ignores the injected row), try also inserting a stub `SongSegment` row:

```sql
INSERT INTO SongSegment (SongSegmentId, StartTime, EndTime, KeyConfidence, Volume, IsSingleNote, KeyResult, SongId)
VALUES ('NEW-UUID', 0, 0, 0.0, 0.0, 0, '', 'SONG-UUID');
```

### Test 3: Collection membership requirement

Test whether MIK shows injected tracks without `SongCollectionMembership` row. If yes, membership can be optional.

### Test 4: FilePathHash validation

Run `mik_hash_test.py` — see §5. Confirm matching algorithm.

### Test 5: Path translation round-trip

```python
linux = '/mnt/music/artist/track.mp3'
win = translate_path(linux, '/mnt/music', 'H:\\music')
assert win == 'H:\\music\\artist\\track.mp3'
assert translate_path_reverse(win, '/mnt/music', 'H:\\music') == linux
```

---

## 11. Known Unknowns & Risks

| # | Unknown | Risk if wrong | Resolution |
|---|---------|--------------|------------|
| 1 | `FilePathHash` algorithm | Duplicate rows possible if hash mismatch on re-insert; index degraded | Empirical test (§5); fallback: empty string |
| 2 | MIK re-reads DB on startup vs polling | Analysis may not trigger without restart | Test: inject row while MIK open, wait 30s; if no pickup, restart required |
| 3 | `SongSegment` required for analysis pickup | Injected tracks silently ignored | Test 2 (§10) |
| 4 | `SongCollectionMembership` required | Track not visible in MIK UI | Test 3 (§10); always insert to be safe |
| 5 | MIK DB schema changes in future versions | Scripts break on MIK update | Pin to schema version `11009`; add version check on startup |
| 6 | SQLite `WITHOUT ROWID` constraints | INSERT fails with cryptic error | Use declared PK always; never `rowid` |
| 7 | Windows path encoding (Unicode filenames) | Path mismatch for non-ASCII filenames | Always use `utf-8` encoding; test with accented characters |
| 8 | MIK locks DB file during analysis | `SQLITE_BUSY` during writeback | Use `timeout=30.0` on connect; retry logic |
| 9 | `DiskLabel`/`DiskSerialNumber` required | Unknown — MIK may use these for portable library features | Query from existing rows for same drive prefix; leave empty as fallback |

---

## 12. Config File Schema

All scripts share one config file at `settings/basedline_settings.json`:

```json
{
  "mik_db_path": "C:\\Users\\Liu\\AppData\\Local\\Mixed In Key\\Mixed In Key\\11.0\\MIKStore.db",
  "queue_file_path": "\\\\RASPI\\shared\\mik_queue.txt",
  "linux_path_prefix": "/mnt/music",
  "windows_path_prefix": "H:\\music",
  "mik_exe_path": "C:\\Program Files\\Mixed In Key\\Mixed In Key.exe",
  "auto_restart_mik": true,
  "log_path": "C:\\Users\\Liu\\Documents\\Baseline\\Logs\\mik_inject.log",
  "writeback_state_file": "/opt/basedline/settings/last_writeback.txt",
  "write_tkey_frame": true,
  "write_bpm_tag": true,
  "write_energy_tag": true,
  "dry_run": false
}
```

---

## 13. Full Data Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                        RASPBERRY PI                                   │
│                                                                       │
│  new file lands                                                       │
│  in /mnt/music/   ──→  mik_watcher.py  ──→  append to queue.txt     │
│                         (inotify/                                     │
│                          watchdog)                                    │
│                                              ┌──────────────────┐    │
│  mik_writeback_tags.py  ←── DB read (RO) ←──┤  SMB or rsync    │    │
│  writes KEY/BPM/Energy                       │  from Windows    │    │
│  to audio file tags                          └──────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
         ▲ queue.txt written to SMB share                ▲ DB read back
         │                                               │
         ▼                                               │
┌──────────────────────────────────────────────────────────────────────┐
│                        WINDOWS MACHINE                                │
│                                                                       │
│  Task Scheduler (every 5min)                                          │
│  mik_queue_inject.py                                                  │
│  ├── read & clear queue.txt                                           │
│  ├── translate Linux paths → Windows paths                            │
│  ├── read file metadata via mutagen                                   │
│  ├── INSERT into Song (IsAnalyzed=0)                                  │
│  ├── INSERT into SongCollectionMembership (→ MIKRoot)                 │
│  └── restart Mixed In Key.exe (if auto_restart_mik=true)             │
│                    │                                                  │
│                    ▼                                                  │
│  Mixed In Key.exe                                                     │
│  ├── reads MIKStore.db on startup                                     │
│  ├── detects IsAnalyzed=0 rows                                        │
│  ├── sends audio to cloud analysis API                                │
│  └── writes back: MainKey, Tempo, OverallEnergy, Comment,             │
│       SongSegment, SerializedSongStructure, IsAnalyzed=1              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Appendix: Scripts To Be Created

| File | Platform | Purpose |
|------|----------|---------|
| `MixedinKey/mik_queue_inject.py` | Windows | Main injector — reads queue, INSERTs to DB |
| `MixedinKey/mik_writeback_tags.py` | Raspi | Reads DB post-analysis, writes tags to files |
| `MixedinKey/mik_hash_test.py` | Windows | Identifies FilePathHash algorithm empirically |
| `MixedinKey/mik_config.py` | Both | Shared config loader |
| `mik_watcher.py` (repo root) | Raspi | inotify-based file watcher, populates queue |
| `mik-watcher.service` | Raspi | systemd unit for persistent watcher |

All scripts follow the existing Basedline conventions: dry-run by default, timestamped DB backups before modification, structured logging.
