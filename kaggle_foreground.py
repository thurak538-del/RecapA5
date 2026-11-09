"""Kaggle launcher with a self-restarting Cloudflare quick tunnel.

Keep the notebook cell running. If a quick tunnel drops, this process starts a
new tunnel and prints its new URL instead of leaving a dead URL on screen.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("RECAP_PORT", "8000"))
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
CLOUDFLARED = ROOT / "cloudflared"


CLOUDFLARED_URLS = [
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
]


def _is_valid_binary(path: Path) -> bool:
    """A real cloudflared is a multi-MB Linux ELF file; anything else is a broken download."""
    try:
        if path.stat().st_size < 5_000_000:
            return False
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def _download(url: str, target: Path) -> bool:
    """Download to a temp file; only a complete, valid file is moved into place."""
    tmp = target.with_suffix(".part")
    try:
        if shutil.which("curl"):
            result = subprocess.run(
                ["curl", "-fL", "--retry", "3", "--retry-delay", "3", "--connect-timeout", "20",
                 "--max-time", "240", "-o", str(tmp), url],
                capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  curl failed: {result.stderr.strip()[-200:]}", flush=True)
                return False
        else:
            urllib.request.urlretrieve(url, tmp)
        if not _is_valid_binary(tmp):
            print("  downloaded file is not a valid cloudflared binary", flush=True)
            return False
        tmp.replace(target)
        return True
    except Exception as exc:  # HTTP 5xx, DNS, timeout ...
        print(f"  download error: {exc}", flush=True)
        return False
    finally:
        tmp.unlink(missing_ok=True)


def ensure_cloudflared() -> None:
    if CLOUDFLARED.exists():
        if _is_valid_binary(CLOUDFLARED):
            CLOUDFLARED.chmod(CLOUDFLARED.stat().st_mode | stat.S_IEXEC)
            return
        print("Existing cloudflared file is broken; downloading again.", flush=True)
        CLOUDFLARED.unlink(missing_ok=True)
    # GitHub sometimes answers 502/504 for a few minutes, so retry with a growing pause.
    for attempt in range(1, 9):
        for url in CLOUDFLARED_URLS:
            print(f"Downloading cloudflared (attempt {attempt}/8)...", flush=True)
            if _download(url, CLOUDFLARED):
                CLOUDFLARED.chmod(CLOUDFLARED.stat().st_mode | stat.S_IEXEC)
                print("cloudflared ready.", flush=True)
                return
        wait = min(5 * attempt, 30)
        print(f"Retrying in {wait}s...", flush=True)
        time.sleep(wait)
    raise RuntimeError(
        "cloudflared could not be downloaded (GitHub is not answering). "
        "Wait a few minutes and run this cell again."
    )


def server_ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/settings", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def start_server() -> None:
    if server_ready():
        print(f"FastAPI already running: http://127.0.0.1:{PORT}", flush=True)
        return
    log = (LOG_DIR / "server_foreground.log").open("ab")
    env = os.environ.copy()
    env["RECAP_HOST"] = "0.0.0.0"
    env["RECAP_PORT"] = str(PORT)
    subprocess.Popen(
        [sys.executable, "run.py"], cwd=ROOT, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if server_ready():
            print(f"FastAPI ready: http://127.0.0.1:{PORT}", flush=True)
            return
        time.sleep(1)
    raise RuntimeError("FastAPI did not start. Check logs/server_foreground.log")


def run_tunnel_forever() -> None:
    ensure_cloudflared()
    print("Cloudflare tunnel supervisor started.", flush=True)
    print("URL ပျက်သွားရင် URL အသစ်ထွက်တဲ့အထိ ဒီ cell ကို မရပ်ပါနှင့်။", flush=True)
    while True:
        log_path = LOG_DIR / "cloudflared.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--protocol", "http2",
                 "--url", f"http://127.0.0.1:{PORT}"],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
            print("Starting a fresh quick tunnel...", flush=True)
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    print(line.rstrip(), flush=True)
                    log.write(line.encode("utf-8", errors="replace"))
                    log.flush()
            finally:
                proc.wait()
        print("Tunnel stopped. Retrying in 3 seconds...", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    start_server()
    run_tunnel_forever()
