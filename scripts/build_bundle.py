#!/usr/bin/env python3
"""Build report_bundle.json for the FULL history in the schema gen_report.py expects."""
import sqlite3, json, datetime as dt, statistics as st
from collections import defaultdict

MMOL = 18.018
LOW, HIGH, VLOW, VHIGH = 3.9, 10.0, 3.0, 13.9
con = sqlite3.connect("cgm_data.db")
rows = con.execute("SELECT sgv, date_ms FROM readings ORDER BY date_ms").fetchall()

by_hour = defaultdict(list)
by_dow_hour = defaultdict(list)
by_day = defaultdict(list)
vals = []
for sgv, ms in rows:
    v = sgv / MMOL
    d = dt.datetime.utcfromtimestamp(ms / 1000)
    vals.append(v)
    by_hour[d.hour].append(v)
    by_dow_hour[(d.weekday(), d.hour)].append(v)
    by_day[d.date()].append((d.hour, v))

def pct(a, p):
    a = sorted(a); k = (len(a) - 1) * p / 100; f = int(k); c = min(f + 1, len(a) - 1)
    return round(a[f] + (a[c] - a[f]) * (k - f), 2)

def tirband(a):
    n = len(a)
    return (round(100*sum(x<VLOW for x in a)/n,1), round(100*sum(VLOW<=x<LOW for x in a)/n,1),
            round(100*sum(LOW<=x<=HIGH for x in a)/n,1), round(100*sum(HIGH<x<=VHIGH for x in a)/n,1),
            round(100*sum(x>VHIGH for x in a)/n,1))

agp = {}
for h in range(24):
    a = by_hour[h]; vl,lo,ir,hi,vh = tirband(a)
    agp[str(h)] = {"p05":pct(a,5),"p25":pct(a,25),"p50":pct(a,50),"p75":pct(a,75),"p95":pct(a,95),
                   "mean":round(st.mean(a),2),"tir":ir,"low":round(vl+lo,1),"high":round(hi+vh,1),"n":len(a)}

heat = {}
for (d,h),a in by_dow_hour.items():
    _,_,ir,_,_ = tirband(a)
    heat[f"{d}_{h}"] = {"tir":ir,"mean":round(st.mean(a),2),"n":len(a)}

vl,lo,ir,hi,vh = tirband(vals)
bands = {"vlow":vl,"low":lo,"inr":ir,"high":hi,"vhigh":vh}

# overnight drift 00->06
drift=[]; rising=falling=0
for day,pts in by_day.items():
    hm = {h:v for h,v in pts}
    if 0 in hm and 6 in hm:
        dd = hm[6]-hm[0]; drift.append(dd)
        if dd>1: rising+=1
        elif dd<-1: falling+=1
overnight = {"avg_drift_00_to_06":round(st.mean(drift),2) if drift else 0,
             "nights":len(drift),"rising_gt1":rising,"falling_gt1":falling}

profile = json.load(open("scripts/__profile.json"))
prof = {
    "basal":[{"time":b["time"],"rate":b["rate"],"unit":"U/hr"} for b in profile["basal_rates"]],
    "isf":[{"time":i["time"],"value":i["value"],"unit":"mmol/L/U"} for i in profile["isf"]],
    "icr":[{"time":c["time"],"value":c["value"],"unit":"g/U"} for c in profile["carb_ratios"]],
    "tdb":profile["total_daily_basal"],"dia":profile["dia"],
    "target":{"time":"00:00","low":profile["targets"][0]["low"],"high":profile["targets"][0]["high"],"unit":"mmol/L"},
}
m = st.mean(vals); s = st.pstdev(vals)
stats = {"mean":round(m,1),"sd":round(s,1),"cv":round(100*s/m,1),
         "gmi":round(3.31+0.02392*(m*MMOL),1),"n":len(vals)}

bundle = {"agp":agp,"heat":heat,"bands":bands,"overnight":overnight,"profile":prof,"stats":stats}
json.dump(bundle, open("report_bundle.json","w"), separators=(",",":"))
print("WROTE report_bundle.json  TIR=%s%% GMI=%s CV=%s n=%s"%(bands["inr"],stats["gmi"],stats["cv"],stats["n"]))
print("overnight:",overnight)
