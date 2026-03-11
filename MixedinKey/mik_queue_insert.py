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
    # Use abspath instead of resolve() — resolve() converts mapped drives
    # (e.g. H:\) to UNC paths (\\server\share\), but MIK stores drive letters.
    file_path = os.path.abspath(file_path)
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
