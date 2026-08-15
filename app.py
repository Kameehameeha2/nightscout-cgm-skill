"""
Nightscout CGM dashboard — cloud-ready Streamlit front end.

What it does
------------
- Pulls glucose from Nightscout into cgm.py's SQLite store (cgm_data.db) and
  keeps it fresh: a background-ish auto-refresh on load (hourly, cached) plus a
  one-click "Refresh all history" button that fetches a full year so every date
  range (1 day … 1 year) is instantly available.
- A top bar with the date-range presets that scope the whole page, a hero
  Time-in-Range figure, status tiles, and an interactive Plotly glucose chart.
  Long ranges aggregate to a median + IQR band, because a month of 5-minute
  readings overplots into an unreadable scribble.
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

import json
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

# --------------------------------------------------------------------------- #
# Palette. Validated, not eyeballed — see .streamlit/config.toml for the run.
#
# One rule drives the whole layout: red and green are near-identical under
# deuteranopia (ΔE 4.1, a hard fail), and "low" vs "in range" is exactly the
# distinction that matters here. So every glucose state carries an ICON and a
# WORD as well as its colour — colour never carries meaning on its own.
# Text always wears ink tokens; identity comes from a coloured mark beside it.
# --------------------------------------------------------------------------- #
SURFACE = "#131C2B"       # card / chart surface
INK = "#F2F5FA"           # primary ink
INK_DIM = "#A8B6CC"       # axis / secondary ink
INK_MUTED = "#8FA0B8"     # labels, captions
GRID = "#22314A"          # hairline gridline, one step off surface
SERIES = "#3987E5"        # glucose line (categorical slot 1)
GOOD = "#0CA30C"          # status: in range
WARNING = "#FAB219"       # status: high
CRITICAL = "#D03B3B"      # status: low
BAND_FILL = "rgba(12,163,12,0.10)"   # in-range wash — a wash, never a block
# Chrome accent: a deeper step of the series-blue ramp, so UI never wears a data
# colour. Keep in sync with primaryColor in .streamlit/config.toml.
ACCENT = "#256ABF"

st.set_page_config(page_title="Glucose", page_icon="🩸", layout="wide")


def style_fig(fig, height: int, ylab: str | None = None, legend: bool = False,
              hover: str = "x unified"):
    """Shared chart chrome: recessive hairline grid, transparent surface, ink text.

    Every chart goes through here so the dashboard reads as one system rather
    than a pile of differently-styled plots.
    """
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK_DIM, size=13,
                  family='system-ui, -apple-system, "Segoe UI", sans-serif'),
        hovermode="x unified",
        showlegend=legend,
        legend=dict(orientation="h", y=-0.2, font=dict(color=INK_MUTED)),
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=GRID,
                        font=dict(color=INK, size=13)),
    )
    # Bars and cells get a per-mark tooltip; only lines get the x-crosshair.
    fig.update_layout(hovermode=hover)
    # Hairline, solid, one step off surface — never dashed (reads as a threshold).
    axis = dict(gridcolor=GRID, zeroline=False, linecolor=GRID,
                tickfont=dict(color=INK_MUTED))
    fig.update_xaxes(**axis)
    fig.update_yaxes(title_text=ylab, **axis)
    return fig


# --------------------------------------------------------------------------- #
# Secrets → environment (so cgm.py's subprocess and the AI brain see them)
# --------------------------------------------------------------------------- #
def _load_secrets() -> None:
    for key in ("NIGHTSCOUT_URL", "ANTHROPIC_API_KEY", "REPORT_MODEL",
                "REPORT_EFFORT", "DISPLAY_UNITS", "APP_PASSWORD"):
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
import trio_settings  # noqa: E402


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
def backfill_history(_bucket: str) -> tuple[int, str]:
    """Fetch a full year — the expensive one (~105k readings, ~11 requests).

    Only needed when there is no local history to build on: a first run, or a
    Streamlit Cloud container restart, which wipes the ephemeral disk and takes
    the database with it. Cached for an hour so a crash-loop can't hammer
    Nightscout.
    """
    proc = run_cgm("refresh", "--days", "365")
    return proc.returncode, (proc.stderr or proc.stdout).strip()[:800]


@st.cache_data(ttl=60, show_spinner=False)
def sync_recent(_bucket: str) -> dict:
    """Top up with just the readings newer than the newest stored one.

    Cheap enough (normally one small request for ~12 readings) to run on every
    page load, so opening the app always shows current data instead of
    something up to an hour stale. The 60s cache stops it firing again on every
    button click within the same visit.
    """
    return analysis.sync_new_readings()


@st.cache_data(show_spinner=False)
def newest_reading(_db_mtime: float) -> pd.Timestamp | None:
    """Timestamp of the newest stored reading, for the freshness indicator."""
    ms = analysis.newest_reading_ms()
    return pd.Timestamp(ms, unit="ms", tz="UTC") if ms else None


def ago(ts: pd.Timestamp | None) -> str:
    if ts is None:
        return "no data yet"
    mins = int((pd.Timestamp.now("UTC") - ts).total_seconds() // 60)
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    return f"{hrs} h ago" if hrs < 48 else f"{hrs // 24} days ago"


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
# Card / tile styling
# --------------------------------------------------------------------------- #
st.markdown(f"""
<style>
  .block-container {{ padding-top: 2.2rem; }}
  .tile {{
    background: {SURFACE};
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px;
    padding: 14px 16px;
    height: 100%;
  }}
  .tile-label {{
    color: {INK_MUTED}; font-size: .78rem; text-transform: uppercase;
    letter-spacing: .05em; display: flex; align-items: center; gap: 7px;
  }}
  /* Proportional figures — tabular-nums makes big numbers look loose. */
  .tile-value {{ color: {INK}; font-size: 1.65rem; font-weight: 600; line-height: 1.35; }}
  .tile-sub   {{ color: {INK_MUTED}; font-size: .76rem; }}
  .hero {{ padding: 2px 0 6px 2px; }}
  .hero-label {{
    color: {INK_MUTED}; font-size: .82rem; text-transform: uppercase;
    letter-spacing: .06em;
  }}
  .hero-value {{ color: {INK}; font-size: 3.6rem; font-weight: 600; line-height: 1.05; }}
  .chip {{
    display: inline-flex; align-items: center; gap: 7px; color: {INK_MUTED};
    font-size: .9rem; background: {SURFACE}; border-radius: 999px;
    padding: 5px 13px; margin-left: 4px;
  }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}

  /* Range buttons. Streamlit marks the selected option by recolouring its
     LABEL to the accent, which reads as low-contrast blue-on-navy. Fill the
     button instead and keep the label white (5.39:1 on this accent) — a filled
     chip is also a much clearer "you are here" than tinted text. Both attribute
     spellings are matched because the testid form varies across versions. */
  div[data-testid="stButtonGroup"] button {{
    font-size: 1rem;
    font-weight: 600;
    padding: 0.5rem 1.15rem;
  }}
  /* The selected option is identified by aria-checked, NOT by a kind= or
     data-testid= attribute — those exist as strings in Streamlit's bundle but
     are not emitted onto these buttons, so selectors built on them match
     nothing. Verified by reading the live DOM with a headless browser.
     Streamlit's own default is accent-coloured label text on a 10% tint, which
     leaves the selected item lower-contrast than its unselected neighbours;
     fill it and force the label white instead. The label renders as a div, so
     the descendant rule uses *. */
  div[data-testid="stButtonGroup"] button[aria-checked="true"] {{
    background: {ACCENT} !important;
    border-color: {ACCENT} !important;
    color: #FFFFFF !important;
  }}
  div[data-testid="stButtonGroup"] button[aria-checked="true"] * {{
    color: #FFFFFF !important;
    fill: #FFFFFF !important;
  }}
  div[data-testid="stButtonGroup"] button[aria-checked="false"] * {{
    color: {INK_DIM} !important;
  }}
</style>
""", unsafe_allow_html=True)


def tile(label: str, value: str, icon: str = "", color: str | None = None,
         sub: str = "") -> str:
    """A stat tile: label · value · optional sub. Icon + word carry the state;
    the coloured dot is a supplement, never the only cue."""
    dot = f'<span class="dot" style="background:{color}"></span>' if color else ""
    return (f'<div class="tile"><div class="tile-label">{dot}{icon} {label}</div>'
            f'<div class="tile-value">{value}</div>'
            f'<div class="tile-sub">{sub}</div></div>')


# --------------------------------------------------------------------------- #
# Top bar — title, settings, and the single filter row that scopes the page
# --------------------------------------------------------------------------- #
head, gear = st.columns([0.86, 0.14], vertical_alignment="center")
head.markdown("## 🩸 Glucose")

with gear.popover("⚙️ Settings", width="stretch"):
    use_mmol = st.toggle("Show in mmol/L", value=True)
    unit_label = "mmol/L" if use_mmol else "mg/dL"
    st.caption(f"Target range ({unit_label})")
    if use_mmol:
        low_in = st.number_input("Low limit", 2.2, 6.7, 3.9, 0.1, format="%.1f")
        high_in = st.number_input("High limit", 6.7, 16.7, 10.0, 0.1, format="%.1f")
        low_thr, high_thr = low_in * MMOL_FACTOR, high_in * MMOL_FACTOR
    else:
        low_thr = st.number_input("Low limit", 40, 120, 70, 1)
        high_thr = st.number_input("High limit", 120, 300, 180, 1)
    st.divider()
    # Trio uploads its profile to Nightscout but not its preferences (dynISF
    # adjustmentFactor, SMB/UAM limits…), so the report can't see them unless
    # they're supplied here.
    st.caption("Trio settings")
    stored = trio_settings.load()
    if stored:
        when = (stored.get("updated_at") or "")[:10]
        st.success(f"{len(stored['settings'])} settings loaded "
                   f"from {stored['source']}{' · ' + when if when else ''}")
    else:
        st.info("Not supplied — the report will say so rather than guess.")
    upload = st.file_uploader("Trio settings export",
                             type=["xlsx", "xls", "csv", "json"],
                             label_visibility="collapsed",
                             help="Export your settings from Trio and drop the "
                                  "file here. Everything in it is passed to the "
                                  "report, so the analysis sees actual values.")
    if upload is not None:
        parsed = trio_settings.parse_upload(upload.name, upload.getvalue())
        if parsed.get("error"):
            st.error(parsed["error"])
        elif stored is None or parsed["settings"] != stored.get("settings"):
            trio_settings.store(parsed["settings"], upload.name)
            run_analysis.clear()
            st.rerun()
    if stored:
        with st.expander("Make it permanent / remove"):
            st.caption("Streamlit Cloud wipes local files when the container "
                       "restarts. To keep these settings for good, paste this "
                       "into App settings → Secrets:")
            st.code('TRIO_SETTINGS = """'
                    + json.dumps(stored["settings"], indent=1) + '"""',
                    language="toml")
            if st.button("Remove stored settings", width="stretch"):
                trio_settings.clear()
                run_analysis.clear()
                st.rerun()

    st.divider()
    do_backfill = st.button("🔄 Rebuild full history", width="stretch",
                            help="Re-fetches a full year from Nightscout. Slow "
                                 "(~105k readings) — only needed if history "
                                 "looks incomplete. Normal updates are "
                                 "incremental and automatic.")

if low_thr >= high_thr:
    st.error("Low limit must be below the high limit — check Settings.")
    st.stop()

if not os.environ.get("NIGHTSCOUT_URL"):
    st.warning("NIGHTSCOUT_URL is not set — add it to the app secrets. Data "
               "refresh and reports will fail until then.")

def clear_data_caches() -> None:
    load_readings.clear()
    run_analysis.clear()
    newest_reading.clear()


if do_backfill:
    with st.spinner("Re-fetching a full year of readings…"):
        proc = run_cgm("refresh", "--days", "365")
    if proc.returncode == 0:
        clear_data_caches()
        backfill_history.clear()
        sync_recent.clear()
        st.success("Full history rebuilt.")
    else:
        st.error("Rebuild failed.")
        st.code((proc.stderr or proc.stdout).strip()[:1000])

# Date range: presets in one row above everything they scope, so every number
# on the page always describes the same slice. Switching range is a local SQL
# query against history we already hold — no network, so it's instant.
RANGES = {"1d": 1, "7d": 7, "14d": 14, "30d": 30, "90d": 90, "6m": 180, "1y": 365}
# Narrow action columns: a stretched button in a wide column balloons to a slab
# that dwarfs the compact range control beside it.
row_l, row_r = st.columns([0.9, 0.1], vertical_alignment="center")
# Natural width, left-aligned — the size comes from the CSS padding above rather
# than from stretching across the page. Add width="stretch" here to go full-width
# again if that reads better on a big monitor.
picked = row_l.segmented_control("Date range", list(RANGES), default="30d",
                                 label_visibility="collapsed")
days = RANGES.get(picked or "30d", 30)
sync_now = row_r.button("⟳ Now", width="stretch",
                        help="Check Nightscout for new readings right now.")

# Keeping data current: an incremental top-up on every load (cheap), and a full
# backfill only when there's no local history to extend.
if os.environ.get("NIGHTSCOUT_URL"):
    if sync_now:
        sync_recent.clear()
        clear_data_caches()
    with st.spinner("Checking for new readings…"):
        res = sync_recent("tick")

    if res.get("status") == "backfill_needed":
        with st.spinner("First run here — fetching a year of history…"):
            rc, msg = backfill_history("cold")
        clear_data_caches()
        if rc != 0:
            st.error("Initial data fetch failed.")
            st.code(msg)
    elif res.get("error"):
        st.warning(f"Could not reach Nightscout — showing stored data. "
                   f"({res['error']})")
    elif res.get("new"):
        clear_data_caches()   # new rows landed; recompute against them
        if res.get("truncated"):
            st.info(f"Caught up {res['new']:,} readings — there may be more. "
                    "Press ⟳ again, or rebuild full history in Settings.")


# --------------------------------------------------------------------------- #
# Header + KPIs + glucose chart
# --------------------------------------------------------------------------- #
st.caption(f"🕒 Latest reading {ago(newest_reading(db_mtime()))}")

df = load_readings(days, db_mtime())
if df.empty:
    st.warning("No readings in the local database yet. Open **⚙️ Settings → "
               "Refresh all history** once NIGHTSCOUT_URL is configured.")
    st.stop()

kpis = compute_kpis(df, low_thr, high_thr)
low_disp, high_disp = to_display(low_thr, use_mmol), to_display(high_thr, use_mmol)
span = f"{df['time'].min():%d %b} – {df['time'].max():%d %b %Y}"

# Hero — the one number this dashboard leads with. The value wears ink, not the
# data colour; the coloured dot beside it carries the identity.
st.markdown(
    f'<div class="hero">'
    f'<div class="hero-label">Time in range · last {days} days</div>'
    f'<div><span class="hero-value">{kpis["tir_pct"]}%</span>'
    f'<span class="chip"><span class="dot" style="background:{GOOD}"></span>'
    f'in range {low_disp}–{high_disp} {unit_label}</span></div></div>',
    unsafe_allow_html=True)


def render_range_bar(k: dict) -> None:
    """The clinical TIR bar — share of time low / in range / high in one line."""
    segs = [("Low", k["low_pct"], CRITICAL, "▼"),
            ("In range", k["tir_pct"], GOOD, "●"),
            ("High", k["high_pct"], WARNING, "▲")]
    fig = go.Figure()
    for name, val, color, icon in segs:
        fig.add_trace(go.Bar(
            x=[val], y=["tir"], orientation="h", name=f"{icon} {name}",
            # A 2px line in the page colour is how Plotly renders the surface
            # gap that separates touching segments.
            marker=dict(color=color, line=dict(color="#0B1220", width=2)),
            hovertemplate=f"{icon} {name}: %{{x:.1f}}%<extra></extra>",
        ))
    # bargap keeps the bar thin (≤24px) — a fat saturated block reads loud.
    fig.update_layout(barmode="stack", bargap=0.78)
    style_fig(fig, 104, legend=True, hover="closest")
    # Legend reads left-to-right in the same order as the segments.
    fig.update_layout(legend=dict(orientation="h", y=-0.5, traceorder="normal",
                                 font=dict(color=INK_MUTED)))
    fig.update_xaxes(visible=False, range=[0, 100])
    fig.update_yaxes(visible=False)
    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": False})


render_range_bar(kpis)

t1, t2, t3, t4 = st.columns(4)
t1.markdown(tile("Above range", f"{kpis['high_pct']}%", "▲", WARNING,
                 f"over {high_disp} {unit_label}"), unsafe_allow_html=True)
t2.markdown(tile("Below range", f"{kpis['low_pct']}%", "▼", CRITICAL,
                 f"under {low_disp} {unit_label} · aim below 4%"),
            unsafe_allow_html=True)
t3.markdown(tile("GMI (est. A1C)", f"{kpis['gmi']}%",
                 sub=f"average {to_display(kpis['mean_mgdl'], use_mmol)} {unit_label}"),
            unsafe_allow_html=True)
t4.markdown(tile("Variability (CV)", f"{kpis['cv']}%",
                 sub=f"{kpis['readings']:,} readings · {span}"),
            unsafe_allow_html=True)

st.markdown("#### Glucose over time")
plot_df = df.copy()
plot_df["display"] = plot_df["sgv"] / MMOL_FACTOR if use_mmol else plot_df["sgv"]

fig = go.Figure()
fig.add_hrect(y0=low_disp, y1=high_disp, fillcolor=BAND_FILL, line_width=0)

# Every reading drawn raw is only readable over a day or two — a month of
# 5-minute data is ~8,600 points, which overplots into a solid scribble and
# hides the very pattern the long view is for. Past that, aggregate into a
# median line with an IQR band: fewer marks, and the shape of the day survives.
BUCKET = {2: None, 7: "1h"}
rule = next((v for k, v in BUCKET.items() if days <= k), "1D")
if rule is None:
    fig.add_trace(go.Scatter(
        x=plot_df["time"], y=plot_df["display"], mode="lines", name="Glucose",
        line=dict(color=SERIES, width=2),
        hovertemplate="%{x|%a %d %b %H:%M}<br>%{y:.1f} " + unit_label
                      + "<extra></extra>"))
    sub = "every reading"
    legend = False
else:
    buckets = plot_df.set_index("time")["display"].resample(rule)
    med, p25, p75 = buckets.median(), buckets.quantile(.25), buckets.quantile(.75)
    med = med.dropna()
    # Plain datetimes and floats — pandas Timestamps aren't serializable by
    # every JSON encoder in the chain, and this removes the dependency.
    x = med.index.to_pydatetime().tolist()
    lo = p25.reindex(med.index).astype(float).tolist()
    hi = p75.reindex(med.index).astype(float).tolist()
    fig.add_trace(go.Scatter(
        x=x + x[::-1], y=hi + lo[::-1],
        fill="toself", fillcolor="rgba(57,135,229,0.16)", line_width=0,
        name="25–75%", hoverinfo="skip"))
    fig.add_trace(go.Scatter(
        x=x, y=med.astype(float).tolist(), mode="lines", name="Median",
        line=dict(color=SERIES, width=2),
        hovertemplate="%{x|%a %d %b %H:%M}<br>%{y:.1f} " + unit_label
                      + "<extra></extra>"))
    sub = {"1h": "hourly median, 25–75% band",
           "1D": "daily median, 25–75% band"}[rule]
    legend = True

# Solid hairlines, labelled — the thresholds read without relying on colour.
# xshift keeps the label clear of the y-axis, which otherwise clips it.
for y, text, color, pos in ((high_disp, f"high {high_disp}", WARNING, "top left"),
                            (low_disp, f"low {low_disp}", CRITICAL, "bottom left")):
    fig.add_hline(y=y, line=dict(color=color, width=1), annotation_text=text,
                  annotation_position=pos, annotation_xshift=10,
                  annotation_font=dict(color=INK_MUTED, size=11))
style_fig(fig, 420, ylab=f"Glucose ({unit_label})", legend=legend)
st.plotly_chart(fig, width="stretch")
st.caption(f"{sub} · {span} · {kpis['readings']:,} readings")


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
    f.add_hrect(y0=low_disp, y1=high_disp, fillcolor=BAND_FILL, line_width=0)
    f.add_trace(go.Scatter(x=x + x[::-1], y=p95 + p05[::-1], fill="toself",
                fillcolor="rgba(57,135,229,0.12)", line_width=0, name="5–95%",
                hoverinfo="skip"))
    f.add_trace(go.Scatter(x=x + x[::-1], y=p75 + p25[::-1], fill="toself",
                fillcolor="rgba(57,135,229,0.28)", line_width=0, name="25–75%",
                hoverinfo="skip"))
    f.add_trace(go.Scatter(x=x, y=med, mode="lines", name="Median",
                line=dict(color=SERIES, width=2)))
    style_fig(f, 360, ylab=f"Glucose ({unit_label})", legend=True)
    f.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(f, width="stretch")


def render_hourly_oor(hours: dict) -> None:
    hs, cells = _hour_series(hours)
    high = [c["high"] for c in cells]
    low = [-c["low"] for c in cells]  # below the axis
    f = go.Figure()
    f.add_trace(go.Bar(x=hs, y=high, name="▲ High", marker_color=WARNING))
    f.add_trace(go.Bar(x=hs, y=low, name="▼ Low", marker_color=CRITICAL))
    f.update_layout(barmode="relative", bargap=0.35)
    style_fig(f, 320, ylab="% out of range", legend=True, hover="closest")
    f.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(f, width="stretch")


def render_heatmap(heat: dict) -> None:
    days_lbl = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    z = [[None] * 24 for _ in range(7)]
    for key, cell in heat.items():
        d, h = map(int, key.split("_"))
        z[d][h] = round(100 - cell["tir"], 1)  # % out of range
    # One-hue sequential ramp (the palette's blue), oriented for a dark surface:
    # the darkest step means "near zero" and recedes toward the background, so
    # the worst cells are the brightest. Never a rainbow; the colorbar is the
    # scale legend.
    blue_ramp = [[0.0, "#0d366b"], [0.3, "#256abf"], [0.6, "#3987e5"],
                 [1.0, "#b7d3f6"]]
    f = go.Figure(go.Heatmap(
        z=z, x=list(range(24)), y=days_lbl, colorscale=blue_ramp,
        xgap=2, ygap=2,  # 2px surface gap between cells
        colorbar=dict(title="% out<br>of range", outlinewidth=0,
                      tickfont=dict(color=INK_MUTED)),
        hovertemplate="%{y} %{x}:00<br>%{z}% out of range<extra></extra>"))
    style_fig(f, 300, hover="closest")
    f.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(f, width="stretch")


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
    g.add_hrect(y0=low_disp, y1=high_disp, fillcolor=BAND_FILL, line_width=0)
    g.add_trace(go.Scatter(x=t + t[::-1], y=p75 + p25[::-1], fill="toself",
                fillcolor="rgba(57,135,229,0.22)", line_width=0, name="IQR",
                hoverinfo="skip"))
    g.add_trace(go.Scatter(x=t, y=med, mode="lines", name="Median glucose",
                line=dict(color=SERIES, width=2)))
    style_fig(g, 300, ylab=f"Glucose ({unit_label})", legend=True)
    g.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(g, width="stretch")

    # Two charts, not one with two y-axes: a dual axis aligns two unrelated
    # scales arbitrarily and invents a correlation that isn't in the data.
    # Basal (U/hr) and bolus (U) are both insulin and share one axis honestly;
    # carbs are grams, so they get their own chart.
    st.markdown("**Average day — insulin**")
    ins = go.Figure()
    ins.add_trace(go.Bar(x=t, y=bolus, name="Bolus / SMB (U)",
                  marker_color="#9085E9", opacity=0.75))
    ins.add_trace(go.Scatter(x=t, y=basal_s, mode="lines", name="Basal, scheduled",
                  line=dict(color="#199E70", width=1.5, dash="dot")))
    ins.add_trace(go.Scatter(x=t, y=basal_d, mode="lines", name="Basal, delivered",
                  line=dict(color="#199E70", width=2)))
    ins.update_layout(bargap=0.35)
    style_fig(ins, 300, ylab="Insulin (U/hr basal · U bolus)", legend=True)
    ins.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(ins, width="stretch")

    st.markdown("**Average day — carbs**")
    cb = go.Figure()
    cb.add_trace(go.Bar(x=t, y=carbs, name="Carbs (g)", marker_color="#D95926",
                 opacity=0.85))
    cb.update_layout(bargap=0.35)
    style_fig(cb, 220, ylab="Carbs (g)", hover="closest")
    cb.update_xaxes(title_text="Hour of day", dtick=3)
    st.plotly_chart(cb, width="stretch")

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

if st.button("Generate full report", type="primary", width="stretch"):
    with st.spinner("Computing analysis (glucose + treatments + Loopalyzer)…"):
        result = run_analysis(days, db_mtime(), str(load_profile("hourly").get("units")))

    # Hand the report the exported Trio preferences alongside the computed
    # stats, so it reasons about real values instead of naming levers blind.
    trio = trio_settings.load()
    if trio:
        result["trio_settings"] = trio["settings"]

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
        # Streamed, so the report appears as it's written instead of after the
        # whole generation (the model thinks before its first word).
        ai = report_ai.ReportStream(result)
        with st.spinner("Claude is reading your data…"):
            st.write_stream(ai)
        if ai.ok:
            st.caption(f"Generated by {ai.model}.")
        else:
            st.warning(ai.error or "AI report unavailable.")
