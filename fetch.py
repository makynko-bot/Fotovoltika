#!/usr/bin/env python3
"""Stiahne dáta z GoodWe SEMS+ a uloží ich do histórie (data/history.json).

Spúšťa sa periodicky (GitHub Actions, každých ~15 min). Každé spustenie:
  1. prihlási sa do SEMS+ (meno/heslo ako v aplikácii SEMS),
  2. nájde elektráreň (ak nie je zadané SEMS_STATION_ID),
  3. načíta okamžitý tok energie (FV, spotreba, sieť, batéria, SOC),
  4. načíta denné súčty v kWh zo štatistík SEMS (pri prvom behu až rok dozadu),
  5. načíta priebeh výkonu za dnešok (a doplní chýbajúce dni dozadu).

Premenné prostredia:
  SEMS_ACCOUNT     – e-mail / login do SEMS
  SEMS_PASSWORD    – heslo do SEMS
  SEMS_STATION_ID  – nepovinné; ID elektrárne (inak sa použije prvá na účte)
  TZ_NAME          – časové pásmo, predvolene Europe/Bratislava
  HISTORY_FILE     – cesta k súboru histórie, predvolene data/history.json

Konvencie v histórii: výkony v kW, grid > 0 = odber zo siete, bat > 0 = vybíjanie.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
GATEWAY_URL = "https://eu-gateway.semsportal.com/web/sems"
SUCCESS_CODES = (0, "0", "00000")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Bratislava"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "data/history.json"))
KEEP_SAMPLE_DAYS = 90      # koľko dní uchovávať podrobný priebeh výkonu (súčty sa držia navždy)
SAMPLE_MINUTES = 5         # priebeh sa ukladá s krokom 5 minút
BACKFILL_CURVE_DAYS = 60   # do akej hĺbky dopĺňať chýbajúce priebehy
BACKFILL_PER_RUN = 6       # koľko chýbajúcich dní doplniť za jeden beh
MAX_GAP_MINUTES = 45

# položky štatistík SEMS+ -> kľúč v histórii
STAT_ITEMS = {
    "proSystemTotalStats": "pv",
    "proConsumStats": "load",
    "proPurchaseStats": "buy",
    "proGridStats": "sell",
    "proSelfConsumStats": "self",
    "proCharStats": "charge",
    "proDischarStats": "discharge",
}
CURVE_ITEMS = ["pSystem", "pConsum", "pBat", "pGrid", "soc"]


class SemsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- SEMS+ klient

class SemsPlus:
    def __init__(self, account: str, password: str) -> None:
        self.account = account
        self.password = password
        self.session = requests.Session()
        self.token: dict[str, Any] | None = None
        self.api_base = GATEWAY_URL

    @staticmethod
    def _signature(uid: str, token: str) -> str:
        ts = round(time.time() * 1000)
        digest = hashlib.sha256(f"{ts}@{uid}@{token}".encode()).hexdigest()
        return base64.b64encode(f"{digest}@{ts}".encode()).decode()

    def login(self) -> None:
        pwd = base64.b64encode(hashlib.md5(self.password.encode()).hexdigest().encode()).decode()
        r = self.session.post(
            LOGIN_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://semsplus.goodwe.com",
                "Referer": "https://semsplus.goodwe.com/",
                "Token": json.dumps({"uid": "", "timestamp": 0, "token": "", "client": "semsPlusWeb",
                                     "version": "", "language": "en"}),
                "X-Signature": self._signature("", ""),
                "User-Agent": USER_AGENT,
            },
            json={"account": self.account, "pwd": pwd, "agreement": 1, "isChinese": False, "isLocal": False},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") not in SUCCESS_CODES:
            raise SemsError(f"Prihlásenie zlyhalo (kód {body.get('code')}): {body.get('msg', '')} "
                            "– skontroluj SEMS_ACCOUNT a SEMS_PASSWORD")
        token = body.get("data")
        if not isinstance(token, dict) or not token.get("token"):
            raise SemsError("Prihlásenie nevrátilo token")
        api = body.get("api") or token.get("api")
        if isinstance(api, str) and api.startswith("https://"):
            self.api_base = api.rstrip("/")
        self.token = {**token, "api": self.api_base}

    def request(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        if self.token is None:
            self.login()
        for attempt in range(2):
            r = self.session.request(
                method,
                self.api_base + path,
                params=params,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "Token": json.dumps(self.token),
                    "X-Signature": self._signature(str(self.token.get("uid") or ""), self.token["token"]),
                },
                timeout=30,
            )
            if r.status_code == 401 and attempt == 0:
                self.login()
                continue
            r.raise_for_status()
            res = r.json()
            code = res.get("code")
            if code == "C0602" and attempt == 0:   # vypršaný token
                self.login()
                continue
            if code not in SUCCESS_CODES:
                raise SemsError(f"{path}: kód {code} {res.get('msg', '')}")
            return res.get("data")
        raise SemsError("Obnovenie relácie zlyhalo")

    # --- vyššie funkcie

    def stations(self) -> list[dict[str, Any]]:
        out, page = [], 1
        while True:
            data = self.request("POST", "/sems-plant/api/portal/stations/page", body={"current": page, "size": 100}) or {}
            rows = data.get("dataList") or []
            out += [r for r in rows if isinstance(r, dict) and r.get("id")]
            if not rows or len(out) >= int(data.get("total") or 0):
                return out
            page += 1

    def flow(self, station: str) -> dict[str, Any]:
        return self.request("GET", "/sems-plant/api/stations/flow", params={"stationId": station}) or {}

    def statistics(self, station: str, start: date, end: date) -> dict[str, dict[str, float]]:
        """Denné súčty v kWh {den: {pv, load, buy, ...}}. Rozsah musí byť v jednom roku."""
        data = self.request("POST", "/sems-plant/api/stations/statistics", body={
            "stationId": station, "isReport": False, "items": list(STAT_ITEMS), "dimension": "day",
            "startTime": f"{start} 00:00:00", "endTime": f"{end} 23:59:59",
        }) or {}
        days: dict[str, dict[str, float]] = {}
        for series in data.get("dataList") or []:
            key = STAT_ITEMS.get(series.get("item"))
            if not key:
                continue
            for pt in series.get("statisticsList") or []:
                v = num(pt.get("val"))
                if pt.get("date") and v is not None:
                    days.setdefault(pt["date"], {})[key] = round(v, 2)
        return days

    def curve(self, station: str, day: date) -> list[list[Any]]:
        """Priebeh výkonu za deň, vzorky [HH:MM, pv, load, grid(+odber), bat(+vybíjanie), soc]."""
        offset = datetime(day.year, day.month, day.day, 12, tzinfo=TZ).utcoffset()
        data = self.request("POST", "/sems-plant/api/v1/hems/power/statisticsAndPreV2", body={
            "stationId": station, "items": CURVE_ITEMS, "timeScale": 1,
            "timeZone": -int(offset.total_seconds() // 3600),
            "startTime": f"{day} 00:00:00", "endTime": f"{day} 23:59:59",
        }) or {}
        by_time: dict[str, dict[str, float | None]] = {}
        for series in data.get("dataList") or []:
            item = series.get("item")
            if item not in CURVE_ITEMS:
                continue
            for pt in series.get("powerData") or []:
                tp = str(pt.get("tp") or "")
                if len(tp) < 16 or not tp.startswith(str(day)):
                    continue
                by_time.setdefault(tp[11:16], {})[item] = num(pt.get("power"))
        return [sample_from(t, by_time[t]) for t in sorted(by_time) if int(t[3:5]) % SAMPLE_MINUTES == 0]


# --------------------------------------------------------------------------- pomocné

def num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def r3(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def sample_from(hhmm: str, f: dict[str, Any]) -> list[Any]:
    """SEMS+ hlási pGrid kladné pri dodávke do siete – otočíme na kladné = odber."""
    pv = num(f.get("pSystem")) if f.get("pSystem") is not None else num(f.get("pAc"))
    load = num(f.get("pConsum"))
    grid = num(f.get("pGrid"))
    return [hhmm, r3(pv), r3(abs(load) if load is not None else None),
            r3(-grid if grid else grid), r3(num(f.get("pBat"))), r3(num(f.get("soc")))]


def load_history() -> dict[str, Any]:
    if HISTORY_FILE.exists():
        h = json.loads(HISTORY_FILE.read_text())
        if h.get("format") == 2:
            return h
        print("Stará história má iný formát, zakladám novú.")
    return {"format": 2, "days": {}}


def save_history(h: dict[str, Any]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(HISTORY_FILE)


def integrate(samples: list[list[Any]]) -> dict[str, float]:
    """Záložný odhad denných kWh z priebehu výkonu (ak štatistiky nie sú k dispozícii)."""
    tot = {"pv": 0.0, "load": 0.0, "buy": 0.0, "sell": 0.0}

    def parts(s: list[Any]) -> dict[str, float]:
        g = s[3] or 0.0
        return {"pv": max(s[1] or 0, 0), "load": max(s[2] or 0, 0), "buy": max(g, 0), "sell": max(-g, 0)}

    def minutes(t: str) -> int:
        return int(t[:2]) * 60 + int(t[3:5])

    for a, b in zip(samples, samples[1:]):
        dt = minutes(b[0]) - minutes(a[0])
        if 0 < dt <= MAX_GAP_MINUTES:
            pa, pb = parts(a), parts(b)
            for k in tot:
                tot[k] += (pa[k] + pb[k]) / 2 * dt / 60
    return {k: round(v, 2) for k, v in tot.items()}


def year_chunks(start: date, end: date):
    """SEMS+ vracia nuly, ak rozsah presahuje kalendárny rok – rozdelíme ho."""
    while start <= end:
        stop = min(end, date(start.year, 12, 31))
        yield start, stop
        start = stop + timedelta(days=1)


# --------------------------------------------------------------------------- hlavný beh

def main() -> int:
    account = os.environ.get("SEMS_ACCOUNT")
    password = os.environ.get("SEMS_PASSWORD")
    if not (account and password):
        print("Chýbajú SEMS_ACCOUNT / SEMS_PASSWORD (GitHub: Settings → Secrets and variables → Actions)",
              file=sys.stderr)
        return 2

    api = SemsPlus(account, password)
    h = load_history()
    now = datetime.now(TZ)
    today = now.date()

    # 1) elektráreň
    station = os.environ.get("SEMS_STATION_ID") or h.get("station", {}).get("id")
    if not station:
        stations = api.stations()
        for s in stations:
            print(f"Nájdená elektráreň: {s.get('name')} (ID {s.get('id')})")
        if not stations:
            raise SemsError("Na účte sa nenašla žiadna elektráreň")
        station = str(stations[0]["id"])
        h["station"] = {"id": station, "name": stations[0].get("name")}

    # 2) okamžitý stav
    live = sample_from(now.strftime("%H:%M"), api.flow(station))
    h["live"] = dict(zip(["time", "pv", "load", "grid", "bat", "soc"], live))
    days = h["days"]

    # 3) denné súčty zo štatistík (prvý beh: rok dozadu, potom posledných 10 dní)
    stats_from = today - timedelta(days=10 if h.get("stats_ok") else 365)
    try:
        for a, b in year_chunks(stats_from, today):
            for k, t in api.statistics(station, a, b).items():
                if k <= str(today) and any(t.values()):
                    days.setdefault(k, {})["stats"] = t
        h["stats_ok"] = True
    except (SemsError, requests.RequestException) as exc:
        print(f"Štatistiky nedostupné: {exc}", file=sys.stderr)

    # 4) priebeh výkonu: dnešok, včerajšok (doplnenie konca dňa) a chýbajúce dni dozadu
    want = [today, today - timedelta(days=1)]
    want += [d for d in (today - timedelta(days=i) for i in range(2, BACKFILL_CURVE_DAYS))
             if not days.get(str(d), {}).get("samples")][:BACKFILL_PER_RUN]
    for d in want:
        key = str(d)
        if d < today and days.get(key, {}).get("complete"):
            continue
        try:
            samples = api.curve(station, d)
        except (SemsError, requests.RequestException) as exc:
            print(f"Priebeh za {key} nedostupný: {exc}", file=sys.stderr)
            break
        if samples:
            days.setdefault(key, {})["samples"] = samples
            if d < today:
                days[key]["complete"] = True
    today_day = days.setdefault(str(today), {})
    if not today_day.get("samples"):            # záloha: aspoň aktuálna vzorka
        today_day["samples"] = [live]

    # 5) upratanie a výsledné súčty
    cutoff = str(today - timedelta(days=KEEP_SAMPLE_DAYS))
    for k, d in days.items():
        if k < cutoff:
            d.pop("samples", None)
        d["totals"] = {**integrate(d.get("samples") or []), **(d.get("stats") or {})}
        d["exact"] = bool(d.get("stats"))

    h["grid_sign"] = 1
    h["updated"] = now.isoformat(timespec="seconds")
    save_history(h)
    t = today_day.get("totals", {})
    print(f"{h.get('station', {}).get('name') or station}: teraz FV={live[1]} kW, spotreba={live[2]} kW, "
          f"sieť={live[3]} kW | dnes {t} | dní v histórii: {len(days)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SemsError, requests.RequestException) as exc:
        print(f"CHYBA: {exc}", file=sys.stderr)
        sys.exit(1)
