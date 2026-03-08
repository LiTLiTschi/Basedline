#!/usr/bin/env python3
"""
mik_identify_hash.py

Run ONCE before first use to identify the FilePathHash algorithm used by MIK.
Compares known hash values in your DB against common hash algorithms.

See: MixedinKey/MIK_AUTOMATION_PLAN.md Section 4

Usage:
    python mik_identify_hash.py /path/to/MIKStore.db
    python mik_identify_hash.py  # auto-detects DB from config
"""

import hashlib
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote

CANDIDATES = [
    ("sha256",      lambda b: hashlib.sha256(b).hexdigest()),
    ("md5",         lambda b: hashlib.md5(b).hexdigest()),
    ("sha1",        lambda b: hashlib.sha1(b).hexdigest()),
    ("sha512",      lambda b: hashlib.sha512(b).hexdigest()),
    ("sha256_256",  lambda b: hashlib.sha256(b).hexdigest()[:32]),  # Truncated?
]
ENCODINGS = ["utf-8", "utf-16-le", "utf-16-be", "latin-1"]


def normalize_variants(path: str) -> list:
    """Generate possible path normalizations MIK might use."""
    clean = path
    if clean.lower().startswith("file:///"):
        clean = clean[8:]
    clean = unquote(clean)
    return [
        clean,
        clean.lower(),
        clean.upper(),
        clean.replace("/", "\\"),
        clean.replace("/", "\\").lower(),
        clean.replace("/", "\\").upper(),
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
                    return name, enc, variant
    return None


def main():
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        # Try to find from config
        config_path = Path(__file__).parent.parent / "data" / "automation_config.json"
        if config_path.exists():
            import json
            with open(config_path) as f:
                config = json.load(f)
            db_path = Path(config["db_path"]).expanduser()
        else:
            print("Usage: python mik_identify_hash.py /path/to/MIKStore.db")
            sys.exit(1)

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT File, FilePathHash FROM Song "
        "WHERE FilePathHash IS NOT NULL AND FilePathHash != '' LIMIT 30"
    ).fetchall()
    conn.close()

    if not rows:
        print("No rows with FilePathHash found. DB may be empty.")
        sys.exit(1)

    print(f"Testing {len(rows)} sample rows...")
    for row in rows:
        result = try_match(row["File"], row["FilePathHash"])
        if result:
            algo, enc, variant = result
            print(f"\n✅ MATCH FOUND")
            print(f"   Algorithm:  {algo}")
            print(f"   Encoding:   {enc}")
            print(f"   Path form:  {variant[:80]}")
            print(f"   Known hash: {row['FilePathHash']}")
            print(f"\n→ Set 'hash_algo': '{algo}' in automation_config.json")
            if enc != "utf-8":
                print(f"  NOTE: Non-UTF-8 encoding detected ({enc}). Script adjustment needed.")
            return

    print("\n❌ No match found across all tested algorithms and encodings.")
    print("   MIK may be using a custom/proprietary hash.")
    print(f"   Sample path:   {rows[0]['File']}")
    print(f"   Sample hash:   {rows[0]['FilePathHash']}")
    print(f"   Hash length:   {len(rows[0]['FilePathHash'])} chars")
    print("   → Open an issue or compare DB before/after adding a known track.")


if __name__ == "__main__":
    main()
