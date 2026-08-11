#!/usr/bin/env python3
"""
Claude-powered report brain.

Takes the computed analysis (from analysis.py) plus the live Trio profile and
asks Claude to (1) surface the patterns in the data, (2) sanity-check them
against the pump settings, and (3) suggest cautious, settings-only experiments
to discuss with a care team — in the same careful register as the hand-written
GLUCOSE_REPORT.md.

Design notes:
  - Uses the official Anthropic SDK (`pip install anthropic`), model default
    claude-opus-5 (override with REPORT_MODEL). ANTHROPIC_API_KEY is read from
    the environment / Streamlit secrets.
  - Streams the response (long output) and returns the assembled Markdown.
  - Fails soft: if the key or SDK is missing, returns a clear message instead
    of raising, so the dashboard still renders the charts.
  - This is a data-pattern analysis, NOT medical advice — the system prompt
    enforces that framing and the user's closed-loop (no-manual-bolus) context.
"""
from __future__ import annotations

import json
import os

DEFAULT_MODEL = os.environ.get("REPORT_MODEL", "claude-opus-5")

# The user runs a fully automated loop; this context stops the model from
# suggesting manual-bolus / standard-pump advice that doesn't apply.
DEFAULT_THERAPY_CONTEXT = (
    "The person runs a fully automated closed loop (Trio on iPhone, oref-style "
    "algorithm with Dynamic ISF + UAM). They do NOT give manual boluses — the "
    "algorithm delivers everything via SMBs. DIA of 10 h and an insulin peak "
    "time around 50 min are correct for their fast insulin (Lyumjev) and are "
    "NOT levers to change. Only discuss algorithm/profile settings that fit a "
    "full closed loop (basal shape, ISF, carb ratios, targets, dynISF "
    "adjustmentFactor, UAM/SMB limits, temp targets, advance carb announcement)."
)

SYSTEM_PROMPT = """You are a diabetes data analyst producing a written report from a person's \
own Nightscout CGM + pump/treatment history. You are given already-computed \
statistics (never raw readings) and their live pump profile.

Hard rules:
- This is a DATA-PATTERN ANALYSIS of past readings, not medical advice. Say so \
once, near the top. Settings changes must be made only with their care team, \
one variable at a time, re-assessed over 1–2 weeks, and any low treated first.
- You cannot see meals, activity, illness, insulin timing, or set changes. Be \
explicit about that limitation and about confounding wherever it matters.
- Be honest and quantitative. Cite the actual numbers you were given. If the \
data does not support a recommendation, say so and downgrade it — do not invent \
a clean fix where none exists. Hypoglycemia (time-low) is the binding safety \
constraint; judge every suggestion by whether it pushes lows up.
- Respect the person's therapy setup exactly as described; never suggest \
changes that don't fit a full closed loop.

Output GitHub-flavored Markdown with these sections, in order:
1. **Headline results** — a table of TIR / GMI / mean / CV / time-low / time-high \
vs. consensus goals, with a one-line verdict each, then a 2–3 sentence bottom line.
2. **The daily pattern** — where control breaks down by time of day (name the \
worst hour windows for highs and lows, with numbers).
3. **Day-of-week & trend** — anything notable across weekdays and over the period.
4. **Settings check** — walk their basal / ISF / carb-ratio / targets against \
what the glucose curves show; flag mismatches (e.g. a missing dedicated dinner \
ratio) and confirm what looks well-tuned.
5. **Cautious experiments to discuss with your care team** — 2–4 options, each \
labelled with an honest confidence level and its trade-off. Frame as hypotheses, \
not fixes.
6. **Safety note** — restate the lows constraint.

Keep it focused and readable; lead with the outcome in each section. Do not pad."""


def _context_payload(analysis: dict, therapy_context: str) -> str:
    """Assemble the compact JSON we hand to Claude (stats only, no raw data)."""
    payload = {
        "therapy_context": therapy_context,
        "period": analysis.get("meta"),
        "overall_stats": analysis.get("stats"),
        "time_in_range_bands_pct": analysis.get("tir"),
        "monthly": analysis.get("months"),
        "hourly_agp": analysis.get("hours"),
        "day_of_week": analysis.get("dows"),
        "overnight_drift": analysis.get("overnight"),
        "insulin_summary": analysis.get("insulin"),
        "announced_vs_unannounced_dinner": analysis.get("dinner"),
        "pump_profile": analysis.get("profile"),
    }
    # drop empty keys so the model isn't distracted by nulls
    payload = {k: v for k, v in payload.items() if v not in (None, {}, [])}
    return json.dumps(payload, indent=1, default=str)


def generate_report(analysis: dict, therapy_context: str | None = None,
                    model: str | None = None) -> dict:
    """Return {'ok': bool, 'markdown': str, 'model': str, 'error': str|None}.

    Never raises for the common failure modes (missing key/SDK, refusal, API
    error) — the caller renders `markdown` either way.
    """
    if not analysis or analysis.get("error"):
        return {"ok": False, "markdown": "", "model": None,
                "error": analysis.get("error", "no analysis")}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "markdown": _no_key_message(), "model": None,
                "error": "ANTHROPIC_API_KEY not set"}

    try:
        import anthropic
    except ImportError:
        return {"ok": False, "markdown": "", "model": None,
                "error": "The `anthropic` package is not installed."}

    model = model or DEFAULT_MODEL
    context = _context_payload(analysis, therapy_context or DEFAULT_THERAPY_CONTEXT)
    user_msg = (
        "Here is the computed analysis of my own CGM + pump history as JSON. "
        "Write the report per your instructions, using these exact numbers.\n\n"
        f"```json\n{context}\n```"
    )

    try:
        client = anthropic.Anthropic()
        with client.messages.stream(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            final = stream.get_final_message()
    except anthropic.APIError as e:  # network/status/rate-limit/etc.
        return {"ok": False, "markdown": "", "model": model,
                "error": f"Claude API error: {e}"}

    if final.stop_reason == "refusal":
        return {"ok": False, "markdown": "", "model": model,
                "error": "The request was declined by the model's safety system."}

    markdown = "".join(b.text for b in final.content if b.type == "text").strip()
    return {"ok": bool(markdown), "markdown": markdown, "model": model,
            "error": None if markdown else "Empty response from the model."}


def _no_key_message() -> str:
    return (
        "### AI report unavailable\n\n"
        "No `ANTHROPIC_API_KEY` is configured, so the written analysis + "
        "settings suggestions can't be generated. The charts and statistics "
        "above are still fully computed.\n\n"
        "Add your Anthropic API key to the app's secrets to enable this section."
    )
