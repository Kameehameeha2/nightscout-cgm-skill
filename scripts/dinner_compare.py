#!/usr/bin/env python3
"""Compare evening glucose on announced vs unannounced dinner days."""
import os, sqlite3, json, datetime as dt, requests, statistics as st
from collections import defaultdict

MMOL = 18.018
LOW, HIGH, VLOW, VHIGH = 3.9, 10.0, 3.0, 13.9

# --- fetch carb entries (reuse pattern from fetch_treatments.py) ---
url = os.environ["NIGHTSCOUT_URL"]
for s in ("/api/v1/entries.json", "/api/v1/entries", "/api/v1"):
    if url.endswith(s):
        url = url[:-len(s)]; break
root = url.rstrip("/") + "/api/v1"

def fetch_carbs():
    out=[]; end="2026-07-15T00:00:00.000Z"; seen=set()
    while True:
        p={"find[carbs][$gte]":1,"find[created_at][$lt]":end,"count":1000,"sort$desc":"created_at"}
        b=requests.get(root+"/treatments.json",params=p,timeout=60).json()
        if not b: break
        new=0
        for t in b:
            i=t.get("_id")
            if i in seen: continue
            seen.add(i); out.append(t); new+=1
        if new==0 or len(b)<1000: break
        end=min(x.get("created_at",end) for x in b)
    return out

carbs = fetch_carbs()

# classify each date: announced dinner = carb entry >=15g between 16:00 and 20:00 local(UTC here)
announced=set()
for t in carbs:
    ts=(t.get("created_at") or "").replace("Z","+00:00")
    try: d=dt.datetime.fromisoformat(ts)
    except ValueError: continue
    if 16 <= d.hour < 20 and (t.get("carbs") or 0) >= 15:
        announced.add(d.date().isoformat())

# --- evening glucose per day ---
con=sqlite3.connect("cgm_data.db")
rows=con.execute("SELECT sgv,date_ms FROM readings ORDER BY date_ms").fetchall()
# window 18:00-00:00 (dinner->pre-midnight)  and 23:00-01:00 (overshoot-low check)
ev=defaultdict(list)      # date -> evening mmol values (18-24)
late=defaultdict(list)    # date -> 23:00-01:00 values (attribute to dinner date)
for sgv,ms in rows:
    d=dt.datetime.utcfromtimestamp(ms/1000); v=sgv/MMOL
    day=d.date().isoformat()
    if 18 <= d.hour <= 23:
        ev[day].append(v)
    if d.hour == 23:
        late[day].append(v)
    if d.hour in (0,) :  # 00:xx belongs to previous dinner date
        prev=(d.date()-dt.timedelta(days=1)).isoformat()
        late[prev].append(v)

def metrics(vals):
    n=len(vals)
    return {
        "days":None,"n":n,
        "mean":round(st.mean(vals),2),
        "tir":round(100*sum(LOW<=x<=HIGH for x in vals)/n,1),
        "high":round(100*sum(x>HIGH for x in vals)/n,1),
        "low":round(100*sum(x<LOW for x in vals)/n,1),
        "peak_p95":round(sorted(vals)[int(0.95*(n-1))],2),
    }

def agg_group(days):
    allv=[]; latev=[]
    for d in days:
        allv+=ev.get(d,[])
        latev+=late.get(d,[])
    m=metrics(allv); m["days"]=len(days)
    m["late_low_2301"]=round(100*sum(x<LOW for x in latev)/len(latev),1) if latev else None
    return m

all_days=set(ev.keys())
ann=sorted(all_days & announced)
unann=sorted(all_days - announced)

A=agg_group(ann); U=agg_group(unann)
print("Evening window = 18:00–23:59 ; late-low window = 23:00–01:00\n")
print(f"{'group':12}{'days':>6}{'readings':>10}{'mean':>7}{'TIR%':>7}{'high%':>7}{'low%':>7}{'p95':>7}{'late-low%':>11}")
for name,g in (("ANNOUNCED",A),("UNANNOUNCED",U)):
    print(f"{name:12}{g['days']:>6}{g['n']:>10}{g['mean']:>7}{g['tir']:>7}{g['high']:>7}{g['low']:>7}{g['peak_p95']:>7}{str(g['late_low_2301']):>11}")
print()
print(f"Delta (announced - unannounced): TIR {A['tir']-U['tir']:+.1f} pts, high {A['high']-U['high']:+.1f} pts, "
      f"mean {A['mean']-U['mean']:+.2f}, late-low {(A['late_low_2301']-U['late_low_2301']):+.1f} pts")
json.dump({"announced":A,"unannounced":U,"announced_days":len(ann)},
          open("dinner_compare.json","w"), indent=1)
