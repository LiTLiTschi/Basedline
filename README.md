# Basedline

Fork of [Baseline](https://github.com/AlexEneas/Baseline) by LiTLiTschi.

> The original README is preserved in [README_BUT_BORING.md](README_BUT_BORING.md).

---

## Windows Mixed In Key MIKStore.db Analysis

**This section is a comprehensive reference so that no future agent or developer needs to re-analyze the database from scratch.**

### Location

```
%LOCALAPPDATA%\Mixed In Key\Mixed In Key\11.0\MIKStore.db
```

Typical expanded path: `C:\Users\<USER>\AppData\Local\Mixed In Key\Mixed In Key\11.0\MIKStore.db`

### Database Engine & Metadata

- **Format**: SQLite 3
- **Schema version**: `11009` (stored in `_Param` table, key `SchemaVersion`)
- **Size**: Can grow very large (~2 GB) primarily due to `Artwork` BLOBs in the `Song` table
- **All primary-keyed tables use `WITHOUT ROWID`** (compact storage, clustered on PK)

---

### Tables Overview

| Table | Row Count (example library) | Purpose |
|---|---|---|
| `Song` | ~123,000 | Core table. One row per audio file ever analyzed. |
| `SongSegment` | ~123,000 (1:1 with Song) | One key-analysis segment per song (always exactly 1 row per song). |
| `SerializedSongStructure` | ~123,000 (1:1 with Song) | JSON blob with detailed energy/key segment breakdowns. |
| `Collection` | ~5 | Library roots and user-created collections/playlists. |
| `SongCollectionMembership` | varies | Many-to-many join between Song and Collection. |
| `Mashup` | 0+ | Mashup pairs created in MIK's mashup feature. |
| `LibraryType` | 4 | Enum: MixedInKey(1), Serato(2), Traktor(3), rekordbox(4). |
| `_Param` | 1 | Key-value config store (currently only `SchemaVersion`). |

---

### Table: `Song` (the main table)

Every audio file that Mixed In Key has ever seen gets a row here. Rows persist even if the file is deleted from disk.

#### Columns

| Column | Type | Description | Example Value |
|---|---|---|---|
| `Id` | TEXT PK | UUID v4 | `000032c8-3018-48a1-8649-b3c9008165f1` |
| `File` | TEXT | Full file path on disk | `H:\music\scdl-mp3\Something.mp3` |
| `FilePathHash` | TEXT | Hash of file path (indexed, used for dedup) | hex string |
| `ArtistName` | TEXT | Artist from file tags | `GAUL` |
| `SongName` | TEXT | Title from file tags | `Something (Funk Tribu Edit)` |
| `Comment` | TEXT | MIK writes analysis results here in format: `{Key} - {BPM} - {Energy}` optionally followed by original comment text | `02A - 155 - 7 - Edit by @funktribu...` |
| `Tempo` | REAL | BPM (high precision float) | `154.99959987962` |
| `OverallVolume` | REAL | Always `0.0` in observed data (deprecated/unused) | `0.0` |
| `OverallEnergy` | INTEGER | Energy level 0-10 (rounded summary) | `7` |
| `EnergySegmentsCount` | INTEGER | Number of energy segments in SerializedSongStructure. Typically 7-8 for analyzed songs. | `8` |
| `StandardPitch` | REAL | Always `0.0` in observed data | `0.0` |
| `KeyResultSummary` | TEXT | Same as MainKey (Camelot notation) | `2A` |
| `DateAdded` | TEXT | ISO datetime when track was added | |
| `ClippedPeaksCount` | INTEGER | Number of detected clipped peaks (0 = clean audio) | `0` or `915` |
| `Artwork` | BLOB | Embedded album art binary (JPEG/PNG). **This is the main contributor to large DB size.** ~60% of songs have artwork. | binary data |
| `LastAnalyzedUtc` | TEXT | ISO UTC datetime of last analysis | |
| `Genre` | TEXT | Genre from file tags | `Trance` |
| `Album` | TEXT | Album from file tags | `EURO/TRANCE` |
| `Grouping` | TEXT | Grouping tag | |
| `Year` | INTEGER | Release year | `2022` |
| `MainKey` | TEXT | Primary detected key in Camelot notation | `2A` |
| `MainKeyConfidence` | REAL | Confidence 0.0-1.0 (avg ~0.827) | `0.459` |
| `SecondKey` | TEXT | Secondary key. **Always `-1A` in practice** (sentinel for "none"). MIK apparently never populates a real second key. | `-1A` |
| `SecondKeyConfidence` | REAL | Always `0.0` (since SecondKey is never set) | `0.0` |
| `IsAnalyzed` | INTEGER | 1 = analyzed, 0 = not yet | `1` |
| `HasPNTag` | INTEGER | Platinum Notes integration flag | `0` |
| `PNTagIsProcessed` | INTEGER | Platinum Notes processed flag | `0` |
| `PNTagAppliedClipRepair` | INTEGER | Platinum Notes clip repair flag | `0` |
| `PNTagVolumeAnalysisVersion` | INTEGER | PN volume analysis version | `0` |
| `PNTagVolumeUnits` | TEXT | PN volume units (usually empty) | `` |
| `PNTagOutputVolume` | REAL | PN output volume (usually `0.0`) | `0.0` |
| `LastModifiedUtc` | TEXT | Last modification timestamp | |
| `OverallVolumeRMS1` | REAL | RMS volume measurement 1 (dB scale, negative) | `-10.51` |
| `OverallVolumeRMS2` | REAL | RMS volume measurement 2 (dB scale, negative) | `-8.61` |
| `OverallVolumeLUFS` | REAL | LUFS loudness measurement (negative) | `-9.31` |
| `DiskIsRemovable` | INTEGER | Whether the source disk was removable (0=fixed) | `0` |
| `DiskLabel` | TEXT | Volume label of source disk | `ierTB` |
| `DiskSerialNumber` | TEXT | Serial number of source disk | `62007FDD` |
| `Label` | TEXT | Record label from tags | |
| `Remixer` | TEXT | Remixer from tags | |
| `Composer` | TEXT | Composer from tags | |
| `FileType` | TEXT | File extension (lowercase usually) | `.mp3`, `.flac`, `.m4a` |
| `FileSize` | BIGINT | File size in bytes | `5743506` |
| `Bitrate` | INTEGER | Bitrate in kbps | `320` |
| `SampleRate` | INTEGER | Sample rate in Hz | `44100` |
| `Rating` | INTEGER | User rating (0 = unrated) | `0` |

#### Indexes

- `IX_Song_FilePathHash` on `FilePathHash` ASC
- `IX_Song_LastAnalyzedUtc` on `LastAnalyzedUtc` ASC

#### Key Observations

- **`Comment` field is overwritten by MIK** with `{Key} - {BPM} - {Energy}` prefix. Original comment text is appended after ` - `. This means MIK clobbers user comments.
- **`SecondKey` is always `-1A`** with confidence `0.0` — MIK 11 never assigns a secondary key in practice despite having the columns.
- **`OverallVolume` and `StandardPitch` are always `0.0`** — likely deprecated in favor of the RMS/LUFS columns.
- **`KeyResultSummary` duplicates `MainKey`** — always the same value.
- **Orphaned rows are common**: songs whose files no longer exist on disk remain in the database indefinitely. The Baseline `mik_prune_missing` tool handles cleanup.
- **Artwork bloat**: ~60% of songs have artwork BLOBs stored. This is the primary reason the DB can reach ~2 GB. Each artwork BLOB can be hundreds of KB to several MB.
- **Platinum Notes (PN) columns**: All `PN*` columns are usually `0`/empty unless Platinum Notes (separate MIK product) has been used.

#### Value Ranges (from ~123k song library)

| Field | Min | Max | Average |
|---|---|---|---|
| `OverallEnergy` | 0 | 10 | 6.4 |
| `Tempo` (BPM) | 0.0 | 186.7 | 120.8 |
| `MainKeyConfidence` | 0.0 | 1.0 | 0.827 |
| `OverallVolumeLUFS` | varies | varies | ~-9 to -18 typical |

#### Key Distribution (Camelot Notation)

Minor keys (A) dominate heavily over major keys (B):

| Key | Count | | Key | Count |
|---|---|---|---|---|
| 8A | 13,181 | | 8B | 1,946 |
| 4A | 12,310 | | 9B | 1,549 |
| 9A | 11,670 | | 12B | 1,385 |
| 6A | 10,697 | | 7B | 1,367 |
| 7A | 9,751 | | 10B | 1,327 |
| 5A | 9,486 | | 11B | 1,141 |
| 2A | 7,879 | | 5B | 1,044 |
| 11A | 7,834 | | 6B | 824 |
| 1A | 7,418 | | 2B | 823 |
| 10A | 6,853 | | 4B | 780 |
| 3A | 6,598 | | 3B | 773 |
| 12A | 5,730 | | 1B | 703 |

Special: `0A` = 7 tracks (likely analysis failures).

#### File Type Distribution

| Type | Count |
|---|---|
| `.mp3` | 103,278 (83.9%) |
| `.flac` | 10,124 (8.2%) |
| `.m4a` | 9,358 (7.6%) |
| `.wav` | 307 |
| `.mp4` | 3 |
| `.aiff`/`.aif` | 6 |

---

### Table: `SongSegment`

One row per song. Despite the table name suggesting multiple segments per song, in practice MIK 11 stores **exactly 1 segment per song** containing the overall key analysis result.

| Column | Type | Description | Example |
|---|---|---|---|
| `SongSegmentId` | TEXT | UUID for this segment | UUID |
| `StartTime` | INTEGER | Start time in **10-nanosecond ticks** (100ns units, .NET TimeSpan ticks) | `0` |
| `EndTime` | INTEGER | End time in ticks. Divide by 10,000,000 to get seconds. | `1097386666` (~109.7 seconds) |
| `KeyConfidence` | REAL | Key detection confidence (same as `Song.MainKeyConfidence`) | `0.992` |
| `Volume` | REAL | Always `0.0` | `0.0` |
| `IsSingleNote` | INTEGER | Whether segment is a single note (always `0`) | `0` |
| `KeyResult` | TEXT | Camelot key (same as `Song.MainKey`) | `10A` |
| `SongId` | TEXT FK | References `Song.Id` | UUID |

**Time unit**: The `StartTime`/`EndTime` values use .NET `TimeSpan.Ticks` (1 tick = 100 nanoseconds = 10^-7 seconds). To convert: `seconds = ticks / 10,000,000`.

---

### Table: `SerializedSongStructure`

One JSON blob per song containing the **detailed** energy and key analysis. This is where the real per-section breakdown lives (the `SongSegment` table is just a summary).

| Column | Type | Description |
|---|---|---|
| `SongId` | TEXT PK/FK | References `Song.Id` |
| `Data` | TEXT | JSON string |

#### JSON Structure

```json
{
  "EnergySegments": [
    {
      "StartTime": "00:00:00.1553305",
      "EndTime": "00:00:24.9295880",
      "Energy": 5
    },
    ...
  ],
  "KeySegments": [
    {
      "Song": null,
      "SongSegmentID": "00000000-0000-0000-0000-000000000000",
      "StartTime": "00:00:00.1553305",
      "EndTime": "00:05:21.8335803",
      "KeyResult": "2A",
      "IsSingleNote": false,
      "KeyConfidence": 0.459,
      "Volume": 0.0
    }
  ],
  "AnalysisEnergySegments": [
    {
      "StartTime": "00:00:00",
      "EndTime": "00:00:12.3874524",
      "Energy": 4
    },
    ...
  ]
}
```

#### JSON Fields Explained

- **`EnergySegments`**: The user-facing energy breakdown (~7-8 segments per song). These correspond to `Song.EnergySegmentsCount`. Energy values 1-10. Time format is `HH:MM:SS.FFFFFFF` (.NET TimeSpan string).
- **`KeySegments`**: Key analysis segments. Usually just 1 segment spanning the whole song (same as `SongSegment` table). The `SongSegmentID` is all-zeros in the JSON but a real UUID in the `SongSegment` table.
- **`AnalysisEnergySegments`**: A more granular energy breakdown (~12-15 segments) used internally by MIK. More detailed than `EnergySegments`. Same energy scale (1-10).

**Time format difference**: The JSON uses .NET `TimeSpan.ToString()` format (`HH:MM:SS.FFFFFFF`), while the `SongSegment` table uses raw ticks (integer).

---

### Table: `Collection`

Represents library roots and user-created collections/playlists.

| Column | Type | Description |
|---|---|---|
| `Id` | TEXT PK | UUID |
| `ExternalId` | TEXT | Usually same as Id for library roots, null for user collections |
| `Name` | TEXT | Display name |
| `Emoji` | TEXT | Optional emoji for the collection |
| `Sequence` | INTEGER | Sort order |
| `LibraryTypeId` | INTEGER FK | References `LibraryType.Id` |
| `IsLibrary` | INTEGER | 1 = root library, 0 = user playlist |
| `IsFolder` | INTEGER | 1 = folder (contains sub-collections), 0 = leaf |
| `ParentFolderId` | TEXT | Parent folder UUID (for nested collections) |

#### Default Library Roots

| Name | LibraryTypeId | Notes |
|---|---|---|
| `MIKRoot` | 1 (MixedInKey) | Main library root |
| `SeratoRoot` | 2 (Serato) | Serato integration root |
| `TraktorRoot` | 3 (Traktor) | Traktor integration root |
| `RekordboxRoot` | 4 (rekordbox) | Rekordbox integration root |

User-created collections have `IsLibrary=0` and reference `LibraryTypeId=1`.

---

### Table: `SongCollectionMembership`

Many-to-many join table between `Song` and `Collection`.

| Column | Type | Description |
|---|---|---|
| `Id` | TEXT PK | UUID |
| `SongId` | TEXT FK | References `Song.Id` (CASCADE delete) |
| `CollectionId` | TEXT FK | References `Collection.Id` (CASCADE delete) |
| `Sequence` | INTEGER | Sort order within collection |

---

### Table: `Mashup`

Stores mashup pairs created in MIK's mashup feature.

| Column | Type | Description |
|---|---|---|
| `Id` | TEXT PK | UUID |
| `Sequence` | INTEGER | Order in mashup list |
| `SongAId` | TEXT FK | First song reference |
| `SongAStemId` | INTEGER | Stem ID for song A (null = full track) |
| `SongAPitchShift` | INTEGER | Semitone pitch shift for song A (default 0) |
| `SongBId` | TEXT FK | Second song reference |
| `SongBStemId` | INTEGER | Stem ID for song B |
| `SongBPitchShift` | INTEGER | Semitone pitch shift for song B |
| `Rating` | INTEGER | User rating of the mashup |
| `AddedUtc` | TEXT | Timestamp (defaults to CURRENT_TIMESTAMP) |

---

### Table: `LibraryType`

Static enum table.

| Id | Name |
|---|---|
| 1 | MixedInKey |
| 2 | Serato |
| 3 | Traktor |
| 4 | rekordbox |

---

### Table: `_Param`

Key-value store for database-level configuration.

| Key | Value |
|---|---|
| `SchemaVersion` | `11009` |

---

### How Baseline (upstream) Uses This Database

The upstream Baseline project has 3 MIK tools:

1. **`mik_prune_missing.py`**: Scans `Song.File` paths, deletes rows where audio file no longer exists on disk. Uses smart path normalization (handles `file://` URIs, percent-encoding, platform slashes).
2. **`mik_sync_tags_from_files.py`**: Reads audio file metadata via Mutagen, updates `Song` columns (ArtistName, SongName, Album, Genre, Tempo, MainKey, Year) if they differ from file tags.
3. **`mik_sync_artwork.py`**: Extracts embedded artwork from audio files, stores as BLOB in `Song.Artwork`. Supports MP3 (APIC), FLAC (PICTURE), MP4 (covr).

All tools: dry-run by default, create timestamped `.backup_*` copies before modification.

---

### Gotchas for Developers

1. **Comment field is destructively managed by MIK**: MIK prepends `{Key} - {BPM} - {Energy}` to the Comment field. Any original comment data comes after. Don't rely on Comment for user data without parsing out the MIK prefix.
2. **File paths may reference non-existent files**: Tracks are never auto-pruned. A library can accumulate tens of thousands of orphaned entries.
3. **Artwork BLOBs cause massive DB size**: Consider `SELECT` without `Artwork` column when you don't need it, or the query will be very slow on large libraries.
4. **WITHOUT ROWID tables**: Most tables use this SQLite optimization. This means no implicit `rowid` column — always use the declared primary key.
5. **UUIDs as text**: All IDs are UUID v4 strings, not integers. Join performance is adequate but slower than integer PKs.
6. **Time units vary**: `SongSegment` uses .NET ticks (100ns units as integers). `SerializedSongStructure` JSON uses `HH:MM:SS.FFFFFFF` strings. Convert ticks: `seconds = ticks / 10_000_000`.
7. **Camelot key notation**: Keys are in Camelot wheel format (`1A`-`12A` for minor, `1B`-`12B` for major). `0A` indicates analysis failure. `-1A` is the sentinel for "no key detected" (used in SecondKey).
8. **CASCADE deletes**: Deleting a `Song` row cascades to `SongSegment`, `SerializedSongStructure`, `SongCollectionMembership`, and `Mashup`. Deleting a `Collection` cascades to `SongCollectionMembership`.

---

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

---
