"""Verify the production schedule without waiting: print every job's next fire
times over the coming 24h so the 60-scan session (18:00:15 .. 22:55:15 IST) and
the 23:00 duplicate-guard reset can be checked instantly.

Usage: python scripts/schedule_check.py
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

config.setup_logging()

from main import build_scheduler

sched = build_scheduler()
now = datetime.now(config.TZ)
end = now + timedelta(hours=24)

fires: list[datetime] = []
for job in sched.get_jobs():
    nxt, seen = job.trigger.get_next_fire_time(None, now), []
    # cap 80 > 60 scans/session so a full single-job schedule is never truncated
    while nxt and nxt.astimezone(config.TZ) < end and len(seen) < 80:
        nxt = nxt.astimezone(config.TZ)
        seen.append(nxt)
        nxt = job.trigger.get_next_fire_time(nxt, nxt + timedelta(seconds=1))
    if job.id == "scan":  # single scan job (was three overlapping scan_* jobs)
        fires.extend(seen)
    # %H:%M:%S so the SCAN_SECOND_OFFSET (:15) is visible, not hidden by %H:%M
    span = f"{seen[0]:%H:%M:%S}..{seen[-1]:%H:%M:%S} ({len(seen)}x)" if seen else "-"
    print(f"{job.id:14s} next fires IST: {span}")

fires = sorted(set(fires))
print(f"\nscan fires in next 24h: {len(fires)}")
if fires:
    print("first:", fires[0].strftime("%Y-%m-%d %H:%M:%S"), "| last:", fires[-1].strftime("%H:%M:%S"))
    gaps = Counter((b - a).seconds // 60 for a, b in zip(fires, fires[1:]))
    print("minute gaps between scans:", dict(gaps))
    inside = all(config.SESSION_START <= f.strftime("%H:%M") < config.SESSION_END for f in fires)
    print(f"all scans inside {config.SESSION_START}-{config.SESSION_END} IST window:", inside)
