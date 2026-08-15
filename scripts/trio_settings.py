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
        elif name.endswith(".csv"):
            import pandas as pd
            settings = _from_frame(pd.read_csv(io.BytesIO(data), header=None), "")
        elif name.endswith((".xlsx", ".xls")):
            import pandas as pd
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
            settings = {}
            for sheet, df in sheets.items():
                settings.update(_from_frame(df, str(sheet)))
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
