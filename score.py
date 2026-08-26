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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import get_db, DATA_DIR

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


def _load_obs(con, var):
    return {
        (c, t): v
        for c, t, v in con.execute("SELECT city, time, value FROM observations WHERE var=?", (var,))
    }


def hourly_board(con, var: str, quantize: bool = False) -> dict:
    """quantize=True rounds every forecast to whole units first - the sensitivity
    check for the 'Foreca only publishes integers' fairness objection."""
    obs = _load_obs(con, var)
    cells: dict = {}
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var=?"
    for source, city, run_time, target_time, value in con.execute(q, (var,)):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        if quantize:
            value = float(round(value))
        _stat(cells, (source, int(lead_h // 24)), value - truth)
    out: dict = defaultdict(dict)
    for (source, lead_d), s in _finish(cells).items():
        out[source][str(lead_d)] = s
    return dict(out)


def wind_direction_board(con) -> dict:
    """Circular error, counted only when the observed wind is >= 2 m/s -
    direction is meteorologically meaningless in near-calm, and including calm
    hours would reward sources that merely guess the climatological direction."""
    obs_dir = _load_obs(con, "wdir")
    obs_ws = _load_obs(con, "ws")
    cells: dict = {}
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='wdir'"
    for source, city, run_time, target_time, value in con.execute(q):
        truth = obs_dir.get((city, target_time))
        ws = obs_ws.get((city, target_time))
        if truth is None or ws is None or ws < 2.0:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        d = abs(value - truth) % 360.0
        _stat(cells, (source, int(lead_h // 24)), min(d, 360.0 - d))
    out: dict = defaultdict(dict)
    for (source, lead_d), st in _finish(cells).items():
        out[source][str(lead_d)] = st
    return dict(out)


def cloud_class_board(con) -> dict:
    """3-class hit rate: clear (<=2 octas, i.e. <=25%), overcast (>=7 octas,
    >=87.5%), else partly. A ceilometer's octas and a model's grid-cell cloud
    fraction are cousins rather than twins, so the class view is the fairer
    headline than raw percent MAE (which is also reported)."""
    obs = _load_obs(con, "cc")
    cls = lambda v: 0 if v <= 25.0 else (2 if v >= 87.5 else 1)
    cells: dict = defaultdict(lambda: {"hit": 0, "n": 0})
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='cc'"
    for source, city, run_time, target_time, value in con.execute(q):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        c = cells[(source, int(lead_h // 24))]
        c["n"] += 1
        c["hit"] += 1 if cls(value) == cls(truth) else 0
    out: dict = defaultdict(dict)
    for (source, lead_d), c in cells.items():
        if c["n"] >= 100:
            out[source][str(lead_d)] = {"n": c["n"], "acc": round(c["hit"] / c["n"], 3)}
    return dict(out)


def rain_occurrence_board(con) -> dict:
    obs = _load_obs(con, "rain1h")
    cells: dict = defaultdict(lambda: {"hit": 0, "miss": 0, "fa": 0, "cn": 0})
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var='rain1h'"
    for source, city, run_time, target_time, value in con.execute(q):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        lead_h = (_utc(target_time) - _utc(run_time)).total_seconds() / 3600
        if lead_h < 0:
            continue
        key = (source, int(lead_h // 24))
        fc_rain, ob_rain = value >= RAIN_THR, truth >= RAIN_THR
        if fc_rain and ob_rain:
            cells[key]["hit"] += 1
        elif not fc_rain and ob_rain:
            cells[key]["miss"] += 1
        elif fc_rain and not ob_rain:
            cells[key]["fa"] += 1
        else:
            cells[key]["cn"] += 1
    out: dict = defaultdict(dict)
    for (source, lead_d), c in cells.items():
        hits, miss, fa = c["hit"], c["miss"], c["fa"]
        n = hits + miss + fa + c["cn"]
        out[source][str(lead_d)] = {
            "n": n,
            "pod": round(hits / (hits + miss), 3) if hits + miss else None,
            "far": round(fa / (hits + fa), 3) if hits + fa else None,
            "csi": round(hits / (hits + miss + fa), 3) if hits + miss + fa else None,
        }
    return dict(out)


def daily_series(con):
    t_by_day: dict = defaultdict(list)
    r_by_day: dict = defaultdict(list)
    for c, t, var, v in con.execute("SELECT city, time, var, value FROM observations WHERE var IN ('t2m','rain1h')"):
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

    fc_daily: dict = {}
    # Foreca's native daily feed: true daily extremes, a DIFFERENT predictand than
    # hourly-sampled extremes - scored for curiosity, excluded from claims.
    q = "SELECT city, run_time, target_time, var, value FROM forecasts WHERE source='foreca' AND var IN ('tmin','tmax','rain')"
    for city, run_time, date, var, value in con.execute(q):
        fc_daily.setdefault(("foreca_daily", city, run_time, date), {})[var] = value
    hr_t: dict = defaultdict(list)
    hr_r: dict = defaultdict(list)
    q = "SELECT source, city, run_time, target_time, var, value FROM forecasts WHERE var IN ('t2m','rain1h')"
    for source, city, run_time, target_time, var, value in con.execute(q):
        # Feeds include already-elapsed hours of the snapshot day (analysis, not
        # forecast) - grading on them would be hindsight. Mirror hourly_board's filter.
        if _utc(target_time) < _utc(run_time):
            continue
        if var == "t2m":
            hr_t[(source, city, run_time, _local_date(target_time))].append(value)
        else:
            hr_r[(source, city, run_time, _local_date_hour_ending(target_time))].append(value)
    for k, vs in hr_t.items():
        if len(vs) >= 23:
            fc_daily[k] = {"tmin": min(vs), "tmax": max(vs)}
    for k, vs in hr_r.items():
        if len(vs) >= 23 and k in fc_daily:
            fc_daily[k]["rain"] = sum(vs)
    return obs_daily, fc_daily


def daily_board(con, obs_daily, fc_daily) -> dict:
    cells: dict = {}
    for (source, city, run_time, date), fc in fc_daily.items():
        truth = obs_daily.get((city, date))
        if truth is None:
            continue
        run_date = _utc(run_time).astimezone(HKI).strftime("%Y-%m-%d")
        lead_d = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(run_date, "%Y-%m-%d")).days
        # d0 excluded: part of the day has already happened at snapshot time.
        if lead_d < 1:
            continue
        for var in ("tmin", "tmax", "rain"):
            if var in fc and var in truth:
                _stat(cells, (source, var, lead_d), fc[var] - truth[var])
    out: dict = defaultdict(dict)
    for (source, var, lead_d), s in _finish(cells).items():
        out[source][f"{var}_d{lead_d}"] = s
    return dict(out)


def _block_bootstrap(samples_by_date: dict, block_len: int, n_boot: int = N_BOOT):
    """Circular moving-block bootstrap over consecutive local target dates.

    Returns (ci_lo, ci_hi, p_two_sided), or (None, None, None) when the
    resampling is DEGENERATE - with fewer distinct dates than the block length
    every draw contains every date, so all draws are identical, the interval
    collapses to a point and p collapses to the 1/n_boot floor. That looks like
    overwhelming evidence when it actually means "not enough data to resample",
    so it must be reported as absent rather than as a number.
    """
    dates = sorted(samples_by_date)
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
    # per-date subtotals are sufficient. Aggregating once here makes a draw
    # O(#dates) instead of O(#samples); at 200k+ pooled samples that is the
    # difference between hours and seconds, with the same statistic.
    agg = {d: (
        sum(abs(ea) for ea, _ in ps),
        sum(abs(eb) for _, eb in ps),
        len(ps),
    ) for d, ps in samples_by_date.items()}
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
    pairs = [p for ps in by_date.values() for p in ps]
    n = len(pairs)
    mae_a = sum(abs(a) for a, _ in pairs) / n
    mae_b = sum(abs(b) for _, b in pairs) / n
    wins = sum(1 for a, b in pairs if abs(a) < abs(b))
    ties = sum(1 for a, b in pairs if abs(a) == abs(b))
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


def pairwise(con, cand: str, comp: str, var: str = "t2m") -> dict:
    """Matched samples: same city, same snapshot, same target hour."""
    obs = _load_obs(con, var)
    errs: dict = defaultdict(dict)
    q = "SELECT source, city, run_time, target_time, value FROM forecasts WHERE var=? AND source IN (?,?)"
    for source, city, run_time, target_time, value in con.execute(q, (var, cand, comp)):
        truth = obs.get((city, target_time))
        if truth is None:
            continue
        if _utc(target_time) < _utc(run_time):
            continue
        errs[(city, run_time, target_time)][source] = value - truth
    by_lead: dict = defaultdict(lambda: defaultdict(list))
    pooled: dict = defaultdict(list)
    for (city, run_time, target_time), pair in errs.items():
        if cand in pair and comp in pair:
            lead_d = int((_utc(target_time) - _utc(run_time)).total_seconds() / 3600 // 24)
            date = _local_date(target_time)
            by_lead[lead_d][date].append((pair[cand], pair[comp]))
            if 1 <= lead_d <= 7:
                pooled[date].append((pair[cand], pair[comp]))
    out = {}
    for lead_d, by_date in sorted(by_lead.items()):
        if sum(len(v) for v in by_date.values()) >= 10 and len(by_date) >= 3:
            out[str(lead_d)] = _summarize_pairs(by_date)
    if pooled and sum(len(v) for v in pooled.values()) >= 10 and len(pooled) >= 3:
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
    n_runs = con.execute("SELECT count(DISTINCT run_time) FROM forecasts").fetchone()[0]
    print(f"Scoring prospective data: {n_runs} collection runs in DB")

    t2m = hourly_board(con, "t2m")
    t2m_q = hourly_board(con, "t2m", quantize=True)
    ws = hourly_board(con, "ws")
    rain_occ = rain_occurrence_board(con)
    # Extended exploratory boards (collection began 2026-08-22; they stay empty
    # until forecast/observation overlap accrues, and hourly_board copes).
    extended = {v: hourly_board(con, v) for v in ("rh", "td", "gust", "cc", "pmsl")}
    wdir = wind_direction_board(con)
    cloud_cls = cloud_class_board(con)
    obs_daily, fc_daily = daily_series(con)
    daily = daily_board(con, obs_daily, fc_daily)
    pairs = {
        (cand, comp): pairwise(con, cand, comp)
        for cand in CANDIDATES for comp in COMPETITORS
    }
    apply_significance(pairs)
    blend_pairs = {
        (cand, comp): pairwise(con, cand, comp)
        for cand in BLENDS for comp in BLEND_COMPETITORS
    }
    # Corrected within itself - a separate family, never merged with the above.
    apply_significance(blend_pairs)

    print_board("Hourly t2m by lead day", t2m)
    print_board("Hourly t2m, ALL sources rounded to integers (quantization sensitivity)", t2m_q)
    print_board("Hourly wind speed by lead day", ws, unit="m/s MAE")
    for v, unit in (("rh", "% MAE"), ("td", "degC MAE"), ("gust", "m/s MAE"),
                    ("cc", "% cloud MAE"), ("pmsl", "hPa MAE")):
        print_board(f"Hourly {v} by lead day", extended[v], unit=unit)
    print_board("Wind direction (obs wind >= 2 m/s)", wdir, unit="deg MAE")
    print_board("Cloud class hit rate (clear/partly/overcast)", cloud_cls,
                unit="accuracy, higher better", higher_better=True)
    print_board("Rain occurrence (>=0.1mm/h) by lead day", rain_occ, unit="CSI, higher better", higher_better=True)
    print_board("Daily tmin/tmax/rain by lead day", daily, unit="degC / mm MAE")
    print_pairwise(pairs, "Pairwise inference: pre-registered family")
    if any(blend_pairs.values()):
        print_pairwise(blend_pairs, "Pairwise inference: derived blends (EXPLORATORY)",
                       "conceived after seeing the data - Holm-corrected as a separate family")

    (DATA_DIR / "prospective_results.json").write_text(json.dumps({
        "hourly_t2m": t2m, "hourly_t2m_quantized": t2m_q, "hourly_ws": ws,
        "rain_occurrence": rain_occ, "daily": daily,
        **{f"hourly_{v}": extended[v] for v in extended},
        "wind_direction": wdir, "cloud_classes": cloud_cls,
        "pairwise_t2m": {f"{a}__vs__{b}": v for (a, b), v in pairs.items()},
        "pairwise_t2m_blends_exploratory": {
            f"{a}__vs__{b}": v for (a, b), v in blend_pairs.items()},
        "config": {"block_len": BLOCK_LEN, "min_days": MIN_DAYS, "alpha": ALPHA,
                   "primary": list(PRIMARY), "n_boot": N_BOOT},
    }, indent=2))
    print(f"\nSaved {DATA_DIR / 'prospective_results.json'}")


if __name__ == "__main__":
    main()
