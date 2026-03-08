#!/usr/bin/env python3
"""
mik_process_manager.py

Manages Wine + MIK process lifecycle:
- Watches restart_flag_path for restart signals
- Gracefully stops MIK, cleans up Wine server
- Starts MIK under Xvfb (headless)
- Polls DB until all IsAnalyzed=0 rows are resolved

See: MixedinKey/MIK_AUTOMATION_PLAN.md Section 7

Run as systemd service: see MIK_AUTOMATION_PLAN.md Section 9
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
from mik_queue_insert import load_config, DEFAULT_CONFIG_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mik-process-manager")


def ensure_xvfb(display: str = ":99") -> bool:
    result = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
    if result.returncode == 0:
        return True
    log.info(f"Starting Xvfb on {display}...")
    subprocess.Popen(["Xvfb", display, "-screen", "0", "1024x768x24"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True).returncode == 0


def find_mik_pids() -> list:
    r = subprocess.run(["pgrep", "-f", "Mixed In Key"], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [int(p.strip()) for p in r.stdout.strip().split("\n") if p.strip()]


def stop_mik(timeout: int = 30):
    pids = find_mik_pids()
    if not pids:
        return
    log.info(f"Stopping MIK PIDs: {pids}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for i in range(timeout):
        time.sleep(1)
        if not find_mik_pids():
            log.info(f"MIK stopped after {i+1}s")
            break
    else:
        for pid in find_mik_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    subprocess.run(["wineserver", "-k"], capture_output=True)
    time.sleep(3)


def start_mik(config: dict):
    display = config.get("wine_display", ":99")
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["WINEPREFIX"] = os.path.expanduser(config.get("wine_prefix", "~/.wine"))
    env["WINEDEBUG"] = "-all"
    mik_exe = config.get("mik_windows_exe_path",
                          "C:\\Program Files\\Mixed In Key\\Mixed In Key 11\\Mixed In Key.exe")
    wine = config.get("wine_executable", "wine")
    log.info(f"Starting MIK: {wine} '{mik_exe}'")
    return subprocess.Popen([wine, mik_exe], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def count_pending(db_path: Path) -> int:
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        c = conn.execute("SELECT COUNT(*) FROM Song WHERE IsAnalyzed = 0").fetchone()[0]
        conn.close()
        return c
    except Exception:
        return -1


def wait_for_completion(db_path: Path, poll: int = 60, timeout_min: int = 240) -> bool:
    start = time.time()
    while True:
        pending = count_pending(db_path)
        elapsed = int(time.time() - start)
        log.info(f"Pending: {pending} tracks (elapsed: {elapsed}s)")
        if pending == 0:
            log.info("Analysis complete.")
            return True
        if elapsed > timeout_min * 60:
            log.warning(f"Timeout after {timeout_min}min. {pending} still pending.")
            return False
        time.sleep(poll)


def do_restart(config: dict):
    db_path = Path(config["db_path"]).expanduser()
    if count_pending(db_path) == 0:
        log.info("No pending tracks. Skipping restart.")
        return
    stop_mik()
    if not ensure_xvfb(config.get("wine_display", ":99")):
        log.error("Xvfb failed to start. Is it installed? apt install xvfb")
        return
    start_mik(config)
    time.sleep(config.get("mik_startup_wait_seconds", 15))
    wait_for_completion(
        db_path,
        poll=config.get("poll_interval_seconds", 60),
        timeout_min=config.get("analysis_timeout_minutes", 240)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)
    db_path = Path(config["db_path"]).expanduser()

    if args.status:
        print(f"MIK PIDs: {find_mik_pids() or 'not running'}")
        print(f"Pending (IsAnalyzed=0): {count_pending(db_path)}")
        return
    if args.stop:
        stop_mik()
        return
    if args.restart:
        do_restart(config)
        return

    # Daemon mode
    flag = Path(config.get("restart_flag_path", "/tmp/mik_restart_requested"))
    poll = config.get("manager_poll_seconds", 10)
    log.info(f"Daemon running. Flag: {flag}")

    if count_pending(db_path) > 0:
        log.info("Pending tracks on startup. Restarting MIK.")
        do_restart(config)

    while True:
        try:
            if flag.exists():
                flag.unlink()
                log.info("Restart flag detected.")
                do_restart(config)
        except Exception as e:
            log.error(f"Daemon error: {e}")
        time.sleep(poll)


if __name__ == "__main__":
    main()
