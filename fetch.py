#!/usr/bin/env python3
"""Stiahne aktuálne dáta z GoodWe SEMS+ a doplní ich do histórie (data/history.json).

Spúšťa sa periodicky (GitHub Actions, každých ~15 min). Každé spustenie:
  1. prihlási sa do SEMS+ (meno/heslo ako v aplikácii SEMS),
  2. načíta okamžitý tok energie (FV, spotreba, sieť, batéria, SOC),
  3. načíta denné počítadlá striedača (napr. dnešná výroba),
  4. uloží vzorku do histórie a prepočíta denné súčty v kWh.

Premenné prostredia:
  SEMS_ACCOUNT     – e-mail / login do SEMS
  SEMS_PASSWORD    – heslo do SEMS
  SEMS_STATION_ID  – ID elektrárne (z URL v SEMS portáli)
  TZ_NAME          – časové pásmo, predvolene Europe/Bratislava
  HISTORY_FILE     – cesta k súboru histórie, predvolene data/history.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
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
KEEP_SAMPLE_DAYS = 400       # koľko dní uchovávať podrobný priebeh výkonu
MAX_GAP_MINUTES = 45         # dlhšie medzery sa pri integrácii neprepájajú


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
    def _signature(token: dict[str, Any]) -> str:
        ts = round(time.time() * 1000)
        digest = hashlib.sha256(f"{ts}@{token.get('uid', '')}@{token.get('token', '')}".encode()).hexdigest()
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
                "X-Signature": self._signature({}),
                "User-Agent": USER_AGENT,
            },
            json={"account": self.account, "pwd": pwd, "agreement": 1, "isChinese": False, "isLocal": False},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") not in SUCCESS_CODES:
            raise SemsError(f"Prihlásenie zlyhalo: {body.get('code')} {body.get('msg', '')}")
        token = body.get("data")
        if not isinstance(token, dict) or not token.get("token"):
            raise SemsError("Prihlásenie nevrátilo token")
        self.token = token
        api = body.get("api") or token.get("api")
        if isinstance(api, str) and api.startswith("https://"):
            self.api_base = api.rstrip("/")

    def get(self, path: str) -> Any:
        if self.token is None:
            self.login()
        for attempt in range(2):
            r = self.session.get(
                self.api_base + path,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "Token": json.dumps(self.token),
                    "X-Signature": self._signature(self.token),
                },
                timeout=20,
            )
            if r.status_code == 401 and attempt == 0:
                self.login()
                continue
            r.raise_for_status()
            body = r.json()
            code = body.get("code")
            if code == "C0602" and attempt == 0:   # vypršaný token
                self.login()
                continue
            if code not in SUCCESS_CODES:
                raise SemsError(f"{path}: {code} {body.get('msg', '')}")
            return body.get("data")
        raise SemsError("Obnovenie relácie zlyhalo")

    # --- vyššie funkcie

    def flow(self, station: str) -> dict[str, Any]:
        return self.get(f"/sems-plant/api/stations/flow?stationId={station}") or {}

    def devices(self, station: str) -> list[dict[str, Any]]:
        data = self.get(f"/sems-plant/api/stations/device/all-status?stationId={station}") or {}
        out = []
        for group in data.get("deviceDetailList", []):
            if group.get("deviceType") not in ("INVERTER", "ENERGY_STORAGE_INTEGRATED_CABINET"):
                continue
            for status in group.get("statusDetailList", []):
                for sn in status.get("snList", []):
                    out.append({"sn": sn, "deviceType": group["deviceType"], "status": status.get("status")})
        return out

    @staticmethod
    def _factors(groups: Any) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for g in groups or []:
            for f in (g or {}).get("factors", []) or []:
                if isinstance(f, dict) and f.get("code") and f.get("data") is not None:
                    res[f["code"]] = f["data"]
        return res

    def counters(self, station: str, sn: str, device_type: str) -> dict[str, Any]:
        q = f"?deviceType={device_type}&pwId={station}"
        return self._factors(self.get(f"/sems-plant/api/equipments/{sn}/telecounting{q}"))


# --------------------------------------------------------------------------- pomocné

def num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def load_history() -> dict[str, Any]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"days": {}}


def save_history(h: dict[str, Any]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(HISTORY_FILE)


def guess_daily_counters(counters: dict[str, Any]) -> dict[str, float]:
    """Z denných počítadiel striedača vyberie tie, ktoré vieme priradiť (kWh dnes)."""
    out: dict[str, float] = {}
    for code, val in counters.items():
        v = num(val)
        c = code.lower()
        if v is None or "today" not in c:
            continue
        if "pv" in c and "pv" not in out:
            out["pv"] = v
        elif ("consum" in c or "load" in c) and "load" not in out:
            out["load"] = v
        elif ("buy" in c or "import" in c or "purchas" in c) and "buy" not in out:
            out["buy"] = v
        elif ("sell" in c or "export" in c or "feed" in c) and "sell" not in out:
            out["sell"] = v
        elif "discharg" in c and "discharge" not in out:
            out["discharge"] = v
        elif "charg" in c and "charge" not in out:
            out["charge"] = v
    return out


def detect_grid_sign(days: dict[str, Any]) -> int:
    """Zistí znamienko pGrid: +1 ak kladné = odber zo siete, -1 ak kladné = pretok do siete.

    V noci (FV ≈ 0, batéria ≈ 0) musí dom odoberať zo siete, takže z nočných vzoriek
    sa dá konvencia spoľahlivo určiť."""
    votes = 0
    for d in days.values():
        for s in d.get("samples", []):
            _, pv, load, grid, bat, _ = s
            if pv is not None and load and grid and pv < 0.02 and load > 0.1 and abs(bat or 0) < 0.05:
                votes += 1 if grid > 0 else -1
    return 1 if votes >= 0 else -1


def integrate(samples: list[list[Any]], grid_sign: int) -> dict[str, float]:
    """Trapezoidálna integrácia výkonov (kW) na energiu (kWh)."""
    tot = {"pv": 0.0, "load": 0.0, "buy": 0.0, "sell": 0.0}

    def parts(s: list[Any]) -> dict[str, float]:
        _, pv, load, grid, _, _ = s
        g = (grid or 0.0) * grid_sign   # kladné = odber
        return {"pv": max(pv or 0, 0), "load": max(load or 0, 0), "buy": max(g, 0), "sell": max(-g, 0)}

    def minutes(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    for a, b in zip(samples, samples[1:]):
        dt = minutes(b[0]) - minutes(a[0])
        if dt <= 0 or dt > MAX_GAP_MINUTES:
            continue
        pa, pb = parts(a), parts(b)
        for k in tot:
            tot[k] += (pa[k] + pb[k]) / 2 * dt / 60
    return {k: round(v, 2) for k, v in tot.items()}


def recompute(h: dict[str, Any]) -> None:
    sign = detect_grid_sign(h["days"])
    h["grid_sign"] = sign
    for day in h["days"].values():
        est = integrate(day.get("samples", []), sign)
        if day.get("samples"):
            day["estimated"] = est
        day["totals"] = {**day.get("estimated", {}), **guess_daily_counters(day.get("counters", {}))}


# --------------------------------------------------------------------------- hlavný beh

def main() -> int:
    account = os.environ.get("SEMS_ACCOUNT")
    password = os.environ.get("SEMS_PASSWORD")
    station = os.environ.get("SEMS_STATION_ID")
    if not (account and password and station):
        print("Chýbajú SEMS_ACCOUNT / SEMS_PASSWORD / SEMS_STATION_ID", file=sys.stderr)
        return 2

    api = SemsPlus(account, password)
    flow = api.flow(station)
    now = datetime.now(TZ)
    day_key, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")

    sample = [
        hhmm,
        num(flow.get("pSystem") if flow.get("pSystem") is not None else flow.get("pAc")),
        num(flow.get("pConsum")),
        num(flow.get("pGrid")),
        num(flow.get("pBat")),
        num(flow.get("soc")),
    ]

    counters: dict[str, Any] = {}
    try:
        for dev in api.devices(station):
            for code, val in api.counters(station, dev["sn"], dev["deviceType"]).items():
                v = num(val)
                if v is None:
                    counters.setdefault(code, val)
                elif code in counters and num(counters[code]) is not None:
                    counters[code] = num(counters[code]) + v   # viac striedačov -> súčet
                else:
                    counters[code] = v
    except (SemsError, requests.RequestException) as exc:
        print(f"Počítadlá nedostupné: {exc}", file=sys.stderr)

    h = load_history()
    day = h["days"].setdefault(day_key, {"samples": []})
    if not day["samples"] or day["samples"][-1][0] != hhmm:
        day["samples"].append(sample)
    if counters:
        day["counters"] = counters

    cutoff = (now - timedelta(days=KEEP_SAMPLE_DAYS)).strftime("%Y-%m-%d")
    for k, d in h["days"].items():
        if k < cutoff:
            d.pop("samples", None)

    recompute(h)
    h["updated"] = now.isoformat(timespec="seconds")
    h["live"] = {"time": hhmm, "pv": sample[1], "load": sample[2], "grid": sample[3],
                 "bat": sample[4], "soc": sample[5]}
    h["counter_codes"] = sorted(counters)   # pre ladenie: aké počítadlá striedač posiela
    save_history(h)
    print(f"{day_key} {hhmm}: FV={sample[1]} kW, spotreba={sample[2]} kW, sieť={sample[3]} kW, "
          f"dnes {h['days'][day_key]['totals']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
