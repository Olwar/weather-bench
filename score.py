"""Score accrued prospective forecasts (collect.py snapshots) against observations.

Boards:
  1. Hourly t2m MAE by lead day - all hourly sources incl. Foreca.
  2. Hourly wind speed MAE by lead day.
  3. Rain occurrence skill by lead day (threshold 0.1 mm/h): POD/FAR/CSI.
  4. Daily tmin/tmax/rain by lead day (foreca_daily = Foreca's native daily feed;
     it is a different predictand than hourly-derived extremes and is NOT part of
     any claim - see README).
  5. Pairwise inference (hourly t2m): matched pairs, circular moving-block
     bootstrap over consecutive local dates (block length 5, sensitivity at 3/7),
     empirical MAE difference as the point estimate, bootstrap CI + p-value.

     PRE-REGISTERED PRIMARY ENDPOINT (fixed 2026-07-23, before data accrual):
       hourly t2m, leads 1-7 pooled, ecmwf_aifs025_single vs foreca.
     All other pairwise cells are secondary and Holm-corrected. "Significant"
     requires >= MIN_DAYS distinct target dates.

Daily values for hourly sources & obs are min/max/sum over Europe/Helsinki local
days (>=23 forecast hours, >=20 obs hours). Hour-ending rain accumulations are
assigned to the local date of (timestamp - 1h) so a "day" is a true calendar day.

Usage: python3 score.py
"""
import json
import math
import random
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from common import get_db, DATA_DIR, CITIES
from blend import MEMBERS, AI_MEMBERS, OPEN_MEMBERS, OPEN_YR_MEMBERS, MIN_MEMBERS
from blend import VARS as BLEND_VARS

# Verification is only comparable within one country: same climate, same truth
# network, same competitor coverage. Every pre-existing board and the entire
# pre-registered inference family are pinned to the Finnish cities, so adding
# countries can never move a Finnish number. New countries get their own
# boards under "countries" in the JSON.
FI_CITIES = frozenset(c["key"] for c in CITIES if c["country"] == "fi")
COUNTRY_CITIES = {}
for c in CITIES:
    COUNTRY_CITIES.setdefault(c["country"], set()).add(c["key"])
# Cross-country pooled scopes for the site's scope toggle. Pooling mixes truth
# networks and competitor coverage, so these are display-only exploratory
# boards: no inference cells, and they must never feed the Finnish families.
REST_CITIES = frozenset(c["key"] for c in CITIES if c["country"] != "fi")
ALL_CITIES = FI_CITIES | REST_CITIES

HKI = ZoneInfo("Europe/Helsinki")
RAIN_THR = 0.1     # mm/h for occurrence skill
# 10000, not 2000: the bootstrap p-value cannot go below 1/N_BOOT, and with a
# Holm family of ~100 secondary cells the threshold alpha/m is ~5e-4 - i.e. the
# old floor of 5e-4 was itself the binding constraint, so a real effect could
# fail purely because the resampling could not express a smaller p. Cheap now
# that a draw costs O(#dates) rather than O(#samples).
N_BOOT = 10000
BLOCK_LEN = 5      # bootstrap block: consecutive target dates (synoptic persistence)
MIN_DAYS = 20      # min distinct target dates before "significant" may print
ALPHA = 0.05
# ecmwf_aifs_ens_mean was added 2026-07-25, AFTER pre-registration, so it is an
# exploratory secondary candidate only - the primary endpoint below stays fixed.
CANDIDATES = ["ecmwf_aifs025_single", "ecmwf_aifs_ens_mean", "best_match"]
COMPETITORS = ["foreca", "fmi_edited"]
PRIMARY = ("ecmwf_aifs025_single", "foreca", "pooled_1_7")
# Derived blends (blend.py). They are tested as their OWN Holm family, kept
# separate from the pre-registered one above: these were conceived after seeing
# the data, and folding them in would inflate m and retroactively weaken the
# pre-registered secondary cells. Their competitor set adds the best single
# model, because "beats Foreca" is a much weaker claim than "beats AIFS".
# Pairwise family kept lean: blend_learned and blend_ai still appear on every
# accuracy board, but they earn no CI cells - learned has been indistinguishable
# from the plain mean since day one, and every extra cell inflates the Holm
# family that the real claims must survive.
BLENDS = ["blend_open", "blend_open_yr", "blend_mean"]
BLEND_COMPETITORS = ["foreca", "fmi_edited", "ecmwf_aifs025_single", "google_weather", "yr"]
# The blends are derived HERE, in memory, from the member rows of each
# (city, run_time, target_time, var) group: the mean of whoever showed up,
# emitted only when >= MIN_MEMBERS members did. Member lists and the variable
# set are blend.py's, so the definition lives in one place. Until 2026-09-20
# blend.py wrote these as rows into `forecasts` every night (2.5 h), score.py
# read them back, and a wipe deleted them (57M rows/day of churn that
# fragmented the file and doubled the backups). blend.py remains as an ad-hoc
# tool for SQL against materialised blend rows; the nightly chain no longer
# runs it. wdir is never blended (circular) - see blend.VARS.
BLEND_DEFS = [
    ("blend_mean", MEMBERS),
    ("blend_ai", AI_MEMBERS),
    ("blend_open", OPEN_MEMBERS),
    ("blend_open_yr", OPEN_YR_MEMBERS),
]
BLEND_SOURCES = frozenset(name for name, _ in BLEND_DEFS)
PAIR_SOURCES = sorted({*CANDIDATES, *COMPETITORS, *BLENDS, *BLEND_COMPETITORS})
WINDOW_DAYS = 7    # run_time span held in memory at once, per city


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _local_date(s: str) -> str:
    return _utc(s).astimezone(HKI).strftime("%Y-%m-%d")


def _local_date_hour_ending(s: str) -> str:
    """Rain values are accumulations over the PRECEDING hour - assign to the
    local date the accumulation actually fell on."""
    return (_utc(s) - timedelta(hours=1)).astimezone(HKI).strftime("%Y-%m-%d")


def _stat(cells, key, err):
    s = cells.setdefault(key, {"n": 0, "sae": 0.0, "se": 0.0})
    s["n"] += 1
    s["sae"] += abs(err)
    s["se"] += err


def _finish(cells):
    return {
        k: {"n": s["n"], "mae": round(s["sae"] / s["n"], 3), "bias": round(s["se"] / s["n"], 3)}
        for k, s in cells.items()
    }


def _obs_city(con, city) -> dict:
    """var -> {time: value} for one city (primary-key prefix read)."""
    out: dict = defaultdict(dict)
    for var, t, v in con.execute("SELECT var, time, value FROM observations WHERE city=?", (city,)):
        out[var][t] = v
    return out


def _city_scopes(scopes):
    """city -> names of the scopes that contain it. Scopes overlap (all ⊃ fi),
    so a row is folded into every scope it belongs to in the same pass."""
    m = defaultdict(list)
    for name, cits in scopes.items():
        for c in cits:
            m[c].append(name)
    return m


def _lead_day(run_time, target_time):
    lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
    return None if lead_h < 0 else int(lead_h // 24)


# Walks the primary key one distinct value at a time. A plain
# SELECT DISTINCT source scans the whole 25M+ row index (minutes on the
# volume); this returns in about a second.
_DISTINCT_SOURCES = """WITH RECURSIVE s(x) AS (
  SELECT min(source) FROM forecasts
  UNION ALL SELECT (SELECT min(source) FROM forecasts WHERE source > x) FROM s WHERE x IS NOT NULL)
SELECT x FROM s WHERE x IS NOT NULL"""


def _sources(con) -> list:
    return [r[0] for r in con.execute(_DISTINCT_SOURCES)]


def _by_source(cells) -> dict:
    out: dict = defaultdict(dict)
    for (source, lead_d), s in cells.items():
        out[source][str(lead_d)] = s
    return dict(out)


# Every board below is a handler fed from ONE walk over the forecasts table
# (walk_city, called per city). The table is 90M+ rows on a slow volume whose
# pages are scattered by months of 5-hourly inserts, so a full scan is ~45 min
# of random 4 KB reads; one scan per board (11 of them) was ~8 h of the
# nightly run. Each handler takes scopes={name: set(cities)} and returns
# {name: board} from finish(). Scopes overlap (all ⊃ fi) - see _city_scopes.
#
# feed(source, city, target_time, lead_d, value, truth, obs_city) is called
# only for rows with an observation and a non-negative lead day.

class _MAE:
    """Hourly MAE/bias by (source, lead day). quantized names the scopes whose
    forecasts are rounded to whole units first - the sensitivity check for
    the 'Foreca only publishes integers' fairness objection."""

    def __init__(self, scopes: dict, quantized=frozenset()):
        self.city_scopes = _city_scopes(scopes)
        self.quantized = quantized
        self.cells = {name: {} for name in scopes}

    def feed(self, source, city, target_time, lead_d, value, truth, obs_city):
        names = self.city_scopes.get(city)
        if not names:
            return
        err = value - truth
        err_q = float(round(value)) - truth if self.quantized else err
        for name in names:
            _stat(self.cells[name], (source, lead_d), err_q if name in self.quantized else err)

    def finish(self) -> dict:
        return {name: _by_source(_finish(c)) for name, c in self.cells.items()}


class _WindDir:
    """Circular error, counted only when the observed wind is >= 2 m/s -
    direction is meteorologically meaningless in near-calm, and including calm
    hours would reward sources that merely guess the climatological direction."""

    def __init__(self, scopes: dict):
        self.city_scopes = _city_scopes(scopes)
        self.cells = {name: {} for name in scopes}

    def feed(self, source, city, target_time, lead_d, value, truth, obs_city):
        names = self.city_scopes.get(city)
        if not names:
            return
        ws = obs_city["ws"].get(target_time)
        if ws is None or ws < 2.0:
            return
        d = abs(value - truth) % 360.0
        for name in names:
            _stat(self.cells[name], (source, lead_d), min(d, 360.0 - d))

    def finish(self) -> dict:
        return {name: _by_source(_finish(c)) for name, c in self.cells.items()}


class _CloudClass:
    """3-class hit rate: clear (<=2 octas, i.e. <=25%), overcast (>=7 octas,
    >=87.5%), else partly. A ceilometer's octas and a model's grid-cell cloud
    fraction are cousins rather than twins, so the class view is the fairer
    headline than raw percent MAE (which is also reported)."""

    @staticmethod
    def cls(v):
        return 0 if v <= 25.0 else (2 if v >= 87.5 else 1)

    def __init__(self, scopes: dict):
        self.city_scopes = _city_scopes(scopes)
        self.cells = {name: defaultdict(lambda: {"hit": 0, "n": 0}) for name in scopes}

    def feed(self, source, city, target_time, lead_d, value, truth, obs_city):
        names = self.city_scopes.get(city)
        if not names:
            return
        hit = 1 if self.cls(value) == self.cls(truth) else 0
        for name in names:
            c = self.cells[name][(source, lead_d)]
            c["n"] += 1
            c["hit"] += hit

    def finish(self) -> dict:
        out = {}
        for name, cs in self.cells.items():
            board: dict = defaultdict(dict)
            for (source, lead_d), c in cs.items():
                if c["n"] >= 100:
                    board[source][str(lead_d)] = {"n": c["n"], "acc": round(c["hit"] / c["n"], 3)}
            out[name] = dict(board)
        return out


class _RainOccurrence:
    def __init__(self, scopes: dict):
        self.city_scopes = _city_scopes(scopes)
        self.cells = {name: defaultdict(lambda: {"hit": 0, "miss": 0, "fa": 0, "cn": 0}) for name in scopes}

    def feed(self, source, city, target_time, lead_d, value, truth, obs_city):
        names = self.city_scopes.get(city)
        if not names:
            return
        fc_rain, ob_rain = value >= RAIN_THR, truth >= RAIN_THR
        slot = "hit" if fc_rain and ob_rain else "miss" if ob_rain else "fa" if fc_rain else "cn"
        for name in names:
            self.cells[name][(source, lead_d)][slot] += 1

    def finish(self) -> dict:
        out = {}
        for name, cs in self.cells.items():
            board: dict = defaultdict(dict)
            for (source, lead_d), c in cs.items():
                hits, miss, fa = c["hit"], c["miss"], c["fa"]
                n = hits + miss + fa + c["cn"]
                board[source][str(lead_d)] = {
                    "n": n,
                    "pod": round(hits / (hits + miss), 3) if hits + miss else None,
                    "far": round(fa / (hits + fa), 3) if hits + fa else None,
                    "csi": round(hits / (hits + miss + fa), 3) if hits + miss + fa else None,
                }
            out[name] = dict(board)
        return out


class _Daily:
    """Daily tmin/tmax/rain by lead day, Finland only. Hourly sources are
    reduced to local-day extremes/sums with running min/max/sum per
    (source, run_time, date) and scored as soon as a city's window is
    complete, so nothing per-sample is retained: the old whole-table version
    kept every hourly value of every snapshot in memory (~1 GB and growing
    with each collection run), which is what pushed the nightly service past
    its 2 GB MemoryMax on 2026-09-17.

    foreca_daily is Foreca's native daily feed (vars tmin/tmax/rain keyed by
    date): true daily extremes, a DIFFERENT predictand than hourly-sampled
    extremes - scored for curiosity, excluded from claims."""

    def __init__(self, con):
        self.obs_daily = _obs_daily(con)
        self.cells: dict = {}
        self._reset()

    def _reset(self):
        self.hr_t: dict = {}     # (source, run_time, date) -> [n, min, max]
        self.hr_r: dict = {}     # (source, run_time, date) -> [n, sum]
        self.native: dict = {}   # (run_time, date) -> {var: value}

    def feed(self, source, run_time, target_time, var, value):
        """Every FI row whose target is not before its run (no hindsight)."""
        if var == "t2m":
            k = (source, run_time, _local_date(target_time))
            a = self.hr_t.get(k)
            if a is None:
                self.hr_t[k] = [1, value, value]
            else:
                a[0] += 1
                if value < a[1]:
                    a[1] = value
                if value > a[2]:
                    a[2] = value
        elif var == "rain1h":
            k = (source, run_time, _local_date_hour_ending(target_time))
            a = self.hr_r.get(k)
            if a is None:
                self.hr_r[k] = [1, value]
            else:
                a[0] += 1
                a[1] += value
        elif source == "foreca" and var in ("tmin", "tmax", "rain"):
            self.native.setdefault((run_time, target_time), {})[var] = value

    def flush(self, city):
        """Score the accumulated groups of `city` (a complete run_time window)."""
        fc: dict = {}
        for (run_time, date), vals in self.native.items():
            fc[("foreca_daily", run_time, date)] = vals
        for (source, run_time, date), (n, lo, hi) in self.hr_t.items():
            if n >= 23:
                fc[(source, run_time, date)] = {"tmin": lo, "tmax": hi}
        for (source, run_time, date), (n, total) in self.hr_r.items():
            k = (source, run_time, date)
            if n >= 23 and k in fc:
                fc[k]["rain"] = total
        for (source, run_time, date), vals in fc.items():
            truth = self.obs_daily.get((city, date))
            if truth is None:
                continue
            run_date = _utc(run_time).astimezone(HKI).strftime("%Y-%m-%d")
            lead_d = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(run_date, "%Y-%m-%d")).days
            # d0 excluded: part of the day has already happened at snapshot time.
            if lead_d < 1:
                continue
            for var in ("tmin", "tmax", "rain"):
                if var in vals and var in truth:
                    _stat(self.cells, (source, var, lead_d), vals[var] - truth[var])
        self._reset()

    def finish(self) -> dict:
        out: dict = defaultdict(dict)
        for (source, var, lead_d), s in _finish(self.cells).items():
            out[source][f"{var}_d{lead_d}"] = s
        return dict(out)


def _obs_daily(con) -> dict:
    t_by_day: dict = defaultdict(list)
    r_by_day: dict = defaultdict(list)
    for c, t, var, v in con.execute("SELECT city, time, var, value FROM observations WHERE var IN ('t2m','rain1h')"):
        if c not in FI_CITIES:
            continue
        if var == "t2m":
            t_by_day[(c, _local_date(t))].append(v)
        else:
            r_by_day[(c, _local_date_hour_ending(t))].append(v)
    obs_daily: dict = {}
    for k, vs in t_by_day.items():
        if len(vs) >= 20:
            obs_daily[k] = {"tmin": min(vs), "tmax": max(vs)}
    for k, vs in r_by_day.items():
        if len(vs) >= 20 and k in obs_daily:
            obs_daily[k]["rain"] = sum(vs)
    return obs_daily


def run_windows(con, days: int = WINDOW_DAYS) -> list:
    """Half-open [lo, hi) run_time ranges that together cover EVERY run: the
    first starts at '' and the last ends at '~', so rows outside collect_log's
    span (early runs) can never be skipped. ISO strings compare in time order."""
    lo, hi = con.execute("SELECT min(run_time), max(run_time) FROM collect_log").fetchone()
    if lo is None:
        return [("", "~")]
    edges = []
    t, end = _utc(lo), _utc(hi)
    while t <= end:
        edges.append(t.strftime("%Y-%m-%dT%H:%MZ"))
        t += timedelta(days=days)
    bounds = ["", *edges[1:], "~"]
    return list(zip(bounds, bounds[1:]))


def walk_city(con, city, sources, windows, handlers, daily, pair_files):
    """Read one city's rows via the primary key, one run_time window at a
    time, derive the blends from each (run_time, target_time, var) group, and
    feed every real and derived row to the boards. Rows reach each consumer
    in (run_time, target_time, var) order, which for the pairwise files is
    exactly the old index order (source, city, run_time, target_time)."""
    obs_city = _obs_city(con, city)
    intern = sys.intern
    is_fi = city in FI_CITIES
    q = ("SELECT run_time, target_time, var, value FROM forecasts"
         " WHERE source=? AND city=? AND run_time>=? AND run_time<?")
    for lo, hi in windows:
        groups: dict = {}
        for source in sources:
            for run_time, target_time, var, value in con.execute(q, (source, city, lo, hi)):
                k = (intern(run_time), intern(target_time), intern(var))
                g = groups.get(k)
                if g is None:
                    g = groups[k] = {}
                g[source] = value
        lead_cache: dict = {}
        for k in sorted(groups):
            run_time, target_time, var = k
            g = groups[k]
            if var in BLEND_VARS:
                for name, members in BLEND_DEFS:
                    vals = [g[m] for m in members if m in g]
                    if len(vals) >= MIN_MEMBERS:
                        g[name] = sum(vals) / len(vals)
            obs_var = obs_city.get(var)
            truth = obs_var.get(target_time) if obs_var else None
            hs = handlers.get(var)
            if truth is not None and hs:
                lead_d = lead_cache.get((run_time, target_time))
                if lead_d is None:
                    lead_d = lead_cache[(run_time, target_time)] = _lead_day(run_time, target_time)
                if lead_d is not None:
                    for source, value in g.items():
                        for h in hs:
                            h.feed(source, city, target_time, lead_d, value, truth, obs_city)
                    if var == "t2m":
                        for source in PAIR_SOURCES:
                            if source in g and is_fi:
                                pair_files[source].write(
                                    f"{city}\t{run_time}\t{target_time}\t{g[source] - truth!r}\n")
            if is_fi:
                if var in ("t2m", "rain1h"):
                    # Feeds include already-elapsed hours of the snapshot day
                    # (analysis, not forecast) - grading them would be hindsight.
                    if _utc(target_time) >= _utc(run_time):
                        for source, value in g.items():
                            daily.feed(source, run_time, target_time, var, value)
                elif var in ("tmin", "tmax", "rain") and "foreca" in g:
                    daily.feed("foreca", run_time, target_time, var, g["foreca"])
        if is_fi:
            daily.flush(city)


def _block_bootstrap(agg_by_date: dict, block_len: int, n_boot: int = N_BOOT):
    """Circular moving-block bootstrap over consecutive local target dates.
    agg_by_date: date -> per-date subtotals (sum|e_cand|, sum|e_comp|, n, ...).

    Returns (ci_lo, ci_hi, p_two_sided), or (None, None, None) when the
    resampling is DEGENERATE - with fewer distinct dates than the block length
    every draw contains every date, so all draws are identical, the interval
    collapses to a point and p collapses to the 1/n_boot floor. That looks like
    overwhelming evidence when it actually means "not enough data to resample",
    so it must be reported as absent rather than as a number.
    """
    dates = sorted(agg_by_date)
    nd = len(dates)
    # Structural degeneracy: when the block is at least as long as the record,
    # one circular block already covers every date, so every draw is the same
    # multiset and only the summation ORDER varies. (Do not try to detect this
    # by comparing draw values - floating-point addition is not associative, so
    # the draws differ in the last bits and an equality test silently passes.)
    if nd <= block_len:
        return None, None, None
    rng = random.Random(42)
    n_blocks = max(1, math.ceil(nd / block_len))
    # A date always enters a draw whole, and the statistic is
    # (sum|e_cand| - sum|e_comp|) / n over the picked dates - so a date's
    # per-date subtotals are sufficient, and pairwise() only ever keeps those.
    # A draw is O(#dates) instead of O(#samples); at 200k+ pooled samples that
    # is the difference between hours and seconds, with the same statistic.
    agg = {d: (a[0], a[1], a[2]) for d, a in agg_by_date.items()}
    diffs = []
    for _ in range(n_boot):
        picked = []
        for _ in range(n_blocks):
            start = rng.randrange(nd)
            picked.extend(dates[(start + i) % nd] for i in range(block_len))
        picked = picked[:nd]
        sum_a = sum_b = 0.0
        n = 0
        for d in picked:
            sa, sb, cnt = agg[d]
            sum_a += sa
            sum_b += sb
            n += cnt
        if n:
            diffs.append((sum_a - sum_b) / n)
    diffs.sort()
    if not diffs or diffs[0] == diffs[-1]:
        return None, None, None  # degenerate: zero variance across draws
    last = len(diffs) - 1
    lo = diffs[min(last, max(0, int(0.025 * len(diffs))))]
    hi = diffs[min(last, int(0.975 * len(diffs)))]
    frac_pos = sum(1 for d in diffs if d > 0) / len(diffs)
    p = max(2 * min(frac_pos, 1 - frac_pos), 1.0 / len(diffs))
    return lo, hi, p


def _summarize_pairs(by_date: dict) -> dict:
    """by_date: date -> [sum|e_cand|, sum|e_comp|, n, wins, ties]."""
    n = sum(a[2] for a in by_date.values())
    mae_a = sum(a[0] for a in by_date.values()) / n
    mae_b = sum(a[1] for a in by_date.values()) / n
    wins = sum(a[3] for a in by_date.values())
    ties = sum(a[4] for a in by_date.values())
    lo, hi, p = _block_bootstrap(by_date, BLOCK_LEN)
    degenerate = p is None
    sens = {}
    for bl in (3, 7):
        s_lo, s_hi, _ = _block_bootstrap(by_date, bl)
        sens[str(bl)] = None if s_lo is None else [round(s_lo, 3), round(s_hi, 3)]
    return {
        "n": n,
        "days": len(by_date),
        "mae_cand": round(mae_a, 3),
        "mae_comp": round(mae_b, 3),
        "diff": round(mae_a - mae_b, 3),   # empirical point estimate
        # ci95/p are null while the bootstrap is degenerate (too few distinct
        # dates to resample); reporting the collapsed values would read as
        # spurious near-certainty.
        "ci95": None if degenerate else [round(lo, 3), round(hi, 3)],
        "ci95_block_sensitivity": sens,
        "p": None if degenerate else round(p, 4),
        "degenerate_bootstrap": degenerate,
        "win_rate": round((wins + 0.5 * ties) / n, 3),  # ties count half
        "tie_rate": round(ties / n, 3),
        "enough_days": len(by_date) >= MIN_DAYS,
    }


def _pair(agg: dict, date: str, ea: float, eb: float):
    a = agg.get(date)
    if a is None:
        a = agg[date] = [0.0, 0.0, 0, 0, 0]
    aa, ab = abs(ea), abs(eb)
    a[0] += aa
    a[1] += ab
    a[2] += 1
    if aa < ab:
        a[3] += 1
    elif aa == ab:
        a[4] += 1


def _read_errors(path):
    with open(path) as f:
        for line in f:
            city, run_time, target_time, err = line.rstrip("\n").split("\t")
            yield city, run_time, target_time, float(err)


def pairwise(files: dict, cand: str, comp: str) -> dict:
    """Matched samples: same city, same snapshot, same target hour.

    Memory is one float per candidate sample (keys interned: a few hundred
    distinct run/target strings, not one copy per row) plus per-date subtotals;
    the pairs themselves are never materialised. The previous version held a
    dict of dicts for every sample of both sources plus every pair as a tuple,
    ~0.5 GB per call, and grew with every collection run."""
    intern = sys.intern
    cand_err: dict = {}
    for city, run_time, target_time, err in _read_errors(files[cand]):
        cand_err[(intern(city), intern(run_time), intern(target_time))] = err
    by_lead: dict = defaultdict(dict)   # lead_d -> date -> subtotals
    pooled: dict = {}
    for city, run_time, target_time, eb in _read_errors(files[comp]):
        ea = cand_err.get((city, run_time, target_time))
        if ea is None:
            continue
        lead_d = int((_utc(target_time) - _utc(run_time)).total_seconds() / 3600 // 24)
        date = _local_date(target_time)
        _pair(by_lead[lead_d], date, ea, eb)
        if 1 <= lead_d <= 7:
            _pair(pooled, date, ea, eb)
    out = {}
    for lead_d, by_date in sorted(by_lead.items()):
        if sum(a[2] for a in by_date.values()) >= 10 and len(by_date) >= 3:
            out[str(lead_d)] = _summarize_pairs(by_date)
    if pooled and sum(a[2] for a in pooled.values()) >= 10 and len(pooled) >= 3:
        out["pooled_1_7"] = _summarize_pairs(pooled)
    return out


def apply_significance(pairs: dict):
    """Primary endpoint tested at ALPHA; all other cells Holm-corrected."""
    tests = []
    for (cand, comp), leads in pairs.items():
        for lead, s in leads.items():
            s["primary"] = (cand, comp, lead) == PRIMARY
            if not s["enough_days"] or s["p"] is None:
                s["significant"] = None  # insufficient data for any inference
            elif s["primary"]:
                s["significant"] = s["p"] <= ALPHA
            else:
                tests.append(s)
    tests.sort(key=lambda s: s["p"])
    m = len(tests)
    still_ok = True
    for i, s in enumerate(tests):
        if still_ok and s["p"] <= ALPHA / (m - i):
            s["significant"] = True
        else:
            still_ok = False
            s["significant"] = False


def print_board(title, results, unit="degC MAE", higher_better=False):
    if not results:
        print(f"\n{title}: no scorable data yet")
        return
    keys = sorted({k for m in results.values() for k in m}, key=lambda k: (k.split("_d")[0], int(k.split("_d")[-1])) if "_d" in k else int(k))
    first = keys[0]
    sample = next(iter(results.values())).get(first, {})
    metric = next((k for k in ("mae", "csi", "acc") if k in sample), "mae")
    worst = -9e9 if higher_better else 9e9
    models = sorted(results, key=lambda m: results[m].get(first, {}).get(metric) or worst, reverse=higher_better)
    print(f"\n{title}  ({unit}; n in parens)")
    print(f"{'':>14}" + "".join(f"{k:>15}" for k in keys))
    for m in models:
        row = f"{m[:14]:>14}"
        for k in keys:
            s = results[m].get(k)
            if not s or s.get(metric) is None:
                row += f"{'-':>15}"
            else:
                row += f"  {s[metric]:>5.2f} ({s['n']:>5})"
        print(row)


def print_pairwise(all_pairs: dict, title: str = "Pairwise inference", note: str = ""):
    print(f"\n=== {title} (hourly t2m, matched pairs, block bootstrap) ===")
    print(f"negative diff = candidate better; PRIMARY = pre-registered endpoint;")
    print(f"sig requires >={MIN_DAYS} distinct days; secondary cells Holm-corrected")
    if note:
        print(note)
    for (cand, comp), leads in all_pairs.items():
        print(f"\n{cand} vs {comp}:")
        if not leads:
            print("  not enough matched data yet")
            continue
        print(f"{'lead':>10}{'n':>7}{'days':>6}{'cand':>7}{'comp':>7}{'diff':>7}{'CI95':>18}{'p':>8}{'win%':>6}  sig")
        for lead, s in leads.items():
            # too few distinct dates to resample -> no interval, no p-value
            ci = "-" if s["ci95"] is None else f"[{s['ci95'][0]:+.2f},{s['ci95'][1]:+.2f}]"
            pv = "-" if s["p"] is None else f"{s['p']:.4f}"
            sig = {True: "YES", False: "no", None: "n/a"}[s["significant"]]
            tag = " *PRIMARY*" if s["primary"] else ""
            print(f"{lead:>10}{s['n']:>7}{s['days']:>6}{s['mae_cand']:>7.2f}{s['mae_comp']:>7.2f}"
                  f"{s['diff']:>+7.2f}{ci:>18}{pv:>8}{100*s['win_rate']:>6.0f}  {sig}{tag}")


def main():
    con = get_db()
    n_runs = con.execute("SELECT count(DISTINCT run_time) FROM collect_log").fetchone()[0]
    print(f"Scoring prospective data: {n_runs} collection runs in DB")

    # Scope map shared by every board pass. "fi" feeds the legacy top-level
    # boards; the rest are exploratory. Overlap is fine - see _city_scopes.
    scopes = {"fi": FI_CITIES, "rest": REST_CITIES, "all": ALL_CITIES,
              **{cc: cits for cc, cits in COUNTRY_CITIES.items() if cc != "fi"}}
    foreign = sorted(k for k in COUNTRY_CITIES if k != "fi")

    fi = {"fi": FI_CITIES}
    t2m_h = _MAE({**scopes, "fi_q": FI_CITIES}, quantized={"fi_q"})
    ws_h, rain_amt_h, cc_h = _MAE(scopes), _MAE(scopes), _MAE(scopes)
    # Extended exploratory boards (collection began 2026-08-22; they stay empty
    # until forecast/observation overlap accrues, and _MAE copes).
    ext_h = {v: _MAE(fi) for v in ("rh", "td", "gust", "pmsl")}
    wdir_h = _WindDir(fi)
    cls_h = _CloudClass(fi)
    occ_h = _RainOccurrence(scopes)
    handlers = {
        "t2m": [t2m_h], "ws": [ws_h],
        # Rain amount in mm/h. MAE over all hours is dominated by dry hours, so
        # this rewards not-crying-wolf as much as nailing the downpour - fair,
        # but a different question than occurrence CSI, hence a separate board.
        "rain1h": [rain_amt_h, occ_h],
        "cc": [cc_h, cls_h], "wdir": [wdir_h],
        **{v: [h] for v, h in ext_h.items()},
    }
    daily_h = _Daily(con)
    # Real sources only: any blend_* rows a manual blend.py left behind are
    # ignored, the blends are always derived fresh from the members.
    sources = [src for src in _sources(con) if src not in BLEND_SOURCES and not src.startswith("blend_")]
    windows = run_windows(con)
    # Pairwise inference needs matched per-sample errors; they are spooled to
    # one TSV per source (~60 MB, /tmp) during the walk and re-read per cell,
    # so the 21 cells below never touch the table. repr() round-trips floats.
    tmpdir = tempfile.mkdtemp(prefix="wb-pairwise-")
    try:
        files = {src: Path(tmpdir) / f"{src}.tsv" for src in PAIR_SOURCES}
        pair_files = {src: open(path, "w") for src, path in files.items()}
        try:
            for city in sorted(ALL_CITIES):
                walk_city(con, city, sources, windows, handlers, daily_h, pair_files)
        finally:
            for f in pair_files.values():
                f.close()
        pairs = {
            (cand, comp): pairwise(files, cand, comp)
            for cand in CANDIDATES for comp in COMPETITORS
        }
        blend_pairs = {
            (cand, comp): pairwise(files, cand, comp)
            for cand in BLENDS for comp in BLEND_COMPETITORS
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    apply_significance(pairs)
    # Corrected within itself - a separate family, never merged with the above.
    apply_significance(blend_pairs)

    t2m_all = t2m_h.finish()
    t2m, t2m_q = t2m_all["fi"], t2m_all["fi_q"]
    ws_all = ws_h.finish()
    ws = ws_all["fi"]
    rain_occ_all = occ_h.finish()
    rain_occ = rain_occ_all["fi"]
    rain_amt_all = rain_amt_h.finish()
    rain_amt = rain_amt_all["fi"]
    cc_all = cc_h.finish()
    extended = {v: h.finish()["fi"] for v, h in ext_h.items()}
    extended["cc"] = cc_all["fi"]
    wdir = wdir_h.finish()["fi"]
    cloud_cls = cls_h.finish()["fi"]
    daily = daily_h.finish()
    countries = {
        cc: {
            "hourly_t2m": t2m_all[cc],
            "hourly_ws": ws_all[cc],
            "hourly_cc": cc_all[cc],
            "rain_occurrence": rain_occ_all[cc],
        }
        for cc in foreign
    }
    scope_boards = {
        name: {
            "hourly_t2m": t2m_all[name],
            "hourly_ws": ws_all[name],
            "rain_occurrence": rain_occ_all[name],
            "hourly_rain_amount": rain_amt_all[name],
        }
        for name in ("rest", "all")
    }
    print_board("Hourly t2m by lead day", t2m)
    print_board("Hourly t2m, ALL sources rounded to integers (quantization sensitivity)", t2m_q)
    print_board("Hourly wind speed by lead day", ws, unit="m/s MAE")
    for v, unit in (("rh", "% MAE"), ("td", "degC MAE"), ("gust", "m/s MAE"),
                    ("cc", "% cloud MAE"), ("pmsl", "hPa MAE")):
        print_board(f"Hourly {v} by lead day", extended[v], unit=unit)
    print_board("Wind direction (obs wind >= 2 m/s)", wdir, unit="deg MAE")
    print_board("Cloud class hit rate (clear/partly/overcast)", cloud_cls,
                unit="accuracy, higher better", higher_better=True)
    for cc in sorted(k for k in COUNTRY_CITIES if k != "fi"):
        print_board(f"[{cc}] hourly t2m", countries[cc]["hourly_t2m"])
    print_board("Rain occurrence (>=0.1mm/h) by lead day", rain_occ, unit="CSI, higher better", higher_better=True)
    print_board("Rain amount by lead day", rain_amt, unit="mm/h MAE")
    for name in ("rest", "all"):
        print_board(f"[{name}] hourly t2m", scope_boards[name]["hourly_t2m"])
    print_board("Daily tmin/tmax/rain by lead day", daily, unit="degC / mm MAE")
    print_pairwise(pairs, "Pairwise inference: pre-registered family")
    if any(blend_pairs.values()):
        print_pairwise(blend_pairs, "Pairwise inference: derived blends (EXPLORATORY)",
                       "conceived after seeing the data - Holm-corrected as a separate family")

    (DATA_DIR / "prospective_results.json").write_text(json.dumps({
        "hourly_t2m": t2m, "hourly_t2m_quantized": t2m_q, "hourly_ws": ws,
        "rain_occurrence": rain_occ, "hourly_rain_amount": rain_amt, "daily": daily,
        "scopes": scope_boards,
        **{f"hourly_{v}": extended[v] for v in extended},
        "wind_direction": wdir, "cloud_classes": cloud_cls,
        "countries": countries,
        "pairwise_t2m": {f"{a}__vs__{b}": v for (a, b), v in pairs.items()},
        "pairwise_t2m_blends_exploratory": {
            f"{a}__vs__{b}": v for (a, b), v in blend_pairs.items()},
        "config": {"block_len": BLOCK_LEN, "min_days": MIN_DAYS, "alpha": ALPHA,
                   "primary": list(PRIMARY), "n_boot": N_BOOT},
    }, indent=2))
    print(f"\nSaved {DATA_DIR / 'prospective_results.json'}")


if __name__ == "__main__":
    main()
