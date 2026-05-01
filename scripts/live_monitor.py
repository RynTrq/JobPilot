"""
Live monitor — submits one career URL to JobPilot, then polls every 3s printing
stage transitions and the full log tail so bugs are immediately visible.

Usage:
    uv run python scripts/live_monitor.py <career_url> [--limit N] [--timeout T]
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8765"
LOG_FILE = Path("/tmp/jp_server.log")
SEEN_BYTES = 0


def _api(method: str, path: str, body: dict | None = None) -> dict:
    import urllib.request, urllib.error
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "code": e.code, "body": e.read().decode()[:300]}
    except Exception as e:
        return {"error": str(e)}


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S")


def _tail_new_logs() -> list[str]:
    global SEEN_BYTES
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open("rb") as fh:
        fh.seek(SEEN_BYTES)
        new_data = fh.read()
        SEEN_BYTES = fh.tell()
    lines = []
    for raw in new_data.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            event = rec.get("event", "")
            level = rec.get("level", "info").upper()
            stage = rec.get("stage", "")
            msg_parts = [event]
            for key in ("job_url", "adapter", "characters", "outcome", "error",
                        "error_code", "message", "skip_reason", "stage",
                        "discovered", "total", "url"):
                val = rec.get(key)
                if val and key not in ("stage",) or (key == "stage" and val):
                    msg_parts.append(f"{key}={val}")
            lines.append(f"  [{_ts()}] {level:5s} {' | '.join(str(p) for p in msg_parts[:6])}")
        except Exception:
            lines.append(f"  [{_ts()}]  {raw[:120]}")
    return lines


def monitor(url: str, limit: int, timeout: int) -> int:
    global SEEN_BYTES
    # Anchor log position to now so we only see new entries
    if LOG_FILE.exists():
        SEEN_BYTES = LOG_FILE.stat().st_size

    print(f"\n{'='*70}")
    print(f"[{_ts()}] SUBMITTING  {url}")
    print(f"[{_ts()}] limit={limit}  timeout={timeout}s  dry_run=True")
    print("="*70)

    # Stop any active run first
    _api("POST", "/run/stop")
    time.sleep(0.5)

    resp = _api("POST", "/run/start", {
        "career_url": url,
        "limit": limit,
        "force_reprocess": False,
        "bypass_classifier": False,
    })
    if "error" in resp:
        print(f"[{_ts()}] ERROR  start failed: {resp}")
        return 1

    run_id = resp.get("run_id")
    print(f"[{_ts()}] run_id={run_id}")

    prev_state = None
    prev_stage = None
    deadline = time.time() + timeout
    stage_start = time.time()

    while time.time() < deadline:
        time.sleep(3)
        status = _api("GET", "/status")

        state = status.get("state", "?")
        stage = status.get("current_stage") or ""
        job = (status.get("current_job") or "")[:60]
        msg = (status.get("current_stage_message") or "")[:80]

        # Print new log lines
        new_lines = _tail_new_logs()
        for line in new_lines:
            print(line)

        # Print stage transition
        if state != prev_state or stage != prev_stage:
            elapsed = f"{time.time() - stage_start:.1f}s"
            print(f"\n[{_ts()}] ── STATE={state:12s}  STAGE={stage or '':30s}  ({elapsed})")
            if job:
                print(f"           job={job}")
            if msg:
                print(f"           msg={msg}")
            prev_state = state
            prev_stage = stage
            stage_start = time.time()

        if state not in ("running", "starting"):
            break

    # Final report
    status = _api("GET", "/status")
    last_failure = status.get("last_failure")
    print(f"\n{'='*70}")
    print(f"[{_ts()}] FINAL STATE: {status.get('state')}")
    print(f"           stage  : {status.get('current_stage')}")
    print(f"           today  : {status.get('today',0)} applied")
    if last_failure:
        print(f"           FAILURE: {json.dumps(last_failure)[:300]}")
    print("="*70)

    # Drain any remaining logs
    for line in _tail_new_logs():
        print(line)

    return 0 if not last_failure else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()
    sys.exit(monitor(args.url, args.limit, args.timeout))
