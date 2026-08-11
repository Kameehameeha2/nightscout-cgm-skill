#!/usr/bin/env python3
"""Pull full treatment history from Nightscout, aggregate insulin & carbs per day."""
import os, sys, json, datetime as dt
import requests
from collections import defaultdict

url = os.environ["NIGHTSCOUT_URL"]
for suf in ("/api/v1/entries.json", "/api/v1/entries", "/api/v1"):
    if url.endswith(suf):
        url = url[: -len(suf)]
        break
API_ROOT = url.rstrip("/") + "/api/v1"

START = "2025-12-01T00:00:00.000Z"
END = "2026-07-15T00:00:00.000Z"

def fetch_all():
    out = []
    # paginate backwards by created_at
    end = END
    seen = set()
    while True:
        params = {
            "find[created_at][$gte]": START,
            "find[created_at][$lt]": end,
            "count": 5000,
            "sort$desc": "created_at",
        }
        r = requests.get(f"{API_ROOT}/treatments.json", params=params, timeout=60)
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
        # move window
        oldest = min(b.get("created_at", END) for b in batch)
        if oldest == end:
            break
        end = oldest
        print(f"  fetched {len(out)} so far, window end={end}", file=sys.stderr)
    return out

treatments = fetch_all()
print(f"TOTAL treatments: {len(treatments)}", file=sys.stderr)

# event type census
types = defaultdict(int)
for t in treatments:
    types[t.get("eventType", "?")] += 1
print("EVENT TYPES:", dict(sorted(types.items(), key=lambda x: -x[1])), file=sys.stderr)

def day_of(t):
    ts = t.get("created_at") or t.get("timestamp")
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d.date().isoformat()

daily = defaultdict(lambda: {"bolus": 0.0, "carbs": 0.0, "n_bolus": 0,
                             "temp_basal_uh_hours": 0.0, "manual_bolus": 0.0,
                             "smb": 0.0, "carb_entries": 0})
for t in treatments:
    d = day_of(t)
    if not d:
        continue
    ins = t.get("insulin")
    if ins:
        daily[d]["bolus"] += float(ins)
        daily[d]["n_bolus"] += 1
        et = (t.get("eventType") or "").upper()
        if "SMB" in et:
            daily[d]["smb"] += float(ins)
        else:
            daily[d]["manual_bolus"] += float(ins)
    carbs = t.get("carbs")
    if carbs:
        daily[d]["carbs"] += float(carbs)
        daily[d]["carb_entries"] += 1

# temp basal integration — OpenAPS issues a new temp basal every ~5 min that
# supersedes the previous one, so effective duration = min(stated, gap-to-next).
def parse_ts(t):
    ts = t.get("created_at") or t.get("timestamp")
    if not ts: return None
    try: return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError: return None

tb = []
for t in treatments:
    if (t.get("eventType") or "") == "Temp Basal":
        ts = parse_ts(t); rate = t.get("rate"); dur = t.get("duration")
        if ts is not None and rate is not None and dur:
            tb.append((ts, float(rate), float(dur)))
tb.sort(key=lambda x: x[0])
for i, (ts, rate, dur) in enumerate(tb):
    stated_end = ts + dt.timedelta(minutes=dur)
    if i + 1 < len(tb):
        eff_end = min(stated_end, tb[i + 1][0])
    else:
        eff_end = stated_end
    eff_min = max(0.0, (eff_end - ts).total_seconds() / 60.0)
    d = ts.date().isoformat()
    daily[d]["temp_basal_uh_hours"] += rate * (eff_min / 60.0)

result = {}
for d in sorted(daily):
    v = daily[d]
    result[d] = {
        "bolus_total": round(v["bolus"], 2),
        "smb": round(v["smb"], 2),
        "manual_bolus": round(v["manual_bolus"], 2),
        "carbs": round(v["carbs"], 1),
        "carb_entries": v["carb_entries"],
        "temp_basal_delivered": round(v["temp_basal_uh_hours"], 2),
    }

json.dump({"daily": result, "event_types": dict(types), "n_treatments": len(treatments)},
          open("treatments_agg.json", "w"), indent=0)
print("WROTE treatments_agg.json", file=sys.stderr)
