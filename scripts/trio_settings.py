#!/usr/bin/env python3
"""Load a Trio settings export so the AI report can see the full picture.

Why this exists: Trio uploads the *profile* to Nightscout (basal, ISF, carb
ratios, targets) but not its preferences — dynISF adjustmentFactor, SMB and UAM
limits, maxUAMSMBBasalMinutes, thresholds, and so on. Those only exist inside the
app, which can export them. Without them the report has to reason about levers
whose values it cannot see.

Design notes:
  - Format-tolerant on purpose. The exact shape of a Trio export isn't assumed:
    .json, .csv, .xlsx and .xls all work, key/value sheets and wide tables alike.
    Everything is flattened to plain key -> value pairs (plus row records for
    schedule-style tables) and handed to the model as-is; Claude reads setting
    names like "adjustmentFactor" perfectly well without a hand-written mapping,
    and a mapping would rot the moment Trio renames a field.
  - Stored in the same SQLite file as the readings, so it survives reruns. That
    file is ephemeral on Streamlit Cloud, so TRIO_SETTINGS (a JSON blob in the
    app secrets) is supported as a permanent alternative.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "cgm_data.db"

# Guard against a pathological upload filling the prompt.
MAX_ITEMS = 800
MAX_VALUE_CHARS = 400


def _clean(value):
    """Normalise a cell to something JSON-serialisable and compact."""
    if value is None:
        return None
    # pandas fills blank cells with float NaN, which is numeric — so this test
    # has to come before the numeric branch or empty cells arrive as "nan".
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    text = str(value).strip()
    if text.lower() in ("nan", "nat", "none", ""):
        return None
    return text[:MAX_VALUE_CHARS]


def _flatten(obj, prefix: str = "") -> dict:
    """Flatten nested dicts/lists into dotted keys."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(obj, list):
        # A list of scalars stays inline; a list of dicts becomes indexed keys.
        if all(not isinstance(i, (dict, list)) for i in obj):
            out[prefix] = [_clean(i) for i in obj]
        else:
            for i, item in enumerate(obj):
                out.update(_flatten(item, f"{prefix}[{i}]"))
    else:
        cleaned = _clean(obj)
        if cleaned is not None:
            out[prefix] = cleaned
    return out


_VALUE_COLS = {"value", "waarde", "setting value", "val"}
_UNIT_COLS = {"unit", "units", "eenheid"}


def _named_export(df) -> dict | None:
    """Parse an export with real column headers and a Value column.

    Trio's own CSV looks like:
        Setting Category,Subcategory,Setting Name,Value,Unit
        Algorithm,Dynamic Settings,Sigmoid Adjustment Factor,80,%
    Everything left of Value becomes a dotted key and Unit is appended to the
    value, so the model reads
    "Algorithm.Dynamic Settings.Sigmoid Adjustment Factor = 80 %".
    Returns None when there's no Value column, so callers can fall back.
    """
    cols = {str(c).strip().lower(): c for c in df.columns}
    value_col = next((cols[k] for k in _VALUE_COLS if k in cols), None)
    if value_col is None:
        return None
    unit_col = next((cols[k] for k in _UNIT_COLS if k in cols), None)
    key_cols = [c for c in df.columns if c not in (value_col, unit_col)]
    if not key_cols:
        return None

    out: dict = {}
    for row in df.to_dict(orient="records"):
        parts = [str(_clean(row[c])) for c in key_cols if _clean(row[c]) is not None]
        value = _clean(row[value_col])
        if not parts or value is None:
            continue
        if unit_col is not None:
            unit = _clean(row[unit_col])
            if unit:
                value = f"{value} {unit}"
        key = ".".join(parts)
        # Repeated keys (two presets sharing a name) get suffixed rather than
        # silently overwriting each other.
        if key in out and out[key] != value:
            n = 2
            while f"{key}#{n}" in out:
                n += 1
            key = f"{key}#{n}"
        out[key] = value
    return out or None


def _from_frame(df, sheet: str) -> dict:
    """Turn one sheet/CSV into settings.

    Two columns reads as key/value (how settings exports usually look); anything
    wider is a table — a basal schedule, say — and is kept as row records so the
    time-of-day structure survives.
    """
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return {}
    prefix = "" if sheet in ("", "Sheet1", "settings", "Settings") else f"{sheet}."

    if df.shape[1] == 2:
        rows = list(df.itertuples(index=False))
        # Drop a header row ("setting | value", "time | rate") so it doesn't land
        # in the output as a setting of its own. Only when the first row's value
        # is text but most of the rest are numbers.
        if len(rows) > 2:
            def _num(v):
                try:
                    float(str(v).strip())
                    return True
                except (TypeError, ValueError):
                    return False
            rest = [r[1] for r in rows[1:]]
            if not _num(rows[0][1]) and sum(map(_num, rest)) > len(rest) * 0.6:
                rows = rows[1:]
        out = {}
        for key, val in rows:
            k, v = _clean(key), _clean(val)
            if k is not None and v is not None:
                out[f"{prefix}{k}"] = v
        return out

    records = []
    for row in df.to_dict(orient="records"):
        rec = {str(k): _clean(v) for k, v in row.items() if _clean(v) is not None}
        if rec:
            records.append(rec)
    return {f"{prefix or sheet or 'table'}rows".rstrip("."): records} if records else {}


def parse_upload(filename: str, data: bytes) -> dict:
    """Parse an uploaded export. Returns {"settings": {...}} or {"error": str}."""
    name = (filename or "").lower()
    try:
        if name.endswith(".json"):
            settings = _flatten(json.loads(data.decode("utf-8")))
        elif name.endswith((".csv", ".xlsx", ".xls")):
            import pandas as pd
            # Read WITH a header row first: Trio's export is a named table
            # (Setting Category, Subcategory, Setting Name, Value, Unit), and
            # header=None would turn those names into a data row.
            if name.endswith(".csv"):
                frames = {"": pd.read_csv(io.BytesIO(data))}
            else:
                frames = pd.read_excel(io.BytesIO(data), sheet_name=None)
            settings = {}
            for sheet, df in frames.items():
                named = _named_export(df)
                if named:
                    settings.update(named)
                    continue
                # No Value column, so the header guess was probably wrong —
                # re-read headerless and treat it as key/value pairs or rows.
                if name.endswith(".csv"):
                    raw = pd.read_csv(io.BytesIO(data), header=None)
                else:
                    raw = pd.read_excel(io.BytesIO(data), sheet_name=sheet,
                                        header=None)
                settings.update(_from_frame(raw, str(sheet)))
        else:
            return {"error": f"Unsupported file type: {filename}"}
    except ImportError:
        return {"error": "Reading Excel needs the openpyxl package."}
    except Exception as e:                      # malformed file, bad encoding…
        return {"error": f"Could not read {filename}: {e}"}

    if not settings:
        return {"error": f"No settings found in {filename}."}
    if len(settings) > MAX_ITEMS:
        settings = dict(list(settings.items())[:MAX_ITEMS])
    return {"settings": settings}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        name TEXT PRIMARY KEY,
        json TEXT,
        source TEXT,
        updated_at TEXT
    )""")
    conn.commit()
    return conn


def store(settings: dict, source: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings VALUES ('trio', ?, ?, ?)",
            (json.dumps(settings), source,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


def load() -> dict | None:
    """Stored settings, else the TRIO_SETTINGS secret, else None.

    Shape: {"settings": {...}, "source": str, "updated_at": str}
    """
    if DB_PATH.exists():
        conn = _connect()
        try:
            row = conn.execute("SELECT json, source, updated_at FROM app_settings "
                               "WHERE name = 'trio'").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row and row[0]:
            try:
                return {"settings": json.loads(row[0]), "source": row[1],
                        "updated_at": row[2]}
            except ValueError:
                pass

    blob = os.environ.get("TRIO_SETTINGS")
    if blob:
        try:
            parsed = json.loads(blob)
        except ValueError:
            return None
        settings = parsed.get("settings") if isinstance(parsed, dict) and \
            "settings" in parsed else parsed
        if isinstance(settings, dict) and settings:
            return {"settings": _flatten(settings), "source": "app secrets",
                    "updated_at": None}
    return None


def clear() -> None:
    if not DB_PATH.exists():
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM app_settings WHERE name = 'trio'")
        conn.commit()
    finally:
        conn.close()
