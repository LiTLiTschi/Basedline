#!/usr/bin/env python3
"""
mik_diagnose.py — Diagnose why MIK isn't picking up inserted tracks.

Run this on Windows after attempting mik_queue_insert.py to see exactly
what happened and what MIK sees.

Usage:
    python MixedinKey/mik_diagnose.py --config data/automation_config.json
    python MixedinKey/mik_diagnose.py  # uses default config path
"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "data" / "automation_config.json"

CANDIDATES = [
    ("sha256",     lambda b: hashlib.sha256(b).hexdigest()),
    ("md5",        lambda b: hashlib.md5(b).hexdigest()),
    ("sha1",       lambda b: hashlib.sha1(b).hexdigest()),
    ("sha512",     lambda b: hashlib.sha512(b).hexdigest()),
]
ENCODINGS = ["utf-8", "utf-16-le", "utf-16-be", "latin-1"]

SEP = "-" * 72


def normalize_variants(path: str) -> list:
    clean = path
    if clean.lower().startswith("file:///"):
        clean = clean[8:]
    clean = unquote(clean)
    return [
        clean,
        clean.lower(),
        clean.replace("/", "\\"),
        clean.replace("/", "\\").lower(),
        clean.replace("\\", "/"),
        clean.replace("\\", "/").lower(),
    ]


def try_match(path: str, known_hash: str):
    known_hash = known_hash.lower()
    for variant in normalize_variants(path):
        for enc in ENCODINGS:
            raw = variant.encode(enc, errors="replace")
            for name, fn in CANDIDATES:
                if fn(raw).lower() == known_hash:
                    return name, enc, repr(variant)
    return None


def section(title: str):
    print(f"\n{SEP}\n  {title}\n{SEP}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    db_path = Path(config["db_path"]).expanduser()

    print(f"\n{'='*72}")
    print("  MIK Diagnostic Report")
    print(f"{'='*72}")
    print(f"  Config:  {config_path}")
    print(f"  DB path: {db_path}")
    print(f"  DB exists: {db_path.exists()}")

    if not db_path.exists():
        print("\nERROR: DB not found. Check 'db_path' in your config.")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # ── 1. Summary counts ──────────────────────────────────────────────────
    section("1. Database Summary")
    total = conn.execute("SELECT COUNT(*) FROM Song").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 1").fetchone()[0]
    unanalyzed = conn.execute("SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 0").fetchone()[0]
    print(f"  Total songs:      {total}")
    print(f"  IsAnalyzed = 1:   {analyzed}")
    print(f"  IsAnalyzed = 0:   {unanalyzed}  ← should include your test file")

    # ── 2. Unanalyzed rows ─────────────────────────────────────────────────
    section("2. Unanalyzed Rows (IsAnalyzed = 0) — most recent 10")
    rows = conn.execute(
        "SELECT Id, File, FilePathHash, DateAdded, LastModifiedUtc "
        "FROM Song WHERE IsAnalyzed = 0 "
        "ORDER BY LastModifiedUtc DESC LIMIT 10"
    ).fetchall()

    if not rows:
        print("  !! No unanalyzed rows found.")
        print("  Possible causes:")
        print("    a) The insert didn't commit (check for errors in queue_insert output)")
        print("    b) MIK already ran and changed IsAnalyzed to 1 (check section 3)")
        print("    c) The wrong DB file is configured (check 'db_path' in config)")
    else:
        for r in rows:
            file_exists = os.path.exists(r["File"]) if r["File"] else False
            print(f"\n  File:        {r['File']}")
            print(f"  Hash:        {r['FilePathHash']}")
            print(f"  DateAdded:   {r['DateAdded']}")
            print(f"  File exists: {file_exists}")
            if not file_exists and r["File"]:
                print(f"  !! WARNING: File not accessible at this path from this machine.")
                print(f"     (Expected if running from Raspi — run this from Windows)")

    # ── 3. Hash algorithm identification ──────────────────────────────────
    section("3. Hash Algorithm Identification")
    analyzed_rows = conn.execute(
        "SELECT File, FilePathHash FROM Song "
        "WHERE IsAnalyzed = 1 AND FilePathHash IS NOT NULL AND FilePathHash != '' LIMIT 20"
    ).fetchall()

    if not analyzed_rows:
        print("  No analyzed rows to compare against.")
        print("  → You need at least one file that MIK itself added to identify the algorithm.")
        print("  → Drag a file manually into MIK, let it analyze, then re-run this script.")
    else:
        match = None
        for r in analyzed_rows:
            match = try_match(r["File"], r["FilePathHash"])
            if match:
                break
        if match:
            algo, enc, variant = match
            current = config.get("hash_algo", "sha256")
            current_enc = config.get("hash_encoding", "utf-8")
            print(f"  ✅ Hash algorithm identified!")
            print(f"     Algorithm: {algo}  (config has: '{current}')")
            print(f"     Encoding:  {enc}  (config has: '{current_enc}')")
            print(f"     Path form: {variant}")
            if algo != current or enc != current_enc:
                print(f"\n  !! MISMATCH — your config uses '{current}'/'{current_enc}'")
                print(f"     Update automation_config.json:")
                print(f"       \"hash_algo\": \"{algo}\",")
                if enc != "utf-8":
                    print(f"       \"hash_encoding\": \"{enc}\",")
                if "lower" in variant.lower() or "lower" in algo:
                    print(f"       (path is lowercased — use algo: \"{algo}_lower\")")
            else:
                print(f"     Config matches ✓")
        else:
            print(f"  ❌ No match found across standard algorithms.")
            sample = analyzed_rows[0]
            print(f"     Sample path: {sample['File']}")
            print(f"     Sample hash: {sample['FilePathHash']} (len={len(sample['FilePathHash'])})")
            print(f"     MIK may use a proprietary hash algorithm.")
            print(f"     → Compare DB before/after adding a known file in MIK UI.")

    # ── 4. Recently analyzed rows ──────────────────────────────────────────
    section("4. Recently Analyzed Rows (last 5) — to verify MIK is working")
    recent = conn.execute(
        "SELECT File, IsAnalyzed, LastAnalyzedUtc, MainKey, Tempo "
        "FROM Song WHERE IsAnalyzed = 1 "
        "ORDER BY LastAnalyzedUtc DESC LIMIT 5"
    ).fetchall()
    if not recent:
        print("  No analyzed rows. MIK hasn't analyzed anything yet.")
    else:
        for r in recent:
            print(f"  [{r['IsAnalyzed']}] {r['File']}")
            print(f"       Key={r['MainKey']}  BPM={r['Tempo']}  Analyzed={r['LastAnalyzedUtc']}")

    # ── 5. Path format check ───────────────────────────────────────────────
    section("5. Path Format in DB vs Config watch_dir")
    watch_dir = config.get("watch_dir", "")
    print(f"  watch_dir in config: {watch_dir}")
    if rows:
        sample_path = rows[0]["File"]
        print(f"  Path stored in DB:   {sample_path}")
        if watch_dir and not sample_path.lower().startswith(watch_dir.lower()[:3]):
            print(f"  !! WARNING: DB path drive letter doesn't match watch_dir")

    # ── 6. Collections ─────────────────────────────────────────────────────
    section("6. Collections (MIKRoot required)")
    try:
        cols = conn.execute(
            "SELECT Id, Name, IsLibrary FROM Collection WHERE IsLibrary = 1"
        ).fetchall()
        for c in cols:
            print(f"  [{c['IsLibrary']}] {c['Name']} (id={c['Id'][:8]}...)")
        if not any(c["Name"] == "MIKRoot" for c in cols):
            print("  !! WARNING: 'MIKRoot' collection not found!")
            print("     Launch MIK at least once to initialize the DB properly.")
    except Exception as e:
        print(f"  Error reading collections: {e}")

    conn.close()

    print(f"\n{'='*72}")
    print("  Diagnostic complete. Share the output above for next steps.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
