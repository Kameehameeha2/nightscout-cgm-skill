# The CGM dashboard — running it and changing it

`app.py` is a Streamlit dashboard over your Nightscout history. It is **deployed
and live**:

**<https://thomasdata.streamlit.app>** — hosted on Streamlit Community Cloud,
behind a password gate, reachable from any device.

## What the app does

- **Refresh all history (1 click)** — fetches a full year from Nightscout into
  the local SQLite DB, so every date range (1 day … 1 year) is instantly
  available. It also auto-refreshes hourly and on cold start, so the app is
  current whenever you open it.
- **Generate full report (1 click)** — computes the AGP, hourly out-of-range,
  day×hour heatmap, and a **Loopalyzer average day** (glucose median/IQR +
  delivered vs. scheduled basal + boluses + carbs), then asks Claude to find the
  patterns, check them against your live Trio profile, and suggest cautious
  settings experiments to discuss with your care team. The written report
  **streams in** as it's generated — expect a pause before the first words while
  the model reasons, then continuous text.

---

## Deploying a change

Streamlit Cloud watches the repo and redeploys on push:

```bash
git push mine master
```

- **`mine`** = `Kameehameeha2/nightscout-cgm-skill` — your repo, the one Streamlit
  deploys from. A fix isn't live until it's pushed here.
- **`origin`** = `shanselman/nightscout-cgm-skill` — the public upstream skill
  repo. Don't push there.
- The branch is **`master`**. There is no `main`.

Code-only changes restart the app in under a minute. Touching
`requirements.txt` triggers a full dependency reinstall (2–3 min).

---

## Secrets

Set these in **App settings → Secrets** on Streamlit Cloud, or in a local
`.streamlit/secrets.toml` (gitignored — see `.streamlit/secrets.toml.example`).
They must be **top-level TOML keys**, not nested under a `[section]` header.

| Key | Required | Value |
|---|---|---|
| `NIGHTSCOUT_URL` | **Yes** | Your Nightscout site. Bare URL or the full `/api/v1/entries.json` — both work, the app normalizes it. |
| `ANTHROPIC_API_KEY` | For the AI report | A console key from <https://console.anthropic.com>, starting `sk-ant-api03-`. Without it the charts and stats still work; only the narrative is skipped. |
| `APP_PASSWORD` | Strongly recommended | The login gate. Without it, your health dashboard is open to anyone with the URL. |
| `REPORT_MODEL` | No | Defaults to `claude-opus-5`. |
| `REPORT_EFFORT` | No | Defaults to `high`. `medium` / `low` are faster but produce a shallower analysis — the report depth is the point, so leave it alone unless you specifically want speed. |
| `DISPLAY_UNITS` | No | `mmol` or `mgdl`. Omit to follow the Nightscout server setting. |

Saving Secrets restarts the app.

---

## Running locally

```bash
python -m pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in
python -m streamlit run app.py
```

Environment variables work instead of a secrets file — the app reads both. A
local `cgm_data.db` means it renders immediately rather than cold-fetching.

---

## Troubleshooting

Problems we've actually hit, and what they turned out to be:

| Symptom | Cause |
|---|---|
| `Failed to download the sources` at deploy | Streamlit's GitHub authorization can't read the repo. It was private under an org while Streamlit was logged in as another account; making the repo public fixed it. |
| Deploy hangs at `Spinning up manager process` with no `Cloning repository` line | It never reached your code — infrastructure or repo access, never a bug in `app.py`. Reboot the app; if it persists, check repo access. |
| `401 invalid x-api-key` in the report pane | The Anthropic key. It must be a **console API key**, not a Claude.ai / Claude Code subscription login. Also check it was pasted whole and hasn't been revoked, and that the workspace has credit. |
| `KeyError` in the report charts | Fixed. `analysis.py` keys `hours` by `int`; the chart code looked them up with `str(h)`, which only worked on the old CLI path where the result went through JSON. |

---

## Notes & limits

- **Data freshness:** there's no always-on worker on Community Cloud, so the app
  refreshes when someone opens it (hourly-cached + on cold start). For true 24/7
  background refresh you'd need an external cron or a self-hosted container
  (Fly.io / Railway).
- **Report cost:** each AI report is a few cents of Anthropic usage.
- **Privacy:** `.gitignore` keeps your data out of git — `*.db`, `config.json`,
  generated `*.html`, the analysis JSON artifacts, and `.streamlit/secrets.toml`
  are all untracked, and the repo contains no CGM readings or keys. **The repo is
  public**, so keep it that way: secrets belong only in Streamlit's Secrets box or
  your local untracked `secrets.toml`, never in a commit. The app sends only
  *computed statistics* + your pump profile to Claude — never raw readings.
- **Custom domain:** a full custom domain (`cgm.example.com`) isn't available on
  Community Cloud; the `*.streamlit.app` subdomain is editable in
  App settings → General.
- **Not medical advice.** The report is a data-pattern analysis; make settings
  changes only with your care team, one variable at a time, treating lows first.
