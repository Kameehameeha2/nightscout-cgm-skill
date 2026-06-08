# Nightscout CGM Skill - Improvement Status

This document records the current enhancement status and the scope decisions for this repository. The skill is intentionally an on-demand AI/CLI tool, not a long-running desktop or mobile application.

## Implemented Capabilities

| Area | Issue | Current behavior |
|------|-------|------------------|
| Meal/event correlations | [#4](https://github.com/shanselman/nightscout-cgm-skill/issues/4) | `events --tag TEXT` analyzes glucose response around existing Nightscout treatment/event data. It does not create a separate local annotation editor. |
| Trend alerts | [#5](https://github.com/shanselman/nightscout-cgm-skill/issues/5) | `alerts --days N` surfaces recurring low/high patterns from local CGM data. |
| Period comparison | [#6](https://github.com/shanselman/nightscout-cgm-skill/issues/6) | `compare --period1 P1 --period2 P2` compares TIR, GMI, average glucose, variability, and related deltas. |
| AGP reports | [#7](https://github.com/shanselman/nightscout-cgm-skill/issues/7) | `agp --days N` generates a clinical-style Ambulatory Glucose Profile report. |
| PDF export | [#8](https://github.com/shanselman/nightscout-cgm-skill/issues/8) | HTML reports include print-friendly CSS and a browser Print / Save PDF action, avoiding extra PDF dependencies. |
| Goal tracking | [#9](https://github.com/shanselman/nightscout-cgm-skill/issues/9) | `goals view/set/clear` stores local goals for TIR, CV, GMI, and average glucose and renders goal status cards in reports. |
| Refresh-on-query | [#10](https://github.com/shanselman/nightscout-cgm-skill/issues/10) | `auto-refresh view/set/on/off` configures stale-data refresh before read-only commands. This replaces daemon behavior with an on-demand model. |
| CAGE/SAGE/IAGE | [#36](https://github.com/shanselman/nightscout-cgm-skill/issues/36) | `ages --count N` reports site, sensor, and insulin change ages from Nightscout treatment events. |
| Report navigation | [#29](https://github.com/shanselman/nightscout-cgm-skill/issues/29) | Reports include sticky section navigation and anchors for long pages. |
| Floating report actions | [#28](https://github.com/shanselman/nightscout-cgm-skill/issues/28) | Report actions are responsive, print-safe, and no longer overlap content. |
| Modal Day target lines | [#27](https://github.com/shanselman/nightscout-cgm-skill/issues/27) | Modal Day charts include target low/high lines using native Chart.js datasets. |
| Executive summary | [#26](https://github.com/shanselman/nightscout-cgm-skill/issues/26) | Reports include deterministic summary status and concise bullets. |
| Weekly context | [#25](https://github.com/shanselman/nightscout-cgm-skill/issues/25) | Weekly summary includes TIR delta, best day, and context notes. |

## Explicitly Deferred or Out of Scope

| Area | Issue | Decision |
|------|-------|----------|
| Notification integration | [#11](https://github.com/shanselman/nightscout-cgm-skill/issues/11) | Deferred as out of scope. Real notifications imply a daemon, scheduler, or OS-specific desktop integration. |
| Multiple profiles | [#12](https://github.com/shanselman/nightscout-cgm-skill/issues/12) | Deferred as out of scope. The safer model is one Nightscout profile per skill install/environment. |
| ML pattern detection | [#13](https://github.com/shanselman/nightscout-cgm-skill/issues/13) | Deferred as out of scope. Deterministic, explainable local analysis is preferred over adding an ML dependency/model layer. |

## Scope Principles

- Keep the skill privacy-first and local.
- Prefer deterministic, explainable analysis over opaque inference.
- Avoid background services unless this repository intentionally becomes an application.
- Avoid new heavy dependencies when browser, Python standard library, or existing data can solve the problem.
- Treat Nightscout as the source of truth for treatments/events instead of maintaining a second annotation system.
