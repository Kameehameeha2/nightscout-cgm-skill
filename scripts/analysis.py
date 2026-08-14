#!/usr/bin/env python3
"""
Parametric CGM + therapy analysis engine.

This is the de-hardcoded successor to the one-off scripts (full_analysis.py,
build_bundle.py, dinner_compare.py). Everything here takes an explicit date
window and reads your live Trio profile, so the same code powers any range the
dashboard asks for (1 day … 1 year) instead of the fixed Dec 2025–Jul 2026 window.

Nothing here prints; every function returns JSON-serialisable dicts so the
Streamlit app can render them and the Claude report brain can reason over them.

Data sources:
  - glucose readings: cgm.py's SQLite store (cgm_data.db)
  - treatments (insulin/carbs/temp-basal): fetched live from Nightscout
  - profile (basal/ISF/ICR/targets): passed in (from `cgm.py profile`)
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

MMOL = 18.018
# Standard consensus bands (mmol/L). The app can override low/high for the KPI
# cards, but the AGP/TIR bands below use the clinical consensus thresholds.
LOW, HIGH = 3.9, 10.0
VLOW, VHIGH = 3.0, 13.9

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "cgm_data.db"

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Nightscout helpers
# --------------------------------------------------------------------------- #
def _api_root() -> str:
    """Derive the Nightscout /api/v1 root from NIGHTSCOUT_URL (any form)."""
    url = os.environ["NIGHTSCOUT_URL"].rstrip("/")
    for suf in ("/api/v1/entries.json", "/api/v1/entries", "/api/v1", "/api"):
        if url.endswith(suf):
            url = url[: -len(suf)]
            break
    return url.rstrip("/") + "/api/v1"


def newest_reading_ms() -> int | None:
    """Timestamp (ms) of the newest stored reading, or None if there are none."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute("SELECT MAX(date_ms) FROM readings").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def sync_new_readings(max_pages: int = 4) -> dict:
    """Top up the DB with only the readings newer than the newest stored one.

    cgm.py's `refresh --days 365` always pages *backwards* from the newest entry
    until it passes the cutoff, so it downloads a whole year (~105k readings,
    ~11 requests) to discover the handful that are actually new. This asks
    Nightscout for the gap instead — normally a single small request.

    Returns one of:
        {"status": "ok", "new": int, "latest_ms": int, "truncated": bool}
        {"status": "backfill_needed", "reason": str}   → caller runs the full fetch
        {"error": str}
    """
    if not os.environ.get("NIGHTSCOUT_URL"):
        return {"error": "NIGHTSCOUT_URL is not set"}

    latest = newest_reading_ms()
    if latest is None:
        # No DB or no rows: nothing to be incremental against. On Streamlit
        # Cloud this is the normal cold start, since the container's disk is
        # ephemeral and the database does not survive a restart.
        return {"status": "backfill_needed",
                "reason": "no local readings yet"}

    conn = sqlite3.connect(str(DB_PATH))
    try:
        before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        cursor_ms, truncated = latest, False
        for page in range(max_pages):
            try:
                resp = requests.get(
                    f"{_api_root()}/entries.json",
                    params={"count": 10000, "find[date][$gte]": cursor_ms + 1},
                    timeout=30,
                )
                resp.raise_for_status()
                entries = resp.json()
            except (requests.RequestException, ValueError) as e:
                return {"error": f"Nightscout fetch failed: {e}"}

            rows = [(e.get("_id"), e.get("sgv"), e.get("date"),
                     e.get("dateString"), e.get("trend"), e.get("direction"),
                     e.get("device"))
                    for e in entries
                    if e.get("type") == "sgv" and e.get("date") and e.get("_id")]
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?)", rows)
                conn.commit()
                cursor_ms = max(r[2] for r in rows)
            if len(entries) < 10000:
                break            # short page → we've caught up
            if page == max_pages - 1:
                truncated = True  # gap bigger than we'll pull in one go
        after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    finally:
        conn.close()

    return {"status": "ok", "new": after - before,
            "latest_ms": newest_reading_ms(), "truncated": truncated}


def fetch_treatments(start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Page treatments in [start, end) backwards by created_at (dedup by _id)."""
    root = _api_root()
    out: list[dict] = []
    seen: set = set()
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    window_end = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    while True:
        params = {
            "find[created_at][$gte]": start_iso,
            "find[created_at][$lt]": window_end,
            "count": 5000,
            "sort$desc": "created_at",
        }
        r = requests.get(f"{root}/treatments.json", params=params, timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        new = 0
        for t in batch:
            tid = t.get("_id")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(t)
            new += 1
        if new == 0 or len(batch) < 5000:
            break
        oldest = min(b.get("created_at", window_end) for b in batch)
        if oldest == window_end:
            break
        window_end = oldest
    return out


# --------------------------------------------------------------------------- #
# Small stat helpers
# --------------------------------------------------------------------------- #
def _pct(sorted_vals: list[float], p: float) -> float:
    a = sorted_vals
    if not a:
        return 0.0
    k = (len(a) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(a) - 1)
    return round(a[f] + (a[c] - a[f]) * (k - f), 2)


def _gmi(mean_mmol: float) -> float:
    return round(3.31 + 0.02392 * (mean_mmol * MMOL), 2)


def _tir(vals: list[float]) -> dict:
    n = len(vals)
    if not n:
        return {"vlow": 0, "low": 0, "inr": 0, "high": 0, "vhigh": 0}
    return {
        "vlow": round(100 * sum(x < VLOW for x in vals) / n, 1),
        "low": round(100 * sum(VLOW <= x < LOW for x in vals) / n, 1),
        "inr": round(100 * sum(LOW <= x <= HIGH for x in vals) / n, 1),
        "high": round(100 * sum(HIGH < x <= VHIGH for x in vals) / n, 1),
        "vhigh": round(100 * sum(x > VHIGH for x in vals) / n, 1),
    }


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": 0, "sd": 0, "cv": 0, "gmi": 0, "median": 0}
    m = st.mean(vals)
    s = st.pstdev(vals)
    return {
        "n": len(vals),
        "mean": round(m, 2),
        "sd": round(s, 2),
        "cv": round(100 * s / m, 1) if m else 0,
        "gmi": _gmi(m),
        "median": round(st.median(vals), 2),
    }


# --------------------------------------------------------------------------- #
# Readings
# --------------------------------------------------------------------------- #
def load_readings(start_ms: int, end_ms: int) -> list[tuple[float, int]]:
    """Return [(mmol_value, date_ms), …] for readings in [start_ms, end_ms]."""
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(str(DB_PATH))
    try:
        rows = con.execute(
            "SELECT sgv, date_ms FROM readings "
            "WHERE date_ms >= ? AND date_ms <= ? AND sgv > 0 ORDER BY date_ms",
            (start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    return [(sgv / MMOL, ms) for sgv, ms in rows]


# --------------------------------------------------------------------------- #
# Core analysis
# --------------------------------------------------------------------------- #
def analyze(days: int, profile: dict | None = None,
            include_treatments: bool = True) -> dict:
    """Full analysis of the last `days` days.

    Returns a dict with: meta, stats, tir bands, monthly, hourly AGP,
    day-of-week, day×hour heatmap, overnight drift, insulin summary,
    dinner announced/unannounced comparison, and a Loopalyzer average day.
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    readings = load_readings(start_ms, end_ms)
    if not readings:
        return {"error": "No readings in range. Refresh data from Nightscout first."}

    vals = [v for v, _ in readings]
    by_month: dict[str, list] = defaultdict(list)
    by_hour: dict[int, list] = defaultdict(list)
    by_dow: dict[int, list] = defaultdict(list)
    by_dow_hour: dict[tuple, list] = defaultdict(list)
    by_day: dict = defaultdict(list)

    for v, ms in readings:
        d = dt.datetime.utcfromtimestamp(ms / 1000)
        by_month[d.strftime("%Y-%m")].append(v)
        by_hour[d.hour].append(v)
        by_dow[d.weekday()].append(v)
        by_dow_hour[(d.weekday(), d.hour)].append(v)
        by_day[d.date()].append((d.hour, v))

    overall = _stats(vals)
    overall_tir = _tir(vals)

    months = {}
    for m in sorted(by_month):
        a = by_month[m]
        months[m] = {**_stats(a), "tir": _tir(a)}

    hours = {}
    for h in range(24):
        a = by_hour.get(h, [])
        if not a:
            continue
        srt = sorted(a)
        t = _tir(a)
        hours[h] = {
            "mean": round(st.mean(a), 2),
            "p05": _pct(srt, 5), "p25": _pct(srt, 25), "p50": _pct(srt, 50),
            "p75": _pct(srt, 75), "p95": _pct(srt, 95),
            "tir": t["inr"], "low": round(t["vlow"] + t["low"], 1),
            "high": round(t["high"] + t["vhigh"], 1), "n": len(a),
        }

    dows = {}
    for d in range(7):
        a = by_dow.get(d, [])
        if a:
            dows[DOW_NAMES[d]] = {"mean": round(st.mean(a), 2), **_tir(a)}

    heat = {}
    for (d, h), a in by_dow_hour.items():
        t = _tir(a)
        heat[f"{d}_{h}"] = {"tir": t["inr"], "mean": round(st.mean(a), 2), "n": len(a)}

    # overnight drift 00:00 -> 06:00
    drift, rising, falling = [], 0, 0
    for _day, pts in by_day.items():
        hm = {h: v for h, v in pts}
        if 0 in hm and 6 in hm:
            dd = hm[6] - hm[0]
            drift.append(dd)
            if dd > 1:
                rising += 1
            elif dd < -1:
                falling += 1
    overnight = {
        "avg_drift_00_to_06": round(st.mean(drift), 2) if drift else 0,
        "nights": len(drift), "rising_gt1": rising, "falling_gt1": falling,
    }

    result = {
        "meta": {
            "days": days,
            "date_from": start.date().isoformat(),
            "date_to": now.date().isoformat(),
            "units": "mmol/L",
            "target_low": LOW, "target_high": HIGH,
        },
        "stats": overall,
        "tir": overall_tir,
        "months": months,
        "hours": hours,
        "dows": dows,
        "heat": heat,
        "overnight": overnight,
    }

    if profile:
        result["profile"] = _summarise_profile(profile)

    if include_treatments:
        try:
            treatments = fetch_treatments(start, now)
            result["insulin"] = _summarise_insulin(treatments, start, now)
            result["dinner"] = _dinner_compare(treatments, readings)
            result["loopalyzer"] = loopalyzer(readings, treatments, profile)
        except Exception as e:  # noqa: BLE001 — treatments are best-effort
            result["insulin"] = {"error": f"Treatment fetch failed: {e}"}

    return result


# --------------------------------------------------------------------------- #
# Insulin / carbs
# --------------------------------------------------------------------------- #
def _parse_ts(t: dict):
    ts = t.get("created_at") or t.get("timestamp")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summarise_insulin(treatments: list[dict], start: dt.datetime,
                       end: dt.datetime) -> dict:
    """Per-day bolus/SMB/manual/carbs plus temp-basal integration."""
    daily = defaultdict(lambda: {"bolus": 0.0, "smb": 0.0, "manual": 0.0,
                                 "carbs": 0.0, "carb_entries": 0, "basal": 0.0})
    for t in treatments:
        ts = _parse_ts(t)
        if not ts:
            continue
        day = ts.date().isoformat()
        ins = t.get("insulin")
        if ins:
            ins = float(ins)
            daily[day]["bolus"] += ins
            et = (t.get("eventType") or "").upper()
            if "SMB" in et:
                daily[day]["smb"] += ins
            else:
                daily[day]["manual"] += ins
        carbs = t.get("carbs")
        if carbs:
            daily[day]["carbs"] += float(carbs)
            daily[day]["carb_entries"] += 1

    # temp basal integration: each temp basal supersedes the previous, so
    # effective duration = min(stated duration, gap to next temp basal).
    tb = []
    for t in treatments:
        if (t.get("eventType") or "") == "Temp Basal":
            ts = _parse_ts(t)
            rate, dur = t.get("rate"), t.get("duration")
            if ts is not None and rate is not None and dur:
                tb.append((ts, float(rate), float(dur)))
    tb.sort(key=lambda x: x[0])
    for i, (ts, rate, dur) in enumerate(tb):
        stated_end = ts + dt.timedelta(minutes=dur)
        eff_end = min(stated_end, tb[i + 1][0]) if i + 1 < len(tb) else stated_end
        eff_min = max(0.0, (eff_end - ts).total_seconds() / 60.0)
        daily[ts.date().isoformat()]["basal"] += rate * (eff_min / 60.0)

    def avg(key):
        xs = [v[key] for v in daily.values()]
        return round(sum(xs) / len(xs), 1) if xs else 0.0

    total_bolus = sum(v["bolus"] for v in daily.values())
    total_basal = sum(v["basal"] for v in daily.values())
    tdi = total_bolus + total_basal
    return {
        "days": len(daily),
        "avg_tdd": round(avg("bolus") + avg("basal"), 1),
        "avg_bolus": avg("bolus"),
        "avg_smb": avg("smb"),
        "avg_manual_bolus": avg("manual"),
        "avg_basal": avg("basal"),
        "avg_carbs": avg("carbs"),
        "avg_carb_entries": round(sum(v["carb_entries"] for v in daily.values())
                                  / len(daily), 1) if daily else 0,
        "bolus_pct": round(100 * total_bolus / tdi, 1) if tdi else 0,
        "basal_pct": round(100 * total_basal / tdi, 1) if tdi else 0,
    }


def _dinner_compare(treatments: list[dict],
                    readings: list[tuple[float, int]]) -> dict:
    """Evening glucose on announced vs unannounced dinner days.

    Announced = a carb entry ≥15 g logged 16:00–20:00 (UTC, matching how
    readings are bucketed here). Reports evening (18:00–23:59) TIR/high/low and
    a late-low (23:00–01:00) check attributed to the dinner date.
    """
    announced = set()
    for t in treatments:
        ts = _parse_ts(t)
        if ts and 16 <= ts.hour < 20 and (t.get("carbs") or 0) >= 15:
            announced.add(ts.date().isoformat())

    ev = defaultdict(list)
    late = defaultdict(list)
    for v, ms in readings:
        d = dt.datetime.utcfromtimestamp(ms / 1000)
        day = d.date().isoformat()
        if 18 <= d.hour <= 23:
            ev[day].append(v)
        if d.hour == 23:
            late[day].append(v)
        if d.hour == 0:
            prev = (d.date() - dt.timedelta(days=1)).isoformat()
            late[prev].append(v)

    def metrics(days_set):
        allv, latev = [], []
        for day in days_set:
            allv += ev.get(day, [])
            latev += late.get(day, [])
        n = len(allv)
        if not n:
            return None
        return {
            "days": len(days_set),
            "n": n,
            "mean": round(st.mean(allv), 2),
            "tir": round(100 * sum(LOW <= x <= HIGH for x in allv) / n, 1),
            "high": round(100 * sum(x > HIGH for x in allv) / n, 1),
            "low": round(100 * sum(x < LOW for x in allv) / n, 1),
            "late_low": round(100 * sum(x < LOW for x in latev) / len(latev), 1)
            if latev else None,
        }

    all_days = set(ev.keys())
    ann = all_days & announced
    unann = all_days - announced
    return {"announced": metrics(ann), "unannounced": metrics(unann),
            "announced_days": len(ann), "unannounced_days": len(unann)}


# --------------------------------------------------------------------------- #
# Loopalyzer — time-of-day aligned "average day"
# --------------------------------------------------------------------------- #
def loopalyzer(readings: list[tuple[float, int]], treatments: list[dict],
               profile: dict | None, bucket_min: int = 30) -> dict:
    """Nightscout-Loopalyzer-style average day.

    Aligns everything onto a single 24h day, bucketed to `bucket_min` minutes:
      - glucose: median + IQR (p25/p75) per bucket
      - bolus/SMB: average units delivered per day landing in each bucket
      - carbs: average grams per day landing in each bucket
      - basal: average *delivered* temp-basal rate per bucket, plus the
        scheduled profile basal for comparison
    IOB/COB are intentionally omitted rather than modelled inaccurately.
    """
    nb = 24 * 60 // bucket_min

    def bucket_of(d: dt.datetime) -> int:
        return (d.hour * 60 + d.minute) // bucket_min

    # glucose per bucket
    g_buckets = [[] for _ in range(nb)]
    for v, ms in readings:
        d = dt.datetime.utcfromtimestamp(ms / 1000)
        g_buckets[bucket_of(d)].append(v)

    n_days = max(1, len({dt.datetime.utcfromtimestamp(ms / 1000).date()
                         for _, ms in readings}))

    # bolus + carbs per bucket (summed then averaged per day)
    bolus_buckets = [0.0] * nb
    carb_buckets = [0.0] * nb
    for t in treatments:
        ts = _parse_ts(t)
        if not ts:
            continue
        b = bucket_of(ts)
        if t.get("insulin"):
            bolus_buckets[b] += float(t["insulin"])
        if t.get("carbs"):
            carb_buckets[b] += float(t["carbs"])

    # delivered temp-basal rate averaged across the window, per bucket
    tb = []
    for t in treatments:
        if (t.get("eventType") or "") == "Temp Basal":
            ts = _parse_ts(t)
            rate, dur = t.get("rate"), t.get("duration")
            if ts is not None and rate is not None and dur:
                tb.append((ts, float(rate), float(dur)))
    tb.sort(key=lambda x: x[0])
    # accumulate rate*minutes into buckets by time-of-day, then divide by
    # minutes observed in each bucket to get an average delivered rate.
    basal_num = [0.0] * nb
    basal_den = [0.0] * nb
    for i, (ts, rate, dur) in enumerate(tb):
        stated_end = ts + dt.timedelta(minutes=dur)
        eff_end = min(stated_end, tb[i + 1][0]) if i + 1 < len(tb) else stated_end
        cur = ts
        while cur < eff_end:
            b = bucket_of(cur)
            nxt = min(eff_end, cur + dt.timedelta(minutes=1))
            mins = (nxt - cur).total_seconds() / 60.0
            basal_num[b] += rate * mins
            basal_den[b] += mins
            cur = nxt

    # scheduled basal from profile (step schedule -> per bucket)
    sched = [None] * nb
    if profile and profile.get("basal_rates"):
        segs = []
        for seg in profile["basal_rates"]:
            hh, mm = (seg["time"].split(":") + ["0"])[:2]
            segs.append((int(hh) * 60 + int(mm), seg["rate"]))
        segs.sort()
        for b in range(nb):
            minute = b * bucket_min
            rate = segs[-1][1]
            for start_min, r in segs:
                if start_min <= minute:
                    rate = r
                else:
                    break
            sched[b] = rate

    buckets = []
    for b in range(nb):
        g = sorted(g_buckets[b])
        buckets.append({
            "t": round(b * bucket_min / 60, 2),  # hours (float)
            "g_med": _pct(g, 50) if g else None,
            "g_p25": _pct(g, 25) if g else None,
            "g_p75": _pct(g, 75) if g else None,
            "bolus": round(bolus_buckets[b] / n_days, 3),
            "carbs": round(carb_buckets[b] / n_days, 2),
            "basal_delivered": round(basal_num[b] / basal_den[b], 3)
            if basal_den[b] else None,
            "basal_scheduled": sched[b],
        })
    return {"bucket_min": bucket_min, "n_days": n_days, "buckets": buckets}


# --------------------------------------------------------------------------- #
# Profile summary (for the report brain)
# --------------------------------------------------------------------------- #
def _summarise_profile(profile: dict) -> dict:
    """Trim cgm.py's get_profile() output to the fields the report needs."""
    if not profile or profile.get("error"):
        return {"error": profile.get("error", "no profile") if profile else "no profile"}
    return {
        "units": profile.get("units"),
        "dia": profile.get("dia"),
        "total_daily_basal": profile.get("total_daily_basal"),
        "basal_rates": profile.get("basal_rates"),
        "isf": profile.get("isf"),
        "carb_ratios": profile.get("carb_ratios"),
        "targets": profile.get("targets"),
        "loop_settings": profile.get("loop_settings"),
    }


if __name__ == "__main__":
    # Quick smoke test against the local DB (no Claude, no rendering).
    import json
    import sys

    d = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    out = analyze(d, include_treatments=False)
    print(json.dumps({k: out[k] for k in ("meta", "stats", "tir") if k in out},
                     indent=2))
