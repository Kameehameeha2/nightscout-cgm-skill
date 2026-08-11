# Deploying the CGM dashboard to the cloud

This hosts `app.py` on **Streamlit Community Cloud** so you can open it from any
device, always on. It's your closed-loop health data, so the steps below keep it
out of any public repo and behind a password.

## What the app does now

- **Refresh all history (1 click)** — fetches a full year from Nightscout into
  the local SQLite DB, so every date range (1 day … 1 year) is instantly
  available. It also auto-refreshes hourly and on cold start, so the app is
  current whenever you open it.
- **Generate full report (1 click)** — computes the AGP, hourly out-of-range,
  day×hour heatmap, and a **Loopalyzer average day** (glucose median/IQR +
  delivered vs. scheduled basal + boluses + carbs), then asks Claude to find the
  patterns, check them against your live Trio profile, and suggest cautious
  settings experiments to discuss with your care team.

---

## Step A — Put the project in your OWN private GitHub repo

The current git remote is `shanselman/nightscout-cgm-skill` (the public skill
repo — you can't deploy your data from there). Create your own **private** repo:

```bash
# from the project folder
gh repo create my-nightscout-dashboard --private --source . --remote myrepo
git push myrepo master
```

(or create an empty private repo on github.com and
`git remote add myrepo <url>` then `git push myrepo master`.)

Your CGM data never gets committed — `.gitignore` already excludes `*.db`,
`*.html`, `config.json`, and `.streamlit/secrets.toml`. The cloud app rebuilds
its database by fetching from Nightscout on startup.

## Step B — Get an Anthropic API key

Create one at <https://console.anthropic.com> (you've already added funds). It
powers the written report + settings suggestions. Without it the charts and
stats still work; only the AI narrative is skipped.

## Step C — Deploy on Streamlit Community Cloud

1. Go to <https://share.streamlit.io> and sign in with the GitHub account that
   owns the private repo.
2. **Create app** → pick `my-nightscout-dashboard`, branch `master`, main file
   `app.py`.
3. **Advanced settings → Secrets** — paste (see `.streamlit/secrets.toml.example`):

   ```toml
   NIGHTSCOUT_URL   = "https://your-nightscout-site"
   ANTHROPIC_API_KEY = "sk-ant-..."
   APP_PASSWORD     = "choose-a-strong-password"
   # REPORT_MODEL   = "claude-opus-5"   # optional
   # DISPLAY_UNITS  = "mmol"            # optional
   ```

4. **Deploy.** First load fetches a year of data (can take a minute or two);
   after that it's cached and fast.

The `APP_PASSWORD` puts a login in front of the app so it isn't openly public.

---

## Running locally

```bash
python -m pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in
python -m streamlit run app.py
```

(You can also keep using `NIGHTSCOUT_URL` / `ANTHROPIC_API_KEY` as environment
variables instead of a secrets file — the app reads both.)

---

## Notes & limits

- **Data freshness on Streamlit Cloud:** there's no always-on background worker,
  so the app refreshes when someone opens it (hourly-cached + on cold start).
  That matches "current whenever I open it." If you want true 24/7 background
  refresh, a small external cron hitting Nightscout or a self-hosted
  container (Fly.io/Railway) would be the next step.
- **Report cost:** each AI report is a few cents of Anthropic usage.
- **Privacy:** the app sends only *computed statistics* + your pump profile to
  Claude — never raw readings — and nothing is written to any public location.
- **Not medical advice.** The report is a data-pattern analysis; make settings
  changes only with your care team, one variable at a time, treating lows first.
```
