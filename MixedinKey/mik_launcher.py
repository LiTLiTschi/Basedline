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
        try:
            c = conn.execute(
                "SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 0"
            ).fetchone()[0]
            return c
        finally:
            conn.close()
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
