#!/usr/bin/env python3
"""Vygeneruje ukážkové dáta (site/data/history.json) na vyskúšanie stránky bez SEMS."""

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import fetch

random.seed(7)
days = {}
today = date.today()
for i in range(75, -1, -1):
    d = today - timedelta(days=i)
    sunny = random.uniform(0.15, 1.0)
    peak = 6.5 * sunny * (0.55 + 0.45 * math.cos((d.timetuple().tm_yday - 172) / 365 * 2 * math.pi))
    soc = 40.0
    samples = []
    last = 24 * 60 if i else 15 * 60
    for m in range(0, last, 15):
        h = m / 60
        pv = max(0.0, peak * math.sin(math.pi * (h - 7) / 10)) if 7 < h < 17 else 0.0
        pv *= random.uniform(0.85, 1.0)
        load = 0.35 + random.uniform(0, 0.25)
        if 6.5 < h < 8 or 17 < h < 21.5:
            load += random.uniform(0.8, 2.2)
        if 11 < h < 13:
            load += random.uniform(0, 1.5)
        surplus = pv - load
        bat = 0.0
        if surplus > 0 and soc < 100:
            bat = min(surplus, 2.5)
        elif surplus < 0 and soc > 10:
            bat = max(surplus, -2.5)
        soc = min(100, max(10, soc + bat * 0.25 / 10 * 100))
        grid = -(surplus - bat)   # kladné = odber
        samples.append([f"{m // 60:02d}:{m % 60:02d}", round(pv, 3), round(load, 3),
                        round(grid, 3), round(-bat, 3), round(soc)])
    days[d.isoformat()] = {"samples": samples}

h = {"days": days}
fetch.recompute(h)
last = days[today.isoformat()]["samples"][-1]
h["updated"] = f"{today.isoformat()}T{last[0]}:00+01:00"
h["live"] = dict(zip(["time", "pv", "load", "grid", "bat", "soc"], last))
h["demo"] = True
out = Path("site/data/history.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(h, separators=(",", ":")))
print("Zapísané", out)
