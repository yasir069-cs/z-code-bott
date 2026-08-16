"""Verify the Phase 12 schedule without waiting: print every job's next
fire times over the coming 24h so the 36-scan session + 21:30 reset can be
checked instantly. Usage: python scripts/schedule_check.py
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
    while nxt and nxt.astimezone(config.TZ) < end and len(seen) < 40:
        nxt = nxt.astimezone(config.TZ)
        seen.append(nxt)
        nxt = job.trigger.get_next_fire_time(nxt, nxt + timedelta(seconds=1))
    if job.id.startswith("scan_"):
        fires.extend(seen)
    span = f"{seen[0]:%H:%M}..{seen[-1]:%H:%M} ({len(seen)}x)" if seen else "-"
    print(f"{job.id:12s} next fires IST: {span}")

fires = sorted(set(fires))
print(f"\nscan fires in next 24h: {len(fires)}")
print("first:", fires[0].strftime("%Y-%m-%d %H:%M"), "| last:", fires[-1].strftime("%H:%M"))
gaps = Counter((b - a).seconds // 60 for a, b in zip(fires, fires[1:]))
print("minute gaps between scans:", dict(gaps))
inside = all(config.SESSION_START <= f.strftime("%H:%M") < config.SESSION_END for f in fires)
print("all scans inside 18:30-21:30 IST window:", inside)
