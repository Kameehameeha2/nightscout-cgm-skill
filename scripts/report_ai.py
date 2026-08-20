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

# claude-opus-5 thinks by default and max_tokens caps thinking + text together,
# so 8k risked truncating the report mid-section.
MAX_TOKENS = 16000
# "high" is the model default and what produced the reports worth keeping —
# the report's depth is the whole point, so don't trade it for latency.
# Set REPORT_EFFORT=medium (or low) to trade depth for speed.
EFFORT = os.environ.get("REPORT_EFFORT", "high")

# The user runs a fully automated loop; this context stops the model from
# suggesting manual-bolus / standard-pump advice that doesn't apply.
DEFAULT_THERAPY_CONTEXT = (
    "The person runs a fully automated closed loop (Trio on iPhone, oref-style "
    "algorithm with Dynamic ISF + UAM) on Lyumjev. They do NOT give manual "
    "boluses — the algorithm delivers everything via SMBs. Their DIA and custom "
    "insulin peak time are deliberate, chosen for an ultra-rapid insulin in the "
    "oref model; read the actual values from trio_settings rather than assuming "
    "them, and do not propose shortening DIA below the 8-10 h the oref curve "
    "expects. Only discuss algorithm/profile settings that fit a full closed "
    "loop (basal shape, ISF, carb ratios, targets, dynISF adjustment factor, "
    "UAM/SMB limits, temp targets, override presets, advance carb announcement)."
)

SYSTEM_PROMPT = """You are a diabetes data analyst producing a written report from a person's \
own Nightscout CGM + pump/treatment history. You are given already-computed \
statistics (never raw readings), their live pump profile, and — when they have \
exported it — `trio_settings`, the full set of preferences from the loop app \
itself (Nightscout does not receive these, so they are absent otherwise).

Hard rules:
- NEVER state, assume, or reason from a numeric value for a setting that is not \
present in the JSON you were given. Nightscout carries the profile (basal, ISF, \
carb ratios, targets) but NOT the algorithm preferences — dynISF \
adjustmentFactor, SMB and UAM limits, maxUAMSMBBasalMinutes, thresholds. If \
`trio_settings` is absent or lacks a setting you would want, say plainly that it \
was not provided and ask for it; naming a lever is fine, inventing its current \
value is not. When `trio_settings` IS present, use those actual values and quote \
them.
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
        # Exported Trio preferences (dynISF adjustmentFactor, SMB/UAM limits…).
        # Nightscout never sees these, so they're absent unless supplied.
        "trio_settings": analysis.get("trio_settings"),
    }
    # drop empty keys so the model isn't distracted by nulls
    payload = {k: v for k, v in payload.items() if v not in (None, {}, [])}
    return json.dumps(payload, indent=1, default=str)


class ReportStream:
    """Iterable of report chunks, sized for Streamlit's `st.write_stream`.

    Iterating yields Markdown as Claude writes it, so the reader watches the
    report build rather than staring at a spinner for the whole generation.
    The outcome fields (`ok`, `markdown`, `model`, `error`) are populated by the
    time iteration finishes; the common failures (missing key/SDK, 401, refusal,
    API error) set `error` and yield an explanation instead of raising.
    """

    def __init__(self, analysis: dict, therapy_context: str | None = None,
                 model: str | None = None):
        self.analysis = analysis
        self.therapy_context = therapy_context or DEFAULT_THERAPY_CONTEXT
        self.model = model or DEFAULT_MODEL
        self.ok = False
        self.markdown = ""
        self.error = None

    def _fail(self, error: str, markdown: str = "") -> str:
        self.error = error
        self.markdown = markdown
        return markdown

    def __iter__(self):
        if not self.analysis or self.analysis.get("error"):
            self._fail(self.analysis.get("error", "no analysis")
                       if self.analysis else "no analysis")
            return

        if not os.environ.get("ANTHROPIC_API_KEY"):
            yield self._fail("ANTHROPIC_API_KEY not set", _no_key_message())
            return

        try:
            import anthropic
        except ImportError:
            self._fail("The `anthropic` package is not installed.")
            return

        user_msg = (
            "Here is the computed analysis of my own CGM + pump history as JSON. "
            "Write the report per your instructions, using these exact numbers.\n\n"
            f"```json\n{_context_payload(self.analysis, self.therapy_context)}\n```"
        )

        parts: list[str] = []
        try:
            client = anthropic.Anthropic()
            with client.messages.stream(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                for text in stream.text_stream:
                    parts.append(text)
                    yield text
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            yield self._fail("Anthropic rejected the API key (401). See below.",
                             _bad_key_message())
            return
        except anthropic.APIError as e:  # network/status/rate-limit/etc.
            # Keep whatever streamed before the failure — a partial report still
            # beats an empty pane.
            self._fail(f"Claude API error: {e}", "".join(parts))
            return

        self.markdown = "".join(parts).strip()
        if final.stop_reason == "refusal":
            self._fail("The request was declined by the model's safety system.")
            return
        self.ok = bool(self.markdown)
        if not self.ok:
            self.error = "Empty response from the model."


def generate_report(analysis: dict, therapy_context: str | None = None,
                    model: str | None = None) -> dict:
    """Blocking wrapper over ReportStream, for non-streaming callers (CLI, tests).

    Returns {'ok': bool, 'markdown': str, 'model': str, 'error': str|None}.
    """
    stream = ReportStream(analysis, therapy_context, model)
    text = "".join(stream)
    return {"ok": stream.ok, "markdown": stream.markdown or text,
            "model": stream.model, "error": stream.error}


def _no_key_message() -> str:
    return (
        "### AI report unavailable\n\n"
        "No `ANTHROPIC_API_KEY` is configured, so the written analysis + "
        "settings suggestions can't be generated. The charts and statistics "
        "above are still fully computed.\n\n"
        "Add your Anthropic API key to the app's secrets to enable this section."
    )


def _bad_key_message() -> str:
    return (
        "### AI report unavailable — the API key was rejected\n\n"
        "Anthropic returned **401 invalid x-api-key**. The charts and statistics "
        "above are unaffected. Check, in this order:\n\n"
        "1. The key is an **API key from <https://console.anthropic.com>** "
        "(starts with `sk-ant-api03-`). A Claude.ai or Claude Code subscription "
        "login is *not* an API key and will always 401 here.\n"
        "2. It was pasted **whole** — these keys are long and are easy to "
        "truncate mid-copy.\n"
        "3. In the Secrets box it reads "
        "`ANTHROPIC_API_KEY = \"sk-ant-api03-...\"` — one line, plain double "
        "quotes, no extra quotes inside the value.\n"
        "4. The key hasn't been **revoked**, and the workspace it belongs to "
        "has credit.\n\n"
        "Saving Secrets restarts the app; the report works on the next click."
    )


# --------------------------------------------------------------------------- #
# Follow-up chat about a generated report
# --------------------------------------------------------------------------- #
CHAT_SYSTEM = """You are discussing a CGM/pump data report that you wrote for this \
person, from their own Nightscout history and their exported loop settings. Both \
the computed analysis and the report itself are given to you below.

Answer their follow-up questions about it. Same hard rules as the report:
- NEVER state, assume, or reason from a numeric value for a setting that is not \
in the analysis JSON. If they ask about something you weren't given, say so and \
ask them to supply it — naming a lever is fine, inventing its current value is \
not.
- This is a data-pattern analysis of past readings, not medical advice. Any \
settings change is for them and their care team, one variable at a time, \
re-assessed over 1–2 weeks.
- They run a fully automated closed loop and do NOT give manual boluses or \
corrections. Never suggest manual dosing, pre-bolusing or split boluses; discuss \
only algorithm and profile settings.
- Hypoglycemia is the binding safety constraint. Judge every idea by whether it \
pushes lows up, and say so when it does.
- Be quantitative and cite the actual numbers from the analysis. If the data \
cannot answer the question, say that plainly instead of speculating.

Style: this is a conversation, not a report. Answer the question directly and \
concisely, lead with the answer, and don't restate the whole report or re-run \
its disclaimers every turn. Markdown is fine; keep it light."""


def context_payload(analysis: dict, therapy_context: str | None = None) -> str:
    """The exact JSON a report was written from — stored so chat can reuse it."""
    return _context_payload(analysis, therapy_context or DEFAULT_THERAPY_CONTEXT)


class ChatStream:
    """Streams one assistant reply in a follow-up conversation.

    Iterating yields text chunks for st.write_stream; `ok`, `text` and `error`
    are populated by the time iteration ends. The analysis + report block is
    marked for prompt caching, so every follow-up re-reads that prefix at about
    a tenth of the input cost instead of paying for it again.
    """

    def __init__(self, question: str, history: list[dict], context_json: str,
                 report_md: str, model: str | None = None):
        self.question = question
        self.history = history or []
        self.context_json = context_json
        self.report_md = report_md
        self.model = model or DEFAULT_MODEL
        self.ok = False
        self.text = ""
        self.error = None

    def __iter__(self):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.error = "ANTHROPIC_API_KEY not set"
            yield "No Anthropic API key is configured, so chat is unavailable."
            return
        try:
            import anthropic
        except ImportError:
            self.error = "The `anthropic` package is not installed."
            return

        context_block = (
            "Here is the analysis the report was written from, and the report "
            "itself.\n\n<analysis_json>\n" + self.context_json +
            "\n</analysis_json>\n\n<report>\n" + self.report_md + "\n</report>"
        )
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in self.history if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": self.question})

        parts: list[str] = []
        try:
            client = anthropic.Anthropic()
            with client.messages.stream(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                system=[
                    {"type": "text", "text": CHAT_SYSTEM},
                    # Cached: the analysis and report don't change between turns.
                    {"type": "text", "text": context_block,
                     "cache_control": {"type": "ephemeral"}},
                ],
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
                    yield chunk
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            self.error = "Anthropic rejected the API key (401)."
            yield "\n\n*The API key was rejected — see Settings.*"
            return
        except anthropic.APIError as e:
            self.error = f"Claude API error: {e}"
            self.text = "".join(parts)
            return

        self.text = "".join(parts).strip()
        if final.stop_reason == "refusal":
            self.error = "The request was declined by the model's safety system."
            return
        self.ok = bool(self.text)
        if not self.ok:
            self.error = "Empty response from the model."
