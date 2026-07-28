# weather-bench

Forecast verification for Finland: do the new AI weather models (ECMWF AIFS) and
the open pipeline a solo developer would ship (Open-Meteo best_match) beat the
incumbents' products (Foreca, Ilmatieteen laitos) and the physics models they
are built on (ECMWF IFS)?

Verification truth: hourly 2 m temperature, wind and precipitation from **14 FMI
observation stations** (Helsinki Kaisaniemi, Tampere, Oulu, Rovaniemi, Turku,
Jyväskylä, Vaasa, Kuopio, Joensuu, Lappeenranta, Pori, Kajaani, Sodankylä,
Mariehamn - all Finnish climate zones), via FMI open data. All forecast sources
are requested/interpolated at the exact station coordinates; the station
identity is asserted on every fetch (drift raises an error).

## Pre-registered claim protocol (fixed 2026-07-23, before data accrual)

- **Primary endpoint**: hourly 2 m temperature MAE, leads 1-7 pooled,
  `ecmwf_aifs025_single` vs `foreca`, matched pairs, tested at alpha=0.05.
- All other pairwise cells (best_match, fmi_edited, per-lead splits) are
  **secondary** and Holm-corrected.
- Inference: circular moving-block bootstrap over consecutive local target dates
  (block length 5; sensitivity reported at 3/7) - accounts for synoptic
  persistence across adjacent days. Point estimate is the empirical matched-pair
  MAE difference; the bootstrap provides interval and p-value only.
- No "significant" verdict prints before 20 distinct target dates.
- A defensible public claim is **narrow**: "over [period], at [the 14 named FMI
  stations], for lead days 1-7, X had Z% lower hourly 2 m temperature MAE than
  Y's published forecast (95% CI, pre-registered endpoint)" - not "more accurate
  forecasts for Finland" in general.

## Two experiments

### 1. Retrospective (`retro.py`) - runnable any time
Open-Meteo's previous-runs archive stores what each model forecast N days ahead,
as issued at the time. Scored against FMI observations over the past ~90 days on
**matched samples** (a sample counts only where IFS, AIFS and best_match all
have values; metno_nordic is day-1-only and scored on its own samples). Covers
models only - there is no public archive of Foreca's or FMI's *edited* past
forecasts, hence experiment 2. Each run wipes and refetches the retro table
(descriptive, no significance testing - the prospective experiment is the
claim-bearing one).

Result 2026-07-23 (90 days, 14 cities, hourly t2m MAE °C): AIFS beats IFS at
every lead; day 1: 1.11 vs 1.13, day 4: 1.52 vs 1.64, day 7: 2.17 vs 2.44
(~13% lower MAE at day 7, i.e. AIFS day 7 ≈ IFS day 6).

### 2. Prospective head-to-head (`collect.py` + `score.py`) - accrues over weeks
`collect.py` snapshots, per city, at the same wall-clock moment (like a user
opening all the apps side by side; `run_time` has minute precision so reruns
never merge):

- **foreca** - two feeds: native daily tmin/tmax/rain, ~13 days
  (`api.foreca.net/data/daily/<id>.json`, the open endpoint their site uses;
  id = 100000000 + geonames id), plus hourly t2m/ws/rain1h, ~11 days, parsed
  from the server-rendered `hour_data` JS blob on `foreca.fi/<path>/details`
  pages (local-time keys, converted to UTC)
- **fmi_edited** - FMI's human-curated hourly t2m/ws/rain1h, ~10 days
  (open WFS `fmi::forecast::edited::weather::scandinavia::point::simple`)
- **ecmwf_ifs025 / ecmwf_aifs025_single / metno_nordic / best_match** - hourly
  t2m/ws/rain1h, 16 days requested (IFS/AIFS deliver ~15, MET Nordic ~2.5) via
  Open-Meteo, wind in m/s; an empty model/var feed raises (a dead feed must not
  be masked by healthy ones)
- **ecmwf_aifs_ens_mean** - ECMWF AIFS *ensemble* (51 members incl. control) from
  Open-Meteo's separate ensemble endpoint, stored as the per-hour mean, 16 days.
  Added 2026-07-25, after pre-registration, so it is an **exploratory secondary
  candidate**: the primary endpoint stays AIFS-deterministic vs Foreca.
- **obs** - last 166 h of station observations (idempotent upsert with
  OR REPLACE so FMI's later QC corrections win; outages up to ~6 days self-heal)

`score.py` reports MAE by lead day (hourly t2m and wind), rain occurrence skill
(POD/FAR/CSI at 0.1 mm/h), daily tmin/tmax/rain (derived over Europe/Helsinki
local days), the pairwise inference table described above, and a quantization
sensitivity board (all sources rounded to integers).

## Fairness / methodology notes (state these with any published claim)

- **Station vs city point**: Foreca's numbers are for their geocoded city point,
  not the FMI station point. Close in practice, but station-adjacent sources
  (FMI edited) have a small home advantage.
- **Quantization**: Foreca's public feeds are integer-quantized (whole degrees /
  whole m/s), which structurally inflates their MAE by roughly +0.05-0.1 °C
  independent of skill. The benchmark measures what their user actually
  receives; the sensitivity board (everyone rounded) shows whether any
  conclusion survives the objection.
- **foreca_daily is a different predictand**: their native daily extremes are
  true min/max; our obs-derived "extremes" are min/max of 24 hourly samples,
  which understate the real range. `foreca_daily` is therefore shown for
  curiosity but excluded from all claims; the like-for-like Foreca comparison
  uses their hourly feed.
- **Day-0 daily scores are excluded**: part of "today" has already happened at
  snapshot time, and several feeds include those elapsed hours as analysis.
- **Rain day convention**: hour-ending accumulations are assigned to the local
  date of (timestamp - 1h), so a "day" is a true calendar day. Foreca's hourly
  rain field convention has not been independently verified - treat Foreca rain
  boards as provisional.
- **Wind predictand**: obs wind is a 10-min mean; model wind is effectively
  instantaneous at the hour. Same for all sources, but disclose it.
- **Lead-day semantics** differ between experiments: retro uses
  issuance-relative previous_dayN; prospective buckets hours [24N, 24N+24)
  after the snapshot moment. Do not compare numbers across the two tables.

## Running

```bash
python3 retro.py [days]   # retrospective benchmark (default 90, max ~92)
python3 collect.py        # one prospective snapshot (~1 min; normally run by the
                          #   launchd agent; exits non-zero if any source failed)
python3 score.py          # score accrued snapshots (meaningful after ~2+ days;
                          #   significance gates open at 20 distinct days)
```

Stdlib only, no venv. Data in `data/bench.sqlite` (tables: `forecasts`,
`observations`, `retro_forecasts`, `collect_log`); results JSON alongside;
daily rotating DB backups in `data/backups/` (last 7 kept).

## Scheduled collection (launchd)

`./install.sh` installs `com.weatherbench.collect`, a launchd agent that runs
`collect.py` every 5 h (see the note below on why not 6) **independently of any
Claude Code session, terminal, or login shell**. launchd (unlike cron) runs a
missed interval as soon as the Mac wakes, so overnight sleep no longer costs
snapshots.

Because macOS blocks launchd-started jobs from reading `~/Documents`, the agent
runs from `~/Library/Application Support/weather-bench/` and the database lives
in its `data/` subdirectory. **This repo remains the source of truth for code** —
`install.sh` copies the `.py` files across, so re-run it after editing them. The
database is *moved* on first install and never overwritten afterwards.

`common.py` resolves `DATA_DIR` to the `~/Library` copy whenever it exists (or
to `$WEATHERBENCH_DATA` if set), so manual `score.py` / `retro.py` runs from the
repo and the scheduled collector share one database automatically.

```bash
./install.sh                                             # install or update
tail -f ~/Library/Logs/weather-bench.log                 # watch it run
launchctl print gui/$UID/com.weatherbench.collect        # status
launchctl kickstart -k gui/$UID/com.weatherbench.collect # collect right now
launchctl bootout gui/$UID/com.weatherbench.collect      # uninstall (keeps data)
```

Scoring is deliberately *not* scheduled — it is cheap, idempotent, and recomputed
from stored data on demand, so only collection is time-critical.

Verification conventions: forecast lead = target time minus snapshot time (fair:
every source is snapshotted simultaneously). Station-point verification slightly
favors high-resolution/post-processed sources (MET Nordic, FMI edited) over raw
0.25° global models - that is the "what users experience" comparison, kept
deliberately. GraphCast is absent: Open-Meteo's feed for it returns nulls (dead).

### Why the schedule is 5-hourly, not 6

The models issue new runs on a 6-hourly cycle (00/06/12/18 UTC). Sampling on
that same period phase-locks the benchmark to a fixed point in every model's
update cycle, so we would capture them at a *constant* staleness while Foreca
(which refreshes far more often) stays near-fresh — a systematic handicap that
more data cannot average out. A 5-hour interval does not divide 6, so successive
snapshots precess through all six phases of the cycle within ~30 h, sampling
fresh and stale runs evenly. Do not "tidy" this back to 6.
