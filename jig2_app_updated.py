"""
JIG 2 – O₂ SENSOR AGING AND DEGRADATION MONITORING
Live Streamlit dashboard reading directly from Google Sheets.

Study protocol:
  • Study window : 21 Oct 2026 (Day 0) → 11 Dec 2026 (Day 51), 52 days inclusive
  • Fixed baselines: O₂ sensors = 98.28 mV; air sensors = 21.0564 mV
  • Valid-data limits: S-9, S-10 ≤ 110 mV · S-11 … S-16 ≤ 30 mV
  • Degradation  : ((Baseline − Daily Average) / Baseline) × 100
  • Dead detection: raw readings only; two-stage 1,000-reading verification
"""

from __future__ import annotations

import io
import math
import re
import threading
import warnings
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# =============================================================================
# 1. FIXED STUDY CONFIGURATION
# =============================================================================
SHEET_ID = "1g1oh-CZgnkgBJRayexm8pvv_jEKrHjIElXNVXM95p9k"
WORKSHEET_NAME = "Sheet1"

STUDY_START = date(2026, 10, 21)          # Day 0
STUDY_DAYS = 52                           # inclusive
STUDY_END = STUDY_START + timedelta(days=STUDY_DAYS - 1)   # 11 Dec 2026 (Day 51)
BASELINE_DATE = STUDY_START               # never changes

REFRESH_SECONDS = 60                      # browser auto-refresh interval
SHEET_CACHE_TTL = 50                      # Google Sheets cache (< refresh interval)
STALE_AFTER_MINUTES = 10                  # warn if logger stops writing

APP_TITLE = "JIG 2 – O₂ SENSOR AGING AND DEGRADATION MONITORING"

# Sheet columns A..J, read by position (header text is not relied upon)
RAW_COLUMNS = ["Timestamp", "Temperature"] + [f"Sensor {i}" for i in range(1, 9)]

SENSORS = [
    # name   sheet column  col  physical ID    exposure              group  threshold  colour
    ("S-9",  "Sensor 1",  "C", "S-16-23%", "With O₂ – 100%",    "O2",  110.0, "#D55E00"),
    ("S-10", "Sensor 2",  "D", "S-02-23%", "With O₂ – 100%",    "O2",  110.0, "#E69F00"),
    ("S-11", "Sensor 3",  "E", "S-21-23%", "Without O₂ – Air",  "AIR",  30.0, "#0072B2"),
    ("S-12", "Sensor 4",  "F", "S-08-23%", "Without O₂ – Air",  "AIR",  30.0, "#009E73"),
    ("S-13", "Sensor 5",  "G", "S-07-23%", "Without O₂ – Air",  "AIR",  30.0, "#56B4E9"),
    ("S-14", "Sensor 6",  "H", "S-03-23%", "Without O₂ – Air",  "AIR",  30.0, "#CC79A7"),
    ("S-15", "Sensor 7",  "I", "S-23-23%", "Without O₂ – Air",  "AIR",  30.0, "#7B61FF"),
    ("S-16", "Sensor 8",  "J", "S-24-23%", "Without O₂ – Air",  "AIR",  30.0, "#8C8C8C"),
]
SENSOR_NAMES = [s[0] for s in SENSORS]
SHEET_TO_SENSOR = {s[1]: s[0] for s in SENSORS}
SHEET_COL = {s[0]: s[2] for s in SENSORS}
PHYSICAL = {s[0]: s[3] for s in SENSORS}
EXPOSURE = {s[0]: s[4] for s in SENSORS}
GROUP = {s[0]: s[5] for s in SENSORS}
THRESHOLD = {s[0]: s[6] for s in SENSORS}
COLOR = {s[0]: s[7] for s in SENSORS}
O2_SENSORS = [s for s in SENSOR_NAMES if GROUP[s] == "O2"]
AIR_SENSORS = [s for s in SENSOR_NAMES if GROUP[s] == "AIR"]

# Fixed Jig 2 baselines and raw-reading dead-sensor verification thresholds.
O2_BASELINE_MV = 98.28
AIR_BASELINE_MV = 21.0564
AIR_REDUCTION_PERCENT = 30.77
AIR_DEAD_THRESHOLD_MV = AIR_BASELINE_MV * (1 - AIR_REDUCTION_PERCENT / 100)
O2_DEAD_TRIGGER_MV = 69.0
O2_RECOVERY_TARGET_MV = 70.0
RECOVERY_WINDOW_READINGS = 1000
NORMAL_CONFIRM_READINGS = 1000


SHEETS_EPOCH = pd.Timestamp("1899-12-30")   # Google Sheets / Excel serial date origin


def label(s: str) -> str:
    return f"{s} ({PHYSICAL[s]})"


# =============================================================================
# 2. OPTIONAL SETTINGS FROM SECRETS
# =============================================================================
def _setting(key: str, default: str) -> str:
    try:
        return str(st.secrets.get("app_settings", {}).get(key, default))
    except Exception:
        return default


DISPLAY_TZ = ZoneInfo(_setting("display_timezone", "Asia/Kolkata"))
TIMESTAMP_ORDER = _setting("timestamp_order", "auto").lower()   # auto | dayfirst | monthfirst


# =============================================================================
# 3. PARSING HELPERS (pure functions – no Streamlit calls)
# =============================================================================
_UNIT_RE = re.compile(r"\s*(mv|°c|degc|c)\s*$", re.IGNORECASE)
_DMY_RE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


def parse_number(v) -> float:
    """Convert one sheet cell to float; anything non-numeric becomes NaN."""
    if v is None or isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else math.nan
    if isinstance(v, str):
        t = _UNIT_RE.sub("", v.strip())
        if not t:
            return math.nan
        if "," in t and "." not in t:
            t = t.replace(",", ".")          # decimal comma
        try:
            f = float(t)
        except ValueError:
            return math.nan
        return f if math.isfinite(f) else math.nan
    return math.nan


def _detect_dayfirst(strings: list[str]) -> bool:
    if TIMESTAMP_ORDER == "dayfirst":
        return True
    if TIMESTAMP_ORDER == "monthfirst":
        return False
    for s in strings:
        m = _DMY_RE.match(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return True
            if b > 12:
                return False
    return True   # ambiguous → day-first (India / dd/mm/yyyy)


def parse_timestamps(raw: pd.Series) -> pd.Series:
    """Parse column A. Accepts Sheets serial numbers and text date-times."""
    vals = raw.tolist()
    out = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")

    serial_idx, serial_val, str_idx, str_val = [], [], [], []
    for i, v in zip(raw.index, vals):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float, np.integer, np.floating)):
            if 20000 < float(v) < 100000:            # plausible date serial
                serial_idx.append(i)
                serial_val.append(float(v))
        elif isinstance(v, str) and re.search(r"\d", v):
            str_idx.append(i)
            str_val.append(v.strip())

    if serial_idx:
        ts = SHEETS_EPOCH + pd.to_timedelta(serial_val, unit="D")
        out.loc[serial_idx] = pd.DatetimeIndex(ts).round("s")

    if str_idx:
        dayfirst = _detect_dayfirst(str_val)
        s = pd.Series(str_val, index=str_idx)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(s, dayfirst=dayfirst, errors="coerce")
            failed = parsed.isna()
            if failed.any():
                parsed.loc[failed] = pd.to_datetime(
                    s[failed], format="mixed", dayfirst=dayfirst, errors="coerce"
                )
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)
        out.loc[str_idx] = parsed.astype("datetime64[ns]")
    return out


# =============================================================================
# 4. CORE CALCULATIONS (pure functions – unit-testable)
# =============================================================================
def detect_dead_sensors(raw_data: pd.DataFrame) -> pd.DataFrame:
    """Run independent, sequential dead-sensor checks on raw numeric readings."""
    results = []
    for sensor in SENSOR_NAMES:
        if GROUP[sensor] == "O2":
            trigger, target = O2_DEAD_TRIGGER_MV, O2_RECOVERY_TARGET_MV
        else:
            trigger = target = AIR_DEAD_THRESHOLD_MV

        state = "NORMAL"
        recovery_count = confirm_count = 0
        last_value = np.nan

        for value in raw_data[sensor].tolist():
            if pd.isna(value):
                continue
            value = float(value)
            last_value = value

            if state == "DEAD SENSOR":
                continue
            if state == "NORMAL":
                if value < trigger:
                    state = "RECOVERY CHECK"
                    recovery_count = confirm_count = 0
            elif state == "RECOVERY CHECK":
                recovery_count += 1
                if value >= target:
                    state = "NORMAL CHECK"
                    confirm_count = 0
                elif recovery_count >= RECOVERY_WINDOW_READINGS:
                    state = "DEAD SENSOR"
            elif state == "NORMAL CHECK":
                if value < target:
                    state = "DEAD SENSOR"
                else:
                    confirm_count += 1
                    if confirm_count >= NORMAL_CONFIRM_READINGS:
                        state = "NORMAL"
                        recovery_count = confirm_count = 0

        if state == "RECOVERY CHECK":
            progress = f"{recovery_count}/{RECOVERY_WINDOW_READINGS} recovery readings"
        elif state == "NORMAL CHECK":
            progress = f"{confirm_count}/{NORMAL_CONFIRM_READINGS} confirmation readings"
        elif state == "DEAD SENSOR":
            progress = "Verification failed"
        else:
            progress = "No active verification"

        results.append({
            "Sensor": sensor,
            "Dead Sensor Status": state,
            "Detection Trigger (mV)": trigger,
            "Recovery Target (mV)": target,
            "Verification Progress": progress,
            "Latest Raw Reading (mV)": last_value,
        })
    return pd.DataFrame(results).set_index("Sensor")


def process_values(values: list[list]) -> dict:
    """Raw sheet values → filtered readings, daily averages, counts.

    Fixed baselines are applied later by resolve_baseline(); raw values are
    retained for dead-sensor detection while filtered values drive averages.
    """
    rows = []
    for r in values or []:
        r = list(r)[:10] + [""] * max(0, 10 - len(r))
        if any(c not in ("", None) for c in r):
            rows.append(r)
    header_skipped = False
    if rows and isinstance(rows[0][0], str) and not re.search(r"\d", rows[0][0]):
        rows = rows[1:]
        header_skipped = True

    df = pd.DataFrame(rows, columns=RAW_COLUMNS, dtype=object)
    ts = parse_timestamps(df["Timestamp"]) if len(df) else pd.Series(dtype="datetime64[ns]")
    valid_ts = ts.notna()

    start_ts = pd.Timestamp(STUDY_START)
    end_ts = pd.Timestamp(STUDY_END) + pd.Timedelta(days=1)       # exclusive
    in_window = valid_ts & (ts >= start_ts) & (ts < end_ts)

    data = pd.DataFrame({"Timestamp": ts})
    data["Temperature (°C)"] = [parse_number(v) for v in df["Temperature"].tolist()]
    for sheet_col, name in SHEET_TO_SENSOR.items():
        data[name] = [parse_number(v) for v in df[sheet_col].tolist()]
    data["Sheet Row"] = np.arange(len(data)) + (2 if header_skipped else 1)

    data = data[in_window].sort_values(["Timestamp", "Sheet Row"], kind="stable").reset_index(drop=True)
    data["Date"] = data["Timestamp"].dt.normalize()
    data["Study Day"] = (data["Date"] - start_ts).dt.days

    raw = data[SENSOR_NAMES]
    thr = pd.Series(THRESHOLD)
    missing = raw.isna()
    excluded = raw.gt(thr, axis=1)          # strictly greater → equal is accepted
    valid = ~missing & ~excluded
    filtered = raw.where(valid)

    # ---- daily aggregation over the full calendar (gaps stay visible) ----
    if len(data):
        last_day = min(data["Date"].max(), pd.Timestamp(STUDY_END))
        all_dates = pd.date_range(start_ts, last_day, freq="D")
    else:
        all_dates = pd.DatetimeIndex([])
    by = data["Date"]
    daily_avg = filtered.groupby(by).mean().reindex(all_dates)
    daily_valid = valid.groupby(by).sum().reindex(all_dates, fill_value=0).astype(int)
    daily_excl = excluded.groupby(by).sum().reindex(all_dates, fill_value=0).astype(int)
    daily_missing = missing.groupby(by).sum().reindex(all_dates, fill_value=0).astype(int)
    daily_records = data.groupby(by).size().reindex(all_dates, fill_value=0).astype(int)
    daily_temp = data.groupby(by)["Temperature (°C)"].mean().reindex(all_dates)
    for frame in (daily_avg, daily_valid, daily_excl, daily_missing, daily_records, daily_temp):
        frame.index.name = "Date"

    # ---- baseline candidates: 21 Oct ONLY ----
    if start_ts in daily_avg.index:
        baseline_computed = daily_avg.loc[start_ts].astype(float)
        baseline_n = daily_valid.loc[start_ts].astype(int)
        baseline_excl = daily_excl.loc[start_ts].astype(int)
    else:
        baseline_computed = pd.Series(np.nan, index=SENSOR_NAMES, dtype=float)
        baseline_n = pd.Series(0, index=SENSOR_NAMES, dtype=int)
        baseline_excl = pd.Series(0, index=SENSOR_NAMES, dtype=int)

    # ---- latest values ----
    latest = {}
    for s in SENSOR_NAMES:
        iv = filtered[s].last_valid_index()
        ir = raw[s].last_valid_index()
        latest[s] = {
            "valid_value": filtered[s].loc[iv] if iv is not None else np.nan,
            "valid_ts": data["Timestamp"].loc[iv] if iv is not None else pd.NaT,
            "raw_value": raw[s].loc[ir] if ir is not None else np.nan,
            "raw_ts": data["Timestamp"].loc[ir] if ir is not None else pd.NaT,
            "raw_excluded": bool(excluded[s].loc[ir]) if ir is not None else False,
        }
    it = data["Temperature (°C)"].last_valid_index()
    latest_temp = (data["Temperature (°C)"].loc[it], data["Timestamp"].loc[it]) if it is not None else (np.nan, pd.NaT)

    return {
        "data": data,
        "filtered": filtered,
        "valid": valid,
        "excluded": excluded,
        "missing": missing,
        "daily_avg": daily_avg,
        "daily_valid": daily_valid,
        "daily_excl": daily_excl,
        "daily_missing": daily_missing,
        "daily_records": daily_records,
        "daily_temp": daily_temp,
        "baseline_computed": baseline_computed,
        "baseline_n": baseline_n,
        "baseline_excl": baseline_excl,
        "latest": latest,
        "dead_sensor": detect_dead_sensors(data),
        "latest_temp": latest_temp,
        "latest_ts": data["Timestamp"].max() if len(data) else pd.NaT,
        "sheet_rows": len(df),
        "header_skipped": header_skipped,
        "invalid_ts_rows": int((~valid_ts).sum()),
        "outside_window_rows": int((valid_ts & ~in_window).sum()),
        "records": len(data),
        "total_readings": int((~missing).to_numpy().sum()),
        "total_valid": int(valid.to_numpy().sum()),
        "total_excluded": int(excluded.to_numpy().sum()),
    }


def baseline_is_valid(b) -> bool:
    return b is not None and isinstance(b, (int, float, np.floating)) and math.isfinite(b) and b > 0


def degradation(avg, baseline):
    """((Baseline − Avg) / Baseline) × 100; NaN where either value is missing/invalid."""
    base = pd.Series(baseline, dtype=float)
    base = base.where(base.gt(0) & np.isfinite(base))
    return (base - avg) / base * 100.0


def build_daily_long(res: dict, baseline: pd.Series) -> pd.DataFrame:
    deg = degradation(res["daily_avg"], baseline)
    frames = []
    for s in SENSOR_NAMES:
        frames.append(pd.DataFrame({
            "Date": res["daily_avg"].index,
            "Study Day": (res["daily_avg"].index - pd.Timestamp(STUDY_START)).days,
            "Sensor": s,
            "Physical ID": PHYSICAL[s],
            "Exposure": EXPOSURE[s],
            "Threshold (mV)": THRESHOLD[s],
            "Daily Avg (mV)": res["daily_avg"][s].to_numpy(),
            "Degradation (%)": deg[s].to_numpy(),
            "Valid Readings": res["daily_valid"][s].to_numpy(),
            "Excluded Readings": res["daily_excl"][s].to_numpy(),
            "Missing/Invalid": res["daily_missing"][s].to_numpy(),
            "Daily Avg Temp (°C)": res["daily_temp"].to_numpy(),
        }))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["Sensor"] = pd.Categorical(out["Sensor"], categories=SENSOR_NAMES, ordered=True)
    return out.sort_values(["Date", "Sensor"]).reset_index(drop=True)


def build_latest_table(res: dict, baseline: pd.Series, baseline_status: dict) -> pd.DataFrame:
    deg = degradation(res["daily_avg"], baseline)
    rows = []
    for s in SENSOR_NAMES:
        col = res["daily_avg"][s].dropna()
        last_day = col.index.max() if len(col) else pd.NaT
        rows.append({
            "Sensor": s,
            "Physical ID": PHYSICAL[s],
            "Exposure": EXPOSURE[s],
            "Baseline Date": "Fixed common baseline",
            "Baseline (mV)": baseline[s],
            "Baseline Status": baseline_status[s],
            "Latest Avg Date": last_day.strftime("%d %b %Y") if pd.notna(last_day) else "—",
            "Latest Average (mV)": col.iloc[-1] if len(col) else np.nan,
            "Degradation (%)": deg[s].loc[last_day] if pd.notna(last_day) else np.nan,
            "Latest Valid Reading (mV)": res["latest"][s]["valid_value"],
            "Latest Reading Time": res["latest"][s]["valid_ts"],
            "Threshold (mV)": THRESHOLD[s],
            "Dead Sensor Status": res["dead_sensor"].loc[s, "Dead Sensor Status"],
            "Verification Progress": res["dead_sensor"].loc[s, "Verification Progress"],
        })
    return pd.DataFrame(rows)


def exclusion_summary(res: dict, mask: pd.Series | None = None) -> pd.DataFrame:
    valid, excl, miss = res["valid"], res["excluded"], res["missing"]
    raw = res["data"][SENSOR_NAMES]
    if mask is not None:
        valid, excl, miss, raw = valid[mask], excl[mask], miss[mask], raw[mask]
    rows = []
    for s in SENSOR_NAMES:
        n_num = int((~miss[s]).sum())
        n_ex = int(excl[s].sum())
        rows.append({
            "Sensor": s,
            "Physical ID": PHYSICAL[s],
            "Exposure": EXPOSURE[s],
            "Threshold (mV)": THRESHOLD[s],
            "Records": int(len(raw)),
            "Numeric Readings": n_num,
            "Valid Readings": int(valid[s].sum()),
            "Excluded (> threshold)": n_ex,
            "Missing/Invalid": int(miss[s].sum()),
            "Excluded (%)": (n_ex / n_num * 100) if n_num else np.nan,
            "Max Raw (mV)": raw[s].max() if n_num else np.nan,
        })
    return pd.DataFrame(rows)


def filtered_raw_table(res: dict) -> pd.DataFrame:
    d = res["data"]
    out = pd.DataFrame({
        "Timestamp": d["Timestamp"],
        "Study Day": d["Study Day"],
        "Temperature (°C)": d["Temperature (°C)"],
    })
    for s in SENSOR_NAMES:
        out[f"{s} (mV)"] = res["filtered"][s]
    excl = res["excluded"]
    names = np.array(SENSOR_NAMES)
    out["Excluded Sensors"] = [", ".join(names[r]) for r in excl[SENSOR_NAMES].to_numpy()]
    out["Sheet Row"] = d["Sheet Row"]
    return out


# =============================================================================
# 5. GOOGLE SHEETS ACCESS (cached, short TTL)
# =============================================================================
@st.cache_resource(show_spinner=False)
def get_gspread_client():
    import gspread
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError(
            "Streamlit secret [gcp_service_account] is missing. "
            "Add your service-account JSON to the app secrets (see README)."
        )
    return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))


@st.cache_resource(ttl=SHEET_CACHE_TTL, show_spinner=False, max_entries=1)
def fetch_sheet_values() -> tuple[list[list], str]:
    """Pull every row of Sheet1. Numbers unformatted (full precision),
    date cells as serial numbers (unambiguous)."""
    from gspread.utils import DateTimeOption, ValueRenderOption

    ws = get_gspread_client().open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    values = ws.get_all_values(
        value_render_option=ValueRenderOption.unformatted,
        date_time_render_option=DateTimeOption.serial_number,
    )
    return values, datetime.now(timezone.utc).isoformat()


@st.cache_resource(show_spinner=False, max_entries=2)
def process_cached(fetch_id: str, _values: list[list]) -> dict:
    """Heavy processing runs once per fetch, shared across reruns/sessions."""
    return process_values(_values)


def friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "SpreadsheetNotFound":
        return ("Spreadsheet not found or not shared. Share the Google Sheet with the "
                "service-account e-mail (Viewer access is enough).")
    if name == "WorksheetNotFound":
        return f"Worksheet '{WORKSHEET_NAME}' was not found in the spreadsheet."
    if name == "APIError":
        return f"Google Sheets API error: {exc}"
    return f"{name}: {exc}"


# =============================================================================
# 6. SHARED STATE: BASELINE LOCK + LAST GOOD SNAPSHOT
# =============================================================================
@st.cache_resource(show_spinner=False)
def shared_state() -> dict:
    """Process-wide store (survives reruns and is shared by all viewers)."""
    return {
        "lock": threading.Lock(),
        "baseline": {},          # sensor -> locked value (mV)
        "baseline_n": {},        # sensor -> number of valid readings used
        "baseline_locked_at": {},
        "last_good": None,       # (result dict, fetched_at ISO string)
    }


def resolve_baseline(res: dict) -> tuple[pd.Series, dict, list[str]]:
    """Return fixed Jig 2 baselines; never derive them from incoming data."""
    baseline = {
        s: (O2_BASELINE_MV if GROUP[s] == "O2" else AIR_BASELINE_MV)
        for s in SENSOR_NAMES
    }
    status = {s: "Fixed common baseline" for s in SENSOR_NAMES}
    return pd.Series(baseline, dtype=float), status, []


# =============================================================================
# 7. EXCEL REPORT
# =============================================================================
@st.cache_data(show_spinner=False, max_entries=2)
def build_excel(cache_key: str, _daily_long, _baseline_df, _latest_df, _excl_df, _raw_df, _dead_df) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheets = {
        "Daily Results": _daily_long,
        "Baseline Summary": _baseline_df,
        "Latest Sensor Comparison": _latest_df,
        "Threshold Exclusion Summary": _excl_df,
        "Filtered Raw Data": _raw_df,
        "Dead Sensor Status": _dead_df,
    }
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            f = frame.copy()
            for c in f.columns:
                if isinstance(f[c].dtype, pd.CategoricalDtype):
                    f[c] = f[c].astype(str)
            f.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F3A5F")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            for i, c in enumerate(f.columns, start=1):
                letter = get_column_letter(i)
                width = max(len(str(c)), *(len(str(v)) for v in f[c].head(200).tolist())) if len(f) else len(str(c))
                ws.column_dimensions[letter].width = min(max(width + 2, 10), 40)
                if pd.api.types.is_datetime64_any_dtype(f[c]):
                    fmt = "dd-mmm-yyyy" if c == "Date" else "dd-mmm-yyyy hh:mm:ss"
                    for cell in ws[letter][1:]:
                        cell.number_format = fmt
                elif pd.api.types.is_float_dtype(f[c]):
                    for cell in ws[letter][1:]:
                        cell.number_format = "0.000"
    return buf.getvalue()


# =============================================================================
# 8. CHART HELPERS
# =============================================================================
def _fmt(v, nd=2, unit=""):
    return "—" if v is None or (isinstance(v, float) and not math.isfinite(v)) or pd.isna(v) else f"{v:,.{nd}f}{unit}"


def _ts(t) -> str:
    return "—" if t is None or pd.isna(t) else pd.Timestamp(t).strftime("%d %b %Y %H:%M:%S")


def line_figure(series_by_sensor: dict, title: str, ytitle: str, unit: str,
                zero_line=False, markers=True, gl=False, height=430, x_title="Date",
                date_fmt="%d %b %Y") -> go.Figure:
    fig = go.Figure()
    Trace = go.Scattergl if gl else go.Scatter
    for s, ser in series_by_sensor.items():
        ser = ser.astype(float)
        if ser.notna().sum() == 0:
            continue
        cd = np.array([[s, PHYSICAL[s], EXPOSURE[s]]] * len(ser), dtype=object)
        fig.add_trace(Trace(
            x=ser.index, y=ser.values, name=label(s),
            mode="lines+markers" if markers else "lines",
            line=dict(color=COLOR[s], width=2), marker=dict(size=5),
            customdata=cd, connectgaps=False,
            hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]}<br>%{customdata[2]}<br>"
                           f"%{{x|{date_fmt}}}<br>%{{y:.3f}} {unit}<extra></extra>"),
        ))
        last = ser.dropna()
        if len(last):
            fig.add_trace(go.Scatter(
                x=[last.index[-1]], y=[last.iloc[-1]], mode="markers+text",
                marker=dict(size=12, color=COLOR[s], line=dict(width=2, color="white")),
                text=[f"{last.iloc[-1]:.2f}"], textposition="middle right",
                textfont=dict(color=COLOR[s], size=11),
                showlegend=False, hoverinfo="skip",
            ))
    if zero_line:
        fig.add_hline(y=0, line_dash="dash", line_color="gray",
                      annotation_text="0 % (baseline)", annotation_position="bottom left")
    fig.update_layout(
        title=dict(text=title, x=0, font=dict(size=16)), height=height,
        margin=dict(l=10, r=40, t=50, b=10), hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=-0.32, x=0),
        xaxis_title=x_title, yaxis_title=ytitle,
    )
    return fig


def plot(fig: go.Figure, key: str):
    st.plotly_chart(fig, width="stretch", key=key)


def downsample(df: pd.DataFrame, max_points: int = 4000) -> tuple[pd.DataFrame, bool]:
    """Display-only time-bin mean for very long windows."""
    if len(df) <= max_points:
        return df, False
    span = df.index.max() - df.index.min()
    rule = max(pd.Timedelta(seconds=1), span / max_points)
    return df.resample(rule).mean(), True


# =============================================================================
# 9. UI
# =============================================================================
CSS = """
<style>
.block-container {padding-top: 1.6rem;}
.app-title {font-size: 1.55rem; font-weight: 800; letter-spacing: .01em; margin: 0;}
.app-sub {opacity: .7; font-size: .9rem; margin-bottom: .8rem;}
.status-bar {display:flex; flex-wrap:wrap; gap:.45rem 1.4rem; align-items:center;
  padding:.65rem 1rem; border:1px solid rgba(128,128,128,.28); border-radius:.7rem; margin-bottom:1rem;}
.badge {font-weight:800; padding:.22rem .7rem; border-radius:999px; color:#fff; font-size:.8rem; letter-spacing:.06em;}
.badge-live {background:#15803d;}
.badge-err {background:#b91c1c;}
.dot {display:inline-block; width:.55rem; height:.55rem; border-radius:50%; background:#fff;
  margin-right:.35rem; animation: pulse 1.6s infinite;}
@keyframes pulse {0%{opacity:1} 50%{opacity:.25} 100%{opacity:1}}
.si {font-size:.85rem;} .si .k {opacity:.65; margin-right:.3rem;}
.group-h {font-weight:700; font-size:.95rem; margin:.4rem 0 .2rem; opacity:.85;}
div[data-testid="stMetricValue"] {font-size: 1.6rem;}
</style>
"""

PAGES = [
    "🟢 Live Monitor",
    "📊 Overview",
    "📈 Sensor Output Analysis",
    "📉 Degradation Analysis",
    "🌡️ Temperature Monitoring",
    "🔎 Sensor Details",
    "🗂️ Data & Validation",
]


def load_data() -> tuple[dict | None, dict]:
    """Fetch → process; on failure fall back to the last good snapshot."""
    store = shared_state()
    status = {"ok": False, "error": None, "fetched_at": None, "stale_snapshot": False}
    try:
        values, fetched_at = fetch_sheet_values()
        res = process_cached(fetched_at, values)
        with store["lock"]:
            store["last_good"] = (res, fetched_at)
        status.update(ok=True, fetched_at=fetched_at)
        return res, status
    except Exception as exc:  # noqa: BLE001 – any failure must not blank the dashboard
        status["error"] = friendly_error(exc)
        with store["lock"]:
            last = store["last_good"]
        if last is None and "last_good" in st.session_state:
            last = st.session_state["last_good"]
        if last is not None:
            status.update(fetched_at=last[1], stale_snapshot=True)
            return last[0], status
        return None, status


def track_new_readings(res: dict, fetched_at: str) -> tuple[int, int]:
    ss = st.session_state
    if ss.get("seen_fetch") != fetched_at:
        prev = ss.get("seen_records")
        ss["new_last"] = max(0, res["records"] - prev) if prev is not None else 0
        ss["new_session"] = ss.get("new_session", 0) + ss["new_last"]
        ss["seen_records"] = res["records"]
        ss["seen_fetch"] = fetched_at
    return ss.get("new_last", 0), ss.get("new_session", 0)


def status_bar(res, status, new_last, new_session):
    fetched = (datetime.fromisoformat(status["fetched_at"]).astimezone(DISPLAY_TZ)
               .strftime("%d %b %Y %H:%M:%S") if status["fetched_at"] else "—")
    if status["ok"]:
        badge = '<span class="badge badge-live"><span class="dot"></span>LIVE</span>'
    else:
        badge = '<span class="badge badge-err">CONNECTION ERROR</span>'
    latest = _ts(res["latest_ts"]) if res else "—"
    day = ""
    if res and pd.notna(res["latest_ts"]):
        d = (res["latest_ts"].date() - STUDY_START).days
        day = f'<span class="si"><span class="k">Study day</span><b>{d} of {STUDY_DAYS - 1}</b></span>'
    excl = f'{res["total_excluded"]:,}' if res else "—"
    st.markdown(
        f'<div class="status-bar">{badge}'
        f'<span class="si"><span class="k">Last successful refresh</span><b>{fetched}</b></span>'
        f'<span class="si"><span class="k">Latest sheet timestamp</span><b>{latest}</b></span>'
        f'<span class="si"><span class="k">New readings (last refresh)</span><b>+{new_last}</b>'
        f'<span class="k" style="margin-left:.3rem">· {new_session} this session</span></span>'
        f'<span class="si"><span class="k">Excluded readings</span><b>{excl}</b></span>{day}'
        f'</div>', unsafe_allow_html=True)


def baseline_messages(res, baseline, bstatus, drift):
    st.caption(
        "Fixed baselines: S-9 and S-10 (with O₂) = 98.28 mV; "
        f"S-11 to S-16 (without O₂) = {AIR_BASELINE_MV:.4f} mV. "
        "Dead-sensor detection uses raw numeric readings; degradation and daily averages "
        "continue to use threshold-filtered readings."
    )


def live_page(res, baseline, bstatus, daily_long):
    latest_tbl = build_latest_table(res, baseline, bstatus).set_index("Sensor")
    now_local = datetime.now(DISPLAY_TZ).replace(tzinfo=None)
    if pd.notna(res["latest_ts"]) and now_local.date() <= STUDY_END:
        age = (now_local - res["latest_ts"]).total_seconds() / 60
        if age > STALE_AFTER_MINUTES:
            st.warning(f"No new readings for **{age:,.0f} min** (latest sheet timestamp "
                       f"{_ts(res['latest_ts'])}). Check the data logger.")

    def sensor_card(s):
        L = res["latest"][s]
        row = latest_tbl.loc[s]
        with st.container(border=True):
            deg = row["Degradation (%)"]
            st.metric(
                f"{s} · {PHYSICAL[s]}",
                _fmt(L["valid_value"], 2, " mV"),
                delta=None if pd.isna(deg) else f"{deg:+.2f} % degradation",
                delta_color="inverse",
                help=f"{EXPOSURE[s]} · threshold ≤ {THRESHOLD[s]:.0f} mV. "
                     "Value = latest valid reading. Degradation = latest daily average vs fixed group baseline.",
            )
            st.caption(f"Day avg {_fmt(row['Latest Average (mV)'], 2, ' mV')} · "
                       f"base {_fmt(baseline[s], 2, ' mV')}  \\n{_ts(L['valid_ts'])}")
            dead_state = res["dead_sensor"].loc[s, "Dead Sensor Status"]
            progress = res["dead_sensor"].loc[s, "Verification Progress"]
            if dead_state == "DEAD SENSOR":
                st.error("DEAD SENSOR")
            elif dead_state in ("RECOVERY CHECK", "NORMAL CHECK"):
                st.warning(f"{dead_state} · {progress}")
            else:
                st.success("NORMAL")
            if L["raw_excluded"]:
                st.caption(f"⚠️ Latest raw {_fmt(L['raw_value'], 2, ' mV')} excluded "
                           f"(> {THRESHOLD[s]:.0f} mV)")

    st.markdown('<div class="group-h">100 % O₂ sensors</div>', unsafe_allow_html=True)
    cols = st.columns(4)
    for c, s in zip(cols[:2], O2_SENSORS):
        with c:
            sensor_card(s)
    with cols[2]:
        with st.container(border=True):
            t, tts = res["latest_temp"]
            st.metric("Current temperature", _fmt(t, 1, " °C"))
            st.caption(_ts(tts))
    with cols[3]:
        with st.container(border=True):
            st.metric("Excluded readings (study)", f"{res['total_excluded']:,}")
            st.caption(f"{res['total_valid']:,} valid of {res['total_readings']:,} numeric readings")

    st.markdown('<div class="group-h">Air sensors</div>', unsafe_allow_html=True)
    for chunk in (AIR_SENSORS[:4], AIR_SENSORS[4:]):
        cols = st.columns(4)
        for c, s in zip(cols, chunk):
            with c:
                sensor_card(s)

    # ---- degradation snapshot ----
    st.subheader("Latest degradation – all sensors")
    snap = latest_tbl["Degradation (%)"]
    fig = go.Figure(go.Bar(
        x=[label(s) for s in SENSOR_NAMES], y=snap.values,
        marker_color=[COLOR[s] for s in SENSOR_NAMES],
        text=[_fmt(v, 2, " %") for v in snap.values], textposition="outside",
        hovertemplate="%{x}<br>%{y:.3f} %<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Degradation (%)")
    plot(fig, "live_deg_bar")

    # ---- live time-series ----
    st.subheader("Live graphs")
    windows = {"Last 1 hour": 1, "Last 6 hours": 6, "Last 24 hours": 24, "Last 3 days": 72,
               "Last 7 days": 168, "Entire study": None}
    choice = st.segmented_control("Time window", list(windows), default="Last 24 hours",
                                  key="live_window") or "Last 24 hours"
    d = res["data"].set_index("Timestamp")
    filt = res["filtered"].set_index(d.index)
    if len(d) and windows[choice]:
        cutoff = d.index.max() - pd.Timedelta(hours=windows[choice])
        filt, temp = filt[filt.index >= cutoff], d.loc[d.index >= cutoff, "Temperature (°C)"]
    else:
        temp = d["Temperature (°C)"]
    filt_ds, ds1 = downsample(filt)
    temp_ds, _ = downsample(temp.to_frame())
    if ds1:
        st.caption("Long window: display is time-binned for speed. Calculations use every reading.")

    fmt = "%d %b %H:%M:%S"
    plot(line_figure({s: filt_ds[s] for s in SENSOR_NAMES}, "Live sensor output – all 8 sensors",
                     "Output (mV)", "mV", markers=False, gl=True, x_title="Time", date_fmt=fmt), "live_all")
    c1, c2 = st.columns(2)
    with c1:
        plot(line_figure({s: filt_ds[s] for s in O2_SENSORS}, "100 % O₂ sensors (S-9, S-10)",
                         "Output (mV)", "mV", markers=False, gl=True, x_title="Time", date_fmt=fmt), "live_o2")
    with c2:
        plot(line_figure({s: filt_ds[s] for s in AIR_SENSORS}, "Air sensors (S-11 – S-16)",
                         "Output (mV)", "mV", markers=False, gl=True, x_title="Time", date_fmt=fmt), "live_air")

    live_deg = degradation(filt_ds[SENSOR_NAMES], baseline)
    plot(line_figure({s: live_deg[s] for s in SENSOR_NAMES},
                     "Live degradation – each valid reading vs fixed group baseline",
                     "Degradation (%)", "%", zero_line=True, markers=False, gl=True,
                     x_title="Time", date_fmt=fmt), "live_deg")
    st.caption("Official degradation (cards, reports) uses daily averages; this live view applies the same "
               "formula to individual valid readings.")

    tfig = go.Figure(go.Scattergl(x=temp_ds.index, y=temp_ds.iloc[:, 0], mode="lines",
                                  line=dict(color="#C2410C", width=2), name="Temperature",
                                  hovertemplate="%{x|%d %b %H:%M:%S}<br>%{y:.2f} °C<extra></extra>"))
    tl = temp.dropna()
    if len(tl):
        tfig.add_trace(go.Scatter(x=[tl.index[-1]], y=[tl.iloc[-1]], mode="markers+text",
                                  marker=dict(size=12, color="#C2410C", line=dict(width=2, color="white")),
                                  text=[f"{tl.iloc[-1]:.1f} °C"], textposition="middle right",
                                  showlegend=False, hoverinfo="skip"))
    tfig.update_layout(title=dict(text="Temperature monitoring", x=0, font=dict(size=16)), height=330,
                       margin=dict(l=10, r=40, t=50, b=10), xaxis_title="Time",
                       yaxis_title="Temperature (°C)", showlegend=False)
    plot(tfig, "live_temp")


def overview_page(res, baseline, bstatus):
    c = st.columns(6)
    c[0].metric("Study start", STUDY_START.strftime("%d %b %Y"), border=True)
    c[1].metric("Study end", STUDY_END.strftime("%d %b %Y"), border=True)
    c[2].metric("Study duration", f"{STUDY_DAYS} days", border=True)
    c[3].metric("Latest reading", _ts(res["latest_ts"]) if pd.notna(res["latest_ts"]) else "—", border=True)
    c[4].metric("Total raw readings", f"{res['total_readings']:,}", border=True,
                help=f"Numeric sensor readings in the study window ({res['records']:,} timestamp records × 8 channels, "
                     "missing cells not counted).")
    c[5].metric("Total excluded", f"{res['total_excluded']:,}", border=True,
                help="Individual readings above the sensor threshold.")
    if pd.notna(res["latest_ts"]):
        d = min(max((res["latest_ts"].date() - STUDY_START).days + 1, 0), STUDY_DAYS)
        st.progress(d / STUDY_DAYS, text=f"Study progress: {d} of {STUDY_DAYS} days with data window "
                                         f"(Day 0 = 21 Oct, Day 51 = 11 Dec)")
    with st.expander("Data quality", expanded=False):
        q = st.columns(4)
        q[0].metric("Sheet rows read", f"{res['sheet_rows']:,}")
        q[1].metric("Rows in study window", f"{res['records']:,}")
        q[2].metric("Invalid timestamps", f"{res['invalid_ts_rows']:,}")
        q[3].metric("Outside study window", f"{res['outside_window_rows']:,}")

    st.subheader("Fixed baseline summary")
    store = shared_state()
    b = pd.DataFrame({
        "Sensor": SENSOR_NAMES,
        "Physical ID": [PHYSICAL[s] for s in SENSOR_NAMES],
        "Exposure": [EXPOSURE[s] for s in SENSOR_NAMES],
        "Baseline (mV)": [baseline[s] for s in SENSOR_NAMES],
        "Valid readings used": ["—"] * len(SENSOR_NAMES),
        "Baseline Type": ["Fixed common baseline"] * len(SENSOR_NAMES),
        "Status": [bstatus[s] for s in SENSOR_NAMES],
        "Baseline Value Source": ["Configured constant"] * len(SENSOR_NAMES),
    })
    st.dataframe(b, hide_index=True, width="stretch",
                 column_config={"Baseline (mV)": st.column_config.NumberColumn(format="%.3f")})

    st.subheader("Latest sensor degradation")
    lt = build_latest_table(res, baseline, bstatus)
    st.dataframe(lt[["Sensor", "Physical ID", "Exposure", "Baseline (mV)", "Latest Avg Date",
                     "Latest Average (mV)", "Degradation (%)"]],
                 hide_index=True, width="stretch",
                 column_config={"Baseline (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Latest Average (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Degradation (%)": st.column_config.NumberColumn(format="%.2f %%")})


def daily_series(daily_long, col, sensors):
    return {s: daily_long[daily_long["Sensor"] == s].set_index("Date")[col] for s in sensors}


def output_page(daily_long):
    st.caption("Daily average of threshold-valid readings. Gaps = days without valid readings.")
    plot(line_figure(daily_series(daily_long, "Daily Avg (mV)", SENSOR_NAMES),
                     "All 8 sensors – daily average output", "Output (mV)", "mV"), "out_all")
    plot(line_figure(daily_series(daily_long, "Daily Avg (mV)", O2_SENSORS),
                     "100 % O₂ sensors – S-9 & S-10", "Output (mV)", "mV"), "out_o2")
    plot(line_figure(daily_series(daily_long, "Daily Avg (mV)", AIR_SENSORS),
                     "Air sensors – S-11 to S-16", "Output (mV)", "mV"), "out_air")


def degradation_page(daily_long):
    st.caption("Degradation (%) = ((Baseline − Daily Average) / Baseline) × 100. "
               "Positive = output decreased; negative = output increased.")
    plot(line_figure(daily_series(daily_long, "Degradation (%)", SENSOR_NAMES),
                     "All 8 sensors – degradation", "Degradation (%)", "%", zero_line=True), "deg_all")
    plot(line_figure(daily_series(daily_long, "Degradation (%)", O2_SENSORS),
                     "100 % O₂ sensors – degradation", "Degradation (%)", "%", zero_line=True), "deg_o2")
    plot(line_figure(daily_series(daily_long, "Degradation (%)", AIR_SENSORS),
                     "Air sensors – degradation", "Degradation (%)", "%", zero_line=True), "deg_air")


def temperature_page(res):
    t = res["daily_temp"]
    td = res["data"]["Temperature (°C)"].dropna()
    c = st.columns(4)
    c[0].metric("Current", _fmt(res["latest_temp"][0], 1, " °C"), border=True)
    c[1].metric("Study mean", _fmt(td.mean(), 2, " °C"), border=True)
    c[2].metric("Study min", _fmt(td.min(), 1, " °C"), border=True)
    c[3].metric("Study max", _fmt(td.max(), 1, " °C"), border=True)
    fig = go.Figure(go.Scatter(x=t.index, y=t.values, mode="lines+markers",
                               line=dict(color="#C2410C", width=2), connectgaps=False,
                               hovertemplate="%{x|%d %b %Y}<br>%{y:.2f} °C<extra></extra>"))
    tl = t.dropna()
    if len(tl):
        fig.add_trace(go.Scatter(x=[tl.index[-1]], y=[tl.iloc[-1]], mode="markers+text",
                                 marker=dict(size=12, color="#C2410C", line=dict(width=2, color="white")),
                                 text=[f"{tl.iloc[-1]:.2f} °C"], textposition="middle right", hoverinfo="skip"))
    fig.update_layout(title=dict(text="Daily average temperature", x=0), height=430, showlegend=False,
                      margin=dict(l=10, r=40, t=50, b=10), xaxis_title="Date", yaxis_title="Temperature (°C)")
    plot(fig, "temp_daily")
    tbl = pd.DataFrame({"Date": t.index, "Study Day": (t.index - pd.Timestamp(STUDY_START)).days,
                        "Daily Avg Temp (°C)": t.values, "Records": res["daily_records"].values})
    st.dataframe(tbl, hide_index=True, width="stretch",
                 column_config={"Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                                "Daily Avg Temp (°C)": st.column_config.NumberColumn(format="%.2f")})


def details_page(res, baseline, bstatus, daily_long):
    lt = build_latest_table(res, baseline, bstatus)
    st.dataframe(lt[["Sensor", "Physical ID", "Exposure", "Baseline Date", "Baseline (mV)",
                     "Latest Average (mV)", "Degradation (%)", "Threshold (mV)"]],
                 hide_index=True, width="stretch",
                 column_config={"Baseline (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Latest Average (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Degradation (%)": st.column_config.NumberColumn(format="%.2f %%"),
                                "Threshold (mV)": st.column_config.NumberColumn(format="%.0f")})
    s = st.selectbox("Inspect sensor", SENSOR_NAMES, format_func=lambda x: f"{label(x)} – {EXPOSURE[x]}",
                     key="detail_sensor")
    row = lt.set_index("Sensor").loc[s]
    c = st.columns(4)
    c[0].metric("Baseline", _fmt(row["Baseline (mV)"], 3, " mV"), border=True)
    c[1].metric("Latest daily avg", _fmt(row["Latest Average (mV)"], 3, " mV"), border=True)
    c[2].metric("Degradation", _fmt(row["Degradation (%)"], 2, " %"), border=True)
    c[3].metric("Threshold", f"≤ {THRESHOLD[s]:.0f} mV", border=True)
    hist = daily_long[daily_long["Sensor"] == s]
    c1, c2 = st.columns(2)
    with c1:
        plot(line_figure({s: hist.set_index("Date")["Daily Avg (mV)"]}, f"{label(s)} – daily output",
                         "Output (mV)", "mV", height=360), "det_out")
    with c2:
        plot(line_figure({s: hist.set_index("Date")["Degradation (%)"]}, f"{label(s)} – degradation",
                         "Degradation (%)", "%", zero_line=True, height=360), "det_deg")
    st.dataframe(hist[["Date", "Study Day", "Daily Avg (mV)", "Degradation (%)", "Valid Readings",
                       "Excluded Readings", "Missing/Invalid"]],
                 hide_index=True, width="stretch",
                 column_config={"Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                                "Daily Avg (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Degradation (%)": st.column_config.NumberColumn(format="%.2f %%")})


def validation_page(res, daily_long, raw_tbl):
    c1, c2 = st.columns([1, 2])
    last = res["latest_ts"].date() if pd.notna(res["latest_ts"]) else STUDY_START
    with c1:
        rng = st.date_input("Date range", value=(STUDY_START, min(last, STUDY_END)),
                            min_value=STUDY_START, max_value=STUDY_END, format="DD/MM/YYYY", key="val_range")
    with c2:
        sel = st.multiselect("Sensors", SENSOR_NAMES, default=SENSOR_NAMES, format_func=label, key="val_sensors")
    if not isinstance(rng, (tuple, list)) or len(rng) != 2:
        st.info("Select a start and end date.")
        return
    d0, d1 = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
    sel = sel or SENSOR_NAMES

    st.subheader("Dead-sensor detection (raw readings)")
    dead_view = res["dead_sensor"].reset_index()
    dead_view = dead_view[dead_view["Sensor"].isin(sel)]
    st.dataframe(dead_view, hide_index=True, width="stretch",
                 column_config={
                     "Detection Trigger (mV)": st.column_config.NumberColumn(format="%.6f"),
                     "Recovery Target (mV)": st.column_config.NumberColumn(format="%.6f"),
                     "Latest Raw Reading (mV)": st.column_config.NumberColumn(format="%.3f"),
                 })

    dl = daily_long[(daily_long["Date"] >= d0) & (daily_long["Date"] <= d1) & daily_long["Sensor"].isin(sel)]
    mask = (res["data"]["Date"] >= d0) & (res["data"]["Date"] <= d1)
    ex = exclusion_summary(res, mask)
    ex = ex[ex["Sensor"].isin(sel)]

    k = st.columns(3)
    k[0].metric("Valid readings", f"{int(ex['Valid Readings'].sum()):,}", border=True)
    k[1].metric("Excluded readings", f"{int(ex['Excluded (> threshold)'].sum()):,}", border=True)
    k[2].metric("Missing / invalid", f"{int(ex['Missing/Invalid'].sum()):,}", border=True)

    st.subheader("Threshold exclusion summary")
    st.dataframe(ex, hide_index=True, width="stretch",
                 column_config={"Excluded (%)": st.column_config.NumberColumn(format="%.2f %%"),
                                "Max Raw (mV)": st.column_config.NumberColumn(format="%.3f")})

    st.subheader("Daily results")
    st.dataframe(dl, hide_index=True, width="stretch", height=380,
                 column_config={"Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                                "Daily Avg (mV)": st.column_config.NumberColumn(format="%.3f"),
                                "Degradation (%)": st.column_config.NumberColumn(format="%.2f %%"),
                                "Daily Avg Temp (°C)": st.column_config.NumberColumn(format="%.2f")})
    st.download_button("⬇️ Daily results (CSV)", dl.to_csv(index=False).encode(), "jig2_daily_results.csv",
                       "text/csv", key="dl_daily_csv")

    st.subheader("Raw data after filtering")
    st.caption("Values above threshold are blanked for that sensor only; the rest of the row is kept.")
    rmask = (raw_tbl["Timestamp"] >= d0) & (raw_tbl["Timestamp"] < d1 + pd.Timedelta(days=1))
    cols = ["Timestamp", "Study Day", "Temperature (°C)"] + [f"{s} (mV)" for s in sel] + ["Excluded Sensors", "Sheet Row"]
    rv = raw_tbl.loc[rmask, cols].iloc[::-1]
    st.dataframe(rv, hide_index=True, width="stretch", height=420,
                 column_config={"Timestamp": st.column_config.DatetimeColumn(format="DD MMM YYYY HH:mm:ss")})
    st.download_button("⬇️ Filtered raw data (CSV)", rv.to_csv(index=False).encode(),
                       "jig2_filtered_raw.csv", "text/csv", key="dl_raw_csv")


def main():
    st.set_page_config(page_title="Jig 2 O₂ Sensor Monitoring", page_icon="🫁", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="live_autorefresh")

    with st.sidebar:
        st.markdown("### Jig 2 · O₂ Sensors")
        page = st.radio("Navigation", PAGES, key="page", label_visibility="collapsed")
        st.divider()
        if st.button("🔄 Refresh Now", width="stretch", type="primary"):
            fetch_sheet_values.clear()
        st.caption(f"Auto-refresh every {REFRESH_SECONDS} s while this tab is open.")

    res, status = load_data()

    st.markdown(f'<p class="app-title">{APP_TITLE}</p>'
                f'<p class="app-sub">Study 21 Oct 2026 → 11 Dec 2026 · Fixed baselines: O₂ 98.28 mV · Air 21.0564 mV · '
                f'Times shown in {DISPLAY_TZ.key}</p>', unsafe_allow_html=True)

    if res is None:
        status_bar(None, status, 0, 0)
        st.error(f"**Could not load Google Sheets data.** {status['error']}")
        st.info("The dashboard will retry automatically every minute.")
        return

    st.session_state["last_good"] = (res, status["fetched_at"])
    new_last, new_session = track_new_readings(res, status["fetched_at"])
    status_bar(res, status, new_last, new_session)
    if not status["ok"]:
        st.error(f"**Connection error – showing last successfully loaded data.** {status['error']}")
    if res["records"] == 0:
        st.error("No readings with valid timestamps between 21 Oct 2026 and 11 Dec 2026 were found in Sheet1.")
        return

    baseline, bstatus, drift = resolve_baseline(res)
    baseline_messages(res, baseline, bstatus, drift)
    daily_long = build_daily_long(res, baseline)
    raw_tbl = filtered_raw_table_cached(status["fetched_at"], res)

    # ---- Excel report (sidebar) ----
    bsig = "|".join(f"{baseline[s]:.9g}" for s in SENSOR_NAMES)
    store = shared_state()
    baseline_df = pd.DataFrame({
        "Sensor": SENSOR_NAMES,
        "Physical ID": [PHYSICAL[s] for s in SENSOR_NAMES],
        "Exposure": [EXPOSURE[s] for s in SENSOR_NAMES],
        "Sheet Column": [SHEET_COL[s] for s in SENSOR_NAMES],
        "Threshold (mV)": [THRESHOLD[s] for s in SENSOR_NAMES],
        "Baseline Date": "Fixed common baseline",
        "Baseline (mV)": [baseline[s] for s in SENSOR_NAMES],
        "Valid Readings Used": ["—"] * len(SENSOR_NAMES),
        "Baseline Type": ["Fixed common baseline"] * len(SENSOR_NAMES),
        "Status": [bstatus[s] for s in SENSOR_NAMES],
        "Baseline Value Source": ["Configured constant"] * len(SENSOR_NAMES),
    })
    latest_df = build_latest_table(res, baseline, bstatus)
    excl_df = exclusion_summary(res)
    dead_df = res["dead_sensor"].reset_index()
    xlsx = build_excel(f"{status['fetched_at']}|{bsig}", daily_long, baseline_df, latest_df, excl_df, raw_tbl, dead_df)
    with st.sidebar:
        st.divider()
        st.download_button(
            "⬇️ Download Excel report", xlsx,
            file_name=f"Jig2_O2_Degradation_Report_{datetime.now(DISPLAY_TZ):%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch", key="dl_excel")
        with st.expander("Sensor mapping & thresholds"):
            st.dataframe(pd.DataFrame({
                "Sensor": SENSOR_NAMES, "Physical": [PHYSICAL[s] for s in SENSOR_NAMES],
                "Col": [SHEET_COL[s] for s in SENSOR_NAMES],
                "≤ mV": [int(THRESHOLD[s]) for s in SENSOR_NAMES]}), hide_index=True)

    st.header(page.split(" ", 1)[1])
    if page == PAGES[0]:
        live_page(res, baseline, bstatus, daily_long)
    elif page == PAGES[1]:
        overview_page(res, baseline, bstatus)
    elif page == PAGES[2]:
        output_page(daily_long)
    elif page == PAGES[3]:
        degradation_page(daily_long)
    elif page == PAGES[4]:
        temperature_page(res)
    elif page == PAGES[5]:
        details_page(res, baseline, bstatus, daily_long)
    elif page == PAGES[6]:
        validation_page(res, daily_long, raw_tbl)


@st.cache_resource(show_spinner=False, max_entries=2)
def filtered_raw_table_cached(fetch_id: str, _res: dict) -> pd.DataFrame:
    return filtered_raw_table(_res)


if __name__ == "__main__":
    main()