"""
Nightscout CGM dashboard — cloud-ready Streamlit front end.

What it does
------------
- Pulls glucose from Nightscout into cgm.py's SQLite store (cgm_data.db) and
  keeps it fresh: a background-ish auto-refresh on load (hourly, cached) plus a
  one-click "Refresh all history" button that fetches a full year so every date
  range (1 day … 1 year) is instantly available.
- KPI cards + an interactive Plotly glucose chart, fully driven by the sidebar
  date filter and thresholds.
- One "Generate full report" button runs the whole pipeline inline: computes
  the analysis (AGP, hourly out-of-range, day×hour heatmap, Loopalyzer average
  day), then asks Claude to find patterns, check them against your live Trio
  profile, and suggest cautious settings experiments.

Secrets (Streamlit Cloud → App settings → Secrets, or a local
.streamlit/secrets.toml):
    NIGHTSCOUT_URL   = "https://your-site/api/v1/entries.json"
    ANTHROPIC_API_KEY = "sk-ant-..."     # enables the AI report section
    APP_PASSWORD     = "something-strong" # gates the app (recommended in cloud)

Run locally:  python -m streamlit run app.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).parent
CGM_SCRIPT = PROJECT_ROOT / "scripts" / "cgm.py"
DB_PATH = PROJECT_ROOT / "cgm_data.db"
MMOL_FACTOR = 18.0182

st.set_page_config(page_title="Nightscout CGM Dashboard", page_icon="🩸", layout="wide")


# --------------------------------------------------------------------------- #
# Secrets → environment (so cgm.py's subprocess and the AI brain see them)
# --------------------------------------------------------------------------- #
def _load_secrets() -> None:
    for key in ("NIGHTSCOUT_URL", "ANTHROPIC_API_KEY", "REPORT_MODEL",
                "DISPLAY_UNITS", "APP_PASSWORD"):
        try:
            if key in st.secrets and st.secrets[key]:
                # strip(): a trailing newline/space pasted into the Secrets box
                # is sent verbatim as the x-api-key header and 401s.
                os.environ[key] = str(st.secrets[key]).strip()
        except Exception:
            pass  # no secrets file locally — rely on the real environment


_load_secrets()

# analysis/report_ai read env lazily, so import after secrets are in place.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import analysis  # noqa: E402
import report_ai  # noqa: E402


# --------------------------------------------------------------------------- #
# Password gate (optional — only if APP_PASSWORD is set)
# --------------------------------------------------------------------------- #
def _check_password() -> bool:
    expected = os.environ.get("APP_PASSWORD")
    if not expected:
        return True  # no gate configured
    if st.session_state.get("_authed"):
        return True

    st.title("🩸 Nightscout CGM Dashboard")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Enter") and pw == expected:
            st.session_state["_authed"] = True
            st.rerun()
        elif pw:
            st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()


# --------------------------------------------------------------------------- #
# cgm.py subprocess wrapper + data loading
# --------------------------------------------------------------------------- #
def run_cgm(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CGM_SCRIPT), *args],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(PROJECT_ROOT), env=os.environ.copy(),
    )


def db_mtime() -> float:
    return DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_readings(days: int, _db_mtime: float) -> pd.DataFrame:
    """Glucose readings for the last N days from cgm.py's SQLite DB."""
    import sqlite3
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["sgv", "time"])
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cutoff_ms = (pd.Timestamp.now("UTC") - pd.Timedelta(days=days)).value // 1_000_000
        rows = conn.execute(
            "SELECT sgv, date_string FROM readings "
            "WHERE date_ms >= ? AND sgv > 0 ORDER BY date_ms",
            (cutoff_ms,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame(columns=["sgv", "time"])
    df = pd.DataFrame(rows, columns=["sgv", "date_string"])
    df["time"] = pd.to_datetime(df["date_string"], utc=True).dt.tz_convert(None)
    return df[["sgv", "time"]]


@st.cache_data(ttl=3600, show_spinner=False)
def auto_refresh(_bucket: str) -> tuple[int, str]:
    """Fetch a full year of history. Cached for 1h so it runs at most hourly
    and always on a cold start (empty DB), satisfying 'always current when I
    open it' without a separate background worker."""
    proc = run_cgm("refresh", "--days", "365")
    return proc.returncode, (proc.stderr or proc.stdout).strip()[:800]


@st.cache_data(ttl=3600, show_spinner=False)
def load_profile(_bucket: str) -> dict:
    """Live Trio profile via `cgm.py profile` (JSON on stdout)."""
    import json
    proc = run_cgm("profile")
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400]}
    try:
        return json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        return {"error": "Could not parse profile output."}


@st.cache_data(ttl=1800, show_spinner=False)
def run_analysis(days: int, _db_mtime: float, _profile_key: str) -> dict:
    """Full parametric analysis incl. treatments + Loopalyzer (see analysis.py)."""
    profile = load_profile("hourly")
    prof = None if profile.get("error") else profile
    return analysis.analyze(days, profile=prof, include_treatments=True)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def to_display(mgdl: float, mmol: bool) -> float:
    return round(mgdl / MMOL_FACTOR, 1) if mmol else round(mgdl, 1)


def compute_kpis(df: pd.DataFrame, low: float, high: float) -> dict:
    sgv = df["sgv"].to_numpy()
    n = len(sgv)
    mean = float(sgv.mean())
    std = float(sgv.std())
    return {
        "readings": n,
        "tir_pct": round(((sgv >= low) & (sgv <= high)).sum() / n * 100, 1),
        "low_pct": round((sgv < low).sum() / n * 100, 1),
        "high_pct": round((sgv > high).sum() / n * 100, 1),
        "gmi": round(3.31 + 0.02392 * mean, 1),
        "cv": round(std / mean * 100, 1) if mean else 0.0,
        "mean_mgdl": mean,
    }


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
st.sidebar.title("🩸 Controls")

if not os.environ.get("NIGHTSCOUT_URL"):
    st.sidebar.warning("NIGHTSCOUT_URL is not set — configure it in the app "
                       "secrets. Data refresh and reports will fail until then.")

RANGE_OPTIONS = [
    ("Last 1 day", 1), ("Last 7 days", 7), ("Last 14 days", 14),
    ("Last 30 days", 30), ("Last 60 days", 60), ("Last 90 days", 90),
    ("Last 6 months", 180), ("Last 9 months", 270), ("Last 1 year", 365),
]
range_choice = st.sidebar.selectbox(
    "Date range", options=RANGE_OPTIONS, index=3, format_func=lambda o: o[0]
)
days = range_choice[1]

use_mmol = st.sidebar.toggle("Show in mmol/L", value=True)
unit_label = "mmol/L" if use_mmol else "mg/dL"

st.sidebar.subheader(f"Target thresholds ({unit_label})")
if use_mmol:
    low_disp_in = st.sidebar.number_input("Low limit", 2.2, 6.7, 3.9, 0.1, format="%.1f")
    high_disp_in = st.sidebar.number_input("High limit", 6.7, 16.7, 10.0, 0.1, format="%.1f")
    low_thr, high_thr = low_disp_in * MMOL_FACTOR, high_disp_in * MMOL_FACTOR
else:
    low_thr = st.sidebar.number_input("Low limit", 40, 120, 70, 1)
    high_thr = st.sidebar.number_input("High limit", 120, 300, 180, 1)
if low_thr >= high_thr:
    st.sidebar.error("Low limit must be below the high limit.")
    st.stop()

st.sidebar.divider()
if st.sidebar.button("🔄 Refresh all history (1 click)", use_container_width=True,
                     help="Fetches a full year from Nightscout so every date "
                          "range is instantly available."):
    with st.spinner("Fetching a full year of readings…"):
        proc = run_cgm("refresh", "--days", "365")
    if proc.returncode == 0:
        load_readings.clear()
        run_analysis.clear()
        auto_refresh.clear()
        st.sidebar.success("All history refreshed.")
    else:
        st.sidebar.error("Refresh failed.")
        st.sidebar.code((proc.stderr or proc.stdout).strip()[:1000])

# Auto-refresh on load (cold start / hourly), non-fatal.
if os.environ.get("NIGHTSCOUT_URL"):
    with st.spinner("Checking for new data…"):
        rc, msg = auto_refresh("hourly")
    if rc != 0 and not DB_PATH.exists():
        st.sidebar.error("Initial data fetch failed.")
        st.sidebar.code(msg)


# --------------------------------------------------------------------------- #
# Header + KPIs + glucose chart
# --------------------------------------------------------------------------- #
st.title("Nightscout CGM Dashboard")
st.caption(f"Showing the last {days} days in {unit_label}")

df = load_readings(days, db_mtime())
if df.empty:
    st.warning("No readings in the local database yet. Use **Refresh all "
               "history** in the sidebar once NIGHTSCOUT_URL is configured.")
    st.stop()

kpis = compute_kpis(df, low_thr, high_thr)
low_disp, high_disp = to_display(low_thr, use_mmol), to_display(high_thr, use_mmol)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Time in Range", f"{kpis['tir_pct']}%", help=f"{low_disp}–{high_disp} {unit_label}")
c2.metric("GMI (est. A1C)", f"{kpis['gmi']}%")
c3.metric(f"Average ({unit_label})", to_display(kpis["mean_mgdl"], use_mmol))
c4.metric("Variability (CV)", f"{kpis['cv']}%")
c5.metric("Readings", f"{kpis['readings']:,}")

b1, b2 = st.columns(2)
b1.progress(min(kpis["low_pct"] / 100, 1.0), text=f"Below range: {kpis['low_pct']}%")
b2.progress(min(kpis["high_pct"] / 100, 1.0), text=f"Above range: {kpis['high_pct']}%")

st.subheader("Glucose over time")
plot_df = df.copy()
plot_df["display"] = plot_df["sgv"] / MMOL_FACTOR if use_mmol else plot_df["sgv"]
fig = go.Figure()
fig.add_hrect(y0=low_disp, y1=high_disp, fillcolor="rgba(46,204,113,0.12)", line_width=0,
              annotation_text="Target range", annotation_position="top left")
fig.add_trace(go.Scatter(x=plot_df["time"], y=plot_df["display"], mode="lines",
              name="Glucose", line=dict(color="#2E86DE", width=1.2),
              hovertemplate="%{x|%b %d %H:%M}<br>%{y} " + unit_label + "<extra></extra>"))
fig.add_hline(y=low_disp, line=dict(color="#E67E22", width=1, dash="dot"))
fig.add_hline(y=high_disp, line=dict(color="#E67E22", width=1, dash="dot"))
fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10),
                  yaxis_title=f"Glucose ({unit_label})", hovermode="x unified",
                  showlegend=False)
st.plotly_chart(fig, use_container_width=True)


# --------------------------------------------------------------------------- #
# Report charts (Plotly renderers over analysis.py output)
# --------------------------------------------------------------------------- #
def _conv(v):
    """analysis.py is in mmol/L; convert to the display unit."""
    if v is None:
        return None
    return round(v, 1) if use_mmol else round(v * MMOL_FACTOR, 0)


def _hour_series(hours: dict) -> tuple[list[int], list[dict]]:
    """Hour keys are ints in-process, but strings after a JSON round-trip, and
    hours with no readings are absent entirely — index by the real keys."""
    keys = sorted(hours, key=int)
    return [int(k) for k in keys], [hours[k] for k in keys]


def render_agp(hours: dict) -> None:
    x, cells = _hour_series(hours)
    med = [_conv(c["p50"]) for c in cells]
    p25 = [_conv(c["p25"]) for c in cells]
    p75 = [_conv(c["p75"]) for c in cells]
    p05 = [_conv(c["p05"]) for c in cells]
    p95 = [_conv(c["p95"]) for c in cells]
    f = go.Figure()
    f.add_hrect(y0=low_disp, y1=high_disp, fillcolor="rgba(46,204,113,0.10)", line_width=0)
    f.add_trace(go.Scatter(x=x + x[::-1], y=p95 + p05[::-1], fill="toself",
                fillcolor="rgba(46,134,222,0.12)", line_width=0, name="5–95%", hoverinfo="skip"))
    f.add_trace(go.Scatter(x=x + x[::-1], y=p75 + p25[::-1], fill="toself",
                fillcolor="rgba(46,134,222,0.28)", line_width=0, name="25–75%", hoverinfo="skip"))
    f.add_trace(go.Scatter(x=x, y=med, mode="lines", name="Median",
                line=dict(color="#2E86DE", width=2.5)))
    f.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10),
                    xaxis_title="Hour of day", yaxis_title=f"Glucose ({unit_label})",
                    xaxis=dict(dtick=3))
    st.plotly_chart(f, use_container_width=True)


def render_hourly_oor(hours: dict) -> None:
    hs, cells = _hour_series(hours)
    high = [c["high"] for c in cells]
    low = [-c["low"] for c in cells]  # below the axis
    f = go.Figure()
    f.add_trace(go.Bar(x=hs, y=high, name="High >target", marker_color="#E9A100"))
    f.add_trace(go.Bar(x=hs, y=low, name="Low <target", marker_color="#D03B3B"))
    f.update_layout(height=320, barmode="relative", margin=dict(l=10, r=10, t=10, b=10),
                    xaxis_title="Hour of day", yaxis_title="% out of range",
                    xaxis=dict(dtick=3))
    st.plotly_chart(f, use_container_width=True)


def render_heatmap(heat: dict) -> None:
    days_lbl = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    z = [[None] * 24 for _ in range(7)]
    for key, cell in heat.items():
        d, h = map(int, key.split("_"))
        z[d][h] = round(100 - cell["tir"], 1)  # % out of range
    f = go.Figure(go.Heatmap(z=z, x=list(range(24)), y=days_lbl, colorscale="Reds",
                  colorbar=dict(title="% OOR"), hovertemplate="%{y} %{x}:00<br>%{z}% out of range<extra></extra>"))
    f.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                    xaxis_title="Hour of day", xaxis=dict(dtick=3))
    st.plotly_chart(f, use_container_width=True)


def render_loopalyzer(loop: dict) -> None:
    buckets = loop["buckets"]
    t = [b["t"] for b in buckets]
    med = [_conv(b["g_med"]) for b in buckets]
    p25 = [_conv(b["g_p25"]) for b in buckets]
    p75 = [_conv(b["g_p75"]) for b in buckets]
    basal_d = [b["basal_delivered"] for b in buckets]
    basal_s = [b["basal_scheduled"] for b in buckets]
    carbs = [b["carbs"] for b in buckets]
    bolus = [b["bolus"] for b in buckets]

    st.markdown("**Average day — glucose**")
    g = go.Figure()
    g.add_hrect(y0=low_disp, y1=high_disp, fillcolor="rgba(46,204,113,0.10)", line_width=0)
    g.add_trace(go.Scatter(x=t + t[::-1], y=p75 + p25[::-1], fill="toself",
                fillcolor="rgba(46,134,222,0.22)", line_width=0, name="IQR", hoverinfo="skip"))
    g.add_trace(go.Scatter(x=t, y=med, mode="lines", name="Median glucose",
                line=dict(color="#2E86DE", width=2.5)))
    g.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                    xaxis_title="Hour of day", yaxis_title=f"Glucose ({unit_label})",
                    xaxis=dict(dtick=3), showlegend=False)
    st.plotly_chart(g, use_container_width=True)

    st.markdown("**Average day — insulin & carbs**")
    ins = go.Figure()
    ins.add_trace(go.Scatter(x=t, y=basal_s, mode="lines", name="Basal (scheduled)",
                  line=dict(color="#1BAF7A", width=1.5, dash="dot")))
    ins.add_trace(go.Scatter(x=t, y=basal_d, mode="lines", name="Basal (delivered avg)",
                  line=dict(color="#1BAF7A", width=2.5)))
    ins.add_trace(go.Bar(x=t, y=bolus, name="Bolus/SMB (U, avg/day)", marker_color="#4A3AA7",
                  yaxis="y2", opacity=0.6))
    ins.add_trace(go.Bar(x=t, y=carbs, name="Carbs (g, avg/day)", marker_color="#EB6834",
                  yaxis="y2", opacity=0.4))
    ins.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                      xaxis_title="Hour of day", xaxis=dict(dtick=3),
                      yaxis=dict(title="Basal (U/hr)"),
                      yaxis2=dict(title="Bolus U / Carbs g", overlaying="y", side="right"),
                      barmode="overlay", legend=dict(orientation="h", y=-0.25))
    st.plotly_chart(ins, use_container_width=True)
    st.caption(f"Averaged over {loop['n_days']} days, {loop['bucket_min']}-min buckets. "
               "IOB/COB are intentionally omitted rather than modelled inaccurately.")


# --------------------------------------------------------------------------- #
# Generate full report (one click → everything)
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("📋 Full report")
st.caption("One click: computes the AGP, hourly out-of-range, day×hour heatmap, "
           "and Loopalyzer average day, then asks Claude to find patterns, check "
           "them against your Trio settings, and suggest what to discuss with your "
           "care team.")

if st.button("Generate full report", type="primary", use_container_width=True):
    with st.spinner("Computing analysis (glucose + treatments + Loopalyzer)…"):
        result = run_analysis(days, db_mtime(), str(load_profile("hourly").get("units")))

    if result.get("error"):
        st.error(result["error"])
    else:
        prof = result.get("profile")
        if prof and not prof.get("error"):
            st.success(f"Analysed {result['stats']['n']:,} readings over "
                       f"{result['meta']['days']} days · live Trio profile loaded.")
        else:
            st.info("Analysed the glucose data. (Trio profile unavailable — the "
                    "settings check will be limited.)")

        st.markdown("#### Ambulatory Glucose Profile (AGP)")
        render_agp(result["hours"])
        st.markdown("#### Time out of range, by hour")
        render_hourly_oor(result["hours"])
        st.markdown("#### Where trouble clusters — day × hour")
        render_heatmap(result["heat"])
        st.markdown("#### Loopalyzer — your average day")
        if result.get("loopalyzer"):
            render_loopalyzer(result["loopalyzer"])
        else:
            st.info("Loopalyzer needs treatment data, which couldn't be fetched.")

        st.markdown("#### Analysis & settings suggestions")
        st.caption("Data-pattern analysis, not medical advice. Discuss any change "
                   "with your care team, one variable at a time, and treat lows first.")
        with st.spinner("Asking Claude to analyse the patterns and your settings…"):
            ai = report_ai.generate_report(result)
        if ai["ok"]:
            st.markdown(ai["markdown"])
            st.caption(f"Generated by {ai['model']}.")
        else:
            st.warning(ai.get("error") or "AI report unavailable.")
            if ai.get("markdown"):
                st.markdown(ai["markdown"])
