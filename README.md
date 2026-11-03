# JIG 2 – O₂ Sensor Aging and Degradation Monitoring

Live Streamlit dashboard that reads Jig 2's Google Sheet every minute, applies the
study's threshold rules, holds the 21 Oct 2026 baseline fixed, and reports daily
average output and degradation for the eight O₂ sensors.

| Item | Value |
|---|---|
| Google Sheet ID | `1g1oh-CZgnkgBJRayexm8pvv_jEKrHjIElXNVXM95p9k` |
| Worksheet | `Sheet1` (columns A–J: Timestamp, Temperature, Sensor 1 … Sensor 8) |
| Study window | 21 Oct 2026 (Day 0) → 11 Dec 2026 (Day 51), 52 days inclusive |
| Baseline | Mean of threshold-valid readings dated 21 Oct 2026 only |
| Auto-refresh | Every 60 s while the browser tab is open, plus a **Refresh Now** button |

## Project files

```
jig2_o2_dashboard/
├── app.py                         # the whole application
├── requirements.txt
├── README.md
├── .gitignore                     # keeps secrets.toml out of Git
└── .streamlit/
    ├── config.toml                # theme / server settings
    └── secrets.toml.example       # credential template (no real keys)
```

## Sensor mapping and thresholds

| Dashboard | Sheet column | Physical ID | Exposure | Accepted if |
|---|---|---|---|---|
| S-9  | C – Sensor 1 | S-16-23% | With O₂ – 100% | ≤ 110 mV |
| S-10 | D – Sensor 2 | S-02-23% | With O₂ – 100% | ≤ 110 mV |
| S-11 | E – Sensor 3 | S-21-23% | Without O₂ – Air | ≤ 30 mV |
| S-12 | F – Sensor 4 | S-08-23% | Without O₂ – Air | ≤ 30 mV |
| S-13 | G – Sensor 5 | S-07-23% | Without O₂ – Air | ≤ 30 mV |
| S-14 | H – Sensor 6 | S-03-23% | Without O₂ – Air | ≤ 30 mV |
| S-15 | I – Sensor 7 | S-23-23% | Without O₂ – Air | ≤ 30 mV |
| S-16 | J – Sensor 8 | S-24-23% | Without O₂ – Air | ≤ 30 mV |

## How the numbers are calculated

1. **Read** every row of `Sheet1` by column position (header text is not relied on).
   Numbers are read at full precision; date-formatted cells are read as serial
   numbers so day/month order can't be confused. Text dates are also accepted.
2. **Study window:** only rows timestamped 21 Oct 2026 00:00 – 11 Dec 2026 23:59:59
   are used. Rows with unreadable timestamps are counted and skipped.
3. **Threshold filter (per reading):** a reading above its sensor's threshold is
   excluded for that sensor only; the rest of the row is kept. A reading equal to
   the threshold is accepted. Blank or non-numeric cells are counted as
   missing/invalid and never enter any average.
4. **Daily average:** mean of the valid readings for each sensor and calendar day.
5. **Baseline:** the 21 Oct 2026 daily average of valid readings. No other date is
   ever used. If 21 Oct has no valid readings for a sensor, the dashboard shows an
   error and does not calculate degradation for it.
6. **Degradation (%)** = ((Baseline − Daily Average) / Baseline) × 100.
   Positive = output fell; negative = output rose. Day 0 is 0 % by construction.
   Not calculated when the daily average or baseline is missing.

### Baseline protection

* As soon as data after 21 Oct exists, each sensor's baseline is **locked** in a
  server-wide store shared by every viewer. Refreshes never recompute or replace it.
* On every refresh the dashboard re-checks the 21 Oct rows in the sheet. If they
  have been edited, the locked value is kept and a **Baseline protection** warning
  names the affected sensors.
* If the app restarts (redeploy or Streamlit Cloud sleep), the baseline is rebuilt
  from the same 21 Oct rows, so it is identical unless those rows were changed.
* The Overview page and the Excel report show each baseline, the number of
  readings used, its status and when it was locked.

## Dashboard pages

| Page | What it shows |
|---|---|
| 🟢 Live Monitor | LIVE / CONNECTION ERROR status, latest output and degradation card for each sensor, current temperature, and live graphs (all sensors, 100 % O₂, air, live degradation, temperature) with the latest point labelled. A time-window selector covers the last hour up to the whole study. |
| 📊 Overview | Study KPIs, data-quality counts, baseline summary, latest degradation table |
| 📈 Sensor Output Analysis | Daily average output: all 8, O₂ pair, air group |
| 📉 Degradation Analysis | Daily degradation with a 0 % reference line |
| 🌡️ Temperature Monitoring | Daily average temperature and statistics |
| 🔎 Sensor Details | Per-sensor table plus history charts for any selected sensor |
| 🗂️ Data & Validation | Date-range and sensor filters, exclusion summary, daily results, filtered raw data, CSV downloads |

The status bar on every page shows the last successful refresh, the latest sheet
timestamp, new readings since the previous refresh and total excluded readings.
If Google Sheets can't be reached, the last good data stays on screen with an
error banner, and the app retries automatically. A warning also appears when the
sheet has had no new rows for more than 10 minutes.

**Excel report** (sidebar) contains: Daily Results, Baseline Summary, Latest Sensor
Comparison, Threshold Exclusion Summary and Filtered Raw Data — the same figures the
dashboard shows.

---

## Setup and deployment

### 1. Google Cloud project

1. Go to <https://console.cloud.google.com/> and sign in.
2. Use the project selector (top bar) → **New Project** → name it, e.g. `jig2-dashboard` → **Create**.

### 2. Enable the APIs

In the new project, open **APIs & Services → Library** and enable both:

* **Google Sheets API**
* **Google Drive API**

### 3. Create a service account and key

1. **IAM & Admin → Service Accounts → Create service account**.
2. Name it, e.g. `jig2-dashboard`, then **Create and continue → Done** (no project roles are needed).
3. Open the service account → **Keys → Add key → Create new key → JSON → Create**.
   A `.json` file downloads. Keep it private; never commit it to GitHub.
4. Copy the service account's e-mail address
   (`jig2-dashboard@<project-id>.iam.gserviceaccount.com`).

> If your organisation blocks key creation (policy
> `iam.disableServiceAccountKeyCreation`), ask your Google Workspace admin to allow it
> for this project.

### 4. Share the Google Sheet with the service account

Open the Jig 2 Google Sheet → **Share** → paste the service-account e-mail →
role **Viewer** → untick "Notify people" → **Share**.
Without this step the app shows *"Spreadsheet not found or not shared"*.

### 5. Streamlit secrets

Open the downloaded JSON and copy each field into the TOML format shown in
`.streamlit/secrets.toml.example` under `[gcp_service_account]`. Keep `private_key`
inside triple quotes exactly as it appears in the JSON.

Optional `[app_settings]`:

* `display_timezone` (default `Asia/Kolkata`) — the time zone the logger writes in, also used for refresh times.
* `timestamp_order` (default `auto`) — only matters if column A contains text dates rather than real date cells.

**Run locally (optional):**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill in real values
streamlit run app.py
```

### 6. Upload to GitHub

1. Create a new **private** repository on <https://github.com/new>, e.g. `jig2-o2-dashboard`.
2. Upload `app.py`, `requirements.txt`, `README.md`, `.gitignore` and the
   `.streamlit` folder (with `config.toml` and `secrets.toml.example` only).
   Using the web UI: **Add file → Upload files**, drag the files in, **Commit changes**.
   Using Git:

   ```bash
   git init
   git add app.py requirements.txt README.md .gitignore .streamlit/config.toml .streamlit/secrets.toml.example
   git commit -m "Jig 2 O2 sensor live dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-user>/jig2-o2-dashboard.git
   git push -u origin main
   ```

   Do **not** upload `secrets.toml` or the JSON key (`.gitignore` blocks them).

### 7. Deploy on Streamlit Community Cloud

1. Go to <https://share.streamlit.io/> and sign in with GitHub.
2. **Create app → Deploy a public app from GitHub** (works with private repos too).
3. Select your repository, branch `main`, main file `app.py`.
4. **Advanced settings** → Python version **3.11** (3.12 also works) →
   paste the full contents of your `secrets.toml` into **Secrets** → **Save**.
5. **Deploy**. The first build takes a few minutes.
6. To limit who can open the dashboard: app **Settings → Sharing** → make it
   private and invite viewers by e-mail.

To change secrets later: app menu **⋮ → Settings → Secrets** (the app restarts automatically).

---

## Operating notes

* **Live refresh** runs while a browser tab has the dashboard open. Streamlit
  Community Cloud puts apps to sleep after a period with no visitors; the next
  visitor wakes it (about 30 s) and the baseline is rebuilt from the 21 Oct rows.
* **API usage:** one Sheets read per minute, shared by all viewers through a 50 s
  cache — far below Google's default quota.
* **Timestamps** are taken as local time in `display_timezone`. If the logger
  writes UTC, set `display_timezone = "UTC"`.
* **Do not edit 21 Oct rows** in the sheet. If they change, the dashboard keeps the
  locked baseline and shows a warning. After a restart it would rebuild from the edited rows.
* **Readings after 11 Dec 2026** are ignored by design and counted as "outside study window".

## Troubleshooting

| Message | Fix |
|---|---|
| `Streamlit secret [gcp_service_account] is missing` / `No secrets found` | Add secrets (step 5 or 7.4). |
| `Spreadsheet not found or not shared` | Share the sheet with the service-account e-mail (step 4). |
| `Worksheet 'Sheet1' was not found` | Make sure the data tab is named exactly `Sheet1`. |
| `Google Sheets API error … has not been used in project` | Enable the Sheets and Drive APIs (step 2), wait a minute, then click **Refresh Now**. |
| `Baseline unavailable` | No valid 21 Oct 2026 readings were found. Check column A dates and the values for that day. |
| Wrong day/month on text dates | Set `timestamp_order = "dayfirst"` or `"monthfirst"` in `[app_settings]`. |
