# weather-bench

Forecast verification for Finland: do the new AI weather models (ECMWF AIFS) beat
the incumbents' products (Foreca, Ilmatieteen laitos) and the physics models they
are built on (ECMWF IFS)?

Verification truth: hourly 2 m temperature from six FMI observation stations
(Helsinki Kaisaniemi, Tampere, Oulu, Rovaniemi, Turku, Jyväskylä), via FMI open data.
All forecast sources are requested/interpolated at the exact station coordinates.

## Two experiments

### 1. Retrospective (`retro.py`) - runnable any time
Open-Meteo's previous-runs archive stores what each model forecast N days ahead,
as issued at the time. Scored against FMI observations over the past ~60 days.
Covers models only (IFS, AIFS, MET Nordic day-1) - there is no public archive of
Foreca's or FMI's *edited* past forecasts, hence experiment 2.

Result 2026-07-23 (60 days, hourly t2m MAE °C, 6 cities pooled):

| lead | AIFS (AI) | IFS (physics) | MET Nordic |
|------|-----------|---------------|------------|
| d1   | 1.05      | 1.06          | 1.23       |
| d3   | 1.30      | 1.37          | -          |
| d5   | 1.71      | 1.93          | -          |
| d7   | 2.09      | 2.41          | -          |

AIFS ≥ IFS at every lead; the edge grows with range (day 7: ~13% lower MAE,
i.e. AIFS at day 7 ≈ IFS at day 6).

### 2. Prospective head-to-head (`collect.py` + `score.py`) - accrues over weeks
`collect.py` snapshots, per city, at the same wall-clock moment (like a user
opening all the apps side by side):

- **foreca** - daily tmin/tmax/rain, ~13 days (`api.foreca.net/data/daily/<id>.json`,
  the open endpoint their own site uses; id = 100000000 + geonames id)
- **fmi_edited** - FMI's human-curated hourly forecast, ~10 days
  (open WFS `fmi::forecast::edited::weather::scandinavia::point::simple`)
- **ecmwf_ifs025 / ecmwf_aifs025_single / metno_nordic** - hourly, 10 days (Open-Meteo)
- **obs** - last 48 h of station observations (idempotent upsert)

`score.py` then reports MAE by lead day: hourly t2m (all hourly sources) and
daily tmin/tmax (adds Foreca, whose feed is daily-only). Daily values for hourly
sources and observations are min/max over Europe/Helsinki local days; Foreca's
native daily values are trusted as-is.

Foreca comparability caveat: Foreca's daily numbers are for the *city*, not the
station point, and its true skill lives partly in its hourly product we cannot
see. The daily board is still the like-for-like consumer-facing comparison.

## Running

```bash
python3 retro.py [days]   # retrospective benchmark (default 60)
python3 collect.py        # one prospective snapshot (~30 s; run every 6-12 h)
python3 score.py          # score accrued snapshots (meaningful after ~2+ days)
```

Stdlib only, no venv. Data in `data/bench.sqlite` (tables: `forecasts`,
`observations`, `retro_forecasts`, `collect_log`); results JSON alongside.

Collection cadence is handled inside the long-running Claude Code session via
self-scheduled wakeups (target: collect every ~6 h). If the session dies, just
run `collect.py` twice a day by hand or wire it to launchd (note: launchd cannot
read `~/Documents` due to TCC - copy the scripts to `~/Library` first, see the
flagged-notifier pattern in the SocialHuman repo).

Verification conventions: forecast lead = target time minus snapshot time (fair:
every source is snapshotted simultaneously). Station-point verification slightly
favors high-resolution/post-processed sources (MET Nordic, FMI edited) over raw
0.25° global models - that is the "what users experience" comparison, kept
deliberately. GraphCast is absent: Open-Meteo's feed for it returns nulls (dead).
