#!/usr/bin/env python3
"""Consolidated full-history analysis: glucose + insulin + carbs."""
import sqlite3, json, datetime as dt, statistics as st
from collections import defaultdict

MMOL = 18.018
LOW, HIGH = 3.9, 10.0
VLOW, VHIGH = 3.0, 13.9

con = sqlite3.connect("cgm_data.db")
rows = con.execute("SELECT sgv, date_ms FROM readings ORDER BY date_ms").fetchall()

def mmol(sgv): return round(sgv / MMOL, 2)

vals = []          # mmol values
by_month = defaultdict(list)
by_hour = defaultdict(list)
by_dow = defaultdict(list)
by_dow_hour = defaultdict(list)

for sgv, ms in rows:
    v = sgv / MMOL
    d = dt.datetime.utcfromtimestamp(ms / 1000)
    vals.append(v)
    by_month[d.strftime("%Y-%m")].append(v)
    by_hour[d.hour].append(v)
    by_dow[d.weekday()].append(v)
    by_dow_hour[(d.weekday(), d.hour)].append(v)

def tir(a):
    n = len(a)
    if not n: return None
    return {
        "vlow": round(100*sum(x<VLOW for x in a)/n,1),
        "low": round(100*sum(VLOW<=x<LOW for x in a)/n,1),
        "inr": round(100*sum(LOW<=x<=HIGH for x in a)/n,1),
        "high": round(100*sum(HIGH<x<=VHIGH for x in a)/n,1),
        "vhigh": round(100*sum(x>VHIGH for x in a)/n,1),
    }

def gmi(mean_mmol):  # GMI(%) = 3.31 + 0.02392*mean_mgdl
    return round(3.31 + 0.02392*(mean_mmol*MMOL), 2)

def stats(a):
    m = st.mean(a); s = st.pstdev(a)
    return {"n":len(a),"mean":round(m,2),"sd":round(s,2),
            "cv":round(100*s/m,1),"gmi":gmi(m),
            "median":round(st.median(a),2)}

overall = stats(vals)
overall_tir = tir(vals)

months = {}
for m in sorted(by_month):
    a = by_month[m]
    months[m] = {**stats(a), **{"tir":tir(a)}}

hours = {}
def pct(a,p):
    a=sorted(a); k=(len(a)-1)*p/100; f=int(k); c=min(f+1,len(a)-1)
    return round(a[f]+(a[c]-a[f])*(k-f),2)
for h in range(24):
    a = by_hour[h]
    t = tir(a)
    hours[h] = {"mean":round(st.mean(a),2),"p05":pct(a,5),"p25":pct(a,25),
                "p50":pct(a,50),"p75":pct(a,75),"p95":pct(a,95),
                "tir":t["inr"],"low":round(t["vlow"]+t["low"],1),
                "high":round(t["high"]+t["vhigh"],1),"n":len(a)}

dow_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
dows = {}
for d in range(7):
    a = by_dow[d]
    dows[dow_names[d]] = {**{"mean":round(st.mean(a),2)}, **tir(a)}

# insulin
agg = json.load(open("treatments_agg.json"))
daily = agg["daily"]
tdd_list=[]; bolus_list=[]; basal_list=[]; carb_list=[]; smb_list=[]; manual_list=[]
ins_month = defaultdict(lambda: {"tdd":[],"bolus":[],"basal":[],"carbs":[]})
for d, v in daily.items():
    # only count full days present in glucose range
    if d < "2025-12-16" or d > "2026-07-14":
        continue
    bolus=v["bolus_total"]; basal=v["temp_basal_delivered"]; carbs=v["carbs"]
    tdd=bolus+basal
    tdd_list.append(tdd); bolus_list.append(bolus); basal_list.append(basal)
    carb_list.append(carbs); smb_list.append(v["smb"]); manual_list.append(v["manual_bolus"])
    mk=d[:7]
    ins_month[mk]["tdd"].append(tdd); ins_month[mk]["bolus"].append(bolus)
    ins_month[mk]["basal"].append(basal); ins_month[mk]["carbs"].append(carbs)

def avg(a): return round(sum(a)/len(a),1) if a else 0

insulin = {
    "days": len(tdd_list),
    "avg_tdd": avg(tdd_list),
    "avg_bolus": avg(bolus_list),
    "avg_basal": avg(basal_list),
    "avg_smb": avg(smb_list),
    "avg_manual_bolus": avg(manual_list),
    "avg_carbs": avg(carb_list),
    "bolus_pct": round(100*sum(bolus_list)/(sum(bolus_list)+sum(basal_list)),1),
    "basal_pct": round(100*sum(basal_list)/(sum(bolus_list)+sum(basal_list)),1),
}
insulin_month = {m:{"tdd":avg(v["tdd"]),"bolus":avg(v["bolus"]),
                    "basal":avg(v["basal"]),"carbs":avg(v["carbs"])}
                 for m,v in sorted(ins_month.items())}

out = {
    "overall": overall, "overall_tir": overall_tir,
    "months": months, "hours": hours, "dows": dows,
    "insulin": insulin, "insulin_month": insulin_month,
    "event_types": agg["event_types"],
    "date_range": {"from":"2025-12-16","to":"2026-07-14"},
}
json.dump(out, open("full_analysis.json","w"), indent=1)

# ---- print human summary ----
print("=== OVERALL (Dec16 2025 - Jul14 2026) ===")
print(overall, overall_tir)
print("\n=== MONTHLY ===")
print(f"{'month':8} {'mean':>5} {'gmi':>4} {'cv':>5} {'TIR':>5} {'low':>5} {'high':>5} {'tdd':>5} {'bol':>5} {'bas':>5} {'carb':>5}")
for m in sorted(months):
    s=months[m]; t=s["tir"]; im=insulin_month.get(m,{})
    print(f"{m:8} {s['mean']:>5} {s['gmi']:>4} {s['cv']:>5} {t['inr']:>5} {t['vlow']+t['low']:>5.1f} {t['high']+t['vhigh']:>5.1f} {im.get('tdd',0):>5} {im.get('bolus',0):>5} {im.get('basal',0):>5} {im.get('carbs',0):>5}")
print("\n=== INSULIN OVERALL ===")
print(insulin)
print("\n=== WORST HOURS (by high%) ===")
for h in sorted(hours, key=lambda h:-hours[h]["high"])[:6]:
    print(f"{h:02d}:00 high={hours[h]['high']}% low={hours[h]['low']}% mean={hours[h]['mean']} TIR={hours[h]['tir']}%")
print("\n=== WORST HOURS (by low%) ===")
for h in sorted(hours, key=lambda h:-hours[h]["low"])[:6]:
    print(f"{h:02d}:00 low={hours[h]['low']}% high={hours[h]['high']}% mean={hours[h]['mean']} TIR={hours[h]['tir']}%")
print("\n=== DAY OF WEEK ===")
for d in dow_names:
    v=dows[d]; print(f"{d} TIR={v['inr']}% mean={v['mean']} low={v['vlow']+v['low']:.1f}% high={v['high']+v['vhigh']:.1f}%")
print("\nWROTE full_analysis.json")
